# DStore Transaction 模块培训材料

## 简介

本培训材料面向数据库内核新手，详细讲解 DStore 中 Transaction 模块的核心概念、数据结构和实现流程。

---

## 第一部分：核心概念基础

### 1.1 XID：事务的唯一标识

XID 定义在 `include/transaction/dstore_transaction_types.h`（第 51-81 行）：

```cpp
union Xid {
    uint64 m_placeHolder;
    struct {
        uint64 m_zoneId : 20;       // 20-bit 区域标识（Undo Zone）
        uint64 m_logicSlotId : 44;  // 44-bit 逻辑槽位标识
    };
};
```

**编码设计解读**：
- **20-bit zoneId**：标识事务所在的 Undo Zone，每个 Zone 独立管理一组事务槽位
- **44-bit logicSlotId**：在该 Zone 内的逻辑槽位编号，可寻址 2^44 个事务槽位
- 使用 union 的 m_placeHolder 可以原子地更新整个 XID（64-bit）

### 1.2 CSN：提交序列号（Commit Sequence Number）

CSN 是 MVCC 机制的核心，在 `interface/transaction/dstore_transaction_struct.h` 中定义：

```cpp
const CommitSeqNo INVALID_CSN = 0;
const CommitSeqNo COMMITSEQNO_FIRST_NORMAL = 0x1;
const CommitSeqNo MAX_COMMITSEQNO = ~INVALID_CSN;
```

**CSN 的三大作用**：
1. **MVCC 快照**：为每个读事务分配一个 CSN，作为其"快照时刻"
2. **版本可见性**：`xid.csn < snapshot.csn` → 该事务在快照前提交 → 可见
3. **垃圾回收**：追踪最小活跃事务的 CSN，决定哪些历史版本可被清理

### 1.3 事务隔离级别

`include/transaction/dstore_transaction_types.h`（第 38-43 行）：

```cpp
enum class TrxIsolationType {
    XACT_READ_UNCOMMITTED,      // 未实现
    XACT_READ_COMMITTED,        // 提交读（默认）
    XACT_TRANSACTION_SNAPSHOT,  // 快照隔离
    XACT_SERIALIZABLE           // 未实现
};
```

| 隔离级别 | 快照策略 | 特点 |
|---------|---------|------|
| READ_COMMITTED | 每条 SQL 获取新快照 | 可能幻读，性能好 |
| TRANSACTION_SNAPSHOT | 整个事务共用一个快照 | 可重复读，一致性强 |

---

## 第二部分：关键数据结构

### 2.1 TransactionSlot（事务槽）

`include/undo/dstore_transaction_slot.h`（第 57-130 行）：

```cpp
struct TransactionSlot {
    uint64 curTailUndoPtr;   // 当前 Undo 链尾指针
    uint64 spaceTailUndoPtr; // 空间 Undo 尾指针
    CommitSeqNo csn;         // 事务的 CSN（提交后才有效）
    TrxSlotStatus status;    // 事务状态
    uint64 logicSlotId;      // 逻辑槽位 ID
    uint64 commitEndPlsn;    // 提交 WAL 位置
    WalId walId;             // WAL ID
};
```

**槽位状态枚举**：
```cpp
enum TrxSlotStatus : uint8 {
    TXN_STATUS_FROZEN,           // 冻结（极早的已提交，所有快照可见）
    TXN_STATUS_IN_PROGRESS,      // 执行中
    TXN_STATUS_PENDING_COMMIT,   // 两阶段提交中间态
    TXN_STATUS_COMMITTED,        // 已提交（CSN 有效）
    TXN_STATUS_ABORTED,          // 已回滚
};
```

### 2.2 Snapshot（快照）

`interface/transaction/dstore_transaction_struct.h`（第 104-147 行）：

```cpp
struct SnapshotData {
    SnapshotType snapshotType;   // SNAPSHOT_MVCC / SNAPSHOT_NOW / SNAPSHOT_DIRTY
    CommitSeqNo snapshotCsn;     // 快照的 CSN（MVCC 模式下有效）
    CommandId currentCid;        // 当前命令 ID
};
```

### 2.3 TransAbortStage（可重入中止阶段枚举）

支持中止过程在失败后重新进入，确保资源一定被清理：
```
AbortNotStart → PreAbortDone → RecordAbortDone → PostAbortDone
→ CleanUpResourceDone → DecreasePdbTransCountDone → DstoreAbortCompleted
```

---

## 第三部分：事务生命周期

### 3.1 StartInternal()：事务启动

`src/transaction/dstore_transaction.cpp`（第 246-285 行）：

**核心步骤**：
1. `InitInfoForNewTrx()`：重置所有字段，state = TRANS_DEFAULT
2. 递增虚拟事务计数器，触发 TRX_EVENT_START 回调
3. 清除线程本地 CSN：`SetThrdLocalCsn(INVALID_CSN)`
4. state → TRANS_INPROGRESS

> 注意：此时尚未分配 XID，XID 在第一次写操作时才通过 AllocTransactionSlot() 分配

### 3.2 SetSnapshotCsn()：建立快照

`src/transaction/dstore_transaction.cpp`（第 1513-1577 行）：

```
SetThrdLocalCsn(MAX_COMMITSEQNO)  ← 临时占位，阻止 GC
         ↓
m_csnMgr->GetNextCsn(csn)         ← 获取当前全局 CSN
         ↓
SetThrdLocalCsn(min(csn, cursorMinCsn))  ← 更新线程 MIN_CSN
         ↓
m_snapshot.SetCsn(csn)
```

**原子性模拟**：三步操作防止垃圾回收线程在"获取 CSN"和"更新 MIN_CSN"之间清理版本。

### 3.3 CommitInternal()：事务提交

`src/transaction/dstore_transaction.cpp`（第 287-336 行）：

#### 两阶段提交（CommitTransactionSlot）：

```
第一阶段：uzone->Commit<TXN_STATUS_PENDING_COMMIT>(xid, csn)
           ↓
    其他事务读到 PENDING_COMMIT → WaitForTransactionEnd()
           ↓
第二阶段：uzone->Commit<TXN_STATUS_COMMITTED>(xid, csn)
           ↓
    释放事务锁，其他等待线程被唤醒
```

**为何两阶段**：第一阶段确保 WAL 已持久化，第二阶段使 CSN 对其他事务可见。

### 3.4 AbortInternal()：多阶段可重入中止

`src/transaction/dstore_transaction.cpp`（第 338-411 行）：

```cpp
switch (m_currTransState.abortStage) {
    case AbortNotStart:    PreAbort(); /* fall through */
    case PreAbortDone:     RecordAbort(); /* fall through */
    case RecordAbortDone:  PostAbort(); /* fall through */
    case PostAbortDone:    CleanupResource(); /* fall through */
    ...
}
```

**设计意义**：任何阶段失败后，系统修复问题并重新调用 AbortInternal()，从上次中断点继续，确保资源一定被清理。

---

## 第四部分：MVCC 可见性判断

### 4.1 XidVisibleToSnapshot() 决策树

`src/transaction/dstore_transaction.cpp`（第 1310-1376 行）：

```
XidVisibleToSnapshot(xid, snapshot)
  │
  ├─ IsFrozen()           → TRUE  （极早事务，总是可见）
  ├─ IsAborted()          → FALSE （回滚事务，永不可见）
  ├─ SNAPSHOT_DIRTY       → TRUE  （脏读快照）
  ├─ !NeedWaitPendingTxn  → FALSE （无需等待时返回不可见）
  ├─ IsCommitted()        → xid.csn < snapshot.csn
  └─ IsPendingCommit()    → WAIT() → xid.csn < snapshot.csn
```

### 4.2 XidStatus：延迟初始化的状态查询

`include/transaction/dstore_transaction.h`（第 508-608 行）：

```cpp
struct XidStatus {
    inline bool IsFrozen()    { InitIfNeeded(); return status == TXN_STATUS_FROZEN; }
    inline bool IsCommitted() { InitIfNeeded(); return status == TXN_STATUS_COMMITTED; }
    inline bool IsPendingCommit() {
        InitIfNeeded();
        if (status == TXN_STATUS_PENDING_COMMIT) {
            isInitialized = false;  // 强制下次重新查询
        }
        return status == TXN_STATUS_PENDING_COMMIT;
    }
};
```

**关键特征**：
- **延迟初始化**：第一次调用 getter 才查询槽位
- **PENDING_COMMIT 重查**：因为状态会变化，需强制重新查询

---

## 第五部分：关键代码位置速查

| 功能 | 文件 | 行号 |
|------|------|------|
| XID 定义 | dstore_transaction_types.h | 51-81 |
| 隔离级别 | dstore_transaction_types.h | 38-43 |
| TransactionSlot | dstore_transaction_slot.h | 57-130 |
| 事务启动 | dstore_transaction.cpp | 246-285 |
| 快照建立 | dstore_transaction.cpp | 1513-1577 |
| 提交流程 | dstore_transaction.cpp | 287-336 |
| 两阶段提交 | dstore_transaction_mgr.cpp | 476-527 |
| 回滚流程 | dstore_transaction.cpp | 338-411 |
| MVCC 可见性 | dstore_transaction.cpp | 1310-1376 |
| XidStatus | dstore_transaction.h | 508-608 |
