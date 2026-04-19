# FSM（Free Space Management）空闲空间管理

## 一、FSM 解决什么问题

Heap 插入一行时，需要找到一个**剩余空间足够**的页面。朴素做法是从头扫描所有页，代价是 O(N)。FSM 用一棵多层树记录每个页面的剩余空间分级，使查找变为 O(log N)。

```
问题：INSERT 需要 300B 的空闲空间，去哪个页面？
FSM：从根往下找 → 叶子指向有 ≥300B 空间的数据页
```

---

## 二、空闲空间分级（9个等级）

FSM 不精确记录字节数，而是按**分级 listId** 记录。分级越高，可用空间越多。

```
listId  可用空间范围（BLCKSZ=8KB）
  0     0B（满页）
  1     (0,   64B]
  2     (64,  128B]
  3     (128, 256B]
  4     (256, 512B]
  5     (512, 1KB]
  6     (1KB, 2KB]
  7     (2KB, 4KB]
  8     (4KB, 8KB]  （几乎空页）
```

`GetListId(space)` 返回能容纳 `space` 字节的最小 listId。

---

## 三、核心数据结构

### FsmPage：FSM 树的中间节点

每个 FSM 页内部维护 **FSM_FREE_LIST_COUNT（9）个双向链表**，每个链表挂着"属于这个分级"的子节点（FsmNode）。

```cpp
struct FsmNode {
    PageId page;    // 指向下一层的FSM页或堆数据页
    uint16 listId;  // 当前所在的 free list
    uint16 prev;    // 链表前驱
    uint16 next;    // 链表后继
};
```

每个 FSM 页用 HWM（High Water Mark）管理 slot 使用量，`SearchSeed` 数组用于从上次位置继续搜索（避免热点）。

### FreeSpaceMapMetaPage：FSM 元数据页

```cpp
struct FreeSpaceMapMetaPage {
    uint8  numFsmLevels;                      // FSM树总层数（最多5层）
    uint16 listRange[FSM_FREE_LIST_COUNT];    // 各 list 的容量上界
    uint64 mapCount[HEAP_MAX_MAP_LEVEL];      // 各层的FSM页数
    PageId currMap[HEAP_MAX_MAP_LEVEL];       // 各层最右侧FSM页
    PageId usedFsmPage;                       // 当前extent已用FSM页
    PageId lastFsmPage;                       // 当前extent最后FSM页
    uint64 numTotalPages;                     // 总数据页数
    uint64 numUsedPages;                      // 已使用数据页数（近似）
    uint16 extendCoefficient;                 // 扩展触发系数
    TimestampTz accessTimestamp;              // 最后访问时间（支持冷FSM回收）
};
```

---

## 四、FSM 树结构

```
Meta Page
    │
    ├─ Level-2 FSM 页（每页最多 ~670 个 FsmNode）
    │       │
    │       ├─ Level-1 FSM 页
    │       │       │
    │       │       ├─ 数据页（Heap/Index）
    │       │       └─ 数据页
    │       └─ Level-1 FSM 页
    │               └─ ...
    └─ Level-2 FSM 页
            └─ ...
```

- 8KB FSM 页可存约 670 个 FsmNode
- 2层树可管理 ~45 万数据页；3层可管理 ~3 亿页
- 动态扩展：`AdjustFsmTree()` 在需要时自动增加层级

---

## 五、关键流程

### 5.1 查找可用页（GetPage）

```
PartitionFreeSpaceMap::GetPage(heapSegMetaPageId, spaceNeeded)

  targetListId = GetListId(spaceNeeded)

  从 Meta 开始，逐层向下：
    SearchPageIdOfChild(fsmPage, spaceNeeded)
      → 从 targetListId 开始扫描 free list
      → 找到 FsmNode → 进入下一层 FSM 页
      → 如果 targetListId 的 list 为空：
          升级到 targetListId+1 继续找（宁可找更大的空间）
      → 重试 retryTime 次仍找不到：
          返回 INVALID，触发 needExtensionTask=true

  最终到达叶子层 → 返回 数据页 PageId
```

### 5.2 插入后更新 FSM（UpdateSpace）

```
INSERT 完成后（页面空间减少）：
  PartitionFreeSpaceMap::UpdateSpace(pageId, newFreeSpace)

    1. newListId = GetListId(newFreeSpace)
    2. 找到该页在叶子 FSM 页中的 FsmNode
    3. 若 listId 变化：MoveNode(node, oldList → newList)
    4. 向上传播：
       若父节点所在 list 也需更新 → 递归更新父 FSM 页
    5. 关键更改写 WAL
```

### 5.3 数据页扩展（Extension）

```
GetPage 找不到满足条件的页时：
  标记 needExtensionTask = true
  → 后台任务/当前线程 扩展 tablespace
      AllocateNewPage() → 新页加入 FSM（listId=8，全空）
      AdjustFsmTree()   → 必要时增加 FSM 树层数
```

---

## 六、FSM 与 Heap 插入的完整协作

```
HeapInsertHandler::Execute()
    │
    ├─ GetBuffer(table, spaceNeeded)
    │     └─ PartitionFreeSpaceMap::GetPage(spaceNeeded)
    │             → 返回 pageId（该页有足够空间）
    │
    ├─ BufferMgr::Read(pageId, LW_EXCLUSIVE)
    │
    ├─ HeapPage::AddTuple(tuple)      ← 实际写入
    │
    └─ PartitionFreeSpaceMap::UpdateSpace(pageId, newFreeSpace)
              ← 更新 FSM 中该页的空间记录
```

---

## 七、FSM 的崩溃恢复

- FSM 所有修改（MoveNode、AdjustFsmTree 等）均写 WAL
- 崩溃恢复时，WAL Redo 重放 FSM 的 WAL 记录，恢复树结构
- Meta Page 的 `numUsedPages` 是近似值（允许轻微不一致，无需强一致）

---

## 八、设计要点总结

| 设计选择 | 原因 |
|---------|------|
| 9级分类而非精确字节 | 减少 FSM 更新频率，查找足够快 |
| 多层树（最多5层） | 8KB页管理亿级数据页，树高 ≤5 |
| SearchSeed 随机起点 | 避免多线程并发时同争一个页 |
| accessTimestamp | 支持冷 FSM 回收，节省内存 |
| UpdateSpace 向上传播 | 保证祖先 FSM 页信息始终准确 |
| WAL 记录 FSM 修改 | 崩溃恢复后 FSM 与数据页一致 |
