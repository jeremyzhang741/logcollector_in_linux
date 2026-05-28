# Day 7 — Heap 模块（含 BigTuple + FSM）

## 目录

1. [Heap 模块总览](#1-heap-模块总览)
2. [HeapPage 物理布局](#2-heappage-物理布局)
3. [ItemId：间接指针的四种状态](#3-itemid间接指针的四种状态)
4. [TD（Transaction Descriptor）详解](#4-tdtransaction-descriptor详解)
5. [TupleTdStatus 三态：MVCC 可见性的核心开关](#5-tupletdstatus-三态mvcc-可见性的核心开关)
6. [TD 分配流程：AllocTd → DoAllocTd → TryReuseTdSlots](#6-td-分配流程allocTd--doalloctd--tryreusetdslots)
7. [写入路径：INSERT / UPDATE / DELETE](#7-写入路径insert--update--delete)
8. [读取路径：GetVisibleTuple + ConstructCrTuple](#8-读取路径getvisibletuple--constructcrtuple)
9. [IS_PREV_XID_CSN 设计细节](#9-is_prev_xid_csn-设计细节)
10. [页面压实：Prune + Compact](#10-页面压实prune--compact)
11. [BigTuple：跨页大行存储](#11-bigtuplepages大行存储)
12. [FSM：空闲空间管理](#12-fsm空闲空间管理)
13. [完整写入时序图（INSERT）](#13-完整写入时序图insert)
14. [与前序模块的连接点](#14-与前序模块的连接点)
15. [快速查阅表](#15-快速查阅表)

---

## 1. Heap 模块总览

```
Heap 模块职责：
  ├─ 行数据的物理存储（HeapPage，行版本链）
  ├─ 行级别 MVCC（TupleTdStatus + TD + Undo 链）
  ├─ TD 槽管理（分配 / 回收 / 扩展）
  ├─ BigTuple 跨页大行存储
  └─ 与 FSM 协作定位可用页面

关键设计原则：
  WAL-First：修改页面 → 生成 WAL → MarkDirty
  Undo-First：修改页面前先写 Undo（支持回滚 + MVCC）
  TD 是页面级事务锚点：每个活跃事务在页面上占一个 TD 槽
```

---

## 2. HeapPage 物理布局

### 2.1 页面结构层次

```
HeapPage（8KB，BLCKSZ）
  │
  ├─ Page（48B，基础头）
  │   ├─ m_lsn          WAL LSN（WAL-First 关键字段）
  │   ├─ m_selfPtr       页面自引用（ItemPointerData）
  │   ├─ m_lower         ItemId 数组下界（向右增长）
  │   ├─ m_upper         Tuple 数据上界（向左增长）
  │   └─ m_pageFlags     页面标志
  │
  ├─ DataPageHeader（包含）
  │   ├─ m_tdCount       TD 槽数量（uint8，默认4，最大128）
  │   └─ m_headerOffset  数据区起始偏移
  │
  ├─ HeapPageHeader（88B，HEAP_PAGE_HEADER_SIZE）
  │   ├─ potentialDelSize   可删除行大小（Prune 用）
  │   ├─ fsmIndex           该页在 FSM 的索引
  │   └─ recentDeadTupleMinCsn  最近死亡 Tuple 最小 CSN
  │
  ├─ TD 数组（紧接头部）
  │   └─ TD[0] ~ TD[tdCount-1]  每个 48 字节
  │
  ├─ ItemId 数组（从 m_lower 向上增长）
  │   └─ ItemId[1], ItemId[2], ...  每个 4 字节（32bit）
  │
  ├─ ← 空闲空间（m_upper - m_lower）→
  │
  └─ Tuple 数据区（从 m_upper 向下增长）
      └─ 最新插入的 Tuple 在最低地址
```

### 2.2 关键偏移常数

```cpp
HEAP_PAGE_DATA_OFFSET = sizeof(Page) + sizeof(DataPageHeader) + sizeof(HeapPageHeader)
                      = 48 + ? + 88 = ~88B（含头部所有开销）

MaxDefaultTupleSpace() = BLCKSZ - MAXALIGN(HEAP_PAGE_DATA_OFFSET
                         + DEFAULT_TD_COUNT * sizeof(TD) + sizeof(ItemId))
                       ≈ 8192 - (88 + 4*48 + 4) ≈ 7892B
                         （超过此值即触发 BigTuple）

MAX_ACTIVE_HEAP_TUPLES_PER_PAGE = (BLCKSZ - data_offset) / sizeof(ItemId)
                                = 约几百行（由空间决定）
```

### 2.3 页面标志（m_pageFlags）

```cpp
PAGE_HAS_FREE_LINES  = 0x01  // 有空闲 ItemId 槽可复用
PAGE_TUPLE_PRUNABLE  = 0x02  // 有死亡 Tuple 可 Prune
PAGE_ITEM_PRUNABLE   = 0x04  // 有 NO_STORAGE ItemId 可 Prune
PAGE_IS_NEW_PAGE     = 0x08  // 新分配页面
PAGE_IS_EXTEND_CR_PAGE = 0x10 // CR 页扩展页（MVCC 历史读）
```

---

## 3. ItemId：间接指针的四种状态

```cpp
// include/page/dstore_itemid.h
struct ItemId {
    union {
        ItemType m_placeHolder;
        struct {                   // 普通模式
            uint32 m_flags  : 2;   // 状态
            uint32 m_offset : 15;  // Tuple 在页面中的字节偏移
            uint32 m_len    : 15;  // Tuple 大小（字节）
        } direct;

        struct {                   // NO_STORAGE 模式（行被压实后）
            uint32 m_flags    : 2;
            uint32 m_tdId     : 8;   // 所属 TD 槽（MVCC 仍需）
            uint32 m_tdStatus : 2;   // TupleTdStatus
            uint32 m_tupLiveMode : 3;
            uint32 m_unused   : 17;
        } redirect;
    };
};
```

**四种状态**：

| ItemIdState | flags | 含义 |
|-------------|-------|------|
| `ITEM_ID_UNUSED` | 0 | 空闲槽，可立即复用，len=0 |
| `ITEM_ID_NORMAL` | 1 | 正常行，direct.offset/len 有效 |
| `ITEM_ID_UNREADABLE_RANGE_HOLDER` | 2 | 已回滚的 Tuple，仅保留用于 BTree 键范围，扫描时始终跳过 |
| `ITEM_ID_NO_STORAGE` | 3 | Tuple 数据已被回收（Compact），redirect 中保存 tdId/tdStatus 供 Undo 读取 |

**NO_STORAGE 的设计价值**：Tuple 数据被压实回收后，ItemId 仍需保留 tdId 和 tdStatus，使得还在扫描历史版本的事务能通过 Undo 链重建该行。

---

## 4. TD（Transaction Descriptor）详解

```cpp
// include/page/dstore_td.h:164
struct TD {
    uint64      m_xid;         // 事务 XID
    CommitSeqNo m_csn;         // 提交 CSN（未提交时 = INVALID_CSN）
    uint64      m_undoRecPtr;  // 此事务对页面上最新修改的 UndoRecPtr
    uint64      m_lockerXid;   // 持有行锁的事务（死锁检测）
    CommandId   m_commandId;   // 事务内命令序号（同事务内 CID 比较）
    uint16      m_status   : 2;   // TDStatus（3 态）
    uint16      m_csnStatus: 2;   // TdCsnStatus（3 态）
    uint16      m_pad      : 12;
} // 共 48 字节
```

### TDStatus 三态

```
UNOCCUPY_AND_PRUNEABLE(0)
  ↑ Reset()          │ AllocTd()
  │                  ↓
  │           OCCUPY_TRX_IN_PROGRESS(1)  ← 事务运行时
  │                  │ 提交/回滚（不自动更新，惰性）
  │                  ↓
  └─ Recycle ← OCCUPY_TRX_END(2)         ← TryReuseTdSlots 分类后
```

**TDStatus 是惰性的**：事务提交后不会主动将 TD 状态改为 `OCCUPY_TRX_END`，只有 `TryReuseTdSlots` 需要回收 TD 时才会查询 Undo Zone 得到准确状态。

### TdCsnStatus 三态

```cpp
enum TdCsnStatus : uint8 {
    IS_INVALID     = 0,  // CSN 无效，事务尚未提交
    IS_PREV_XID_CSN = 1, // CSN 属于前一事务（TD 被复用时保留）
    IS_CUR_XID_CSN  = 2  // CSN 属于当前事务（正常已提交状态）
};
```

---

## 5. TupleTdStatus 三态：MVCC 可见性的核心开关

```cpp
// include/tuple/dstore_data_tuple.h
enum TupleTdStatus : uint8 {
    ATTACH_TD_AS_NEW_OWNER     = 0,  // TD 存的就是该 Tuple 的最新事务信息
    ATTACH_TD_AS_HISTORY_OWNER = 1,  // TD 已被新事务复用，此 Tuple 是历史版本
    DETACH_TD                  = 2,  // TD 完全清空，Tuple 独立（MVCC 直接可见）
};
```

**状态在哪里存储**：

1. `HeapDiskTuple.m_tdStatus`（2bit）：行的当前状态
2. `ItemId.redirect.m_tdStatus`（2bit）：行被 Compact 后迁移到 ItemId

**状态转换时机**：

```
INSERT 时：
  → Tuple.m_tdStatus = ATTACH_TD_AS_NEW_OWNER

TD 被新事务复用时（TryReuseTdSlots → RefreshTupleTdStatus）：
  if TD 保留了 CSN（IS_PREV_XID_CSN）：
    → Tuple.m_tdStatus = ATTACH_TD_AS_HISTORY_OWNER
  if TD 被完全清空（Reset）：
    → Tuple.m_tdStatus = DETACH_TD

行被 Compact 到 NO_STORAGE 时：
  → 状态迁移到 ItemId.redirect.m_tdStatus
```

**三态决定读取策略**：

| 状态 | 读取策略 |
|------|---------|
| `ATTACH_TD_AS_NEW_OWNER` | 读 TD.xid/csn → XidVisibleToSnapshot |
| `ATTACH_TD_AS_HISTORY_OWNER` | 读 TD.csn（IS_PREV_XID_CSN）→ CsnVisible？否则查 Undo |
| `DETACH_TD` | 直接可见（无需查 TD 或 Undo）|

---

## 6. TD 分配流程：AllocTd → DoAllocTd → TryReuseTdSlots

### 6.1 AllocTd()：100 次重试包装

```cpp
// src/page/dstore_data_page.cpp:243
TdId DataPage::AllocTd(TDAllocContext &context) {
    for (int i = 0; i < 100; ++i) {
        TdId tdId = DoAllocTd(context);
        if (tdId != INVALID_TD_SLOT) return tdId;
        if (context.NeedRetryAllocTd()) {
            // 需要释放 buffer 后重试（等待进行中事务结束）
            ReleaseBufAndWaitXids(context);
        }
        GaussUsleep(10L);  // 微等 10μs
    }
    return INVALID_TD_SLOT;  // 真正失败（极少）
}
```

### 6.2 DoAllocTd()：四步决策树

```
DoAllocTd(context)
  │
  ├─ Step 1: 扫描所有 TD
  │    ├─ context.txn 已占有该 TD → 复用（事务重入）
  │    └─ 记录第一个 UNOCCUPY_AND_PRUNEABLE 槽
  │
  ├─ 有空闲 TD → 直接返回
  │
  ├─ Step 2: TryReuseTdSlots(context)
  │    └─ 回收已完成事务的 TD（见下文详解）
  │         → 成功找到可用槽 → 返回
  │
  ├─ Step 3: ExtendTd(context)
  │    └─ 物理扩展 TD 数组（2个一批，上限128）
  │         → 成功 → 返回新槽
  │
  └─ 失败 → context.EnableRetryAllocTd() + return INVALID_TD_SLOT
```

### 6.3 TryReuseTdSlots()：三步回收

**Step A：TryReuseOneTdSlot() —— 判断每个 TD 可否回收**

```cpp
switch (XidStatus(td->GetXid()).GetStatus()) {
    case TXN_STATUS_FROZEN:
    case TXN_STATUS_COMMITTED:
        td->FillCsn();  // 从 Undo Zone 填充真实 CSN
        if (td->GetCsn() < context.recycleMinCsn):
            return TD_RECYCLE_UNUSED;   // CSN 太旧，可完全清空
        else:
            return TD_RECYCLE_REUSE;    // CSN 较新，只复用不 Reset

    case TXN_STATUS_IN_PROGRESS:
        context.canResetTd = false;     // 有进行中事务，禁止 Reset
        context.AddTdReuseWaitXid(xid); // 等待这些事务
        return TD_IS_IN_PROGRESS;
}
```

**Step B：RefreshTupleTdStatus() —— 批量更新页面上所有 Tuple 的 tdStatus**

```cpp
// 遍历页面所有 ItemId/Tuple
for each offset in page:
    tdId = GetTupleTdId(offset)
    if tdId 在回收集合中:
        if slot.unused (TD_RECYCLE_UNUSED):
            SetTupleTdStatus(offset, DETACH_TD)
        else (TD_RECYCLE_REUSE):
            SetTupleTdStatus(offset, ATTACH_TD_AS_HISTORY_OWNER)
```

**Step C：GetAvailableTd() —— 选择最优槽**

```
优先级1：canResetTd=true && slot.unused=true
  → td->Reset()，完全归零 → 返回

优先级2：!slot.unused（保留 CSN）
  → 选 CSN 最小（最旧）的已提交 TD 复用
  → td->SetStatus(OCCUPY_TRX_END) → 等 SetTd() 时再设新 XID
```

**canResetTd 标志的关键作用**：
```
有活跃事务（IN_PROGRESS）时 → canResetTd = false
  原因：活跃事务可能要沿 TD.undoRecPtr 回滚；
        如果 TD 被 Reset，undoRecPtr 丢失，回滚就找不到 Undo 链了

所有 TD 槽均已提交 → canResetTd = true
  → 可以安全 Reset TD，彻底释放空间
```

### 6.4 ExtendTd()：物理扩展 TD 数组

```
扩展前：
[DataHeader][TD0~TD3][ItemId1][ItemId2]...[Tuples]

ExtendTd(context) 流程：
  检查 tdCount + EXTEND_TD_NUM(2) ≤ MAX_TD_COUNT(128)
  检查 freeSpace ≥ EXTEND_TD_MIN_NUM(1) * sizeof(TD)
  → memmove ItemId 数组后移 2*sizeof(TD) 字节
  → 在腾出位置初始化 TD4、TD5（Reset）
  → dataHeader.tdCount += 2；m_lower += 2*sizeof(TD)

扩展后：
[DataHeader][TD0~TD5][ItemId1][ItemId2]...[Tuples]
```

---

## 7. 写入路径：INSERT / UPDATE / DELETE

### 7.1 写入全局顺序（WAL-First + Undo-First）

```
分配 TD → 写 Undo → 设置 TD → 修改页面 → 生成 WAL → MarkDirty
   ↑            ↑                                  ↑
Undo-First    页面修改前                         WAL 必须在
              已有回滚能力                       MarkDirty 前完成
```

### 7.2 INSERT 流程

```
HeapInsertHandler::Execute()
  │
  ├─ 1. GetBuffer(table, tupleSize)
  │       → FSM::GetPage(spaceNeeded) → 找到有足够空间的页面
  │       → BufMgr::Read(pageId, LW_EXCLUSIVE) → 上锁
  │
  ├─ 2. AllocTd(context)    → 获取 TD 槽位（tdId）
  │
  ├─ 3. 构建 UndoRecord
  │       type = UNDO_HEAP_INSERT（无旧数据，dataSize=0）
  │       SetPreTdInfo(tdId, td)  ← 快照当前 TD 状态
  │       SetTxnPreUndoPtr(slot.curTailUndoPtr)
  │
  ├─ 4. InsertUndoRecord(record) → undoRecPtr = U_new
  │
  ├─ 5. SetTd(tdId, xid, undoRecPtr, cid)
  │       TD[tdId] = {xid=curXid, undoRecPtr=U_new,
  │                   status=OCCUPY_TRX_IN_PROGRESS, csnStatus=IS_INVALID}
  │
  ├─ 6. HeapPage::AddTuple(diskTuple)
  │       → 分配 ItemId，写入 Tuple 数据
  │       → diskTuple.m_tdId = tdId
  │       → diskTuple.m_tdStatus = ATTACH_TD_AS_NEW_OWNER
  │
  ├─ 7. GenerateWAL(WAL_HEAP_INSERT)
  │
  ├─ 8. MarkDirty(bufDesc)
  │       → BgDiskPageWriter 异步刷盘
  │       → recoveryPlsn 登记（Checkpoint 用）
  │
  └─ 9. FSM::UpdateSpace(pageId, newFreeSpace)
```

### 7.3 UPDATE 三种类型

| 类型 | 触发条件 | 核心行为 | WAL 类型 |
|------|---------|---------|---------|
| **In-Place** | 新旧 Tuple 等大 | 原地覆盖 Tuple 数据 | `WAL_HEAP_INPLACE_UPDATE` |
| **Same-Page Append** | 新 Tuple 更大，当前页有空间 | 旧 Tuple 标记删除，新 Tuple 插入同页 | `WAL_HEAP_SAME_PAGE_APPEND` |
| **Another-Page Append** | 当前页空间不足 | 旧页标记，新 Tuple 插入新页 | `WAL_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD/NEW_PAGE` |

**Another-Page Update 的两条 Undo**：
```
旧页产生：UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE
  （记录旧页上行的变化，回滚时恢复旧页状态）
新页产生：UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE
  （无数据，dataSize=0，回滚时删除新页上的行）
```

### 7.4 DELETE 流程

```
HeapDeleteHandler::Execute()
  │
  ├─ 1. AllocTd()（若没有可复用的当前 TD）
  ├─ 2. 构建 UndoRecord（UNDO_HEAP_DELETE，含旧行数据）
  ├─ 3. InsertUndoRecord()
  ├─ 4. SetTd()
  ├─ 5. diskTuple.m_liveMode = TUPLE_BY_NORMAL_DELETE
  │       AddPotentialDelItemSize()  ← 更新 potentialDelSize
  ├─ 6. GenerateWAL(WAL_HEAP_DELETE)
  └─ 7. MarkDirty()
```

---

## 8. 读取路径：GetVisibleTuple + ConstructCrTuple

### 8.1 GetVisibleTuple()：三态分支决策

```cpp
// include/page/dstore_heap_page.h:125
HeapTuple *HeapPage::GetVisibleTuple(pdbId, txn, ctid, snapshot, is_lob)

  TdId tdId = GetTupleTdId(offset);
  TD *td = GetTd(tdId);

  switch (GetTupleTdStatus(offset)):

    case DETACH_TD:
      // TD 已清空，Tuple 完全独立，直接可见（最快路径）
      needUndo = false;

    case ATTACH_TD_AS_HISTORY_OWNER:
      // TD 已被新事务复用，原事务信息在 TD.csn（IS_PREV_XID_CSN）或 Undo
      if snapshot == SNAPSHOT_DIRTY:
        needUndo = false;
      elif td.csnStatus == IS_INVALID:
        needUndo = true;  // CSN 无效，必须走 Undo 链
      elif XidVisibleToSnapshot(snapshot, td.csn):
        needUndo = false; // 前事务 CSN 可见，当前版本即可见
      else:
        needUndo = true;  // 前事务 CSN 不可见，需更早版本

    case ATTACH_TD_AS_NEW_OWNER:
      xid = td.GetXid()
      if txn.IsCurrent(xid):                 // 同事务内
        needUndo = !CidVisibleToSnapshot(txn, snapshot, td.commandId)
      else:                                  // 其他事务
        needUndo = !XidVisibleToSnapshot(snapshot, xid, txn)

  if needUndo:
    ConstructCrTuple(pdbId, txn, ctid, tdId, &resTuple, snapshot)
  return resTuple
```

### 8.2 ConstructCrTuple()：Undo 链回溯

```cpp
// src/page/dstore_heap_page.cpp:875
RetStatus HeapPage::ConstructCrTuple(pdbId, txn, ctid, tdId, resTuple, snapshot)

  // Step1: 复制 TD 数组（不破坏原页面）
  TD *crTd = DstorePalloc(sizeof(TD) * GetTdCount());
  for i in [0, tdCount): crTd[i] = *GetTd(i);

  TdId tupleTdId = tdId;

  for (;;):
    xid      = crTd[tupleTdId].GetXid()
    undoPtr  = crTd[tupleTdId].GetUndoRecPtr()

    // 无 Undo → 到了最初版本，直接可见
    if undoPtr == INVALID_UNDO_RECORD_PTR:
      txnVisible = true; break;

    // 从 Undo Zone 读取记录，并回退 crTd
    FetchUndoRecordByMatchedCtid(xid, &record, undoPtr, ctid, &matchedCsn)
    crTd[tupleTdId].RollbackTdInfo(&record)
    //   → crTd[tupleTdId] 现在存的是前一个事务的 xid/undoPtr/csn

    // 可见性判断
    if txn.IsCurrent(xid):
      txnVisible = CidVisibleToSnapshot(txn, snapshot, record.GetCid())
    elif matchedCsn != INVALID_CSN:
      txnVisible = XidVisibleToSnapshot(snapshot, matchedCsn)
    else:
      txnVisible = XidVisibleToSnapshot(snapshot, xid, txn)

    if txnVisible: break;

    // 根据 UndoType 构造前版本 Tuple
    ConstructCrTupleFromUndo(&record, resTuple)
    // 继续循环，进入更早的历史版本...

  DstorePfree(crTd)
```

**各 UndoType 对应的历史版本重建函数**：

| UndoType | 构造函数 | 说明 |
|---------|---------|------|
| `UNDO_HEAP_DELETE` | `ConstructTupleFromUndoDelete` | 恢复被删行 |
| `UNDO_HEAP_INPLACE_UPDATE` | `ConstructTupleFromInplaceUpdate` | 原地 UPDATE 前的数据 |
| `UNDO_HEAP_SAME_PAGE_APPEND_UPDATE` | `ConstructTupleFromSamePageUpdate` | 同页扩展前的数据 |
| `UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE` | `ConstructTupleFromAnotherPageUpdateOldPage` | 跨页 UPDATE 旧页数据 |

---

## 9. IS_PREV_XID_CSN 设计细节

### 9.1 为何需要它

```
时序：
  T1 INSERT Tuple A（tdId=5），提交（csn=100）
  T2 需要 TD，复用 TD5：SetTd(5, T2, ...)

如果 SetTd 直接覆盖 TD5.csn：
  快照 S（snapshotCsn=80）读 Tuple A：
    Tuple A.tdStatus = ATTACH_TD_AS_HISTORY_OWNER
    TD5.csn = ??? → 被 T2 覆盖，无法得知 T1 的 csn=100

有 IS_PREV_XID_CSN：
  SetTd(5, T2, ...) 时：
    if TD5.csnStatus == IS_CUR_XID_CSN:
      TD5.csnStatus = IS_PREV_XID_CSN  （仅改标记）
      TD5.csn = 100  （保持不变！）
      TD5.xid = T2   （更新为新事务）
  
  快照 S 读 Tuple A：
    IS_PREV_XID_CSN → TD5.csn=100 属于前一事务 T1
    XidVisibleToSnapshot(csn=100, snapshot=80) → FALSE
    → 走 Undo 找 T1 之前的版本
```

### 9.2 三种 csnStatus 的含义对照

| csnStatus | TD.csn 的含义 | TD.xid 的含义 |
|-----------|--------------|--------------|
| `IS_INVALID` | 无效（当前事务未提交） | 当前事务 |
| `IS_PREV_XID_CSN` | 前一事务的提交 CSN | 当前（新）事务 |
| `IS_CUR_XID_CSN` | 当前事务的提交 CSN | 当前事务 |

### 9.3 提交时的 csnStatus 更新

```cpp
// Transaction commit 时：
TD[tdId].SetCsn(commitCsn);
TD[tdId].SetCsnStatus(IS_CUR_XID_CSN);
TD[tdId].SetStatus(OCCUPY_TRX_END);
```

---

## 10. 页面压实：Prune + Compact

### 10.1 触发时机

```
触发条件：
  INSERT/UPDATE 时发现页面空间不足 → HeapPage::TryCompactTuples()

依据字段：
  potentialDelSize：页面头中记录的"可删除行大小"
  recentDeadTupleMinCsn：最近死行的最小 CSN（用于判断是否可安全 Prune）
```

### 10.2 Prune 流程（物理删除死亡行）

```
ScanCompactableItems(compactItems, &notPrunedDelSize)
  ↓ 扫描页面找到可 Prune 的 ItemId（已提交的 DELETE/UPDATE 旧版本）

PruneItems(itemIdDiff, nItems)
  ↓ 对每个可 Prune 的 ItemId：
    → 将 Tuple 数据标记为不可访问
    → ItemId 转为 ITEM_ID_NO_STORAGE（保留 tdId/tdStatus）
    → 生成 WAL（WAL_HEAP_PRUNE_ITEMS）
```

### 10.3 Compact 流程（整理页面碎片）

```
TryCompactTuples()
  ↓ 对页面上所有 ITEM_ID_NORMAL 的 Tuple 按 offset 降序排列
  ↓ 从 m_upper 向下连续重新排布 Tuple
  ↓ 更新 ItemId.direct.m_offset
  ↓ 更新 m_upper
  生成 WAL（WAL_HEAP_COMPACT_TUPLES）
```

---

## 11. BigTuple：跨页大行存储

### 11.1 触发阈值

```cpp
// include/page/dstore_heap_page.h:194
static bool TupBiggerThanPage(HeapTuple *tuple)
{
    return tuple->GetDiskTupleSize() > MaxDefaultTupleSpace();
    // MaxDefaultTupleSpace() ≈ 7892B（默认 4 个 TD 时）
}
```

超过 ~7.9KB 的行触发 BigTuple 分块存储。

### 11.2 Chunk 结构

```
BigTuple 切分成 N 个 Chunk，每个 Chunk = 普通 HeapDiskTuple + 12B 链接头

首 Chunk（m_linkInfo = TUP_LINK_FIRST_CHUNK_TYPE）：
  ┌─ HeapDiskTuple header
  ├─ NextChunkCtid（8B，ItemPointerData → 第2个 Chunk 的位置）
  ├─ NumTupChunks（4B，总 Chunk 数量）
  └─ 原始数据第1段

后续 Chunk（m_linkInfo = TUP_LINK_NOT_FIRST_CHUNK_TYPE）：
  ┌─ HeapDiskTuple header
  ├─ NextChunkCtid（8B，下一 Chunk 或 INVALID）
  ├─ NumTupChunks（4B，占位与首 Chunk 相同）
  └─ 原始数据第N段

LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE = sizeof(ItemPointerData) + sizeof(uint32) = 12B
```

### 11.3 INSERT：反向插入

```cpp
// src/heap/dstore_heap_insert.cpp
InsertBigTuple(insertContext):
  ItemPointerData tupNextChunkCtid = INVALID_ITEM_POINTER;

  // ★ 从最后一个 Chunk 倒序插入
  for (int32 i = m_tupChunks.m_chunkNum - 1; i >= 0; --i):
    chunk->SetNextChunkCtid(tupNextChunkCtid)  // 已知下一个 Chunk 的位置
    InsertSmallDiskTup(insertContext, chunk, chunkSize)
    tupNextChunkCtid = insertContext->ctid     // 记录本 Chunk 位置
  // 最终 Chunk0（首 Chunk）指向 Chunk1，Chunk1 指向 Chunk2，...
```

**反向插入的原因**：顺序插入时，写 Chunk[i] 时不知道 Chunk[i+1] 的位置；倒序插入时，写 Chunk[i] 时 Chunk[i+1] 已插入，位置已知。

每个 Chunk 独立走完整写入流程：AllocTd → InsertUndoRecord → SetTd → AddTuple → WAL → MarkDirty。

### 11.4 SCAN：链式读取 + 重组

```cpp
// src/heap/dstore_heap_scan.cpp
FetchBigTuple(firstChunk, needCheckVisibility):
  numChunks = firstChunk.GetNumChunks()
  tupChunks[0] = firstChunk

  ctid = firstChunk.GetNextChunkCtid()
  i = 1
  while ctid != INVALID:
    if needCheckVisibility:
      tuple = FetchVisibleDiskTuple(ctid)  // 带 MVCC 可见性判断
    else:
      tuple = FetchNewestDiskTuple(ctid)   // 最新版本（不校验）
    tupChunks[i++] = tuple
    ctid = tuple.GetNextChunkCtid()

  return AssembleTuples(tupChunks, numChunks)
```

**AssembleTuples() 核心逻辑**：

```cpp
AssembleTuples(tupChunks[], numChunks):
  // 计算完整 Tuple 大小（每个 Chunk 去掉 12B 链接头）
  bigSize = sizeof(HeapDiskTuple)
  for chunk in tupChunks:
    bigSize += chunk.dataSize - sizeof(HeapDiskTuple) - 12B

  // 复制首 Chunk header
  memcpy(bigTup.header, firstChunk.header, sizeof(HeapDiskTuple))

  // 拼接各 Chunk 数据（跳过 12B 链接头）
  for chunk in tupChunks:
    src = chunk.data + 12B  // 跳过 NextChunkCtid + NumChunks
    memcpy(dst, src, chunkDataLen)
    dst += chunkDataLen

  bigTup.SetNoLink()  // ★ 清除 BigTuple 标记，变回普通 Tuple
  return bigTup
```

### 11.5 DELETE：链式删除

```cpp
DeleteBigTuple(delContext):
  ctid = delContext->ctid
  nextCtid = GetNextChunk(bufDesc, ctid.GetOffset())  // 先读下一跳

  DeleteDiskTuple(delContext, ctid.GetOffset())        // 删首 Chunk

  while nextCtid != INVALID:
    ctid = nextCtid
    bufDesc = BufMgr::Read(ctid.GetPageId(), LW_EXCLUSIVE)
    nextCtid = GetNextChunk(bufDesc, ctid.GetOffset())  // 先读再删！
    DeleteDiskTuple(delContext, ctid.GetOffset())
```

**必须先读 nextCtid 再删**：删除会修改 Tuple 的 liveMode，如果先删再读，下一个 Chunk 的指针可能已被回收。

### 11.6 UPDATE：三种策略

```
UpdateBigTuple 策略决策：
  newChunkNum = ceil(newDataSize / maxChunkDataSize)
  oldChunkNum = firstChunk.GetNumChunks()

  Case A: newChunkNum >= oldChunkNum
    → UpdateBigTupSizeBigger
    复用前 oldChunkNum 个 Chunk 槽，末尾追加新增 Chunk

  Case B: newChunkNum < oldChunkNum
    → UpdateBigTupSizeSmaller
    删除多余旧 Chunk，复用保留的 Chunk 覆盖新数据

  Case C: 新 Tuple ≤ MaxDefaultTupleSpace（变成普通 Tuple）
    → 删除所有后续 Chunk，首 Chunk 变回普通 Tuple
```

### 11.7 BigTuple 与各模块的关系

| 模块 | BigTuple 处理方式 |
|------|-----------------|
| **TD** | 每个 Chunk 独立分配 TD 槽，首 Chunk 是主 TD |
| **Undo** | 每个 Chunk 独立写 Undo 记录，链式维护 |
| **WAL** | 每个 Chunk 独立生成 WAL；链接变化由 `WAL_HEAP_UPDATE_NEXT_CTID` 单独记录 |
| **回滚** | `RollbackBigTuple()` 链式遍历所有 Chunk 逐一回滚 |
| **MVCC** | 每个 Chunk 独立做可见性判断，再 AssembleTuples 重组 |
| **FSM** | 首 Chunk 选页和普通 Tuple 相同；后续 Chunk 可能分布在不同页面 |

---

## 12. FSM：空闲空间管理

### 12.1 FSM 解决的问题

```
INSERT 需要找一个"有足够空间"的 Heap 页面：

朴素方案：从头线性扫描所有页 → O(N)，不可接受
FSM 方案：多层树结构，O(log N) 定位满足条件的页面
```

### 12.2 空间等级（9 级）

```
listId  可用空间范围（BLCKSZ=8192B）
  0     0B（满页，无空间）
  1     (0,   64B]
  2     (64,  128B]
  3     (128, 256B]
  4     (256, 512B]
  5     (512, 1024B]
  6     (1024, 2048B]
  7     (2048, 4096B]
  8     (4096, 8192B]（几乎空页）
```

FSM 不精确记录字节数，只记录等级（9 个等级够用），减少 FSM 更新频率。

### 12.3 FsmNode 与 FsmPage

```cpp
struct FsmNode {
    PageId page;    // 指向下一层 FSM 页或数据页
    uint16 listId;  // 当前所在的 free list 等级
    uint16 prev;    // 链表前驱
    uint16 next;    // 链表后继
};

// 每个 FsmPage 内部：
//   FSM_FREE_LIST_COUNT(9) 个双向链表，每个链表挂属于该等级的 FsmNode
//   HWM 管理 slot 使用量
//   SearchSeed[] 数组记录上次搜索位置（避免热点）
```

### 12.4 FSM 树结构（最多 5 层）

```
FreeSpaceMapMetaPage
  ├─ numFsmLevels         // 树总层数
  ├─ mapCount[level]      // 各层 FSM 页数
  ├─ currMap[level]       // 各层最右侧 FSM 页
  └─ numTotalPages / numUsedPages

Level-2 FSM 页（每页 ~670 个 FsmNode）
  └─ Level-1 FSM 页（~670 个 FsmNode）
      └─ 叶子节点 → 数据页（Heap/Index）

2 层树：可管理 670*670 ≈ 45 万数据页
3 层树：可管理 670^3 ≈ 3 亿数据页
动态扩展：AdjustFsmTree() 在需要时自动增加层级
```

### 12.5 查找可用页：GetPage()

```
PartitionFreeSpaceMap::GetPage(spaceNeeded)

  targetListId = GetListId(spaceNeeded)  // 需要哪个等级的页

  从 Meta 开始，逐层向下：
    SearchPageIdOfChild(fsmPage, spaceNeeded)
      从 targetListId 对应的链表开始搜索 FsmNode
      if 当前 list 为空：
        升级到 targetListId+1（宁可找更大空间的页）
      找到 FsmNode → 进入下一层 FSM 页
      重试 retryTime 次失败：
        return INVALID + needExtensionTask=true

  到叶子层 → 返回数据页 PageId
```

### 12.6 更新 FSM：UpdateSpace()

```
INSERT/UPDATE/DELETE 完成后：
PartitionFreeSpaceMap::UpdateSpace(pageId, newFreeSpace)

  1. newListId = GetListId(newFreeSpace)
  2. 找到该页在叶子 FSM 页中的 FsmNode
  3. if listId 变化：
       MoveNode(node, oldList → newList)
  4. 向上传播：
       if 父节点所在 list 也需更新 → 递归更新父 FSM 页
  5. 关键更改写 WAL（FSM 修改也受 WAL 保护）
```

### 12.7 FSM 与 HeapPage.fsmIndex 的关系

```cpp
// HeapPageHeader 中：
FsmIndex fsmIndex;  // 该页在 Level-0 FSM 页中的 FsmNode 索引

// 作用：快速找到该页在 FSM 树中对应的 FsmNode，
//       无需从 Meta 重新搜索，UpdateSpace 直接定位
```

### 12.8 页面扩展

```
GetPage() 找不到满足空间的页 → needExtensionTask=true
  → 后台任务 / 当前线程触发扩展：
      AllocateNewPage() → 新页 listId=8（全空）加入 FSM
      AdjustFsmTree()   → 必要时增加 FSM 树层数
  → 再次 GetPage()
```

---

## 13. 完整写入时序图（INSERT）

```
用户执行 INSERT INTO t VALUES(...)
│
├─ 1. 获取可用页面
│      PartitionFreeSpaceMap::GetPage(tupleSize)
│        → 遍历 FSM 树找有足够空间的 pageId
│      BufMgr::Read(pageId, LW_EXCLUSIVE)
│        → 命中 Buffer 或从磁盘读入
│
├─ 2. 分配 TD 槽
│      HeapPage::AllocTd(context)
│        → DoAllocTd：扫描空闲 → TryReuse → Extend
│        → 返回 tdId
│
├─ 3. 写 Undo
│      构建 UndoRecord（UNDO_HEAP_INSERT, dataSize=0）
│      UndoZone::InsertUndoRecord(record)
│        → 写 Undo 页 + WAL_UNDO_RECORD + WAL_UNDO_UPDATE_SLOT_UNDO_PTR
│        → 返回 undoRecPtr
│
├─ 4. 更新页面 TD
│      HeapPage::SetTd(tdId, xid, undoRecPtr, cid)
│        → TD[tdId] = {xid, undoRecPtr, IS_INVALID, IN_PROGRESS}
│
├─ 5. 写行数据
│      HeapPage::AddTuple(diskTuple)
│        → 分配 ItemId（从 m_lower 增长）
│        → 写 Tuple（从 m_upper 缩减）
│        → diskTuple.m_tdId=tdId, m_tdStatus=ATTACH_TD_AS_NEW_OWNER
│
├─ 6. 生成 WAL
│      AtomicWalWriterContext::BeginAtomicWal()
│      PutNewWalRecord(WAL_HEAP_INSERT) + Append(data)
│      EndAtomicWal() → WalGroupLsnInfo
│      WaitTargetPlsnPersist() → 等 WAL 落盘（同步 commit 时）
│
├─ 7. MarkDirty
│      BufMgr::MarkDirty(bufDesc)
│        → bufDesc.state |= BUF_CONTENT_DIRTY
│        → BgDiskPageWriter::PushDirtyPageQueue(bufDesc)
│        → recoveryPlsn 记录（Checkpoint 用）
│
└─ 8. 更新 FSM
       PartitionFreeSpaceMap::UpdateSpace(pageId, newFreeSpace)
         → 更新 FsmNode listId
         → 向上传播
         → 写 WAL（FSM WAL）
```

---

## 14. 与前序模块的连接点

### 14.1 与 Day 2（Page 结构 + Tuple 格式）

| Day2 概念 | Day7 中的体现 |
|----------|--------------|
| Page 三级地址（FileId+BlockNum+Offset） | ItemPointerData = ctid，BigTuple 用 ctid 串联各 Chunk |
| m_lower / m_upper 夹心结构 | HeapPage AddTuple 时 m_lower 增长（ItemId），m_upper 减少（Tuple数据）|
| TD 槽位默认 4 个，最大 128 | Day7 详述 AllocTd 全流程（扩展/回收/复用）|
| TupleTdStatus 三态 | Day7 是核心内容，与 GetVisibleTuple 路径直接绑定 |
| IS_PREV_XID_CSN | Day2 介绍意义，Day7 展示 SetTd 具体实现 |

### 14.2 与 Day 3（Buffer Manager）

| Day3 概念 | Day7 中的体现 |
|----------|--------------|
| BufMgr::Read(LW_EXCLUSIVE) | INSERT/UPDATE/DELETE 都先 Read 获取独占锁 |
| MarkDirty | 写操作最后一步，触发 BgDiskPageWriter 异步刷盘 |
| CR 页 | ConstructCrTuple 的输出就是 CR 页内容（Heap 是 CR 页的生产者）|
| recoveryPlsn | 每个 HeapPage 的 WAL LSN，Checkpoint 截断用 |

### 14.3 与 Day 4（Transaction + MVCC）

| Day4 概念 | Day7 中的体现 |
|----------|--------------|
| XidVisibleToSnapshot | GetVisibleTuple 的最终判断依据 |
| recycleMinCsn | TryReuseTdSlots 决定 TD 是否可 Reset 的阈值 |
| CidVisibleToSnapshot | 同事务内命令可见性（SELECT 能否看见同事务前一 INSERT）|
| TransactionSlot.curTailUndoPtr | InsertUndoRecord 前读取，设置 SetTxnPreUndoPtr |

### 14.4 与 Day 5（WAL）

| Day5 概念 | Day7 中的体现 |
|----------|--------------|
| 5 步写入工作流 | HeapPage 写操作内部调用 BeginAtomicWal/PutRecord/EndAtomicWal |
| WAL_HEAP_* 类型 | INSERT/UPDATE/DELETE/PRUNE/COMPACT 各有对应 WAL 类型 |
| WAL-First | GenerateWAL 必须在 MarkDirty 前 |

### 14.5 与 Day 6（Undo）

| Day6 概念 | Day7 中的体现 |
|----------|--------------|
| InsertUndoRecord | Heap 写操作调用此 API 写 Undo |
| UndoRecord.m_tdPreInfo | SetPreTdInfo 捕获 TD 当前状态 |
| ConstructCrTuple | Day6 介绍机制，Day7 展示 Heap 层调用入口 |
| RollbackUndoZone | DELETE/UPDATE 回滚时恢复 Tuple 数据 |

---

## 15. 快速查阅表

### 关键常数

| 常数 | 值 | 含义 |
|------|-----|------|
| `MIN_TD_COUNT` | 2 | 页面最小 TD 槽数 |
| `DEFAULT_TD_COUNT` | 4 | 页面初始 TD 槽数 |
| `MAX_TD_COUNT` | 128 | 页面最大 TD 槽数 |
| `EXTEND_TD_NUM` | 2 | 每次扩展 TD 槽数 |
| `EXTEND_TD_MIN_NUM` | 1 | 最少扩展槽数 |
| `HEAP_PAGE_HEADER_SIZE` | 88B | HeapPageHeader 大小 |
| `MaxDefaultTupleSpace()` | ~7892B | BigTuple 触发阈值 |
| `LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE` | 12B | BigTuple Chunk 额外头 |
| `FSM_FREE_LIST_COUNT` | 9 | FSM 空间等级数 |
| `HEAP_MAX_MAP_LEVEL` | 5 | FSM 最大树层数 |

### 核心 API 速查

| 操作 | API | 文件 |
|------|-----|------|
| 查找可用页 | `PartitionFreeSpaceMap::GetPage(spaceNeeded)` | `dstore_fsm_page.h` |
| 分配 TD | `HeapPage::AllocTd(context)` | `dstore_heap_page.h` |
| 设置 TD | `DataPage::SetTd(tdId, xid, ptr, cid)` | `dstore_data_page.h` |
| 写行数据 | `HeapPage::AddTuple(diskTuple)` | `dstore_heap_page.h` |
| 删行 | `HeapPage::DelTuple(offset)` | `dstore_heap_page.h` |
| 读可见行 | `HeapPage::GetVisibleTuple(...)` | `dstore_heap_page.h` |
| 构造历史版本 | `HeapPage::ConstructCrTuple(...)` | `dstore_heap_page.h` |
| 回滚行修改 | `HeapPage::UndoHeap(record)` | `dstore_heap_page.h` |
| 更新 FSM | `PartitionFreeSpaceMap::UpdateSpace(pageId, space)` | `dstore_fsm_page.h` |

### 核心文件速查

| 功能 | 文件 |
|------|------|
| TD 结构 + TdCsnStatus + TDAllocContext | `include/page/dstore_td.h` |
| ItemId 四态 | `include/page/dstore_itemid.h` |
| HeapPage + HeapPageHeader | `include/page/dstore_heap_page.h` |
| DataPage 基础类（AllocTd/SetTd/RefreshTupleTdStatus）| `include/page/dstore_data_page.h` |
| HeapDiskTuple | `include/tuple/dstore_heap_tuple.h` |
| TupleTdStatus | `include/tuple/dstore_data_tuple.h` |
| FSM（FsmNode/FsmPage/FreeSpaceMapMetaPage）| `include/page/dstore_fsm_page.h` |
| AllocTd/DoAllocTd/TryReuse/Extend 实现 | `src/page/dstore_data_page.cpp` |
| GetVisibleTuple/ConstructCrTuple 实现 | `src/page/dstore_heap_page.cpp` |
| BigTuple INSERT | `src/heap/dstore_heap_insert.cpp` |
| BigTuple SCAN | `src/heap/dstore_heap_scan.cpp` |
| BigTuple DELETE | `src/heap/dstore_heap_delete.cpp` |

### 三态对照速查

```
TupleTdStatus 三态        TdCsnStatus 三态
─────────────────────    ──────────────────────
ATTACH_TD_AS_NEW_OWNER   IS_INVALID    （未提交）
ATTACH_TD_AS_HISTORY_OWNER IS_PREV_XID_CSN（前事务 CSN）
DETACH_TD                IS_CUR_XID_CSN（当前事务已提交）

TDStatus 三态
──────────────────────────────
UNOCCUPY_AND_PRUNEABLE  （空闲）
OCCUPY_TRX_IN_PROGRESS  （惰性，可能已提交）
OCCUPY_TRX_END          （经 TryReuse 验证已完成）
```

---

## 下一步

Day 8 深入 Index / BTree：
- BTree 页面结构（BtrPage vs HeapPage 的异同）
- BTree 插入 / 分裂（SMO：Structure Modification Operation）
- BTree 扫描与可见性判断（索引层 vs 堆层可见性分离）
- BTree Undo（UNDO_BTREE_INSERT / UNDO_BTREE_DELETE）
- BTree WAL 记录体系
