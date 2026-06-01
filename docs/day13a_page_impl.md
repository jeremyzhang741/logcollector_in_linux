# Page 模块 .cpp 精读

## 1. dstore_page.cpp — 基础页面操作

### 1.1 页面 Init 的隐藏细节：LSN 保留语义

`Page::Init()` 并不是一次简单的清零。它在 `memset_sp` 全页清零前，先把 `m_glsn`、`m_plsn`、`m_walId` 保存到局部变量，清零后再写回。注释明确说明：

> "For new page: the caller must make sure the header is all-zero. For **reused** page: should retain the old glsn plsn and walId."

这是页面复用（reuse）时 WAL 一致性的关键保证——物理位置 LSN 必须单调递增，清零重用一个页面不能抹掉它已知的最新 WAL 位置。

`special` 区域的初始化也有细节：
- `m_special.m_offset = BLCKSZ - specialSize`
- `m_special.m_needCrc = (specialSize != 0) ? 0 : 1`

即有 special 区域时 **CRC 只覆盖到 special 前**（`checkSize = m_special.m_offset - sizeof(uint32)`），无 special 区域时覆盖整页。

### 1.2 校验和：FNV 算法，跳过前 4 字节

`SetChecksum()` 和 `CheckPageCrcMatch()` 均从 `this + sizeof(uint32)` 开始计算，即跳过存储校验和的那 4 字节本身，使用 `CHECKSUM_FNV` 算法，结果截断为 `uint16`。

全零页有一个特殊常量：`ALL_ZERO_PAGE_CHECKSUM = 25258`。`CheckPageCrcMatch` 允许 `checksum==0` 的旧页与该常量匹配，实现向后兼容。

### 1.3 TD::FillCsn — 惰性写回 CSN 的双状态机

```
FillCsn(transaction, inXidStatus):
  if IS_CUR_XID_CSN: return m_csn    // 已填充，快速路径
  if xid == INVALID_XID: return INVALID_CSN
  构造 XidStatus
  if Frozen/Committed:
    填 m_csn = csn
    m_csnStatus = IS_CUR_XID_CSN
    if OCCUPY_TRX_IN_PROGRESS && lockerXid == INVALID: 推进到 OCCUPY_TRX_END
  return m_csn
```

关键：仅当事务已提交或冻结时才把 CSN 持久化进 TD（需调用方持有 LW_EXCLUSIVE 锁）。`IS_PREV_XID_CSN` 状态下 CSN 不写回，直到成为新 xid 持有者。

### 1.4 TD::RollbackTdInfo — TD 回滚到 UndoRecord 里的前驱

从 `UndoRecord` 中读出 `preXid`、`preUndoPtr`、`preCsn`、`preCsnStatus`，写回 TD。状态转换规则：
- `preXid == INVALID_XID` → `UNOCCUPY_AND_PRUNEABLE`
- `preXid != currentXid` → `OCCUPY_TRX_END`
- CID 无条件回滚为 `INVALID_CID`

### 1.5 TD::RollbackTdToPreTxn — 循环沿 Undo 链回退到不同 xid

用于 B-tree 分裂场景中 aborted xid 残留在 TD 的处理：循环拉取 UndoRecord 并调用 `RollbackTdInfo`，直到 TD 的 xid 变化。若 undo 已被回收（`UNDO_ERROR_RECORD_RECYCLED`），则直接 `Reset()` 并退出。

### 1.6 ItemPointerData 的变长序列化

`GetCompressedSize()` 使用 `VarintCompress::GetUnsigned32CompressedSize`，FileId/BlockNum/Offset 三个字段分别 varint 压缩。这意味着小 BlockNum 的 CTID 存储开销远小于固定 8 字节。

---

## 2. dstore_data_page.cpp — 数据页核心逻辑

### 2.1 空闲空间计算的两种模式

```cpp
template<FreeSpaceCondition cond>
uint32 DataPage::GetFreeSpace()
```

- `RAW`：直接返回 `m_upper - m_lower`，包含 ItemId 的空间
- `EXCLUDE_ITEMID`：减去一个 `sizeof(ItemId)`，用于 insert 决策

`GetFreeSpaceForInsert()` 内部调用 `EXCLUDE_ITEMID` 版本。

### 2.2 ExtendTd — TD 扩展的精确算法

**触发条件**：TD 全满且无法复用时。

**空间计算**：
- 尝试扩展 `EXTEND_TD_NUM` 个槽，若空间不足则降为 `EXTEND_TD_MIN_NUM`
- 若连 `EXTEND_TD_MIN_NUM * sizeof(TD)` 都不够，返回 `PAGE_ERROR_NO_SPACE_FOR_TD`
- 不得超过 `MAX_TD_COUNT`（128）

**物理操作**：用 `memmove_s` 把整个 ItemId 数组向高地址移动 `numExtended * sizeof(TD)` 字节，为新 TD 腾位置，然后 `m_lower += numExtended * sizeof(TD)`。新 TD 调用 `td->Reset()` 初始化。

### 2.3 RecycleTd — TD 回收与 Tuple TD Status 联动

`RecycleTd(numRecycled)` 把 ItemId 数组向低地址移动，同步递减 `dataHeader.tdCount` 和 `m_lower`。

关键副作用：遍历所有 ItemId，对 HeapPage 和 IndexPage 分别检查：**若某 tuple 的 tdId >= 新 tdCount 且当前状态不是 `DETACH_TD`，则强制设为 `DETACH_TD`**。这保证被回收的 TD 槽不会有 tuple 仍持有对它的引用。

### 2.4 AllocTd — 四步骤分配算法，带重试

`DoAllocTd<pageType>()` 四步骤：
1. **检查当前事务是否已有 TD**（xid 或 lockerXid 匹配）——直接复用
2. **找 `UNOCCUPY_AND_PRUNEABLE` 槽**——记录 `firstUseableTdSlot`
3. **`TryReuseTdSlots`**——扫描已提交/冻结事务的 TD
4. **`ExtendTd`**——物理扩展

外层 `AllocTd()` 最多重试 **100 次**（`allocTdFailedThreshold = 100`），每次失败 sleep 10μs。

`tryLocalXidOnly` 优化：优先只查本地 zone 的 xid 状态，避免跨节点 RPC，失败时切回全局扫描。

### 2.5 TryReuseTdSlots — TD 复用的状态机

核心枚举 `TdReuseState`：
- `TD_RECYCLE_UNUSED`：可直接 `Reset()`（冻结或 CSN < recycleMinCsn）
- `TD_RECYCLE_REUSE`：提交但 CSN >= recycleMinCsn，需保留 CSN 供 MVCC 读
- `TD_IS_IN_PROGRESS`：进行中，加入等待列表
- `TD_CONTENT_UPDATED`：IndexPage 中 aborted xid，需回滚 TD 链
- `TD_IGNORE`：xid == INVALID_XID 但 lockerXid 在进行中

对于有 lockerXid 的 TD：若 locker 已结束则清除 lockerXid，若仍进行中则加入等待集合并阻止 canResetTd。

`canResetTd` 为 false 时，`GetAvailableTd()` 不能调用 `td->Reset()`，只能把 TD 标记为 `OCCUPY_TRX_END`。

### 2.6 RefreshTupleTdStatus — 批量更新 Tuple TD Status

遍历所有 ItemId，对匹配到被回收/复用 TD 槽的 tuple：
- 若对应 TD slot 变 `unused`（完全冻结）：tuple → `DETACH_TD`
- 若对应 TD slot 变 `reuse`（仅结束不冻结）：tuple → `ATTACH_TD_AS_HISTORY_OWNER`

同时清理 lockerTdId（写 `INVALID_TD_SLOT`）。

**注意**：此函数仅对 `HeapDiskTuple` 有具体实现，对 `IndexTuple` 调用时的 lockerTd 处理强转为 `HeapDiskTuple*`——这是一个隐含假设：locker 字段布局相同。

### 2.7 CompactTuples — Tuple 紧凑化算法

输入：已按 offset 降序排列的 `ItemIdCompactData` 数组（即从右到左按物理位置顺序）。

算法分三步：
1. **无需移动检测**：连续扫描，若 tuple 恰好紧贴当前 upper 边界则跳过
2. **批量移动**：发现间隙时，把一段连续的 tuples 整块 `memmove_s` 到新 upper 位置
3. **处理尾部**：最后一批的移动

**不对 upper 指针做整体清零**，仅移动有数据的区域，高效且安全。

### 2.8 ConstructCR — CR 页面构建的两阶段

**阶段 1**：线性扫描所有 TD，分情况处理：
- 当前 xid（`curXid`）：按 CID 回滚到 snapshot 可见版本；若使用了 `IS_PREV_XID_CSN` 且其 CSN >= snapshotCsn，还需回滚前一事务
- `PENDING_COMMIT`：非 dirty 快照需等待事务结束
- `IN_PROGRESS`：非 dirty 快照需回滚整条 undo 链
- `ABORTED`：调用 `RollbackTdOneXidForCRAsNeed`

**阶段 2**：使用 `binaryheap`（最大堆，按 CSN 降序）处理 CSN >= snapshotCsn 的提交事务，从最新到最旧逐步回滚，直到堆顶 CSN < snapshotCsn。

`useLocalCr` 标志：若任何 TD 需要回滚，CR 页面在本地内存构建而非从 CR Buffer 获取。

### 2.9 WAL 写入：RollbackTdOneXidAsNeed

每回滚一条 UndoRecord 后：
1. `bufMgr->MarkDirty(bufferDesc)`
2. `walContext->BeginAtomicWal(curXid)`
3. `UndoZone::GenerateWalForRollback(bufferDesc, undoRecord, walType)`
4. `walContext->EndAtomicWal()`

`walType` 区分 `WAL_UNDO_BTREE_PAGE_ROLL_BACK` 和 `WAL_UNDO_HEAP_PAGE_ROLL_BACK`，保证 redo 时可以精确回放。

### 2.10 ExtendCrPage — CR 页扩展为 2×BLCKSZ

当索引页回滚删除需要插入 tuple 但 CR 页空间不够时：
- 把 `[upper, BLCKSZ)` 区间的数据 `memmove` 到 `[upper+BLCKSZ, 2×BLCKSZ)` 位置
- 所有 ItemId 的 offset += BLCKSZ
- 更新 upper、special offset，设置 `IsCrExtend = true`
- 重新计算 checksum（传入 `isCrExtend=true`，checkSize 用 `EXTEND_PAGE_SIZE`）

---

## 3. dstore_heap_page.cpp — Heap 页特有逻辑

### 3.1 InitHeapPage — Heap 页初始化的额外字段

相比 `Page::Init`，Heap 页额外初始化：
- `SetRecentDeadTupleMinCsn(INVALID_CSN)`
- `SetPotentialDelSize(0)`
- `SetDataHeaderSize(HEAP_PAGE_HEADER_SIZE)`
- `m_header.m_lower = HEAP_PAGE_HEADER_SIZE`（不是 `sizeof(Page)=48`，是 Heap 特有的更大头部）
- `AllocateTdSpace()`（初始分配默认 TD 槽）
- `SetFsmIndex(fsmIndex)` — 维护 FSM 反向索引
- `SetIsNewPage(true)`

### 3.2 AddTuple — 精确的 lower/upper 更新逻辑

1. 调用 `GetFreeItemId()` 寻找空闲 ItemId 槽（已有 `HasFreeItemId()` 快速检查）
2. 可指定 `specifyOffset` 强制使用特定槽（用于 undo 回放时还原到原位）
3. 仅当 offset == `OffsetNumberNext(GetMaxOffset())` 时（新分配的 ItemId），才递增 `lower`
4. `upper -= size`，复制 tuple 数据
5. 调用 `itemId->SetNormal(upper, size)`

### 3.3 Heap Undo 的 7 种类型

`UndoHeap()` 分发到 7 个 Execute 函数：

| Undo 类型 | 核心操作 |
|-----------|----------|
| `UNDO_HEAP_INSERT` | itemId → Unused，TD 回滚 |
| `UNDO_HEAP_BATCH_INSERT` | 按范围区间批量设 Unused |
| `UNDO_HEAP_DELETE` | 从 undo 取旧 tuple 写回页面，检查 TD 是否仍有效 |
| `UNDO_HEAP_INPLACE_UPDATE` | 调用 `UndoActionOnTuple` 就地恢复，若新 size > 旧则需 CR compact |
| `UNDO_HEAP_SAME_PAGE_APPEND_UPDATE` | 恢复旧 tuple 数据并调整 ItemId 长度 |
| `UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE` | 旧页恢复，优先 UpdateTuple，不够则 CR compact + AddTuple |
| `UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE` | 新页侧：itemId → Unused，PotentialDelSize 更新 |

**TD 回滚时机**：对于需要 CompactCR 的情况（Delete、InplaceUpdate 扩大、OldPage 跨页更新），必须先 compact 再回滚 TD，防止 compact 时错误地回收当前 TD。

### 3.4 CompactCRPageIfFreeSpaceLessThan — CR 页三步压缩

触发条件：free space < needSpace

1. **从后向前回收 TD**：xid == INVALID_XID 的尾部 TD 安全可回收，调用 `RecycleTd`
2. **从后向前回收 ItemId**：Unused 的尾部 ItemId 调用 `RemoveLastItem()`
3. **CompactTuples**：收集所有 Normal+HasStorage 的 tuple，排序，压缩

### 3.5 ExecuteUndoForDelete 中 TD 有效性的二次检查

从 undo data 拿到的旧 tuple 其 tdId 可能已被回收/冻结：

```cpp
if (!IsTdValidAndOccupied(diskTuple->GetTdId())) {
    diskTuple->SetTdStatus(DETACH_TD);
} else {
    Xid curXid = GetTd(record->GetTdId())->GetXid();
    Xid tupleXid = diskTuple->GetXid(); // from undo record
    if (curXid != tupleXid) {
        diskTuple->SetTdStatus(ATTACH_TD_AS_HISTORY_OWNER);
    }
}
```

这体现了 TD 复用后的一致性修复：undo record 生成时 TD 状态与回放时可能已不同。

### 3.6 PruneItems — 两种修剪状态

- `ITEM_ID_UNUSED`：直接 `itemId->SetUnused()`，设 `HasFreeItemId`
- `ITEM_ID_NO_STORAGE`：从 diskTuple 拷贝 tdId 和 tdStatus 到 itemId（保留 MVCC 信息），设 `ItemId::NoStorage`，同时保存 `TupLiveMode`

### 3.7 TryCompactTuples vs CompactCRPageIfFreeSpaceLessThan 的区别

- `TryCompactTuples`：正常 prune 流程，不回收 TD，扫描 `ScanCompactableItems`（排除 NoStorage），结束后更新 `PotentialDelSize`
- `CompactCRPageIfFreeSpaceLessThan`：CR 构建流程，会回收尾部 TD 和 ItemId，更激进

### 3.8 ConstructCrTuple — Heap CR Tuple 链式追踪

核心循环：
1. 从 TD 链（本地 copy 的 `crTd[]`）取 xid 和 undoRecPtr
2. 调用 `FetchUndoRecordByMatchedCtid`（根据 ctid 精确匹配）
3. `crTd[tupleTdId].RollbackTdInfo(&record)` 推进 TD 链
4. `txnVisible` 判断：当前事务用 CID 比较，其他用 CSN 比较
5. 若可见且是跨页更新（`ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE`），切换 ctid 到新页
6. 不可见则 `ConstructCrTupleFromUndo` 更新 resTuple，继续追链

**关键**：undo 被回收时（`UNDO_ERROR_RECORD_RECYCLED`）认为 txnVisible = true，不报错。

### 3.9 ShowAnyTuple — 历史版本遍历（flashback）

`ShowAnyTupleFetch` 提供迭代器语义：首次调用从当前页面获取最新版本；后续调用沿 undo 链追溯。`ShowAnyTupleContext` 保存本地 TD copy，支持跨调用维护状态。`IsSnapMatchedTup` 按 (xid, cid, deleteXid) 三元组匹配特定版本。

### 3.10 Dump 中的大 Tuple（LinkedChunk）特殊处理

`alignShift` 是 `Dump()` 中特有的逻辑：linked tuple 的 values 偏移与 `GetValuesOffset()` 计算值不同（因为 `LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE = 9` 不满足 8 字节对齐），需做负向修正才能正确定位数据区。

---

## 4. dstore_index_page.cpp — Index 页特有逻辑

### 4.1 BtrPage 初始化：special 区域和无 TD 内部节点

`InitBtrPageInner()` 分配 `specialSize = MAXALIGN(sizeof(BtrPageLinkAndStatus))` 字节的 special 区域（用于存放左右链指针、level、root/split/live 状态等）。

内部节点（INTERNAL_PAGE）：`tdCount = 0`，AllocTd 直接返回 `INVALID_TD_SLOT`——内部节点不需要 TD，因为其 pivot tuple 本身不对应堆数据行。

叶子节点初始化后额外 `m_header.m_lower += sizeof(ItemId)` 为 HIKEY 占位。

### 4.2 IsBtrPageValid — 双重 Xid 校验

除页面类型检查外，还校验 `GetBtrMetaCreateXid() == checkingXid`，防止 btree 重建后新旧页面混淆。三参数版本还校验 `btrMetaPageId` 匹配。

### 4.3 分裂期间 TD 的特殊复制

`InitMemLeftForSplit`（临时左页）：从分裂原页面直接 `memcpy_s` 整个 TD 数组。

`InitNewRightForSplit`（新右页）：从**临时左页**（已包含新分配的 TD 槽）复制 TD 数组，同时设置新右页的左/右链接。

这保证分裂后两个新页都持有相同的 TD 快照，使得进行中的事务在两页上均可见。

### 4.4 AddTupleData — ItemId 数组原地插入（shuffle）

插入时若 `offset < maxAfterInsert`：`memmove_s(itemID+1, size, itemID, size)` 把后续所有 ItemId 后移一格，维护有序数组。与 Heap 页不同，Index 页的 ItemId 是有序紧排的，不用空闲链表。

### 4.5 OverwriteTuple — 原地替换，避免指针调整

当新 tuple size < 旧 size 时：`sizeDiff = oldsize - newsize`（正值），把 `[upper, offset)` 区间的所有 tuple 数据整体上移 `sizeDiff` 字节（`memmove_s` 起点 `+sizeDiff`），然后遍历所有 offset <= 旧 offset 的 ItemId 各加 `sizeDiff`。比删后重插高效，且不改变 ItemId 的逻辑顺序。

### 4.6 BinarySearch — 带相等标志的二分查找

使用闭区间 `[low, high)` 变体，`cmpval = 1` 意味着遇到相等时不走 `low = mid+1`，而是走 `high = mid`，最终 `low` 是第一个 >= target 的位置。`isEqual` 在找到相等时设为 true。

### 4.7 UndoBtreeInsert — 第一个 DataOffset 的特殊处理

若 offset 恰好是 `GetFirstDataOffset()`（即页面上最小 key 位置），**不能直接删除**，而是 `MarkUnreadableAndRangeholder()`——保留占位，维持 B-tree 低键约束。同时 `SetTdStatus(DETACH_TD)`。其他位置用 `RemoveItemId()` 做移位删除。

### 4.8 PruneNonItemIdTuples — 索引页的 CR 压缩

与 Heap 不同，索引 CR 压缩是**在物理 ItemId 数组上原地重写**：遍历时跳过已删除的 ItemId（不是 Normal 且不是 firstDataOffset），直接缩减 `m_lower`，重用指针位置。然后按 offset 降序排序存活 ItemId，`memmove` 消除 tuple 间隙。若还不够则尝试回收尾部 `INVALID_XID` 的 TD。

### 4.9 RemoveItemId — 顺序移位删除

```cpp
memmove_s(item, size, nextItem, size)  // 把后续 ItemId 前移
GetItemIdPtr(maxOff)->SetUnused()
RemoveLastItem()
```

维护了 ItemId 数组的紧凑性，代价是 O(n) 的移位，但 Index 页 tuple 数量通常可控。

### 4.10 UndoBtreeDelete 中的"删除被剪枝"情况

若 `m_isDeletionPruned == true`（deleted tuple 已被物理移除），需要调用 `UndoInsertPrunedTupleBack`：先 binary search 找插入位置，若空间不够则 `PruneNonItemIdTuples` 或 `ExtendCrPage`，然后 `AddTupleData` 插回。

恢复后的 tuple `tdStatus`：
- TD 已失效或 `m_isIns4Del` → `DETACH_TD`
- 否则 → `ATTACH_TD_AS_HISTORY_OWNER`

### 4.11 Dump 体系

提供 `DumpForLeaf`（含 TD 数组、heap ctid、ins4del、ccindexStatus、sameWithLastLeft）、`DumpForPivot`（含 downlink）、`DumpForMeta`（根页、lowest single page、属性类型信息）三路分派。`CollectTupleKeys` 支持 24 种 OID 类型的人类可读展示。

---

## 5. dstore_undo_page.cpp — Undo 页操作

### 5.1 TransactionSlotPage::Dump

遍历固定数量 `TRX_PAGE_SLOTS_NUM` 个事务槽，打印版本号和 `GetNextFreeLogicSlotId()`。**无任何修改逻辑**，此 .cpp 文件仅提供 Dump 能力，所有读写操作在 .h 或其他模块实现。

### 5.2 UndoRecordPage::Dump

打印三链指针：`cur`（自身）、`prev`（前驱）、`next`（后继），体现 Undo 页面以**双向链表**形式组织。版本号字段便于检测页面格式变化。

### 5.3 隐含设计

Undo 页没有 TD 槽、没有 ItemId 数组——它的"数据区"直接是序列化的 UndoRecord 流，空间管理完全交给上层的 UndoZone/UndoSegment。这是与 Heap/Index 页最本质的区别。

---

## 6. BtrRecycle 两个 Meta Page

### 6.1 两级层次结构

```
BtrRecycleRootMetaPage（1个）
    └── BtrRecyclePartitionMetaPage（最多 MAX_BTR_RECYCLE_PARTITION 个）
            ├── RecycleQueue head（待回收页面的队列头）
            └── FreeQueue head（可重用页面的队列头）
```

`recyclePartitionId = nodeId % MAX_BTR_RECYCLE_PARTITION`——按节点 ID 分片，减少锁竞争。

### 6.2 AcquireRecycleRootMetaBuf — 三重校验

1. 页面类型必须是 `BTR_RECYCLE_ROOT_META_PAGE_TYPE`
2. 若 `createdXid != INVALID_XID`，则必须与页面中存储的 `createdXid` 匹配（防止 B-tree 重建后旧 meta 被误用）
3. 函数入参 `createdXid` 是 in/out：未知时传 INVALID_XID 进去，返回后被填充

### 6.3 InitRecyclePartitionMeta — 完整的 WAL 原子化

```
BeginAtomicWal(curXid)
    partMetaPage->InitRecyclePartitionMetaPage(...)
    recycleRootMetaPage->SetRecyclePartitionMeta(...)
    MarkDirty(partition_buf)
    MarkDirty(root_buf)
    GenerateWal(partition_init)
    GenerateWal(root_set_partition_meta)
EndAtomicWal()
```

两个页面的修改在同一个原子 WAL 记录组内，保证崩溃恢复时的原子性。临时索引（`IsTempSegment()`）跳过 WAL。

### 6.4 WAL 记录格式

`WalRecordBtrRecyclePartitionMetaInit` 和 `WalRecordBtrRecycleRootMetaSetPartitionMeta` 均通过 `WalPageHeaderContext` 传递 `preWalId`、`prePlsn`、`preGlsn`，用于 redo 时验证 LSN 连续性。`glsnChangedFlag = (page->GetWalId() != walWriterContext->GetWalId())` 标识跨 WAL 段的情况。

### 6.5 IsEmpty 的无锁化优化

先检查 `RecycleQueue`（持 LW_SHARED 锁），若非空立即返回 false（无需锁 FreeQueue）。这是一种"早退"策略，减少不必要的锁操作。

### 6.6 BufMgr 懒初始化

`GetBufMgr()` 检查 `bufMgr == nullptr`，然后根据 `isGlobalTempIndex`（或 `segment->IsTempSegment()`）决定用线程局部 tmp buffer manager 还是全局 buffer manager。避免构造时的循环依赖。

---

## 7. dstore_page_diagnose.cpp — 诊断工具

### 7.1 诊断能力覆盖范围

`PageDump(Page*, showTupleData, metaPage)` 的 switch-case 支持全部 17 种页面类型：

| 类型 | 诊断入口 |
|------|---------|
| HEAP_PAGE | `HeapPage::Dump(showTupleData)` |
| INDEX_PAGE | `BtrPage::Dump(metaPage, showData)` |
| TRANSACTION_SLOT_PAGE | `TransactionSlotPage::Dump()` |
| UNDO_PAGE | `UndoRecordPage::Dump()` |
| FSM_PAGE | `FsmPage::Dump()` |
| FSM_META_PAGE | `FreeSpaceMapMetaPage::Dump()` |
| DATA_SEGMENT_META | `DataSegmentMetaPage::DumpDataSegmentMetaPage()` |
| HEAP_SEGMENT_META | `HeapSegmentMetaPage::DumpHeapSegmentMetaPage()` |
| UNDO_SEGMENT_META | `UndoSegmentMetaPage::DumpUndoSegmentMetaPage()` |
| TBS_EXTENT_META | `SegExtentMetaPage::Dump()` |
| TBS_BITMAP | `TbsBitmapPage::Dump()` |
| TBS_BITMAP_META | `TbsBitmapMetaPage::Dump()` |
| TBS_FILE_META | `TbsFileMetaPage::Dump()` |
| BTR_QUEUE_PAGE | `BtrQueuePage::Dump()` |
| BTR_RECYCLE_PARTITION_META | `BtrRecyclePartitionMetaPage::Dump()` |
| BTR_RECYCLE_ROOT_META | `BtrRecycleRootMetaPage::Dump()` |
| TBS_SPACE_META | `TbsSpaceMetaPage::Dump()` |

此外有独立的 `PageDump(ControlFileMetaPage*)`、`PageDump(ControlPage*)`、`PageDump(DecodeDictMetaPage*)`、`PageDump(DecodeDictPage*)` 重载。

### 7.2 DumpToolHelper — 面向工具程序的 VFS 抽象

支持两种存储后端：
- `PAGESTORE`：动态链接 VFS 库，读取 tenant config JSON，通过网络访问远端 pagestore
- `LOCAL` / `TENANT_ISOLATION`：`GetStaticLocalVfsInstance`，直接操作本地文件

`Init()` 流程：`InitVfsModule` → `GetTenantConfig` → `UpdateCommConfig`（可从命令行覆盖通信参数）→ `DynamicLinkVFS` → `MountVfs`。

支持 `reuseVfs` 模式：外部已持有 VFS handle 时直接复用，用于嵌入进程内的诊断调用。

### 7.3 通信配置的自动检测

`GetNetworkAdapterLocalIp()` 通过 `gethostname` + `gethostbyname` 自动获取本机 IP，作为默认通信地址，无需人工配置。

### 7.4 ReadPage 的严格边界检查

精确按页读取，不做跨页读取。bufferSize 必须等于 `BLCKSZ`，否则直接返回 FAIL。

### 7.5 HasGarbageSpace — Index 页碎片检测

```cpp
bool BtrPage::HasGarbageSpace() {
    uint16 tupleTotalLen = 0;
    for each offset: tupleTotalLen += GetItemIdPtr(offset)->GetLen();
    return tupleTotalLen != (GetSpecialOffset() - GetUpper());
}
```

若 tuple 实际总长度 ≠ `special_offset - upper`，说明存在物理碎片（holes），需要 compact。

---

## 8. 综合对比表：各页类型关键差异

| 特性 | HeapPage | IndexPage (BtrPage) | UndoPage | BtrRecyclePage |
|------|---------|---------------------|----------|----------------|
| **TD 槽** | 有（默认4，最大128） | 叶节点有，内部节点无（tdCount=0） | 无 | 无 |
| **WAL** | Undo 回滚时按条写 `WAL_UNDO_HEAP_PAGE_ROLL_BACK` | Undo 回滚时按条写 `WAL_UNDO_BTREE_PAGE_ROLL_BACK` | 无（由 UndoZone 管理） | 初始化/设分区时写原子 WAL |
| **空间分配** | ItemId 链表（有空闲列表 `HasFreeItemId`），支持指定 offset 的 AddTuple | ItemId 有序数组，插入时 memmove shuffle | 无 ItemId，直接流式写入 | 无数据区，仅存队列头指针 |
| **Compact/Defrag** | `TryCompactTuples`（正常）+ `CompactCRPageIfFreeSpaceLessThan`（CR） | `PruneNonItemIdTuples`（物理重写 ItemId 数组 + TD 回收） | 不适用 | 不适用 |
| **Special 区域** | 无（specialSize=0，needCrc=1） | 有 `BtrPageLinkAndStatus`（左右链/level/root/split状态），不参与 CRC | 无 | 无 |
| **MVCC/CR** | ConstructCrTuple 追 undo 链，支持跨页更新 ctid 切换 | ConstructCR 需 BtreeUndoContext，处理分裂后 offset 漂移 | 不适用（undo 源头） | 不适用 |
| **Undo 回放** | 7种类型，含就地更新和大 tuple 扩容 | 2种类型（Insert/Delete），删除回滚需处理已剪枝情况 | 不适用 | 不适用 |
| **分裂支持** | 无 | `InitMemLeftForSplit`/`InitNewRightForSplit`，TD 数组在两页间复制 | 不适用 | 不适用 |
| **历史版本查询** | `ShowAnyTupleFetch` 迭代器，支持 flashback | 无 | 不适用 | 不适用 |
| **垃圾检测** | `PotentialDelSize` + `HasPrunableTuple` 标志 | `HasGarbageSpace()`（物理碎片检测） | 不适用 | `IsEmpty()`（队列头检查） |

---

## 9. .h vs .cpp 新发现汇总

| 序号 | 模块 | 在 .h 中的理解 | .cpp 中的真实实现 |
|------|------|--------------|----------------|
| 1 | `Page::Init` | 初始化页面 | 页面复用时**保留旧 LSN**（glsn/plsn/walId）；CRC 覆盖范围依 special 区域动态变化 |
| 2 | `Page::CheckPageCrcMatch` | 校验 CRC | 允许 `checksum==0` 对应全零页常量 `25258`，向后兼容未写 checksum 的旧格式 |
| 3 | `DataPage::AllocTd` | 分配 TD 槽 | 最多重试 **100 次**，每次失败 sleep 10μs；`tryLocalXidOnly` 优先本地 zone 避免 RPC |
| 4 | `DataPage::ExtendTd` | 扩展 TD 数组 | 通过 `memmove` 向高地址移动整个 ItemId 数组；扩展量动态决定（`EXTEND_TD_NUM` vs 剩余空间 vs `EXTEND_TD_MIN_NUM`） |
| 5 | `DataPage::RecycleTd` | 回收 TD 槽 | 回收后**强制**把 tdId 越界的所有 tuple 设为 `DETACH_TD`，防止悬空引用 |
| 6 | `TupleTdStatus::ATTACH_TD_AS_HISTORY_OWNER` | 历史拥有者状态 | Undo 回放时统一设为此状态（即使原状态非此），保证一致性 |
| 7 | `DataPage::ConstructCR` | CR 页构建 | 两阶段：线性扫描处理特殊事务 + 堆排序按 CSN 降序回滚提交事务；`IS_PREV_XID_CSN` 触发前驱事务可见性检查 |
| 8 | `HeapPage::AddTuple` | 插入 tuple | 支持 `specifyOffset` 强制回到原位（undo 回放专用）；仅新 slot 时才递增 lower |
| 9 | `HeapPage::CompactCRPageIfFreeSpaceLessThan` | CR 页压缩 | 三步骤：先回收尾部 TD → 再回收尾部 ItemId → 最后 compact tuples；**与正常 prune 路径完全不同** |
| 10 | `BtrPage::AllocTd` | 索引页分配 TD | 内部节点直接返回 `INVALID_TD_SLOT`，不调用基类 `AllocTd` |
| 11 | `BtrPage::UndoBtreeInsert` | 索引 undo insert | 页面最小 key 位置（`GetFirstDataOffset()`）不能删除，改为 `MarkUnreadableAndRangeholder` |
| 12 | `BtrPage::InitMemLeftForSplit` | 分裂初始化 | 调用前必须先 `memset_sp` 全部清零，再调 `Init`，避免随机 LSN 被错误保留 |
| 13 | `DataPage::ExtendCrPage` | CR 页扩展 | 大小扩至 `2×BLCKSZ`，所有 ItemId offset += BLCKSZ；重算 checksum 使用 `EXTEND_PAGE_SIZE` |
| 14 | `BtrRecycleRootMeta::AcquireRecycleRootMetaBuf` | 获取 meta buf | 通过 `createdXid` 双向校验防止 btree 重建后混淆；`createdXid` 作为 in/out 参数填充 |
| 15 | `PageDiagnose` | 诊断工具 | 覆盖全部 17 种页面类型；`DumpToolHelper` 内置网络 IP 自动检测，支持 pagestore 远程访问 |
| 16 | `UndoPage` | Undo 页 | .cpp 仅含 Dump，无任何修改逻辑——读写操作全在 UndoZone/UndoRecord 层，页面结构简单（无 TD/ItemId） |
| 17 | `DataPage::RollbackByUndoRec` | 按 UndoRecord 回滚 | 通过 `GetType()` 动态分派到 `HeapPage::UndoHeap` 或 `BtrPage::UndoBtree`，IndexPage 需额外传 `BtreeUndoContext` |
| 18 | `DataPage::JudgeTupCommitBeforeSpecCsn` | 判断 tuple 可见性 | `needFillCSN=true` 时要求调用方持有 LW_EXCLUSIVE；IndexPage 需先取 heap ctid 再到 UndoRecord 追溯 |
