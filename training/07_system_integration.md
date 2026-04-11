# DStore 存储引擎系统集成：各模块如何协作

## 总览：模块依赖关系

```
┌─────────────────────────────────────────────────────────────┐
│                      SQL 执行层                              │
│              (INSERT / UPDATE / DELETE / SELECT)             │
└───────────────────────┬─────────────────────────────────────┘
                        │
          ┌─────────────▼──────────────┐
          │     Transaction Manager     │  ← 事务生命周期管理
          │   (XID 分配、快照、提交)    │
          └──┬──────────┬──────────────┘
             │          │
    ┌─────────▼──┐  ┌───▼──────────┐
    │  Heap Page │  │  Index/BTree │  ← 数据与索引
    │  (TD机制)  │  │  (TD机制)    │
    └──┬─────────┘  └──┬───────────┘
       │               │
    ┌──▼───────────────▼──┐
    │    Buffer Manager    │  ← 缓冲池（所有页面读写必经之路）
    └──┬──────────────────┘
       │           │
  ┌────▼───┐  ┌────▼────┐
  │  Undo  │  │   WAL   │  ← 持久化保障（WAL优先于页面落盘）
  └────────┘  └─────────┘
```

**核心原则**：
- **事务**是控制流的起点，贯穿一切
- **Buffer**是数据的门户，所有页面读写都经过它
- **WAL** 是持久化的底线，数据页落盘前WAL必须先落盘
- **Undo** 是时间机器，支持回滚和历史版本读取
- **Heap/Index** 是实际数据，通过 TD 机制与事务绑定

---

## 一、完整写入路径（以 INSERT 为例）

```
用户: INSERT INTO t VALUES (1, 'hello')
```

### 第一步：事务初始化

```
TransactionMgr::StartInternal()
  ├─ 分配 XID（20bit zoneId + 44bit logicSlotId）
  ├─ 创建 TransactionSlot（status=IN_PROGRESS）
  └─ 分配快照（READ_COMMITTED: 每条SQL一个；SERIALIZABLE: 事务开始时一个）
```

### 第二步：获取页面（经过 Buffer）

```
BufferMgr::Read(tablePageId, LW_EXCLUSIVE)
  ├─ 哈希表查找（4096分区）
  │   ├─ 命中 → Pin页面（thread-local refcount++）
  │   └─ 未命中 → LRU选取牺牲页 → 从磁盘读入
  └─ 返回加锁的 BufferDesc*
```

### 第三步：在 Heap 页分配 TD

```
HeapPage::AllocTd(context)
  └─ DoAllocTd()
      ├─ 当前事务已有TD → 复用（重入）
      ├─ 有空闲UNOCCUPY槽 → 直接占用
      ├─ TryReuseTdSlots() → 回收已提交事务的TD
      │   ├─ TryReuseOneTdSlot(): 查 TXN_STATUS 分类
      │   │   ├─ CSN < recycleMinCsn → TD_RECYCLE_UNUSED（可完全Reset）
      │   │   └─ CSN ≥ recycleMinCsn → TD_RECYCLE_REUSE（保留CSN）
      │   ├─ RefreshTupleTdStatus(): 批量更新受影响Tuple的m_tdStatus
      │   │   ├─ slot.unused=true  → DETACH_TD
      │   │   └─ slot.unused=false → ATTACH_TD_AS_HISTORY_OWNER
      │   └─ GetAvailableTd(): 选最优槽位返回
      └─ ExtendTd() → memmove ItemId数组，插入新TD（最多128个）
```

### 第四步：写 Undo（版本链入口）

```
构建 UndoRecord:
  ├─ m_undoType = UNDO_HEAP_INSERT
  ├─ m_ctid = 目标行的 ItemPointer
  ├─ m_cid  = 当前命令ID
  └─ m_tdPreInfo = {旧TD的 xid, csn, undoRecPtr}  ← 跨事务链入口

TransactionMgr::InsertUndoRecord()
  ├─ SetTxnPreUndoPtr(curTailPtr)    ← 链入同事务Undo链
  └─ UndoZone::InsertUndoRecord()
      ├─ 找写入位置（m_nextAppendUndoPtr）
      ├─ Pin Undo页（Buffer管理）
      ├─ Serialize写入（Varint压缩）
      ├─ GenerateWalForUndoRec()      ← Undo写入本身也有WAL！
      └─ 返回 UndoRecPtr
```

### 第五步：设置 TD，绑定事务

```
HeapPage::SetTd(tdId, xid, undoRecPtr, cid)
  ├─ 若TD已有旧XID：IS_CUR_XID_CSN → IS_PREV_XID_CSN  ← 保留旧CSN供并发快照使用
  ├─ td->SetXid(curXid)
  ├─ td->SetUndoRecPtr(undoRecPtr)
  ├─ td->SetStatus(OCCUPY_TRX_IN_PROGRESS)
  └─ td->SetCsnStatus(IS_INVALID)    ← 提交时才变为IS_CUR_XID_CSN
```

### 第六步：写入 Tuple 数据

```
HeapPage::AddTuple(tuple)
  ├─ 在页面 Free Space 分配空间（m_upper向下）
  ├─ 写入 HeapDiskTuple:
  │   ├─ m_tdId = tdId
  │   ├─ m_tdStatus = ATTACH_TD_AS_NEW_OWNER  ← 新写入，直接关联TD
  │   └─ m_data = 实际列值
  └─ 分配 ItemId（m_lower向上）指向该Tuple
```

### 第七步：更新索引

```
（若表有索引）
BTree::Insert(key, heapCtid)
  ├─ SearchBtreeForInsert() → 找叶子插入位置
  ├─ CheckUnique()（唯一索引）
  ├─ AllocTd() on 索引页（与Heap相同机制）
  ├─ 写 Undo（WAL_BTREE_INSERT_ON_LEAF 内含 undoRecPtr）
  └─ BtrPage::AddTuple(indexTuple)
      └─ indexTuple.m_tdId = 索引页TD槽
```

### 第八步：生成 WAL（原子组）

```
AtomicWalWriterContext:
  BeginAtomicWal(xid)
    └─ 初始化 WalRecordAtomicGroup {groupLen, crc, xid, recordNum}

  RememberPageNeedWal(heapBufDesc)   ← 记录修改的页面
  RememberPageNeedWal(indexBufDesc)  ← 记录索引页

  PutNewWalRecord(WAL_HEAP_INSERT)
  Append(tupleData, size)
  PutNewWalRecord(WAL_BTREE_INSERT_ON_LEAF)
  Append(indexTupleData, size)

  EndAtomicWal()
    ├─ WalStream::Append(buf) → 写入 WalStreamBuffer（内存）
    ├─ SetPagesLSN(heapPage, walId, plsn, glsn)
    ├─ SetPagesLSN(indexPage, walId, plsn, glsn)
    └─ 返回 WalGroupLsnInfo{walId, startPlsn, endPlsn}

  WaitTargetPlsnPersist(lsnInfo)
    └─ 等待 BgWalWriter 将该WAL组刷入磁盘
```

### 第九步：标记脏页

```
BufferMgr::MarkDirty(heapBufDesc)
  ├─ state |= BUF_CONTENT_DIRTY
  └─ 加入 DirtyPageQueue（MPSC队列）

（同样对索引页 MarkDirty）
```

### 第十步：提交事务

```
TransactionMgr::CommitInternal()
  ├─ 1. 生成 WAL_TXN_COMMIT 记录
  ├─ 2. 分配 CSN（全局原子递增）
  ├─ 3. 更新 TransactionSlot: status=PENDING_COMMIT, csn=newCsn
  ├─ 4. WaitTargetPlsnPersist() ← 等待COMMIT WAL落盘
  ├─ 5. status → COMMITTED      ← 对外可见
  └─ 6. td->SetCsn(csn), SetCsnStatus(IS_CUR_XID_CSN)  ← 页面TD更新
```

**写入时序总结**：
```
分配XID → 读页面 → 分配TD → 写Undo → 设置TD → 写Tuple → 写索引 → 生成WAL → MarkDirty → 提交
                                                                           ↑
                                                              WAL必须在MarkDirty前完成（WAL-First）
```

---

## 二、完整读取路径（以 SELECT 为例）

```
用户: SELECT * FROM t WHERE id = 1
```

### 第一步：获取快照

```
TransactionMgr::GetSnapshot()
  └─ snapshotCsn = 当前已提交的最大CSN
     （READ_COMMITTED: 每条SQL重新取；SNAPSHOT: 事务开始时固定）
```

### 第二步：索引查找（走 BTree）

```
BTree::Search(key=1)
  ├─ GetRoot() → 从元数据缓存获取根页
  ├─ 内部节点：BinarySearchOnPage() + 跟随 downlink 下降
  └─ 叶子节点：找到 IndexTuple
      ├─ m_isDeleted=1 → 跳过
      └─ MVCC检查：
          ├─ GetItupInsertXidFromUndo(tdId) → insertXid
          ├─ GetItupDeleteXidFromUndo(tdId) → deleteXid（若有）
          └─ insertXid对快照可见 AND deleteXid对快照不可见 → 返回 heapCtid
```

### 第三步：读取 Heap 页

```
BufferMgr::Read(heapPageId, LW_SHARED)
  ├─ 哈希表命中 → Pin（refcount++）
  └─ 未命中 → 从磁盘读入（WAL-First: 若页面LSN > flushedWalLsn，等WAL先落盘）
```

### 第四步：可见性判断（GetVisibleTuple）

```
HeapPage::GetVisibleTuple(heapCtid, snapshot)

  TupleTdStatus = tuple->m_tdStatus
  TD *td = GetTd(tuple->m_tdId)

  switch (TupleTdStatus):

    case DETACH_TD:
      needUndo = false  ← TD已清空，Tuple独立，直接可见

    case ATTACH_TD_AS_HISTORY_OWNER:
      // TD已被新事务复用，当前Tuple是历史版本
      if snapshot == DIRTY:
        needUndo = false
      elif td->csnStatus == IS_INVALID:
        needUndo = true   ← 无CSN信息，必须查undo
      elif XidVisibleToSnapshot(snapshot, td->csn):
        needUndo = false  ← IS_PREV_XID_CSN对快照可见，当前页面版本有效
      else:
        needUndo = true   ← IS_PREV_XID_CSN不可见，需更旧版本

    case ATTACH_TD_AS_NEW_OWNER:
      xid = td->GetXid()
      if txn->IsCurrent(xid):
        needUndo = !CidVisibleToSnapshot(txn, snapshot, td->commandId)
      else:
        needUndo = !XidVisibleToSnapshot(snapshot, xid, txn)
        // XidVisibleToSnapshot:
        //   查TransactionSlot → xidStatus
        //   COMMITTED: csn <= snapshotCsn → 可见
        //   IN_PROGRESS: 不可见
        //   PENDING_COMMIT: 重查（乐观检查）

  if needUndo:
    ConstructCrTuple(...)  ← 回溯Undo链找历史版本
```

### 第五步：Undo 链回溯（需要时）

```
HeapPage::ConstructCrTuple(ctid, tdId, snapshot)

  // 复制TD数组（不修改原页面）
  crTd[] = copy(page->td[])

  loop:
    xid = crTd[tdId].xid
    undoPtr = crTd[tdId].undoRecPtr

    if undoPtr == INVALID:
      return 初始版本（INSERT前，不存在）

    // 跨事务Undo链跳转
    FetchUndoRecordByMatchedCtid(xid, undoPtr, ctid)
      └─ while(不匹配ctid): 沿TdPreUndoPtr跳转到前一事务

    // 回退crTd到前版本
    crTd[tdId].RollbackTdInfo(&record)
      // crTd[tdId] = {record.tdPreXid, record.tdPreCsn, record.tdPreUndoPtr}

    // 判断该版本可见性
    visible = XidVisibleToSnapshot(snapshot, csn_or_xid)

    if visible:
      ConstructTupleFromUndo(&record, resTuple)  ← 重建该版本数据
      break

    // 继续往更早回溯
    ConstructCrTupleFromUndo(&record, resTuple)
    continue loop
```

**读取时序总结**：
```
获取快照 → 索引查找(heapCtid) → 读Heap页 → GetVisibleTuple
                                              ├─ 直接返回（DETACH或CSN可见）
                                              └─ ConstructCrTuple → Undo链回溯 → 重建历史版本
```

---

## 三、事务回滚路径

```
TransactionMgr::AbortInternal()
  ├─ 写 WAL_TXN_ABORT
  └─ 回放 Undo 链（逆序）:

  TransactionSlot.tailUndoPtr
    → [UndoRecord3] m_txnPreUndoPtr
        → [UndoRecord2] m_txnPreUndoPtr
            → [UndoRecord1] m_txnPreUndoPtr = NULL

  对每条 UndoRecord:
    UNDO_HEAP_INSERT     → 删除该Tuple（设置TUPLE_BY_INSERT_DELETE）
    UNDO_HEAP_DELETE     → 恢复Tuple（清除删除标记）
    UNDO_HEAP_INPLACE_UPDATE → 恢复旧数据
    UNDO_HEAP_ANOTHER_PAGE_UPDATE → 跨页恢复

  最后：TransactionSlot.status = ABORTED
```

---

## 四、崩溃恢复路径

### WAL 重放（Redo）

```
系统重启
  └─ WalManager::Recovery(walId)
      ├─ BuildDirtyPageSet()
      │   └─ 扫描WAL文件，找出所有被修改过的页面
      │       按GLSN排序（跨流顺序保证）
      │
      └─ StartParallelRedo()
          ├─ Dispatch Thread 分发WAL记录
          └─ N个 Redo Worker 并行重放
              ├─ 同一页面 → 同一Worker（保序）
              └─ 不同页面 → 不同Worker（并行）
```

### Undo 回滚（Abort 未提交事务）

```
Redo完成后：扫描所有 TransactionSlot
  → status=IN_PROGRESS 的事务 → 执行 AbortInternal()
  → 沿 Undo 链逆序回放，撤销未提交修改
```

### 多流 Failover（主备切换）

```
旧Primary崩溃
  → 新Primary: TakeOverStreams([旧StreamId])
      ├─ 旧流: WRITE_WAL → ONLY_READ
      ├─ 对旧流执行 Recovery（Redo + Undo）
      ├─ 旧流恢复完成 → 删除
      └─ CreateWritingWalStreamWhenPromoting() ← 新写入流
```

---

## 五、模块间的关键"契约"

### 契约1：WAL-First（Buffer ↔ WAL）

```
BufferMgr::WriteBlock(bufDesc)
  └─ PrepareCheckPageBeforeStartIo()
      └─ if page.walId != bufDesc.walId OR page.plsn > flushedPlsn:
              WaitTargetPlsnPersist(page.walId, page.plsn)  ← 等WAL落盘
```

**保证**：磁盘上的数据页，其对应WAL一定已落盘。崩溃恢复时WAL一定比数据完整。

### 契约2：recycleMinCsn（Transaction ↔ Undo ↔ Heap）

```
TransactionMgr 维护:
  recycleMinCsn = min(所有活跃事务的snapshotCsn)

Undo使用:  CSN < recycleMinCsn → Undo页可回收
Heap使用:  CSN < recycleMinCsn → TD可完全Reset（canResetTd）
```

**保证**：不会回收任何活跃快照还可能需要的历史版本。

### 契约3：IS_PREV_XID_CSN（Heap TD ↔ MVCC 可见性）

```
TD被新事务复用时（SetTd）:
  旧事务 IS_CUR_XID_CSN → IS_PREV_XID_CSN（CSN值保留）

GetVisibleTuple读取时:
  ATTACH_TD_AS_HISTORY_OWNER + IS_PREV_XID_CSN
  → 使用保留的CSN判断可见性
  → 避免查Undo（快速路径）
```

**保证**：TD复用不破坏仍然活跃的快照的可见性判断。

### 契约4：crTd副本（Heap ↔ Undo 回溯）

```
ConstructCrTuple 复制TD数组（只读副本）
  → 通过 RollbackTdInfo 逐步回退 crTd 状态
  → 不修改原页面TD

原页面TD始终保存最新版本信息
历史版本通过 Undo 链动态重建
```

**保证**：历史版本读取与当前版本写入不互相干扰。

---

## 六、一张图看全局

```
┌──────────────────────────────────────────────────────────────┐
│                    一条 INSERT 的旅程                         │
│                                                              │
│  SQL ──→ TransactionMgr ──→ XID + Snapshot                  │
│              │                                               │
│              ▼                                               │
│         BufferMgr::Read(heapPage)   ← 缓冲池门户            │
│              │                                               │
│         HeapPage::AllocTd()         ← TD机制                │
│              │                                               │
│         UndoZone::InsertUndoRecord() ← 版本链入口           │
│              │    └─ WAL(Undo)                               │
│         HeapPage::SetTd()           ← 绑定事务              │
│              │                                               │
│         HeapPage::AddTuple()        ← 写数据                │
│              │                                               │
│         BTree::Insert()             ← 写索引                │
│              │    └─ BtrPage::AllocTd/AddTuple              │
│              │                                               │
│         EndAtomicWal()              ← WAL原子组             │
│              │    └─ SetPagesLSN(heap, index)               │
│              │                                               │
│         MarkDirty(heap, index)      ← 进入DirtyPageQueue    │
│              │                                               │
│         CommitInternal()            ← 提交                  │
│              │    ├─ WAL_TXN_COMMIT                         │
│              │    ├─ 分配CSN                                │
│              │    └─ TransactionSlot.status=COMMITTED       │
│              │                                               │
│    ┌─────────┴──────────┐                                   │
│    │  BgWalWriter       │  BgWriter/Checkpoint              │
│    │  WAL落盘           │  脏页落盘（WAL-First保证）         │
│    └────────────────────┘                                   │
└──────────────────────────────────────────────────────────────┘
```

---

## 七、各模块职责一句话总结

| 模块 | 核心职责 | 对外提供 |
|------|---------|---------|
| **Transaction** | 事务生命周期、MVCC可见性判断 | XID、CSN、Snapshot、XidVisibleToSnapshot |
| **Buffer** | 所有页面的读写门户、缓冲管理 | 加锁的内存页指针、WAL-First检查 |
| **Undo** | 历史版本存储、回滚支撑 | UndoRecPtr、FetchUndoRecord、版本链 |
| **Heap** | 行数据存储、TD机制、可见性 | GetVisibleTuple、ConstructCrTuple |
| **Index/BTree** | 键到行的快速定位 | heapCtid、范围扫描 |
| **WAL** | 崩溃恢复保障、主备同步 | WalGroupLsnInfo、Redo恢复 |

**核心闭环**：Transaction 控流 → Buffer 管页 → Heap/Index 存数 → Undo 留历史 → WAL 保安全 → 崩溃后 WAL Redo + Undo Rollback 恢复一致状态。
