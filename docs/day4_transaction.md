# Day 4 — Transaction + MVCC 深度解析

> 覆盖文件：`dstore_transaction_types.h`、`dstore_transaction_struct.h`、`dstore_transaction.h`、`dstore_transaction_slot.h`、`dstore_csn_mgr.h`、`01_transaction.md`

---

## 第一部分：MVCC 总体架构

DStore 使用 **CSN（提交序列号）驱动的 MVCC**，与 PostgreSQL 的 xmin/xmax 方案不同：

```
读事务获取快照 CSN = S
写事务提交时获取 CSN = C

可见性规则：C < S → 该版本对读事务可见
```

核心组件关系：

```
CsnMgr（全局）
  │  分配单调递增的 CSN
  │
  ├── Transaction（每线程）
  │     │  持有 SnapshotData（snapshotCsn = S）
  │     │  持有当前 XID（zoneId + logicSlotId）
  │     │
  │     └── TransactionSlot（Undo Zone 中持久化）
  │               持有 csn = C（提交后写入）
  │               持有 status（IN_PROGRESS/COMMITTED/ABORTED/...）
  │
  └── XidStatus（查询辅助结构）
        延迟初始化，按需查询 TransactionSlot
```

---

## 第二部分：XID — 事务的唯一地址

定义：`include/transaction/dstore_transaction_types.h`（第 51-81 行）

```cpp
union Xid {
    uint64 m_placeHolder;   // 整体原子操作入口
    struct {
        uint64 m_zoneId     : 20;  // Undo Zone ID（最多 2^20 个 Zone）
        uint64 m_logicSlotId: 44;  // Zone 内逻辑槽位（最多 2^44 个事务/Zone）
    };
} PACKED;

const Xid INVALID_XID = Xid(-1);  // 全 1 表示无效
```

**设计解读**：

| 字段 | 位宽 | 含义 |
|------|------|------|
| `m_zoneId` | 20 bit | 指向具体的 Undo Zone，每个 Zone 独立管理一批事务槽位 |
| `m_logicSlotId` | 44 bit | Zone 内的逻辑槽位编号，单调递增，不循环 |

XID 既是**逻辑标识**（唯一区分事务）也是**物理地址**（直接定位 Undo 中的 TransactionSlot）：

```
XID → UndoZone[zoneId] → TransactionSlot[logicSlotId]
```

使用 `union` 的关键原因：`m_placeHolder` 支持对整个 64-bit XID 进行原子操作（CAS）。

---

## 第三部分：CSN — MVCC 的时钟

定义：`interface/transaction/dstore_transaction_struct.h`（第 50-56 行）

```cpp
const CommitSeqNo INVALID_CSN           = 0;
const CommitSeqNo COMMITSEQNO_FIRST_NORMAL = 0x1;
const CommitSeqNo MAX_COMMITSEQNO       = ~INVALID_CSN;  // 全 1（最大值）
```

**CSN 的三大作用**：

| 作用 | 说明 |
|------|------|
| 快照时刻 | 读事务建立快照时，把当前全局 CSN 存为 `snapshotCsn` |
| 版本过滤 | 判断 `xid.csn < snapshot.snapshotCsn`，决定版本可见性 |
| 垃圾回收 | 追踪所有活跃事务的最小 CSN，低于此 CSN 的旧版本可被清理 |

### CSN 单调性保证

`CsnMgr::GetNextCsn()` 使用原子递增（`m_nextCsn`）保证全局唯一单调，无需全局锁：

```cpp
// 伪代码
CommitSeqNo GetNextCsn() {
    return GsAtomicFetchAdd(&m_nextCsn, m_csnAssignmentIncrement);
}
```

---

## 第四部分：TransactionSlot — 事务槽位

定义：`include/undo/dstore_transaction_slot.h`（第 57-130 行）

```cpp
struct TransactionSlot {
    uint64        curTailUndoPtr;   // 当前 Undo 链尾（每次插入 Undo 记录后更新）
    uint64        spaceTailUndoPtr; // Undo 空间尾（申请 Undo 空间时更新）
    CommitSeqNo   csn;              // 提交 CSN（仅 COMMITTED 后有意义）
    TrxSlotStatus status    : 8;    // 事务状态（见下方枚举）
    uint64        reserve   : 8;    // 保留
    uint64        logicSlotId : 48; // 槽位逻辑 ID（用于校验）
    uint64        commitEndPlsn;    // 提交 WAL 的 PLSN（用于 WAL 可见性判断）
    WalId         walId;            // 提交所在 WAL 流
} PACKED;
```

### TrxSlotStatus 枚举

```cpp
enum TrxSlotStatus : uint8 {
    TXN_STATUS_UNKNOWN        = 0,
    TXN_STATUS_FROZEN,          // 极早已提交，所有快照可见，无需 CSN 比较
    TXN_STATUS_IN_PROGRESS,     // 执行中
    TXN_STATUS_PENDING_COMMIT,  // 两阶段提交中间态（第一阶段完成）
    TXN_STATUS_COMMITTED,       // 已提交，CSN 有效
    TXN_STATUS_ABORTED,         // 已回滚
    TXN_STATUS_FAILED,          // 回滚进行中出现错误
    TXN_STATUS_PREPARED         // XA 预备事务
};
```

**关键：TransactionSlot 持久化在 Undo Zone 中**，重启后可通过 XID 重新定位，支持崩溃恢复。

---

## 第五部分：SnapshotData — 快照结构

定义：`interface/transaction/dstore_transaction_struct.h`（第 104-149 行）

```cpp
struct SnapshotData {
    SnapshotType snapshotType;  // SNAPSHOT_MVCC / SNAPSHOT_NOW / SNAPSHOT_DIRTY
    CommitSeqNo  snapshotCsn;   // MVCC 快照的 CSN（S）
    CommandId    currentCid;    // 当前命令 ID（用于同事务内命令级可见性）

    inline CommitSeqNo GetCsn() const {
        // SNAPSHOT_NOW 不受 CSN 限制，返回 MAX → 看到所有已提交版本
        return snapshotType == SnapshotType::SNAPSHOT_MVCC ? snapshotCsn : MAX_COMMITSEQNO;
    }
};
using Snapshot = SnapshotData *;
```

### 三种快照类型

| 类型 | 含义 | 使用场景 |
|------|------|---------|
| `SNAPSHOT_MVCC` | 快照隔离，只见 `csn < snapshotCsn` 的已提交版本 | 普通 SQL 查询 |
| `SNAPSHOT_NOW`  | 看到此刻所有已提交事务（包括并发提交的） | 系统内部操作 |
| `SNAPSHOT_DIRTY`| 脏读，包含进行中事务的修改 | 特殊诊断用途 |

### CommandId 的作用

同一事务内多条 SQL 依次执行：
- 每条 SQL 前调用 `IncreaseCommandCounter()` 递增 `currentCid`
- Tuple 写入时记录 `m_tdId`（其中含 CID）
- 可见性判断：`tuple.cid < snapshot.currentCid` → 当前 SQL 之前的命令已写入，对本 SQL 可见

---

## 第六部分：Transaction 类结构

定义：`include/transaction/dstore_transaction.h`（第 67-502 行）

### 关键成员变量

```cpp
class Transaction : public BaseObject {
private:
    PdbId               m_pdbId;             // 所属可插拔数据库
    StorageInstance    *m_instance;
    ThreadContext      *m_thrd;              // 线程上下文（含线程本地 CSN）
    CsnMgr             *m_csnMgr;            // CSN 管理器引用
    TransactionStateData m_currTransState;   // 当前状态（xid, state, blockState, abortStage）
    SnapshotData        m_snapshot;          // 本事务快照
    ZoneId              m_zid;               // Undo Zone ID
    TrxIsolationType    m_isolationLevel;    // 隔离级别
    CommandId           m_currentCommandId;  // 当前命令编号
    UndoRecPtr          m_lastUndoPtr;       // Undo 链尾缓存
    // ...
};
```

### TransactionStateData

```cpp
struct TransactionStateData {
    Xid            xid;          // 当前 XID（启动时无效，第一次写操作时分配）
    TransState     state;        // 低级状态
    TBlockState    blockState;   // 高级状态（SQL 层视角）
    bool           readOnly;
    bool           holdXactLock;
    TransAbortStage abortStage;  // 中止阶段（用于可重入中止）
};
```

### TransState（低级状态）

```
TRANS_DEFAULT → TRANS_START → TRANS_INPROGRESS → TRANS_COMMIT
                                               ↘ TRANS_ABORT
                                                    ↓
                                               TRANS_DEFAULT
```

### TBlockState（高级状态，SQL 层）

```
TBLOCK_DEFAULT → TBLOCK_STARTED（单条 SQL 自动提交）
              → TBLOCK_BEGIN → TBLOCK_INPROGRESS → TBLOCK_END（COMMIT）
                                                 ↘ TBLOCK_ABORT_PENDING（ROLLBACK）
                                                      ↓
                                               TBLOCK_DEFAULT
```

---

## 第七部分：事务生命周期

### 7.1 StartInternal() — 事务启动

`src/transaction/dstore_transaction.cpp`（第 246-285 行）

```
StartInternal()
  ├─ InitInfoForNewTrx()          重置 xid = INVALID_XID, state = TRANS_DEFAULT
  ├─ 递增虚拟事务计数器
  ├─ FireCallback(TRX_EVENT_START) 通知各模块
  ├─ SetThrdLocalCsn(INVALID_CSN)  清空线程本地 CSN
  └─ state → TRANS_INPROGRESS
```

**关键**：此时 XID 仍是 `INVALID_XID`，**不分配 Undo 槽位**。
只读事务全程不需要 XID。

### 7.2 AllocTransactionSlot() — 延迟分配 XID

第一次修改操作时触发：

```
AllocTransactionSlot()
  ├─ UndoZone[m_zid]->AllocSlot()   从 Undo Zone 分配槽位
  ├─ xid = Xid(m_zid, logicSlotId)  构造 XID
  ├─ 在 TransactionSlot 写入 TXN_STATUS_IN_PROGRESS
  └─ LockMgr->AcquireXactLock(xid)  持有事务锁，阻止其他事务读到未完成提交
```

### 7.3 SetSnapshotCsn() — 快照建立

`src/transaction/dstore_transaction.cpp`（第 1513-1577 行）

**三步原子模拟**（防止 GC 在建立快照中途清理版本）：

```
步骤 1: SetThrdLocalCsn(MAX_COMMITSEQNO)
         ↑ 临时声明"我的 MIN_CSN = MAX"，GC 不会清理任何版本

步骤 2: m_csnMgr->GetNextCsn(csn)
         ↑ 从全局原子 m_nextCsn 获取当前 CSN 值（不递增）

步骤 3: SetThrdLocalCsn(min(csn, cursorMinCsn))
         ↑ 更新线程 MIN_CSN 为实际值，允许 GC 清理快照 CSN 之前的版本

步骤 4: m_snapshot.SetCsn(csn)
         ↑ 快照 CSN 确定
```

**为何是三步**：如果直接两步（获取CSN → 设置MIN_CSN），GC 可能在这两步之间清理了刚才快照所需的历史版本。

### 7.4 CommitInternal() — 两阶段提交

`src/transaction/dstore_transaction.cpp`（第 287-336 行）

```
CommitInternal()
  │
  ├─ PreCommit()
  │   ├─ WAL 刷盘（确保 WAL 已持久化）
  │   └─ 记录 commitEndPlsn 到 TransactionSlot
  │
  ├─ RecordCommit()
  │   ├─ 第一阶段: uzone->Commit<TXN_STATUS_PENDING_COMMIT>(xid, csn)
  │   │   ↑ 其他事务读到 PENDING_COMMIT → WaitForTransactionEnd() 等待
  │   │
  │   ├─ m_csnMgr->GetNextCsn(csn, advance=true)  获取并递增全局 CSN
  │   │
  │   └─ 第二阶段: uzone->Commit<TXN_STATUS_COMMITTED>(xid, csn)
  │       ↑ CSN 写入 TransactionSlot，事务对外可见
  │
  └─ PostCommit()
      ├─ LockMgr->ReleaseXactLock(xid)  释放事务锁，唤醒等待线程
      └─ FireCallback(TRX_EVENT_POST_COMMIT)
```

**两阶段意义**：

| 阶段 | 目的 |
|------|------|
| PENDING_COMMIT | 确保 WAL 已落盘，其他读者等待（不看到中间状态） |
| COMMITTED | CSN 原子写入，对其他事务可见 |

### 7.5 AbortInternal() — 多阶段可重入中止

`src/transaction/dstore_transaction.cpp`（第 338-411 行）

```cpp
switch (m_currTransState.abortStage) {
    case AbortNotStart:           PreAbort();          /* fall through */
    case PreAbortDone:            RecordAbort();       /* fall through */
    case RecordAbortDone:         PostAbort();         /* fall through */
    case PostAbortDone:           CleanupResource();   /* fall through */
    case CleanUpResourceDone:     DecreasePdbCount();  /* fall through */
    case DecreasePdbTransCountDone: /* complete */
}
```

每个阶段完成后立即持久化 `abortStage`，崩溃重启后可从断点继续：

```
PreAbort          → 回滚 Undo 链（数据恢复到事务前状态）
RecordAbort       → 在 TransactionSlot 写 TXN_STATUS_ABORTED
PostAbort         → 触发 TRX_EVENT_POST_ABORT 回调
CleanupResource   → 释放内存、锁等资源
DecreasePdbCount  → 递减 PDB 活跃事务计数
```

---

## 第八部分：MVCC 可见性判断

### 8.1 XidVisibleToSnapshot() 决策树

`src/transaction/dstore_transaction.cpp`（第 1310-1376 行）

```
XidVisibleToSnapshot(xid, snapshot)
  │
  ├─ xid == currentXid → TRUE（当前事务自己的修改可见）
  │
  ├─ IsFrozen()        → TRUE（极早事务，全局可见）
  │
  ├─ IsAborted()       → FALSE（已回滚，永不可见）
  │
  ├─ snapshot.type == SNAPSHOT_DIRTY → TRUE（脏读）
  │
  ├─ IsInProgress() && !NeedWaitPendingTxn → FALSE（进行中不等待）
  │
  ├─ IsCommitted()     → xid.csn < snapshot.snapshotCsn
  │                       TRUE = 提交在快照之前 → 可见
  │                       FALSE = 提交在快照之后 → 不可见
  │
  └─ IsPendingCommit() → WaitForTransactionEnd(xid)
                          等待提交完成 → 重新判断 csn < snapshotCsn
```

### 8.2 CSN 可见性快捷路径

当 CSN 已直接可知（不需要查 TransactionSlot）时使用：

```cpp
// Transaction 成员方法（inline，零开销）
inline bool XidVisibleToSnapshot(CommitSeqNo csn) const {
    StorageAssert(csn != INVALID_CSN);
    return csn < m_snapshot.GetCsn();
}

// 全局函数
inline bool XidVisibleToSnapshot(Snapshot snapshot, CommitSeqNo csn) {
    return csn < snapshot->GetCsn();
}
```

### 8.3 CID 可见性（命令级）

```cpp
inline bool CidVisibleToSnapshot(CommandId cid) const {
    // cid == snapshotCid 时不可见（当前命令的修改对本命令不可见）
    return cid < m_snapshot.GetCid();
}
```

同一事务内，`INSERT` → `SELECT` 可见（CID 递增），但 `INSERT` 本身执行期间不可见自己插入的行。

---

## 第九部分：XidStatus — 延迟查询辅助

定义：`include/transaction/dstore_transaction.h`（第 508-608 行）

```cpp
struct XidStatus : public BaseObject {
    // 公共查询 getter（全部延迟初始化）
    bool IsFrozen();
    bool IsCommitted();
    bool IsAborted();
    bool IsInProgress();
    bool IsPendingCommit();   // 特殊：强制下次重查
    CommitSeqNo GetCsn();

private:
    Xid           xid;
    TrxSlotStatus status;
    CommitSeqNo   csn;
    Transaction  *trx;
    bool          needWaitPendingTxn;
    bool          isInitialized;     // 惰性标志

    void InitIfNeeded() {
        if (!isInitialized) {
            trx->QueryXidStatus(this);  // 查询 Undo Zone 中的 TransactionSlot
            isInitialized = true;
        }
    }
};
```

**PENDING_COMMIT 的特殊处理**：

```cpp
bool IsPendingCommit() {
    InitIfNeeded();
    bool isPendingCommit = (status == TXN_STATUS_PENDING_COMMIT);
    if (isPendingCommit) {
        isInitialized = false;  // 强制下次重新查询 — 状态马上要变
    }
    return isPendingCommit;
}
```

调用方在外层循环等待：
```
while (xidStatus.IsPendingCommit()) {
    WaitForTransactionEnd(xid);  // 等到 COMMITTED 或 ABORTED
}
```

---

## 第十部分：CsnMgr — CSN 管理器

定义：`include/transaction/dstore_csn_mgr.h`（第 45-158 行）

### 关键成员

```cpp
class alignas(DSTORE_CACHELINE_SIZE) CsnMgr {
    gs_atomic_uint64 m_nextCsn;           // 下一个可分配的 CSN（原子递增）
    gs_atomic_uint64 m_maxReservedCsn;    // 预分配上限（批量申请优化）
    std::atomic<CommitSeqNo> m_localCsnMin;       // 本节点最小活跃快照 CSN
    std::atomic<CommitSeqNo> m_localBarrierCsnMin; // 屏障 CSN（跨节点同步）
    std::atomic<CommitSeqNo> m_flashbackCsnMin;    // 闪回最小 CSN
    std::atomic<CommitSeqNo> m_backupRestoreCsnMin;// 备份恢复最小 CSN
};
```

### recycleMinCsn 的计算与传播

`recycleMinCsn`（或 `GetRecycleCsnMin()`）是 Undo 垃圾回收的截止点：

```
recycleMinCsn = min(
    m_localCsnMin,         // 本节点最小活跃事务 CSN
    m_localBarrierCsnMin,  // 集群屏障 CSN（主备同步后的最小可见 CSN）
    m_flashbackCsnMin,     // 闪回保留最小 CSN
    m_backupRestoreCsnMin  // 备份保留最小 CSN
)
```

**传播机制**：

```
每个线程事务建立快照时:
  SetThrdLocalCsn(snapshotCsn)    ← 注册到线程本地

定期调用 UpdateLocalCsnMin():
  遍历所有线程的 localCsn
  → 取最小值 → m_localCsnMin
  → 通告给主备复制/闪回模块

Undo GC 线程:
  GetRecycleCsnMin() → 返回以上各 min 的结果
  → 只清理 csn < recycleMinCsn 的 Undo 版本
```

**为何要线程本地 CSN**：

```
问题：如果只依赖全局 m_nextCsn，无法知道哪些"历史"事务还活跃
解决：每个活跃事务将 snapshotCsn 注册到 m_thrd->localCsn
     UpdateLocalCsnMin() 扫描所有活跃线程，取最小值
     此值以下的 Undo 可安全回收
```

---

## 第十一部分：完整时序图

### 11.1 读事务时序

```
时间轴 →
T1 开始写  T1 提交(CSN=100)  T2 读快照(CSN=105)  T2 读 T1 数据
               ↓                    ↓                    ↓
        TransactionSlot.csn=100   snapshot.csn=105   100 < 105 → 可见
```

### 11.2 并发写/读事务时序（PENDING_COMMIT 等待）

```
T1 写事务:     IN_PROGRESS → PENDING_COMMIT → [WAL落盘] → COMMITTED(csn=200)
                                    ↑                         ↓
T2 读事务:              SetSnapshot(csn=201)         XidVisibleToSnapshot(T1.xid)
                               ↓                              ↓
                        IsPendingCommit() == true     WaitForTransactionEnd(T1)
                               ↓                              ↓
                        等待...                       T1.csn(200) < T2.snapshot.csn(201) → 可见
```

### 11.3 Abort + MVCC 时序

```
T1 写事务:  IN_PROGRESS → [崩溃] → 重启恢复 → AbortInternal() → ABORTED
                                                                   ↓
T2 读事务:              XidVisibleToSnapshot(T1.xid)
                               ↓
                        IsAborted() == TRUE → FALSE（不可见）
```

---

## 第十二部分：隔离级别对快照的影响

```cpp
enum class TrxIsolationType {
    XACT_READ_UNCOMMITTED,      // 未实现
    XACT_READ_COMMITTED,        // 每条 SQL 调用 SetSnapshotCsn()
    XACT_TRANSACTION_SNAPSHOT,  // 事务开始时调用一次 SetSnapshotCsn()
    XACT_SERIALIZABLE           // 未实现
};
```

| 隔离级别 | SetSnapshotCsn() 时机 | 效果 |
|---------|----------------------|------|
| READ_COMMITTED | 每条 SQL 执行前 | 每次读到最新已提交数据，可能幻读 |
| TRANSACTION_SNAPSHOT | BEGIN 时一次 | 整个事务看到同一快照，可重复读 |

---

## 第十三部分：与 TD（Transaction Descriptor）的联系

Day 2 学习的页面 TD 槽位和 TransactionSlot 的关系：

```
HeapPage 中的 TD（in-page 缓存）:
  TD.xid      → 最近修改该页面的事务 XID
  TD.csn      → 若事务已提交，缓存其 CSN（避免每次查 Undo Zone）
  TD.status   → TdCsnStatus（INVALID/PREV_XID/CUR_XID）

TransactionSlot（in-Undo 持久化）:
  真正的事务状态（COMMITTED/ABORTED 等）
  真正的 CSN

MVCC 读取流程:
  1. 读 HeapTuple.m_info.tdStatus（ATTACH_NEW / ATTACH_HISTORY / DETACH）
  2. 定位 TD[tdId]（in-page 快速路径）
  3. TD.csn 有效？ → 直接比较，跳过 Undo 查询
  4. TD.csn 无效？ → 通过 xid 定位 TransactionSlot → 获取真正 csn/status
  5. 需要历史版本？ → 构造 CR 页（见 Day 3）
```

---

## 第十四部分：关键文件速查

| 功能 | 文件路径 | 关键行号 |
|------|---------|---------|
| XID 定义 | `include/transaction/dstore_transaction_types.h` | 51-81 |
| TransState 枚举 | `include/transaction/dstore_transaction_types.h` | 87-93 |
| TransAbortStage 枚举 | `include/transaction/dstore_transaction_types.h` | 95-108 |
| TransactionStateData | `include/transaction/dstore_transaction_types.h` | 110-119 |
| SnapshotData / SnapshotType | `interface/transaction/dstore_transaction_struct.h` | 60-149 |
| CSN 常量（INVALID/MAX） | `interface/transaction/dstore_transaction_struct.h` | 50-56 |
| TrxEvent 枚举 | `interface/transaction/dstore_transaction_struct.h` | 35-48 |
| TBlockState 枚举 | `interface/transaction/dstore_transaction_struct.h` | 158-171 |
| TransactionSlot 结构 | `include/undo/dstore_transaction_slot.h` | 57-130 |
| TrxSlotStatus 枚举 | `include/undo/dstore_transaction_slot.h` | 44-55 |
| Transaction 类声明 | `include/transaction/dstore_transaction.h` | 67-502 |
| XidStatus 结构 | `include/transaction/dstore_transaction.h` | 508-608 |
| CsnMgr 类 | `include/transaction/dstore_csn_mgr.h` | 45-158 |
| StartInternal() | `src/transaction/dstore_transaction.cpp` | 246-285 |
| SetSnapshotCsn() | `src/transaction/dstore_transaction.cpp` | 1513-1577 |
| CommitInternal() | `src/transaction/dstore_transaction.cpp` | 287-336 |
| AbortInternal() | `src/transaction/dstore_transaction.cpp` | 338-411 |
| XidVisibleToSnapshot() | `src/transaction/dstore_transaction.cpp` | 1310-1376 |

---

## Day 5 预告 — WAL + Checkpoint

下一步将深入 WAL（Write-Ahead Logging）和 Checkpoint 机制：

- WAL 流（WalStream）的写入流程和 PLSN/GLSN
- `WaitTargetPlsnPersist()` 如何实现 WAL-First 保证（Day 3 的底层支撑）
- Checkpoint 如何截断 WAL、建立恢复点
- 崩溃恢复如何使用 Checkpoint + WAL 重放
