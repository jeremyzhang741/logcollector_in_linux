# Day 8 — Index / BTree 模块

## 目录

1. [BTree 模块总览](#1-btree-模块总览)
2. [BtrPage 物理布局](#2-btrpage-物理布局)
3. [BtrPageLinkAndStatus：页面的"特殊区"](#3-btrpagelinkandstatus页面的特殊区)
4. [BtrMeta：BTree 元数据页](#4-btrmeta-btree-元数据页)
5. [IndexTuple 结构](#5-indextuple-结构)
6. [BTree MVCC：与 Heap 的本质差异](#6-btree-mvcc与-heap-的本质差异)
7. [插入流程：BtreeInsert](#7-插入流程btreeinsert)
8. [页面分裂（SMO）：BtreeSplit](#8-页面分裂smoBtreeSplit)
9. [扫描流程：SearchBtree + ScanOnLeaf](#9-扫描流程searchbtree--scanonleaf)
10. [删除与 Prune](#10-删除与-prune)
11. [Undo 体系：BTree Undo](#11-undo-体系btree-undo)
12. [WAL 记录体系](#12-wal-记录体系)
13. [页面生命周期：BtrPageLiveStatus](#13-页面生命周期btrpagelivestatuss)
14. [完整插入时序图](#14-完整插入时序图)
15. [与前序模块的连接点](#15-与前序模块的连接点)
16. [快速查阅表](#16-快速查阅表)

---

## 1. BTree 模块总览

```
BTree 模块职责：
  ├─ 按序存储索引键 + 堆行指针（heapCtid）
  ├─ 支持点查（精确匹配）和范围扫描
  ├─ 并发安全的页面分裂（SMO）
  ├─ 索引级 MVCC（基于 TD + Undo，但机制不同于 Heap）
  └─ 死亡索引项的逻辑删除 + Prune 回收

与 Heap 的核心差别：
  ┌──────────────────────────────────┬────────────────────────────────┐
  │          Heap MVCC               │         BTree MVCC             │
  ├──────────────────────────────────┼────────────────────────────────┤
  │ 多版本共存（旧版本在 Undo）        │ 单条索引项 + TD 标记          │
  │ 可见 = insertXid 可见 AND        │ 可见 = insertXid 可见 AND      │
  │        deleteXid 不可见           │        deleteXid 不可见        │
  │ 行数据在页面中直接存储            │ 只存键 + heapCtid（指向 Heap）  │
  │ 用 ConstructCrTuple 重建历史版本  │ 用 GetItupInsert/DeleteXid 查 Undo│
  └──────────────────────────────────┴────────────────────────────────┘

类继承层次：
  DataPage（基础）
    └─ BtrPage（BTree 页面）

  Btree（基础逻辑）
    └─ BtreeSplit（分裂能力）
        └─ BtreeInsert（插入能力）
```

---

## 2. BtrPage 物理布局

```
BtrPage（8KB，BLCKSZ）
  │
  ├─ Page（基础头，48B）
  │   ├─ m_lsn / m_selfPtr / m_lower / m_upper
  │   └─ m_pageFlags
  │
  ├─ DataPageHeader
  │   └─ m_tdCount（默认4，最大128）
  │
  ├─ BtrPageHeader（紧接 DataPageHeader）
  │
  ├─ TD 数组（从 data 区开始）
  │   └─ TD[0] ~ TD[tdCount-1]（每个 48B，与 HeapPage 相同）
  │
  ├─ ItemId 数组（从 m_lower 向上增长）
  │   ├─ ItemId[1] = HIKEY（该页的高键，仅非最右页有效）
  │   ├─ ItemId[2] = 第一个数据键
  │   └─ ItemId[3..N] = 后续键
  │
  ├─ ← 空闲空间 →
  │
  ├─ IndexTuple 数据区（从 m_upper 向下增长）
  │
  └─ BtrPageLinkAndStatus（页面尾部"特殊区"，MAXALIGN(sizeof(BtrPageLinkAndStatus))B）
      ├─ btrMetaPageId（所属 BTree 的 Meta 页 ID）
      ├─ prev / next（左右兄弟页）
      ├─ level（0=叶子，越大越接近根）
      └─ status（type/isRoot/liveStat/splitStat，各 2bit）
```

**关键设计**：BtrPageLinkAndStatus 在页面**尾部**（Special Area），通过 `GetSpecialOffset()` 定位，不在页面头部，避免与数据区的变长增长冲突。

### HIKEY（高键）的作用

```
内部节点页面：HIKEY = 该页最大键（分裂后遗留）
  → 向右搜索时，如果目标键 > HIKEY，说明在右兄弟页面

最右页（IsRightmost=true）：无 HIKEY
  → GetFirstDataOffset() = BTREE_PAGE_FIRSTKEY(2)
非最右页：有 HIKEY
  → GetFirstDataOffset() = BTREE_PAGE_HIKEY(1)  （从 offset=1 开始）

每页填充因子：
  叶子页：BTREE_DEFAULT_FILLFACTOR = 90%
  内部页：BTREE_NONLEAF_FILLFACTOR = 70%
  单值填充：BTREE_SINGLEVAL_FILLFACTOR = 96%
```

### 关键约束

```cpp
// 每页至少 2 个 IndexTuple（加 HIKEY 最多可使用数据区的规则）
constexpr int MIN_CTID_PER_BTREE_PAGE = 2;
constexpr uint32 MAX_INDEXTUPLE_SIZE_ON_BTREE_PAGE =
    DATA_SIZE_ON_BTREE_PAGE / (1 + 2) - sizeof(ItemId) - sizeof(ItemPointerData);
```

---

## 3. BtrPageLinkAndStatus：页面的"特殊区"

```cpp
// include/page/dstore_index_page.h:106
struct BtrPageLinkAndStatus {
    PageId btrMetaPageId;  // 所属 BTree 的 Meta 页 ID（用于验证页面归属）
    PageId prev;           // 左兄弟页（INVALID_PAGE_ID = 最左页）
    PageId next;           // 右兄弟页（INVALID_PAGE_ID = 最右页）
    uint16 level;          // 树层级（0 = 叶子）
    union {
        uint32 stat;
        struct {
            uint32 type      : 2;   // BtrPageType（LEAF/INTERNAL/META）
            uint32 isRoot    : 1;   // 是否当前根页
            uint32 liveStat  : 2;   // BtrPageLiveStatus（页面存活状态）
            uint32 splitStat : 2;   // BtrPageSplitStatus（分裂完成状态）
            uint32 reserved  : 25;
        } bitVal;
    } status;
} PACKED;
```

### BtrPageType 三种页面类型

| 类型 | 说明 |
|------|------|
| `LEAF_PAGE` | 叶子页：存储 `(key, heapCtid)` 对，level=0 |
| `INTERNAL_PAGE` | 内部节点：存储 `(key, downlink→子页)` 对，level≥1 |
| `META_PAGE` | 元数据页：存储根页位置、树高、索引 schema 等 |

---

## 4. BtrMeta：BTree 元数据页

```cpp
// include/page/dstore_index_page.h:470
struct BtrMeta {
    PageId rootPage;              // 真正的根页 ID
    uint32 rootLevel;             // 根页层级
    PageId lowestSinglePage;      // "快速根"：最低的单页层（减少下降层数）
    uint32 lowestSinglePageLevel; // 快速根层级

    // 索引 Schema（用于 Undo 回滚时重建比较函数）
    uint16 nkeyAtts;              // 索引键列数
    uint16 natts;                 // 总属性数（含 include 列）
    int16  indexOption[INDEX_MAX_KEY_NUM];  // 排序选项（DESC/NULLS_FIRST）
    Oid    attTypeIds[INDEX_MAX_KEY_NUM];
    int16  attlen[INDEX_MAX_KEY_NUM];
    bool   attbyval[INDEX_MAX_KEY_NUM];
    Oid    opcinTypes[INDEX_MAX_KEY_NUM];   // 操作符类型（比较函数用）
    Oid    functionOids[INDEX_MAX_KEY_NUM * BTREE_SUPPORT_FUNC_NUM];
    Xid    createXid;             // BTree 创建时的 XID（用于 Undo 验证）
    char   relKind;               // 关系类型
    uint64 operCount[BTR_OPER_MAX][BTREE_HIGHEST_LEVEL]; // 统计计数
} PACKED;
```

### 快速根（lowestSinglePage）

```
问题：大量删除后，BTree 可能出现多个单页层（每层只有一个页面）
      正常从根下降要经过许多无意义的单页层

解决：lowestSinglePage = 最低的单页层
      普通操作从 lowestSinglePage 开始而非真根，减少下降层数

维护：根页分裂时更新；页面合并后也相应更新
```

### BtreeStorageMgr：BTree 的存储管理入口

```cpp
class BtreeStorageMgr {
    PageId m_segMetaPageId;  // Segment 元数据页
    PageId m_btrMetaPageId;  // BTree Meta 页
    IndexSegment *m_segment; // 底层存储段

    BtrMeta *GetBtrMeta(LWLockMode, BufferDesc **);
    RetStatus GetNewPage(BtreePagePayload &, ...);     // 分配新页
    RetStatus PutIntoRecycleQueue(RecyclablePage);     // 空页放入回收队列
    RetStatus GetFromRecycleQueue(PageId &, CommitSeqNo minCsn); // 从回收队列取页
};
```

---

## 5. IndexTuple 结构

```cpp
// include/tuple/dstore_index_tuple.h:172
struct IndexTuple : public DataTuple {
    IndexLink m_link;         // heapCtid（指向 Heap 行的物理地址）
    uint32 m_hasNull    : 1;  // 是否有 NULL 列
    uint32 m_tupleSize  : 13; // IndexTuple 总大小
    uint32 m_tdId       : 8;  // TD 槽位 ID（MVCC）
    uint32 m_isDeleted  : 1;  // 逻辑删除标志（DELETE 时设置）
    // 后接变长数据：NULL bitmap + 索引键值
};
```

**关键字段说明**：

| 字段 | 含义 |
|------|------|
| `m_link.heapCtid` | 指向 Heap 中对应行的 `(FileId, BlockNum, Offset)` |
| `m_tdId` | 插入/删除该索引项的事务的 TD 槽，MVCC 入口 |
| `m_isDeleted` | 1=已逻辑删除，0=存活；删除时只改此位 |

**索引 vs 堆的可见性核心差异**：索引不存旧版本数据，只存"当前最新"的 `(key, heapCtid)`，通过两个时间点（insert XID 和 delete XID）判断对快照的可见性。

---

## 6. BTree MVCC：与 Heap 的本质差异

### 6.1 索引可见性公式

```
IndexTuple 对快照 S 可见，当且仅当：
  insertXid 对 S 可见（行已被插入）
  AND
  deleteXid 对 S 不可见（行未被删除，或删除尚未提交）
```

### 6.2 获取 insertXid 和 deleteXid

```cpp
// include/index/dstore_btree_scan.h
Xid GetItupInsertXidFromUndo(BtrPage *page, IndexTuple *tuple, uint8 insertTdId);
Xid GetItupDeleteXidFromUndo(BtrPage *page, IndexTuple *tuple, uint8 &insertTdId);
```

```
获取 insertXid：
  → TD[tuple.m_tdId].xid（若 ATTACH_TD_AS_NEW_OWNER）
  → 或从 Undo 链读取（若 ATTACH_TD_AS_HISTORY_OWNER）

获取 deleteXid（仅当 m_isDeleted=1 时需要）：
  → 从 Undo 反向追溯，找到设置 m_isDeleted=1 的事务 XID
  → 用于判断删除是否对当前快照可见
```

### 6.3 与 Heap 的对比

```
Heap 读取行 R（快照 S）：
  TD[tdId].xid = T2（不可见）
  → ConstructCrTuple（Undo 链回溯，重建 T1 写的数据）
  → 返回重建的历史数据

BTree 读取 (key, heapCtid)（快照 S）：
  insertXid = T1（可见，T1 已提交且 csn < S.csn）
  deleteXid = T2（不可见，T2 未提交）
  → 索引项可见，返回 (key, heapCtid) → 再去 Heap 读行数据
  （BTree 无需重建历史版本，只需判断可见性）
```

### 6.4 唯一索引冲突检查（CheckUnique）

```
CheckUnique(waitXid)：

  在叶子页上扫描相同 key 的索引项：
    for each itup with same key:
      if itup.isDeleted:
        continue   // 已删除，不冲突

      insertXid = GetItupInsertXidFromUndo(...)
      if XidIsAborted(insertXid):
        continue   // 插入者已回滚，不冲突

      if XidIsCommitted(insertXid) AND NOT XidIsVisible(insertXid, snapshot):
        continue   // 插入者已提交但对当前快照不可见（旧快照）

      if XidIsInProgress(insertXid):
        *waitXid = insertXid  // 需要等待冲突事务结束
        return CONFLICT_WAIT

      return CONFLICT_FOUND   // 唯一冲突
  
  // 同时检查右兄弟（StepRightWhenCheckUnique），因为分裂可能将相同 key 挪到右页
```

---

## 7. 插入流程：BtreeInsert

### 7.1 类继承结构

```
Btree（基础：SearchBtree, StepRight, BinarySearch）
  └─ BtreeSplit（SMO：SplitPage, CompleteSplit, SplitAndAddDownlink）
      └─ BtreeInsert（主流程：InsertTuple, CheckUnique, AddTupleToLeaf）
```

### 7.2 InsertTuple() 完整流程

```
BtreeInsert::InsertTuple(values, isnull, heapCtid)

  Step 1: FormIndexTuple(values, isnull, heapCtid, &insertTuple)
            构建 IndexTuple（填充键值 + heapCtid + m_isDeleted=0）

  Step 2: SearchBtreeForInsert()
            从 lowestSinglePage 向下二分搜索：
              SearchBtree() → BinarySearch() → 找到叶子插入位置
              维护 m_leafStack（从根到叶子的路径，用于分裂时更新父页）

  Step 3: CheckUnique()（仅唯一索引）
            在叶子页扫描相同 key → 发现冲突 → 报错或等待
            StepRightWhenCheckUnique() → 检查右兄弟（防漏检）

  Step 4: AddTupleToLeaf()
    ├─ 4.1: AllocAndSetTd(btrPage, insertTuple)
    │         → BtrPage::AllocTd(context)（与 Heap 相同的 TryReuse 机制）
    │         → SetTd(tdId, xid, undoRecPtr, cid)
    │         → insertTuple.m_tdId = tdId
    │
    ├─ 4.2: InsertUndoRecAndSetTd(tdId, insOff, insPage, undoRecord)
    │         → 构建 UndoRecord（UNDO_BTREE_INSERT）
    │         → InsertUndoRecord(record) → undoRecPtr
    │         → 更新 TD[tdId].undoRecPtr = undoRecPtr
    │
    ├─ 4.3: BtrPage::AddTuple(insertTuple, insertOff, tdId)
    │         → 写入 IndexTuple 到页面（类似 HeapPage::AddTuple）
    │
    └─ 4.4: GenerateLeafInsertWal()
              → WalRecordBtreeInsertOnLeaf { m_offset, m_undoRecPtr, rawData[] }
              → EndAtomicWal() + WaitTargetPlsnPersist()
              → MarkDirty()
```

### 7.3 BtrStack：路径追踪

```cpp
struct BtrStackData {
    ItemPointerData currItem;    // 当前节点的 (pageId, offset)
    BtrStackData   *parentStack; // 父节点指针（链式栈）
};

// SearchBtree() 每下降一层都 push 一个 BtrStackData：
stack = BtrStackData::SaveNewStack(currPageId, currOffset, higherLevelStack)

// 分裂需要插入 downlink 到父页时：
// CompleteSplit() / AddPageDownlinkToParent() 通过 m_leafStack 找到父页
```

---

## 8. 页面分裂（SMO）：BtreeSplit

### 8.1 分裂触发条件

```
AddTupleToLeaf() 发现叶子页满（freeSpace < tupleSize + ItemId）
  → SplitAndAddDownlink(insTuple, insertOff, stack)
```

### 8.2 分裂全流程（SMO）

```
BtreeSplit::SplitPage(insTuple, insOff, childBuf)

  Phase 1: 准备分裂
    PrepareSplittingAndRightPage(leftPage, insTuple, splitCxt, &oldRBuf)
      ├─ 计算分裂点（firstRightOff），使左右页大致均等
      ├─ 创建 memLeftPage（内存临时左页副本）
      └─ 获取右页：从 RecycleQueue 或 AllocNewPage()

  Phase 2: 标记分裂未完成（原子性保证）
    leftPage.SetSplitStatus(SPLIT_INCOMPLETE)
    WAL: WAL_BTREE_UPDATE_SPLITSTATUS
    ← 此时崩溃：恢复时检测到 SPLIT_INCOMPLETE，补全分裂

  Phase 3: 构建新右页
    rightPage.InitNewRightForSplit(memLeft)
      → 复制 memLeftPage 右半部分到 rightPage
      → 更新 rightPage 的 prev = leftPageId
      → 更新 rightPage 的 next = leftPage.next（原右兄弟）
    WAL: WAL_BTREE_NEW_LEAF_RIGHT（或 NEW_INTERNAL_RIGHT）

  Phase 4: 更新原右兄弟的 prev 指针
    oldRightPage.prev = newRightPageId
    WAL: WAL_BTREE_UPDATE_RIGHT_SIB_LINK

  Phase 5: 修改左页（移除右半部分）
    leftPage.InitMemLeftForSplit()
      → 保留左半部分键，右半部分已移到右页
      → 保留 HIKEY = 分裂键（右页第一个键）
      → 更新 leftPage.next = newRightPageId
    WAL: WAL_BTREE_SPLIT_INSERT_LEAF（含插入元组）或 WAL_BTREE_SPLIT_LEAF

  Phase 6: 标记分裂完成
    leftPage.SetSplitStatus(SPLIT_COMPLETE)
    （通常合并在 Phase 5 的 WAL 中）

SplitAndAddDownlink(insTuple, insertOff, stack):
  SplitPage()  ← 上述分裂
  AddPageDownlinkToParent(stack, isRoot)
    → 将 (HIKEY, newRightPageId) 作为 downlink 插入父页
    → 父页也满 → 递归分裂父页
    → 若根页分裂 → 创建新根（WalRecordBtreeNewInternalRoot）
```

### 8.3 SPLIT_INCOMPLETE 并发安全机制

```
SPLIT_INCOMPLETE 的作用：
  问题：分裂过程中崩溃，新右页存在但父页 downlink 未插入
        → 正常搜索找不到新右页的键
        → 数据可以访问（通过左页 next 指针），但树结构不完整

解决：
  CompleteSplit(splitBuf, stack, access)：
    → 检测到 splitStat == SPLIT_INCOMPLETE
    → 读取右页的 HIKEY（分裂键）
    → 向父页插入 downlink（补全分裂）
    → splitStat → SPLIT_COMPLETE

触发时机：
  任何搜索操作遇到 SPLIT_INCOMPLETE 页面时，都会先调用 CompleteSplit
  → 搜索 + 自愈，无需专门的恢复 worker
```

### 8.4 分裂时 TD 的处理

```cpp
struct BtreeTdSplitInfo {
    uint8 origId;  // 原左页的 TD ID
    uint8 newId;   // 新右页的 TD ID
    TD *td;        // TD 内容
};

// CopyTd(info)：将左页 TD 复制到右页对应位置
// 原因：分裂时，左页上的某些 TD 槽的数据移到了右页，
//       这些 TD 必须同步复制，否则右页上的 IndexTuple 找不到对应 TD
```

---

## 9. 扫描流程：SearchBtree + ScanOnLeaf

### 9.1 点查（精确搜索）

```
BtreeSplit::SearchBtree(pageBuf, strictlyGreaterThan, ...)

  从 BtrMeta.lowestSinglePage 开始：
    loop:
      BinarySearch(page, searchKey, &isEqual)
        → 在当前页二分查找，返回 offset（目标位置）
      
      if page.IsLeaf():
        break  // 找到叶子层，offset 就是插入/查询位置
      
      downlink = page.GetIndexTuple(offset).heapCtid  // 内部页存的是子页 ID
      childPageId = downlink.GetPageId()
      
      stack = BtrStackData::SaveNewStack(currPageId, offset, stack)
      pageBuf = BufMgr::Read(childPageId, LW_SHARED/EXCLUSIVE)
      
      StepRightIfNeeded(pageBuf, ...)
        → 并发分裂导致目标键移到右页？
        → 检查 HIKEY：key > HIKEY → 向右移动
```

### 9.2 范围扫描

```
BtreeScan::DescendToLeaf()  → 定位起始叶子页和起始 offset

ScanOnLeaf(leafBuf, startOff)：
  for offset = startOff to maxOffset:
    itup = leafPage.GetIndexTuple(offset)

    // 跳过条件
    if itup.isDeleted: continue  // 已逻辑删除
    if 不满足 scanKey: continue or break（SCANKEY_STOP_FORWARD）

    // MVCC 可见性判断
    insertXid = GetItupInsertXidFromUndo(page, itup, itup.m_tdId)
    if NOT XidVisibleToSnapshot(snapshot, insertXid): continue

    if itup.isDeleted:
      deleteXid = GetItupDeleteXidFromUndo(page, itup, ...)
      if XidVisibleToSnapshot(snapshot, deleteXid): continue

    // 可见 → 返回 heapCtid → 去 Heap 取行数据
    yieldResult(itup.heapCtid)

WalkRightOnLeaf()：
  当前页扫描完 → pageBuf = BufMgr::Read(page.next, LW_SHARED)
  → 继续 ScanOnLeaf()
```

### 9.3 CR 页面（Consistent Read on BTree）

```
并发修改导致扫描页面数据不一致时：
  BtrPage::ConstructCR(transaction, crCtx, btrUndoContext, bufMgr)
    → 类似 Heap 的 ConstructCrTuple，但操作对象是 BtrPage
    → 在内存临时副本上回退索引键的修改
    → 在 CR 副本上继续扫描（不阻塞写操作）

设计意义：索引层也需要 CR 页，保证并发读的一致性快照
```

### 9.4 SCANKEY_STOP_FORWARD / BACKWARD

```cpp
// 扫描提前终止标志
SCANKEY_STOP_FORWARD  = 0x00010000  // 正向扫描：不匹配就停止
SCANKEY_STOP_BACKWARD = 0x00020000  // 逆向扫描：不匹配就停止

// 用于范围扫描的边界条件（如 WHERE x > 10 AND x < 100）
// 遇到 x >= 100 的键 → STOP_FORWARD → 立即结束扫描
```

---

## 10. 删除与 Prune

### 10.1 逻辑删除

```cpp
// include/tuple/dstore_index_tuple.h
inline void IndexTuple::SetDeleted() {
    m_info.val.m_isDeleted = 1;  // 只设标志位，不移除索引项
}
```

**逻辑删除流程**：
```
DELETE heap row → UndoHeap → HeapPage 修改 liveMode
  → 同时：BTree 逻辑删除对应 IndexTuple
      1. AllocAndSetTd(page, itup)
      2. InsertUndoRecAndSetTd（UNDO_BTREE_DELETE）
      3. itup.SetDeleted()（m_isDeleted = 1）
      4. WAL: WAL_BTREE_DELETE_ON_LEAF
      5. MarkDirty()
```

**为何用逻辑删除**：
- 物理删除需要移动 ItemId，代价高
- 逻辑删除只改一个 bit，非常快
- Prune 操作定期清理，摊还成本

### 10.2 Prune（物理清理）

```
BtrPrune::PrunePage(btrPage, minCsn)

  判断条件：
    对每个 itup（m_isDeleted=1）：
      deleteXid = GetItupDeleteXidFromUndo(...)
      if deleteXid 已提交 AND deleteXid.csn < minCsn:
        → 所有活跃快照都不再需要此索引项（minCsn 保证）
        → 可安全物理删除

  物理删除：
    BtrPage::RemoveItemId(offset)
    生成 WAL: WAL_BTREE_PAGE_PRUNE
    → 空间归还后可被新插入使用
```

### 10.3 页面变空后的处理

```
最后一个 IndexTuple 被 Prune 后：
  liveStat: NORMAL_USING → EMPTY_HAS_PARENT_HAS_SIB
  WAL: WAL_BTREE_UPDATE_LIVESTATUS

  → 后台任务检测到此状态
  → BtrPageUnlink：从父页删除 downlink
  → liveStat: → EMPTY_NO_PARENT_HAS_SIB
  → 从兄弟链移除 prev/next 指针
  → liveStat: → EMPTY_NO_PARENT_NO_SIB
  → 放入 RecycleQueue（可供后续插入复用）
```

---

## 11. Undo 体系：BTree Undo

### 11.1 BTree Undo 类型

```cpp
// include/undo/dstore_undo_types.h
UNDO_BTREE_INSERT  // 插入索引项（回滚时删除该项）
UNDO_BTREE_DELETE  // 删除索引项（回滚时恢复该项）
// 对应临时表版本：
UNDO_BTREE_INSERT_TMP
UNDO_BTREE_DELETE_TMP
```

### 11.2 UndoDataBtreeInsert（插入 Undo 数据）

```cpp
// include/index/dstore_btree_undo_data_struct.h:45
struct UndoDataBtreeInsert : public UndoData {
    bool   m_hasNull;       // 索引键是否有 NULL
    bool   m_hasVariable;   // 是否有变长列
    bool   m_ins4Del;       // 是否是"为删除而插入"（特殊场景）
    uint64 m_heapCtid;      // 堆行地址（回滚时验证）
    Xid    m_metaCreateXid; // BTree 创建 XID（验证 Undo 属于正确的索引）
    char   m_rawData[];     // NULL bitmap + 索引键值数据
};
```

### 11.3 BTree Undo 的特殊性

```
与 Heap Undo 的关键差异：

Heap Undo 存旧行数据：
  INSERT → Undo 无数据（INSERT 回滚 = 删除行）
  DELETE → Undo 存旧行完整数据
  UPDATE → Undo 存旧行数据

BTree Undo 存索引键（用于重建 IndexTuple）：
  INSERT → Undo 存 (key, heapCtid)（回滚时用来找到并删除此 IndexTuple）
  DELETE → Undo 存 (key, heapCtid)（回滚时用来重新插入此 IndexTuple）

BTree 回滚入口：
  BtrPage::UndoBtree(undoRec, btrUndoContext)
    → UNDO_BTREE_INSERT: UndoBtreeInsert → 找到 IndexTuple 并 SetDeleted 或物理移除
    → UNDO_BTREE_DELETE: UndoBtreeDelete → 重新插入 IndexTuple（m_isDeleted=0）
```

### 11.4 BtreeUndoContext

```cpp
struct BtreeUndoContext {
    // 包含用于 Undo 操作所需的上下文：
    // - IndexInfo（索引 schema，比较函数等）
    // - BtreeStorageMgr（存储管理）
    // - 缓冲管理
};
```

---

## 12. WAL 记录体系

### 12.1 WAL 继承层次

```
WalRecord（4B: size + type）
  └─ WalRecordForPage（+ pageId + flags + preWal 三元组）
      └─ WalRecordForDataPage（+ AllocTd 扩展）
          └─ WalRecordIndex（BTree WAL 基类）
              ├─ WalRecordBtreeBuild            WAL_BTREE_BUILD
              ├─ WalRecordBtreeInitMetaPage      WAL_BTREE_INIT_META_PAGE
              ├─ WalRecordBtreeNewInternalRoot   WAL_BTREE_NEW_INTERNAL_ROOT
              ├─ WalRecordBtreeNewLeafRoot       WAL_BTREE_NEW_LEAF_ROOT
              ├─ WalRecordBtreeInsertOnLeaf      WAL_BTREE_INSERT_ON_LEAF
              ├─ WalRecordBtreeInsertOnInternal  WAL_BTREE_INSERT_ON_INTERNAL
              ├─ WalRecordBtreeSplitLeaf         WAL_BTREE_SPLIT_LEAF / SPLIT_INSERT_LEAF
              ├─ WalRecordBtreeNewLeafRight      WAL_BTREE_NEW_LEAF_RIGHT
              ├─ WalRecordBtreeUpdateSibLink     WAL_BTREE_UPDATE_LEFT/RIGHT_SIB_LINK
              ├─ WalRecordBtreeDeleteOnLeaf      WAL_BTREE_DELETE_ON_LEAF
              ├─ WalRecordBtreePagePrune         WAL_BTREE_PAGE_PRUNE
              ├─ WalRecordBtreeAllocTd           WAL_BTREE_ALLOC_TD
              └─ WalRecordBtreeUpdateLiveStatus  WAL_BTREE_UPDATE_LIVESTATUS
```

### 12.2 WalRecordBtreeInsertOnLeaf

```cpp
struct WalRecordBtreeInsertOnLeaf : public WalRecordIndex {
    OffsetNumber m_offset;    // 插入位置（ItemId 序号）
    uint64       m_undoRecPtr;// Undo 记录地址（用于回滚）
    char         m_rawData[]; // IndexTuple 数据 + AllocTd 信息
};
// Redo：在 m_offset 处插入 m_rawData 中的 IndexTuple
```

### 12.3 WalRecordBtreeAllocTd（与 Heap 对应）

```cpp
// BTree 也记录 TD 分配的 WAL，用于崩溃恢复后正确重建 TD 状态
// WAL_BTREE_ALLOC_TD：记录哪些 TD 被分配、哪些被回收
// 内嵌在 WalRecordForDataPage::AllocTdRecord 中（Day5 曾提到）
```

### 12.4 分裂相关 WAL 的原子性

```
分裂操作涉及多个页面（左页、右页、父页），必须确保崩溃后可恢复：

方案：先写 WAL_BTREE_UPDATE_SPLITSTATUS（INCOMPLETE）
      → 中间各步骤各有独立 WAL
      → 最后 WAL_BTREE_SPLIT_INSERT_LEAF 标志分裂完成

崩溃恢复：
  发现 SPLIT_INCOMPLETE → CompleteSplit() 补全
  各步骤 WAL 独立 Redo → 幂等操作
```

---

## 13. 页面生命周期：BtrPageLiveStatus

```
BtrPageLiveStatus 四态：

  NORMAL_USING（1）
    正常使用中，有索引数据
    ↓ 最后一个 tuple 被 Prune
    ↓ WAL: WAL_BTREE_UPDATE_LIVESTATUS

  EMPTY_HAS_PARENT_HAS_SIB（2）
    已空，但仍在树中（父页有 downlink，兄弟页有链接）
    注意：此状态 ≠ 真的空，可能有新插入！
    ↓ BtrPageUnlink：从父页删除 downlink

  EMPTY_NO_PARENT_HAS_SIB（3）
    已空，父页 downlink 已删，但兄弟链接仍存在
    正常搜索找不到此页（无 downlink 入口）
    ↓ 从兄弟链移除（更新相邻页的 prev/next）

  EMPTY_NO_PARENT_NO_SIB（0）
    完全从树中移除，可立即复用
    ↓ PutIntoRecycleQueue()
    → 等待 recycleMinCsn 保证所有快照都不再需要此页
    → GetFromRecycleQueue() 取出复用
```

**EMPTY_HAS_PARENT_HAS_SIB 的注意事项**：
```
此状态设置后，仍可能有新插入（设置 liveStat 不是原子锁）
复用此页前必须检查页面是否真的为空（GetNonDeletedTupleNum() == 0）
```

---

## 14. 完整插入时序图

```
用户执行 INSERT INTO t VALUES(10, 'abc') → 同时维护索引 idx_t_col1

Heap 插入（见 Day7）
  → heapCtid = (FileId=1, BlockNum=5, Offset=3)

BTree 插入（对应索引键 10）：
│
├─ 1. FormIndexTuple(values=[10], heapCtid)
│       IndexTuple = {heapCtid=(1,5,3), m_isDeleted=0, m_tdId=INVALID}
│
├─ 2. SearchBtreeForInsert()
│       从 lowestSinglePage 向下二分搜索
│       BranchStack: [Level2_page → Level1_page → LeafPage]
│       找到插入位置：leafPage, offset=7
│
├─ 3. CheckUnique()（若唯一索引）
│       扫描 offset=7 附近相同 key 的项
│       StepRightWhenCheckUnique → 检查右兄弟
│       无冲突 → 继续
│
├─ 4. AllocAndSetTd(leafPage, insertTuple)
│       BtrPage::AllocTd(tdContext)  → tdId=2
│       insertTuple.m_tdId = 2
│
├─ 5. InsertUndoRecAndSetTd(tdId=2, offset=7, leafPage, undoRecord)
│       UndoRecord(UNDO_BTREE_INSERT, key=10, heapCtid=(1,5,3))
│       UndoZone::InsertUndoRecord → undoRecPtr=U_btree
│       TD[2].undoRecPtr = U_btree
│
├─ 6. BtrPage::AddTuple(insertTuple, offset=7, tdId=2)
│       写入 IndexTuple 到叶子页
│       ItemId[7] 指向 IndexTuple 位置
│
├─ 7. GenerateLeafInsertWal()
│       WalRecordBtreeInsertOnLeaf { offset=7, undoRecPtr=U_btree, rawData }
│       EndAtomicWal() → WalGroupLsnInfo
│       WaitTargetPlsnPersist()（同步 commit）
│
└─ 8. MarkDirty(leafPageBufDesc)
       BgDiskPageWriter 异步刷盘
       recoveryPlsn 登记

──── 若叶子页满，触发分裂 ────
│
├─ A. SplitPage()
│       InitNewRightForSplit（右页接收右半部分）
│       更新 leftPage.next → rightPageId
│       WAL: NEW_LEAF_RIGHT + UPDATE_SIB_LINK + SPLIT_INSERT_LEAF
│
└─ B. AddPageDownlinkToParent(stack)
        (key=midKey, downlink=rightPageId) 插入父页
        父页满 → 递归分裂父页 → ... → 可能新建根页
        WAL: INSERT_ON_INTERNAL / NEW_INTERNAL_ROOT
```

---

## 15. 与前序模块的连接点

### 15.1 与 Day 2（Page 结构）

| Day2 概念 | Day8 中的体现 |
|----------|--------------|
| ItemPointerData 三级地址 | IndexTuple.heapCtid = 指向 Heap 行的精确位置 |
| m_lower / m_upper | BtrPage 同样使用，ItemId 上增，IndexTuple 下减 |
| TD 槽（共享机制）| BtrPage 也有 TD 数组，AllocTd 机制与 HeapPage 完全一致 |

### 15.2 与 Day 3（Buffer Manager）

| Day3 概念 | Day8 中的体现 |
|----------|--------------|
| LW_SHARED / LW_EXCLUSIVE | 扫描用 SHARED，插入/分裂用 EXCLUSIVE |
| CR 页 | BtrPage::ConstructCR() — 索引层也有 CR 页机制 |
| MarkDirty | BTree 写操作最后也调用 MarkDirty |

### 15.3 与 Day 4（Transaction + MVCC）

| Day4 概念 | Day8 中的体现 |
|----------|--------------|
| XidVisibleToSnapshot | BTree 可见性 = insertXid 可见 AND deleteXid 不可见 |
| recycleMinCsn | BTree Prune：deleteXid.csn < recycleMinCsn 才物理删除 |
| CommitSeqNo | GetFromRecycleQueue() 传入 minCsn 确保可安全复用页面 |

### 15.4 与 Day 5（WAL）

| Day5 概念 | Day8 中的体现 |
|----------|--------------|
| WalRecordForDataPage | WalRecordIndex 继承自它（含 AllocTd 扩展）|
| 5步写入工作流 | BTree 插入内部也调用 BeginAtomicWal ... EndAtomicWal |
| WAL-First | GenerateWal 必须在 MarkDirty 前 |
| CRC 原子组 | 分裂涉及多页 WAL，通过 AtomicGroup 保证原子性 |

### 15.5 与 Day 6（Undo）

| Day6 概念 | Day8 中的体现 |
|----------|--------------|
| UndoRecord 两条链 | BTree Undo 也挂在事务 Undo 链上（m_txnPreUndoPtr）|
| InsertUndoRecord | BTree 也调用 InsertUndoRecord 写 UNDO_BTREE_INSERT/DELETE |
| RollbackUndoZone | 回滚时遍历 Undo 链，遇到 UNDO_BTREE_* 调用 UndoBtree() |

### 15.6 与 Day 7（Heap）

| Day7 概念 | Day8 中的体现 |
|----------|--------------|
| TupleTdStatus 三态 | IndexTuple 同样有 tdStatus，判断索引项版本归属 |
| AllocTd/TryReuseTdSlots | BtrPage::AllocTd 完全复用 DataPage 的 AllocTd 机制 |
| WAL_HEAP_DELETE | Heap DELETE 时同步触发 BTree 的 WAL_BTREE_DELETE_ON_LEAF |
| 可见性路径 | Heap GetVisibleTuple → 找到行后，BTree 提供了行的定位 |

---

## 16. 快速查阅表

### 关键常数

| 常数 | 值 | 含义 |
|------|-----|------|
| `BTREE_HIGHEST_LEVEL` | 32 | BTree 最大层数 |
| `BTREE_DEFAULT_FILLFACTOR` | 90% | 叶子页填充因子 |
| `BTREE_NONLEAF_FILLFACTOR` | 70% | 内部页填充因子 |
| `BTREE_SINGLEVAL_FILLFACTOR` | 96% | 单值填充因子 |
| `BTREE_PAGE_HIKEY` | 1 | HIKEY 的 ItemId 序号 |
| `BTREE_PAGE_FIRSTKEY` | 2 | 数据起始序号（最右页）|
| `MIN_CTID_PER_BTREE_PAGE` | 2 | 每页最少 IndexTuple 数 |
| `MAX_RETRY_COUNT` | 10000 | 分裂最大重试次数 |
| `BTREE_MAGIC` | 0xEBEBEB... | BTree 验证魔数 |

### 核心 API 速查

| 操作 | API | 文件 |
|------|-----|------|
| BTree 插入 | `BtreeInsert::InsertTuple(values, isnull, heapCtid)` | `dstore_btree_insert.h` |
| 搜索 BTree | `BtreeSplit::SearchBtree(pageBuf, ...)` | `dstore_btree_split.h` |
| 页面分裂 | `BtreeSplit::SplitPage(insTuple, insOff, childBuf)` | `dstore_btree_split.h` |
| 补全分裂 | `BtreeSplit::CompleteSplit(splitBuf, stack, access)` | `dstore_btree_split.h` |
| 索引可见性 | `GetItupInsertXidFromUndo / GetItupDeleteXidFromUndo` | `dstore_btree_scan.h` |
| BTree Undo | `BtrPage::UndoBtree(undoRec, btrUndoContext)` | `dstore_index_page.h` |
| BTree CR 页 | `BtrPage::ConstructCR(txn, crCtx, ...)` | `dstore_index_page.h` |
| 获取 Meta | `BtreeStorageMgr::GetBtrMeta(mode, &desc)` | `dstore_btree.h` |

### 核心文件速查

| 功能 | 文件 |
|------|------|
| BtrPage / BtrMeta / BtrPageLinkAndStatus | `include/page/dstore_index_page.h` |
| BtreeStorageMgr / BtrStack / BtrScanKeyValues | `include/index/dstore_btree.h` |
| BtreeInsert（插入主流程）| `include/index/dstore_btree_insert.h` |
| BtreeSplit（SMO 分裂）| `include/index/dstore_btree_split.h` |
| BTree WAL 记录体系 | `include/index/dstore_btree_wal.h` |
| BTree Undo 数据结构 | `include/index/dstore_btree_undo_data_struct.h` |
| IndexTuple | `include/tuple/dstore_index_tuple.h` |
| BTree Prune | `include/index/dstore_btree_prune.h` |
| BTree 页面回收 | `include/index/dstore_btree_page_recycle.h` |

### 页面状态速查

```
BtrPageLiveStatus:
  EMPTY_NO_PARENT_NO_SIB(0) → 可复用
  NORMAL_USING(1)            → 正常使用
  EMPTY_HAS_PARENT_HAS_SIB(2)→ 刚变空，仍在树中（可能有新插入！）
  EMPTY_NO_PARENT_HAS_SIB(3) → 从父页移除，仍有兄弟链

BtrPageSplitStatus:
  SPLIT_COMPLETE(0)           → 正常
  SPLIT_INCOMPLETE(1)         → 分裂中断，需 CompleteSplit 补全
```

---

## 下一步

Day 9 深入 Lock + FSM + Tablespace：
- Lock Manager：行锁 / 页锁 / 表锁的层次结构
- 死锁检测与等待图（Wait-For Graph）
- FSM 与 Tablespace 的物理存储组织
- Segment / Extent 的分配管理
