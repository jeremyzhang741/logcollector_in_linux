# DStore Heap 模块培训材料

## 目录
1. [Heap 页面基本结构](#1-heap-页面基本结构)
2. [HeapTuple 结构详解](#2-heaptuple-结构详解)
3. [TD 分配流程](#3-td-分配流程)
4. [SetTd 与 CSN 状态保留](#4-settd-与-csn-状态保留)
5. [Heap 读取路径](#5-heap-读取路径)
6. [Heap 写入路径简述](#6-heap-写入路径简述)

---

## 1. Heap 页面基本结构

### 1.1 页面布局总览

```
┌─────────────────────────────────────────────────────────┐
│  Page Header (PageHeaderData)                            │  64 bytes
├─────────────────────────────────────────────────────────┤
│  Data Page Header (DataPageHeader)                       │
│  - tdCount (uint8)        : TD 槽位数量                 │
│  - headerOffset (uint16)  : 数据区起始位置              │
├─────────────────────────────────────────────────────────┤
│  Heap Page Header (HeapPageHeader)                       │
│  - potentialDelSize       : 可删除行的大小              │
│  - recentDeadTupleMinCsn  : 最近死亡 Tuple 最小 CSN    │
├─────────────────────────────────────────────────────────┤
│  TD 数组 (Transaction Descriptors)                       │
│  └─ TD[0], TD[1], ..., TD[tdCount-1]                   │
├─────────────────────────────────────────────────────────┤
│  ItemId 数组（从 m_lower 向上增长）                      │
│  └─ ItemId[1], ItemId[2], ...                          │
├─────────────────────────────────────────────────────────┤
│                  Free Space                              │
├─────────────────────────────────────────────────────────┤
│  Tuple 数据区（从 m_upper 向下增长）                     │
└─────────────────────────────────────────────────────────┘
```

**关键常数**（`include/page/dstore_td.h` 第 38-45 行）：

| 常数 | 值 | 含义 |
|------|-----|------|
| DEFAULT_TD_COUNT | 4 | 页面初始 TD 槽位数 |
| EXTEND_TD_NUM | 2 | 每次扩展增加数量 |
| MAX_TD_COUNT | 128 | 最多 128 个 TD |
| EXTEND_TD_MIN_NUM | 1 | 最少扩展数量 |

### 1.2 TD（Transaction Descriptor）结构

`include/page/dstore_td.h`（第 164-307 行）：

```cpp
struct TD {
    uint64 m_xid;           // 事务 ID
    CommitSeqNo m_csn;      // 事务提交序号（未提交时为 INVALID_CSN）
    uint64 m_undoRecPtr;    // 第一条 undo 记录指针
    uint64 m_lockerXid;     // 持有锁的事务 ID（死锁检测）
    CommandId m_commandId;  // 事务内命令序号
    uint16 m_status   : 2;  // TDStatus
    uint16 m_csnStatus: 2;  // TdCsnStatus
    uint16 m_pad      : 12;
};
```

### 1.3 TDStatus 枚举

`include/page/dstore_td.h`（第 49-55 行）：

```cpp
enum class TDStatus {
    UNOCCUPY_AND_PRUNEABLE = 0,  // 未被占用，可回收
    OCCUPY_TRX_IN_PROGRESS,       // 被进行中的事务占用
    OCCUPY_TRX_END,               // 被已结束（提交/回滚）的事务占用
};
```

状态转移：
```
初始化 → UNOCCUPY_AND_PRUNEABLE
分配   → OCCUPY_TRX_IN_PROGRESS
提交   → OCCUPY_TRX_END
回收   → UNOCCUPY_AND_PRUNEABLE
```

### 1.4 TdCsnStatus 枚举

`include/page/dstore_td.h`（第 62-66 行）：

```cpp
enum TdCsnStatus : uint8 {
    IS_INVALID = 0,      // CSN 无效（事务未提交）
    IS_PREV_XID_CSN,     // CSN 来自前一个事务（TD 被复用时保留）
    IS_CUR_XID_CSN       // CSN 来自当前事务（正常状态）
};
```

**IS_PREV_XID_CSN 是关键设计**：当 TD 被新事务复用时，旧事务的 CSN 被保留并标记为 PREV，让仍然活跃的快照能识别该 CSN 属于哪个事务。

### 1.5 ItemId：间接指针

ItemId 指向 Tuple 在页面中的位置，主要状态：
- **Normal**：正常指向 Tuple
- **NoStorage**：Tuple 已被压缩，ItemId 中保存 TD 信息
- **Unused**：槽位已释放，可重用

---

## 2. HeapTuple 结构详解

### 2.1 HeapDiskTuple 字段

`include/tuple/dstore_heap_tuple.h`（第 58-99 行）：

```cpp
struct HeapDiskTuple : public DataTuple {
    uint8  m_tdId;        // 关联的 TD 槽位 ID（0-127）
    uint8  m_lockerTdId;  // 持有行锁的事务的 TD ID
    uint16 m_size;        // Tuple 总大小（含头部）
    Xid    m_xid;         // 事务 ID（冗余存储）

    uint32 m_hasNull    : 1;   // 是否有 NULL 值
    uint32 m_hasVarwidth: 1;   // 是否有变长列
    uint32 m_tdStatus   : 2;   // TupleTdStatus（3 种状态）
    uint32 m_liveMode   : 3;   // 行的生命周期模式
    uint32 m_numColumn  : 11;  // 列数

    char m_data[];  // 可变长数据（NULL bitmap + 列数据）
};
```

### 2.2 TupleTdStatus 枚举：最重要的 2 个 bit

`include/tuple/dstore_data_tuple.h`（第 41-45 行）：

```cpp
enum TupleTdStatus : uint8 {
    ATTACH_TD_AS_NEW_OWNER = 0,  // Tuple 当前版本的事务信息在 TD 中
    ATTACH_TD_AS_HISTORY_OWNER,  // Tuple 是历史版本，TD 已被新事务复用
    DETACH_TD,                    // Tuple 与 TD 完全解耦，所有信息在 undo
};
```

**三种状态的详细含义**：

| 状态 | 含义 | 读取可见性时的行为 |
|------|------|-----------------|
| ATTACH_TD_AS_NEW_OWNER | 直接读 TD 中的 xid/csn | 查 TD 判断 XID 可见性 |
| ATTACH_TD_AS_HISTORY_OWNER | TD 已被复用，原事务信息需查 undo | 读 TD.csn（IS_PREV_XID_CSN）或查 undo |
| DETACH_TD | TD 已被完全清空，Tuple 独立 | 无需 TD，直接判断可见 |

**状态转换时机**：
```
INSERT 时：        ATTACH_TD_AS_NEW_OWNER
TD 被复用时：      → ATTACH_TD_AS_HISTORY_OWNER （RefreshTupleTdStatus）
TD 被完全清空时：  → DETACH_TD                  （RefreshTupleTdStatus）
```

---

## 3. TD 分配流程

### 3.1 AllocTd()：重试包装

`src/page/dstore_data_page.cpp`（第 243-267 行）：

```cpp
TdId DataPage::AllocTd(TDAllocContext &context) {
    for (int i = 0; i < 100; ++i) {         // 最多重试 100 次
        tdId = DoAllocTd(context);
        if (tdId != INVALID_TD_SLOT) break;
        GaussUsleep(10L);                    // 等待 10 微秒
    }
    return tdId;
}
```

### 3.2 DoAllocTd()：四步决策树

`src/page/dstore_data_page.cpp`（第 198-241 行）：

```
DoAllocTd()
  │
  ├─ Step 1: 扫描所有 TD
  │   ├─ 当前事务已有 TD → 复用（重入）
  │   └─ 记录第一个 UNOCCUPY 槽位
  │
  ├─ 有空闲 TD → 直接返回该槽位
  │
  ├─ Step 2: TryReuseTdSlots()
  │   └─ 尝试回收已提交/冻结事务的 TD
  │       └─ 成功 → 返回回收后的槽位
  │
  ├─ Step 4: ExtendTd()
  │   └─ 物理扩展 TD 数组（最多到 128）
  │       └─ 成功 → 返回新槽位
  │
  └─ 失败 → INVALID_TD_SLOT（上层 100 次重试）
```

### 3.3 TryReuseTdSlots()：三步回收流程

`src/page/dstore_data_page.cpp`（第 343-441 行）：

#### 子步骤1：TryReuseOneTdSlot() - 分类单个 TD

`src/page/dstore_data_page.cpp`（第 269-341 行）：

```cpp
switch (xs.GetStatus()) {
    case TXN_STATUS_FROZEN:
    case TXN_STATUS_COMMITTED:
        td->FillCsn();  // 从 undo 系统填充 CSN
        if (td->GetCsn() < context.recycleMinCsn) {
            return TD_RECYCLE_UNUSED;  // 可完全清空
        } else {
            return TD_RECYCLE_REUSE;   // 复用但保留 CSN
        }

    case TXN_STATUS_IN_PROGRESS:
        context.canResetTd = false;    // 有进行中事务，禁止 Reset
        context.AddWaitXid(td->GetXid());
        return TD_IS_IN_PROGRESS;
}
```

**分类结果**：

| 返回值 | 含义 | 可否回收 |
|--------|------|---------|
| TD_RECYCLE_UNUSED | CSN < recycleMinCsn，可完全清空 | 是，且可 Reset |
| TD_RECYCLE_REUSE | CSN ≥ recycleMinCsn，保留 CSN | 是，不可 Reset |
| TD_IS_IN_PROGRESS | 事务进行中 | 否 |

#### 子步骤2：RefreshTupleTdStatus() - 批量更新 Tuple 状态

`src/page/dstore_data_page.cpp`（第 451-504 行）：

遍历页面所有 Tuple，对指向被回收 TD 的 Tuple 更新其 `m_tdStatus`：

```cpp
if (slot.unused) {
    // TD 被完全清空
    SetTupleTdStatus(offset, DETACH_TD);
} else {
    // TD 被复用但保留 CSN
    SetTupleTdStatus(offset, ATTACH_TD_AS_HISTORY_OWNER);
}
```

**状态转换示意**：
```
Tuple A（tdId=5, tdStatus=ATTACH_TD_AS_NEW_OWNER）
         ↓  TD5 被判定为可回收
         ↓
若 slot.unused=true  → Tuple A 变为 DETACH_TD
若 slot.unused=false → Tuple A 变为 ATTACH_TD_AS_HISTORY_OWNER
```

#### 子步骤3：GetAvailableTd() - 选择最优槽位

`src/page/dstore_data_page.cpp`（第 506-551 行）：

```cpp
// 优先级1：canResetTd=true 且 slot.unused=true → 完全 Reset
if (canResetTd && slot.unused) {
    td->Reset();            // 清空所有字段
    return slot.id;
}
// 优先级2：选择 CSN 最小（最旧）的已提交 TD
if (!slot.unused) {
    td->SetStatus(OCCUPY_TRX_END);
    if (td->GetCsn() < minCsn) {
        firstCommitTdSlot = slot.id;
        minCsn = td->GetCsn();
    }
}
```

**canResetTd 标志的作用**：
- 若页面上有进行中的事务，`canResetTd = false`
- 这防止了在活跃事务需要通过 `undoRecPtr` 回滚时，TD 被清空

### 3.4 ExtendTd()：物理扩展 TD 数组

`src/page/dstore_data_page.cpp`（第 98-151 行）：

```
扩展前：
[DataHeader][TD0][TD1][TD2][TD3][ItemId1][ItemId2]...[Tuples]

Step 1: memmove_s ItemId 数组向后移动 2×sizeof(TD)
Step 2: 在空出的位置初始化 TD4、TD5（td->Reset()）
Step 3: dataHeader.tdCount += 2；m_lower += 2×sizeof(TD)

扩展后：
[DataHeader][TD0][TD1][TD2][TD3][TD4][TD5][ItemId1][ItemId2]...[Tuples]
```

**限制检查**：
- `tdCount >= MAX_TD_COUNT(128)` → 失败
- `freeSpace < EXTEND_TD_MIN_NUM × sizeof(TD)` → 失败（空间不足）

---

## 4. SetTd 与 CSN 状态保留

### 4.1 SetTd() 函数

`include/page/dstore_data_page.h`（第 158-179 行）：

```cpp
inline void SetTd(uint8 tdId, Xid xid, UndoRecPtr undoPtr, CommandId cid) {
    TD *td = GetTd(tdId);

    // 【关键】当 TD 要被新事务使用时，保留旧事务的 CSN
    if (td->GetXid() != INVALID_XID && td->GetXid() != xid) {
        if (td->TestCsnStatus(IS_CUR_XID_CSN)) {
            // 旧事务的 CSN 转为 IS_PREV_XID_CSN
            td->SetCsnStatus(IS_PREV_XID_CSN);
            // m_csn 值不变，只改标记
        }
        // IS_PREV_XID_CSN 和 IS_INVALID 则保持不变
    }

    // 写入新事务信息
    td->SetXid(xid);
    td->SetUndoRecPtr(undoPtr);
    td->SetStatus(TDStatus::OCCUPY_TRX_IN_PROGRESS);
    td->SetCommandId(cid);
    // 新事务的 csnStatus = IS_INVALID（提交时才改为 IS_CUR_XID_CSN）
}
```

### 4.2 IS_PREV_XID_CSN 的设计价值

**场景**：快照 S1（csn=50）活跃时，Txn1 提交（csn=100），TD 被 Txn2 复用

```
时间线：
S1 启动（csn=50）
  → Txn1 INSERT Tuple A（TD5），提交（csn=100）
  → Txn2 INSERT Tuple B，需要 TD：复用 TD5
      → SetTd(5, Txn2, ...)
          → TD5.csn=100 标记为 IS_PREV_XID_CSN
          → TD5.xid=Txn2
  → S1 读 Tuple A：
      → Tuple A.m_tdStatus = ATTACH_TD_AS_HISTORY_OWNER
      → TD5.csnStatus = IS_PREV_XID_CSN → TD5.csn=100 是前一事务的
      → XidVisibleToSnapshot(100, snapshot_csn=50) → FALSE → 不可见
      → 查 undo 找到 Tuple A 的前版本（INSERT 前，不存在）
      → 返回 NULL（Tuple 不存在）✓ 正确！

若没有 IS_PREV_XID_CSN：
  → 无法区分 csn=100 是 Txn1 还是 Txn2 的
  → MVCC 语义被破坏
```

---

## 5. Heap 读取路径

### 5.1 GetVisibleTuple()：可见性判断入口

`src/page/dstore_heap_page.cpp`（第 160-240 行）：

```cpp
HeapTuple *HeapPage::GetVisibleTuple(pdbId, txn, ctid, snapshot) {

    TdId tdId = GetTupleTdId(offset);
    TD *td = GetTd(tdId);
    bool needUndo = true;

    switch (GetTupleTdStatus(offset)) {

        case DETACH_TD:
            // Tuple 完全独立，直接判断当前版本可见性
            needUndo = false;
            break;

        case ATTACH_TD_AS_HISTORY_OWNER:
            if (snapshot == DIRTY) {
                needUndo = false;         // 脏读：直接读
            } else if (td->TestCsnStatus(IS_INVALID)) {
                needUndo = true;          // CSN 无效，查 undo
            } else if (XidVisibleToSnapshot(snapshot, td->GetCsn())) {
                needUndo = false;         // 前一事务 CSN 可见，不查 undo
            } else {
                needUndo = true;          // CSN 不可见，查 undo 找更早版本
            }
            break;

        case ATTACH_TD_AS_NEW_OWNER:
            xid = td->GetXid();
            if (txn->IsCurrent(xid)) {
                // 当前事务内：用 CID 判断
                needUndo = !CidVisibleToSnapshot(txn, snapshot, td->GetCommandId());
            } else {
                // 其他事务：用 XID/CSN 判断
                needUndo = !XidVisibleToSnapshot(snapshot, xid, txn);
            }
            break;
    }

    if (needUndo) {
        ConstructCrTuple(pdbId, txn, ctid, tdId, &resTuple, snapshot);
    }
    return resTuple;
}
```

**决策树总结**：

```
GetVisibleTuple
  │
  ├─ DETACH_TD
  │   └─ needUndo = false（直接用页面上的 tuple）
  │
  ├─ ATTACH_TD_AS_HISTORY_OWNER
  │   ├─ DIRTY 快照 → needUndo = false
  │   ├─ IS_INVALID → needUndo = true
  │   ├─ CsnVisible → needUndo = false
  │   └─ CsnNotVisible → needUndo = true
  │
  └─ ATTACH_TD_AS_NEW_OWNER
      ├─ 当前事务 → CID 判断
      └─ 其他事务 → XID/CSN 判断
```

### 5.2 ConstructCrTuple()：Undo 链回溯重建历史版本

`src/page/dstore_heap_page.cpp`（第 875-964 行）：

```cpp
RetStatus HeapPage::ConstructCrTuple(pdbId, txn, ctid, tdId, resTuple, snapshot) {

    // 复制页面上的 TD 数组（不修改原页面）
    TD *crTd = DstorePalloc(sizeof(TD) * GetTdCount());
    for (uint8 i = 0; i < GetTdCount(); i++) {
        crTd[i] = *GetTd(i);
    }

    TdId tupleTdId = tdId;
    UndoRecord record;

    for (;;) {  // 循环沿 undo 链回溯

        Xid xid = crTd[tupleTdId].GetXid();
        UndoRecPtr undoPtr = crTd[tupleTdId].GetUndoRecPtr();

        // 无 undo 记录 → 到了最初版本
        if (undoPtr == INVALID_UNDO_RECORD_PTR) {
            txnVisible = true;
            break;
        }

        // 从 undo 系统获取记录，并回退 crTd 到前一版本
        txnMgr->FetchUndoRecordByMatchedCtid(xid, &record, undoPtr, ctid, &csn);
        crTd[tupleTdId].RollbackTdInfo(&record);

        // 判断该版本是否对快照可见
        txnVisible = txn->IsCurrent(xid)
            ? CidVisibleToSnapshot(txn, snapshot, record.GetCid())
            : XidVisibleToSnapshot(snapshot, csn 或 xid);

        if (txnVisible) {
            break;  // 找到对快照可见的版本
        }

        // 从 undo 记录构造前一版本的 tuple
        ConstructCrTupleFromUndo(&record, resTuple);
        // 继续循环...
    }

    DstorePfree(crTd);
    return DSTORE_SUCC;
}
```

**关键点：为什么要复制 TD（crTd）？**

- 页面上的 TD 只保存最新版本信息
- 回溯历史版本时，需要逐步"回滚" TD 状态（通过 `RollbackTdInfo`）
- 必须用临时副本，不能修改原页面 TD

**回溯流程示意**：

```
页面状态：
Tuple A（tdStatus=ATTACH_TD_AS_HISTORY_OWNER, tdId=5）
TD5 = {xid=Txn2, csn=150, undoPtr→U3}

快照 csn=125，Txn2（csn=150）不可见

回溯过程：
[U3] Txn2 的操作 → crTd[5].RollbackTdInfo → crTd[5]={xid=Txn1,csn=100,undoPtr→U1}
     Txn1 visible？100 < 125 → YES！
     ConstructCrTupleFromUndo(U3) → resTuple = Txn1 写入的数据
     break，返回
```

---

## 6. Heap 写入路径简述

### 6.1 INSERT 基本流程

```
1. AllocTd()          → 获取 TD 槽位（tdId）
2. 构建 UndoRecord     → 设置 UNDO_HEAP_INSERT，m_tdPreInfo 指向旧 TD 信息
3. InsertUndoRecord() → 写入 undo 区域，得到 undoRecPtr
4. SetTd()            → TD[tdId] = {xid=curXid, undoRecPtr, IS_INVALID}
5. AddTuple()         → 将 Tuple 写入页面，设置 m_tdId=tdId, m_tdStatus=ATTACH_TD_AS_NEW_OWNER
6. GenerateWAL()      → 写 WAL_HEAP_INSERT 记录
7. MarkDirty()        → 标记页面为脏页
```

### 6.2 UPDATE 三种情况

| 类型 | 触发条件 | WAL 类型 |
|------|---------|---------|
| In-Place Update | 新旧 Tuple 大小相同 | WAL_HEAP_INPLACE_UPDATE |
| Same-Page Update | 新 Tuple 更大，当前页有空间 | WAL_HEAP_SAME_PAGE_APPEND |
| Another-Page Update | 当前页空间不足 | WAL_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD/NEW_PAGE |

### 6.3 DELETE 基本流程

```
1. AllocTd()（若需要新 TD）
2. 构建 UndoRecord     → 设置 UNDO_HEAP_DELETE
3. InsertUndoRecord()
4. SetTd()
5. 修改 Tuple.m_liveMode → TUPLE_BY_NORMAL_DELETE
6. GenerateWAL()      → 写 WAL_HEAP_DELETE 记录
7. MarkDirty()
```

### 6.4 写入操作的整体顺序（WAL-First）

```
分配 TD → 写 Undo → 设置 TD → 修改页面 → 生成 WAL → MarkDirty
                                              ↑
                         WAL 必须在 MarkDirty 前完成（WAL-First 协议）
```

---

## 总结：关键概念速记

| 概念 | 含义 | 核心文件 |
|------|------|---------|
| TD | 页面内事务描述符，存储 xid/csn/undoPtr | dstore_td.h |
| TupleTdStatus | Tuple 与 TD 的 3 种关系状态 | dstore_data_tuple.h |
| ATTACH_TD_AS_NEW_OWNER | 直接读 TD，是最新版本 | - |
| ATTACH_TD_AS_HISTORY_OWNER | TD 被复用，需查 undo | - |
| DETACH_TD | TD 完全清空，Tuple 独立 | - |
| IS_PREV_XID_CSN | TD 保留的前一事务 CSN，支持并发 MVCC | dstore_td.h |
| recycleMinCsn | 低于此 CSN 的 TD 可完全清空 | dstore_td.h |
| canResetTd | 有进行中事务时禁止完全 Reset TD | dstore_data_page.cpp |
| RefreshTupleTdStatus | TD 回收时批量更新 Tuple 的 tdStatus | dstore_data_page.cpp |
| GetVisibleTuple | Heap 读入口，3 分支决定是否查 undo | dstore_heap_page.cpp |
| ConstructCrTuple | Undo 链回溯重建历史版本 | dstore_heap_page.cpp |

## 关键文件速查

| 功能 | 文件 | 行号 |
|------|------|------|
| TD 结构 | include/page/dstore_td.h | 164-307 |
| TdCsnStatus | include/page/dstore_td.h | 62-66 |
| TDStatus | include/page/dstore_td.h | 49-55 |
| TupleTdStatus | include/tuple/dstore_data_tuple.h | 41-45 |
| HeapDiskTuple | include/tuple/dstore_heap_tuple.h | 58-99 |
| SetTd() | include/page/dstore_data_page.h | 158-179 |
| AllocTd() 重试 | src/page/dstore_data_page.cpp | 243-267 |
| DoAllocTd() | src/page/dstore_data_page.cpp | 198-241 |
| TryReuseOneTdSlot() | src/page/dstore_data_page.cpp | 269-341 |
| TryReuseTdSlots() | src/page/dstore_data_page.cpp | 343-441 |
| RefreshTupleTdStatus() | src/page/dstore_data_page.cpp | 451-504 |
| GetAvailableTd() | src/page/dstore_data_page.cpp | 506-551 |
| ExtendTd() | src/page/dstore_data_page.cpp | 98-151 |
| GetVisibleTuple() | src/page/dstore_heap_page.cpp | 160-240 |
| ConstructCrTuple() | src/page/dstore_heap_page.cpp | 875-964 |
