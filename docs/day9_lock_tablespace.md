# Day 9 — Lock Manager + Tablespace + Segment

> 前置知识：Day 1（全局架构）、Day 3（Buffer/LWLock）、Day 4（Transaction/XID）、Day 5（WAL）、Day 7（Heap/FSM）

---

## 目录

1. [模块全景](#1-模块全景)
2. [LWLock — 轻量锁基础](#2-lwlock--轻量锁基础)
3. [Lock Manager 类层次](#3-lock-manager-类层次)
4. [LockTag — 锁对象标识](#4-locktag--锁对象标识)
5. [LockEntry — 每锁状态](#5-lockentry--每锁状态)
6. [LockHashTable — 锁哈希表](#6-lockhashtable--锁哈希表)
7. [TableLockMgr — 快速路径](#7-tablelockmgr--快速路径)
8. [XactLockMgr — 事务锁](#8-xactlockmgr--事务锁)
9. [死锁检测 — WaitForGraph](#9-死锁检测--waitforgraph)
10. [Tablespace 架构](#10-tablespace-架构)
11. [TbsDataFile — 文件内部结构](#11-tbsdatafile--文件内部结构)
12. [Segment 架构](#12-segment-架构)
13. [HeapSegment + 分布式 FSM](#13-heapsegment--分布式-fsm)
14. [Tablespace WAL](#14-tablespace-wal)
15. [跨模块连接](#15-跨模块连接)
16. [快速参考](#16-快速参考)

---

## 1. 模块全景

```
┌─────────────────────────────────────────────────────────────────┐
│                       Lock Manager 层                            │
│  LockMgr（基类）                                                 │
│    ├─ TableLockMgr（表锁，含快速路径）                           │
│    └─ XactLockMgr （事务锁，Wait/Transfer）                      │
│  LWLock（轻量锁，用于内部数据结构保护）                          │
│  DeadlockDetector（WaitForGraph + 受害者选择）                   │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│                      Tablespace 层                               │
│  TablespaceMgr                                                   │
│    ├─ TableSpace[MAX_TABLESPACE_ITEM_CNT]                        │
│    │    └─ TbsDataFile[]（每种 ExtentSize 一个文件）             │
│    │         └─ TbsDataFileBitmapMgr（Bitmap 空间管理）          │
│    └─ m_datafiles[MAX_DATAFILE_ITEM_CNT]（全局文件索引）         │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│                       Segment 层                                 │
│  SegmentInterface                                                │
│    └─ Segment（segmentId 即第一个 PageId）                       │
│         └─ DataSegment（有序 Extent 链）                         │
│              ├─ HeapSegment（+ FSM Tree 列表）                   │
│              └─ IndexSegment（BTree 索引段）                     │
└─────────────────────────────────────────────────────────────────┘
```

**核心关系链**：
```
SQL 操作 → TableLockMgr（保护表/分区并发）
        → XactLockMgr（等待事务提交）
        → TableSpace.AllocExtent（分配 Extent）
        → Segment.Extend（挂载到 Segment 链）
        → HeapSegment.GetNewPage（通过 FSM 分配数据页）
        → HeapPage.AddTuple（实际写入数据）
```

---

## 2. LWLock — 轻量锁基础

LWLock 是整个引擎内部数据结构保护的基石（Buffer 分区、Hash 表分区、Tablespace 文件锁等均使用它）。

```cpp
// include/lock/dstore_lwlock.h
typedef struct LWLock {
    int              spinsPerDelay; // 自旋等待次数调节
    uint16           groupId;       // 组ID（用于诊断）
    gs_atomic_uint64 state;         // 64bit 原子状态（当前持有者计数 + 标志位）
    dlist_head       waiters;       // 等待队列
} LWLock;

// cache-line 对齐版本，避免伪共享
typedef union LWLockPadded {
    LWLock lock;
    char   pad[DSTORE_CACHELINE_SIZE]; // 通常 64 字节
} LWLockPadded;

enum LWLockMode : uint8 {
    LW_EXCLUSIVE,          // 排他写锁
    LW_SHARED,             // 共享读锁
    LW_WAIT_UNTIL_FREE     // 内部状态，等待锁空闲（非申请参数）
};

constexpr uint MAX_SIMUL_LWLOCKS = 4224; // 单线程同时持有上限
```

**LWLock 与 Regular Lock 对比**：

| 属性 | LWLock | LockMgr（Regular Lock） |
|------|--------|-------------------------|
| 粒度 | 内部数据结构 | 数据库对象（表/事务/行） |
| 模式 | Shared / Exclusive | 9种 ACCESS_SHARE~ACCESS_EXCLUSIVE |
| 死锁检测 | 无（依靠代码规范避免） | 有（WaitForGraph） |
| 持有时间 | 极短（微秒级） | 可跨越整个事务 |
| 等待 | 自旋 + 内核挂起 | 内核 futex 等待 |
| 记录 | LWlockContext.held_lwlocks[] | LocalLock（线程私有） |

---

## 3. Lock Manager 类层次

```
LockMgr（基类）
 ├─ Initialize(hashTableSize, partitionNum)
 ├─ Lock(tag, mode, dontWait, info)   ← 通用锁获取
 ├─ Unlock(tag, mode)
 └─ m_lockTable: LockHashTable*

TableLockMgr : LockMgr
 ├─ Lock(tag, mode, dontWait, info)   ← 重写：弱锁走快速路径
 ├─ LockTable(lockContext, isAlreadyHeld)
 ├─ BatchUnlock(tag, mode, unlockTimes)
 └─ m_tableLockStats: TableLockStats

XactLockMgr : LockMgr
 ├─ Lock(pdbId, xid)                  ← 独占锁定事务 XID
 ├─ Unlock(pdbId, xid)
 ├─ Wait(pdbId, xid)                  ← 以 ROW_EXCLUSIVE 等待事务结束
 ├─ WaitForAnyTransactionEnd(...)
 └─ TransferXactLockHolder(pdbId, xid)

enum LockMgrType : uint8 {
    LOCK_MGR,       // 通用 LockMgr
    TABLE_LOCK_MGR, // 表级 TableLockMgr
    XACT_LOCK_MGR   // 事务 XactLockMgr
};
```

---

## 4. LockTag — 锁对象标识

`LockTag` 是锁的唯一键，28 字节紧凑结构（无 padding）：

```cpp
// include/lock/dstore_lock_datatype.h
struct LockTag {
    uint32         field1;        // 通用 ID 字段1（通常为 pdbId）
    uint32         field2;        // 通用 ID 字段2（通常为 relId/xid低32位）
    uint32         field3;        // 通用 ID 字段3（xid高32位/partId）
    uint32         field4;        // 通用 ID 字段4
    uint32         field5;        // 通用 ID 字段5
    LockTagType    lockTagType;   // uint16：锁对象类型（20种）
    LockMethodId   lockMethodId;  // uint8：锁方法（DEFAULT/USER/NODELEVEL）
    LockRecoveryMode recoveryMode;// uint8：崩溃恢复时是否保留
};
// static_assert: 4*5 + 2 + 1 + 1 = 24 字节（注：代码注释有误，实际含5个field32）
```

**LockTagType 完整枚举（20种）**：

| LockTagType | 说明 | LockMethod |
|-------------|------|-----------|
| `LOCKTAG_TABLE` | 整张表 | DEFAULT |
| `LOCKTAG_PARTITION` | 分区（pdbId+relId+partId） | DEFAULT |
| `LOCKTAG_TABLE_EXTEND` | 表文件扩展权 | DEFAULT |
| `LOCKTAG_TBS_EXTEND` | Tablespace 扩展权 | DEFAULT |
| `LOCKTAG_TRANSACTION` | 等待事务提交 | DEFAULT |
| `LOCKTAG_CSN` | CSN 分配全局锁 | NODELEVEL |
| `LOCKTAG_ZONE` | Undo Zone 锁 | NODELEVEL |
| `LOCKTAG_CONTROL_FILE` | ControlFile 操作锁 | DEFAULT |
| `LOCKTAG_DEADLOCK_DETECT` | 死锁检测节点选举 | NODELEVEL |
| `LOCKTAG_PDB` | 可插拔数据库锁 | DEFAULT |
| `LOCKTAG_TABLESPACE` | 表空间操作锁 | DEFAULT |
| `LOCKTAG_ADVISORY` | 用户自定义咨询锁 | USER |
| `LOCKTAG_OBJECT` | 数据库对象锁（5个field） | DEFAULT |
| `LOCKTAG_PACKAGE/PROCEDURE` | 存储过程/包锁 | DEFAULT |
| `LOCKTAG_BACKUP_RESTORE` | 备份恢复操作锁 | DEFAULT |

**LockRecoveryMode 语义**：

```
RELEASE_AFTER_LOCK_RECOVERY  — 崩溃后重建时释放（临时性锁）
RELEASE_AFTER_SYSTEM_RECOVERY — 系统恢复完成后才释放（LOCKTAG_TABLE/ADVISORY/PACKAGE/PROCEDURE）
```

**LockMode 9种（PostgreSQL 兼容）**：

```
NO_LOCK(0) < ACCESS_SHARE(1) < ROW_SHARE(2) < ROW_EXCLUSIVE(3)
< SHARE_UPDATE_EXCLUSIVE(4) < SHARE(5) < SHARE_ROW_EXCLUSIVE(6)
< EXCLUSIVE(7) < ACCESS_EXCLUSIVE(8)
```

---

## 5. LockEntry — 每锁状态

每个被锁定的对象在 `LockHashTable` 中对应一个 `LockEntry`：

```cpp
// include/lock/dstore_lock_entry.h

class LockEntryCore {
    LockTag  m_lockTag;                        // 锁对象标识
    LockMask m_grantMask;                      // 已授予的锁模式位图
    LockMask m_waitMask;                       // 等待中的锁模式位图
    uint32   m_grantedTotal;                   // 总授予数
    uint32   m_waitingTotal;                   // 总等待数
    uint32   m_grantedCnt[DSTORE_LOCK_MODE_MAX]; // 各模式授予计数
    uint32   m_waitingCnt[DSTORE_LOCK_MODE_MAX]; // 各模式等待计数
};

struct LockEntry {
    LockEntryCore       lockEntryCore;   // 锁状态核心
    LockRequestSkipList grantedQueue;    // 已授予请求的跳表（O(log N)）
    dlist_head          waitingQueue;    // 等待请求的链表（FIFO）
};
```

**LockRequestSkipList** — 已授予队列使用跳表（而非普通链表）：

```
MAX_SKIPLIST_LEVEL = 8
时间复杂度: 插入/删除/查找 O(log N)

| Level 2 |->| A |----->| F |------>| NULL |
| Level 1 |->| A |->| B |->| D |->| F |->| H |->| NULL |
| Level 0 |->| A |->| B |->| C |->| D |->| E |->| F |->| G |->| H |->| NULL |
```

**LockEnqueueMethod 四种入队方式**：

```cpp
enum class LockEnqueueMethod : uint8 {
    HOLD_OR_WAIT      = 0, // 正常：可入授予队列或等待队列
    HOLD_BUT_DONT_WAIT= 1, // 不等待，不入等待队列（LOCK_DONT_WAIT）
    DONT_HOLD_ONLY_WAIT=2, // 只等待，不入授予队列（Wait for xact）
    FORCE_HOLD        = 3, // 强制入授予队列（不检测冲突）
};
```

---

## 6. LockHashTable — 锁哈希表

```cpp
// include/lock/dstore_lock_hash_table.h
class LockHashTable {
    HTAB          *m_lockTable;         // 哈希表本体
    LWLockPadded  *m_partitionLocks;    // 分区锁数组（每分区 1 个 LWLock）
    uint32         m_partitionNum;      // 分区数（初始化时决定）
    LockRequestFreeLists m_freeLists;   // 每分区的 LockRequest 对象池
};
```

**工作流程**：
```
Lock(tag, mode):
  hashCode = Hash(tag)
  partitionIdx = hashCode % m_partitionNum
  
  AcquireLWLock(m_partitionLocks[partitionIdx], LW_EXCLUSIVE)
  entry = HTAB.Find(tag) or HTAB.Insert(tag)
  entry.EnqueueLockRequest(request, freeList, info)
    ├─ HasConflictWithAnyHolder? → 是 → TryAddToWaiterQueue
    └─ 否 → AddToHolderQueue（复用 LockRequest 对象池）
  ReleaseLWLock
  
  如果进入等待队列: 挂起线程等待 WakeUp()
```

---

## 7. TableLockMgr — 快速路径

`TableLockMgr` 为**弱锁**（`< SHARE_UPDATE_EXCLUSIVE_LOCK`）提供**快速路径**，避免竞争主锁哈希表。

```cpp
// include/lock/dstore_lock_thrd_local.h
constexpr uint32 FAST_LOCK_ENTRY_MAP_MAX_SLOT = 512;        // 线程本地槽数
constexpr uint32 FAST_PATH_STRONG_LOCK_HASH_PARTITIONS = 1024; // 强锁标记分区

struct LocalLockEntry {
    LockTag  tag;
    LockMgrType type;
    uint8    grantedByFastPath;    // 哪些模式走了快速路径（位图）
    uint32   grantedTotal;
    uint32   granted[DSTORE_LOCK_MODE_MAX];
};
```

**快速路径决策树**：

```
TableLockMgr::Lock(tag, mode):
  
  弱锁？(mode < SHARE_UPDATE_EXCLUSIVE)
  ├─ 是 → WeakLockAcquire:
  │         localEntry = ThreadLocal.Find(tag)
  │         if (StrongLockExists?):    ← 检查是否有强锁占用同一对象
  │           localEntry.granted[mode]++
  │           localEntry.grantedByFastPath |= mode_bit
  │           return SUCC              ← 快速路径完成！无共享内存操作
  │         else:
  │           TryMarkStrongLockByFastPath(tag) ← 先原子标记
  │           then fall through to main table
  └─ 否 → StrongLockAcquire:
           WaitLazyLockGoneOnAllThreads(tag, mode)  ← 等弱锁清空
           EnableLazyLockOnAllThreads(tag, mode)
           LockInMainLockTable(context)              ← 走主哈希表
```

**统计计数**（`TableLockStats`）：
- `numWeakLockTransfers` — 弱锁转入主表次数
- `numFastPathSuccesses` — 快速路径成功次数
- `numStrongLocksAcquired` — 强锁获取次数

---

## 8. XactLockMgr — 事务锁

事务锁的作用：**当一个事务需要等待另一个事务提交或回滚时**，使用 `LOCKTAG_TRANSACTION` 锁。

```cpp
// include/lock/dstore_xact_lock_mgr.h
#define LOCK_XACT_SHARED_WAIT_LOCK DSTORE_ROW_EXCLUSIVE_LOCK

class XactLockMgr : public LockMgr {
    RetStatus Lock(PdbId pdbId, Xid xid);         // 独占锁定（持有者：运行中事务）
    void      Unlock(PdbId pdbId, Xid xid);
    RetStatus Wait(PdbId pdbId, Xid xid);         // ROW_EXCLUSIVE 等待（等待者）
    RetStatus WaitForAnyTransactionEnd(PdbId *pdbIds, const Xid *xids, uint32 arrayLen);
    RetStatus TransferXactLockHolder(PdbId pdbId, Xid xid); // 锁持有者从旧线程转到新线程
};
```

**典型使用场景**：

```
场景1：PENDING_COMMIT 等待
  事务A读行 → XID=B → B处于PENDING_COMMIT状态
  → XidVisibleToSnapshot: 需要等B完成
  → XactLockMgr.Wait(pdb, xidB)    ← ROW_EXCLUSIVE 阻塞在 LOCKTAG_TRANSACTION(xidB)
  → B提交完成 → XactLockMgr.Unlock(pdb, xidB)
  → A被唤醒，重新检查可见性

场景2：写写冲突（行锁实现）
  事务A修改行 → td.m_lockerXid = xidA
  事务B尝试修改同一行 → 发现 lockerXid != INVALID → Wait(xidA)
  A提交后 → B被唤醒，重新尝试

场景3：异步回滚后恢复
  TransferXactLockHolder: RollbackWorker 接管事务锁后，
  旧线程的锁持有权转移给 RollbackWorker 线程
```

---

## 9. 死锁检测 — WaitForGraph

dstore 的死锁检测是**周期性的后台检测**（非即时检测），通过 WaitForGraph 找环。

```cpp
// include/lock/dstore_deadlock_detector.h

// 时间常量
constexpr uint64 DEADLOCK_DETECT_GLOBAL_CHECK_INTERVAL_US = 3 * 1000 * 1000; // 3秒全局间隔
constexpr uint64 DEADLOCK_DETECT_COLLECT_WAIT_MIN_TIME_US = 2 * 1000 * 1000; // 等待>2秒才纳入检测
```

**WaitForGraph 数据结构**：

```cpp
// include/lock/dstore_lock_wait_for_graph.h
class WaitForGraph {
    HTAB    *m_vertexs;          // 顶点哈希表（key=VertexTag）
    Vertex   m_vertexQueue;      // 待删除顶点队列
};

class Vertex {                   // 图中的顶点（代表一个等待线程）
    Edge       m_inEdgeList;     // 入边链表（哪些线程在等我释放锁）
    Edge       m_outEdgeList;    // 出边链表（我在等哪些线程）
    VertexTag *m_tag;            // 顶点唯一标识
    bool       m_isToBeRemoved;  // 已标记为非环顶点，待删除
};

class Edge {                     // 有向边：A → B 表示"A 等待 B 持有的锁"
    Vertex *m_peerVertex;        // 边的对端顶点
    Edge   *m_reverseEdge;       // 反向边引用
};

class ThreadVertex : public Vertex {
    uint32 m_threadCoreIndex;
    uint64 m_waitStartTime;      // 开始等待的时间戳（用于受害者选择）
    uint64 m_trxStartTime;       // 事务开始时间戳
};
```

**死锁检测完整流程**：

```
DeadlockDetector::RunDeadlockDetect():
  
  1. CompeteForDetectionExecutor()
     ├─ 多节点竞争：获取 LOCKTAG_DEADLOCK_DETECT 锁（NODELEVEL）
     └─ 只有一个节点执行检测
  
  2. CollectLockWaiters()
     ├─ 遍历所有线程的 DeadlockThrdState
     ├─ 找出等待时间 > COLLECT_WAIT_MIN_TIME_US 的线程
     └─ 加入 LockWaitingMap（按 LockTag 分组）
  
  3. CollectLockHoldersAndBuildGraph()
     ├─ 对每个等待线程 W：
     │   ├─ 找出持有 W 等待的锁的所有持有者 H
     │   ├─ AddWaitForEdge(W → H, lockTag, blockMask)
     │   └─ 构建 ThreadVertex 和 WaitLockEdge
     └─ soft edge：弱锁等待关系（非严格阻塞）
  
  4. DoesDeadlockExist()
     ├─ ScanVerticesNotInCycleAndPushIntoDeleteQueue()
     │   └─ 拓扑排序：出度=0 的顶点放入删除队列
     ├─ DeleteAllVerticesNotInCycle() ← 删除非环顶点
     └─ 剩余顶点 = 环中顶点
  
  5. ChooseVictimAndNotify()
     ├─ CycleIterator 遍历所有环顶点
     ├─ 选择最年轻（trxStartTime 最大）的事务作为受害者
     └─ NotifyVictim(): 设置 DeadlockThrdState.m_isDeadlock = true
  
  6. 受害者线程检测到 IsDeadlock() → 抛出死锁错误
```

**每线程死锁状态**：

```cpp
class DeadlockThrdState {
    uint64  m_startWaitTime;         // 锁等待开始时间
    uint64  m_waitingTransStartTime; // 当前事务开始时间
    char   *m_deadlockReport;        // 死锁报告文本（受害者专有）
    bool    m_isDeadlock;            // 是否被选中为死锁受害者
    int32   m_deadlockGraphVertexNum;
    LWLock  m_statLock;              // 保护此结构的锁
};
```

---

## 10. Tablespace 架构

### 系统内置 Tablespace

```cpp
// include/tablespace/dstore_tablespace.h
enum class TBS_ID : TablespaceId {
    INVALID_TABLE_SPACE_ID    = 0,
    GLOBAL_TABLE_SPACE_ID     = 1, // 跨PDB全局对象（系统表）
    DEFAULT_TABLE_SPACE_ID    = 2, // 默认用户表空间
    CATALOG_TABLE_SPACE_ID    = 3, // 系统目录
    UNDO_TABLE_SPACE_ID       = 4, // Undo 段专用
    TEMP_TABLE_SPACE_ID       = 5, // 临时表（无WAL）
    CATALOG_AUX_TABLE_SPACE_ID= 6, // 辅助系统目录
    UNLOGGED_TABLE_SPACE_ID   = 7  // 非日志表
};
```

### 类层次与成员

```
TablespaceMgr
  ├─ m_tablespaces[MAX_TABLESPACE_ITEM_CNT]: TableSpace*
  ├─ m_datafiles[MAX_DATAFILE_ITEM_CNT]: TbsDataFile*
  ├─ m_tablespaceLWLocks[MAX_TABLESPACE_ITEM_CNT]: LWLock  ← 每个 tablespace 独立锁
  ├─ m_datafileLWLocks[MAX_DATAFILE_ITEM_CNT]: LWLock      ← 每个 datafile 独立锁
  └─ m_tmpTbsHashTable: TbsTempBitmapPageHashTable*        ← 临时表 bitmap 内存表

TableSpace
  ├─ m_tablespaceId: TablespaceId
  ├─ m_files[MAX_SPACE_FILE_COUNT]: TbsDataFile*           ← 每种 ExtentSize 对应一个文件
  ├─ m_fileCountByType[EXTENT_TYPE_COUNT]: uint16          ← 各 ExtentSize 文件数
  ├─ m_allocExtents[EXTENT_TYPE_COUNT]: TbsAllocExtentContext
  └─ m_tbsCacheRWlock: RWLock                              ← 表空间缓存读写锁

TableSpaceInterface
  ├─ AllocExtent(extentSize, *newExtentPageId, *isReuseFlag)
  └─ FreeExtent(extentSize, extentPageId)
```

**每种 ExtentSize 单独一个数据文件**：

| ExtentSize | 大小 | 索引范围 |
|------------|------|---------|
| `EXT_SIZE_8` | 64 KB（8 页） | extent[0, 16) |
| `EXT_SIZE_128` | 1 MB（128 页） | extent[16, 144) |
| `EXT_SIZE_1024` | 8 MB（1024 页） | extent[144, 272) |
| `EXT_SIZE_8192` | 64 MB（8192 页） | extent[272, ∞) |

---

## 11. TbsDataFile — 文件内部结构

每个 `TbsDataFile` 对应一个物理磁盘文件，前 3 个 Block 固定为元数据页：

```
文件物理布局:
┌─────────────────────────────────────────────────────────┐
│ Block 0: TbsFileMetaPage   (TBS_FILE_META_PAGE)         │
│ Block 1: TbsSpaceMetaPage  (TBS_SPACE_META_PAGE)        │
│ Block 2: TbsBitmapMetaPage (TBS_BITMAP_META_PAGE)       │
│ Block 3+: 数据 Extent（每个 Extent 起点为一个 Bitmap 组）│
└─────────────────────────────────────────────────────────┘

const uint32_t INIT_FILE_PAGE_COUNT = 1024 * 8 = 8192 页 = 64 MB（初始大小）
FILE_EXTEND_SMALL_STEP = 16 * 1024 = 128 MB（文件 < 1 GB 时每次扩展）
FILE_EXTEND_BIG_STEP   = FILE_EXTEND_SMALL_STEP * 8 = 1024 MB（文件 ≥ 1 GB 时）
MAX_BITMAP_PAGE_COUNT  = 8448（最多 8448 个 Bitmap 页）
```

**Bitmap 管理**：

```
TbsDataFileBitmapMgr
  ├─ m_startPos[MAX_BITMAP_PAGE_COUNT]: FreeBitsSearchPos*
  └─ FreeBitsSearchPos { m_bitmapPageId, m_freeBitsSearchPos }

AllocExtent():
  1. 读 TbsBitmapMetaPage（定位 freeGroup）
  2. 遍历 FreeBitsSearchPos 找到空闲 bit
  3. CAS 设置 bit（原子标记 extent 已用）
  4. 写 WAL_TBS_BITMAP_SET_BIT
  5. 返回 PageId（bit 位置 × ExtentSize = 物理 PageId）
  
FreeExtent():
  1. LocateBitsPosByPageId（反查 bit 位置）
  2. 清除 bit
  3. 写 WAL_TBS_BITMAP_SET_BIT（value=0）
```

**TempTbsDataFile**（临时表特化）：
- Bitmap 页存内存而非 Buffer Pool（`TbsTempBitmapPageHashTable`）
- 无需写 WAL（崩溃后临时表丢失是允许的）
- 同样使用 LWLock 保护并发访问

---

## 12. Segment 架构

Segment 是连续 Extent 链的逻辑容器，段头页 = `m_segmentId`（第一个 PageId）：

```cpp
// include/tablespace/dstore_segment.h

// 四种 ExtentSize 对应的 extent 索引边界
const uint64 EXT_NUM_LINE[4] = {0, 16, 144, 272};
const ExtentSize EXT_SIZE_LIST[4] = {EXT_SIZE_8, EXT_SIZE_128, EXT_SIZE_1024, EXT_SIZE_8192};
// 含义：
//   第 0~15 号 extent：64K
//   第 16~143 号 extent：1M  （段容量达 128*1M=128MB 时从 64K 升级到 1M）
//   第 144~271 号 extent：8M
//   第 272+ 号 extent：64M

class Segment {
    PageId         m_segmentId;     // 段头页 ID（= 段 ID）
    BufMgrInterface *m_bufMgr;
    SegmentType    m_type;
    TablespaceId   m_tablespaceId;
    PdbId          m_pdbId;
    bool           m_isInitialized;
    bool           m_isDrop;
};

// 段类型继承链：
// Segment → DataSegment → HeapSegment（含 FSM）
//                       → IndexSegment（BTree 段）
// Segment → UndoSegment（通过 SegmentInterface.AllocUndoSegment 创建）
```

**Segment Extend 流程**：

```
Segment::Extend(extSize, *extMetaPageId):
  1. SegmentInterface::AllocExtent(pdbId, tablespaceId, extSize, *newExtentPageId)
     └─ TableSpace.AllocExtent(extSize, ...) → TbsDataFile.AllocExtent(...)
  2. InitExtMetaPage(newExtent, extSize)          ← 初始化 Extent 元数据页
  3. LinkNextExtInPrevExt(prevExtMeta, newExtMeta) ← 上一个 Extent 尾部链接新 Extent
  4. SegMetaLinkExtent(newExtMeta, extSize, isSecondExtent)  ← 更新段头
  5. WAL: WalRecordTbsSegmentAddExtent
```

---

## 13. HeapSegment + 分布式 FSM

`HeapSegment` 在 `DataSegment` 基础上增加了**多棵 FSM Tree** 的管理（分布式设计支持多节点）：

```cpp
// include/tablespace/dstore_heap_segment.h

constexpr uint16 INIT_FSM_NEED_PAGE_COUNT = 2; // fsmMetaPage + fsmRootPage

class HeapSegment : public DataSegment {
    FreeSpaceMapList *m_fsmList;    // FSM Tree 列表（支持多个分区 FSM）
    bool m_isFsmInitialized;
    
    // 分配数据页
    PageId GetPageFromFsm(uint32 spaceNeeded, uint16 retryTime);
    PageId GetNewPage(const PageId fsmMetaPageId = INVALID_PAGE_ID);
    
    // 更新 FSM 空间信息
    RetStatus UpdateFsm(const PageId &dataPageId, uint32 remainSpace);
    RetStatus UpdateFsm(const FsmIndex &fsmIndex, uint32 remainSpace);
    
    // FSM 资源回收与重新分配（多节点场景）
    RetStatus RecycleUnusedFsm();
};
```

**FSM 搜索重试机制**：

```cpp
// include/fsm/dstore_partition_fsm.h
constexpr uint16 MAX_FSM_SEARCH_RETRY_TIME    = 1000; // 超过 1000 次才 Extend
constexpr uint16 FSM_SEARCH_UPGRADE_RETRY_TIME = 100; // 100 次后提升空间要求级别
```

**完整的"从 SQL 到物理页"分配链**：

```
INSERT 需要一个页面:
  HeapSegment::GetPageFromFsm(spaceNeeded=100, retryTime=0)
    └─ PartitionFreeSpaceMap.GetPage(spaceNeeded=100)
        ├─ 命中 FSM → 返回 PageId         ← 快速路径
        └─ 100次后 retryTime++ → 降低空间要求（搜索更大范围）

  未找到 → HeapSegment::GetNewPage(fsmMetaPageId)
    └─ DataSegment::PrepareFreeDataPages()
        └─ DataSegment::DoExtend(targetExtSize)
            └─ Segment::Extend(extSize) → TableSpace.AllocExtent()
                → TbsDataFile.AllocExtent() [bitmap操作]
    → InitNewDataPageWithFsmIndex(count, pageIdList, ...) ← 初始化新页
    → AddDataPagesToFsm(fsm, newPages, ...) ← 登记到 FSM
    → 返回其中一个 PageId
```

**FSM 回收流程（多节点场景）**：

```
HeapSegment::RecycleUnusedFsm():
  1. FindRecyclableFsm()：扫描各 FSM Tree 的使用率
  2. OrderRecyclableFsmsBasedOnNumTotalPages()：按持有页数排序（min-heap）
  3. OrderNodesBasedOnNumTotalPages()：找到页面最少的节点
  4. ReassignRecyclableFsm()：将冷 FSM 移交给"最饥渴"的节点
  5. WAL: WalRecordTbsSegMetaRecycleFsmTree / WalRecordTbsSegMetaAddFsmTree
```

---

## 14. Tablespace WAL

所有 Tablespace 物理操作都有对应 WAL 记录，确保崩溃恢复：

```
WalRecordTbs : WalRecordForPage     ← 所有 TBS 物理 WAL 的基类
  ├─ WalRecordTbsInitBitmapMetaPage   : totalBlockCount + extentSize
  ├─ WalRecordTbsBitmapSetBit         : allocatedExtentCount + startBitPos + value
  ├─ WalRecordTbsAddBitmapPages       : groupCount + groupIndex + groupFirstPage + groupFreePage
  ├─ WalRecordTbsUpdateFirstFreeBitmapPageId : groupIndex + firstFreePageNo
  ├─ WalRecordTbsExtendFile           : fileId + totalBlockCount
  ├─ WalRecordTbsInitOneDataPage      : dataPageType + curFsmIndex
  ├─ WalRecordTbsInitOneBitmapPage    : pageType + curDataPageId
  ├─ WalRecordTbsSegmentAddExtent     : extMetaPageId + extSize + extUseType(数据/FSM/Undo)
  ├─ WalRecordTbsDataSegmentAddExtent : extSize + addedPageId + isReUsedFlag
  ├─ WalRecordTbsSegmentUnlinkExtent  : nextExtMeta + unlinkExtMeta + unlinkExtSize + extUseType
  ├─ WalRecordTbsSegMetaAddFsmTree    : fsmMetaPageId + assignedNodeId + fsmId
  ├─ WalRecordTbsSegMetaRecycleFsmTree: fsmMetaPageId + assignedNodeId + fsmId
  ├─ WalRecordTbsFsmMetaUpdateFsmTree : fsmExtents + numFsmLevels + mapCount[] + currMap[]
  └─ WalRecordTbsInitFreeSpaceMap     : fsmRootPageId + accessTimestamp

WalRecordTbsLogical : WalRecord      ← 逻辑 WAL（DDL 级别，影响 ControlFile）
  ├─ WalRecordTbsCreateTablespace    : tbsMaxSize + ddlXid
  ├─ WalRecordTbsCreateDataFile      : fileId + fileMaxSize + extentSize + ddlXid
  ├─ WalRecordTbsAddFileToTbs        : hwm + fileId + slotId + ddlXid
  ├─ WalRecordTbsDropTablespace      : hwm + ddlXid
  ├─ WalRecordTbsDropDataFile        : hwm + fileId + slotId + ddlXid
  └─ WalRecordTbsAlterTablespace     : tbsMaxSize + ddlXid
```

**逻辑 WAL vs 物理 WAL 的区别**：
- 物理 WAL（`WalRecordTbs`）：Redo 时直接 memcpy 到 Buffer Page
- 逻辑 WAL（`WalRecordTbsLogical`）：Redo 时调用高级接口（创建/删除文件系统对象），含 `ddlXid` 支持崩溃后的两阶段恢复

---

## 15. 跨模块连接

### 连接1：Lock + Transaction（XactLockMgr.Wait）

```
PENDING_COMMIT 等待场景：
  XidVisibleToSnapshot: xidStatus == PENDING_COMMIT
    → XactLockMgr.Wait(pdb, xid)
        → LockInMainLockTable(LOCKTAG_TRANSACTION, ROW_EXCLUSIVE)
        → 阻塞，直到 xid 拥有者调用 Unlock(xid)
  CommitInternal 完成: XactLockMgr.Unlock(xid)
    → LockEntry.AdvanceWaitingQueue() → 唤醒所有等待者
```

### 连接2：Lock + Tablespace（扩展锁）

```
Segment Extend 并发控制:
  TableSpace.AllocExtent():
    LockTag.SetTableExtensionLockTag(pdbId, segmentId)  ← LOCKTAG_TABLE_EXTEND
    LockMgr.Lock(tag, EXCLUSIVE)
    
    AllocExtentFromBitmapPage()  ← 读写 Bitmap（现在是独占的）
    
    LockMgr.Unlock(tag, EXCLUSIVE)
  
  多线程同时 INSERT 时：只有一个线程能持有 TABLE_EXTEND 锁，
  其他线程阻塞等待，确保 Extent 分配序列化
```

### 连接3：Lock + Buffer（LWLock 基础）

```
所有 Buffer 分区锁都是 LWLockPadded：
  BufTable.m_partitionLocks[4096]   ← 哈希分区 LWLock
  LockHashTable.m_partitionLocks[]  ← 锁哈希分区 LWLock
  TablespaceMgr.m_tablespaceLWLocks[] ← 表空间 LWLock
  TablespaceMgr.m_datafileLWLocks[]   ← 数据文件 LWLock
  UndoZone.m_currentInsertUndoPageBuf ← Undo 页 LWLock
```

### 连接4：Tablespace + Segment + Heap（物理存储链路）

```
表创建时：
  DDL → TableSpace.AllocExtent(EXT_SIZE_8)  ← 分配第一个 Extent
      → Segment.Init()                      ← 加载段头页
      → HeapSegment.InitFreeSpaceMap()      ← 创建初始 FSM（2页）
      → WAL: WalRecordTbsInitDataSegment + WalRecordTbsInitHeapSegment

INSERT 写入时：
  HeapSegment.GetPageFromFsm(spaceNeeded)   ← 从 FSM 找可用页
  HeapPage.AddTuple(...)                    ← 写入数据
  HeapSegment.UpdateFsm(pageId, newSpace)   ← 更新 FSM 记录剩余空间
  WAL: WalRecordTbsFsmMetaUpdateFsmTree
```

### 连接5：Tablespace WAL + 崩溃恢复

```
崩溃恢复（WalManager.Recovery）:
  ParallelRedo Worker:
    WalType == WAL_TBS_*:
      WalRecordTbs.RedoTbsRecord(ctx, record, bufDesc)
        ├─ WAL_TBS_BITMAP_SET_BIT → BitmapPage.SetBit(bitNo, value)
        ├─ WAL_TBS_EXTEND_FILE   → 扩展文件到 totalBlockCount
        ├─ WAL_TBS_INIT_DATA_PAGE → HeapPage 初始化（设置 fsmIndex）
        └─ WAL_TBS_SEGMENT_ADD_EXTENT → SegMetaPage.LinkExtent()
  
  逻辑 WAL Redo（DDL 恢复）:
    WalRecordTbsCreateDataFile.Redo() → CreateFile(StoragePdb*) ← 重新创建物理文件
```

---

## 16. 快速参考

### Lock Manager 关键常量

| 常量 | 值 | 说明 |
|------|----|------|
| `FAST_LOCK_ENTRY_MAP_MAX_SLOT` | 512 | 线程本地快速锁槽数 |
| `FAST_PATH_STRONG_LOCK_HASH_PARTITIONS` | 1024 | 强锁标记分区数 |
| `MAX_SKIPLIST_LEVEL` | 8 | 跳表最大层数 |
| `MAX_SIMUL_LWLOCKS` | 4224 | 单线程同时持有 LWLock 上限 |
| `DEADLOCK_DETECT_GLOBAL_CHECK_INTERVAL_US` | 3,000,000 | 3秒全局检测间隔 |
| `DEADLOCK_DETECT_COLLECT_WAIT_MIN_TIME_US` | 2,000,000 | 纳入检测的最短等待时间 |
| `DEADLOCK_DETECT_PRINT_WFG_MAX_VERTEX_NUM` | 50 | 打印 WFG 的顶点上限 |

### Tablespace 关键常量

| 常量 | 值 | 说明 |
|------|----|------|
| `INIT_FILE_PAGE_COUNT` | 8192 页 (64MB) | 文件初始大小 |
| `FILE_EXTEND_SMALL_STEP` | 16×1024 页 (128MB) | 文件 < 1GB 时每次扩展 |
| `FILE_EXTEND_BIG_STEP` | 8×SMALL = 1024MB | 文件 ≥ 1GB 时每次扩展 |
| `MAX_BITMAP_PAGE_COUNT` | 8448 | 单文件最大 Bitmap 页数 |
| `INIT_FSM_NEED_PAGE_COUNT` | 2 | 新建 FSM Tree 所需初始页数 |
| `MAX_FSM_SEARCH_RETRY_TIME` | 1000 | FSM 搜索扩展前最大重试次数 |
| `FSM_SEARCH_UPGRADE_RETRY_TIME` | 100 | 100次后提升空间级别 |
| `SEGMENT_HEAD_MAGIC` | 0x44414548544e454d | 段头魔数 |
| `EXTENT_SIZE_COUNT` | 4 | 支持 4 种 ExtentSize |

### 锁模式冲突矩阵（简化版）

```
          AS  RS  RE  SUE  S   SRE  E   AE
AS        .   .   .   .    .   .    .   X
RS        .   .   .   .    .   .    X   X
RE        .   .   .   .    X   X    X   X
SUE       .   .   .   X    X   X    X   X
S         .   .   X   X    .   X    X   X
SRE       .   .   X   X    X   X    X   X
E         .   X   X   X    X   X    X   X
AE        X   X   X   X    X   X    X   X

AS=ACCESS_SHARE, RS=ROW_SHARE, RE=ROW_EXCLUSIVE,
SUE=SHARE_UPDATE_EXCLUSIVE, S=SHARE, SRE=SHARE_ROW_EXCLUSIVE,
E=EXCLUSIVE, AE=ACCESS_EXCLUSIVE
X=冲突, .=兼容
```

### Segment ExtentSize 升级策略

```
Extent 编号 → ExtentSize 对应（EXT_NUM_LINE[]）：
  [0,  16)  → EXT_SIZE_8    (64KB)   = 16 × 64KB  = 1 MB
  [16, 144) → EXT_SIZE_128  (1MB)    = 128 × 1MB  = 128 MB
  [144,272) → EXT_SIZE_1024 (8MB)    = 128 × 8MB  = 1024 MB
  [272, ∞)  → EXT_SIZE_8192 (64MB)   = ∞

随段成长自动从小 Extent 升级到大 Extent，减少 Tablespace 操作次数
```

---

## Day 9 总结

**Lock Manager 三层**：LWLock（内部数据结构）→ TableLockMgr（表对象，快速路径优化弱锁）→ XactLockMgr（事务等待）。

**死锁检测**：非即时检测，周期 3 秒；等待 > 2 秒的线程才纳入 WaitForGraph；选最年轻事务为受害者。

**物理存储链路**：TablespaceMgr（多表空间）→ TableSpace（每 ExtentSize 一个文件）→ TbsDataFile（Bitmap 追踪可用 Extent）→ Segment（有序 Extent 链，从 64K 逐步升到 64M）→ HeapSegment（+ 多 FSM Tree）→ HeapPage（实际数据）。

**所有操作都有 WAL 保护**：物理 WAL（`WalRecordTbs`）保护 Bitmap/段头/FSM；逻辑 WAL（`WalRecordTbsLogical`）保护 DDL 操作（含 `ddlXid` 支持两阶段恢复）。

---

> Day 10 综合串联：把 Day 1～9 所有模块串联，梳理完整的 INSERT/SELECT/UPDATE 跨模块时序、多PDB隔离边界、以及扩展模块（逻辑复制、Flashback）与核心模块的接触点。
