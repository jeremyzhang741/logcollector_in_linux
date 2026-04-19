# Checkpoint 机制

## 一、Checkpoint 解决什么问题

数据库运行时，数据页的修改只写到内存（Buffer Pool）和 WAL，并不立刻落盘。崩溃恢复时必须从某个起点开始重放 WAL。如果没有 Checkpoint，每次崩溃都要从头重放所有 WAL，代价无法接受。

```
没有 Checkpoint：
  崩溃恢复 → 重放 全部WAL历史 → 数小时

有 Checkpoint：
  崩溃恢复 → 从最后一个 Checkpoint 的 diskRecoveryPlsn 开始重放
           → 只重放少量 WAL → 分钟级恢复
```

**Checkpoint 做的事**：把当前内存中的脏页刷到磁盘，记录一个"安全点"（diskRecoveryPlsn），WAL 可以在此点之前截断。

---

## 二、核心数据结构

### WalCheckPoint：检查点记录

```cpp
struct WalCheckPoint {
    Timestamp time;
    uint64 diskRecoveryPlsn;     // 脏页已刷盘的最小 WAL 位点
                                 // 恢复时从这里开始重放
    MemoryCheckpoint memoryCheckpoint;  // 内存状态快照（事务表等）
};
```

### CheckpointRequest：触发与计数

```cpp
class CheckpointRequest {
    uint32 m_checkpointStart;   // 请求计数
    uint32 m_checkpointDone;    // 完成计数
    uint32 m_checkpointFail;    // 失败计数
    CheckpointFlag m_checkpointFlag;  // FULL / INCREMENTAL
};
```

### WalCheckpointInfoData：每个 WAL 流的 Checkpoint 状态

```cpp
struct WalCheckpointInfoData {
    WalId walId;
    CheckpointRequest checkpointStreamRequest;
    LWLock checkpointLwLock;           // 同流只允许一个 Checkpoint 并发
    Timestamp lastCheckpointTime;
    uint64 lastCheckPointRecoveryPlsn;
    WalCheckPoint lastCheckPoint;
};
```

---

## 三、触发条件

Checkpoint 由 `CheckpointerMain()` 后台线程驱动，两种触发方式：

### 3.1 时间触发（定期）

```cpp
Timestamp elapsedTime = now - lastCheckpointTime;
if (elapsedTime > checkpointTimeout) {
    // 触发 Checkpoint
}
```

### 3.2 请求触发（按需）

其他模块（如 WAL 文件满、手动触发）调用：
```cpp
walCheckpointInfo->checkpointStreamRequest.RequestCheckpoint(flag);
```
CheckpointerMain 检测到请求标志后立即执行。

---

## 四、Checkpoint 执行流程（CreateCheckpoint）

```
CheckpointMgr::CreateCheckpoint(walId, flags)

  Step 1: 获取 checkpointLwLock（独占）
            同一 WAL 流同时只有一个 Checkpoint 进行

  Step 2: 计算 diskRecoveryPlsn
            bgPageWriter->GetMinRecoveryPlsn()
            → 脏页队列中最小的 PLSN
              （即：所有 PLSN ≤ 此值的脏页已安全落盘）

  Step 3: 与上次 Checkpoint 对比
            if lastCheckpoint.diskRecoveryPlsn >= newPlsn:
                跳过（没有新脏页被刷盘，无需重复记录）

  Step 4: 构建 WalCheckPoint 记录
            checkPoint.diskRecoveryPlsn = newPlsn
            checkPoint.memoryCheckpoint = 当前事务表快照

  Step 5: 写入控制文件（ControlFile）
            持久化 WalCheckPoint，供下次崩溃恢复使用

  Step 6: 释放锁，更新 lastCheckpointTime
```

---

## 五、脏页刷盘机制（BgDiskPageWriter）

Checkpoint 本身**不直接刷脏页**，刷盘由后台的 `BgDiskPageMasterWriter` 持续进行，Checkpoint 只是**读取当前刷盘进度**并记录安全点。

### 5.1 脏页进入队列

```
BufferMgr::MarkDirty(bufDesc)
  ├─ bufDesc->state |= BUF_CONTENT_DIRTY
  └─ bgPageWriterMgr->PushDirtyPageQueue(bufDesc->GetBufferTag())
                       ↑ MPSC 无锁队列（多写者单消费者）
```

### 5.2 BgDiskPageWriter 持续消费

```
BgDiskPageWriter::Run() 后台循环：
  while true:
    entry = DirtyPageQueue.Pop()
    WriteBlock(bufDesc)
      └─ PrepareCheckPageBeforeStartIo()   ← WAL-First 检查
           if page.plsn > flushedWalPlsn:
               WaitTargetPlsnPersist()     ← 等 WAL 先落盘
      └─ pwrite(fd, page, BLCKSZ)          ← 实际写盘
    更新 m_minRecoveryPlsn（已落盘的最小PLSN向前推进）
```

### 5.3 Checkpoint 读取进度

```
bgPageWriter->GetMinRecoveryPlsn()
  → 返回脏页队列中尚未落盘的最老 PLSN
  → diskRecoveryPlsn = 这个值
  → 含义：所有 PLSN < diskRecoveryPlsn 的数据页已安全在磁盘上
```

---

## 六、WAL 截断与 diskRecoveryPlsn

```
WAL 文件时间线：

  ──[旧WAL]──[diskRecoveryPlsn]──[新WAL]──→ PLSN增大方向

  diskRecoveryPlsn 左侧：
    所有脏页已落盘，崩溃恢复不需要这段 WAL
    → 可以安全删除旧 WAL 文件

  diskRecoveryPlsn 右侧：
    可能有对应脏页还在内存中
    → 崩溃恢复必须重放这段 WAL
```

**崩溃恢复起点 = lastCheckPoint.diskRecoveryPlsn**

---

## 七、多 WAL 流下的 Checkpoint

每个 WAL 流（WalId）有独立的 `WalCheckpointInfoData`，独立触发 Checkpoint，互不阻塞。

```
WAL流0：Checkpoint独立进行 → diskRecoveryPlsn_0
WAL流1：Checkpoint独立进行 → diskRecoveryPlsn_1
...
WAL流N：Checkpoint独立进行 → diskRecoveryPlsn_N

系统整体安全点 = min(diskRecoveryPlsn_0, ..., diskRecoveryPlsn_N)
```

failover 后，新 Primary 对旧流做一次 Recovery，完成未完成的 Checkpoint 数据。

---

## 八、Checkpoint 与其他模块的交互

```
┌─────────────────────────────────────────────────────┐
│                   Checkpoint 协作图                  │
│                                                      │
│  MarkDirty()                                         │
│  Buffer ──→ DirtyPageQueue ──→ BgDiskPageWriter      │
│                                      │               │
│                               WAL-First检查          │
│                                      │               │
│                               WAL ←──┘               │
│                               (BgWalWriter刷盘)      │
│                                      │               │
│                               pwrite到磁盘           │
│                                      │               │
│                          GetMinRecoveryPlsn()        │
│                                      ↓               │
│  CheckpointerMain ───────→ CreateCheckpoint()        │
│                                      │               │
│                              写 ControlFile          │
│                              (diskRecoveryPlsn)      │
│                                      │               │
│                              删除旧 WAL 文件          │
└─────────────────────────────────────────────────────┘
```

---

## 九、增量 vs 全量 Checkpoint

| 类型 | diskRecoveryPlsn 取值 | 触发场景 |
|------|----------------------|---------|
| **增量（INCREMENTAL）** | 脏页队列中最小 PLSN | 定期触发，代价小 |
| **全量（FULL）** | WAL 当前插入点 | 主动请求，确保所有脏页落盘 |

全量 Checkpoint 需要等待所有当前脏页落盘后才记录 Checkpoint 点，代价更大，但恢复时几乎无需重放 WAL。

---

## 十、设计要点总结

| 设计选择 | 原因 |
|---------|------|
| Checkpoint 不直接刷脏页 | BgWriter 持续后台刷盘，Checkpoint 只记录进度，避免停顿 |
| diskRecoveryPlsn = 最小PLSN | 保守但正确：未刷盘的最老页对应的WAL一定要保留 |
| WAL-First 强制顺序 | 数据页上盘前WAL必须先上盘，保证WAL永远比数据更完整 |
| 每WAL流独立Checkpoint | 多流并行，不因一个流 Checkpoint 阻塞其他流写入 |
| ControlFile 持久化 | 崩溃后能找到最后一个Checkpoint点，确定恢复起点 |
| 时间+请求双触发 | 定期保证恢复时间上界，按需应对紧急场景 |
