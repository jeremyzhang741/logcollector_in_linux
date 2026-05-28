# Undo 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 InsertUndoRecord、Recycle、Rollback、Restore 四条主线的具体实现细节。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_undo_record.cpp` | 107 | UndoRecord 构造/校验/ctid匹配 |
| `dstore_transaction_slot.cpp` | 83 | TransactionSlot 初始化/WAL持久性判断 |
| `dstore_undo_zone_txn_mgr.cpp` | 859 | 事务槽分配/提交/回滚/GC |
| `dstore_undo_zone.cpp` | 1492 | UndoRecord写入/读取/扩容/GC/回滚 |
| `dstore_rollback_trx_task_mgr.cpp` | 295 | 异步回滚调度器（Dispatch 线程） |
| `dstore_rollback_trx_worker.cpp` | 172 | 异步回滚 Worker（双线程安全架构） |
| `dstore_undo_mgr.cpp` | 802 | UndoZone 全局管理（Zone Map + 懒加载） |
| `dstore_undo_wal.cpp` | 346 | WAL 的 Redo/Dump/Compress 调度表 |
| `dstore_undo_txn_info_cache.cpp` | 198 | 事务信息热缓存（无锁 128-bit CAS） |

---

## 一、UndoRecord 的结构与校验

### 1.1 写入格式（三段式）

`WriteUndoRecord()` 按固定顺序写入三段数据：

```
Undo 页面中一条记录的内存布局：
┌──────────────────────────────────────────┐
│  SerializeData                           │  ← header 序列化（固定大小，Varint压缩）
│  （undoType/ctid/txnPreUndoPtr/tdId等）   │    大小 = serializeSize（uint8，最大 255B）
├──────────────────────────────────────────┤
│  m_dataInfo.len (4B int)                 │  ← 旧数据长度
├──────────────────────────────────────────┤
│  m_dataInfo.data (变长)                  │  ← 旧 Tuple 数据（INSERT 无此段）
└──────────────────────────────────────────┘
```

对应代码（`WriteUndoRecord`，zone.cpp:321）：
```cpp
InsertUndoBytes({record.GetSerializeData(), record.GetSerializeSize()}, ...);
InsertUndoBytes({&record.m_dataInfo.len, sizeof(int)}, ...);
InsertUndoBytes({record.m_dataInfo.data, record.GetUndoDataSize()}, ...);
```

### 1.2 有效性规则（CheckValidity）

```cpp
case UNDO_HEAP_INSERT:                           // INSERT 类型
case UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE:
    return m_dataInfo.data == nullptr ? SUCC : FAIL;  // 必须无旧数据
default:
    return m_dataInfo.data == nullptr ? FAIL : SUCC;  // 必须有旧数据
```

原因：INSERT 回滚只需要知道 ctid 然后删除即可，不需要保存旧版本。

### 1.3 批量 INSERT 的 ctid 匹配（IsMatchedCtid）

`UNDO_HEAP_BATCH_INSERT` 一个 Undo 记录对应一批行，存的是 offset 范围数组：

```
UndoDataHeapBatchInsert.rawData = [startOffset1, endOffset1, startOffset2, endOffset2, ...]
```

匹配逻辑：页面相同 + `ctid.offset` 在任意范围 `[startOffset, endOffset]` 内即命中。

---

## 二、TransactionSlot 的提交可见性保护

### 2.1 Init 初始值

```cpp
void TransactionSlot::Init(uint64 tmpLogicSlotId) {
    curTailUndoPtr  = INVALID_UNDO_RECORD_PTR;   // 同事务 Undo 链尾
    spaceTailUndoPtr = INVALID_UNDO_RECORD_PTR;  // 已分配空间的边界（GC 用）
    status          = TXN_STATUS_IN_PROGRESS;
    logicSlotId     = tmpLogicSlotId;
    csn             = INVALID_CSN;
    walId           = INVALID_WAL_ID;
    commitEndPlsn   = INVALID_PLSN;
}
```

### 2.2 IsCommitWalPersist：延迟提交可见性检测

这是 COMMIT_LOGIC_TAG 的核心（slot.cpp:47）：

```cpp
bool TransactionSlot::IsCommitWalPersist(PdbId pdbId) {
    WalStreamManager *walMgr = ...GetWalStreamManager();
    // WAL 还没有刷到磁盘？→ 不算持久化
    if (commitEndPlsn > walMgr->GetWritingWalStream()->GetMaxFlushedPlsn()) {
        return false;
    }
    // 主库还需等 CsnMgr 上界满足
    CsnMgr::WaitUpperBoundSatisfy(csn);
    return true;
}
```

**为什么需要这个检查**：

事务提交分两个原子步骤：
1. 先写 slot.status = COMMITTED（内存可见）
2. 再等 WAL 落盘

若系统在 WAL 落盘前崩溃，Recovery 会把该事务标记为 ABORTED。
所以，在 WAL 未落盘时就把 status=COMMITTED 告诉其他线程，其他线程不能就此认为事务已提交。

`CopySlot()` 读取 slot 时会做保护：
```cpp
// zone_txn_mgr.cpp:376
if (trxSlot.status == TXN_STATUS_COMMITTED && !trxSlot.IsCommitWalPersist(m_pdbId)) {
    trxSlot.status = TXN_STATUS_PENDING_COMMIT;   // 降级为 PENDING
}
```

---

## 三、事务槽管理（UndoZoneTrxManager）

### 3.1 AllocSlot：新事务分配 XID

```
AllocSlot() 流程：
  ① HasFreeSlot()               检查环形槽是否已满（nextFree < recycle + 环容量）
  ② PinAndLockTxnSlotBuf()      锁住目标槽页面（缓存优化：优先复用上次的buf）
  ③ slot->Init(nextFreeLogicSlotId)  初始化槽
  ④ m_nextFreeLogicSlotId.fetch_add(1)  原子推进
  ⑤ txnSlotPage->SetNextFreeLogicSlotId()  持久化到页面（用于 Restore）
  ⑥ MarkDirty + WAL_UNDO_ALLOCATE_TXN_SLOT
  ⑦ 返回 Xid{zoneId, logicSlotId}
```

关键：步骤 ⑤ 在页面上记录 `nextFreeLogicSlotId`，这是 `Restore()` 时恢复 Zone 状态的锚点。

### 3.2 Commit（模板函数）：两阶段提交实现

```cpp
template<TrxSlotStatus status>   // PENDING_COMMIT 或 COMMITTED
void UndoZoneTrxManager::Commit(Xid xid, CommitSeqNo &csn, bool isAsyncCommit) {
    csnMgr->GetNextCsn(csn, status == COMMITTED);   // 第1阶段用 false，第2阶段用 true
    PinAndLockTxnSlotBuf(pageId);
    slot->SetCsn(csn);
    slot->SetTrxSlotStatus(status);
    MarkDirty();
    WAL(WAL_TXN_COMMIT);
    EndAtomicWal() → walGroupPtr;
    UnlockAndRelease();  // ← 先解锁，再等 WAL 落盘（COMMIT_LOGIC_TAG）

    if (status == COMMITTED && !isAsyncCommit) {
        WaitTargetPlsnPersist(walGroupPtr);   // 等 WAL 落盘
        csnMgr->WaitUpperBoundSatisfy(csn);   // 等 CSN 上界（Lamport 时钟模式）
    }
    WriteTxnInfoToCache(xid, *slot, csn);     // 写入热缓存
}
```

**关键设计**：先解锁再等待 WAL 落盘。好处：
- 其他事务可以立即看到 COMMITTED 状态（status 已在内存中）
- 但 `CopySlot()` 会检测 `IsCommitWalPersist()`，如果 WAL 未落盘则降级为 PENDING_COMMIT
- 避免了持锁等 I/O 造成的竞争瓶颈

### 3.3 RecycleTxnSlots：GC 核心

```
RecycleTxnSlots(recycleMinCsn):
  while recycleLogicSlotId < nextFreeLogicSlotId:
    IsSlotRecyclable(slotId, recycleMinCsn)?
      NOT: IN_PROGRESS → 返回（有活跃事务，截断）
      NOT: PENDING_COMMIT → 返回（提交中，截断）
      NOT: csn >= recycleMinCsn → 返回（CSN 太新，截断）
      YES: slot.status = TXN_STATUS_FROZEN
           recycleUndoPtr = slot.spaceTailUndoPtr  ← 记录最后分配的 Undo 位置
    recycleLogicSlotId++
    换页时: MarkDirty + WAL_UNDO_RECYCLE_TXN_SLOT
  m_recycleLogicSlotId.store(recycleLogicSlotId)
```

**IsSlotRecyclable 判断逻辑**（zone_txn_mgr.cpp:244）：
```
slotStatus == IN_PROGRESS  → false（活跃）
slotStatus == PENDING_COMMIT → false（活跃）
slotCsn == INVALID_CSN     → true（空事务，无 Undo，可回收）
slotCsn < recycleMinCsn    → true（所有活跃快照都比这个事务新，可回收）
slotCsn >= recycleMinCsn   → false（太新，可能还有快照需要其 Undo）
  并设 nextToBeRecycledIsCommitted = true  ← 标记是"已提交但太新"
```

### 3.4 RollbackTxnSlot：回滚后设 ABORTED

```cpp
void UndoZoneTrxManager::RollbackTxnSlot(Xid xid) {
    csnMgr->GetNextCsn(csn, false);         // 分配一个"伪CSN"
    slot->SetTrxSlotStatus(TXN_STATUS_ABORTED);
    slot->SetCsn(csn);                       // 设 CSN，避免过早被 GC
    MarkDirty + WAL(WAL_TXN_ABORT);
}
```

为什么 ABORTED 也要设 CSN？
> 活跃的 MVCC 读取者可能在 CR 页中缓存了该事务的"幻象数据"。在 csn（即所有快照都比这个新）之前，不能回收该事务槽，否则 CR 页中的数据会被错判为可见。

---

## 四、InsertUndoRecord：跨页写入详解

### 4.1 整体流程（zone.cpp:637）

```
InsertUndoRecord(record):
  recPtr = m_nextAppendUndoPtr        ← Step 1: 记录起始地址
  
  record->Serialize()                 ← Varint 压缩 header
  
  loop:                               ← Step 2: 可能跨多页写入
    ① 复用/Pin 当前页 Buffer（m_currentInsertUndoPageBuf）
    ② WriteUndoRecord(record, page, startingByte, alreadyCopy)
       → 写完当页能写的字节
    ③ GenerateWalForUndoRec(buf, startingByte, walDataSize)
       → WAL：WAL_UNDO_INSERT_RECORD
    ④ MarkDirty(buf)
    ⑤ if 写完: break
       else: 跟随 page->GetNextPageId() 切换到下一页
             m_nextAppendUndoPtr 更新为新页首
             m_needCheckPageId = nextPage->GetNextPageId()
  
  record->DestroyDiskData()           ← 释放序列化内存
  
  Step 3: 更新 m_nextAppendUndoPtr.offset  ← 指向本记录末尾
  return recPtr                            ← 返回本记录的起始地址
```

### 4.2 Buffer 复用优化

```cpp
// m_currentInsertUndoPageBuf 是 Zone 成员变量，跨调用保持
if (m_currentInsertUndoPageBuf == INVALID_BUFFER_DESC) {
    m_currentInsertUndoPageBuf = bufMgr->Read(..., LW_EXCLUSIVE);  // 首次：全量读
} else {
    m_currentInsertUndoPageBuf->Pin();
    if (页面ID不同) {
        Unpin(); 重新Read;
    } else {
        LockContent(LW_EXCLUSIVE);  // 同一页面：只加锁，不读
    }
}
```

同一事务连续插多条 Undo 时，大概率写同一页，这个优化减少了 BufTable 哈希查找次数。

### 4.3 InsertUndoBytes：精巧的断点续写

```cpp
bool InsertUndoBytes(src, writePtr, endPtr, myBytesWritten, alreadyWritten):
    if myBytesWritten >= srcLen:
        myBytesWritten -= srcLen    // 这段已经写完，跳过
        return true
    
    remaining = srcLen - myBytesWritten
    canWrite = min(remaining, endPtr - writePtr)
    memcpy(writePtr, src + myBytesWritten, canWrite)
    
    *writePtr += canWrite
    alreadyWritten += canWrite
    myBytesWritten = 0              // 写了一部分，下次从0开始（因为已更新 src+alreadyWritten 隐式追踪）
    
    return (canWrite == remaining)  // true=这段写完了
```

`alreadyCopy`（外层传入）跟踪总已写字节数。切换到下一页时，`alreadyCopy` 保持不变，作为"从哪里续写"的断点依据。

### 4.4 全局临时表优化

```cpp
uint8 globalTempTableUndoWalSize = record->GetCompressedSize() + sizeof(record->m_dataInfo.len);
if (record->IsGlobalTempTableUndoRec() && alreadyCopy >= globalTempTableUndoWalSize) {
    walFinished = true;   // WAL 只写 header+len，不写 data
}
```

全局临时表数据不跨节点，崩溃后也不用 Redo，所以 Undo 的旧数据段不写 WAL，节省 WAL I/O。

---

## 五、FetchUndoRecord：带回收检测的读取

```
FetchUndoRecord(pdbId, record, undoRecPtr, xid, bufferMgr, commitCsn):

  1. 回收检测：
     if commitCsn 有效:
       if *commitCsn < recycleCsn → 返回 UNDO_ERROR_RECORD_RECYCLED（已回收）
       else needSkipJudge = true   → 跳过 Xid 检测（已知是已提交事务）
     if !needSkipJudge:
       IsXidRecycled(xid)?         → FROZEN 或 logicSlotId 不匹配 → 已回收
  
  2. FetchUndoRecordInternal():
     a. Buffer 复用（同 InsertUndoRecord 一样的 m_currentFetchUndoPageBuf 机制）
     b. ReadUndoRecord()           → 按三段读取
     c. 校验 IsUndoDataValid()     → 失败则 PANIC 或记错误日志
     d. 校验 PreUndoPtr 指向的文件存在  → 防止表空间被 DROP 后悬空指针

  3. 返回填充好的 record
```

**IsXidRecycled 判断（zone_txn_mgr.cpp:297）**：
```cpp
if (slot.status == TXN_STATUS_FROZEN || slot.logicSlotId != xid.m_logicSlotId)
    return recycled = true;   // 槽已被重用
if (slot.status == TXN_STATUS_COMMITTED && slot.csn < recycleCsn)
    return recycled = true;   // 已提交且 CSN 太旧（GC 标记了 FROZEN 但还没落盘）
```

---

## 六、Recycle：Undo 空间的两层回收

### 6.1 两级指针

```
m_nextAppendUndoPtr     ← 当前写入位置（实时推进）
m_needCheckPageId       ← 当前写入页的 next 指针（预警：若与回收页重合则扩容）
m_undoRecyclePageId     ← 已回收到哪一页（向前追赶 nextAppend）

环形链表（Undo Page Ring）：
  page1 ↔ page2 ↔ page3 ↔ page4 ↔ page5 ↔ (回到 page1)
          ↑                        ↑
     undoRecycle          nextAppend（写入点）

当 m_needCheckPageId == m_undoRecyclePageId:
  写入点已追上回收点 → 必须扩容（ExtendSpaceIfNeeded）
```

### 6.2 Recycle 流程（zone.cpp:392）

```
Recycle(recycleMinCsn):
  ① m_txnSlotManager->RecycleTxnSlots()
     → 遍历 Slot，找最后一个可回收 slot 的 spaceTailUndoPtr
     → recycleUndoPtr = 该 slot 的最后一条 Undo 记录的地址

  ② if recycleUndoPtr == INVALID: return（没有可回收的）

  ③ FetchUndoRecordInternal(recycleUndoPtr)  读取该记录
     → endPtr = GetNextUndoRecPtr(recycleUndoPtr, record.size)  ← 记录结束位置

  ④ RecycleUndoPage(endPtr):
     → 从 m_undoRecyclePageId 遍历到 endPtr.pageId（计数页数）
     → m_undoRecyclePageId = endPtr.pageId
     （逻辑回收：仅移动指针，物理页面由 ExtendSpaceIfNeeded 在扩容时实际复用）
```

### 6.3 环形扩容（ExtendSpaceIfNeeded → ExtendUndoPageRing）

```
触发条件: m_needCheckPageId == m_undoRecyclePageId（写入追上回收）

ExtendUndoPageRing(firstFree, lastFree):
  Step 1: 更新 prev 页面的 next → firstFree  [WAL_UNDO_EXTEND_PAGE_RING_PREV_PAGE]
  Step 2: 更新 next 页面的 prev → lastFree   [WAL_UNDO_EXTEND_PAGE_RING_NEXT_PAGE]
  Step 3: 初始化新页面链表（prevPageId → curPage → nextPageId）
          每页: [WAL_UNDO_EXTEND_PAGE_RING_NEW_PAGE]

扩容前:  ... → curWrite → nextPage → ...
扩容后:  ... → curWrite → first_new → ... → last_new → nextPage → ...
```

---

## 七、RollbackUndoRecords：回滚主循环详解

```
RollbackUndoRecords(xid, startUndoPtr, endUndoPtr, isRecovery):
  nextRollbackUndoPtr = endUndoPtr  ← 从 tailUndoPtr 开始（最新的 Undo）

  while nextRollbackUndoPtr != startUndoPtr:
    currUndoPtr = nextRollbackUndoPtr
    
    ① FetchUndoRecord(currUndoPtr, xid)
       → 读取 Undo 记录（含回收检测）
    
    ② nextRollbackUndoPtr = undoRec.GetTxnPreUndoPtr()
       ← 沿事务链往前（更早的 Undo）
    
    ③ if 全局临时表 + isRecovery: 只推进 SlotUndoPtr，不回滚页面 → continue
    
    ④ bufMgr->Read(undoRec.GetPageId(), LW_EXCLUSIVE)  ← 锁住目标数据页
    
    ⑤ IsPageTypeMatchUndoRecordType(page, undoRec)
       → 检查页面类型是否与 Undo 类型匹配（Heap/BTree）
       → 不匹配说明 DDL 已删除该表 → continue 跳过
    
    ⑥ if IsBtreeUndoRecord():
          BtreeUndoContext::FindUndoRecRelatedPage()
          ← BTree 可能因 Split 移动，需沿右兄弟搜索实际页面
    
    ⑦ BeginAtomicWal(xid)
    
    ⑧ if page.GetTd(tdId).m_xid == xid:   ← 检查 TD 归属
          page->RollbackByUndoRec(&undoRec)  ← 真正回滚（逆操作）
          MarkDirty(buf)
          GenerateWalForRollback()  [WAL_UNDO_HEAP 或 WAL_UNDO_BTREE]
       else:
          跳过（另一事务已经占用了该 TD 槽）
    
    ⑨ SetSlotUndoPtr(xid, nextRollbackUndoPtr)  ← 持久化"已回滚到哪里"
       ← 每步持久化，崩溃后可从断点继续
    
    ⑩ UnlockAndRelease(buf)
       EndAtomicWal()

  BtreeUndoContext cleanup
```

**步骤 ⑧ 的跳过原因**：
> 如果另一个事务已经在同一 TD 槽上写了新 Undo（即 m_xid != xid），说明这个 TD 被复用了，该回滚已经由 IS_PREV_XID_CSN 机制隐含处理，无需再次回滚。

**步骤 ⑨ 的崩溃恢复意义**：
> `curTailUndoPtr` 在每条记录回滚后推进。崩溃重启时，从 `curTailUndoPtr` 继续，避免重复回滚。

---

## 八、RestoreUndoZoneFromTxnSlots：崩溃恢复

```
RestoreUndoZoneFromTxnSlots():

Step 1: m_txnSlotManager->Init(startPageId, false)  ← 不初始化页面，只加载
Step 2: m_txnSlotManager->Restore()
        → 扫描 TxnSlot 页面，重建 nextFreeLogicSlotId / recycleLogicSlotId
        → 返回最后活跃事务的 tailUndoPtr + tailPtrStatus

Step 3: 根据 tailPtrStatus 处理：

   VALID_STATUS:
     tailUndoPtr = 最后一条活跃 Undo 记录的地址
   
   NEED_FETCH_FROM_COMMITED_SLOT:
     没有活跃事务，但有已提交且未回收的 Slot
     GetFirstSpaceUndoRecPtr() → 从最旧未回收 Slot 找 spaceTailUndoPtr
     tailUndoPtr = 该 slot 的 spaceTailUndoPtr
   
   NO_VALID_TAIL_UNDO_PTR:
     Zone 完全空（只有一个空进行中事务）

Step 4: 定位 m_undoRecyclePageId
   if tailUndoPtr 有效:
     从 tailUndoPtr 沿 m_txnPreUndoPtr 走到头（第一条 Undo）
     m_undoRecyclePageId = 第一条 Undo 的 pageId

   if tailUndoPtr 无效:
     从 UndoSegmentMetaPage 读取 firstUndoPageId
     m_undoRecyclePageId = firstUndoPageId

Step 5: RestoreUndoWritePtr()
   找最后一条分配的 Undo 记录 → GetNextUndoRecPtr() → m_nextAppendUndoPtr
   m_needCheckPageId = m_nextAppendUndoPtr 所在页的 next

Step 6: IsUndoZoneNeedRollback()?
   最后一个 Slot 是 IN_PROGRESS → AsyncRollback()

Step 7: undoMgr->AddZoneOwned(zoneId)  ← 宣布 Zone 可用
```

---

## 九、UndoZoneTrxManager::Restore：Slot 页面扫描重建

```
Restore():
Step 1: 重建 m_nextFreeLogicSlotId
  遍历 TRX_PAGES_PER_ZONE(127) 个页面
  每页: 读取 txnSlotPage->GetNextFreeLogicSlotId()
  取最大值（页面单调递增写入）
  
  边界：若某页的 nextFreeLogicSlotId <= 当前累计值 → 后续页面无效 → break

Step 2: 验证下一个空闲 Slot
  读取 nextFreeLogicSlotId 对应页面
  检查该 Slot 的 logicSlotId 是否异常大 → PANIC

Step 3: 重建 m_recycleLogicSlotId
  从 nextFreeLogicSlotId 对应页开始，向前找最旧的已用页面
  "已用" = 最后一个 Slot 的 status 不是 UNKNOWN/FROZEN

Step 4: 调用 RecycleTxnSlots(recycleMinCsn)
  再次过一遍，把已经满足 recycleMinCsn 的 Slot 直接标记 FROZEN
  返回 tailPtrStatus（VALID / NEED_FETCH / NO_VALID）
```

---

## 十、UndoMgr：Zone Map 全局管理

### 10.1 Zone Map 结构

UndoMgr 用一个独立 Segment（**Zone Map Segment**）记录 `ZoneId → PageId(SegmentId)` 的映射：

```
Control File
  └── undoZoneMapSegmentId (PageId)
        └── Zone Map Segment
              ├── ExtentMetaPage          ← Segment 自身元数据
              └── Map Data Pages (SEGMENT_ID_PAGES 个)
                    每页存储若干 PageId（每个 ZoneId 的 SegmentId）
                    offset = sizeof(Page) + zid_in_page * sizeof(PageId)
```

关键：`m_mapStartPage.m_blockId++` 跳过 ExtentMeta 页面，直接指向第一个数据页。

### 10.2 GetUndoZone：懒加载三段式

```cpp
GetUndoZone(zid, canCreate):
  ① 原子读 m_undoZones[zid] → 已有则直接返回（快路径）
  ② m_fullyInited == false → 返回 UNDO_ERROR_NOT_FULLY_INITED
     （Map Segment 还没加载完，不能定位 Zone 的物理位置）
  ③ GetUndoZoneSegmentId(zid) → segmentId
     ├── segmentId 有效 → LoadUndoZone(zid, segmentId)   ← 加载已有 Zone
     └── segmentId 无效:
         canCreate==true  → CreateUndoZone(zid)          ← 首次创建
         canCreate==false → return FAIL
```

`m_fullyInited`（std::atomic_bool）在 `LoadUndoMapSegment()` 完成后通过 `store(true, release)` 设置，GetUndoZone 用 `load(acquire)` 读取，保证可见性。

### 10.3 CreateUndoZone vs LoadUndoZone

```
CreateUndoZone(zid):           [首次创建，对应新会话第一次分配 Slot]
  pthread_rwlock_wrlock()
  AllocUndoSegment()           ← 在 UNDO_TABLE_SPACE 中分配新 Segment
  SetUndoZoneSegmentId()       ← 写入 Zone Map（含 WAL_UNDO_SET_ZONE_SEGMENT_ID）
  AllocateZoneMemory()         ← new UndoZone + zone->Init()
  pthread_rwlock_unlock()

LoadUndoZone(zid, segmentId): [重启恢复，加载已有 Zone]
  pthread_rwlock_wrlock()
  new Segment(segmentId)       ← 不分配，只包装已有 Segment
  segment->Init()              ← 读取 Segment 元数据
  AllocateZoneMemory()         ← 同上
  pthread_rwlock_unlock()
```

锁是 `m_zoneLocks[zid % MAX_THREAD_NUM]`（分段 rwlock），减少 Zone 并发加载时的竞争。

### 10.4 RecoverUndoZone：启动时后台扫描

```
RecoverUndoZone():
  LoadUndoMapSegment()        ← 加载 Map Segment（设 m_fullyInited=true）
  scan SEGMENT_ID_PAGES 页:
    for each 页面:
      bufMgr->Read(page)
      copy page content to local rawPage（LW_SHARED，立刻释放）
      for each slot in page:
        m_needStopRecover?  → return（支持优雅中断）
        pageId = rawPage[offset]
        if valid: LoadUndoZone(zid, pageId)
        thrd->RefreshWorkingVersionNum()  ← 防止被 GC 线程标记为"僵尸"
```

注意：每个 ZoneId 只占一个 `sizeof(PageId)` 的位置，SEGMENT_ID_PAGES * SEGMENT_ID_PER_PAGE 覆盖所有 UNDO_ZONE_COUNT 个 Zone。

### 10.5 Recycle：全局协调回收

```cpp
void UndoMgr::Recycle(CommitSeqNo recycleMinCsn) {
    for (ZoneId i = 0; i < UNDO_ZONE_COUNT; ++i) {
        if (pdb->IsNeedStopBgThread()) return;        // 支持优雅停止
        undoZone = m_undoZones[i];                    // 原子读
        if (undoZone && undoZone->GetUndoRecyclePageId().IsValid()) {
            thrd->RefreshWorkingVersionNum();
            undoZone->Recycle(recycleMinCsn);         // 调用各 Zone 的回收
        }
    }
}
```

---

## 十一、RollbackTrxWorker：双线程安全架构

Worker 采用两级线程来处理索引回滚可能触发 SQL 线程初始化的风险：

```
WorkerMain Thread（轻量线程，由 RollbackTrxTaskMgr 的 Dispatch 分发）
  ↓ 启动并 join
RollbackSubMain Thread（SQL 线程上下文，支持索引操作）
  ↓ 调用
DoRollback() → zone->RollbackUndoZone(xid, isAsync=true)
```

**为什么要两级**（代码注释明确说明）：

> Index may need access SQL thrd, ERR_LEVEL_FATAL may happen when init SQL thrd, then current thread may be killed, so need create sub-thrd to init SQL thrd to avoid dstore resource leaks.

若直接在 WorkerMain 中初始化 SQL 线程，FATAL 会杀掉 WorkerMain，导致 UndoZone 锁未释放、任务丢失。通过子线程隔离，WorkerMain 可以安全地执行清理（UnregisterThread、Destroy）。

**失败重试**：

```cpp
if (m_rollbackResult == DSTORE_SUCC) {
    zone->SetAsyncRollbackState(false);  // 成功：标记完成
} else {
    taskMgr->AddRollbackTrxTask(xid, zone);  // 失败：重新入队
}
Destroy();
taskMgr->WakeupDispatch();  // 唤醒调度器分配下一个任务
```

---

## 十二、AllUndoZoneTxnInfoCache：无锁事务信息热缓存

MVCC 读取时频繁查询事务状态（每次 `CopySlot()` 都要读 Txn Slot 页面），TxnInfoCache 通过两级缓存将热路径提升到 O(1)。

### 12.1 两级架构

```
第一级: m_recycleLogicSlotId[UNDO_ZONE_COUNT]  （per-zone 原子 uint64）
  → 若 xid.logicSlotId < recycleLogicSlotId[zid]
    → 直接返回 TXN_STATUS_FROZEN（无需任何页面读取）

第二级: m_cachedEntry[UNDO_ZONE_COUNT]         （per-zone 缓存数组，懒分配）
  每个 zone 的数组：CachedTransactionSlot[CACHED_SLOT_NUM_PER_ZONE]
  定址：slotId = xid.logicSlotId % CACHED_SLOT_NUM_PER_ZONE
```

### 12.2 无锁 128-bit CAS

`CachedTransactionSlot.placeHolder` 是 16 字节（128 bit）的原子量：

```cpp
struct CachedTransactionSlot {
    union {
        volatile uint128_u placeHolder;    // 128-bit 原子操作单元
        struct {
            CommitSeqNo csn;               // 8B
            TrxSlotStatus status;          // 4B
            uint32 logicSlotId;            // 4B（低32位，取 logicSlotId % 2^32）
        } txnInfo;
    };
};
```

读取：`atomic_compare_and_swap_u128(&slot.placeHolder)` — 无锁原子读
写入：CAS 循环，失败则重读再写

### 12.3 缓存命中逻辑

```cpp
ReadTxnInfoFromCache(xid):
  // Level 1: watermark 快速判断
  if logicSlotId < recycleLogicSlotId[zid]:
      return FROZEN（命中率高：大量旧事务）
  
  // Level 2: 缓存槽读取
  slotId = logicSlotId % CACHED_SLOT_NUM_PER_ZONE
  slotInfo = atomic_read_128bit(m_cachedEntry[zid][slotId])
  
  if slotInfo.csn == INVALID_CSN: return MISS（未缓存）
  if slotInfo.logicSlotId != xid.logicSlotId: return MISS（槽被其他事务占用）
  
  outSlot = {status, csn, logicSlotId}
  RefreshRecycleLogicSlotId(xid, csn, recycleCsnMin)  // 顺便推进水位
  return HIT
```

### 12.4 缓存更新策略

```cpp
WriteTxnInfoToCache(xid, slot, recycleCsnMin, cacheInprogress):
  // 第一步: 推进 recycleLogicSlotId watermark
  if status in {FROZEN, COMMITTED, ABORTED}:
      RefreshRecycleLogicSlotId(xid, csn, recycleCsnMin)
  
  // 第二步: 写入缓存数组（条件）
  if cacheInprogress || (csn >= recycleCsnMin && status in {FROZEN/COMMITTED/ABORTED}):
      写入 m_cachedEntry[zid][slotId]（128-bit CAS 循环）
  
  // 原因：若 csn < recycleCsnMin → watermark 已覆盖，缓存数组无意义
```

**懒分配**：首次 Write 时，CAS 尝试分配数组，若并发时其他线程已分配则释放自己的分配，保证只有一个数组被使用。

---

## 十三、WAL 分发机制（dstore_undo_wal.cpp）

### 13.1 三张调度表

```cpp
// 表1: Redo 调度
UNDO_WAL_REDO_TABLE[] = {
    {WAL_TYPE, [](WalRecordUndo*, Page*) { cast_and_call_Redo(); }},
    ...
}

// 表2: Dump 调度（诊断/调试用）
UNDO_WAL_DUMP_TABLE[] = {
    {WAL_TYPE, [](WalRecordUndo*, FILE*) { cast_and_call_DumpUndo(); }},
    ...
}

// 表3: Compress/Decompress（WAL 压缩传输）
COMPRESS_AND_DECOMPRESS_TABLE[MAX_UNDO_WAL_TYPE_SIZE] = {
    {WAL_TYPE, headerSize, GetMaxCompressedSize, Compress, Decompress},
    ...
}
```

### 13.2 RedoUndoRecord 实现

```cpp
void WalRecordUndo::RedoUndoRecord(redoCtx, undoRecord, bufferDesc):
  page = bufferDesc->GetPage()
  
  if recordType == WAL_UNDO_ALLOCATE_TXN_SLOT:
      // 特殊处理：需要 redoCtx->xid
      cast<WalRecordUndoTxnSlotAllocate>->Redo(page, redoCtx->xid)
  else:
      // 表驱动分发
      for item in UNDO_WAL_REDO_TABLE:
          if item.type == recordType: item.redo(undoRecord, page)
  
  // 更新 GLSN（跨 WAL 流时 +1）
  glsn = (page.walId != redoCtx.walId) ? page.preGlsn + 1 : page.preGlsn
  page->SetLsn(walId, recordEndPlsn, glsn)
```

**GLSN 跨流递增规则**：若页面上一次修改和本次修改属于不同 WAL 流（walId 不同），则 glsn+1；同一流内不递增。这是多流 WAL 架构下全局版本号（GLSN）的维护方式。

### 13.3 WalRecordUndoRingOldPage::Redo

```cpp
void WalRecordUndoRingOldPage::Redo(Page *page) const {
    if (m_type == WAL_UNDO_EXTEND_PAGE_RING_PREV_PAGE)
        page->m_undoRecPageHeader.next = adjacentPageId;  // 前驱页更新其 next
    else if (m_type == WAL_UNDO_EXTEND_PAGE_RING_NEXT_PAGE)
        page->m_undoRecPageHeader.prev = adjacentPageId;  // 后继页更新其 prev
}
```

同一个类（`WalRecordUndoRingOldPage`）复用处理两种 WAL 类型，通过 `m_type` 区分操作方向。

---

## 十四、异步回滚调度器（RollbackTrxTaskMgr）

```
结构：
  Dispatch Thread（主控）
    ↓ 分发 RollbackTrxTask
  Worker[0..9]（RollbackTrxWorker）
    每个 Worker 自创两级 std::thread（WorkerMain + RollbackSubMain）

DispatchMain 线程启动：
  ThreadContext 完整初始化（含 StorageContext）
  AddVisibleThread("RollbackTrxMgr")
  DoDispatch() 主循环

DoDispatch 主循环：
  loop:
    if m_needStop && IsAllTaskFinished(): return
    idleWorker = GetNextIdleWorker()    // 轮询找空闲 Worker
    task = GetNextRollbackTrxTask()     // 从 dlist 队列取任务（SpinLock 保护）
    if both valid: worker->SetTask(task); worker->Run()
    else: 等待 notify（condition_variable）
    sleep 指数退避（1s → 2s → ... → maxSleepSeconds）
```

**AddRollbackTrxTask 触发路径**：
```
UndoZone::RestoreUndoZoneFromTxnSlots() → IsUndoZoneNeedRollback()
  → TransactionMgr::AsyncRollback(rollbackXid, undoZone)
      → RollbackTrxTaskMgr::AddRollbackTrxTask(xid, zone)
          → dlist 入队 + WakeupDispatch()（notify_one）
```

**MAX_ROLLBACK_WORKER_NUM = 10**（头文件中定义，对应之前 Day 6 的笔记）

---

## 十五、关键发现总结（.h vs .cpp 的差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| InsertUndoRecord | "五步" | 实际是跨页循环写入，含缓存 Buffer 复用和断点续写 |
| PENDING_COMMIT | "等待场景" | CopySlot 读取时动态降级：WAL 未落盘 → 强制 PENDING |
| Recycle 时机 | "recycleMinCsn 驱动" | 两指针追逐（needCheck vs recyclePageId），只回收整页 |
| Rollback 原子性 | "多阶段可重入" | SetSlotUndoPtr 每步持久化，TD.xid != xid 则跳过 |
| Restore 流程 | "崩溃恢复" | 三种 tailPtrStatus 分支处理，Slot 页面单调扫描重建 |
| 全局临时表 | "不写 WAL" | InsertUndoRecord 中特判：WAL 只写 header+len |
| BTree 回滚 | "回滚=删索引项" | 需 FindUndoRecRelatedPage 跟随 Split 找实际页面 |
| 批量 INSERT | "一条 Undo 对应多行" | IsMatchedCtid 检查 offset 在范围数组中 |
| UndoZone 加载 | "按需创建" | 懒加载 + m_fullyInited 原子门控，Zone Map 存控制文件 |
| 异步回滚 Worker | "后台线程回滚" | 两级线程：WorkerMain + RollbackSubMain（隔离 SQL 初始化 FATAL） |
| 事务状态查询 | "读 Txn Slot 页面" | 两级缓存：watermark O(1) + 128-bit CAS 无锁缓存数组 |
| WAL Redo 分发 | "按类型 Redo" | 两张静态表（Redo/Dump）+ GLSN 跨流递增规则 |

---

## 附：Undo 模块 WAL 记录类型全表

以下完整枚举来自 `UNDO_WAL_REDO_TABLE` 和 `COMPRESS_AND_DECOMPRESS_TABLE`：

```
写入相关:
  WAL_UNDO_INSERT_RECORD           插入 Undo 记录到 UndoRecordPage
  WAL_UNDO_ALLOCATE_TXN_SLOT       分配事务槽（AllocSlot；Redo 需要 xid，特殊处理）
  WAL_UNDO_UPDATE_TXN_SLOT_PTR     推进 curTailUndoPtr 和 spaceTailUndoPtr

提交/回滚:
  WAL_TXN_COMMIT                   事务提交（含 csn 和 status）
  WAL_TXN_ABORT                    事务回滚（含伪 csn）
  WAL_UNDO_HEAP                    回滚 Heap 页面的 Undo 操作（RollbackByUndoRec）
  WAL_UNDO_BTREE                   回滚 BTree 页面的 Undo 操作
  WAL_UNDO_HEAP_PAGE_ROLL_BACK     Heap 页面级别回滚（整页）
  WAL_UNDO_BTREE_PAGE_ROLL_BACK    BTree 页面级别回滚（整页）

GC:
  WAL_UNDO_RECYCLE_TXN_SLOT        批量 FROZEN 事务槽（RecycleTxnSlots 换页时）

扩容:
  WAL_UNDO_EXTEND_PAGE_RING_PREV_PAGE  更新前驱页的 next 指针
  WAL_UNDO_EXTEND_PAGE_RING_NEXT_PAGE  更新后继页的 prev 指针
  WAL_UNDO_EXTEND_PAGE_RING_NEW_PAGE   初始化新 Undo Record 页

Zone Map:
  WAL_UNDO_INIT_MAP_SEGMENT        初始化 Zone Map 数据页（CreateUndoMapSegment）
  WAL_UNDO_SET_ZONE_SEGMENT_ID     写入 ZoneId → SegmentId 映射

初始化:
  WAL_UNDO_INIT_TXN_PAGE           初始化事务槽页（InitTransactionSlotSpace）
  WAL_UNDO_INIT_RECORD_SPACE       初始化 Undo 记录空间（含 firstUndoPageId）
  WAL_UNDO_SET_TXN_PAGE_INITED     标记 alreadyInitTxnSlotPages = true
```

共 18 种 WAL 类型，均有对应的 Redo 和 Compress/Decompress 实现。
