# Day 3：Buffer Manager

## 学习目标
理解所有页面读写的门户：Buffer Pool 如何缓存页面、如何防止脏页早于 WAL 写盘、如何通过 LRU 调度内存，以及后台写线程的工作原理。

---

## 1. 核心数据结构

### 1.1 BufferTag：页面的唯一标识

```cpp
// include/buffer/dstore_buf.h:301
struct BufferTag {
    PageId pageId;  // fileId(16bit) + blockId(32bit)
    int16  padding;
    PdbId  pdbId;   // 所属 PDB（多租户）
};
// sizeof(BufferTag) == 12B
```

`BufferTag = pdbId + fileId + blockId`，唯一标识一个缓冲页面。哈希表以此为 key。

### 1.2 BufferDesc：每个缓冲页面的描述符

```cpp
// include/buffer/dstore_buf.h:650
struct BufferDesc : public BaseObject {
    BufBlock        bufBlock;      // 指向实际 8KB 页面数据
    BufferTag       bufTag;        // 该 buffer 缓存的页面标识
    BufferDescController *controller; // I/O 锁、CR 锁、lastModifyTime
    gs_atomic_uint64 state;        // 64bit 原子状态（核心！）
    LruNode         lruNode;       // LRU 链表节点
    CRInfo          crInfo;        // 关联的 CR（Consistent Read）页面信息
    LWLock          contentLwLock; // 页面内容读写锁

    std::atomic<BufferDesc *> nextDirtyPagePtr[5]; // 脏页队列（5条队列）
    std::atomic<uint64>       recoveryPlsn[5];     // 各队列的最小 recovery PLSN
    PageVersion     pageVersionOnDisk;             // 最后一次写盘时的 glsn/plsn
    uint64          fileVersion;                   // 文件版本（文件删除检查）
};
```

### 1.3 state：64bit 原子状态变量

```cpp
// include/buffer/dstore_buf.h:45-63
// 低 32 bit：refcount（引用计数，支持 ~40亿并发 pin）
// 高 32 bit：各类标志位

constexpr uint64 BUF_REFCOUNT_ONE    = 1;               // 引用计数加 1
constexpr uint64 BUF_REFCOUNT_MASK   = ((1ULL<<32)-1);  // 取低32位

// 关键标志位（bit position）：
enum BufFlagBit : uint8 {
    BUF_LOCKED_BIT          = 32,  // header 锁（保护 state 字段修改）
    BUF_CONTENT_DIRTY_BIT   = 33,  // 强脏标记，必须写回磁盘
    BUF_VALID_BIT           = 34,  // 数据有效，可安全读取
    BUF_TAG_VALID_BIT       = 35,  // bufTag 已关联（在哈希表中有对应条目）
    BUF_IO_IN_PROGRESS_BIT  = 36,  // 正在进行 I/O（读或写）
    BUF_IO_ERROR_BIT        = 37,  // 上次 I/O 失败
    BUF_HINT_DIRTY_BIT      = 38,  // 弱脏标记（Hint bit，不保证落盘）
    BUF_CR_PAGE_BIT         = 40,  // 这是一个 CR（一致性读）页面
    BUF_IS_WRITING_WAL_BIT  = 41,  // 正在写 WAL
};
```

**state 字段的并发操作模式**：

| 操作 | 方式 |
|------|------|
| 修改 refcount | CAS（`state += BUF_REFCOUNT_ONE`） |
| 修改标志位 | LockHdr()/UnlockHdr()（bit32 自旋锁保护） |
| 读取 state | `GsAtomicReadU64(&state)`（无锁） |

### 1.4 LruNode：LRU 列表节点

```cpp
// include/buffer/dstore_buf.h:451
enum LruNodeType : uint8 {
    LN_CANDIDATE = 0,   // 待淘汰候选（最冷）
    LN_LRU       = 1,   // 普通 LRU 区
    LN_HOT       = 2,   // HOT 区（最热，频繁访问）
    LN_PENDING   = 3,   // 新分配，尚未加入任何列表
    LN_TO_BE_INVALIDATED = 4, // 待废弃
};

struct LruNode {
    dlist_node m_list_node;        // 双向链表节点
    void      *m_value;            // 指向所属 BufferDesc
    uint32     lruIndex;           // 所在 LRU 分区的 index
    std::atomic<LruNodeType> m_type;
    std::atomic<uint8>       m_usage; // 访问频次计数
};
```

### 1.5 BufferDescController：独立的 I/O 控制器

```cpp
// include/buffer/dstore_buf.h:524
struct BufferDescController {
    LWLockPadded ioInProgressLwlock;  // I/O 进行中锁（防止并发 I/O）
    LWLockPadded crAssignLwlock;      // CR 槽分配锁
    std::atomic<uint64> lastPageModifyTime; // 最后修改时间（CR 超时判断）
};
```

---

## 2. 哈希表（BufTable）

### 2.1 结构

```cpp
// include/buffer/dstore_buf_table.h:28
const long NUM_BUFFER_PARTITIONS = 4096;  // 4096 个哈希分区

class BufTable {
    // 内部：DstoreHtab（开链哈希表）+ 4096 个 LWLock（每分区一把）
    LWLockPadded *m_bufMappingLwlock;  // 分区锁数组
};
```

**核心接口**：
```cpp
uint32      BufTable::GetHashCode(const BufferTag *bufTag);  // 计算分区号
BufferDesc *BufTable::LookUp(const BufferTag *, uint32 hashCode);  // 查找（需持有分区锁）
BufferDesc *BufTable::Insert(const BufferTag *, uint32 hashCode, BufferDesc *);  // 插入
void        BufTable::Remove(const BufferTag *, uint32 hashCode);   // 删除

// 分区锁操作
void BufTable::LockBufMapping(uint32 hashCode, LWLockMode mode);
void BufTable::UnlockBufMapping(uint32 hashCode);
```

**查找路径（无竞争情况）**：
```
GetHashCode(bufTag) → partition = hashCode % 4096
LockBufMapping(hashCode, LW_SHARED)
  → LookUp(bufTag, hashCode)        ← O(1) 平均
UnlockBufMapping(hashCode)
```

4096 个分区的设计目标：缓存命中时，4096 个并发查找几乎不产生锁竞争。

---

## 3. LRU 三层淘汰结构

### 3.1 三层链表

```
BufLruListArray（多分区，每分区一个 BufLruList）
  └── BufLruList
        ├── HOT_LIST    （热页，频繁访问，m_usage 高）
        ├── LRU_LIST    （温冷页，正常访问路径）
        └── CANDIDATE_LIST（待淘汰，m_usage 低）
```

**默认 HOT 区占比**：50%（`BUFLRU_DEFAULT_HOT_RATIO = 0.5`）

### 3.2 页面在 LRU 中的流转

```
新分配 → CANDIDATE_LIST（冷端）
                 ↑
           MarkDirty 或 被多次访问（usage 增加）
                 │
               LRU_LIST（中间区）
                 │
           访问频次达到阈值（usage 满）
                 │
             HOT_LIST（热端）
                 │
         长时间未访问（usage 衰减）
                 ↓
           CANDIDATE_LIST → 被驱逐
```

**访问时的行为**：每次 `Pin` 一个 buffer 会调用 `LruNode::IncUsage()`，增加使用计数。后台 LRU 扫描线程定期将 usage 衰减，将冷却的 buffer 从 HOT → LRU → CANDIDATE 降级。

### 3.3 驱逐（Evict）

当需要新的空闲 buffer 时，`GetFreeBuffer()` 从 CANDIDATE_LIST 末尾取候选页：
1. 若候选页是**脏页** → `WriteBlock()` 先写盘，再复用
2. 若候选页有 **CR 子页** → 先释放 CR 页，再复用
3. 若候选页 **refcount > 0**（仍被 Pin）→ 跳过，继续扫描

---

## 4. Pin / Unpin：双层引用计数

### 4.1 设计原理

直接用全局原子计数性能较差（多线程争抢）。dstore 采用**双层计数**：

```
层1（线程私有）：PrivateRefCountEntry（无锁，O(1)）
层2（全局共享）：BufferDesc.state 低32位（仅首次 pin 时原子操作）
```

**Pin 操作**：
```
1. 检查线程私有表：若已有记录，仅递增 privateRef->refcount（无原子操作）
2. 若首次 pin（privateRef 不存在）：
   - 原子 CAS：state += BUF_REFCOUNT_ONE
   - 新建 PrivateRefCountEntry
```

**Unpin 操作（Release）**：
```
1. 递减 privateRef->refcount
2. 若 privateRef->refcount == 0（最后一次 unpin）：
   - 原子操作：state -= BUF_REFCOUNT_ONE
   - 删除 PrivateRefCountEntry
```

**核心规则**：
- **只有全局 refcount == 0 的 buffer 才能被 LRU 驱逐**
- 持有 pin 期间，buffer 不会被移走

---

## 5. Read() 完整调用链

```
BufMgrInterface::Read(pdbId, pageId, LWLockMode mode)
│
├─ 步骤1：LookupBuffer(bufTag)
│    ├── GetHashCode(bufTag) → partition
│    ├── LockBufMapping(SHARED)
│    ├── BufTable::LookUp() → BufferDesc* or nullptr
│    └── 若找到：Pin(bufferDesc)，UnlockBufMapping，跳到步骤4
│
├─ 步骤2（Miss）：AllocBufferForBaseBuffer(bufTag)
│    ├── GetFreeBuffer()           ← 从 CANDIDATE_LIST 找空闲 buffer
│    │     └── MakeBaseBufferFree()  （若脏则先刷盘，若有 CR 子页先释放）
│    ├── LockBufMapping(EXCLUSIVE)  ← 独占锁，准备插入哈希表
│    ├── 再次 LookUp()             ← 二次检查（防止竞争插入）
│    │    └── 若他人已插入：复用已插入的 buffer（otherInsertHash=true）
│    ├── BufTable::Insert()         ← 插入哈希表
│    ├── 设置 bufTag，设置 BUF_TAG_VALID
│    └── UnlockBufMapping
│
├─ 步骤3（新分配，需要读磁盘）：
│    ├── StartIo(bufferDesc, true)  ← 设置 BUF_IO_IN_PROGRESS
│    ├── ReadBlock()                ← VFS::ReadPageSync()，从磁盘读 8KB
│    └── TerminateIo(clearDirty=false, setBits=BUF_VALID)
│         └── 清除 BUF_IO_IN_PROGRESS，设置 BUF_VALID
│
└─ 步骤4：LockContent(bufferDesc, mode)  ← 获取页面内容锁（SHARED/EXCLUSIVE）
      └── 返回已 pin、已加锁的 BufferDesc*
```

**调用者职责**：使用完毕后必须调用 `UnlockAndRelease(bufferDesc)` 释放锁和 pin。

### 5.1 StartIo / TerminateIo：防并发 I/O

```cpp
// 防止两个线程同时读同一个页面
bool StartIo(BufferDesc *bufferDesc, bool forInput) {
    DstoreLWLockAcquire(controller->ioInProgressLwlock, LW_EXCLUSIVE);
    while (state & BUF_IO_IN_PROGRESS) {
        // 等待他人完成 I/O
        DstoreLWLockRelease(ioInProgressLwlock);
        DstoreLWLockAcquire(ioInProgressLwlock, LW_EXCLUSIVE);
    }
    if (state & BUF_VALID) {
        // 他人已完成读取，直接复用
        DstoreLWLockRelease(ioInProgressLwlock);
        return false;  // 告知调用者不需要再读
    }
    // 设置 BUF_IO_IN_PROGRESS
    state |= BUF_IO_IN_PROGRESS;
    DstoreLWLockRelease(ioInProgressLwlock);
    return true;  // 告知调用者可以开始 I/O
}

void TerminateIo(BufferDesc *bufferDesc, bool clearDirty, uint64 setFlagBits) {
    // 清除 BUF_IO_IN_PROGRESS
    // 若 clearDirty: 清除 BUF_CONTENT_DIRTY + BUF_HINT_DIRTY
    // 设置 setFlagBits（如 BUF_VALID）
}
```

---

## 6. MarkDirty()：脏页标记

```cpp
// include/buffer/dstore_buf_mgr.h
RetStatus BufMgrInterface::MarkDirty(BufferDesc *bufferDesc, bool needUpdateRecoveryPlsn = true)
```

**调用前提**：调用者必须持有 `contentLwLock` 的 **EXCLUSIVE** 锁。

**内部步骤**：
```
1. 原子设置脏标志：
   state |= (BUF_CONTENT_DIRTY | BUF_HINT_DIRTY)

2. 更新 recoveryPlsn：
   bufferDesc->recoveryPlsn[slotId] = page->GetPlsn()
   （记录这次修改对应的 WAL PLSN，用于 WAL-First 检查）

3. 推入后台脏页写入队列：
   bgPageWriterMgr->PushDirtyPageToQueue(bufferDesc, bgWriterSlotId)
```

**recoveryPlsn 的作用**：脏页入队时记录其对应的 WAL PLSN。`BgDiskPageMasterWriter::GetMinRecoveryPlsn()` 扫描所有脏页的 recoveryPlsn，取最小值作为 `diskRecoveryPlsn`，用于 Checkpoint 判断哪些 WAL 已经不再需要（可以截断）。

---

## 7. WriteBlock()：脏页写回（WAL-First 的关键执行者）

```cpp
RetStatus BufMgr::WriteBlock(BufferDesc *bufferDesc) {
    // 【核心：WAL-First 检查】
    PrepareCheckPageBeforeStartIo(bufferDesc);
    // ↑ 阻塞等待：bufferDesc 对应 WAL PLSN 已落盘
    //   内部调用 walStream->WaitTargetPlsnPersist(page->GetPlsn())

    // 获取 I/O 锁
    if (!StartIo(bufferDesc, false)) {
        return DSTORE_SUCC;  // 他人已完成写盘
    }

    // 计算并写入页面校验和
    bufferDesc->GetPage()->SetChecksum();

    // 同步写到磁盘
    vfs->WritePageSync(bufferDesc->GetPageId(), bufferDesc->GetPage());

    // 记录写盘时的版本（用于 missing dirty check）
    bufferDesc->UpdatePageVersion(bufferDesc->GetPage());

    // 清除脏标记，完成 I/O
    TerminateIo(bufferDesc, /*clearDirty=*/true, 0);
}
```

**WAL-First 时序保证**：

```
应用线程（写入路径）：
  修改页面内容
  → 生成 WAL 记录（得到 PLSN = P）
  → Page::SetLsn(walId, P, G)     ← 更新页面头的 LSN
  → MarkDirty()                   ← 标记脏 + 入队

后台写线程（BgDiskPageMasterWriter）：
  → WriteBlock(bufferDesc)
      → PrepareCheckPageBeforeStartIo()
          → walStream->WaitTargetPlsnPersist(page->GetPlsn() = P)
          ← 阻塞直到 WAL PLSN P 已落盘
      → VFS::WritePageSync()        ← 此刻 WAL P 已在磁盘上 ✓

结论：数据页落盘时，对应 WAL 记录一定已落盘。即使此后立刻宕机，
      重启后也能从 WAL 恢复该页面的内容。
```

---

## 8. 后台脏页写线程（BgDiskPageMasterWriter）

### 8.1 架构

```cpp
// include/buffer/dstore_bg_disk_page_writer.h
class BgDiskPageMasterWriter : public BgPageWriterBase {
    const WalStream *m_walStream;    // 绑定的 WAL 流（每 PDB 一个）
    DirtyPageQueue  *m_dirtyPageQueue; // 该 WAL 流的脏页队列
    PdbId m_pdbId;
    uint32 m_slaveNum;               // 从写线程数量
    BgSlavePageWriterEntry *m_slaves; // 从写线程数组
};
```

**主从架构**：
- 一个 Master 管理一个 WAL 流的脏页队列
- Master 派发写任务给多个 Slave（并行写盘）
- 每个 PDB 有独立的 BgDiskPageMasterWriter

### 8.2 DirtyPageQueue：MPSC 脏页队列

- **MPSC**（Multi-Producer Single-Consumer）：多个应用线程可以并发推入脏页，一个 BgDiskPageMasterWriter 消费
- 脏页**按 MarkDirty 顺序**排列（近似 WAL 顺序），保证写盘时 WAL-First 检查更高效

### 8.3 GetMinRecoveryPlsn()：Checkpoint 的锚点

```cpp
// include/buffer/dstore_bg_disk_page_writer.h:59
uint64 BgDiskPageMasterWriter::GetMinRecoveryPlsn() const;
```

此函数扫描所有尚未写盘的脏页，返回它们中最小的 `recoveryPlsn`。

**含义**：`minRecoveryPlsn` 之前的 WAL 已经不再需要用于恢复（因为对应页面已落盘），Checkpoint 可以安全截断 WAL 到此点。

```
脏页队列中：
  页A: recoveryPlsn = 100（已写盘）
  页B: recoveryPlsn = 200（等待写盘）
  页C: recoveryPlsn = 150（等待写盘）

GetMinRecoveryPlsn() = min(200, 150) = 150
→ Checkpoint 可截断 PLSN < 150 的 WAL
```

---

## 9. CR 页（Consistent Read Page）

### 9.1 什么是 CR 页

CR 页是基础页面（Base Page）的历史快照副本，用于 MVCC 读取。当一个读取快照需要看到某个时间点的数据，但 Base Page 已经被后续事务修改，Buffer Manager 会构造一个 CR 页：将 Base Page 拷贝，然后把 CSN > snapshotCSN 的修改通过 Undo 回滚掉。

```
Base Page: [A=10, B=20]  CSN=200
读快照要看 CSN=100 的版本
→ 构造 CR 页：
  从 Base Page 拷贝
  回滚 CSN=200 的修改 → CR Page: [A=5, B=20]  (CSN=100 时刻的样子)
```

### 9.2 CRInfo 结构

```cpp
// include/buffer/dstore_buf.h:400
union CRInfo {
    // For Base Buffer（BufferDesc 缓存基础页时）
    struct {
        BufferDesc *crBuffer;      // 关联的 CR 页面 buffer（可以为 null）
        CommitSeqNo crPageMaxCsn;  // CR 页面覆盖的最大 CSN
        bool isUsable;
    };
    // For CR Buffer（BufferDesc 缓存 CR 页时）
    struct {
        BufferDesc *baseBufferDesc; // 指回基础页面的 buffer
    };
};
```

### 9.3 CR 页命中判断

```cpp
bool CRInfo::IsCrMatched(CommitSeqNo snapshotCsn) const {
    // snapshotCsn > crPageMaxCsn：快照在 CR 页构建时间点之后
    // 即 CR 页记录的是 "snapshotCsn 时刻之前" 的所有修改
    return snapshotCsn > crPageMaxCsn;
}
```

---

## 10. 完整读写流程时序图

### 10.1 写入路径（INSERT）

```
应用线程：
  bufDesc = BufMgr::Read(pageId, LW_EXCLUSIVE)    [1] 读取并加锁页面
  page = bufDesc->GetPage()
  HeapPage::AddTuple(diskTuple, size)              [2] 修改页面内存
  wal = GenerateWalRecord(page)                    [3] 生成 WAL 记录
  page->SetLsn(walId, wal.plsn, wal.glsn)         [4] 更新页面 LSN
  BufMgr::MarkDirty(bufDesc)                      [5] 标记脏 + 入脏页队列
  BufMgr::UnlockAndRelease(bufDesc)               [6] 释放锁和 pin

后台线程（BgDiskPageMasterWriter）：
  bufDesc = DirtyPageQueue::Pop()                  [7] 取脏页
  BufMgr::WriteBlock(bufDesc)                      [8] 写盘（内含 WAL-First 检查）
```

### 10.2 读取路径（SELECT）

```
应用线程：
  bufDesc = BufMgr::Read(pageId, LW_SHARED)        [1] 读取并加锁（共享）
  page = bufDesc->GetPage()
  HeapPage::GetVisibleTuple(ctid, snapshot)        [2] MVCC 可见性判断
  if (需要历史版本) {
      crBufDesc = BufMgr::ReadCr(bufDesc, snap)   [3] 获取 CR 页
      crPage->GetVisibleTuple(ctid, snapshot)       [4] 在 CR 页读历史版本
      BufMgr::UnlockAndRelease(crBufDesc)
  }
  BufMgr::UnlockAndRelease(bufDesc)               [5] 释放
```

---

## 11. contentLwLock 的持锁规则

| 操作 | 锁模式 | 理由 |
|------|--------|------|
| 读取页面（SELECT） | `LW_SHARED` | 多个读并发，互不干扰 |
| 修改页面（DML） | `LW_EXCLUSIVE` | 独占修改，不允许其他读写 |
| 刷脏页（WriteBlock） | `LW_SHARED` | 读页面内容写到磁盘，不修改内存 |
| MarkDirty | 前提持有 `LW_EXCLUSIVE` | 修改完成后才标记脏 |
| `TryFlush` | `TryLock(LW_SHARED)` | 避免死锁，获取失败则放弃 |

---

## 12. Day 3 核心速查表

| 概念 | 位置 | 一句话 |
|------|------|--------|
| `BufferDesc` | `include/buffer/dstore_buf.h:650` | 缓冲页面的描述符，含 state/tag/lock/lru/cr |
| `BufferTag` | `include/buffer/dstore_buf.h:301` | 页面标识：pdbId + fileId + blockId（12B） |
| `state` 低32bit | `include/buffer/dstore_buf.h:65` | refcount，只有为 0 才可驱逐 |
| `BUF_CONTENT_DIRTY` | `include/buffer/dstore_buf.h:74` | bit33，脏页标记，WriteBlock 时清除 |
| `BUF_VALID` | `include/buffer/dstore_buf.h:76` | bit34，读取完成后设置 |
| `BUF_IO_IN_PROGRESS` | `include/buffer/dstore_buf.h:80` | bit36，防止并发 I/O |
| `LruNodeType` | `include/buffer/dstore_buf.h:451` | CANDIDATE/LRU/HOT 三层冷热 |
| `NUM_BUFFER_PARTITIONS` | `include/buffer/dstore_buf_table.h:28` | 4096，哈希表分区数 |
| `BufMgr::Read()` | `include/buffer/dstore_buf_mgr.h:276` | 查哈希→分配→读盘→加锁，返回 pinned+locked |
| `BufMgr::MarkDirty()` | `include/buffer/dstore_buf_mgr.h:293` | 设脏标志 + 入脏页队列（需持 EXCLUSIVE 锁） |
| `BufMgr::WriteBlock()` | `include/buffer/dstore_buf_mgr.h:566` | WAL-First 检查 + 同步写盘 + 清脏标志 |
| `BgDiskPageMasterWriter` | `include/buffer/dstore_bg_disk_page_writer.h:42` | 每 PDB 一个，驱动后台脏页写盘 |
| `GetMinRecoveryPlsn()` | `include/buffer/dstore_bg_disk_page_writer.h:59` | 最小未落盘 WAL PLSN，Checkpoint 截断依据 |
| `CRInfo` | `include/buffer/dstore_buf.h:400` | CR 页关联信息，用于 MVCC 历史版本构造 |

---

## 13. 与 Day 4 的衔接

Buffer Manager 是所有模块的底座：每次读写数据库页面都要经过它。但 Buffer Manager 本身不知道"哪些修改对哪些事务可见"——这是 **Transaction + MVCC** 的责任。

Day 4 将深入 Transaction Manager：
- `XID` 的分配与结构（zoneId + logicSlotId）
- `CommitSeqNo`（CSN）的全局单调递增机制
- 快照（Snapshot）的构建：`snapshotCSN` 从何而来
- `XidVisibleToSnapshot()` 的完整判断逻辑
- `recycleMinCsn`：MVCC 可回收水位线的计算
