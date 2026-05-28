# Day 10 — 综合串联 + 扩展模块

> **目标**：把 Day 1～9 所有模块串成一张完整的地图，补全逻辑复制、Flashback、Barrier 等扩展模块，并梳理后台线程全景、GC 协同、PDB 多租户隔离边界。

---

## 一、10 天知识全景地图

```
╔══════════════════════════════════════════════════════════════════════╗
║                  dstore 存储引擎 — 全局架构                          ║
║                                                                      ║
║  StorageInstance (全局唯一，进程级)                                  ║
║  ├─ BufMgrInterface        ← Day 3  所有页面的内存缓冲池              ║
║  ├─ LockMgr / TableLockMgr / XactLockMgr  ← Day 9  三层锁管理       ║
║  ├─ CsnMgr                 ← Day 4  全局提交序号，驱动 MVCC           ║
║  └─ StoragePdb[MAX_PDB_COUNT]                                        ║
║                                                                      ║
║  StoragePdb (每数据库一个，多租户)                                   ║
║  ├─ TransactionMgr         ← Day 4  XID/CSN/Snapshot/提交/回滚      ║
║  ├─ UndoMgr                ← Day 6  版本链 + 历史快照重建            ║
║  ├─ WalManager             ← Day 5  WAL写入/多流/崩溃恢复            ║
║  ├─ CheckpointMgr          ← Day 5  diskRecoveryPlsn/WAL截断        ║
║  ├─ TablespaceMgr          ← Day 9  物理存储管理/Segment/Extent      ║
║  ├─ LogicalReplicaMgr      ← Day 10 逻辑复制槽+解码                 ║
║  └─ BgPageWriterMgr        ← Day 3  脏页异步刷盘                    ║
║                                                                      ║
║  页面层（存在于 Buffer + Tablespace）                                ║
║  ├─ HeapPage               ← Day 7  行存储/TD机制/MVCC可见性         ║
║  │   └─ BigTuple           ← Day 7  超大行跨页链式存储               ║
║  ├─ BtrPage (Index)        ← Day 8  BTree 索引/SMO 安全/索引MVCC    ║
║  ├─ UndoPage               ← Day 6  Undo记录存储                    ║
║  ├─ FsmPage                ← Day 7  空闲空间位图树                  ║
║  └─ TbsDataFile Pages      ← Day 9  表空间位图/段元数据             ║
╚══════════════════════════════════════════════════════════════════════╝
```

---

## 二、PDB 多租户隔离边界

### 2.1 全局共享 vs PDB 私有

```
┌─────────────────────────────────────────────────────────┐
│                  全局共享（跨所有PDB）                   │
│                                                         │
│  BufMgrInterface          — 所有PDB的页面共用一个缓冲池  │
│  LockMgr / TableLockMgr   — 行/表级锁（含死锁检测）     │
│  XactLockMgr              — 事务等待锁（含PENDING_COMMIT）│
│  CsnMgr                   — 全局CSN单调递增，MVCC时间轴  │
│  ThreadCoreMgr            — 线程/CPU绑定管理            │
│  StorageMemoryMgr         — 全局内存池                  │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                  每PDB私有（完全隔离）                   │
│                                                         │
│  TransactionMgr           — XID空间、Snapshot、提交状态 │
│  UndoMgr                  — Undo Zone（XID.zoneId定位） │
│  WalManager               — WAL流（WalId空间、PLSN）    │
│  CheckpointMgr            — diskRecoveryPlsn            │
│  TablespaceMgr            — 物理文件、Segment、Extent    │
│  LogicalReplicaMgr        — 逻辑复制槽、DecodeDict      │
│  ControlFile              — PDB版本、WAL流注册表        │
└─────────────────────────────────────────────────────────┘
```

### 2.2 PdbId 与 XID 的绑定关系

```
XID 结构：
  ┌────────────────┬──────────────────────────────────────┐
  │  zoneId (20bit)│          logicSlotId (44bit)          │
  └────────────────┴──────────────────────────────────────┘

  zoneId → UndoZone（同一 PDB 内，每事务一个 Zone）
  逻辑：XID 是 Undo Zone 的物理地址，O(1) 定位事务槽

PDB 切换：AutoPdbCxtSwitch（include/framework/dstore_instance.h:51）
  构造时: thrd->SetXactPdbId(newPdbId)
  析构时: thrd->SetXactPdbId(oldPdbId)
  — RAII 确保线程上下文与目标 PDB 绑定
```

### 2.3 PDB 状态机

```
PdbStatus:
  PDB_STATUS_CLOSED
    → OpenPdb()
  PDB_STATUS_OPENED_READ_WRITE  ← 正常工作状态
    → ClosePdb()
  PDB_STATUS_CLOSING            ← 关闭中（仍可内部访问）
    → 完成 → CLOSED

PdbRoleMode:
  PRIMARY   — 可读写，生成WAL
  STANDBY   — 只读，回放WAL（Redo）
  PROMOTING — 主备切换中
```

---

## 三、完整 INSERT 时序（跨全部模块）

```
用户：INSERT INTO t VALUES (1, 'hello')
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1: 事务初始化
  TransactionMgr::StartInternal()
    ├─ CsnMgr::AllocXid()           → XID = zoneId(20) + logicSlotId(44)
    ├─ UndoZone::CreateTxnSlot()    → TransactionSlot{status=IN_PROGRESS}
    └─ SetSnapshotCsn()             → snapshotCsn（三步原子模拟）
       ├─ MAX_CSN 占位（防GC中途清理）
       ├─ 读当前 committedCsn
       └─ 原子更新 MIN

Step 2: 锁表（TableLockMgr）
  TableLockMgr::LockTable(rel, ROW_EXCLUSIVE)
    ├─ 弱锁快速路径: LocalLockEntry（线程本地，无共享内存）
    └─ 强锁: LockHashTable 分区 → LockEntry.grantedQueue(SkipList)

Step 3: 获取 Heap 页（BufferMgr）
  HeapSegment::GetPageFromFsm(spaceNeeded, retryTime)
    → PartitionFreeSpaceMap::Search()  → PageId
  BufferMgr::Read(pageId, LW_EXCLUSIVE)
    ├─ BufTable[partition].lookup()    → 命中: Pin (thread-local++)
    └─ 未命中: LRU驱逐 → WAL-First检查 → 磁盘读入

Step 4: Heap 页分配 TD（Heap TD机制）
  HeapPage::AllocTd(context)
    ├─ 当前事务已有TD → 复用
    ├─ UNOCCUPY槽 → 直接占用
    ├─ TryReuseTdSlots():
    │   ├─ CSN < recycleMinCsn → canResetTd=true → 完全Reset
    │   └─ CSN >= recycleMinCsn → IS_PREV_XID_CSN，保留CSN
    └─ ExtendTd() → memmove ItemId数组，最多128个TD

Step 5: 写入 Undo（UndoMgr）
  InsertUndoRecord(UNDO_HEAP_INSERT):
    ├─ UndoRecord.m_tdPreInfo = {旧xid, 旧csn, 旧undoRecPtr}  ← 跨事务链入口
    ├─ SetTxnPreUndoPtr(curTailPtr)                           ← 同事务链入口
    ├─ UndoZone::InsertUndoRecord():
    │   ├─ ExtendIfNeeded()
    │   ├─ Serialize(Varint压缩)
    │   ├─ Pin Undo页 → WalRecordUndo → MarkDirty
    │   └─ 更新 nextAppendUndoPtr
    └─ 返回 UndoRecPtr

Step 6: 设置 TD，绑定事务
  HeapPage::SetTd(tdId, xid, undoRecPtr, cid)
    ├─ 旧TD有CSN: IS_CUR_XID_CSN → IS_PREV_XID_CSN（保留CSN值）
    ├─ td->xid = curXid
    ├─ td->undoRecPtr = undoRecPtr
    ├─ td->status = OCCUPY_TRX_IN_PROGRESS
    └─ td->csnStatus = IS_INVALID

Step 7: 写入 Tuple 数据
  HeapPage::AddTuple(tuple)
    ├─ m_upper 向下分配空间
    ├─ HeapDiskTuple.m_tdId = tdId
    ├─ HeapDiskTuple.m_tdStatus = ATTACH_TD_AS_NEW_OWNER
    └─ ItemId 分配（m_lower 向上）

Step 8: 更新索引（BTree）
  BTree::Insert(key, heapCtid)
    ├─ SearchBtreeForInsert() → BtrStack（根到叶路径）
    ├─ CheckUnique()（唯一索引）
    ├─ BtrPage::AllocTd()（与Heap相同机制）
    ├─ InsertUndoRecord(UNDO_BTREE_INSERT)
    └─ BtrPage::AddTuple(indexTuple)
        └─ indexTuple.m_link = heapCtid

Step 9: 生成 WAL（原子组）
  BeginAtomicWal(xid)
    RememberPageNeedWal(heapBufDesc)
    RememberPageNeedWal(indexBufDesc)
    PutRecord(WAL_HEAP_INSERT) + Append(tupleData)
    PutRecord(WAL_BTREE_INSERT_ON_LEAF) + Append(indexData)
  EndAtomicWal()
    ├─ WalStream::Append(buf)
    ├─ SetPagesLSN(heap, walId, plsn, glsn)
    ├─ SetPagesLSN(index, walId, plsn, glsn)
    └─ 返回 WalGroupLsnInfo

Step 10: 标记脏页
  BufferMgr::MarkDirty(heapBuf)  → state |= BUF_CONTENT_DIRTY
                                  → DirtyPageQueue.enqueue()
  BufferMgr::MarkDirty(indexBuf) → (同上)

Step 11: 更新 FSM
  HeapSegment::UpdateFsm(fsmIndex, remainSpace)
    → PartitionFreeSpaceMap::Update()
    → WalRecordTbsFsmMetaUpdateFsmTree
    → MarkDirty(fsmBuf)

Step 12: 提交事务
  TransactionMgr::CommitInternal()
    ├─ 1. PutRecord(WAL_TXN_COMMIT)
    ├─ 2. CsnMgr::AllocCsn()           → commitCsn（全局原子递增）
    ├─ 3. TransactionSlot.status = PENDING_COMMIT, csn = commitCsn
    ├─ 4. WaitTargetPlsnPersist()       ← 等COMMIT WAL落盘（WAL-First）
    ├─ 5. TransactionSlot.status = COMMITTED
    └─ 6. td->SetCsn(csn), SetCsnStatus(IS_CUR_XID_CSN)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
关键时序约束（WAL-First原则）：

  分配XID → 读页面 → AllocTd → WriteUndo → SetTd → AddTuple
     → 写索引 → EndAtomicWal[WAL落盘] → MarkDirty → 提交
                        ↑
              脏页绝不能在 WAL 之前写盘
```

---

## 四、完整 SELECT 时序（含 MVCC 快照读）

```
用户：SELECT * FROM t WHERE id = 1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1: 建立快照
  TransactionMgr::GetSnapshot(SNAPSHOT_MVCC)
    → snapshotCsn = 当前已提交最大CSN
    （READ_COMMITTED: 每条SQL; SERIALIZABLE: 事务开始时固定）

Step 2: BTree 索引查找
  BTree::Search(key=1)
    ├─ GetRoot() → BtrMeta.lowestSinglePage（快速根）
    ├─ 内部节点: BinarySearchOnPage() + downlink 下降
    └─ 叶子节点: 对每个 IndexTuple
        ├─ m_isDeleted=1 → 跳过
        └─ MVCC检查（索引可见性）:
            ├─ insertXid = GetItupInsertXidFromUndo(tdId)
            ├─ deleteXid = GetItupDeleteXidFromUndo(tdId)（有则取）
            └─ XidVisible(insertXid) AND NOT XidVisible(deleteXid)
                → 返回 heapCtid

Step 3: 读取 Heap 页
  BufferMgr::Read(heapPageId, LW_SHARED)
    ├─ 命中 → Pin（refcount++）
    └─ 未命中 → 磁盘读入
        WAL-First 检查: page.plsn > flushedPlsn → WaitTargetPlsnPersist

Step 4: 可见性判断（GetVisibleTuple）
  tuple = HeapPage::GetTuple(heapCtid)
  switch(tuple.m_tdStatus):

    DETACH_TD:
      → 直接可见（TD已清空，Tuple独立存在）

    ATTACH_TD_AS_NEW_OWNER:
      xid = td->GetXid()
      if thrd->IsCurrent(xid):
        → CidVisibleToSnapshot(cid)  ← 同事务内命令可见性
      else:
        XidVisibleToSnapshot(snapshot, xid):
          FROZEN     → 可见
          ABORTED    → 不可见
          PENDING_COMMIT → 等待（XactLockMgr::Wait）
          COMMITTED  → csn <= snapshotCsn ? 可见 : 不可见

    ATTACH_TD_AS_HISTORY_OWNER（TD已被新事务复用）:
      if td->csnStatus == IS_PREV_XID_CSN:
        → XidVisible(td->csn)  ← 快速路径，免查Undo
      else:
        → needUndo = true

Step 5: Undo 链回溯（needUndo = true 时）
  HeapPage::ConstructCrTuple(ctid, tdId, snapshot)
    crTd[] = copy(page->td[])     ← 只读副本，不修改原页面
    loop:
      undoPtr = crTd[tdId].undoRecPtr
      if INVALID → 该行在当前快照不存在（INSERT尚未可见）
      FetchUndoRecordByMatchedCtid(xid, undoPtr, ctid)
        → 沿 m_tdPreInfo.undoRecPtr 跨事务跳转，直到 ctid 匹配
      crTd[tdId].RollbackTdInfo(&record)  ← 回退到前版本
      if XidVisibleToSnapshot(record.tdPreCsn/xid):
        ConstructTupleFromUndo(&record) → 返回历史版本
      else:
        ConstructCrTupleFromUndo(&record) → 继续往更早回溯
```

---

## 五、UPDATE / DELETE 时序

### 5.1 UPDATE

```
HeapPage::UpdateTuple(oldCtid, newTuple)
  ├─ GetBuffer(oldPage, LW_EXCLUSIVE)
  ├─ GetBuffer(newPage, LW_EXCLUSIVE)   ← 可能是同一页面
  ├─ AllocTd(newPage)
  ├─ InsertUndoRecord(UNDO_HEAP_UPDATE):
  │   ├─ 保存旧 Tuple 数据（m_oldData）
  │   └─ m_tdPreInfo = 旧页面 TD 的前版本链接
  ├─ SetTd(newTdId, xid, undoRecPtr)
  ├─ HeapPage::AddTuple(newTuple)       ← 新版本写入新页面
  ├─ 旧 Tuple: 设置 m_isDeleted = true + 记录 newCtid
  ├─ WAL: WalRecord(WAL_HEAP_UPDATE)
  └─ MarkDirty(oldPage), MarkDirty(newPage)

跨页 Update（旧页/新页不同）：
  → UNDO_HEAP_ANOTHER_PAGE_UPDATE（含两端页信息）
```

### 5.2 DELETE

```
HeapPage::DeleteTuple(ctid)
  ├─ GetBuffer(page, LW_EXCLUSIVE)
  ├─ AllocTd(page)
  ├─ InsertUndoRecord(UNDO_HEAP_DELETE):
  │   └─ 保存被删 Tuple 完整数据（用于回滚恢复）
  ├─ SetTd(tdId, xid, undoRecPtr)
  ├─ Tuple.m_isDeleted = true（逻辑删除标记）
  ├─ WAL: WalRecord(WAL_HEAP_DELETE)
  └─ MarkDirty(page)

可见性影响：
  SELECT 看到 m_isDeleted=true → 走 MVCC 判断 deleteXid
  deleteXid 未提交 → 可见（DELETE 尚未生效）
  deleteXid 已提交且 csn <= snapshotCsn → 不可见（已删除）
```

---

## 六、崩溃恢复完整流程

```
系统重启
  └─ StorageInstance::StartupInstance()
       └─ 对每个PDB: StoragePdb::InitRecoveryThreadAndWaitDone()

Phase 1: BuildDirtyPageSet
  从 ControlFile 读取 diskRecoveryPlsn（Checkpoint 记录的安全点）
  扫描 WAL 文件（从 diskRecoveryPlsn 起）:
    → 收集所有被修改页面（WalDirtyPageEntry）
    → 按 GLSN 排序（保证多流修改同页的顺序正确）

Phase 2: ParallelRedo（并行回放）
  Dispatch Thread: 读 WAL 记录 → 按 pageId 哈希分发
  N 个 Redo Worker:
    同一 pageId → 同一 Worker（保序）
    不同 pageId → 不同 Worker（并行）
  每条 WAL 记录: 检查 page.lsn vs record.lsn
    page.lsn >= record.lsn → 跳过（已落盘，幂等）
    page.lsn < record.lsn  → Redo（重放到页面）

Phase 3: FlushDirtyPages
  所有 Redo 完成 → BgDiskPageWriter 刷盘
  WriteBlock(): WAL-First 再次检查（page.plsn ≤ flushedWalPlsn）

Phase 4: Undo Rollback（回滚未完成事务）
  扫描所有 TransactionSlot:
    status=IN_PROGRESS → AbortInternal()（异步，最多10个Worker）
    status=PENDING_COMMIT → 等WAL恢复 → COMMITTED 或 ABORTED

Phase 5: 开放连接
  所有 PDB 恢复完成 → InstanceState::ACTIVE

多流 Failover（主备切换）：
  旧Primary崩溃
  新Primary: TakeOverStreams([旧WalId])
    ├─ 旧流: WRITE_WAL → ONLY_READ
    ├─ 对旧流执行 Recovery（Redo + Undo）
    ├─ 旧流恢复完成 → WalStreamState::RECOVERY_DROPPING
    └─ CreateWritingWalStreamWhenPromoting() ← 新写入流
```

---

## 七、GC 协同机制（recycleMinCsn 驱动）

```
recycleMinCsn = min(localCsnMin, barrierCsnMin, flashbackCsnMin)
  其中:
    localCsnMin   = min(所有活跃事务 snapshotCsn)
    barrierCsnMin = 主备同步 Barrier CSN 的最小值
    flashbackCsnMin = Flashback 功能需要保留的最早 CSN

recycleMinCsn 对各模块的影响：

┌─────────────────┬──────────────────────────────────────────────┐
│ 模块            │ recycleMinCsn 触发的回收动作                 │
├─────────────────┼──────────────────────────────────────────────┤
│ Undo            │ TransactionSlot.csn < recycleMinCsn          │
│                 │ → FROZEN → 回收 UndoRecordPage               │
├─────────────────┼──────────────────────────────────────────────┤
│ Heap TD         │ td.csn < recycleMinCsn AND canResetTd=true   │
│                 │ → TD可完全Reset（xid/undoPtr清零）            │
├─────────────────┼──────────────────────────────────────────────┤
│ BTree TD        │ 同 Heap TD，索引页的 TD 复用规则相同          │
├─────────────────┼──────────────────────────────────────────────┤
│ LogicalRepl     │ m_slotsPlsnMin 阻止 WAL 被截断               │
│                 │ m_slotsCatalogCsnMin 阻止旧 Catalog 被回收   │
└─────────────────┴──────────────────────────────────────────────┘

StoragePdb::m_pdbRecycleCsnMin（per-PDB精细控制）：
  每个 PDB 独立维护 RecycleCsnMin
  当逻辑复制槽 confirmedCsn 远落后时，该 PDB 的回收被整体阻塞
  → 需定期 AdvanceLogicalReplicationSlot() 推进槽 CSN
```

---

## 八、后台线程全景（StoragePdb 维度）

```
StoragePdb 拥有的后台线程：

┌──────────────────────────────┬────────────────────────────────────┐
│ 线程                         │ 职责                               │
├──────────────────────────────┼────────────────────────────────────┤
│ m_recycleUndoThread          │ Undo GC（recycleMinCsn驱动回收）   │
│ m_asyncRecoverUndoThread     │ 异步回滚（最多10个Worker，大事务用）│
│ m_checkpointThread           │ Checkpoint周期执行                 │
│ m_bgPageWriterMgr            │ BgDiskPageWriter脏页刷盘           │
│ m_standbyMonitorThread       │ 主备状态监控                       │
│ m_barrierCreatorThread       │ 创建主备同步 Barrier               │
│ m_collectMinBarrierThread    │ 收集最小 Barrier CSN               │
│ m_updateBarrierCsnThread     │ 更新 Barrier CSN（主备切换协调）   │
│ m_objSpaceMgrWorkerList[]    │ 对象空间管理 Worker（多个）        │
├──────────────────────────────┼────────────────────────────────────┤
│ StorageInstance（全局）      │                                    │
│ m_updateCsnMinThread         │ 定期更新全局 recycleMinCsn         │
└──────────────────────────────┴────────────────────────────────────┘

BgDiskPageWriter 层次：
  BgPageWriterMgr
    └─ BgDiskPageMasterWriter
         └─ BgDiskPageWriter（每个WAL流一个）
              ├─ 从 DirtyPageQueue 取脏页
              ├─ WAL-First 检查（WaitTargetPlsnPersist）
              ├─ 调用 WriteBlock() 写盘
              └─ 更新 GetMinRecoveryPlsn()（供 Checkpoint 使用）

WAL 背景刷盘：
  WalStreamBuffer → BgWalWriter
    ├─ 每次写完: 通知 PlsnWaitSlot（2048个槽）
    └─ slot leader 广播 followers（避免惊群）
```

---

## 九、逻辑复制模块（Logical Replication）

### 9.1 架构总览

```
WAL 流
  │
  ▼
WalDispatcher（WAL读取+分发线程）
  ├─ WalRecordReader → 按 PLSN 顺序读 WAL
  ├─ WalSortBuffer   → 按事务聚合、重排序
  │   ├─ TrxChangeCtx（一个事务的所有 RowChange）
  │   │   ├─ RowChange（INSERT/UPDATE/DELETE/CATALOG_*）
  │   │   └─ dependTxnCnt（DDL 事务依赖计数）
  │   └─ 事务提交时 → ChooseWorkerToDecode()（轮询）
  │
  └─ ParallelDecodeWorker × N（并行解码线程）
       ├─ m_trxChangesQueue（LogicalQueue，待解码）
       ├─ DecodeTrx():
       │   ├─ 无 DDL: DecodeTrxWithoutDDLInternal()
       │   └─ 有 DDL: DecodeTrxWithDDLInternal()
       │       → 从 DecodeDict 查表结构 → 拼装 TrxLogicalLog
       └─ m_trxLogicalLogQueue（LogicalQueue，已解码）

LogicalDecodeHandler（整体控制）
  ├─ GetNextTrxLogicalLog()  → 从 Worker 轮询取结果
  └─ ConfirmTrxLogicalLog()  → 推进 confirmedCsn + restartPlsn
```

### 9.2 关键数据结构

```cpp
// 复制槽（include/logical_replication/dstore_logical_replication_slot.h）
class LogicalReplicationSlot {
    WalId     m_walId;           // 从哪条 WAL 流解码
    WalPlsn   m_restartPlsn;     // WAL 截断锚点（不能截断早于此）
    CommitSeqNo m_catalogCsnMin;  // 阻止 Catalog 旧版本被回收
    CommitSeqNo m_confirmedCsn;   // 消费方已确认的最新 CSN
    CommitSeqNo m_decodeDictCsnMin;
    StartPointState m_state;     // DEFAULT→WAIT_ACTIVE_TRX_FINISH→CONSISTENT
};

// 单事务变更上下文（include/logical_replication/dstore_wal_sort_buffer.h）
struct TrxChangeCtx {
    Xid xid;
    WalPlsn firstPlsn, endPlsn, commitPlsn;
    CommitSeqNo commitCsn;
    dlist_head changes;          // RowChange 链表
    int ddlCounts;               // 含DDL数量
    int dependTxnCnt;            // 依赖其他事务（DDL依赖）
    dlist_head referTxns;        // 依赖本事务的其他事务
};

// 单行变更（insert/update/delete）
struct RowChange {
    RowChangeType type;          // INSERT/UPDATE/DELETE/CATALOG_*
    Oid tableOid;
    TupleBuf *oldTuple;          // DELETE/UPDATE 有效
    TupleBuf *newTuple;          // INSERT/UPDATE 有效
    CommitSeqNo snapshotCsn;
};
```

### 9.3 与核心模块的接触点

```
逻辑复制 ↔ WAL：
  WalDispatcher 调用 WalRecordReader 按 PLSN 读取 WAL
  m_restartPlsn 阻止 WAL 文件被 WalManager 删除

逻辑复制 ↔ Transaction（CSN）：
  TrxChangeCtx.commitCsn 从 WAL_TXN_COMMIT 记录中读取
  SlotAdvance 推进 confirmedCsn → 更新 recycleMinCsn 的槽贡献

逻辑复制 ↔ Undo/Catalog：
  DDL 操作（建表/改列）改变 Catalog 系统表
  ParallelDecodeWorker 通过 DecodeDict 查表结构
  m_catalogCsnMin 阻止旧版本 Catalog 的 Undo 被回收

MAX_LOGICAL_SLOT_NUM = 6（每PDB最多6个逻辑复制槽）
DEFAULT_LOGICAL_QUEUE_SIZE = 128 条 TrxLogicalLog
```

---

## 十、Flashback 模块

### 10.1 设计原理

```
Flashback Table = "时间机器"查询
  目标：以过去某个时间点（flashbackCsn）的数据状态查询当前表

实现方式：
  复用 Heap MVCC 机制：传入 flashbackCsn 作为快照
  遍历当前所有存活 Tuple + 所有 Undo 历史版本

两类 Tuple 结果：
  Delta Tuple（增量）: flashbackCsn 之后新增的行
                       → 这些行在 flashback 时刻不存在
  Lost Tuple（丢失）:  flashbackCsn 之前存在，现在已删除的行
                       → 这些行在 flashback 时刻存在但现在消失了
```

### 10.2 FlashbackTableHandler

```cpp
// include/flashback/dstore_flashback_table.h
class FlashbackTableHandler {
    CommitSeqNo m_flashbackCsn;  // 目标历史时间点
    HeapScanHandler *m_heapScanHandler;  // 复用 HeapScan

    HeapTuple *GetDeltaTuple();   // 获取 flashback 之后新增的行
    HeapTuple *GetLostTuple();    // 获取 flashback 之后被删除的行

private:
    bool IsTupleVisibleFlashbackCsn(HeapTuple *tuple);
    // 判断: tuple 在 flashbackCsn 时刻是否可见
    // 复用 XidVisibleToSnapshot(flashbackCsn, tuple.td.xid)
};
```

### 10.3 与核心模块的接触点

```
Flashback ↔ MVCC（Transaction + Undo）：
  flashbackCsn 作为 snapshotCsn 传入 XidVisibleToSnapshot()
  需要 Undo 保留到 flashbackCsn 时刻的历史版本
  → flashbackCsnMin 贡献到 recycleMinCsn，阻止过早回收

Flashback ↔ Heap：
  HeapScanHandler 全表扫描
  对每个 Tuple 调用 IsTupleVisibleFlashbackCsn()
  需要时调用 ConstructCrTuple() 重建历史版本

Flashback 时间精度：
  精度取决于 Undo 保留时长（由 flashbackCsnMin 控制）
  flashbackCsn 越旧，需要的 Undo 链越长
```

---

## 十一、Barrier 机制（主备同步协调）

### 11.1 Barrier 概念

```
Barrier = 主备库就某个 CSN 达成一致的"检查点协议"
  barrierCsn: 所有节点已确认同步到该 CSN 的最小值
  作用: 安全推进 recycleMinCsn（不回收任何备库可能还需要的版本）

WalBarrierCsn（include/framework/dstore_pdb.h:107）:
struct WalBarrierCsn : public WalRecord {
    CommitSeqNo barrierCsn;
    uint32 nodeCnt;
    uint64 term;
    uint32 pdbCount;
    StandbyPdbSyncMode syncModeArray[];  // 每PDB的同步模式
}
```

### 11.2 Barrier 生命周期

```
主库 BarrierCreatorThread:
  定期创建 Barrier → 写 WalBarrierCsn WAL
  → 等待备库确认（通过 CollectMinBarrierThread）

备库：
  Redo 到 WalBarrierCsn → 回复 ack（barrierCsn）
  → 主库收集所有备库的最小 barrierCsn → m_barrierCsnMin

UpdateBarrierCsnThread:
  定期更新 StoragePdb.pdbRecycleCsnMin
  = min(localCsnMin, m_barrierCsnMin, flashbackCsnMin)

Failover 时的 Barrier 处理：
  m_needRollbackBarrierInFailover = true
  新主库对 Barrier 之后的未提交事务执行 Rollback
  m_rollbackBarrierCsn：主备切换时的回滚边界
```

### 11.3 PdbSyncMode

```
enum class PdbSyncMode:
  SYNC_MODE_NONE    — 不同步（只有主库）
  SYNC_MODE_SYNC    — 同步复制（主库等备库确认）
  SYNC_MODE_ASYNC   — 异步复制（主库不等待）
  SYNC_MODE_QUORUM  — 多数派确认

影响: WAL 写入时的 WaitTargetPlsnPersist 是否等待备库
```

---

## 十二、模块间契约大全（Days 1-9 全收录）

```
契约 1：WAL-First（Buffer ↔ WAL）
  规则: 页面写盘前，对应 WAL 必须已落盘
  实现: BufferMgr::WriteBlock() → WaitTargetPlsnPersist(page.plsn)
  保证: 崩溃时 WAL 一定比数据页完整，Redo 能从 WAL 重建页面

契约 2：recycleMinCsn（Transaction ↔ Undo ↔ Heap ↔ LogicalRepl ↔ Flashback）
  规则: 低于 recycleMinCsn 的历史版本才能被安全回收
  实现: recycleMinCsn = min(所有活跃快照 + 备库 + 逻辑复制槽 + Flashback)
  保证: 任何仍在使用的快照（本地/备库/逻辑复制/Flashback）都不会丢失所需历史版本

契约 3：IS_PREV_XID_CSN（Heap ↔ MVCC 可见性）
  规则: TD 被复用时，旧事务的 CSN 必须保留（仅改 csnStatus 标记，不改 csn 值）
  实现: SetTd() → IS_CUR_XID_CSN → IS_PREV_XID_CSN
  保证: TD 复用不破坏并发快照的可见性判断

契约 4：crTd 副本（Heap ↔ Undo 回溯）
  规则: ConstructCrTuple 使用只读 crTd[] 副本回溯，不修改原页面 TD
  实现: crTd[] = copy(page->td[])，通过 RollbackTdInfo 回退
  保证: 历史版本读取与当前写入不互相干扰

契约 5：SetSnapshotCsn 三步原子模拟（Transaction ↔ CsnMgr）
  规则: 建立快照时防止 GC 在中途清理版本
  实现: MAX_CSN占位 → 读 committedCsn → 原子替换
  保证: 快照建立的整个过程中不会错误清理快照可见版本

契约 6：PENDING_COMMIT 等待（Transaction ↔ Lock ↔ MVCC）
  规则: 遇到 PENDING_COMMIT 事务必须等待其完成
  实现: XactLockMgr::Wait(pdbId, xid) → LOCKTAG_TRANSACTION
  保证: 其他事务不会在 PENDING_COMMIT 阶段读到"半提交"状态

契约 7：SMO 自愈（BTree ↔ 并发访问）
  规则: BTree 分裂期间设置 SPLIT_INCOMPLETE 标记，后续访问遇到则补完
  实现: Split 前设 SPLIT_INCOMPLETE → 任何访问到该页的事务调 CompleteSplit()
  保证: SMO 不阻塞其他并发事务，且最终一定完成

契约 8：WAL-First for Undo（Undo ↔ WAL）
  规则: Undo 页面修改同样遵循 WAL-First
  实现: UndoZone::InsertUndoRecord() → WalRecordUndo → MarkDirty
  保证: 崩溃恢复时 Undo 链完整可 Redo

契约 9：逻辑复制槽 restartPlsn（LogicalRepl ↔ WAL）
  规则: WAL 文件不能删除到 restartPlsn 之前
  实现: LogicalReplicaMgr::FlushDependentMinPlsn() 更新全局最小 PLSN
  保证: 逻辑解码可以从 restartPlsn 重新读取 WAL（故障重连时）

契约 10：Barrier recycleMinCsn（主备 ↔ Undo/TD）
  规则: 备库确认的 barrierCsn 之前的历史版本才能被回收
  实现: recycleMinCsn = min(..., barrierCsnMin)
  保证: 备库 Redo 需要的历史版本不被主库提前回收
```

---

## 十三、完整启动时序

```
StorageInstance::Bootstrap(guc)
  ├─ BufMgrInit()             ← 分配缓冲池（全局共享）
  ├─ InitAllLockMgrs()        ← LockMgr + TableLockMgr + XactLockMgr
  ├─ InitCsnMgr()             ← 全局 CSN 管理器
  └─ InitPdbSlots()           ← 初始化 PDB 槽位数组

StorageInstance::StartupInstance(guc)
  ├─ 对每个 PDB: StoragePdb::OpenPdb()
  │    ├─ MountExistingVFS()  ← 打开 VFS（本地文件/PageStore）
  │    ├─ InitControlFile()   ← 读取 ControlFile（diskRecoveryPlsn等）
  │    ├─ InitWalMgr()        ← WAL 流管理器
  │    ├─ InitTransactionMgr()← XID分配器、快照管理器
  │    ├─ InitUndoMgr()       ← Undo Zone 映射
  │    ├─ InitTableSpaceMgr() ← 表空间、Segment、FSM
  │    ├─ InitLogicalReplicaMgr() ← 从磁盘恢复逻辑复制槽
  │    ├─ InitRecoveryThreadAndWaitDone()  ← WAL Redo 崩溃恢复
  │    └─ StartBgThread()     ← 启动所有后台线程
  └─ m_instanceState = ACTIVE ← 开放连接
```

---

## 十四、快速参考：关键常量汇总

```
模块           常量名                           值        含义
─────────────────────────────────────────────────────────────────
Buffer         BUFFER_POOL_PARTITIONS           4096      BufTable分区数
Buffer         MAX_SIMUL_LWLOCKS                4224      同时持有的最大LWLock数
Transaction    XID_ZONE_BITS                    20        XID中zoneId位宽
Transaction    MAX_CSN                          ~2^63     特殊标记（快照建立占位）
Undo           UNDO_ZONE_COUNT                  1024*1024  最大Zone数
Undo           TRX_PAGES_PER_ZONE               127       每Zone事务槽页数（环形）
WAL            WAL_STREAM_COUNT                 1024      最大WAL流数
WAL            PLSN_WAIT_SLOTS                  2048      通知槽数（避免惊群）
Heap           MAX_TD_COUNT                     128       每页最大TD槽数
Heap           HEAP_BIGTUPLE_THRESHOLD          ~7892B    BigTuple阈值
Index          BtrFillFactor(leaf)              90%       叶页填充因子
Index          BtrFillFactor(internal)          70%       非叶页填充因子
Lock           FAST_LOCK_ENTRY_MAP_MAX_SLOT     512       本地弱锁缓存槽数
Lock           DEADLOCK_DETECT_INTERVAL_US      3,000,000 死锁检测周期(3s)
Lock           DEADLOCK_DETECT_MIN_WAIT_US      2,000,000 纳入检测的最小等待(2s)
Tablespace     TBS_SYSTEM_COUNT                 8         系统表空间数
Tablespace     INIT_FILE_PAGE_COUNT             8192      文件初始大小(64MB)
Segment        EXT_NUM_LINE[4]                  0/16/144/272  Extent升级边界
LogicalRepl    MAX_LOGICAL_SLOT_NUM             6         每PDB最大逻辑复制槽数
LogicalRepl    DEFAULT_LOGICAL_QUEUE_SIZE       128       解码队列默认容量
LogicalRepl    STREAM_DECODE_ADVANCE_INTERVAL   3s        槽推进时间间隔
```

---

## 十五、一张图看懂 dstore

```
╔══════════════════════════════════════════════════════════════════════════╗
║  SQL → TransactionMgr                                                   ║
║           │  ① 分配XID + 建立快照CSN                                   ║
║           │                                                              ║
║           ▼                                                              ║
║  TableLockMgr ──────────── XactLockMgr                                  ║
║  ② 表锁（快速路径）        ③ PENDING_COMMIT等待                        ║
║           │                        ↑                                    ║
║           ▼                        │ 等待通知                           ║
║  BufferMgr（缓冲池门户）           │                                    ║
║  ④ 读页面（BufTable命中/LRU驱逐） │                                    ║
║           │                        │                                    ║
║       ┌───┴────────────┐           │                                    ║
║       ▼                ▼           │                                    ║
║  HeapPage           BtrPage        │                                    ║
║  ⑤ AllocTd          AllocTd        │                                    ║
║  ⑦ AddTuple         AddTuple       │                                    ║
║       │                │           │                                    ║
║       ▼                ▼           │                                    ║
║  UndoMgr                           │                                    ║
║  ⑥ InsertUndoRecord                │                                    ║
║  （版本链入口）                    │                                    ║
║       │                            │                                    ║
║       ▼                            │                                    ║
║  WAL（原子组）                     │                                    ║
║  ⑧ EndAtomicWal → WaitPersist ─────┘                                    ║
║       │                                                                  ║
║       ▼                                                                  ║
║  MarkDirty → DirtyPageQueue                                              ║
║  ⑨ 脏页进入队列                                                         ║
║       │                                                                  ║
║  ┌────┴──────────────────────────────────────────────┐                  ║
║  │              后台线程异步处理                      │                  ║
║  │  BgWalWriter: WAL落盘                             │                  ║
║  │  BgDiskPageWriter: 脏页写盘（WAL-First后）        │                  ║
║  │  CheckpointMgr: 更新diskRecoveryPlsn              │                  ║
║  │  RecycleUndoThread: 回收过期Undo (recycleMinCsn)  │                  ║
║  │  LogicalReplicaMgr: WAL解码→逻辑日志              │                  ║
║  └───────────────────────────────────────────────────┘                  ║
║                                                                          ║
║  崩溃时: ControlFile.diskRecoveryPlsn → WAL Redo → Undo Rollback       ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## 十六、各模块学习路径回顾

| Day | 模块 | 核心问题 | 关键文件 |
|-----|------|---------|---------|
| 1 | 全局框架 | 两层架构如何分工？ | dstore_instance.h, dstore_pdb.h |
| 2 | Page/Tuple | 数据在磁盘怎么排布？ | dstore_page_struct.h, dstore_heap_page.h |
| 3 | Buffer | 页面如何在内存中管理？ | dstore_buf_mgr.h, dstore_buf_desc.h |
| 4 | Transaction | MVCC 可见性如何判断？ | dstore_transaction_mgr.h, dstore_csn_mgr.h |
| 5 | WAL | 如何保证崩溃安全？ | dstore_wal.h, dstore_wal_stream.h |
| 6 | Undo | 历史版本如何存储和读取？ | dstore_undo_zone.h, dstore_undo_record.h |
| 7 | Heap | 写入/读取/BigTuple/FSM？ | dstore_heap_handler.h, dstore_partition_fsm.h |
| 8 | Index | BTree 如何并发安全？ | dstore_btree_insert.h, dstore_btr_page.h |
| 9 | Lock/TBS | 锁如何避免死锁？存储如何扩展？ | dstore_lock_mgr.h, dstore_tablespace.h |
| 10 | 综合 | 模块如何协作？扩展点在哪？ | dstore_instance.h, dstore_logical_replication_mgr.h |

---

*Day 10 完成。dstore 存储引擎 10 天学习全部结束。*
