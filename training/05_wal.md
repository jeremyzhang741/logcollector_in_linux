# DStore WAL 模块培训材料

## 第一部分：WAL 基本概念

### 1.1 Write-Ahead Logging 原则

**先写日志，后写数据**：
- 任何数据库修改必须先记录到 WAL 日志
- 系统崩溃后，通过重放 WAL 日志恢复数据库状态
- WAL 是主备数据同步的基础

### 1.2 LSN 两级体系

#### PLSN（Physical Log Sequence Number）
- **含义**：日志在单个 WAL Stream 内的物理字节偏移
- **范围**：每个 Stream 独立计数，从 0 开始
- **用途**：定位 WAL 文件内的具体位置

#### GLSN（Global Log Sequence Number）
- **含义**：全局日志序列号，统一比较来自不同 Stream 的日志顺序
- **规则**：同一页面被不同 WalId 修改时，GLSN 需递增
- **关键用途**：多流恢复时的全局顺序保证、脏页刷新顺序

```cpp
// include/wal/dstore_wal_struct.h:295-316
struct WalRecordLsnInfo {
    WalId walId;     // 所属 WAL 流
    uint64 endPlsn;  // 流内结束位置
    uint64 glsn;     // 全局序号
};
```

### 1.3 WalId：日志流唯一标识

- `using WalId = uint64;`（`include/common/dstore_datatype.h:113`）
- 最多 1024 个流：`MAX_WAL_STREAM_COUNT = 1024`（`include/control/dstore_control_walinfo.h:37`）

---

## 第二部分：WAL 记录结构

### 2.1 WalRecord：最小日志记录

`include/wal/dstore_wal_struct.h`（第 246-270 行）：

```cpp
struct WalRecord {
    uint16 m_size;  // 记录大小（含 header）
    WalType m_type; // 操作类型（INSERT/DELETE/UPDATE 等）
};
// 最小 4 字节，最大 UINT16_MAX - 压缩开销
```

**WalType 覆盖**（80+ 种）：
- `WAL_HEAP_*`：堆表操作
- `WAL_BTREE_*`：BTree 索引操作
- `WAL_UNDO_*`：Undo 日志操作
- `WAL_TXN_*`：事务标记（COMMIT/ABORT）

### 2.2 WalRecordAtomicGroup：原子操作日志组

`include/wal/dstore_wal_struct.h`（第 279-285 行）：

```cpp
struct WalRecordAtomicGroup {
    uint32 groupLen;        // 整个组的总长度
    uint32 crc;             // CRC 校验码
    Xid xid;                // 事务 ID
    uint16 recordNum;       // 组内记录数量
    WalRecord walRecords[]; // 可变长的记录数组
};
```

**特性**：
- 一次 `EndAtomicWal()` 产生一个 WalGroup
- 最多包含 `MAX_PAGES_COUNT_PER_WAL_GROUP = 1030` 个页面的记录

### 2.3 WalRecordForPage：页面日志记录

`include/wal/dstore_wal_struct.h`（第 352-496 行）：

```cpp
struct WalRecordForPage : public WalRecord {
    PageId m_pageId;         // 修改的页面 ID
    WalRecordFlag m_flags;   // GLSN 变化、DDL、文件版本等标志
    WalId m_pagePreWalId;    // 页面上次修改所在的 WAL 流
    uint64 m_pagePrePlsn;    // 页面上次修改的 PLSN
    uint64 m_pagePreGlsn;    // 页面上次修改的 GLSN
    uint64 m_filePreVersion; // 文件版本（追踪文件重建）
};
```

**链式追踪**：通过 `preWalId/prePlsn/preGlsn` 形成历史链，支持增量恢复。

### 2.4 WalGroupLsnInfo：WAL 组三元组

`include/wal/dstore_wal_struct.h`（第 323-327 行）：

```cpp
struct WalGroupLsnInfo {
    WalId m_walId;      // 所属 WAL 流
    uint64 m_startPlsn; // 组起始 PLSN
    uint64 m_endPlsn;   // 组结束 PLSN
};
```

`EndAtomicWal()` 的返回值，用于 `WaitTargetPlsnPersist()` 等待持久化。

---

## 第三部分：WAL 写入流程

### 3.1 AtomicWalWriterContext：事务级写入上下文

`include/wal/dstore_wal_write_context.h`（第 38-214 行）：

```cpp
class AtomicWalWriterContext {
    uint8 *m_buf;                     // WAL 数据缓冲区（初始 4KB）
    uint32 m_bufUsed;                 // 已使用字节数
    WalId m_walId;                    // 绑定的 Stream ID
    WalStream *m_walStream;           // 绑定的 Stream 指针
    BufferDesc *m_pagesNeedWal[1030]; // 涉及修改的页面列表
    uint16 m_numPagesNeedWal;
};
```

### 3.2 完整写入五步工作流

```
┌─ BeginAtomicWal(xid)
│   初始化 WalRecordAtomicGroup，设置 xid
│
├─ RememberPageNeedWal(bufDesc)
│   将修改页面记录到 m_pagesNeedWal
│
├─ 循环：PutNewWalRecord(record) + Append(data, size)
│   ├─ PutNewWalRecord：添加新记录到组，执行压缩
│   └─ Append：向当前记录追加变长数据
│
├─ EndAtomicWal()  →  WalGroupLsnInfo
│   ├─ WalStream::Append(m_buf, m_bufUsed)  ← 写入 Buffer
│   ├─ SetPagesLSN(result)                  ← 为所有页面设置 walId/plsn/glsn
│   └─ ClearState()                         ← 重置上下文
│
└─ WaitTargetPlsnPersist(result)
    等待该 WalGroup 已刷盘（同步点）
```

### 3.3 AllocWalId()：绑定写入流

`src/wal/dstore_wal_write_context.cpp`（第 133-153 行）：

```cpp
void AtomicWalWriterContext::AllocWalId() {
    // 获取唯一的 WRITE_WAL 流
    WalStream *walStream = m_walManager->GetWalStreamManager()->GetWritingWalStream();
    if (walStream != nullptr) {
        m_walStream = walStream;
        m_walId = walStream->GetWalId();
    }
}
```

**关键约束**：同一时刻只有一个 WRITE_WAL 流，所有事务都写入同一流。

---

## 第四部分：多日志流架构

### 4.1 WalStreamManager：多流统一管理

`include/wal/dstore_wal_logstream.h`（第 848-1047 行）：

```cpp
class WalStreamManager {
    dlist_head m_walStreamsListHead;  // WAL 流链表（最多 1024 个）
    WalStream *m_writingWalStream;   // 当前唯一写入流
    LWLock m_lwlock;                 // 保护流列表
};
```

### 4.2 流生命周期状态机

`include/wal/dstore_wal_logstream.h`（第 55-70 行）：

```
CREATING → USING（正常写入）→ SYNC_DONE（已同步）
                                    ↓
                         RECOVERY_DROPPING 或 CLOSE_DROPPING
                                    ↓
                                  删除
```

### 4.3 流用途分类

```cpp
enum class WalStreamUsage {
    WAL_STREAM_USAGE_WRITE_WAL,  // 写入流（同时只有一个）
    WAL_STREAM_USAGE_ONLY_READ,  // 只读流（恢复用，可有多个）
};
```

### 4.4 TakeOverStreams()：Failover 后接管旧流

`include/wal/dstore_wal_logstream.h`（第 969 行）：

**Failover 流程**：
```
旧 Primary 崩溃
        ↓
新 Primary 接管：TakeOverStreams([旧StreamId])
  ├─ 旧流：WRITE_WAL → ONLY_READ（转为只读）
  ├─ 对旧流执行 Recovery（BuildDirtyPageSet + ParallelRedo）
  ├─ 旧流恢复完成 → RECOVERY_DROPPING → 删除
  └─ CreateWritingWalStreamWhenPromoting()  ← 新的写入流
```

### 4.5 为什么需要多流？

- **正常运行**：只有一个 WRITE_WAL 流
- **Failover 期间**：新旧流并存，新流负责接收新日志，旧流用于恢复
- **跨流排序**：GLSN 提供全局统一顺序，保证恢复正确性

---

## 第五部分：BgWalWriter 异步刷盘

### 5.1 工作原理

```
应用线程
  └─ EndAtomicWal() → WalStream::Append() → WalStreamBuffer（内存）

BgWalWriter 后台线程
  └─ 循环：WalStream::Flush(maxFlushedPlsn, maxAppendedPlsn)
           └─ 写入 WalFile（磁盘）
           └─ 更新 maxFlushedPlsn
           └─ 唤醒等待的 WaitTargetPlsnPersist() 调用者
```

### 5.2 PlsnWaitSlot：高效等待机制

`include/wal/dstore_wal_logstream.h`（第 113-141 行）：

```
WAL_WAIT_SLOTS_SIZE = 2048 个等待槽
slot = ((plsn - 1) / plsnWaitSlotBlockSize) & (WAL_WAIT_SLOTS_SIZE - 1)

BgWalWriter 刷完一批 → 通知对应 slot 的 leader 线程
                      → leader 通知 slot 内其他 follower 线程
```

**分组通知**避免惊群效应（thundering herd）。

### 5.3 BgWalWriter 初始化

`src/wal/dstore_wal_bgwriter.cpp`（第 39-130 行）：

- 每个 WalStream 绑定一个 BgWalWriter 线程
- 支持 CPU 亲和性绑定（`walwriterCpuBind` GUC 参数）

---

## 第六部分：Redo 恢复机制

### 6.1 WalRecoveryStage：恢复阶段

`include/wal/dstore_wal_recovery.h`（第 185-195 行）：

```
RECOVERY_NO_START
  → RECOVERY_STARTING
  → RECOVERY_GET_DIRTY_PAGE_SET      ← 扫描 WAL，找出脏页
  → RECOVERY_GET_DIRTY_PAGE_SET_DONE
  → RECOVERY_REDO_STARTED            ← 并行重放日志
  → RECOVERY_REDO_DONE              ← 重放完成
  → RECOVERY_DIRTY_PAGE_FLUSHED     ← 脏页已刷盘
```

### 6.2 阶段1：BuildDirtyPageSet

扫描 WAL 文件，找出所有被修改过的页面及其最新 (walId, plsn, glsn)：

```cpp
struct WalDirtyPageEntry {
    PageId pageId;
    WalId walId;
    uint64 plsn;
    uint64 glsn;
};
// 输出：按 GLSN 排序的脏页数组
```

### 6.3 阶段2：ParallelRedo 并行重放

`include/wal/dstore_wal_parallel_redo_worker.h`：

```
分发线程（Dispatch Thread）
  ├─→ Redo Worker 1（处理分配给它的页面）
  ├─→ Redo Worker 2
  └─→ Redo Worker N
      最多 MAX_REDO_WORKER_NUM = 500 个
```

**并行策略**：
- 同一页面的日志只由一个 Worker 处理（保证顺序）
- 不同页面可以并行重放（提高吞吐）
- 每个 Worker 队列容量：`REDO_WORKER_QUE_CAPACITY = 16384`

### 6.4 多流恢复的顺序保证

**问题**：多个 Stream 修改同一页面，如何确定重放顺序？

**解决**：按 GLSN 排序脏页数组：

```
页面 P 的修改历史：
  Stream1 WAL1: GLSN=100
  Stream2 WAL2: GLSN=200
  Stream1 WAL3: GLSN=300

重放顺序：WAL1(GLSN=100) → WAL2(GLSN=200) → WAL3(GLSN=300)
```

---

## 第七部分：完整流程图

```
【写入阶段】
AtomicWalWriterContext
  BeginAtomicWal() → PutNewWalRecord() + Append() → EndAtomicWal()
                                                         │
                                              WalStream::Append()
                                                         │
                                              WalStreamBuffer（内存）

【异步刷盘】
BgWalWriter::BgFlushMain()
  → WalStream::Flush()
  → WalFile（磁盘）
  → 更新 maxFlushedPlsn
  → 唤醒 WaitTargetPlsnPersist() 调用者

【故障恢复】（Failover 后）
TakeOverStreams([旧 StreamId])
  → WalRecovery::Init()
  → BuildDirtyPageSet()     扫描 WAL 文件
  → StartParallelRedo()     并行重放（N 个 Worker）
  → FlushDirtyPages()       脏页落盘
  → 新 Primary Ready
```

---

## 第八部分：关键常数与文件速查

### 关键常数

| 常数 | 值 | 说明 |
|------|-----|------|
| MAX_WAL_STREAM_COUNT | 1024 | 最多 WAL 流数 |
| MAX_PAGES_COUNT_PER_WAL_GROUP | 1030 | 单 Group 最多页面数 |
| ATOMICLOG_BUF_INIT_SIZE | 4096 | 初始缓冲区大小 |
| MAX_REDO_WORKER_NUM | 500 | 最多 Redo 工作线程数 |
| REDO_WORKER_QUE_CAPACITY | 16384 | Redo 队列容量 |
| WAL_WAIT_SLOTS_SIZE | 2048 | PLSN 等待槽数量 |

### 关键文件

| 功能 | 头文件 | 源文件 |
|------|--------|--------|
| WAL 管理器入口 | dstore_wal.h | - |
| 数据结构定义 | dstore_wal_struct.h | - |
| 写入上下文 | dstore_wal_write_context.h | dstore_wal_write_context.cpp |
| Stream 和 Manager | dstore_wal_logstream.h | dstore_wal_logstream.cpp |
| 缓冲区管理 | dstore_wal_buffer.h | dstore_wal_buffer.cpp |
| 恢复类 | dstore_wal_recovery.h | dstore_wal_recovery.cpp |
| 后台写入器 | dstore_wal_bgwriter.h | dstore_wal_bgwriter.cpp |
| 并行 Redo | dstore_wal_parallel_redo_worker.h | dstore_wal_parallel_redo_worker.cpp |
