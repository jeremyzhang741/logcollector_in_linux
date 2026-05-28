# Heap 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 INSERT/UPDATE/DELETE/SCAN 四条主线以及 TD 分配、Prune、大元组处理的具体实现细节。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_heap_insert.cpp` | 1014 | 单条插入、批量插入、大元组分块 |
| `dstore_heap_delete.cpp` | 438 | 删除、大元组链式删除 |
| `dstore_heap_update.cpp` | 1510 | 原地/同页/跨页/大元组更新 |
| `dstore_heap_scan.cpp` | 1279 | 顺序扫描、随机读取、大元组重组 |
| `dstore_heap_lock_tuple.cpp` | 972 | 行锁、等待并发事务、CheckTupleChanged |
| `dstore_heap_undo_struct.cpp` | — | InplaceUpdate 差量 Undo 的生成与回放 |
| `dstore_heap_wal_struct.cpp` | — | Heap WAL 的 Redo 分发表与各 Redo 实现 |
| `dstore_page/dstore_data_page.cpp` | 1418 | 页面 TD 操作、GetVisibleTuple、ConstructCrTuple |
| `dstore_page/dstore_heap_page.cpp` | 1567 | AddTuple、DelTuple、Prune、CompactTuples |

---

## 一、INSERT 完整路径

### 1.1 整体流程

```
Insert(ctx)
  ├─ BeginInsert
  │    ├─ PrepareTuple      ← 初始化 diskTuple 头字段；大元组则 SplitTupIntoChunks
  │    └─ AllocTransactionSlot
  ├─ InsertSmallTuple / InsertBigTuple
  └─ EndInsert              ← 回写 ctid 到 tuple->SetCtid
```

### 1.2 InsertSmallDiskTup：核心五步

```
① GetBuffer          ← FSM 查找可用页（含 AllocTd 决策，失败则重试）
② page->AddTuple     ← 页面 ItemId 数组占 slot，diskTuple 写入 upper 区
③ MarkDirty
④ ExtendUndoSpaceIfNeeded  ← 预扩展 Undo 空间（失败则回滚页面并生成 AllocTd WAL）
⑤ BeginAtomicWal
   → InsertUndoRecord（写 Undo）
   → SetTd（xid + undoRecPtr + cid）
   → GenerateHeapInsertWal（写 Redo WAL）
   → EndAtomicWal
⑥ UnlockAndRelease
```

### 1.3 GetBuffer：页面选择与重试

```cpp
for (;;) {
    // 尝试从 FSM 获取，或用缓存的 lastPageId
    targetPageId = fsm.GetPage() 或 lastPageId
    bufDesc = bufMgr->Read(targetPageId, LW_EXCLUSIVE)
    tdResult = page->AllocTd()
    result = CheckPageHasEnoughSpace(page, tupleSize):
        HAS_ENOUGH_SPACE         → break（找到）
        NO_SPACE_AFTER_PRUNE     → Release; UpdateFSM; ++retryTimes
        NO_SPACE_INVALID_TD      → Release; ++retryTimes
}
```

`CheckPageHasEnoughSpace` 内部：若空间不足先调 `TryPrunePage` 回收死元组，再判断。

### 1.4 大元组：反向插入链（InsertBigTuple）

大元组（`diskTupSize > maxTupSpaceSize`）先拆块：

```
SplitTupIntoChunks():
  numChunks = ceil((diskTupSize - sizeof(HeapDiskTuple)) / maxChunkDataSize)
  chunk[0]:  SetFirstLinkChunk() + SetNumTupChunks(numChunks) + header + 第一段数据
  chunk[i]:  SetNotFirstLinkChunk() + 后续数据段
```

**从最后一块到第一块逆序插入**，每次插入的 ctid 作为前一块的 nextChunkCtid：

```cpp
for (int32 i = m_chunkNum - 1; i >= 0; --i) {
    chunk->SetNextChunkCtid(tupNextChunkCtid);  // 指向下一块
    InsertSmallDiskTup(...);
    tupNextChunkCtid = insertContext->ctid;     // 记录本块位置
}
```

这样第 0 块的 nextChunkCtid 形成完整的单向链表。

### 1.5 批量插入（BatchInsert）

```
BatchInsertSmallTuples():
  GetBuffer → GetUsableOffsetRanges    ← 收集本页可用 ItemId（含复用 unused slot）
  一个 AtomicWal 完成本页所有元组:
    InsertUndoRecord(UNDO_HEAP_BATCH_INSERT, [(start,end)…])
    loop: SetTd + AddTuple per tuple
    GenerateBatchInsertWal
```

`GetUsableOffsetRanges`：先扫 unused ItemId（`LocateUsableItemIds`），再追加从 maxOffset+1 的连续新 slot。

### 1.6 WAL 格式

| WAL 类型 | 携带内容 |
|---------|---------|
| `WAL_HEAP_INSERT` | pageId + offset + undoPtr + diskTuple + AllocTd + (可选 tableOid + snapshotCsn) |
| `WAL_HEAP_BATCH_INSERT` | undoPtr + N×(offset + diskTuple) + AllocTd |

---

## 二、UPDATE 三种路径

### 2.1 路径选择

```
AllocTd → tdId
if newTupleSize <= oldItemLen:
    UpdateSmallTupleInplace       ← 原地覆写（新≤旧物理空间）
elif page.freeSpace >= newTupleSize:
    UpdateSmallTupleSamePage      ← 同页追加
else:
    TryPrunePage
    if page.freeSpace >= newTupleSize:
        UpdateSmallTupleSamePage
    else:
        UpdateSmallTupleAnotherPage  ← 跨页更新
```

### 2.2 原地更新（UpdateSmallTupleInplace）

- `GetDiffBetweenTuples` — 计算新旧元组差异区间列表 `(pos[], num)`
- Undo = `UndoDataHeapInplaceUpdate`：**仅记录差量区间的旧数据**（节省 Undo 空间）
- `page->UpdateTuple(offset, newData, size)` — 原地覆写
- `newCtid == oldCtid`，不需要更新索引（除非 replica key 列变化）

差量回放（`UndoActionOnTuple`）：若 undoTupleSize ≠ pageTupleSize 先 memmove 调整偏移，再按区间 memcpy 恢复旧字节。

### 2.3 同页追加（UpdateSmallTupleSamePage）

- 旧元组：`LiveMode = OLD_TUPLE_BY_SAME_PAGE_UPDATE`，`AddPotentialDelItemSize`
- 新元组：写入同页，**复用旧 ItemId slot**（`AddTuple(..., offset)`），`LiveMode = NEW_TUPLE_BY_SAME_PAGE_UPDATE`
- Undo = `UNDO_HEAP_SAME_PAGE_APPEND_UPDATE`：完整旧 diskTuple
- `newCtid == oldCtid`（同一 ItemId slot）

### 2.4 跨页更新（UpdateSmallTupleAnotherPage）

分两步两次页面操作：

**Step 1 — 新页插入**（`UpdateSmallTupleNewPage`）：
```
GetBuffer(excludePageId=旧页)   ← 排除旧页，避免退化同页
新元组 LiveMode = NEW_TUPLE_BY_ANOTHER_PAGE_UPDATE
Undo = UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE（只记 ctid，无数据体）
WAL: WAL_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE
```

**Step 2 — 旧页标记**（`UpdateSmallTupleOldPage`）：
```
DoLock(旧页)                 ← 防止并发修改
Read(旧页, LW_EXCLUSIVE)
旧元组 LiveMode = OLD_TUPLE_BY_ANOTHER_PAGE_UPDATE
Undo = UndoDataHeapAnotherPageAppendUpdate: 完整旧 diskTuple + newCtid
WAL: WAL_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE
```

`newCtid` 存入 Undo 是关键：后续通过 Undo 记录可以找到新版本的物理位置。

### 2.5 LiveMode 与更新类型对应

| 更新类型 | 旧元组 LiveMode | 新元组 LiveMode |
|---------|---------------|---------------|
| 原地更新 | （覆盖，无"旧"） | NEW_TUPLE_BY_INPLACE_UPDATE |
| 同页追加 | OLD_TUPLE_BY_SAME_PAGE_UPDATE | NEW_TUPLE_BY_SAME_PAGE_UPDATE |
| 跨页追加 | OLD_TUPLE_BY_ANOTHER_PAGE_UPDATE | NEW_TUPLE_BY_ANOTHER_PAGE_UPDATE |

SeqScan 时 `OLD_TUPLE_BY_ANOTHER_PAGE_UPDATE` 直接跳过；`TUPLE_BY_NORMAL_DELETE` 也跳过。

### 2.6 大元组更新（UpdateBigTuple）

先对第一块 DoLock（防死锁），再按新旧块数选择策略：

- **新块数 ≥ 旧块数**：先插入多余的新块，再逐块调 `UpdateSmallTuple` 覆盖旧块，更新 nextChunkCtid 链
- **新块数 < 旧块数**：先删除多余的旧块，再逐块更新剩余旧块

`UpdateOldTupChunks`：从最后一块到第一块反向更新，每次传递 nextChunkCtid。

---

## 三、DELETE 流程

### 3.1 DeleteDiskTuple 核心步骤

```
① AllocTd
② diskTuple->SetLockerTdId(INVALID_TD_SLOT)  ← 清行锁
③ 构造 UndoDataHeapDelete: 完整旧 diskTuple
④ ExtendUndoSpaceIfNeeded
⑤ page->DelTuple(offset)
   → 设 LiveMode = TUPLE_BY_NORMAL_DELETE
   → ItemId 标记"deleted"（不释放物理空间）
⑥ AddPotentialDelItemSize
⑦ MarkDirty
⑧ BeginAtomicWal → InsertUndoRecord → SetTd → GenerateHeapDeleteWal → EndAtomicWal
```

注意：`DelTuple` 只改 LiveMode，物理空间由 Prune 回收。

### 3.2 DeleteBigTuple：链式删除

```
DeleteDiskTuple(第一块)
UpdateFsmForPrune(第一块所在页)  ← 主动通知 FSM 有可回收空间
loop nextChunkCtid:
    DeleteDiskTuple(每一块)
```

**失败不重试**：因为不知道已删到哪一块，直接让事务回滚处理。

---

## 四、SCAN 可见性判断（GetVisibleTuple / ConstructCrTuple）

### 4.1 SeqScanNext 核心逻辑

```
PrepareValidCrPage(crPage)    ← 获取 CR（一致性读）页面
for each ItemId on page:
    skip: IsUnused / IsNoStorage
    GetTuple(&m_resTuple, offset)
    skip: LiveMode == TUPLE_BY_NORMAL_DELETE
    skip: LiveMode == OLD_TUPLE_BY_ANOTHER_PAGE_UPDATE
    if diskTuple->IsLinked():
        if IsFirstLinkChunk: FetchBigTuple → m_bigTuple
        else: skip（非首块）
    break（found）
```

### 4.2 PrepareValidCrPage：一致性读页面

调用 `bufMgr->ConsistentRead(crContext)` 根据快照构造 CR 页面（若有版本链则应用 Undo）：
- CR buffer 在 buffer pool 中命中 → 使用 CR buffer
- 未命中 → 结果写入 `m_localCrPage`（本地预分配缓冲区）

### 4.3 FetchTuple（随机读取）

`FetchVisibleDiskTuple` 维护 `m_curPageId/m_curPageDesc` 缓存上次读取的页面：
- 页面 ID 命中缓存 → 直接复用（避免重复 BufTable 查找）
- 否则重新 Read

对 SNAPSHOT_NOW 下的大元组，临时切换为 SNAPSHOT_MVCC 追踪完整链。

### 4.4 FetchBigTuple：重组大元组

```cpp
tupChunks[0] = firstChunk;
ctid = firstChunk->GetNextChunkCtid();
while (ctid != INVALID) {
    tuple = FetchVisibleDiskTuple(ctid);
    tupChunks[i++] = tuple;
    ctid = tuple->GetNextChunkCtid();
}
assert(i == numTupChunks);  // 否则 PANIC + DumpPage
return AssembleTuples(tupChunks, i);
```

`AssembleTuples`：alloc 连续内存，memcpy 拼接；清 IsLinked 标志，`SetTupleSize(0)`（实际大小超 uint16 上限，调用者用 `HeapTuple::GetDiskTupleSize()`）。

---

## 五、行锁机制（LockTuple）

### 5.1 行锁不写 WAL

行锁直接写在页面 TD 的 `lockerXid` 字段：
```cpp
diskTuple->SetLockerTdId(tdId);
td->SetLockerXid(xid);
td->SetStatus(OCCUPY_TRX_IN_PROGRESS);
MarkDirty();  // 不写 WAL
```

崩溃重启后行锁信息丢失是可以接受的（事务已不在运行，行锁自然失效）。

### 5.2 CheckTupleChanged：并发控制核心

```
Step 1: WaitTupleIfNeed
  → 若 lockerTdId 有效或 tdId+ATTACH_TD_AS_NEW_OWNER → WaitTxn 等待
  
Step 2: 检查当前事务的 lockerTdId（EPQ/DETACH/CSN 比较）

Step 3: DETACH_TD → 元组必然已提交，未变化

Step 4: TD.xid == curXid → CheckTupleChangedByCid（Undo 链比较 CID）

Step 5: JudgeTupCommitBeforeSpecCsn → 快照前提交，未变化

Step 6: FROZEN → 未变化；否则 isChanged = true
```

**WaitTxn 流程**：释放页面锁 → `TransactionMgr::WaitForTransactionEnd(xid)` → 若事务异常中止则加 Exclusive 锁调 `page->RollbackByXid`。

### 5.3 AllocTd 失败重试（CanRetry）

```
① TryPrunePage(page)                    ← 先尝试回收 TD slots
② 若 TD 未满且有 freeSpace → 立即重试
③ 否则 WaitForOneTransactionEnd() → 重试
goto INSERTSTART / UPDATESTART / ...
```

大元组操作：失败直接报错，不重试（已操作了部分块，无法安全重试）。

---

## 六、Prune：空间回收

### 6.1 TryPrunePage 触发条件

```cpp
// 满足其一则执行 Prune：
① potentialDelSize > MaxPossibleTupleSpace * potentialFreeSpaceFactor
② freeSpace < threshold AND potentialDelSize > 0
③ needSize > 0 AND needSize < potentialDelSize   // 需要特定大小空间
```

仅当 `recentDeadTupleMinCsn < recycleMinCsn` 时才有元组可回收。

### 6.2 ItemId 两步回收

- **DETACH_TD**（TD 被复用）→ `ITEM_ID_UNUSED`（完全回收，slot 可重用）
- 否则 → `ITEM_ID_NO_STORAGE`（ItemId 保留以支持 MVCC，但无物理数据）

### 6.3 Prune WAL（WAL_HEAP_PRUNE）

```
WalRecordHeapPrune = header + diffNum + recentDeadMinCsn
  + diffNum × ItemIdDiff{offNum, newState}
```

---

## 七、WAL Redo 分发表

所有 Heap WAL 类型通过静态 `HEAP_WAL_REDO_TABLE[]` 数组分发，每条记录对应一个 lambda：

| WAL 类型 | Redo 操作要点 |
|---------|-------------|
| INSERT | RedoAllocTdWal；AddTuple；SetTd |
| BATCH_INSERT | RedoAllocTdWal；循环 AddTuple；SetTd |
| DELETE | RedoAllocTdWal；DelTuple；AddPotentialDelItemSize；SetTuplePrunable |
| INPLACE_UPDATE | RedoAllocTdWal；memmove 调整偏移；差量区间 memcpy；SetTupleSize |
| SAME_PAGE_APPEND | RedoAllocTdWal；AddTuple；旧元组 LiveMode=OLD_BY_SAME_PAGE |
| ANOTHER_PAGE_OLD | RedoAllocTdWal；DelTuple；旧元组 LiveMode=OLD_BY_ANOTHER_PAGE |
| ANOTHER_PAGE_NEW | RedoAllocTdWal；AddTuple |
| PRUNE | PruneItems；TryCompactTuples；SetRecentDeadTupleMinCsn |

**共同模式**：所有需要 AllocTd 的 WAL 先调 `RedoAllocTdWal`，重建页面 TD slot 状态；最后更新页面 LSN（walId + recordEndPlsn + glsn）。

---

## 八、Undo 数据类型对应

| Undo 类型 | 存储内容 |
|---------|---------|
| HEAP_INSERT | 仅 ctid（回滚时删除该行） |
| HEAP_BATCH_INSERT | (startOffset, endOffset) 范围对列表 |
| HEAP_DELETE | 完整旧 diskTuple |
| HEAP_INPLACE_UPDATE | 差量：旧数据区间 (start, end, bytes) 列表 |
| HEAP_SAME_PAGE_APPEND_UPDATE | 完整旧 diskTuple |
| HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE | 完整旧 diskTuple + newCtid |
| HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE | 仅 ctid（无旧数据） |

---

## 九、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| INSERT 页面选择 | "FSM 找可用页" | GetBuffer 含重试循环：Prune 失败后等一个事务结束再重试 |
| 大元组插入 | "分块存储" | 反向插入（从最后块到第0块），nextChunkCtid 逆向构建 |
| UPDATE 路径 | "三种模式" | 路径选择含 TryPrunePage 作为中间态，阈值基于 freeSpace |
| 跨页 UPDATE 的 newCtid | "存在 Undo" | 确切存在 `UndoDataHeapAnotherPageAppendUpdate::newCtid`，供 LockNewestTuple 追踪 |
| 行锁 | "不写 WAL" | lockerXid 写在 TD 字段，MarkDirty 不写 WAL；崩溃后自动失效 |
| BigTuple 大小字段 | "GetTupleSize()" | SetTupleSize(0)（uint16 溢出），实际大小用 `HeapTuple::GetDiskTupleSize()` |
| Prune 触发 | "recycleMinCsn 驱动" | recentDeadTupleMinCsn 存在页面头，快速判断是否有可回收元组 |
| Undo INPLACE_UPDATE | "记录旧值" | 只记差量字节区间，回放时含 memmove 处理大小变化 |
| SeqScan 跳过逻辑 | "可见性判断" | LiveMode 快速路径：OLD_ANOTHER_PAGE 直接跳过，不进 MVCC |
| AllocTd 失败 | "TD 槽耗尽" | CanRetry：先 Prune，再等一个事务，再 goto XXXSTART 重试 |
| 逻辑复制 WAL | "额外信息" | 携带 tableOid + snapshotCsn + identity tuple（replica key 列） |
| Batch INSERT Undo | "一条 Undo 多行" | 存 (startOffset, endOffset) 范围对，IsMatchedCtid 扫描范围 |
