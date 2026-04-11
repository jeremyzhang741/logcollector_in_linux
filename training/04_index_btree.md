# DStore Index/BTree 模块培训材料

## 第一部分：BTree 基本结构

### 1.1 页面类型

`include/page/dstore_index_page.h`（第 68-73 行）：

```cpp
enum class BtrPageType {
    INVALID_BTR_PAGE = 0,
    LEAF_PAGE,      // 叶子页面：存储键值和堆行指针（TID）
    INTERNAL_PAGE,  // 内部节点：存储键值和下链指针（downlink）
    META_PAGE       // 元数据页面：根页面 ID、树高等
};
```

### 1.2 页面链接结构

`include/page/dstore_index_page.h`（第 106-219 行）：

```cpp
struct BtrPageLinkAndStatus {
    PageId btrMetaPageId;  // BTree 元数据页 ID
    PageId prev;           // 左兄弟页（INVALID 表示最左）
    PageId next;           // 右兄弟页（INVALID 表示最右）
    uint32 level;          // 层级（0 = 叶子，越往上越大）
    uint32 type  : 2;      // LEAF/INTERNAL/META
    uint32 isRoot: 1;      // 是否根页面
    uint32 splitStat : 2;  // 分裂状态（SPLIT_COMPLETE/SPLIT_INCOMPLETE）
};
```

**双向链表**：同层页面通过 prev/next 链接，叶子层支持高效范围扫描。

### 1.3 索引元组结构

`include/tuple/dstore_index_tuple.h`（第 172-203 行）：

```cpp
struct IndexTuple : public DataTuple {
    IndexLink m_link;         // 包含 heapCtid（指向堆行位置）
    uint32 m_hasNull  : 1;
    uint32 m_tupleSize: 13;
    uint32 m_tdId     : 8;   // TD 槽位 ID（MVCC 版本控制）
    uint32 m_isDeleted: 1;   // 逻辑删除标志
};
```

---

## 第二部分：TD 机制在索引中的应用

### 2.1 索引也有 TD

索引页面同样拥有 TD（Transaction Descriptor）槽位，用于版本控制：

```cpp
// include/page/dstore_index_page.h:250-268
class BtrPage : public DataPage {
    TdId AllocTd(TDAllocContext &context);
    OffsetNumber AddTuple(IndexTuple *tuple, OffsetNumber offset, uint8 tdID = INVALID_TD_SLOT);
};
```

WAL 类型 `WAL_BTREE_ALLOC_TD` 记录索引页面的 TD 分配，用于故障恢复。

### 2.2 索引 MVCC vs Heap MVCC

| 特性 | Heap MVCC | Index MVCC |
|------|----------|-----------|
| 版本存储 | 多版本存在堆页面 | 单版本 + TD 标记 |
| 可见性检查 | 遍历 Undo 链 | 查询 TD 的 CSN |
| 删除方式 | 逻辑/物理标记 | 逻辑删除（m_isDeleted 位） |
| 旧版本获取 | Undo 链重建 | 从 Undo 获取插入/删除 XID |

---

## 第三部分：索引写入流程

### 3.1 Insert 整体步骤

`include/index/dstore_btree_insert.h`：

1. **构建索引元组**：填充 key、heapCtid、初始 TD
2. **SearchBtreeForInsert()**：从 root 向下找到叶子页面的插入位置
3. **CheckUnique()**（唯一索引）：扫描叶子及右兄弟，检查重复
4. **AllocAndSetTd()**：分配 TD 槽位，写 Undo 记录
5. **写入元组**：`BtrPage::AddTuple()`
6. **生成 WAL**：`WAL_BTREE_INSERT_ON_LEAF`

### 3.2 页面分裂（Split）

当叶子页面满时触发分裂：

**四种分裂相关 WAL**：

| WAL 类型 | 说明 |
|---------|------|
| WAL_BTREE_SPLIT_LEAF | 仅分裂（数据移到右页） |
| WAL_BTREE_SPLIT_INSERT_LEAF | 分裂并同时插入新元组 |
| WAL_BTREE_NEW_LEAF_RIGHT | 创建新右页面 |
| WAL_BTREE_UPDATE_RIGHT_SIB_LINK | 更新右兄弟链接 |

**分裂并发安全**：`SPLIT_INCOMPLETE` 状态标记分裂未完成（downlink 未插入父页），恢复时检测并补全。

### 3.3 插入 WAL 记录结构

`include/index/dstore_btree_wal.h`（第 144-177 行）：

```cpp
struct WalRecordBtreeInsertOnLeaf : public WalRecordIndex {
    OffsetNumber m_offset;   // 插入位置
    uint64 m_undoRecPtr;     // Undo 记录指针（用于回滚）
    char m_rawData[];        // IndexTuple + AllocTd 数据
};
```

---

## 第四部分：索引读取流程

### 4.1 BTree 查找

```
GetRoot()              ← 从元数据缓存获取根页（fast path）
  ↓
BinarySearchOnPage()   ← 二分查找当前层
  ↓
下降到子页面           ← 内部节点：跟随 downlink
  ↓
重复直到叶子层
  ↓
叶子页面：精确定位 + 可见性判断
```

### 4.2 范围扫描

`include/index/dstore_btree_scan.h`：

```
DescendToLeaf()        ← 定位起始叶子
  ↓
ScanOnLeaf()           ← 扫描当前叶子
  ↓
WalkRightOnLeaf()      ← 跟随 next 指针到右兄弟
  ↓
继续扫描
```

**CR 页面（Consistent Read）**：扫描遇到并发修改时，创建页面一致读副本，在副本上继续扫描，不阻塞写操作。

### 4.3 MVCC 可见性（索引层）

```cpp
// include/index/dstore_btree_scan.h:242-246
Xid GetItupInsertXidFromUndo(BtrPage *page, IndexTuple *tuple, uint8 insertTdId);
Xid GetItupDeleteXidFromUndo(BtrPage *page, IndexTuple *tuple, uint8 &insertTdId);

// 判断逻辑：
// 1. 获取 insertXid（从 TD 或 Undo 链）
// 2. 获取 deleteXid（若已删除）
// 3. insertXid 对快照可见 AND deleteXid 对快照不可见 → 该元组可见
```

---

## 第五部分：索引删除流程

### 5.1 逻辑删除

```cpp
// include/tuple/dstore_index_tuple.h:229-242
inline void SetDeleted() {
    m_info.val.m_isDeleted = 1;  // 只设置标志位，不移除元组
}
```

**设计理由**：物理删除需要移动数据代价高；逻辑删除后，通过 PRUNE 操作定期回收空间。

### 5.2 删除 WAL

| WAL 类型 | 说明 |
|---------|------|
| WAL_BTREE_DELETE_ON_LEAF | 叶子页面删除 |
| WAL_BTREE_DELETE_ON_INTERNAL | 内部页面删除 downlink |
| WAL_BTREE_PAGE_PRUNE | 页面 PRUNE（物理清理） |

---

## 第六部分：完整 WAL 类型清单

### 元数据类

| WAL 类型 | 触发时机 |
|---------|---------|
| WAL_BTREE_BUILD | 索引构建 |
| WAL_BTREE_INIT_META_PAGE | 创建 BTree |
| WAL_BTREE_UPDATE_META_ROOT | 根页面变化 |
| WAL_BTREE_NEW_INTERNAL_ROOT | 创建新内部根 |
| WAL_BTREE_NEW_LEAF_ROOT | 创建新叶子根 |

### 插入类

| WAL 类型 | 说明 |
|---------|------|
| WAL_BTREE_INSERT_ON_INTERNAL | 内部页面插入 downlink |
| WAL_BTREE_INSERT_ON_LEAF | 叶子页面插入元组 |

### 分裂类

| WAL 类型 | 说明 |
|---------|------|
| WAL_BTREE_SPLIT_LEAF | 叶子分裂（不含插入） |
| WAL_BTREE_SPLIT_INSERT_LEAF | 叶子分裂并插入 |
| WAL_BTREE_SPLIT_INTERNAL | 内部页面分裂 |
| WAL_BTREE_SPLIT_INSERT_INTERNAL | 内部页面分裂并插入 |
| WAL_BTREE_NEW_LEAF_RIGHT | 创建右叶子页 |
| WAL_BTREE_NEW_INTERNAL_RIGHT | 创建右内部页 |
| WAL_BTREE_UPDATE_LEFT_SIB_LINK | 更新左兄弟链接 |
| WAL_BTREE_UPDATE_RIGHT_SIB_LINK | 更新右兄弟链接 |

### 状态更新类

| WAL 类型 | 说明 |
|---------|------|
| WAL_BTREE_ALLOC_TD | 分配 TD 槽位 |
| WAL_BTREE_UPDATE_LIVESTATUS | 更新页面存活状态 |
| WAL_BTREE_UPDATE_SPLITSTATUS | 更新页面分裂状态 |
| WAL_BTREE_UPDATE_DOWNLINK | 更新父页面 downlink |
| WAL_BTREE_ERASE_INS_FOR_DEL_FLAG | 清除删除标记的插入信息 |

---

## 第七部分：关键文件速查

| 功能 | 头文件 | 源文件 |
|------|--------|--------|
| 基础结构 | include/index/dstore_btree.h | src/index/dstore_btree.cpp |
| 插入操作 | include/index/dstore_btree_insert.h | src/index/dstore_btree_insert.cpp |
| 分裂操作 | include/index/dstore_btree_split.h | src/index/dstore_btree_split.cpp |
| 扫描操作 | include/index/dstore_btree_scan.h | src/index/dstore_btree_scan.cpp |
| 删除操作 | include/index/dstore_btree_delete.h | src/index/dstore_btree_delete.cpp |
| 页面结构 | include/page/dstore_index_page.h | src/page/dstore_index_page.cpp |
| WAL 记录 | include/index/dstore_btree_wal.h | src/index/dstore_btree_wal.cpp |
