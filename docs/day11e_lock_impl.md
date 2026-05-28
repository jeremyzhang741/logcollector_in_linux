# Lock 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 HeapLockTuple 行锁、LockEntry 两队列结构、EarlyDeadLockPreventor 同步死锁检测、DeadlockDetector N 线程环路检测、锁释放与事务生命周期绑定的具体实现细节。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_lock_entry.cpp` | 1121 | LockEntry：授权队列、等待队列、冲突判断 |
| `dstore_lock_interface.cpp` | 819 | HeapLockTuple、LockRelation、事务锁清理入口 |
| `dstore_deadlock_detector.cpp` | 1438 | DeadlockDetector：等待图构建、环路检测、选择 victim |
| `dstore_lock_thrd_local.cpp` | 1150 | 线程本地锁状态、2PL 锁列表管理 |
| `dstore_table_lock_mgr.cpp` | 624 | 表级锁管理（弱锁/强锁协议） |

---

## 一、LockEntry 结构：双队列

```
LockEntry（对应一个锁对象，如一行数据）：
  ┌────────────────────────────────────────┐
  │ grantedQueue: SkipList                 │  ← 已获得锁的持有者（有序）
  │   元素: GrantedLockInfo{xid, lockMode} │    O(log n) 插入/查找/删除
  │                                        │
  │ waitingQueue: dlist（FIFO）            │  ← 等待锁的线程
  │   元素: WaitingLockInfo{xid, lockMode, │    FIFO 保证公平性
  │           condVar, isNotified}         │
  │                                        │
  │ lockTag: {pdbId, tableId, ctid}        │  ← 锁的标识
  │ grantedModes: bitmask                  │  ← 当前所有授权模式的并集
  └────────────────────────────────────────┘
```

**为什么 grantedQueue 用 SkipList**：支持"快速查找某个 xid 的当前锁模式"（用于锁升级判断），O(log n) 优于链表 O(n)。

---

## 二、加锁流程（LockInterface::Lock）

```
Lock(lockTag, lockMode, xid):
  ① 查找 LockEntry（哈希表 + 分桶锁 LWLock）
  ② 检查 grantedQueue 中是否已有当前事务的同级或更强锁 → 可重入返回
  ③ ConflictCheck: 是否与 grantedModes 冲突？
     ├─ 无冲突 → AddToGrantedQueue(xid, lockMode) → 返回
     └─ 有冲突 → TryAddToWaiterQueue(xid, lockMode)
         ├─ EarlyDeadLockPreventor → 同步处理 2 线程死锁
         └─ 等待（condVar.wait）
```

### 2.1 ConflictCheck 矩阵

```
lockModes = {SHARE, EXCLUSIVE, INTENT_SHARE, INTENT_EXCLUSIVE, ...}
conflictMatrix[mode1][mode2] = true/false

// 示例：
SHARE    × SHARE     = 无冲突  → 多读者兼容
SHARE    × EXCLUSIVE = 冲突    → 读写互斥
EXCLUSIVE × EXCLUSIVE = 冲突   → 写写互斥
```

---

## 三、EarlyDeadLockPreventor：同步 2 线程死锁检测

在 `TryAddToWaiterQueue` 时**同步**运行，处理最常见的 2 线程互等场景：

```
TryAddToWaiterQueue(xid, lockMode):
    // Step 1: 检查 waitingQueue 中是否有线程在等待当前事务持有的锁
    for each waiter in waitingQueue:
        if waiter.xid 等待的某锁 被 curXid 持有:
            → 发现 2 线程死锁！

    // Step 2: 处理策略
    if 对方事务比当前事务"老"（startTime 更早）:
        // 让当前事务等（较新的事务等较老的）
        AddToWaiterQueue(curXid)
        return LOCK_WAIT
    
    elif 对方事务比当前事务"新":
        // 抢占：把对方从 waitingQueue 中移到队列前面
        // 当前事务直接获得锁
        GrantLockEarly(curXid)
        RequeueWaiter(otherXid)
        return LOCK_GRANTED
    
    // 若构成环路（2→1→2）：选较新事务为 victim
    → return LOCK_ERROR_DEADLOCK（较新事务立即中止）
```

**同步运行的优势**：2 线程死锁（最常见的死锁形式）无需后台检测器，直接在加锁时解决，延迟极低。

---

## 四、DeadlockDetector：N 线程环路检测

处理 3+ 线程的复杂死锁（如 A→B→C→A），由后台线程或超时触发：

### 4.1 等待图构建（BuildWaitForGraph）

```
RunDeadlockDetect():
  ① CollectLockWaiters():
      for each LockEntry in lockTable:
          for each waiter in waitingQueue:
              edges = waiter.xid → granted xids（等待锁持有者）
              AddEdge(waiterVertex, grantedVertices)
  
  ② FindCycles(graph):
      ScanVerticesNotInCycle()   ← 剪枝：排除明确无环的节点（拓扑排序）
      DFS on remaining nodes
      if cycle detected: record cycle
```

### 4.2 选择 Victim（ChooseVictimAndNotify）

```
victim = cycle 中 startTime 最新（最年轻）的事务
// "最年轻"定义：m_startTimestamp 最大的事务
// 原理：年轻事务做的工作更少，回滚代价小

NotifyVictim(victim):
    victim.thread.Interrupt(LOCK_INTERRUPT_DEADLOCK)
    // victim 线程从 condVar.wait 返回，得到 LOCK_ERROR_DEADLOCK 状态
    // victim 事务开始 Abort 流程
```

### 4.3 RecheckCycle：二次确认

```
// 死锁图构建到选出 victim 之间，某些等待可能已超时/已被解除
RecheckCycle(victim):
    LockEntry.lock()           // 加锁重新确认
    IsStillWaiting(victim)?
    IsStillInCycle(cycle)?     // 重构图验证
    if 不再成环: skip（死锁已自然消解）
    else: NotifyVictim(victim)
```

---

## 五、锁释放与事务生命周期

### 5.1 事务锁追踪（LockResource / 2PL List）

```
事务加锁时：
  TransactionMgr::AddLock(lockTag, lockMode, subLockId)
  → 记录到 transaction->m_lockResource[]（哈希表）
  → SubLockResourceID = m_subLockCount++（用于 Savepoint 部分释放）

提交/中止时：
  ReleaseLocks():
    for each lock in m_lockResource:
        LockEntry.RemoveFromGrantedQueue(xid, lockMode)
        AdvanceWaitingQueue(LockEntry)  ← 唤醒下一个等待者
    m_lockResource.Clear()
```

### 5.2 Session 锁 vs 事务锁

```
事务锁：记录在 transaction->m_lockResource
  → 事务结束时释放（提交/中止）

Session 锁：记录在 session->m_sessionLocks
  → 连接断开时释放（跨事务存活）
  → 典型：LOCK TABLE ... IN ... MODE（显式表锁）
```

### 5.3 AdvanceWaitingQueue：唤醒等待者

```
AdvanceWaitingQueue(LockEntry):
    updated = false
    for each waiter in waitingQueue (FIFO):
        if !ConflictCheck(waiter.lockMode, grantedModes):
            // 无冲突 → 授予锁
            RemoveFromWaiting(waiter)
            AddToGranted(waiter)
            grantedModes |= waiter.lockMode
            waiter.condVar.notify_one()  ← 唤醒等待线程
            updated = true
        elif updated:
            break  // FIFO 原则：前面有人等待则停止
```

---

## 六、HeapLockTuple：行锁与表锁协调

### 6.1 行锁加锁步骤

```
HeapLockTuple(ctid, lockMode):
  // Step 1: 表级意向锁（Intention Lock）
  TableLockMgr::Lock(table, IS_LOCK 或 IX_LOCK)
  
  // Step 2: 行级锁
  lockTag = {pdbId, tableId, ctid}
  LockMgr::Lock(lockTag, lockMode)
  
  // Step 3: 页面 TD 标记（同 DoLock，见 Heap 精读）
  page->SetLockerTdId(tdId)
  page->SetLockerXid(xid)
```

### 6.2 行锁与 TD 的关系

- **TD lockerXid** = 轻量行锁标记（无 LockMgr 记录，仅页面 TD 字段）
- **LockMgr 行锁** = 重量级行锁（SELECT FOR UPDATE/SHARE 等显式加锁）

大多数 DML 只设 TD lockerXid，不调 LockMgr（轻量路径）。`SELECT FOR UPDATE` 同时调 LockMgr 保证锁等待语义。

---

## 七、表级锁：弱锁/强锁协议

### 7.1 弱锁 vs 强锁

```
弱锁（DML）：IS、IX（Intention Share/Exclusive）
  → 多个 DML 操作兼容（可并发）
  → 通过 LazyLock 快速路径（见 Transaction 精读）

强锁（DDL）：S、X（Share/Exclusive）
  → 与弱锁冲突（DDL 需要独占）
  → ALTER TABLE、DROP TABLE 等使用强锁
```

### 7.2 LazyLock 机制（table_lock_mgr.cpp）

```cpp
LazyLock(table, weakMode):
    if (!HasStrongLock(table)):
        // 快速路径：仅在本地记录，不调 LockMgr
        m_localWeakLocks.Add(table, weakMode)
        return;
    
    // 有强锁竞争：走完整锁流程
    LockMgr::Lock(table, weakMode)
```

`HasStrongLock(table)`：原子读取每个表的强锁计数器，O(1)。只有 DDL 操作时才升到非零值。

---

## 八、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| LockEntry 队列 | "等待队列" | 两个队列：grantedQueue（SkipList，O(log n)）+ waitingQueue（FIFO dlist）|
| 死锁检测 | "后台检测" | 两级：EarlyDeadLockPreventor（同步，2线程）+ DeadlockDetector（异步，N线程）|
| Victim 选择 | "选一个回滚" | startTime 最新（最年轻）的事务为 victim，回滚代价最小 |
| 2 线程死锁 | "最常见情况" | 同步内联检测：加锁时发现互等立即解决，无需后台介入 |
| 行锁与 TD | "TD 记录锁" | 轻量路径：只设 TD lockerXid；重量路径：SELECT FOR UPDATE 同时调 LockMgr |
| Session 锁 | "持久锁" | 跨事务存活，存在 session->m_sessionLocks，连接断开时释放 |
| LazyLock | "DML 弱锁" | HasStrongLock O(1) 原子判断；仅本地记录，不调 LockMgr |
| AdvanceWaitingQueue | "唤醒等待者" | FIFO 顺序唤醒，遇到仍有冲突的等待者则停止 |
| RecheckCycle | "防误判" | 死锁图到通知 victim 期间的竞态窗口：通知前二次确认等待仍成立 |
| Savepoint 部分释放 | "回滚到存档点" | SubLockResourceID 标记锁的归属层级，线性扫描释放指定层之后的锁 |
