# Day 1：全局框架 + 公共基础

## 学习目标
建立对整个 dstore 存储引擎的鸟瞰图，理解公共基础设施，为后续模块学习打下基础。

---

## 1. 存储引擎整体架构

### 1.1 核心骨架：两层管理者

dstore 的所有资源被组织成两层：

```
g_storageInstance   ← 进程级全局单例（StorageInstance）
│
│  ← 所有PDB共享的资源
├── BufMgr           缓冲池（统一管理所有数据库的页面缓存）
├── LockMgr          锁管理器（行锁、死锁检测）
├── TableLockMgr     表级锁
├── XactLockMgr      事务锁
├── CsnMgr           CSN管理（全局提交序号，MVCC核心）
├── MemoryMgr        内存管理
│
│  ← 每个数据库独立的资源
└── StoragePdb[0..N]   每个PDB（Pluggable Database）一个实例
      ├── TransactionMgr  事务管理：XID分配、提交、快照
      ├── UndoMgr          Undo管理：历史版本、回滚
      ├── WalManager       WAL管理：预写日志、崩溃恢复
      └── TablespaceMgr    表空间：页面分配、FSM
```

**关键设计原则**：
- Buffer Pool **全局共享**：所有PDB的页面统一缓存，集中调度内存
- CSN **全局单调递增**：跨PDB的事务可见性用同一把尺子衡量
- Transaction / Undo / WAL **每PDB独立**：故障隔离，互不影响

### 1.2 什么是 PDB

PDB = **Pluggable Database**（可插拔数据库）。

dstore 支持在一个存储引擎进程中托管多个完全独立的数据库，每个数据库就是一个 PDB。类比理解：类似 Oracle Multitenant 的 PDB，或者 PostgreSQL 的多个 database。

```cpp
// include/framework/dstore_pdb.h
class StoragePdb {
    TransactionMgr *m_transactionMgr;  // 该PDB的事务管理
    UndoMgr        *m_undoMgr;         // 该PDB的Undo
    WalManager     *m_walMgr;          // 该PDB的WAL流
    TablespaceMgr  *m_tablespaceMgr;   // 该PDB的存储空间
    ControlFile    *m_controlFile;     // 该PDB的控制文件
};
```

每个 PDB 有独立的 WAL 流（WalId），因此崩溃恢复可以按PDB粒度独立进行。

### 1.3 引擎生命周期

```
首次建库：
  Bootstrap(guc)
    └── 初始化共享资源（BufMgr、LockMgr、CsnMgr）
    └── 创建模板PDB（template1）

正常启动：
  StartupInstance(guc)
    ├── BufMgrInit()        ← 分配缓冲池内存
    ├── InitLockMgr()       ← 初始化锁管理器
    ├── InitCsnMgr()        ← 启动CSN计数器
    └── 对每个PDB：
          ├── InitWalMgr()          ← 打开/回放WAL文件
          ├── InitTransactionMgr()  ← 加载事务状态
          └── InitUndoMgr()         ← 挂载Undo空间

正常关闭：
  ShutdownInstance()
    └── Checkpoint + 刷脏页 + 关WAL流 + 释放所有资源
```

---

## 2. 基础数据类型（dstore_datatype.h）

所有模块共用的基础类型都定义在 `include/common/dstore_datatype.h`。

### 2.1 整数类型别名

```cpp
using int8   = int8_t;    using uint8   = uint8_t;
using int16  = int16_t;   using uint16  = uint16_t;
using int32  = int32_t;   using uint32  = uint32_t;
using int64  = int64_t;   using uint64  = uint64_t;
```

整个代码库统一用这套类型，不用 `long`、`short` 等平台相关类型。

### 2.2 核心业务 ID 类型

```cpp
using WalId      = uint64;   // WAL流标识符（每个PDB一个WAL流）
using TimeLineID = uint16;   // 时间线ID（主备切换时递增）
```

其他核心ID（在各自模块头文件中定义）：

| 类型 | 位宽 | 含义 |
|------|------|------|
| `XID` (TransactionId) | uint64 | 事务ID：高20bit=zoneId，低44bit=逻辑槽位 |
| `CommitSeqNo` (CSN) | uint64 | 提交序号，全局单调递增，MVCC核心 |
| `PLSN` | uint64 | Physical LSN，WAL流内字节偏移 |
| `ItemPointerData` | pageId+offset | 行的物理位置（页号+槽位号） |
| `UndoRecPtr` | 64bit | Undo记录位置：16bit fileId + 32bit pageId + 16bit offset |

### 2.3 时间戳

```cpp
typedef int64 TimestampTz;  // 微秒精度，从2000-01-01起算
```

`GetCurrentTimestamp()` 通过 `gettimeofday()` 获取，存储为 int64 微秒数。

### 2.4 变长字段编码（varlena）

代码中大量出现 `VarAttIs4B()`、`DstoreVarSize()` 等函数，它们用于处理文本/varchar等变长字段的头部格式。

dstore 沿用 PostgreSQL 的 varlena 编码：

```
4字节头格式（普通，最大1GB）：
  [va_header: 32bit] [data...]
  高30bit = 数据长度，低2bit：00=普通，10=压缩

1字节头格式（短，最大127字节）：
  [va_header: 8bit] [data...]
  bit0=1 表示短格式，高7bit=长度

外部大对象格式（DLOB）：
  [0x01] [tag=38] [VarattLobLocator {relid, rawsize, extsize, ctid}]
  通过 ctid 指向外部存储的大对象页面
```

判断方法：
```cpp
VarAttIs4BU(ptr)   → 普通4字节头（未压缩）
VarAttIs4BC(ptr)   → 压缩4字节头
VarAttIs1B(ptr)    → 1字节短头
VarAttIs1BE(ptr)   → 外部存储（DLOB）
```

---

## 3. 内存管理（dstore_mctx.h）

dstore 使用**内存上下文（MemoryContext）**管理内存，杜绝直接 malloc/free。

### 3.1 内存上下文树

```
TopMemoryContext（进程级根节点）
  ├── InstanceMemoryContext    ← StorageInstance 持有
  │     ├── BufferMgrMemoryContext
  │     ├── LockMgrMemoryContext
  │     └── TransactionMemoryContext
  │
  ├── ThreadMemoryContext      ← 每个线程独立
  │     └── PerQueryMemoryContext  ← 每次查询重置
  │
  └── SessionMemoryContext     ← 会话级
```

上下文树的作用：**批量释放**。释放父节点时，所有子节点自动释放。例如查询结束只需重置 `PerQueryMemoryContext`，不需要逐一 free。

### 3.2 内存上下文类型

```cpp
enum class MemoryContextType {
    THREAD_CONTEXT,   // 线程级，只被当前线程使用（无锁）
    SESSION_CONTEXT,  // 会话级，不同线程串行访问
    STACK_CONTEXT,    // 栈式分配，不支持单独free
    SHARED_CONTEXT,   // 共享上下文，多线程并发访问（有锁）
    MEMALIGN_CONTEXT, // 对齐内存专用
};
```

### 3.3 常用内存操作宏

```cpp
// 在当前上下文分配
DstorePalloc(size)         // 分配，不清零
DstorePalloc0(size)        // 分配并清零（常用）

// 切换上下文
AutoMemCxtSwitch(ctx)      // RAII，析构时自动恢复

// 释放
DstorePfreeExt(ptr)        // 释放并置nullptr（安全释放）

// 创建/销毁上下文
DstoreAllocSetContextCreate(parent, name, ...)
DstoreMemoryContextDelete(ctx)
```

### 3.4 BaseObject：所有类的基类

```cpp
class BaseObject {
    // 重载 operator new → 走 DstoreMemoryContextAlloc
    // 重载 operator delete → 走 DstorePfree
};
```

代码里所有重要的类（BufferMgr、TransactionMgr、StoragePdb 等）都继承自 `BaseObject`，确保内存分配统一走上下文管理。

创建对象的惯用写法：
```cpp
auto *obj = DstoreNew(memContext) MyClass(args...);
// 等价于：new(memContext, __FILE__, __LINE__) MyClass(args...)
```

---

## 4. 并发原语（concurrent/）

### 4.1 原子操作

```cpp
// volatile 整数类型
typedef volatile uint32 gs_atomic_uint32;
typedef volatile uint64 gs_atomic_uint64;

// 常用原子操作
GsAtomicAdd32(ptr, inc)           // 原子加 32bit
GsAtomicAdd64(ptr, inc)           // 原子加 64bit
GsAtomicAddFetchU32(ptr, inc)     // 原子加并返回新值（uint32）
GsAtomicAddFetchU64(ptr, inc)     // 原子加并返回新值（uint64）
```

内存序（MemoryOrder）：
```cpp
MEMORY_ORDER_RELAXED   // 无屏障（最快，用于统计计数）
MEMORY_ORDER_ACQUIRE   // 读屏障（与 RELEASE 配对）
MEMORY_ORDER_RELEASE   // 写屏障
MEMORY_ORDER_ACQ_REL   // 读写屏障（CAS常用）
MEMORY_ORDER_SEQ_CST   // 全序（最强，最慢）
```

### 4.2 自旋锁（DstoreSpinLock）

```cpp
// include/lock/dstore_s_lock.h
struct DstoreSpinLock {
    std::atomic<uint8> lock;   // RELEASE=0 / LOCKED=1
    int spinsPerDelay;         // 自旋次数（自适应调整）
};
```

**自适应自旋**：先空转 `spinsPerDelay` 次，若仍未获得则调用 `PerformSpinDelay()` 让出CPU，避免忙等浪费。

使用场景：极短临界区（< 几十条指令），如 BufferDesc.state 的 CAS 操作。

### 4.3 轻量级锁（LWLock）

```cpp
// include/lock/dstore_lwlock.h
typedef struct LWLock {
    int              spinsPerDelay;
    uint16           groupId;
    gs_atomic_uint64 state;     // 64bit状态：记录共享/独占持有者数
    dlist_head       waiters;   // 等待队列
} LWLock;

enum LWLockMode {
    LW_EXCLUSIVE,   // 独占（写）
    LW_SHARED,      // 共享（读）
};
```

**LWLock vs SpinLock**：

| | SpinLock | LWLock |
|--|---------|--------|
| 持有时间 | 极短（纳秒级） | 较短（微秒级） |
| 等待方式 | 忙等自旋 | 先自旋，超时后入队睡眠 |
| 读写分离 | 否（互斥） | 是（SHARED/EXCLUSIVE） |
| 使用场景 | Buffer哈希桶头、引用计数 | Buffer分区锁、PDB锁、WAL流锁 |

常用宏：
```cpp
DstoreLWLockAcquire(lock, LW_EXCLUSIVE)   // 获取独占锁
DstoreLWLockAcquire(lock, LW_SHARED)      // 获取共享锁
LWLockRelease(lock)                        // 释放
```

---

## 5. 三条主线回顾（07_system_integration.md 精华）

Day 1 最重要的认知——dstore 所有代码都围绕这三条主线运转：

```
写入主线（INSERT/UPDATE/DELETE）：
  事务拿XID → Buffer读页 → Heap分配TD → 写Undo → SetTd
  → 写数据 → 写索引 → 生成WAL → MarkDirty → Commit

读取主线（SELECT）：
  拿快照(CSN) → 索引查找 → Buffer读页
  → GetVisibleTuple(MVCC判断) → 必要时 ConstructCrTuple(Undo回溯)

崩溃恢复主线（重启）：
  扫描WAL建脏页集 → ParallelRedo重放
  → 找未提交事务 → Undo链逆序回滚
```

详细内容见 `docs/training/07_system_integration.md`。

---

## 6. Day 1 核心知识速查表

| 概念 | 位置 | 一句话 |
|------|------|--------|
| `g_storageInstance` | `include/framework/dstore_instance.h:463` | 全局单例，所有模块的访问入口 |
| `StoragePdb` | `include/framework/dstore_pdb.h:155` | 每个数据库的资源容器 |
| `WalId = uint64` | `include/common/dstore_datatype.h:113` | WAL流唯一标识 |
| `DstorePalloc(size)` | `include/common/memory/dstore_mctx.h:160` | 在当前内存上下文分配 |
| `BaseObject` | `include/common/memory/dstore_mctx.h:186` | 所有类的基类，重载new/delete |
| `DstoreSpinLock` | `include/lock/dstore_s_lock.h:51` | 自旋锁，极短临界区 |
| `LWLock` | `include/lock/dstore_lwlock.h:78` | 轻量锁，支持读写分离 |
| `GsAtomicAddFetchU64` | `include/common/concurrent/dstore_atomic.h:562` | 原子加并取新值 |
| `MemoryContextType` | `include/common/memory/dstore_mctx.h:69` | 内存上下文类型枚举 |

---

## 7. 下一步（Day 2 预告）

Day 2 将深入**页面结构和Tuple格式**——这是所有数据在磁盘上的物理布局：

- `Page` → `DataPageHeader` → TD数组 → ItemId数组 → Free Space → Tuple数据
- `HeapDiskTuple`：m_tdId、m_tdStatus、m_linkInfo（BigTuple标记）等字段
- `ItemPointerData`：如何用 pageId + offset 定位一行
