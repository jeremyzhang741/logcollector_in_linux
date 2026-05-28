# BTree 索引模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 INSERT 路径、SPLIT 四步 WAL 协议、并发安全机制、CR 页构建、索引 TD 机制、Undo 回滚的具体实现细节。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_btree_insert.cpp` | 951 | INSERT 完整路径：搜索→唯一性检查→插入→触发 Split |
| `dstore_btree_split.cpp` | 2148 | SPLIT 全流程：页面分裂、WAL 协议、崩溃自愈 |
| `dstore_btree.cpp` | 1444 | BTree 核心框架：Search、Init、扫描接口 |
| `dstore_btree_undo.cpp` | 805 | 索引 Undo：FindUndoRecRelatedPage、回滚逻辑 |
| `dstore_btree_delete.cpp` | 768 | 索引逻辑删除与物理 Prune |

---

## 一、INSERT 完整路径（五步）

```
BtreeInsert(indexTuple):
  Step 1: FormIndexTuple          ← 从行数据提取索引键，附加 ctid 组成索引元组

  Step 2: SearchBtree(key, INSERT_MODE)
    → 检查 lastInsertionPageId 缓存   ← 顺序插入场景的快路径（无需从根搜索）
    → BtreeDescend: 从根 B 到叶
      · 每层: GetChild(key) → pageId
      · 记录 BtreeStack: {pageId, downlinkOffset}（供 Split 后找父节点）
      · 写锁只在下降到 level-1（叶子上方）时才加，之前都用共享锁

  Step 3: CheckUnique(key, ctid)
    → 在叶子及右兄弟页面扫描相同键
    → 遇到 in-progress 事务：WaitTransaction → 重检（事务完成后可能提交该键）
    → 遇到 MVCC 不可见的已删除键：跳过

  Step 4: FindInsertLoc(leafPage)
    → 检查叶子空间：若不足先 BtreePrunePage
    → 若仍不足 → 触发 SplitPage（见 §二）

  Step 5: AddTupleToLeaf(indexTuple, leafPage)
    → 两阶段：先生成 Undo 数据 + WAL 数据；再 BeginAtomicWal
    → InsertUndoRecord
    → 填回 undoRecPtr 到 WAL 记录中（Undo 地址是运行时才知道的）
    → page->InsertTuple(offset)
    → EndAtomicWal
```

### 1.1 lastInsertionPageId 缓存

对顺序递增键的批量插入，每次写入的键大概率仍在同一叶子页面：

```cpp
if (lastInsertionPageId.IsValid()) {
    leaf = TryInsertWithCache(lastInsertionPageId, key);
    if (leaf != nullptr) return;  // 命中，不用从根搜索
}
// 未命中：从根搜索
leaf = SearchBtree(key, INSERT_MODE);
lastInsertionPageId = leaf->GetPageId();
```

### 1.2 CheckUnique：跨页唯一性检查

```
while 当前页有相同键 或 StepRightIfNeeded():
    for each tuple with same key:
        if tuple.csn < snapshot.csn && !isDeleted: 唯一冲突 → ERROR
        if tuple.xid == curXid && cid < curCid: 同事务前一个 cmd 插入 → 允许
        if in_progress(tuple.xid): WaitTransaction(xid) → 重检
```

`StepRightIfNeeded`：若当前页的高键（hikey）小于查询键，说明目标可能在右兄弟（SPLIT 期间可能发生），自动向右步进。

---

## 二、SPLIT 四步 WAL 协议

### 2.1 触发条件与分裂策略

- 叶子页面空间不足（Prune 后仍不足）
- 内部节点在 SplitPage 时递归触发上层分裂（`SplitParentIfNeeded`）

分裂策略（`BtreeSplitStrategy`）：
- 默认 50% 平衡分裂
- 顺序插入场景：90/10 不对称分裂（新键全进右页，旧左页满负荷）

### 2.2 SplitPage：六步流程与四个 WAL 记录

```
SplitPage(leftPage, rightPage, newTuple):

  // 准备阶段
  ① AllocNewPage()                ← 分配新右页
  ② DetermineMedianKey()          ← 确定分裂键（中键）

  // 在一个 AtomicWal 原子块中写 4 个 WAL 记录（顺序严格固定）：
  ③ WAL 1: WAL_BTREE_SPLIT_LEAF（或 WAL_BTREE_SPLIT_INSERT_LEAF）
            ← 记录左页的变化（移走的元组列表）
  ④ WAL 2: WAL_BTREE_NEW_RIGHT
            ← 全页快照（右页完整内容）
  ⑤ WAL 3: WAL_BTREE_UPDATE_LEFT_SIB_LINK
            ← 更新原右兄弟的 leftLink 指向新右页
  ⑥ WAL 4: WAL_BTREE_UPDATE_SPLITSTATUS
            ← 标记父节点的下行链接状态 = SPLIT_INCOMPLETE

  // 更新父节点（插入新的下行链接指向右页）
  SplitParentIfNeeded()
  
  // 更新右兄弟的 leftLink
  UpdateRightSiblingLeftLink()
  
  // 标记完成
  SetSplitStatus(SPLIT_COMPLETE)   ← WAL_BTREE_UPDATE_SPLITSTATUS（更新为完成）
```

**为什么是全页快照 WAL（WAL 2）**？新右页是全新页面，Redo 只需把快照写回即可，无需重建构造过程，实现简单且高效。

### 2.3 SPLIT_INCOMPLETE 崩溃自愈

若系统在 WAL 3（更新右兄弟 leftLink）完成前崩溃：

```
StepRightIfNeeded() 遇到 SPLIT_INCOMPLETE:
    CompleteSplit():
        ① 找到父节点（沿 BtreeStack）
        ② 检查父节点是否已有新右页的下行链接
        ③ 若没有: InsertDownlink(rightPageId, splitKey) → 补上父节点项
        ④ SetSplitStatus(SPLIT_COMPLETE)
```

**关键设计**：任何线程遇到 SPLIT_INCOMPLETE 都能触发自愈，不依赖后台进程。这是 BTree 并发安全的重要保障。

---

## 三、并发安全：锁协议

### 3.1 写锁只在 level-1 获取

下降路径：
- 根 → level-2：共享读锁（不阻塞其他读/写线程）
- level-1（叶子的父层）：**升级为写锁**
- 叶子：写锁（在 level-1 写锁保护下获取）

这意味着：修改叶子时，同时持有叶子和其父节点的写锁，保证下行链接的一致性。

### 3.2 BtreeStack：记录下降路径

```cpp
struct BtreeStackItem {
    PageId pageId;
    OffsetNumber downlinkOffset;  // 在父页面中指向本页面的 downlink 位置
};
BtreeStack stack[MAX_BTREE_HEIGHT];
```

Split 后用 `stack.pop()` 找到父节点，插入新下行链接，可能递归触发父层 Split。

### 3.3 锁定顺序：始终 left→right→parent

防止死锁：写操作总是先锁左页（旧页），再锁右页（新页），再锁父页。不会出现循环等待。

---

## 四、CR 页（一致性读页面）

### 4.1 CR 页构建（MakeLeafCrPage）

```
MakeLeafCrPage(snapshot):
  ① Read(leafPage, LW_SHARED)     ← 加共享读锁
  ② memcpy to localBuffer          ← 复制页面内容到本地
  ③ Release(leafPage)              ← 立即释放读锁（读者不持锁！）
  ④ ConstructCR(localBuffer, snapshot)
     → 对 localBuffer 中每个 in-progress 或 post-snapshot 的 TD
     → 应用其 Undo 记录，将页面回滚到 snapshot 时刻
  ⑤ 放入 CR Buffer Pool           ← 供其他同快照的 scan 线程复用
```

**关键**：基础页面的读锁在 memcpy 后立即释放，写操作不被扫描阻塞。扫描线程在本地缓冲区上操作。

### 4.2 CR Buffer Pool 复用

多个 scan 线程使用相同快照时，复用同一个 CR 页：

```
LookupCrPage(pageId, snapshot.csn) → 命中 → 直接用
未命中 → MakeLeafCrPage → 放入 CR Pool → 用
```

CR 页有版本号，CSN 不匹配时重建。

---

## 五、索引 TD 机制

### 5.1 TD Slot 容量限制

每个 BTree 叶子页面最多 **128 个 TD slots**（TD_SLOT_MAX = 128）。

满后触发扩展：

```cpp
RetryAllocAndSetTdWhenSplit():
    if tdSlotId == INVALID && page->IsTdFull():
        // 策略1: SplitPage 扩展页面（新页有更多 TD 空间）
        SplitPage → 新右页有 128 个新 TD slot
        // 策略2: 等待一个事务结束后重试
        WaitForOneTransactionEnd() → retry
```

### 5.2 TdStatus 状态转换

```
NEW_OWNER (in-progress)
  ↓ 事务提交
HISTORY_OWNER (已提交，Undo 链可见但 TD 可复用)
  ↓ 所有活跃快照都比该事务新
DETACH (FROZEN，TD slot 完全释放)
```

索引 TD 的 HISTORY_OWNER 对应 Heap TD 的 TXN_STATUS_COMMITTED，DETACH 对应 TXN_STATUS_FROZEN。

### 5.3 m_isDeleted：逻辑删除

```cpp
// 逻辑删除（DELETE/UPDATE 时）：
indexTuple->SetDeleted(true);   // 仅设 bit，不移动数据

// CheckUnique 时：
if (tuple->IsDeleted() && isVisible): return invisible   // 不阻挡新插入
if (tuple->IsDeleted() && !isVisible): skip

// BtreePagePrune 时才真正物理删除
```

逻辑删除允许 MVCC 读取到旧版本，物理删除只在所有活跃快照都不再需要该版本后执行。

---

## 六、Undo 回滚：FindUndoRecRelatedPage

### 6.1 为什么 BTree 回滚需要找页面

```
回滚场景：
  事务插入了索引项 (key, ctid) 到 leafPage P
  之后系统发生了 SPLIT，P 被分裂为 P_left 和 P_right
  Undo 记录中仍然存储的是原始 P

  → 回滚时需要找到 (key, ctid) 实际在哪个页面（P_left 还是 P_right）
```

### 6.2 FindUndoRecRelatedPage 算法

```
FindUndoRecRelatedPage(pageId, key, ctid):
  page = Read(pageId)
  
  while !DoesUndoRecMatchCurrPage(page, key, ctid):
      // 元组不在当前页，向右步进（Split 将元组移到右兄弟）
      if IsPageSplit(page):
          pageId = page->rightLink
          page = Read(pageId)
          // 同时对经过的每一页回滚其 TD 信息
          UndoTdOnPassedPage(page, undoRec)
      else:
          // PANIC: 无法找到对应页面
  
  return page
```

**DoesUndoRecMatchCurrPage**：
1. 比较 hikey：key > page.hikey → 在右兄弟
2. `FindTupleOnPage(key, ctid)`：二分搜索目标元组

### 6.3 SameWithLastLeft 防止重复回滚

```cpp
// 分裂边界的元组可能同时出现在两个页面的历史记录中
if (tuple->SameWithLastLeft(lastLeftPage)) {
    // 这个元组在 Split 时属于左页，已经在处理左页时回滚过了
    skip;  // 避免在右页重复回滚
}
```

---

## 七、BTree 扫描（dstore_btree_scan.cpp）

### 7.1 扫描初始化

```
BtreeScanBegin(scanKey, snapshot):
  SearchBtree(lowKey, SCAN_MODE) → 定位起始叶子
  StoreScanPosition(startPage, startOffset)
```

### 7.2 顺序扫描：BtreeScanGetTuple

```
while true:
    tuple = GetTupleAtCurrentPos()
    if tuple == nullptr:
        StepRight() → 移到右兄弟
        if 右兄弟 == INVALID: 扫描结束
    
    if IsVisible(tuple, snapshot):
        if IsDeleted(tuple): skip（逻辑删除）
        else: return tuple
    
    AdvancePos()
```

### 7.3 与 CR 页的配合

扫描使用 CR 页（一致性读）：
- 读取的是 snapshot 时刻的页面快照
- 写操作不阻塞扫描（CR 页在本地缓冲区）
- 多个 scan 共享同一 CR 页（CR Pool 复用）

---

## 八、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| INSERT 定位 | "从根 Search" | lastInsertionPageId 缓存：顺序插入跳过根搜索 |
| 写锁时机 | "叶子加写锁" | 写锁在 level-1 获取，叶子在父保护下才加锁 |
| SPLIT WAL | "4 个 WAL 类型" | 必须在同一 AtomicWal 中，顺序严格；WAL_2 是全页快照 |
| SPLIT_INCOMPLETE | "崩溃恢复" | 任何读者线程在 StepRight 时发现都能触发自愈 |
| CR 页 | "一致性读" | 读锁在 memcpy 后立即释放；CR Pool 供同快照线程复用 |
| 索引 TD 满 | "分配新 TD" | 触发 SplitPage 扩展页面，或等一个事务释放 TD |
| m_isDeleted | "逻辑删除" | CheckUnique 跳过已删键；物理删除由 BtreePagePrune 做 |
| Undo 回滚 | "找回原页" | FindUndoRecRelatedPage 沿 rightLink 步进；SameWithLastLeft 防重复 |
| CheckUnique 并发 | "等待写者" | WaitTransaction + 重检；StepRight 追踪 SPLIT_INCOMPLETE |
| 唯一性检查范围 | "当前页" | 实际跨越右兄弟链，直到 hikey > 查询键为止 |
