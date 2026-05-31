# Buffer Manager 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 GetBuffer 驱逐流程、Pin/Unpin 双层计数、LRU 三层策略、MarkDirty 与 WAL-First 协调、BgDiskPageWriter 后台刷脏、Checkpointer 协调、CR 页池机制、临时表缓冲差异。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_buf_mgr.cpp` | 4118 | GetBuffer/Read 主流程、MarkDirty、WriteBlock、Invalidate、CR 页分配/查找 |
| `dstore_buf_lru.cpp` | 825 | 三层 LRU 管理（HOT/LRU/CANDIDATE）、BufferAccessStat 升温降温、LruPageClean 后台线程 |
| `dstore_bg_disk_page_writer.cpp` | 655 | BgDiskPageMasterWriter 主循环、Slave 线程分片刷脏 |
| `dstore_buf_desc.cpp` | 525 | LockHdr/UnlockHdr spinlock、Pin/SharedPin/Unpin 双层引用计数实现 |
| `dstore_buf_refcount.cpp` | 357 | PrivateRefCount 数组+哈希两层存储、快慢路径 |
| `dstore_buf_table.cpp` | 269 | BufTable 分区哈希表 LookUp/Insert/Remove、双锁排序 |
| `dstore_bg_page_writer_mgr.cpp` | 745 | BgPageWriterMgr 管理多 WalStream 的 BgPageWriter 数组、DirtyPageQueue Push |
| `dstore_checkpointer.cpp` | 490 | CheckpointMgr 主循环、从 BgDiskPageMasterWriter 获取 recoveryPlsn 写 ControlFile |
| `dstore_buf_mgr_temporary.cpp` | 614 | TmpLocalBufMgr 临时表缓冲、无 LRU/WAL/CR，线程私有 |
| `dstore_buf_memchunk.cpp` | 379 | BufferMemChunk 内存大块管理、弹性扩缩容统计 |
| `dstore_buf_interface.cpp` | 65 | 接口层，无核心实现 |

---

## 一、GetBuffer：完整查找与驱逐流程

### 1.1 顶层入口 BufMgr::Read()

```
BufMgr::Read(pdbId, pageId, mode, flag):

  Step1: LookupBuffer(bufTag)          ← 无锁快速哈希查表
    ├─ 命中 → Pin() + BufferAccessStat() (更新 LRU 热度) → 返回
    └─ 未命中 → AllocBufferForBaseBuffer()
        └─ 无限循环调用 TryToReuseBufferForBasePage()，直到成功
            ├─ GetFreeBuffer()           ← 从 CANDIDATE 或 LRU 尾部取空闲 desc
            └─ ReuseCrBufferForBasePage() 或 ReuseBaseBufferForBasePage()

  Step2: isBufValid == false → StartIo() + ReadBlock() + TerminateIo()

  Step3（带锁版本）: 获取 contentLwLock(shared/exclusive)
    ├─ LW_EXCLUSIVE → UpdateLastModifyTimeToNow() + WaitIfIsWritingWal() + InvalidateCrPage()
    └─ 返回 bufferDesc
```

**关键设计**：`LookupBuffer` 不加锁，通过 Pin + bufTag 二次验证保证正确性。命中率高时完全无锁，极大减少热点竞争。

### 1.2 GetFreeBuffer()：缓冲驱逐选择核心

```
GetFreeBuffer(bufTag, needRetry, bufRing):

  Step1: 优先从 BufferRing（关系扫描预分配环）取

  Step2: BufferRing 空 → m_buffer_lru->GetCandidateBuffer(bufTag):
    ├─ hash(bufTag) % lru_partitions → 定位分区
    ├─ mCandidateList.Pop() → 弹出候选
    │   ├─ 成功 → FastLockHdrIfReusable() → 原子操作抢锁并验证可复用
    │   └─ 失败 → AddTail 回 LRU，重试最多 8 次
    ├─ CANDIDATE 空 → ScanLruListToFindCandidateBuffer()
    │   └─ 从 LRU 尾部 PopTail → FastLockHdrIfReusable → 成功即返回，失败放回头部
    └─ 所有分区都找不到 → WARNING + return INVALID

  Step3: 候选是 CR 页 → TryAcquireCrAssignLwlock(EXCLUSIVE) + MakeCrBufferFree()
    └─ 失败 → PushBackToLru() + 重试

  Step4: 候选是 base 页 → MakeBaseBufferFree()
    ├─ hasCrBuffer → 先 FindFreeCrBufferFromBaseBuffer() 回收 CR 槽
    └─ 无 CR → TryFlush(dirty) → 重新验证无 CR → return candidateBuffer
```

### 1.3 ReuseBaseBufferForBasePage()：驱逐提交的关键步骤

```
ReuseBaseBufferForBasePage(bufTag, candidateBuffer, usedBuffer, bufRing):

  AcquireCrAssignLwlock(SHARED) → 检查 HasCrBuffer（有则退让）

  若 BUF_TAG_VALID: LockBufMapping(oldHash, newHash, EXCLUSIVE)  // 按地址排序避免死锁
  否则:             LockBufMapping(newHash, EXCLUSIVE)

  LockHdr() → Insert(&newBufTag, newHash, candidateBuffer):
    ├─ 其他线程已插入 → PushBackToLru + UnlockHdr + return false
    └─ 插入成功 →
        IsBaseBufferReusable():
          refcount==1(仅我自己 pin) && !dirty && !HasCrBuffer && IsBufferReusableByCurrentPdb()
        ├─ 可复用 → AntiCacheHandleBufferEvicted() → 更新 bufTag + bufState → 返回
        └─ 不可复用 → Remove(newBufTag) + PushBackToLru + Unpin → return false
```

**关键（.h 不可见）**：
- 先插入新 BufTag 再判断 refcount——并发插入同一页时，后来者通过插入冲突发现已有缓冲，直接复用
- 双锁按**指针地址排序**加锁，彻底避免死锁
- `IsBufferReusableByCurrentPdb()` 额外检查 `IsInDirtyPageQueue(slotId)`，防止跨 PDB 复用时脏页信息丢失

---

## 二、Pin/Unpin：双层引用计数

### 2.1 数据结构

```
私有计数（线程局部）：
  BufPrivateRefCount m_private_refcount_array[REFCOUNT_ARRAY_ENTRIES]  ← 固定数组，O(N) 扫描
  m_private_refcount_hash（溢出时的小哈希表，100 槽）
  m_private_refcount_clock（时钟指针，选择牺牲槽）

全局 refcount：
  嵌入 BufferDesc::state 的低 32 位（BUF_REFCOUNT_MASK）
```

### 2.2 Pin 完整序列

```cpp
BufferDesc::Pin():
  快路径: GetPrivateRefcount(this) → 找到私有槽
    ├─ privateRef->refcount > 0 → 直接 refcount++（无任何原子操作）
    └─ privateRef->refcount == 0 → SharedPin() 然后 refcount++

SharedPin():                         // 首次 pin，需要原子操作
  GsAtomicFetchAddU64(&state, BUF_REFCOUNT_ONE)
  若 state & BUF_LOCKED:             // header 正被其他线程锁定
    └─ GsAtomicSubFetchU64(&state, BUF_REFCOUNT_ONE)  // 回滚
       WaitHdrUnlock() → 重试
```

### 2.3 PinUnderHdrLocked()：持有 BUF_LOCKED 期间的优化 Pin

```cpp
PinUnderHdrLocked():
// 调用者已持有 BUF_LOCKED，跳过 WaitHdrUnlock 循环
GsAtomicFetchAddU64(&state, BUF_REFCOUNT_ONE)
privateRef->refcount++
```

### 2.4 Unpin 完整序列

```cpp
BufferDesc::Unpin():
  GetPrivateRefcount(this) → 找私有槽
  privateRef->refcount--
  privateRef->refcount == 0:
    Assert(!LWLockHeldByMe(contentLwLock))  // 确保内容锁已释放
    SharedUnpin()                            // 原子减全局 refcount
    ForgetPrivateRefcountEntry()             // 清除私有槽
```

### 2.5 私有计数溢出处理（.h 未披露）

```
私有槽满时 → MoveEntryToHashTable():
  用时钟指针 m_private_refcount_clock 选择牺牲槽
  将牺牲槽移入 hash，腾出数组位置给新 buffer
  m_private_refcount_overflowed++ 记录溢出数量

overflowed > 0 时：慢路径需同时查数组 + 哈希
```

### 2.6 UnlockHdr 的精细原子操作（.cpp 独有）

```cpp
UnlockHdr(state):
// 只写高 32 位（BUF_LOCKED 在高位），低 32 位 refcount 不受干扰
GsAtomicWriteU32(((volatile uint32*)&state)+1, highBits & ~BUF_LOCKED_BIT)
```

---

## 三、LRU 驱逐策略

### 3.1 三层结构详述

```
每个 LRU 分区 BufLruList：
┌──────────────────────────────────────────────────────────┐
│  LruHotList                                              │
│  [usage >= LRU_MAX_USAGE(5)，最大容量 = bufPoolSize *    │
│   HOT_RATIO / lruPartitions]                             │
├──────────────────────────────────────────────────────────┤
│  LruList（双向链表，头=最近访问，尾=最久未访问）           │
│  [usage < 5 的活跃页]                                    │
├──────────────────────────────────────────────────────────┤
│  CandidateList（无锁栈，LIFO）                            │
│  [空闲或即将被驱逐的页]                                   │
└──────────────────────────────────────────────────────────┘

第四个隐藏列表：
  InvalidationList（弹性缩容时暂存被踢出的 buffer）
```

### 3.2 BufferAccessStat 升温/降温（usage 驱动，非 clock-sweep）

```
BufferAccessStat(bufferDesc):  ← 每次 LookupBuffer 命中后调用

  在 HOT 列表 → 什么都不做（已足够热）
  在 CANDIDATE 列表 → 移到 LRU 头部，usage 重置为 1
  在 LRU 列表：
    IncUsage() → usage++
    usage < 2 → 不移动
    2 <= usage < 5 → MoveHead() 移到 LRU 头
    usage >= 5 →
      Remove from LRU → 尝试 mHotList.Push()
        ├─ HOT 未满 → 直接入 HOT
        └─ HOT 已满 → TryMoveOneNodeFromHotToLruList()  // 驱逐最冷 HOT 到 LRU 头
```

### 3.3 PushBackToLru（驱逐失败回写）

```cpp
PushBackToLru(bufferDesc, reuseSuccess):
  lruNode.ResetUsage()      // usage 重置为 0
  reuseSuccess == true  → IncUsage() 设为 1，AddHead(LRU)  // 新分配页从 LRU 头开始
  reuseSuccess == false → AddHead(LRU)  // 失败后放回头部，给一定保护时间
```

### 3.4 LruPageClean 后台线程（.h 不可见的关键机制）

```
LruPageClean::Run()  // "BgLruPageClean" 线程
  初始 sleep=100ms，动态调节 [10ms, 1s]
  对每个 LRU 分区：
    candidateSafeNum = initCandidateLen * candidateSafePercent(GUC)
    recycleGap = candidateSafeNum - currentCandidateLen
    TryCleanLruListToCandidate(ignoreDirty=true, recycleGap):
      从 LRU 尾部 PopTail → LockBufMapping(EXCLUSIVE) → TryAcquireCrAssignLwlock(SHARED)
      FastLockHdrIfReusable → 若为脏页 → TryFlush → 重新验证
      清除 buftag → Remove from HashTable → PushBackToCandidate
      失败 → AddHead(LRU) 放回
  pageMoved=true → sleep *= 0.8（加速）；false → sleep *= 1.2（减速）
```

**为什么需要 LruPageClean**：提前将 LRU 尾部干净页填入 CANDIDATE，避免高负载时业务线程陷入 GetFreeBuffer 忙等。

---

## 四、MarkDirty 与 recoveryPlsn

### 4.1 MarkDirty 完整实现

```cpp
BufMgr::MarkDirty(bufferDesc, needUpdateRecoveryPlsn):
  前置: Assert(contentLwLock held in LW_EXCLUSIVE)
  LockHdr()

  state |= (BUF_CONTENT_DIRTY | BUF_HINT_DIRTY)  // 两个脏标志同时设置

  if needUpdateRecoveryPlsn && bgPageWriterMgr != nullptr:
    bgWriterSlotId = pdb->GetBgWriterSlotId(page->GetWalId())  // 按 WalId 定位槽位
    bgPageWriterMgr->PushDirtyPageToQueue(bufferDesc, bgWriterSlotId)
      └─ DirtyPageQueue::Push() → recoveryPlsn[slotId] = page->GetPlsn()

  if bufferDesc->fileVersion == INVALID_FILE_VERSION:
    SetFileVersion(WalUtils::GetFileVersion(...))  // 用于跨版本驱逐检测

  UnlockHdr(state)
```

### 4.2 BUF_CONTENT_DIRTY vs BUF_HINT_DIRTY（.cpp 揭示）

- `BUF_CONTENT_DIRTY`：页面实际内容修改，**必须刷盘**
- `BUF_HINT_DIRTY`：hint bits 等轻量修改（不影响恢复正确性）
- `TerminateIo` 关键逻辑：`clearDirty && !(bufState & BUF_HINT_DIRTY) → 清 BUF_CONTENT_DIRTY`——只有 HINT_DIRTY 为零时才清除 CONTENT_DIRTY，防止写入途中有新 MarkDirty 导致数据丢失

### 4.3 WAL-First 协调（WriteBlock 前的 PrepareCheckPageBeforeStartIo）

```cpp
PrepareCheckPageBeforeStartIo(bufferDesc):
  bufferDesc->WaitIfIsWritingWal()        // 等待 BUF_IS_WRITING_WAL 清除（微秒级 spin）

  walStreamMgr->IsSelfWritingWalStream(page->GetWalId()) == true:
    → walStream->WaitTargetPlsnPersist(page->GetPlsn())
      // 等 WAL 中 plsn 位置已落盘，保证数据页晚于 WAL 写磁盘
```

### 4.4 recoveryPlsn 数组设计（.h 未见完整实现）

```cpp
// 每个 BufferDesc 有多个 recoveryPlsn 槽（DIRTY_PAGE_QUEUE_MAX_SIZE 个）
atomic<uint64> recoveryPlsn[DIRTY_PAGE_QUEUE_MAX_SIZE]

// Push:  recoveryPlsn[slotId] = page->GetPlsn()
// Check: IsInDirtyPageQueue(slotId) 即 recoveryPlsn[slotId] != INVALID_PLSN
// 刷完: AdvanceHeadAfterFlush → recoveryPlsn[slotId].store(INVALID_PLSN)
```

**为什么用数组**：支持多 WalStream 并存（多租户），每个 WalStream 对应一个 slot，同一 buffer 可同时属于不同 WalStream 的脏页队列。

---

## 五、BgDiskPageWriter：后台刷脏

### 5.1 主从架构

```
BgDiskPageMasterWriter（一个 WalStream 对应一个）：
  m_dirtyPageQueue: 链表式 DirtyPageQueue（MarkDirty 时 Push 填入）
  m_slaveWriterArray[bgDiskWriterSlaveNum]: 多个 Slave
    └─ BgDiskPageSlaveWriter（实际执行 Flush 的工作线程）
  m_flushCxt: CandidateFlushCxt（Master 扫描后填入，Slave 竞争取任务）
```

### 5.2 Master 主循环

```
BgDiskPageMasterWriter::Run():
  无限循环：
    maxAppendPlsn = walStream->GetMaxAppendedPlsn()
    ScanDirtyListForFlush(advanceNum, slotId):
      先处理 tmpDirtyPageVec（上轮未处理完的）
      从 DirtyPageQueue 头部扫描，最多 maxIoCapacityKb*1024/BLCKSZ 页
        → 填入 m_flushCxt.candidateFlushArray[]
    dirtyPageNum > 0:
      WakeUpSlaveWriter()    // 唤醒所有 Slave
      WaitSlaveWriterFlushFinish()
    AdvanceHeadAfterFlush(advanceNum, slotId)
      → 推进 DirtyPageQueue 头指针，更新 m_recoveryPlsn
    SmartSleep()  // 精确等待到 nextFlushTime（支持 m_flushAll 紧急唤醒）
```

### 5.3 Slave 无锁分片竞争

```cpp
BgDiskPageSlaveWriter::SeizeDirtyPageListForFlush():
  m_flushCxt->ScrambleLoc(maxBatchFlush=1000):
    // 原子 fetch_add 抢占起始位置
    m_startFlushLoc = atomicAdd(&startLoc, 1000)
  m_startFlushLoc >= totalFlushCnt → m_needFlushCnt = 0（无任务）
  计算本 Slave 的 [startLoc, startLoc+1000) 区间
```

**设计精髓**：Master 一次扫描填满 candidateFlushArray，多个 Slave 通过原子操作各取 1000 页，实现无锁分片并行刷脏。

### 5.4 WriteBlock 完整流程（WAL-First 全链路）

```
BufMgr::WriteBlock(bufferDesc):
  Step1: PrepareCheckPageBeforeStartIo() → WaitTargetPlsnPersist(page.plsn)  ← WAL 落盘屏障
  Step2: StartIo() → 设置 BUF_IO_IN_PROGRESS
  Step3: state &= ~BUF_HINT_DIRTY
  Step4: page->SetChecksum() → vfs->WritePageSync(pageId, page)
  Step5: TerminateIo(clearDirty=true) → 清 BUF_IO_IN_PROGRESS，条件清 BUF_CONTENT_DIRTY
```

---

## 六、Checkpointer（buffer 目录下）

### 6.1 CheckpointMgr 主循环

```
CheckpointMgr::CheckpointerMain():
  对所有 WalStream（过滤：IsWalStreamNeedCkpt）循环：
    flag = checkpointStreamRequest.StartCheckpoint()
    CreateCheckpoint(walId, flag, &isPerformed):
      获取 checkpointLwLock EXCLUSIVE
      recoveryPlsn = bgPageWriter->GetMinRecoveryPlsn()  // 拉取 BgWriter 最小已刷 PLSN
      与 controlFile 中上次 checkpoint 比较，若无进展则跳过
      controlFile->UpdateWalStreamForCheckPointWithBarrier(walId, ...)
      释放 checkpointLwLock
  超时 → 强制触发下一轮
  否则 → 等待 1 秒
```

### 6.2 Checkpoint 不触发刷脏（.cpp 关键发现）

```
Checkpoint 的 recoveryPlsn 来源：
  bgPageWriter->GetMinRecoveryPlsn()
  └─ m_recoveryPlsn.load(memory_order_acquire)
     由 BgDiskPageMasterWriter::AdvanceHeadAfterFlush() 异步更新

Checkpoint 本身：只读取 recoveryPlsn 并写 ControlFile
实际刷脏：完全由 BgDiskPageMasterWriter 异步进行
FullCheckpoint()：等待 GetMinRecoveryPlsn() >= maxAppendedPlsn（主动等刷完）
```

### 6.3 FlushAll 三步骤

```cpp
BufMgr::FlushAll(isBootstrap, onlyOwnedByMe, pdbId):
  Step1: StoreMaxAppendPlsnOfPdbs() → 快照当前所有 WAL 流的 maxAppendedPlsn
  Step2: FlushBuffers() → 启动 10 个线程扫全部 MemChunk 刷脏
  Step3: CreateCheckpointForPdbs(plsnRecords) → 用快照 PLSN 更新 ControlFile
```

**为什么先快照再刷**：保证 Checkpoint 记录的 PLSN 不超过实际已刷的最大 PLSN，防止恢复起点错误。

---

## 七、CR 页池（CrBufferPool）

### 7.1 CR 页与 base 页共享内存池（.h 层误导性描述）

`.h` 层提到 `CrBufferPool`，`.cpp` 揭示：**CR 页和 base 页共享同一个 MemChunk 内存池，通过 `BUF_CR_PAGE` 标志区分**，没有独立分配器。

### 7.2 CR 页分配流程（AllocCrEntry）

```
AllocCrEntry(baseBufferDesc, bufRing):
  前置: baseBufferDesc->IsCrAssignLocked(LW_EXCLUSIVE)

  HasCrBuffer == false:
    无限循环调用 TryToReuseBufferForCrPage():
      GetFreeBuffer() → 从 LRU/CANDIDATE 取空闲 buffer（与 base page 共享同一池）
      若取到 CR 页 → ReuseCrBufferForCrPage():
        bufState |= BUF_CR_PAGE
        SetBaseBuffer(baseBufferDesc)
        baseBufferDesc->SetCrBuffer(freeBuffer)
        PushBufferBackToLru(reuseSuccess=true)
      若取到 base 页 → ReuseBaseBufferForCrPage():
        IsBaseBufferReusable() 检查
        Remove from HashTable
        bufState = BUF_FLAG_RESET_MASK | BUF_CR_PAGE

  HasCrBuffer == true（已有 CR 槽但可能过期）:
    LockHdr + GetCrBuffer + Pin
    MakeCrBufferFree() → refcount==1(仅我 pin) 则回收
      失败（他人在读）→ Unpin → return INVALID（本次放弃）
    成功后复用已有槽位
```

### 7.3 CR 页查找（FindCrBuffer）

```
FindCrBuffer(baseBufferDesc, snapshot):
  前提: baseBufferDesc->IsCrAssignLocked(SHARED)

  IsCrUsable() && crInfo.IsCrMatched(snapshot->snapshotCsn):
    crBuffer->Pin()
    分布式模式额外验证: crPage.glsn == basePage.glsn && crPage.plsn == basePage.plsn
    不匹配 → Unpin + SetCrUnusable → return INVALID（基页已被修改，CR 失效）
  不匹配 → return INVALID
```

### 7.4 CR 页缓存条件（ConsistentReadInternal 揭示）

```
仅 INDEX_PAGE_TYPE 才缓存 CR 页（HEAP_PAGE 不缓存）

缓存条件：
  !crCtx.useLocalCr                                   // 非事务本地专属
  && snapshot->GetCsn() != MAX_COMMITSEQNO            // 非"读最新版本"快照
  && pageType == INDEX_PAGE_TYPE
  && now >= lastModifyTime + TIMESTAMP_THRESHOLD_IN_CR // 时间窗口检查

FinishCrBuild(crBufferDesc, pageMaxCsn):
  baseBufferDesc->crInfo.SetCrPageMaxCsn(pageMaxCsn)
  baseBufferDesc->SetCrUsable()
  crBufferDesc->state |= BUF_VALID
  ReleaseCrAssignLwlock
```

---

## 八、Temporary Buffer

### 8.1 核心差异对比

| 特性 | BufMgr（普通缓冲） | TmpLocalBufMgr（临时表缓冲） |
|------|-------------------|------------------------------|
| 存储范围 | 跨线程共享 | **线程（会话）私有** |
| 哈希表 | 全局分区哈希（4096分区，LWLock） | 线程私有简单哈希（无锁） |
| LRU | 三层（HOT/LRU/CANDIDATE） | **无 LRU，简单环形数组** |
| Pin/Unpin | 双层计数（私有+全局原子操作） | **直接加减 state，无原子操作** |
| MarkDirty | 记录 recoveryPlsn，推 DirtyPageQueue | **仅设 BUF_CONTENT_DIRTY，无 WAL 协调** |
| WriteBlock | WAL-First（WaitTargetPlsnPersist） | **直接 vfs->WritePageSync，无 WAL 等待** |
| CR 页 | 完整支持 IndexPage CR 缓存 | **不支持** |
| 并发控制 | contentLwLock, BUF_IO_IN_PROGRESS | **无锁（单线程访问）** |

### 8.2 TmpLocalBufMgr 驱逐机制

```
GetAvailableBuffer():
  Step1: InvalidBufferList 中取已手动 Invalidate 的槽 (LIFO slist)
  Step2: m_nextBufIdx 指向新槽（首次使用时分配内存）
  Step3: 循环遍历 m_buffers[0..N-1]，找 refcount==0 的槽：
    若旧 bufTag 非空 → 先刷脏 WriteBlock
    从哈希表删除旧 buftag
    InitBufferDesc(reuse bufBlock) → 返回
```

**为什么临时表不需要 WAL-First**：临时表不记录 WAL，会话结束后数据无需恢复，直接写盘无需等待 WAL 落盘，大幅降低延迟。

---

## 九、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| GetBuffer 查找 | BufTable 哈希 + Pin 返回 | LookupBuffer **无锁**，Pin + bufTag 二次验证保证正确性 |
| 驱逐插入顺序 | 驱逐前检查 refcount | **先插入新 BufTag 再检查 refcount**，冲突时直接返回已有 desc |
| 双锁死锁避免 | 需要锁两个 hashCode 分区 | 按**锁指针地址排序**加锁，完全避免死锁 |
| BUF_HINT_DIRTY | dirty 标志位 | 两种脏：CONTENT_DIRTY（需恢复）和 HINT_DIRTY；TerminateIo 仅当 HINT_DIRTY=0 才清 CONTENT_DIRTY |
| LRU 升温阈值 | 三层结构、usage 驱动 | LRU_MAX_USAGE=5；HOT 满时 Pop 最冷入 LRU 头（非尾） |
| LruPageClean | 不可见 | 独立后台线程，动态 sleep[10ms,1s]，提前将 LRU 尾部干净页移入 CANDIDATE |
| CR 页内存来源 | CrBufferPool 独立池 | **与 base page 共享 MemChunk**，通过 BUF_CR_PAGE 标志区分 |
| CR 缓存条件 | 索引页 MVCC | 时间窗口检查（TIMESTAMP_THRESHOLD_IN_CR）+ 仅非最新快照 + 仅 INDEX 页 |
| recoveryPlsn | 单个值 | **数组**（DIRTY_PAGE_QUEUE_MAX_SIZE 个），支持多 WalStream 并存 |
| BgWriter 架构 | 后台刷脏线程 | Master 扫描填 CandidateFlushArray，多 Slave **原子 fetch_add 竞争分片**（每片1000页） |
| Checkpoint 触发 | 超时或请求 | CheckpointMgr **不刷脏**，只读取 BgDiskPageMasterWriter::m_recoveryPlsn 写 ControlFile |
| FlushAll | 全量刷脏 | 先快照 maxAppendedPlsn，10 线程刷完，再用快照值更新 ControlFile |
| UnlockHdr | 释放 BUF_LOCKED | 只写高 32 位，低 32 位 refcount 不受干扰 |
| 临时表缓冲 | 线程私有 | 完全无原子操作、无 LRU、无 WAL-First；InvalidBufferList 避免扫描整个环形数组 |
| 弹性缩容 | 不可见 | SortByTemperature（按 hotPageCount 排序），从最冷 MemChunk 开始驱逐 |
| 文件版本检查 | 不可见 | MarkDirty 时记录 fileVersion；WriteBlock 失败时比较版本号，文件已删除则静默跳过 |
