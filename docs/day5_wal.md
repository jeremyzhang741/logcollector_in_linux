# Day 5 — WAL + Checkpoint 深度解析

> 覆盖文件：`dstore_wal_struct.h`、`dstore_wal_write_context.h`、`dstore_wal_logstream.h`、`dstore_wal_recovery.h`、`dstore_checkpointer.h`、`05_wal.md`、`09_checkpoint.md`

---

## 第一部分：WAL 的核心价值

**Write-Ahead Logging（预写日志）**是数据库持久性和崩溃恢复的基石：

```
原则：所有数据页修改，必须先在 WAL 中记录，WAL 落盘后才允许数据页落盘

保证：
  1. 原子性  — 事务 WAL 要么完整，要么不存在（CRC 校验）
  2. 持久性  — 提交时 WAL 已落盘，宕机不丢数据
  3. 快速恢复 — Checkpoint 截断旧 WAL，恢复只需重放一小段
  4. 主备同步 — 备节点回放主节点的 WAL 流保持一致
```

dstore 的 WAL 相较于传统单流架构，支持**多流并行**（最多 1024 个），每个 PDB 可以有独立的 WAL 流，GLSN 提供跨流全局顺序保证。

---

## 第二部分：LSN 两级体系

定义：`include/wal/dstore_wal_struct.h`（`WalRecordLsnInfo`，第 295-316 行）

### 2.1 PLSN — 流内物理偏移

```
PLSN = Physical Log Sequence Number
含义：单个 WAL Stream 内的字节偏移（从 0 开始，单调递增）
用途：
  - 定位 WAL 文件中的具体位置
  - WriteBlock() 的 WAL-First 检查：page.plsn ≤ flushedWalPlsn → 允许写盘
  - Checkpoint 的 diskRecoveryPlsn：崩溃恢复起点
```

### 2.2 GLSN — 全局逻辑序号

```
GLSN = Global Log Sequence Number
含义：跨所有 WAL Stream 的单调递增全局序号
用途：
  - 多流恢复时确定跨流操作顺序（不同流修改同一页面时）
  - MVCC 相关的版本排序
  - 脏页刷盘顺序依赖
```

### 2.3 WalRecordLsnInfo — 单条记录 LSN 三元组

```cpp
// include/wal/dstore_wal_struct.h:295-316
struct WalRecordLsnInfo {
    WalId  walId;    // 所属 WAL 流
    uint64 endPlsn;  // 记录在流内的结束位置
    uint64 glsn;     // 全局序号

    bool operator>(const WalRecordLsnInfo &val) const {
        // 先比 glsn，glsn 相同时比 endPlsn（同流内）
        return (this->glsn > val.glsn) ||
               (val.glsn == this->glsn && this->endPlsn > val.endPlsn);
    }
};
```

### 2.4 WalGroupLsnInfo — 原子组 LSN 信息

```cpp
// include/wal/dstore_wal_struct.h:323-327
struct WalGroupLsnInfo {
    WalId  m_walId;      // 所属 WAL 流
    uint64 m_startPlsn;  // 组起始 PLSN
    uint64 m_endPlsn;    // 组结束 PLSN
};
```

`EndAtomicWal()` 的返回值，调用方用 `WaitTargetPlsnPersist(m_walId, m_endPlsn)` 等待落盘。

### 2.5 LSN 关系约束（与 Day 3 的连接）

```
数据页允许落盘的条件：
  page.walId + page.plsn ≤ walStream.flushedPlsn
                              ↑
                   BgWalWriter 每次 Flush 后更新

Checkpoint 截断点：
  diskRecoveryPlsn = min(所有脏页的 recoveryPlsn)
  WAL 文件中 PLSN < diskRecoveryPlsn 的部分 → 可安全删除
```

---

## 第三部分：WAL 记录结构体系

### 3.1 WalRecord — 最小记录单元

```cpp
// include/wal/dstore_wal_struct.h:246-270
struct WalRecord {
    uint16  m_size;  // 总大小（含 header），最大 MAX_WAL_RECORD_SIZE
    WalType m_type;  // 操作类型（见下方枚举）
} PACKED;
constexpr uint16 MIN_WAL_RECORD_SIZE = sizeof(WalRecord);  // 4 字节
```

### 3.2 WalType 枚举全览

`include/wal/dstore_wal_struct.h`（第 57-195 行），共 80+ 种类型：

| 类别 | 典型 WalType | 说明 |
|------|------------|------|
| 堆表 | `WAL_HEAP_INSERT/DELETE/UPDATE` | 行级 DML |
| 堆表 | `WAL_HEAP_ALLOC_TD` | 扩展 TD 槽位 |
| 堆表 | `WAL_HEAP_PRUNE` | 清理死元组 |
| BTree | `WAL_BTREE_INSERT_ON_LEAF/INTERNAL` | 索引插入 |
| BTree | `WAL_BTREE_SPLIT_LEAF/INTERNAL` | 页面分裂 |
| BTree | `WAL_BTREE_RECYCLE_*` | 索引页回收队列 |
| 表空间 | `WAL_TBS_EXTEND_FILE` | 文件扩展 |
| 表空间 | `WAL_TBS_CREATE/DROP_TABLESPACE` | DDL 操作 |
| Checkpoint | `WAL_CHECKPOINT_SHUTDOWN/ONLINE` | 检查点记录 |
| Undo | `WAL_UNDO_INSERT_RECORD` | Undo 记录写入 |
| Undo | `WAL_UNDO_ALLOCATE_TXN_SLOT` | 事务槽分配 |
| 事务 | `WAL_TXN_COMMIT/ABORT` | 事务结束标记 |
| 逻辑复制 | `WAL_NEXT_CSN` | CSN 推进通知 |
| 屏障 | `WAL_BARRIER_CSN` | 集群 CSN 屏障 |

### 3.3 WalRecordAtomicGroup — 原子组

```cpp
// include/wal/dstore_wal_struct.h:279-285
struct WalRecordAtomicGroup {
    uint32    groupLen;        // 整个组的总字节数
    uint32    crc;             // CRC 校验（保证原子性）
    Xid       xid;             // 所属事务 ID
    uint16    recordNum;       // 组内 WalRecord 数量
    WalRecord walRecords[];    // 可变长记录数组
} PACKED;
```

**原子性保证**：崩溃恢复读到 CRC 不匹配的组 → 丢弃该组 → 整个事务修改回滚，不会出现半提交。

单组上限：`MAX_PAGES_COUNT_PER_WAL_GROUP = 1030` 个页面的记录。

### 3.4 WalRecordForPage — 页面记录

```cpp
// include/wal/dstore_wal_struct.h:352-496
struct WalRecordForPage : public WalRecord {
    PageId         m_pageId;         // 修改的页面（fileId + blockId）
    WalRecordFlag  m_flags;          // 标志位（见下方）
    WalId          m_pagePreWalId;   // 页面上次修改所在的 WAL 流
    uint64         m_pagePrePlsn;    // 页面上次修改的 PLSN（链式追踪）
    uint64         m_pagePreGlsn;    // 页面上次修改的 GLSN
    uint64         m_filePreVersion; // 文件版本（追踪文件重建）
} PACKED;
```

**链式追踪**：每条 WalRecordForPage 记录页面"前一次修改"的位置（preWalId/prePlsn/preGlsn），形成该页的完整修改历史链，支持增量恢复（只需找最新记录，而非扫描全部 WAL）。

### 3.5 WalRecordFlag 标志位

```cpp
struct WalRecordFlag {
    uint8 glsnChangeFlag          : 1; // GLSN 是否发生变化（跨流修改）
    uint8 containLogicalInfoFlag  : 1; // 包含逻辑复制信息
    uint8 decodeDictChangeFlag    : 1; // 目录变更（逻辑解码关注）
    uint8 heapDeleteContainsReplicaKeyFlag : 1; // DELETE 含主键
    uint8 heapUpdateContainsReplicaKeyFlag : 1; // UPDATE 含主键
    uint8 containFileVersionFlag  : 1; // 包含文件版本
    uint8 unused                  : 2;
};
```

`glsnChangeFlag = 1` 意味着该页在不同 WAL 流之间"切换"过，恢复时需要更新 GLSN。

### 3.6 WalRecordForDataPage — 数据页专用（含 TD 扩展）

```cpp
struct WalRecordForDataPage : public WalRecordForPage {
    struct AllocTdRecord {
        uint8 extendNum;                // 扩展 TD 数量（0 表示只回收不扩展）
        char  data[];                   // TrxSlotStatus 数组（tdNum 个）
    } PACKED;
    // 提供：SetAllocTdWal() / RedoAllocTdWal() / RedoExtendTdWal()
};
```

TD 扩展操作（`WAL_HEAP_ALLOC_TD`）会将原有 TD 的 `TrxSlotStatus` 数组一并记录，恢复时：
1. 根据各 TD 的 status（COMMITTED/FROZEN → 可回收）重新计算可用 TD
2. 如有 `extendNum > 0`，在页内新增 TD 槽（移动 ItemId 数组，腾出空间）

### 3.7 WalFileHeaderData — WAL 文件头

```cpp
// include/wal/dstore_wal_struct.h:796-810
struct WalFileHeaderData {
    uint32 crc;                  // 文件头 CRC 校验
    uint32 version;              // 格式版本
    uint64 startPlsn;            // 本文件第一条记录的起始 PLSN
    uint64 fileSize;             // 文件大小（默认 128MB）
    uint64 timelineId;           // 时间线（Failover 后递增）
    uint32 lastRecordRemainLen;  // 最后一条记录跨文件的遗留长度
    uint32 magicNum;             // = 0xD2A8F347
} PACKED;
```

**关键常量**：

| 常量 | 值 | 说明 |
|------|-----|------|
| `TEMPLATE_WAL_FILE_SIZE` | 128 MB | 单文件默认大小 |
| `WAL_FILE_HEAD_MAGIC` | `0xD2A8F347` | 文件头魔数 |
| `WAL_FILE_HDR_SIZE` | `MAXALIGN(sizeof(WalFileHeaderData))` | 头部对齐大小 |

---

## 第四部分：WAL 写入流程

### 4.1 AtomicWalWriterContext — 写入上下文

```cpp
// include/wal/dstore_wal_write_context.h:38-214
class AtomicWalWriterContext : public BaseObject {
    uint8     *m_buf;                            // WAL 数据缓冲区（初始 4KB）
    uint32     m_bufUsed;                        // 已使用字节数
    WalId      m_walId;                          // 绑定的 Stream ID
    WalStream *m_walStream;                      // 绑定的 Stream 指针
    BufferDesc *m_pagesNeedWal[1030];            // 本次涉及修改的页面列表
    uint16     m_numPagesNeedWal;
    PdbId      m_pdbId;
};
```

**关键约束**：`AllocWalId()` 获取当前唯一的 `WRITE_WAL` 流，同一时刻全系统只有一个写入流。

### 4.2 标准五步写入工作流

```
Step 1: BeginAtomicWal(xid)
  └─ 初始化 WalRecordAtomicGroup header，记录 xid
  └─ AllocWalId() → 绑定当前 WRITE_WAL 流

Step 2: RememberPageNeedWal(bufDesc)  [每个修改页面调用一次]
  └─ 将 bufDesc 推入 m_pagesNeedWal[]

Step 3: PutNewWalRecord(record) + Append(data, size)  [循环]
  └─ PutNewWalRecord：将 WalRecord 复制进 m_buf（含压缩）
  └─ Append：向当前 Record 追加变长 payload

Step 4: EndAtomicWal() → WalGroupLsnInfo
  ├─ 计算 CRC，写入 WalRecordAtomicGroup.crc
  ├─ WalStream::Append(m_buf, m_bufUsed) → 写入 WalStreamBuffer（内存）
  ├─ SetPagesLSN(result)  → 为 m_pagesNeedWal 中每个页面设置 walId/plsn/glsn
  └─ ClearState()         → 重置上下文，可复用

Step 5: WaitTargetPlsnPersist(result.m_walId, result.m_endPlsn)  [可选]
  └─ 阻塞等待 BgWalWriter 将该 Group 刷入磁盘
  └─ 事务提交路径必须等待（持久性保证）
```

### 4.3 WalRecordForPage 压缩

`WalRecordForPage::Compress()` 对 `pageId` 和 `pre*` 字段进行 Varint 压缩：

```
原始:  FileId(2B) + BlockId(4B) + flags(1B) + preWalId(8B) + prePlsn(8B) + preGlsn(8B) = 31B
压缩:  FileId(1-5B) + BlockId(1-5B) + flags(1B) + preWalId(1-9B) + prePlsn(1-9B) + preGlsn(1-9B)
最坏:  增加 MAX_WAL_RECORD_FOR_PAGE_COMPRESSED_SIZE 字节
最好:  小值时可节省 10~20 字节
```

---

## 第五部分：WAL Stream 架构

### 5.1 WalStreamState — 流状态机

```cpp
enum class WalStreamState : uint8_t {
    CREATING = 0,        // 正在初始化
    USING,               // 正常写入中
    SYNC_DONE,           // 所有日志已同步到备节点
    CLOSE_DROPPING,      // PDB 关闭，等待删除文件
    RECOVERY_DROPPING,   // 恢复完成，等待删除文件
};
```

状态转换：

```
CREATING → USING（初始化完成）
USING    → SYNC_DONE（主备同步完成）
SYNC_DONE → RECOVERY_DROPPING（Failover 后旧流恢复完毕）
USING    → CLOSE_DROPPING（PDB 关闭）
```

### 5.2 WalStreamUsage — 流用途

```cpp
enum class WalStreamUsage {
    WAL_STREAM_USAGE_INVALID,     // 无效
    WAL_STREAM_USAGE_WRITE_WAL,   // 写入流（全局唯一）
    WAL_STREAM_USAGE_ONLY_READ,   // 只读流（Failover 期间的旧流）
};
```

正常运行时：全局只有 **1 个** `WRITE_WAL` 流。  
Failover 期间：新写入流 + 多个旧只读流并存，旧流恢复完成后删除。

### 5.3 WalStream 关键接口

```cpp
class WalStream : virtual public BaseObject {
    WalId            m_walId;           // 流 ID
    WalStreamBuffer *m_walStreamBuffer; // 内存缓冲区
    WalFileManager  *m_walFileManager;  // 文件管理器
    PlsnWaitSlot    *m_waitSlots;       // PLSN 等待槽（2048 个）
    // ...

    // 核心接口
    void   Append(const uint8 *buf, uint32 size);  // 写入缓冲区
    RetStatus Flush(uint64 &maxFlushedPlsn, uint64 maxAppendedPlsn); // 刷盘
    RetStatus WaitTargetPlsnPersist(uint64 targetPlsn); // 等待持久化
};
```

### 5.4 PlsnWaitSlot — 高效等待机制

```cpp
class PlsnWaitSlot {
    LWLock                  m_waitLock;    // 保护槽位
    std::mutex              m_waitMtx;
    std::condition_variable m_waitCv;      // 等待/通知
    gs_atomic_uint64        m_waiterCount; // 等待线程数
};

// 槽位计算（2048 个槽，按 PLSN 哈希）
slot_index = ((plsn - 1) / plsnWaitSlotBlockSize) & (WAL_WAIT_SLOTS_SIZE - 1)
```

**分组通知协议**（避免惊群）：

```
BgWalWriter 刷完一批 WAL
  └─ 遍历受影响的 slot：NotifySlotLeaderIfNecessary(slot)
      └─ 唤醒该 slot 的 leader 线程
          └─ leader 广播通知 slot 内的所有 follower 线程
```

---

## 第六部分：BgWalWriter — 异步持久化

### 6.1 工作原理

```
应用线程
  └─ EndAtomicWal()
       └─ WalStream::Append() → WalStreamBuffer（内存，环形缓冲区）

BgWalWriter 后台线程（每个 WalStream 一个）
  └─ BgFlushMain() 循环：
       ├─ WalStream::Flush(maxFlushedPlsn, maxAppendedPlsn)
       │    └─ pwrite(walFile, buffer, size)  ← 真正写盘
       ├─ 更新 maxFlushedPlsn
       └─ 唤醒 WaitTargetPlsnPersist() 的等待者
```

### 6.2 完整刷盘时序

```
时间轴 →

应用线程:   [BeginAtomicWal]──[PutRecord+Append]──[EndAtomicWal]──[WaitTargetPlsnPersist]
                                                          ↓                    ↑ 阻塞
WalStreamBuffer:                             [WAL data 追加到环形缓冲]         │
                                                                               │
BgWalWriter:                                                  [Flush → pwrite] ┘
                                                                [更新 flushedPlsn]
                                                                [通知 PlsnWaitSlot]
```

---

## 第七部分：Checkpoint 机制

### 7.1 核心问题与解法

```
问题：没有 Checkpoint，崩溃恢复需要重放全部历史 WAL（可能数小时）

解法：定期将所有脏页刷到磁盘，记录安全点 diskRecoveryPlsn
      崩溃恢复从 diskRecoveryPlsn 开始重放，只需几分钟
```

### 7.2 WalCheckPoint — 检查点记录结构

```cpp
// include/wal/dstore_wal_struct.h:219-223
struct WalCheckPoint {
    Timestamp time;
    uint64    diskRecoveryPlsn;   // 崩溃恢复起点 PLSN（关键！）
    MemoryCheckpoint memoryCheckpoint;  // 内存节点状态快照
};

struct MemoryCheckpoint {
    uint64 term;            // 主备选举任期
    uint32 memoryNodeCnt;   // 内存节点数量
    uint64 memRecoveryPlsn; // 内存节点可重放起点
};
```

`diskRecoveryPlsn` 的含义：**所有 PLSN ≤ diskRecoveryPlsn 的数据页已安全落磁盘**，崩溃恢复只需重放 PLSN > diskRecoveryPlsn 的 WAL。

### 7.3 WalCheckpointInfoData — 每流 Checkpoint 状态

```cpp
// include/buffer/dstore_checkpointer.h:104-115
struct WalCheckpointInfoData {
    dlist_node        node;
    WalId             walId;                       // 对应的 WAL 流
    CheckpointRequest checkpointStreamRequest;     // 请求/完成计数器
    LWLock            checkpointLwLock;            // 同流只允许一个 Checkpoint
    Timestamp         lastCheckpointTime;          // 上次 Checkpoint 时间
    DstoreSpinLock    recoveryLock;                // 保护下面两个字段
    uint64            lastCheckPointRecoveryPlsn;  // 上次 diskRecoveryPlsn
    WalCheckPoint     lastCheckPoint;              // 上次完整 Checkpoint 记录
};
```

每个 WAL 流有独立的 `WalCheckpointInfoData`，互不阻塞。

### 7.4 CheckpointRequest — 请求/完成协议

```cpp
class CheckpointRequest {
    DstoreSpinLock m_checkpointLock;
    uint32         m_checkpointStart;  // 已启动的 Checkpoint 数（单调递增）
    uint32         m_checkpointDone;   // 已完成的 Checkpoint 数
    uint32         m_checkpointFail;   // 失败次数
    CheckpointFlag m_checkpointFlag;
};
```

**Backend 等待 Checkpoint 完成的协议**（无轮询）：

```
1. 记录当前 m_checkpointFail 和 m_checkpointStart
2. 设置 m_checkpointFlag，发信号给 CheckpointerMain
3. 等待 m_checkpointStart 变化（说明新 Checkpoint 已开始）
4. 记录新 m_checkpointStart
5. 等待 m_checkpointDone >= 新 m_checkpointStart（模数比较，防溢出）
6. 若 m_checkpointFail 变化 → 失败；否则 → 成功
```

### 7.5 CheckpointMgr — 管理器

```cpp
class CheckpointMgr : public BaseObject {
    dlist_head              m_checkpointInfoList; // 各流的 CheckpointInfoData 链表
    uint32                  m_walCheckpointDataNum; // 流数量
    std::atomic_bool        m_isFullCkpting;      // 是否正在全量 Checkpoint
    WalManager             *m_walMgr;

    // 核心接口
    void      CheckpointerMain();                  // 后台主循环
    RetStatus CreateCheckpoint(WalId, flags, *isPerformed); // 单流 Checkpoint
    RetStatus FullCheckpoint(PdbId);               // 全量（所有流）
};
```

### 7.6 CreateCheckpoint() 详细流程

```
CreateCheckpoint(walId, flags)
  │
  ├─ Step 1: checkpointLwLock 独占（同流串行化）
  │
  ├─ Step 2: bgPageWriter->GetMinRecoveryPlsn()
  │           → 扫描脏页队列，返回最小 recoveryPlsn
  │           → newPlsn = 所有未落盘页中最老的那个 WAL 位置
  │
  ├─ Step 3: if lastCheckPoint.diskRecoveryPlsn >= newPlsn:
  │               → 跳过（没有新进度，无需重复写 Checkpoint）
  │
  ├─ Step 4: 构造 WalCheckPoint：
  │           checkPoint.diskRecoveryPlsn = newPlsn
  │           checkPoint.time = now
  │           checkPoint.memoryCheckpoint = 当前内存节点状态
  │
  ├─ Step 5: 写 WalCheckPoint WAL 记录（WAL_CHECKPOINT_ONLINE）
  │           → 这本身也是一条 WAL！先 WAL 再持久化
  │
  ├─ Step 6: 写入 ControlFile（持久化）
  │           → 更新 lastCheckPoint、lastCheckPointRecoveryPlsn
  │
  └─ Step 7: 释放 checkpointLwLock，更新 lastCheckpointTime
```

### 7.7 Checkpoint 类型

| 类型 | diskRecoveryPlsn 取值 | 触发场景 |
|------|----------------------|---------|
| **增量（INCREMENTAL）** | `GetMinRecoveryPlsn()` 的返回值 | 定时触发，代价小 |
| **全量（FULL）** | 当前 WAL 插入点（等待所有脏页落盘） | 手动/紧急，代价大但恢复快 |

---

## 第八部分：崩溃恢复流程

### 8.1 恢复阶段状态机

`include/wal/dstore_wal_recovery.h`

```
RECOVERY_NO_START
  → RECOVERY_STARTING
  → RECOVERY_GET_DIRTY_PAGE_SET        // 阶段1：扫描 WAL，建立脏页集合
  → RECOVERY_GET_DIRTY_PAGE_SET_DONE
  → RECOVERY_REDO_STARTED              // 阶段2：并行重放 WAL
  → RECOVERY_REDO_DONE
  → RECOVERY_DIRTY_PAGE_FLUSHED        // 阶段3：脏页刷盘
```

### 8.2 WalDirtyPageEntry — 脏页集合条目

```cpp
// include/wal/dstore_wal_recovery.h:71-96
struct WalDirtyPageEntry {
    PageId pageId;
    WalId  walId;
    uint64 plsn;
    uint64 glsn;

    bool operator>(const WalDirtyPageEntry &entry) const {
        if (glsn > entry.glsn) return true;
        // 同 glsn 时比 plsn（同流内保证顺序）
        if (glsn == entry.glsn && plsn > entry.plsn) return true;
        return false;
    }
};
```

**按 GLSN 排序**是多流恢复顺序正确的关键。

### 8.3 阶段1：BuildDirtyPageSet

从 `lastCheckPoint.diskRecoveryPlsn` 开始扫描 WAL 文件：

```
for each WalRecordAtomicGroup in WAL (PLSN > diskRecoveryPlsn):
  if CRC 校验失败: 丢弃（truncate 点）
  for each WalRecordForPage in Group:
    dirtyPageSet[pageId] = 最新的 (walId, plsn, glsn)
    // 同一页面多次出现 → 只保留 GLSN 最大的那次
输出：按 GLSN 排序的 WalDirtyPageEntry 数组
```

### 8.4 阶段2：ParallelRedo 并行重放

```
WalRecovery::StartParallelRedo()
  │
  ├─ 分发线程（Dispatch Thread）
  │   └─ 按页面哈希分配到各 Worker
  │       保证：同一页面的所有 WAL 都由同一个 Worker 处理
  │
  ├─ Redo Worker 1
  │   └─ 处理分配给它的页面，按 GLSN 顺序重放
  │
  ├─ Redo Worker 2
  │   └─ ...
  │
  └─ Redo Worker N（最多 MAX_REDO_WORKER_NUM = 500）
      每个 Worker 队列容量 REDO_WORKER_QUE_CAPACITY = 16384
```

**并行策略**：不同页面 → 不同 Worker → 并行；同一页面 → 同一 Worker → 顺序。

### 8.5 Failover 后的恢复

```
旧 Primary 崩溃
        ↓
新 Primary 调用 TakeOverStreams([旧 StreamId])
  ├─ 旧流：WRITE_WAL → ONLY_READ
  ├─ 对旧流执行完整恢复（BuildDirtyPageSet + ParallelRedo）
  ├─ 旧流恢复完成 → RECOVERY_DROPPING → 文件删除
  └─ CreateWritingWalStreamWhenPromoting() → 新写入流 Ready
```

---

## 第九部分：完整交互图

### 9.1 正常写入路径

```
应用线程                 WalStreamBuffer         BgWalWriter        磁盘
    │                         │                      │               │
    │─BeginAtomicWal()──────→│                      │               │
    │─PutRecord+Append()────→│                      │               │
    │─EndAtomicWal()────────→│ Append(buf,size)     │               │
    │  返回 WalGroupLsnInfo   │←─────────────────────────────────   │
    │                         │                      │               │
    │─WaitTargetPlsnPersist() │                      │               │
    │  (阻塞)                  │                   Flush()──────────→│
    │                         │                   更新 flushedPlsn  │
    │                         │                   NotifySlot()      │
    │  唤醒 ←─────────────────────────────────────────────────      │
    │  (返回)                  │                      │               │
```

### 9.2 Checkpoint 路径（不直接刷脏页）

```
MarkDirty()  →  DirtyPageQueue  →  BgDiskPageWriter
                                        │
                               PrepareCheckPage()
                                        │
                               WaitTargetPlsnPersist()  ←  BgWalWriter 刷 WAL
                                        │
                               pwrite(dataPage)  →  磁盘
                               更新 minRecoveryPlsn
                                        │
CheckpointerMain()                      │
    └─ GetMinRecoveryPlsn() ────────────┘
    └─ WriteCheckpointWAL()
    └─ 写 ControlFile（diskRecoveryPlsn）
    └─ 删除 PLSN < diskRecoveryPlsn 的旧 WAL 文件
```

### 9.3 多流下的 WAL 排序示例

```
页面 P 的修改历史（来自不同流）：

Stream1: WAL_HEAP_INSERT (GLSN=100, PLSN=500)
Stream2: WAL_HEAP_DELETE  (GLSN=200, PLSN=300)  ← 另一流
Stream1: WAL_HEAP_UPDATE  (GLSN=300, PLSN=700)

WalRecordForPage 链接：
  UPDATE.pre = (Stream1, PLSN=500, GLSN=100)  ← 指向 INSERT
  DELETE.pre = (Stream1, PLSN=500, GLSN=100)
  INSERT.pre = (0, 0, 0)  ← 第一条

恢复重放顺序（按 GLSN）：
  1. Stream1 INSERT  (GLSN=100)
  2. Stream2 DELETE  (GLSN=200)
  3. Stream1 UPDATE  (GLSN=300)
```

---

## 第十部分：与 Day3/Day4 的连接点

| 概念 | Day3 Buffer | Day5 WAL |
|------|-------------|----------|
| WAL-First | `PrepareCheckPageBeforeStartIo()` 等待 WAL 落盘 | `WaitTargetPlsnPersist()` 是等待的目标 |
| recoveryPlsn | `BufferDesc.recoveryPlsn[]` 在 MarkDirty 时记录 | Checkpoint 读取的 `GetMinRecoveryPlsn()` 源头 |
| page.plsn | 数据页头部存储 PLSN | 由 `EndAtomicWal()` 的 `SetPagesLSN()` 写入 |
| CR 页 | 历史快照通过 Undo 构造 | CR 页自身也需要 WAL（DAY6 Undo 涵盖） |
| BgWalWriter | Day3 提到后台写 WAL | Day5 详解其刷盘机制和 PlsnWaitSlot |

| 概念 | Day4 Transaction | Day5 WAL |
|------|-----------------|----------|
| 两阶段提交 | PENDING_COMMIT 时等 WAL 落盘 | `WaitTargetPlsnPersist(commitEndPlsn)` 是等待点 |
| WAL_TXN_COMMIT | Commit 时写 WAL 记录 | WAL_TXN_COMMIT 是 WalType 枚举值 |
| commitEndPlsn | TransactionSlot 记录提交 WAL 位置 | 用于判断备节点能否看到该事务 |

---

## 第十一部分：关键文件速查

| 功能 | 文件路径 | 关键行号 |
|------|---------|---------|
| WalType 枚举 | `include/wal/dstore_wal_struct.h` | 57-195 |
| WalRecord 基础结构 | `include/wal/dstore_wal_struct.h` | 246-270 |
| WalRecordAtomicGroup | `include/wal/dstore_wal_struct.h` | 279-285 |
| WalRecordForPage | `include/wal/dstore_wal_struct.h` | 352-496 |
| WalRecordForDataPage | `include/wal/dstore_wal_struct.h` | 619-792 |
| WalCheckPoint | `include/wal/dstore_wal_struct.h` | 219-223 |
| WalFileHeaderData | `include/wal/dstore_wal_struct.h` | 796-810 |
| WalRecordLsnInfo | `include/wal/dstore_wal_struct.h` | 295-316 |
| WalGroupLsnInfo | `include/wal/dstore_wal_struct.h` | 323-327 |
| AtomicWalWriterContext | `include/wal/dstore_wal_write_context.h` | 38-214 |
| WalStreamState/Usage | `include/wal/dstore_wal_logstream.h` | 55-80 |
| PlsnWaitSlot | `include/wal/dstore_wal_logstream.h` | 113-141 |
| WalStream 类 | `include/wal/dstore_wal_logstream.h` | 208+ |
| CheckpointFlag | `include/buffer/dstore_checkpointer.h` | 38-41 |
| CheckpointRequest | `include/buffer/dstore_checkpointer.h` | 76-102 |
| WalCheckpointInfoData | `include/buffer/dstore_checkpointer.h` | 104-115 |
| CheckpointMgr | `include/buffer/dstore_checkpointer.h` | 143-231 |
| WalDirtyPageEntry | `include/wal/dstore_wal_recovery.h` | 71-96 |
| MAX_REDO_WORKER_NUM | `include/wal/dstore_wal_recovery.h` | 40 |

### 关键常量速查

| 常量 | 值 | 位置 |
|------|-----|------|
| `MAX_WAL_STREAM_COUNT` | 1024 | `dstore_control_walinfo.h:37` |
| `MAX_PAGES_COUNT_PER_WAL_GROUP` | 1030 | `dstore_wal_struct.h` |
| `TEMPLATE_WAL_FILE_SIZE` | 128 MB | `dstore_wal_logstream.h` |
| `WAL_WAIT_SLOTS_SIZE` | 2048 | `dstore_wal_logstream.h` |
| `MAX_REDO_WORKER_NUM` | 500 | `dstore_wal_recovery.h:40` |
| `REDO_WORKER_QUE_CAPACITY` | 16384 | `dstore_wal_parallel_redo_worker.h` |
| `WAL_FILE_HEAD_MAGIC` | `0xD2A8F347` | `dstore_wal_struct.h` |

---

## Day 6 预告 — Undo

下一步深入 Undo 模块：

- Undo Zone 的组织结构（Zone / Slot / Record 三层）
- UndoRecord 的格式和链式组织
- 事务回滚如何通过 Undo 链恢复数据页
- MVCC 读历史版本如何构造 CR 页（与 Day3 CR 页的连接）
- recycleMinCsn 如何驱动 Undo GC（与 Day4 的连接）
