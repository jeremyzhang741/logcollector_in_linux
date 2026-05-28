# Day 6 — Undo 模块

## 目录

1. [Undo 的双重使命](#1-undo-的双重使命)
2. [物理地址：UndoRecPtr](#2-物理地址undorecptr)
3. [四层物理结构](#3-四层物理结构)
4. [UndoRecord 格式详解](#4-undorecord-格式详解)
5. [两条链：事务链 vs 跨事务链](#5-两条链事务链-vs-跨事务链)
6. [TransactionSlot：事务在 Undo 中的锚点](#6-transactionslot事务在-undo-中的锚点)
7. [UndoZone：写入流程](#7-undozone写入流程)
8. [回滚流程：RollbackUndoZone](#8-回滚流程rollbackundozone)
9. [MVCC 旧版本重建：CR 页构造](#9-mvcc-旧版本重建cr-页构造)
10. [Undo GC：回收机制](#10-undo-gc回收机制)
11. [Undo WAL 记录体系](#11-undo-wal-记录体系)
12. [异步回滚：RollbackTrxTaskMgr](#12-异步回滚rollbacktrxtaskmgr)
13. [完整交互时序图](#13-完整交互时序图)
14. [与 Day3/4/5 的连接点](#14-与-day345-的连接点)
15. [快速查阅表](#15-快速查阅表)

---

## 1. Undo 的双重使命

```
使命1：事务回滚（Rollback）
  事务失败或用户执行 ROLLBACK
  → 按逆序回放 Undo 记录
  → 恢复数据页到修改前状态

使命2：MVCC 旧版本重建
  读操作的快照不可见当前版本
  → 沿 Undo 链向前遍历
  → 重建满足快照 CSN 的历史版本
```

**与 PostgreSQL/InnoDB 的区别**：

| 特性 | dstore | PostgreSQL | InnoDB |
|------|--------|------------|--------|
| Undo 存储位置 | 独立 Undo Segment（每个 Zone） | 堆内旧版本 | 独立 Undo Log |
| 版本链方向 | TD → Undo → 前版本 | ctid forward chain | 逆向链 |
| XID 与 Zone 关系 | XID.zoneId = ZoneId（物理地址） | 无直接关系 | 无直接关系 |

---

## 2. 物理地址：UndoRecPtr

```cpp
// include/undo/dstore_undo_types.h:66
using UndoRecPtr = ItemPointerData;
// 复用 ItemPointerData 的 64bit 编码：
//   FileId(16bit) + BlockNumber(32bit) + Offset(16bit)
```

```
UndoRecPtr 布局（64bit）：

 63          48 47          16 15        0
 ┌────────────┬──────────────┬───────────┐
 │ FileId(16) │ BlockNum(32) │ Offset(16)│
 └────────────┴──────────────┴───────────┘
```

特殊值：
```cpp
const UndoRecPtr INVALID_UNDO_RECORD_PTR = INVALID_ITEM_POINTER;
constexpr ZoneId UNDO_ZONE_COUNT = 1024 * 1024;  // 最多 1M 个 Zone
constexpr ZoneId INVALID_ZONE_ID = -1;
```

**关键设计**：XID 中的 `zoneId` 字段直接就是这个 Zone 的索引，XID 同时是 Undo Zone 的物理地址（Day 4 的核心概念）。

---

## 3. 四层物理结构

```
UndoMgr（每PDB一个）
  │
  ├─ m_undoZones[]（最多 1M 个 UndoZone，按 ZoneId 索引）
  │   │
  │   └─ UndoZone（每个活跃事务线程分配一个 ZoneId）
  │       │
  │       ├─ Segment（底层存储段）
  │       │   ├─ Meta Page（段元数据页）
  │       │   ├─ Transaction Slot Pages（事务槽页面环）
  │       │   └─ Undo Record Pages（Undo 记录页面环）
  │       │
  │       ├─ UndoZoneTrxManager（事务槽管理）
  │       │   ├─ m_nextFreeLogicSlotId（原子，下一个空闲槽）
  │       │   └─ m_recycleLogicSlotId（原子，下一个回收槽）
  │       │
  │       ├─ m_nextAppendUndoPtr（下一条 Undo 写入位置）
  │       ├─ m_undoRecyclePageId（下一个待回收页）
  │       └─ m_needCheckPageId（写入前检查扩展的页）
  │
  └─ m_txnInfoCache（AllUndoZoneTxnInfoCache，事务状态缓存）
```

### 3.1 Transaction Slot Pages 布局

```
每个 Zone 的 TxnSlot 区：TRX_PAGES_PER_ZONE 个页面（环形）

TRX_PAGES_PER_ZONE = UNDO_ZONE_EXTENT_SIZE - 1 = 127（生产环境）

每页 TransactionSlot 数（TRX_PAGE_SLOTS_NUM）：
  = (BLCKSZ - PageHeader) / sizeof(TransactionSlot)

logicSlotId → 物理页：
  pageId = startPage + (logicSlotId / TRX_PAGE_SLOTS_NUM) % TRX_PAGES_PER_ZONE
  slotId  = logicSlotId % TRX_PAGE_SLOTS_NUM
```

### 3.2 Undo Record Pages 布局

```
Undo 记录页面：循环链表（Ring）

Page0 ←prev→ PageN
  │               ↑
  └─next→ Page1 → ... → PageN

UndoRecordPageHeader：
  { uint32 usedBytes; PageId self; PageId prev; PageId next; }

写满当前页 → 移向 next 页（m_nextAppendUndoPtr 推进）
next 页无空间 → AllocNewUndoPages() → ExtendUndoPageRing()
```

---

## 4. UndoRecord 格式详解

### 4.1 完整结构层次

```
UndoRecord（内存对象）
  ├─ UndoRecordHeader m_header        ← 序列化到磁盘的头部
  │   ├─ UndoType m_undoType          ← 操作类型
  │   ├─ CommandId m_cid              ← 事务内命令序号
  │   ├─ UndoTdInfo m_tdPreInfo       ← 前一版本的 TD 快照
  │   │   ├─ uint64 m_undoRecPtr      ← 前版本 UndoRecPtr
  │   │   ├─ uint64 m_xid             ← 前版本 XID
  │   │   ├─ CommitSeqNo m_csn        ← 前版本 CSN
  │   │   ├─ uint8 m_tdId             ← 前版本 TD 槽位
  │   │   └─ TdCsnStatus m_csnStatus  ← 前版本 CSN 状态
  │   ├─ uint64 m_txnPreUndoPtr       ← 同事务前一条 Undo 地址
  │   ├─ uint64 m_ctid                ← 修改的行 ItemPointer
  │   └─ uint64 m_fileVersion         ← 文件版本（检测文件重建）
  │
  ├─ char *m_serializeData            ← 序列化后的磁盘数据
  ├─ uint8 m_serializeSize            ← 序列化后大小
  ├─ StringInfoData m_dataInfo        ← 变长 Undo 数据（旧行数据）
  └─ BufferDesc *m_currentFetchUndoPageBuf  ← 读取时的 buffer 缓存
```

### 4.2 UndoType 枚举

```cpp
// include/undo/dstore_undo_types.h:35
enum UndoType : uint8 {
    // Heap 操作
    UNDO_HEAP_INSERT = 0,           // INSERT（无旧数据）
    UNDO_HEAP_BATCH_INSERT,         // 批量插入
    UNDO_HEAP_DELETE,               // DELETE（含旧行数据）
    UNDO_HEAP_INPLACE_UPDATE,       // 原地 UPDATE
    UNDO_HEAP_SAME_PAGE_APPEND_UPDATE,          // 同页扩展 UPDATE
    UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE, // 跨页 UPDATE 旧页
    UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE, // 跨页 UPDATE 新页（无数据）
    UNDO_HEAP_BOUND,
    // BTree 操作
    UNDO_BTREE_INSERT,
    UNDO_BTREE_DELETE,
    UNDO_BTREE_BOUND,
    // 临时表变体（_TMP 后缀，相同语义）
    UNDO_HEAP_INSERT_TMP, ...
};
```

**INSERT/新页无旧数据**：`IsUndoDataValid()` 对 INSERT 类型允许 dataSize==0，因为回滚 INSERT 只需删除行，不需要存旧数据。

### 4.3 磁盘序列化（Varint 压缩）

```cpp
// UndoRecord::Serialize() 压缩顺序：
[serializeSize(1B)] [undoType(1B)] [cid(varint32)]
[tdPreUndoRecPtr(compressed)] [tdPreXid.zoneId(varint64)] [tdPreXid.slotId(varint64)]
[tdPreCsn(varint64)] [tdId(1B)] [tdCsnStatus(1B)]
[txnPreUndoPtr(compressed)] [ctid(compressed)] [fileVersion(varint64)]
-- 可变长部分 --
[dataLen(2B)] [data(dataLen bytes)]
```

---

## 5. 两条链：事务链 vs 跨事务链

### 5.1 两条链的定义

```
链1：事务链（Intra-Transaction Chain）
  UndoRecord.m_txnPreUndoPtr
  连接同一事务内所有 Undo 记录，从尾到头
  用途：事务回滚时按逆序回放

链2：跨事务链（Inter-Transaction Chain / TD Chain）
  UndoRecord.m_header.m_tdPreInfo.m_undoRecPtr
  连接同一行的不同事务版本
  用途：MVCC 读时跨事务遍历历史版本
```

### 5.2 结构示意

```
TransactionSlot(T3)
  curTailUndoPtr ──→ [U3c: UPDATE row R, ctid=R]
                          m_txnPreUndoPtr ──→ [U3b: DELETE row Q]
                                                  m_txnPreUndoPtr ──→ [U3a: INSERT row P]
                                                                            m_txnPreUndoPtr = NULL

事务链：U3c → U3b → U3a → NULL（回滚从 U3c 开始，逆序执行）

行 R 的跨事务链：
  数据页 TD[x] ──→ [U3c: T3修改R, tdPreInfo.xid=T2, tdPreInfo.undoRecPtr=U2]
                         ↓ tdPreInfo（跨事务链）
                    [U2: T2修改R, tdPreInfo.xid=T1, tdPreInfo.undoRecPtr=U1]
                         ↓ tdPreInfo（跨事务链）
                    [U1: T1修改R, tdPreInfo.xid=INVALID（初始版本）]
```

### 5.3 TD 槽的关键作用

页面的 TD 槽是版本链的入口：

```
HeapPage TD[tdId]：
  ├─ xid         → 最新修改行 R 的事务 XID
  ├─ undoRecPtr  → U3c（最新 Undo 记录地址）
  └─ csn         → T3 的提交 CSN（如已提交）

读取行 R 时：
  1. 检查 TD[tdId].xid 对当前快照是否可见
  2. 不可见 → 沿 tdPreInfo 链找到前一个版本
  3. 直到找到对快照可见的版本
```

---

## 6. TransactionSlot：事务在 Undo 中的锚点

```cpp
// include/undo/dstore_transaction_slot.h:57
struct TransactionSlot {
    uint64 curTailUndoPtr;   // 事务当前最新 Undo 记录（回滚入口）
    uint64 spaceTailUndoPtr; // 事务分配的 Undo 空间尾指针（不同于 cur）
    CommitSeqNo csn;         // 提交 CSN（COMMITTED 后才有效）
    TrxSlotStatus status : 8;  // 七态状态机
    uint64 reserve : 8;
    uint64 logicSlotId : 48;   // 槽的逻辑 ID
    uint64 commitEndPlsn;      // 提交 WAL 的结束 PLSN（两阶段提交）
    WalId walId;               // 提交 WAL 所在的流
} PACKED;
```

**curTailUndoPtr vs spaceTailUndoPtr 的区别**：

| 字段 | 含义 | 何时更新 |
|------|------|---------|
| `curTailUndoPtr` | 事务中最后一条 Undo 记录位置 | 每次 Insert/Rollback 后更新 |
| `spaceTailUndoPtr` | 事务分配的 Undo 空间末尾 | 仅 InsertUndoRecord 时更新 |

回滚时从 `curTailUndoPtr` 开始，沿事务链逆向回放；回收时用 `spaceTailUndoPtr` 确定此事务占用的 Undo 空间范围。

### TailUndoPtrStatus

```cpp
enum TailUndoPtrStatus : uint8 {
    UNKNOWN_STATUS = 0,
    NO_VALID_TAIL_UNDO_PTR,           // Undo 区无有效指针（槽已被回收）
    NEED_FETCH_FROM_COMMITED_SLOT,    // 需从已提交但未回收的槽获取
    VALID_STATUS                       // 正常有效指针
};
```

### 事务槽的物理位置寻址

```cpp
// XID.zoneId → UndoZone → TxnSlotManager.m_startPage
// XID.logicSlotId → 槽页面和槽偏移

PageId slotPage = {
    m_startPage.fileId,
    m_startPage.blockId + (logicSlotId / TRX_PAGE_SLOTS_NUM) % TRX_PAGES_PER_ZONE
};
uint32 slotOffset = logicSlotId % TRX_PAGE_SLOTS_NUM;
```

---

## 7. UndoZone：写入流程

### 7.1 InsertUndoRecord() 全流程

```
UndoZone::InsertUndoRecord(record)

  Step 1: ExtendSpaceIfNeeded()
            检查 m_needCheckPageId 是否已满
            如满 → AllocNewUndoPages() → ExtendUndoPageRing()
            （保证写入时总有空间）

  Step 2: record->Serialize()
            Varint 压缩写入 thrd->GetUndoContext()

  Step 3: 锁定当前 Undo 页（m_currentInsertUndoPageBuf）
            BufMgr::Read(pageId, LW_EXCLUSIVE)
            （缓存当前页，减少 buffer 加锁次数）

  Step 4: WriteUndoRecord()
            while 未写完：
              InsertUndoBytes(src, writePtr, pageEnd, ...)
              if 跨页：
                生成 WAL（GenerateWalForUndoRec）
                解锁当前页，移到 next 页

  Step 5: 更新 m_nextAppendUndoPtr
            SetOffset(newOffset)

  返回 UndoRecPtr（新记录的物理地址）
```

### 7.2 写入时的 WAL

```
每写一个 Undo 页就生成一条 WAL_UNDO_RECORD：
  WalRecordUndoRecord { m_offset, m_data[] }
  Redo: memcpy 到 page + m_offset

同时生成 WAL_UNDO_UPDATE_SLOT_UNDO_PTR：
  WalRecordUndoUpdateSlotUndoPtr { m_slotId, insertUndoFlag, m_tailPtr }
  Redo: slot->SetCurTailUndoPtr(tailPtr)
        if insertUndoFlag: slot->SetSpaceTailUndoPtr(tailPtr)
```

### 7.3 事务链挂接

```cpp
// InsertUndoRecord 调用前，TransactionMgr 已执行：
record->SetTxnPreUndoPtr(zone->GetSlotCurTailUndoPtr(xid));
//                              ↑ 读取当前事务槽的 curTailUndoPtr
// 写入 Undo 后：
zone->SetSlotUndoPtr(xid, newUndoRecPtr, /*insertUndoFlag=*/true);
//                         ↑ 更新槽的 curTailUndoPtr 和 spaceTailUndoPtr
```

---

## 8. 回滚流程：RollbackUndoZone

```
UndoZone::RollbackUndoZone(xid, isRecovery)

  Step 1: 读取 TransactionSlot
            CopySlot(xid, &txnSlot)

  Step 2: 获取回滚范围
            startUndoPtr = txnSlot.curTailUndoPtr（最新 Undo）
            endUndoPtr   = 确定链尾（NULL or 已提交边界）

  Step 3: RollbackUndoRecords(xid, startUndoPtr, endUndoPtr)
            while curPtr != endUndoPtr:
              FetchUndoRecord(pdbId, &record, curPtr, xid, bufMgr)
              ApplyUndo(record)          ← 根据 UndoType 执行逆操作
              GenerateWalForRollback()   ← 生成回滚 WAL（WalRecordRollbackForData）
              curPtr = record.GetTxnPreUndoPtr()  ← 沿事务链向前
              SetSlotUndoPtr(xid, curPtr)         ← 更新进度（可重入）

  Step 4: 更新事务槽状态为 ABORTED
```

### 回滚 WAL：WalRecordRollbackForData

```cpp
struct WalRecordRollbackForData : public WalRecordUndo {
    UndoType m_undoType;      // 原始 Undo 类型
    uint8 m_tdId;             // TD 槽 ID
    TdCsnStatus m_preCsnStatus;
    OffsetNumber m_tupOff;    // 行偏移
    uint64 m_preXid;          // 前版本 XID
    uint64 m_prePtr;          // 前版本 UndoRecPtr
    CommitSeqNo m_preCsn;     // 前版本 CSN
    char m_data[];            // 旧行数据
};

// Redo 逻辑：
// HeapPage → UndoHeap(&rec)   恢复行数据
// IndexPage → RollbackBtrForRecovery(&rec) 恢复索引
```

**可重入性**：每步回放后即更新 `curTailUndoPtr`，崩溃重启后可从断点继续（与 Day 4 的 `abortStage` 同理）。

---

## 9. MVCC 旧版本重建：CR 页构造

### 9.1 完整流程

```
HeapPage::ConstructCrTuple(snapshot, ctid, ...)

  Step 1: 复制页面 TD 数组
            crTd[0..N] = *GetTd(0..N)
            （不修改原页面，在临时副本上回退）

  Step 2: 确定起始 TD
            tupleTdId = 行元组的 TD 槽 ID
            csn = crTd[tupleTdId].GetCsn()

  Step 3: 回溯版本链（循环）
            xid = crTd[tupleTdId].GetXid()
            undoRecPtr = crTd[tupleTdId].GetUndoRecPtr()

            FetchUndoRecordByMatchedCtid(xid, &record, undoRecPtr, ctid, &csn)
              ↑ 沿跨事务链找到匹配 ctid 的 Undo 记录

            crTd[tupleTdId].RollbackTdInfo(&record)
              ↑ 将 crTd 的 xid/undoRecPtr/csn 回退到前版本

            txnVisible = XidVisibleToSnapshot(snapshot, csn 或 xid)
            if visible: break

  Step 4: 根据 UndoType 重建 Tuple
            ConstructTupleFromXxxUpdate(&record, resTuple)
```

### 9.2 FetchUndoRecordByMatchedCtid() 的跨事务跳转

```
FetchUndoRecordByMatchedCtid(startXid, record, startPtr, ctid, &matchedCsn)

  curXid = startXid
  curPtr = startPtr

  loop:
    FetchUndoRecord(pdbId, record, curPtr, curXid, bufMgr)

    if record.IsMatchedCtid(ctid):
      return SUCC  // 找到这个事务对 ctid 行的最近修改

    // ctid 不匹配：说明此事务修改了其他行，跳到前一事务
    preXid = record.GetTdPreXid()
    if preXid != curXid:
      *matchedCsn = record.GetTdPreCsn()  // 记录前版本 CSN

    curXid = preXid
    curPtr = record.GetTdPreUndoPtr()
```

### 9.3 为何需要复制 TD

```
原因：页面上 TD 槽只存最新版本，
      ConstructCrTuple 需要把 TD 逐步回退到历史状态，
      但不能破坏原页面（可能有并发读取）。

crTd[] = 临时 TD 副本，专供版本重建使用，函数返回后丢弃。
```

### 9.4 MVCC 可见性判断顺序

```
在 ConstructCrTuple 的循环中：

  csn = INVALID_CSN（事务仍在进行中或 FROZEN）？
    → XidVisibleToSnapshot(snapshot, xid, txn)  ← 按 XID 查状态
  csn 有效（已提交）？
    → XidVisibleToSnapshot(snapshot, csn)        ← 直接比较 CSN

  两种路径最终都调用 Day4 的可见性决策树
```

---

## 10. Undo GC：回收机制

### 10.1 触发入口

```
UndoMgr::Recycle(recycleMinCsn)
  ↓
  遍历所有 UndoZone
  UndoZone::Recycle(recycleMinCsn)
```

`recycleMinCsn` 由 Day4 的 `CsnMgr::GetRecycleCsnMin()` 提供：
```
recycleMinCsn = min(localCsnMin, barrierCsnMin, flashbackCsnMin, backupRestoreCsnMin)
含义：所有活跃事务中最小快照 CSN，低于此值的历史版本对所有事务不可见，可安全回收
```

### 10.2 回收流程

```
UndoZone::Recycle(recycleMinCsn)

  Phase 1: 回收事务槽（RecycleTxnSlots）
    遍历 m_recycleLogicSlotId → m_nextFreeLogicSlotId：
      对每个槽：IsSlotRecyclable(logicSlotId, recycleMinCsn, &tailUndoPtr, ...)
        可回收条件：
          ① 槽已提交/回滚（status != IN_PROGRESS）
          ② 槽 CSN < recycleMinCsn
        可回收 → 记录 recycleUndoPtr（最后一个可回收槽的 spaceTailUndoPtr）
        不可回收 → 停止扫描（保守策略：遇到不可回收就停）

    生成 WAL_UNDO_RECYCLE_SLOT：
      WalRecordRecycleSlot { zoneId, recycleCsn, lastRecycleSlotId, newRecycleSlotId }
      Redo：将范围内槽状态设为 TXN_STATUS_FROZEN

    更新 m_recycleLogicSlotId（原子推进）

  Phase 2: 回收 Undo 页（RecycleUndoPage）
    m_undoRecyclePageId 推进到 recycleUndoPtr 对应的页面
    清空（或标记）回收范围内的 Undo Record Page
```

### 10.3 Undo 页回收条件

```
可以回收 UndoRecordPage 的条件：

  该页面内所有 Undo 记录对应的事务，其 CSN 均 < recycleMinCsn

  判断逻辑：
    事务槽 spaceTailUndoPtr 的页号 ≤ 当前回收页号
    → 该事务的所有 Undo 都在已经过的页内
    → 该事务 CSN < recycleMinCsn
    → 该页面可回收
```

### 10.4 回收 vs 扩展的关系

```
Undo 页面是循环链表（Ring）：

  → ... → Page[recyclePageId] → ... → Page[nextAppendPageId] → ...
           ↑ 回收推进方向                  ↑ 写入推进方向

  写入速度 > 回收速度 → 需要 ExtendUndoPageRing() 扩展
  回收追上写入 → Ring 缩小（节省空间）
```

---

## 11. Undo WAL 记录体系

### 11.1 继承层次

```
WalRecord (4B: size + type)
  └─ WalRecordForPage (+ pageId + flags + preWalId/Plsn/Glsn)
      └─ WalRecordUndo (保持结构，无新字段)
          ├─ WalRecordUndoRecord          WAL_UNDO_RECORD
          │   { m_offset, m_data[] }
          │   Redo: memcpy(page + offset, data, len)
          │
          ├─ WalRecordUndoUpdateSlotUndoPtr  WAL_UNDO_UPDATE_SLOT_UNDO_PTR
          │   { m_slotId, insertUndoFlag, m_tailPtr }
          │   Redo: slot->SetCurTailUndoPtr / SetSpaceTailUndoPtr
          │
          ├─ WalRecordTransactionCommit    WAL_TXN_COMMIT
          │   { m_slotId, m_csn, m_status(PENDING/COMMITTED), m_commitTime }
          │   Redo: slot->SetCsn(csn), slot->SetTrxSlotStatus(status)
          │         UpdateMaxReservedCsnIfNecessary(csn)
          │
          ├─ WalRecordTransactionAbort     WAL_TXN_ABORT
          │   { m_slotId, m_csn, m_abortTime }
          │   Redo: slot->SetCsn(csn), slot->SetTrxSlotStatus(ABORTED)
          │
          ├─ WalRecordRollbackForData      WAL_UNDO_HEAP / WAL_UNDO_BTREE
          │   { m_undoType, m_tdId, m_preCsnStatus, m_tupOff,
          │     m_preXid, m_prePtr, m_preCsn, m_data[] }
          │   Redo: HeapPage::UndoHeap() 或 BtrPage::RollbackBtrForRecovery()
          │
          ├─ WalRecordRecycleSlot          WAL_UNDO_RECYCLE_SLOT
          │   { m_zoneId, m_recycleCsn, m_lastRecycle, m_newRecycle }
          │   Redo: 将范围内 slot 状态设为 FROZEN
          │
          ├─ WalRecordUndoRingNewPage      WAL_UNDO_RING_NEW_PAGE
          │   { m_prev, m_next }
          │   Redo: 初始化新 UndoRecordPage 头部和双向链接
          │
          └─ WalRecordUndoRingOldPage      WAL_UNDO_RING_OLD_PAGE
              { adjacentPageId }
              Redo: 更新相邻页面指针（接入新页）
```

### 11.2 WAL 类型与操作的对应

| WAL 类型 | 触发时机 |
|---------|---------|
| `WAL_UNDO_RECORD` | InsertUndoRecord 写入 Undo 页 |
| `WAL_UNDO_UPDATE_SLOT_UNDO_PTR` | InsertUndoRecord 更新事务槽指针 |
| `WAL_UNDO_TXN_SLOT_ALLOCATE` | AllocSlot 分配新事务槽 |
| `WAL_TXN_COMMIT` | CommitInternal 两阶段提交 |
| `WAL_TXN_ABORT` | AbortInternal 事务回滚完成 |
| `WAL_UNDO_HEAP` | RollbackUndoRecords 回滚 Heap 操作 |
| `WAL_UNDO_BTREE` | RollbackUndoRecords 回滚 BTree 操作 |
| `WAL_UNDO_RECYCLE_SLOT` | Recycle 回收事务槽 |
| `WAL_UNDO_RING_NEW_PAGE` | ExtendUndoPageRing 新增 Undo 页 |
| `WAL_UNDO_INIT_REC_SPACE` | InitUndoRecordSpace 初始化记录空间 |

---

## 12. 异步回滚：RollbackTrxTaskMgr

### 12.1 为何需要异步回滚

```
场景：事务异常终止（线程崩溃/超时/强制回滚）
问题：同步回滚可能耗时极长（大事务有几十万条 Undo）
解决：将回滚任务提交到 RollbackTrxTaskMgr，由后台 Worker 异步执行
```

### 12.2 架构

```
RollbackTrxTaskMgr（每 PDB 一个）
  │
  ├─ m_rollbackTrxTaskQueue（dlist，任务队列）
  ├─ m_dispatchThread（单一分发线程）
  └─ m_workers[MAX_ROLLBACK_WORKER_NUM]（最多 10 个 Worker）

分发线程逻辑（DispatchMain）：
  while not needStop:
    task = GetNextRollbackTrxTask()
    worker = GetNextIdleWorker()
    worker.Assign(task)
    WakeupWorker()
```

### 12.3 任务结构

```cpp
struct RollbackTrxTask {
    dlist_node nodeInList;       // 队列链表节点
    Xid rollbackXid;             // 需要回滚的 XID
    UndoZone *rollbackUndoZone;  // 对应的 UndoZone（含所有 Undo 数据）
};
```

### 12.4 与 TransactionSlot 的状态交互

```
异步回滚期间：
  TransactionSlot.status = TXN_STATUS_IN_PROGRESS（仍在回滚中）
  UndoZone::m_isAsyncRollbacking = true

  其他事务读到 IN_PROGRESS → 等待
  异步回滚完成 → status = ABORTED
```

---

## 13. 完整交互时序图

### 13.1 INSERT 写 Undo + 提交

```
事务 T1 执行 INSERT ROW R
│
├─ 1. AllocSlot()
│      ZoneId = XID.zoneId
│      UndoZoneTrxManager::AllocSlot()
│      → WAL: WAL_UNDO_TXN_SLOT_ALLOCATE
│      返回 XID = (zoneId=Z, logicSlotId=S)
│
├─ 2. 构建 UndoRecord
│      type=UNDO_HEAP_INSERT, ctid=R, cid=0
│      SetPreTdInfo(tdId, td)    ← 记录当前 TD 的 xid/undoPtr/csn
│      SetTxnPreUndoPtr(INVALID) ← 事务链头
│      dataSize = 0（INSERT 无旧数据）
│
├─ 3. InsertUndoRecord(record)
│      → ExtendSpaceIfNeeded()
│      → Serialize()（Varint 压缩）
│      → WriteUndoRecord(page, ...)
│      → WAL: WAL_UNDO_RECORD（写入 Undo 页）
│      → WAL: WAL_UNDO_UPDATE_SLOT_UNDO_PTR（更新槽指针）
│      → m_nextAppendUndoPtr 推进
│      返回 undoRecPtr = U1
│
├─ 4. 更新 HeapPage TD 槽
│      TD[tdId].xid = XID(T1)
│      TD[tdId].undoRecPtr = U1
│      WAL: WAL_HEAP_INSERT（页面修改）
│
├─ 5. CommitInternal()
│      Phase A: WAL: WAL_TXN_COMMIT(PENDING_COMMIT, csn=0)
│               WaitTargetPlsnPersist()   ← Day5 WAL 持久化等待
│      Phase B: WAL: WAL_TXN_COMMIT(COMMITTED, csn=C1)
│               TransactionSlot.csn = C1
│               TransactionSlot.status = COMMITTED
│
└─ 完成
```

### 13.2 MVCC 旧版本读取

```
事务 T_read（snapshotCsn=45）读取行 R

行 R 当前：TD[x].xid=T3（csn=60，不可见），TD[x].undoRecPtr=U3

ConstructCrTuple(snapshot=45, ctid=R)
│
├─ crTd[x] = TD[x] 的副本
│
├─ 循环1：
│    FetchUndoRecord(U3) → record3（T3对R的修改）
│    FetchUndoRecordByMatchedCtid → record3.tdPreInfo = {xid=T2, ptr=U2, csn=50}
│    crTd[x].RollbackTdInfo(record3)
│      → crTd[x].xid = T2, undoRecPtr = U2, csn = 50
│    XidVisibleToSnapshot(snapshot=45, csn=50) → FALSE（50 > 45）
│    继续循环
│
├─ 循环2：
│    FetchUndoRecord(U2) → record2（T2对R的修改）
│    FetchUndoRecordByMatchedCtid → record2.tdPreInfo = {xid=T1, ptr=U1, csn=30}
│    crTd[x].RollbackTdInfo(record2)
│      → crTd[x].xid = T1, undoRecPtr = U1, csn = 30
│    XidVisibleToSnapshot(snapshot=45, csn=30) → TRUE（30 < 45）
│    break
│
└─ ConstructTupleFromXxxUpdate(record2, resTuple)
     将行 R 的数据回退到 T2 修改前（T1 插入时）的状态
     返回给读操作
```

### 13.3 回滚流程

```
事务 T2 执行 ROLLBACK
│
├─ TransactionMgr::AbortInternal()
│    abortStage: AbortNotStart → PreAbortDone → RecordAbortDone ...
│
├─ UndoZone::RollbackUndoZone(xid=T2)
│    txnSlot.curTailUndoPtr = U2c（最后一条 Undo）
│
├─ RollbackUndoRecords(U2c → NULL)
│    ┌─ FetchUndoRecord(U2c) → record（UPDATE）
│    │  ApplyUndo → HeapPage::UndoHeap()：恢复行为 update 前的值
│    │  WAL: WAL_UNDO_HEAP（记录回滚操作）
│    │  SetSlotUndoPtr(T2, U2c.txnPrePtr)（推进进度，可重入）
│    │
│    ├─ FetchUndoRecord(U2b) → record（DELETE）
│    │  ApplyUndo → HeapPage::UndoHeap()：恢复删除行
│    │  WAL: WAL_UNDO_HEAP
│    │  SetSlotUndoPtr(T2, U2b.txnPrePtr)
│    │
│    └─ FetchUndoRecord(U2a) → record（INSERT）
│       ApplyUndo → HeapPage::UndoHeap()：删除插入的行
│       WAL: WAL_UNDO_HEAP
│       SetSlotUndoPtr(T2, NULL)
│
└─ WAL: WAL_TXN_ABORT
   TransactionSlot.status = ABORTED
```

---

## 14. 与 Day3/4/5 的连接点

### 14.1 与 Day 3（Buffer Manager）

| Day3 概念 | Day6 中的体现 |
|----------|--------------|
| CR 页（Consistent Read Page）| `ConstructCrTuple()` 通过 Undo 链回溯构造历史快照 |
| BufferDesc.recoveryPlsn | Undo 页写入时也需 WAL-First：先 WAL 落盘再刷 Undo 页 |
| BufMgr::Read(LW_EXCLUSIVE) | InsertUndoRecord 时获取 Undo 页独占锁 |
| m_currentInsertUndoPageBuf | 缓存当前 Undo 写入页面的 BufferDesc，减少 buffer 查找 |

### 14.2 与 Day 4（Transaction + MVCC）

| Day4 概念 | Day6 中的体现 |
|----------|--------------|
| XID.zoneId = ZoneId | XID 同时是 Undo Zone 物理地址，O(1) 定位事务槽 |
| TransactionSlot（Undo Zone） | Day4 定义结构，Day6 详述物理存储和管理 |
| TrxSlotStatus 七态 | Day4 的状态机定义，Day6 的 WAL Redo 恢复这些状态 |
| recycleMinCsn | Day4 的 CsnMgr 计算，Day6 的 Recycle 用它决定回收范围 |
| CommitInternal 两阶段 | Day6 的 WAL_TXN_COMMIT(PENDING→COMMITTED) 正是两阶段 WAL |
| AbortInternal 多阶段可重入 | Day6 RollbackUndoZone 每步保存进度，支持崩溃后续跑 |
| XidVisibleToSnapshot | Day6 ConstructCrTuple 最终调用此函数判断历史版本可见性 |

### 14.3 与 Day 5（WAL + Checkpoint）

| Day5 概念 | Day6 中的体现 |
|----------|--------------|
| WalRecordForPage 继承链 | WalRecordUndo 继承 WalRecordForPage，Undo WAL 也是页面级 WAL |
| 5 步写入工作流 | InsertUndoRecord 内部也调用 BeginAtomicWal/PutRecord/EndAtomicWal |
| WAL-First | Undo 页刷盘前 WAL 必须先落盘（和数据页一样遵守 PrepareCheckPage）|
| Redo 恢复 | Undo WAL 有完整 Redo 逻辑（WalRecordUndoRecord.Redo 等），崩溃后可恢复 |
| Checkpoint | Undo 页也是脏页，也通过 BgDiskPageWriter 刷盘，也被 GetMinRecoveryPlsn 追踪 |

---

## 15. 快速查阅表

### 关键常数

| 常数 | 值 | 含义 |
|------|-----|------|
| `UNDO_ZONE_COUNT` | 1,048,576 | 最大 UndoZone 数量（= 最大并发事务线程数）|
| `TRX_PAGES_PER_ZONE` | 127（生产）/ 7（UT）| 每 Zone 事务槽页面数 |
| `MAX_ROLLBACK_WORKER_NUM` | 10 | 异步回滚最大 Worker 数 |
| `UNDO_RECORD_VECTOR_DEFAULT_CAPACITY` | 8 | UndoRecordVector 初始容量 |
| `INVALID_ZONE_ID` | -1 | 无效 ZoneId |
| `INVALID_TXN_SLOT_ID` | -1 | 无效事务槽 ID |

### 核心文件速查

| 功能 | 文件 |
|------|------|
| UndoRecPtr / ZoneId / UndoType | `include/undo/dstore_undo_types.h` |
| UndoTdInfo / UndoRecordHeader / UndoRecord | `include/undo/dstore_undo_record.h` |
| TransactionSlot / TrxSlotStatus / TailUndoPtrStatus | `include/undo/dstore_transaction_slot.h` |
| UndoZone（写入/回滚/回收） | `include/undo/dstore_undo_zone.h` |
| UndoZoneTrxManager（事务槽管理） | `include/undo/dstore_undo_zone_txn_mgr.h` |
| UndoMgr（多 Zone 管理器） | `include/undo/dstore_undo_mgr.h` |
| Undo WAL 记录体系 | `include/undo/dstore_undo_wal.h` |
| RollbackTrxTaskMgr（异步回滚） | `include/undo/dstore_rollback_trx_task_mgr.h` |
| UndoZone 实现 | `src/undo/dstore_undo_zone.cpp` |
| FetchUndoRecordByMatchedCtid | `src/transaction/dstore_transaction_mgr.cpp:825` |
| ConstructCrTuple | `src/page/dstore_heap_page.cpp:875` |

### 核心 API 速查

| 操作 | API | 说明 |
|------|-----|------|
| 分配事务槽 | `UndoZone::AllocSlot()` | 返回 XID |
| 写入 Undo | `UndoZone::InsertUndoRecord(record)` | 返回 UndoRecPtr |
| 读取 Undo | `UndoZone::FetchUndoRecord(pdbId, record, ptr, xid, bufMgr)` | 按地址读 |
| 跨事务链查找 | `FetchUndoRecordByMatchedCtid(...)` | MVCC 读版本链 |
| 构造历史版本 | `HeapPage::ConstructCrTuple(snapshot, ctid, ...)` | CR 页重建 |
| 回滚事务 | `UndoZone::RollbackUndoZone(xid, isRecovery)` | 可重入 |
| 提交事务 | `UndoZone::Commit<COMMITTED>(xid, csn)` | 写入提交 CSN |
| 回收 Undo | `UndoZone::Recycle(recycleMinCsn)` | GC 入口 |
| 异步回滚任务 | `RollbackTrxTaskMgr::AddRollbackTrxTask(xid, zone)` | 提交异步任务 |

### 两条链对比

| 维度 | 事务链 | 跨事务链（TD 链）|
|------|--------|----------------|
| 字段 | `m_txnPreUndoPtr` | `m_tdPreInfo.m_undoRecPtr` |
| 方向 | 同事务逆向（最新→最早）| 同行跨事务（当前→前版本）|
| 用途 | Rollback 时逆序回放 | MVCC 读时寻找可见版本 |
| 起点 | TransactionSlot.curTailUndoPtr | HeapPage TD[tdId].undoRecPtr |
| 终点 | NULL（事务首条 Undo）| tdPreInfo.xid = INVALID（行初始版本）|

---

## 下一步

Day 7 深入 Heap（含 BigTuple）：
- HeapPage 的行插入/删除/更新完整逻辑
- BigTuple：跨页大行的存储机制
- 行可见性判断（TupleTdStatus 三态决策）
- FSM（Free Space Map）与空间管理的协同
- Heap 与 Undo 的交互（Day6 基础上构建完整写路径）
