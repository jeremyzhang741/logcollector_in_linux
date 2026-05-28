# Transaction 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 Start/Commit/Abort 完整链路、快照采集、CSN 传播、锁资源管理的具体实现细节。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_transaction.cpp` | 2048 | 事务生命周期：Start/Commit/Abort/Savepoint |
| `dstore_transaction_mgr.cpp` | 993 | 多线程事务管理：注册/注销/等待/CSN最小值 |
| `dstore_transaction_interface.cpp` | 875 | 对外接口封装、DML 弱锁（LazyLock）|
| `dstore_resowner.cpp` | 1317 | 资源跟踪：Buffer Pin、Lock、文件句柄 |

---

## 一、事务启动（StartInternal）

### 1.1 XID 分配是懒惰的

```
StartInternal():
  分配 m_memoryCtx          ← 事务专属内存上下文
  分配 m_abortMemoryCtx     ← 16KB 固定大小，专用于 Abort 路径（防 OOM）
  Init snapshot             ← 读取当前 CSN（见 §三）
  注册 ResOwner
  // XID 此时不分配！
```

**XID 只在第一次写操作（`AllocTransactionSlot`）时分配**。只读事务全程没有 XID，永远不会接触 TransactionSlot 页面。

### 1.2 两个内存上下文

- `m_memoryCtx`：正常执行分配，事务提交/中止时整体销毁
- `m_abortMemoryCtx`：16KB 固定大小，Abort 路径专用

为什么需要独立的 `m_abortMemoryCtx`？

> Abort 路径中可能分配内存（如 Undo 回滚的临时缓冲）。若此时系统内存已耗尽，`m_memoryCtx` 的分配会失败，导致回滚无法完成。预留一块固定内存保证 Abort 路径永远可执行。

---

## 二、两阶段提交（CommitInternal）

### 2.1 提交时序

```
CommitInternal():
  Step 1: CleanupResources     ← 归还 Buffer Pin、释放文件句柄
  Step 2: CommitTransactionSlot(PENDING_COMMIT)
          → CSN 分配（GetNextCsn，isCommit=false）
          → slot.status = TXN_STATUS_PENDING_COMMIT
          → m_pendingCommitTrxCnt++
          → WAL(WAL_TXN_COMMIT with PENDING status)
          → EndAtomicWal → walGroupPtr
          → UnlockSlot          ← 先解锁（COMMIT_LOGIC_TAG，同 Undo 模块）
  Step 3: WaitTargetPlsnPersist(walGroupPtr)  ← 等 WAL 落盘
  Step 4: CommitTransactionSlot(COMMITTED)
          → slot.status = TXN_STATUS_COMMITTED
          → m_pendingCommitTrxCnt--
          → CsnMgr::WaitUpperBoundSatisfy(csn)  ← 等 CSN 上界
  Step 5: ReleaseLocks          ← 释放所有事务锁
  Step 6: DestroyMemoryCtx
```

### 2.2 PENDING_COMMIT 状态的作用

其他事务读到 `TXN_STATUS_PENDING_COMMIT` 时：

```cpp
if (slot.status == PENDING_COMMIT && slot.pendingCsn >= snapshotCsn) {
    return invisible;  // 快速路径：CSN 比快照新，不可见
}
// 否则：等待事务结束
XactLockMgr::Wait(xid);
```

即使快照 CSN 比 pendingCsn 新（提交先于快照），也需要等 WAL 落盘后才能确认可见性。

---

## 三、快照采集与 CSN 传播

### 3.1 SetThrdLocalCsn(MAX) 原子窗口

```cpp
// 获取快照前：
thrd->SetThrdLocalCsn(MAX_COMMITSEQNO);  // 设最大值作为"采样中"信号
GS_MEMORY_BARRIER();
snapshotCsn = csnMgr->GetCurrentCsn();   // 读当前全局 CSN
thrd->SetThrdLocalCsn(snapshotCsn);      // 设为实际快照 CSN
```

`GetLocalCsnMin()` 在计算 recycleMinCsn 时会**跳过值为 MAX 的线程**（正在采样中），避免读到无效状态。

### 3.2 recycleMinCsn 的后台计算

```
UpdateCsnMinThreadLoop（后台线程）:
  loop:
    localMin = MAX_COMMITSEQNO
    for each thread in ThreadCoreMgr:
        csnMin = thread->GetThrdLocalCsn()
        if csnMin == MAX: skip（采样中）
        localMin = min(localMin, csnMin)
    CAS advance m_localCsnMin to localMin
    sleep(interval)
```

最终 `recycleMinCsn = min(localCsnMin, barrierCsnMin, flashbackCsnMin)`（同 day10_integration.md 记录）。

---

## 四、事务中止（AbortInternal）

### 4.1 可恢复状态机

```cpp
// TransAbortStage 枚举：
TRANS_ABORT_STAGE_NONE
TRANS_ABORT_STAGE_CLEANUP_RESOURCES
TRANS_ABORT_STAGE_ROLLBACK_UNDO
TRANS_ABORT_STAGE_RELEASE_LOCKS
TRANS_ABORT_STAGE_ABORT_TXN_SLOT
TRANS_ABORT_STAGE_DESTROY_CTX
```

```cpp
AbortInternal():
  switch (m_abortStage) {  // fall-through：从上次中断处继续
  case NONE:
      m_abortStage = CLEANUP_RESOURCES;
  case CLEANUP_RESOURCES:
      CleanupResources();
      m_abortStage = ROLLBACK_UNDO;
  case ROLLBACK_UNDO:
      UndoZone::RollbackUndoRecords(xid, ...);  // 同步回滚
      m_abortStage = RELEASE_LOCKS;
  case RELEASE_LOCKS:
      ReleaseLocks();
      m_abortStage = ABORT_TXN_SLOT;
  case ABORT_TXN_SLOT:
      RollbackTxnSlot(xid);  // status = ABORTED + 伪CSN
      m_abortStage = DESTROY_CTX;
  case DESTROY_CTX:
      DestroyMemoryCtx();
  }
```

崩溃重启后，从持久化的 `m_abortStage` 继续，无需从头重做已完成的步骤。

---

## 五、可见性判断（XidVisibleToSnapshot）

### 5.1 核心判断逻辑

```cpp
XidVisibleToSnapshot(xid, snapshot):
  slot = CopySlot(xid)   // 读槽（含 WAL 未落盘时降级为 PENDING_COMMIT）

  if slot.status == FROZEN:  return visible
  if slot.csn < snapshot.csn:
      if slot.status == COMMITTED:  return visible
      if slot.status == ABORTED:    return invisible
      if slot.status == PENDING_COMMIT:
          if slot.pendingCsn >= snapshot.csn: return invisible  // 快速路径
          WaitForTxnEnd(xid)          // 等待事务结束再重判
          return XidVisibleToSnapshot(xid, snapshot)  // 递归重判

  return invisible  // csn >= snapshot.csn
```

### 5.2 LazyLock：DML 弱锁快速路径

```cpp
// DML 操作（INSERT/UPDATE/DELETE）加的是"弱锁"：
if (!HasConflictWithStrongLock(table)) {
    LazyLock(table, weakMode);   // 仅记录在 lockResource，不调 LockMgr
    return;                      // 无竞争：O(1) 路径
}
// 有强锁（DDL）竞争时才真正加锁：
LockMgr::Lock(table, weakMode);  // 走完整的锁等待流程
```

大多数 OLTP 操作没有 DDL 并发，LazyLock 完全在事务本地状态中记录，性能接近无锁。

---

## 六、TransactionMgr：多线程事务管理

### 6.1 线程注册与注销

```
RegisterTransaction(xid):
  SpinLock protect
  m_activeTrxList[xid.zoneId] dlist_push_head

UnregisterTransaction(xid):
  SpinLock protect
  dlist_remove from m_activeTrxList[xid.zoneId]
  notify_all waiters on m_activeTrxCv[xid.zoneId]
```

### 6.2 WaitForTransactionEnd

```cpp
WaitForTransactionEnd(xid):
  cv.wait_until([=] {
      slot = CopySlot(xid);
      return slot.status != IN_PROGRESS && slot.status != PENDING_COMMIT;
  }, timeout);
```

若事务超时仍未结束，记录日志并返回（调用方负责处理超时）。

---

## 七、ResOwner：资源跟踪

### 7.1 事务级资源生命周期

ResOwner 是一个栈式资源跟踪器，通过链表链接（父→子→孙），支持 Savepoint：

```
Transaction ResOwner
  └── Savepoint ResOwner
        └── Portal ResOwner
```

提交时：从叶到根合并资源（子资源上交给父）。
中止时：从叶到根逐层释放资源。

### 7.2 追踪的资源类型

| 资源类型 | 追踪时机 | 释放时机 |
|---------|---------|---------|
| Buffer Pin | `bufMgr->Pin()` 后 | `ResOwnerForgetBuffer` |
| 事务锁 | `LockMgr::Lock()` 后 | `ReleaseLocks()` |
| 文件句柄 | `open()` 后 | `CleanupResources()` |
| Undo Buffer | `InsertUndoRecord` 后 | 事务提交/中止 |

### 7.3 SubLockResourceID 与 Savepoint

```cpp
// Savepoint 时：SubLockResourceID = m_subLockCount++
// 事务锁记录中携带 subLockResourceID
// Rollback to Savepoint 时：释放 subLockResourceID > savepointId 的所有锁
```

这样只需线性扫描 lockResource 列表即可实现部分锁释放。

---

## 八、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| XID 分配时机 | "事务开始时分配" | 实际是第一次写操作时懒分配，只读事务无 XID |
| 两阶段提交 | "PENDING→COMMITTED" | m_pendingCommitTrxCnt 引用计数；WaitUpperBoundSatisfy 同步 CSN 上界 |
| Abort 可恢复 | "崩溃恢复" | TransAbortStage 状态机，switch-fallthrough 从断点续跑 |
| recycleMinCsn | "各模块协作" | 后台线程扫所有线程 csnMin；MAX 值跳过（表示采样中）|
| 快照 CSN 采集 | "读 GetCurrentCsn" | SetThrdLocalCsn(MAX) 原子窗口防止 recycleMinCsn 误读 |
| DML 加锁 | "调 LockMgr" | LazyLock 快速路径：无 DDL 竞争时仅本地记录，不调 LockMgr |
| 内存管理 | "事务内存上下文" | 专用 16KB abortMemoryCtx 防止 Abort 路径 OOM |
| Savepoint | "子事务" | SubLockResourceID 标记锁的归属，Rollback 线性扫描释放 |
