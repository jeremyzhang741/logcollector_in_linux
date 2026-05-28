# WAL 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 AtomicWal 写入流程、WalGroup 原子单元、多流状态机、ARM64 批量提交、WAL Buffer 管理、WaitTargetPlsnPersist 等待机制的具体实现。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_wal_write_context.cpp` | 710 | AtomicWalWriterContext：组装、压缩、提交 WAL 原子单元 |
| `dstore_wal_logstream.cpp` | 3125 | WalStream：单流/批量 Append、Flush、状态机管理 |
| `dstore_wal_buffer.cpp` | 440 | WAL 环形 Buffer：写入、刷盘时机、溢出处理 |

---

## 一、AtomicWalWriterContext：WAL 写入全流程

### 1.1 三个 API 的内部实现

#### BeginAtomicWal

```cpp
BeginAtomicWal(xid):
    // 检查是否已在原子块中（m_bufUsed > 0 即为嵌套，禁止）
    StorageAssert(m_bufUsed == 0);
    
    // 在本地 m_buf 中写入 WalRecordAtomicGroup 头（占位）
    m_buf = localBuffer;
    m_bufUsed = sizeof(WalRecordAtomicGroup);   // 预留 header 空间
    
    m_atomicGroupHeader.xid = xid;
    m_atomicGroupHeader.recordCount = 0;
    m_walId = GetCurrentWalStream()->GetWalId();
```

`m_bufUsed > 0` 是"已 Begin"的唯一判断依据，简洁且线程安全。

#### PutNewWalRecord

```cpp
PutNewWalRecord(walRecord):
    // Step 1: 压缩 WAL 记录
    compressedSize = walRecord->Compress(compressedBuf);
    
    // Step 2: 若 m_buf 空间不足，扩展 4KB
    while (m_bufUsed + compressedSize > m_bufCapacity) {
        m_buf = Realloc(m_buf, m_bufCapacity + 4096);
        m_bufCapacity += 4096;
    }
    
    // Step 3: 追加到 m_buf
    memcpy(m_buf + m_bufUsed, compressedBuf, compressedSize);
    m_bufUsed += compressedSize;
    m_atomicGroupHeader.recordCount++;
    
    // Step 4: 若涉及页面，记录 RememberPageNeedWal（用于 WAL-First 检查）
    RememberPageNeedWal(bufDesc);
```

`m_buf` 以 4KB 为单位动态扩展，正常事务 WAL 通常在一次扩展内完成。

#### EndAtomicWal

```cpp
EndAtomicWal() -> WalGroupPtr:
    // 填写 AtomicGroup header（总大小、记录数、checksum）
    FinalizeAtomicGroupHeader();
    
    // 提交到 WalStream
    walStream = GetCurrentWalStream();
    walGroupPtr = walStream->Append(m_buf, m_bufUsed);
    
    // 重置 m_buf
    m_bufUsed = 0;
    
    return walGroupPtr;   // 调用方用于 WaitTargetPlsnPersist
```

### 1.2 WalGroupPtr 的含义

```cpp
struct WalGroupPtr {
    WalStreamId streamId;
    uint64 endBytePos;   // 本原子组在 WAL 流中的结束位置（字节偏移）
};
```

`WaitTargetPlsnPersist(walGroupPtr)` 等待对应 WAL 流的 `flushedBytePos >= endBytePos`。

---

## 二、WalStream::Append：空间预留与写入

### 2.1 空间预留：一次 atomic fetch_add

```cpp
uint64 Append(data, size):
    // 唯一的同步点：原子递增 endBytePos
    uint64 startPos = m_endBytePos.fetch_add(size, memory_order_acq_rel);
    uint64 endPos = startPos + size;
    
    // 写入 WAL Buffer（可能跨多个 Buffer 块）
    WriteToBuffer(data, size, startPos);
    
    // 标记本段已就绪（供 Flush 检测连续写入范围）
    MarkReady(startPos, endPos);
    
    return WalGroupPtr{walId, endPos};
```

**关键设计**：`fetch_add` 是唯一协调多写者顺序的操作，无锁、无等待。每个写者获得自己的 `[startPos, endPos)` 区间，可以并发写入不同位置。

### 2.2 x86 SingleAppend vs ARM64 BatchAppend

```
// x86：简单单次 Append
SingleAppend(data, size):
    fetch_add endBytePos
    memcpy to WalBuffer
    MarkReady

// ARM64 NUMA：Leader-Follower 批量提交
BatchAppend(data, size):
    // 线程将自己注册到 NUMA 节点的 per-group 链表（CAS 操作）
    node.data = data; node.size = size;
    CAS insert to m_pendingList[numaId][groupId]
    
    if 当前线程成为 Leader:
        // Leader 收集所有 Follower 的数据，一次性写入
        for each follower in m_pendingList:
            fetch_add endBytePos (for follower's size)
            memcpy follower.data to WalBuffer
            MarkReady(follower)
        // Leader 自身的数据也一起写
        notify all followers
    else:
        // Follower 等待 Leader 完成
        wait_until_notified()
```

**BatchAppend 的收益**：
- 多线程并发写 WAL 时，多个 `fetch_add` 合并为少量批量操作
- NUMA 节点内 Leader 负责本节点所有线程的数据，减少跨 NUMA 访问
- 总体降低 WAL 写入的 cache line 竞争

---

## 三、WAL Buffer 管理（dstore_wal_buffer.cpp）

### 3.1 环形 WAL Buffer 结构

```
WAL Buffer = 固定大小的环形数组（若干个 WalBlock）
  每个 WalBlock = 固定大小（如 8MB）的连续内存

写入位置：m_endBytePos mod bufferSize → 对应 WalBlock + 块内偏移
```

### 3.2 溢出检测

写入前检查写入位置是否已追上未刷盘的读取位置（Buffer 满）：

```cpp
if (writePos - m_flushedBytePos > bufferCapacity) {
    // Buffer 已满，需要等待 Flush 线程排空
    WaitForFlush(writePos - bufferCapacity);
}
```

### 3.3 Flush 刷盘触发条件

```
1. EndAtomicWal 后调用方调 WaitTargetPlsnPersist → 主动 Flush
2. WAL Buffer 超过 wal_buffer_size * flush_threshold → 后台 Flush
3. Checkpoint → 全量 Flush
```

---

## 四、WaitTargetPlsnPersist：三层等待策略

```cpp
WaitTargetPlsnPersist(walGroupPtr):
    targetPos = walGroupPtr.endBytePos;
    
    // 第 1 层: 快速检查（无锁）
    if (m_flushedBytePos >= targetPos) return;
    
    // 第 2 层: 短自旋（10 次，每次 pause/yield）
    for (int i = 0; i < 10; i++) {
        CpuRelax();
        if (m_flushedBytePos >= targetPos) return;
    }
    
    // 第 3 层: 等待数组（2048 槽位，避免惊群）
    slotId = targetPos % WAIT_ARRAY_SIZE;  // 2048
    WaitSlot &slot = m_waitArray[slotId];
    slot.AddWaiter(curThread, targetPos);
    
    // Flush 线程在推进 flushedBytePos 后：
    //   for each slot: if slot.targetPos <= newFlushedPos: notify_one
    
    Wait(slot);
```

**2048 槽位等待数组的设计**：
- 避免单个 condition_variable 的惊群效应（notify_all 唤醒所有等待者）
- 每个槽只有少量等待者（约 1-2 个），notify 精确
- 槽位按 `endBytePos % 2048` 哈希，不同提交批次分散到不同槽

---

## 五、WalStream 状态机

```
状态:
  IDLE     → 无事务在写
  WRITING  → 有事务正在 Append
  FLUSHING → 后台线程在刷盘

转换:
  IDLE → WRITING:   第一个 BeginAtomicWal
  WRITING → IDLE:   EndAtomicWal 且 pendingCount == 0
  IDLE/WRITING → FLUSHING: Flush 触发条件满足
  FLUSHING → IDLE/WRITING: Flush 完成
```

多流（multi-stream）下，每个流独立推进 `flushedBytePos`，`maxFlushedPlsn` 取所有流中最小值（最慢流决定全局可见性）。

---

## 六、WAL 记录压缩（Compress/Decompress）

```cpp
// 每种 WAL 类型有对应的压缩函数：
uint32 WalRecordHeapInsert::Compress(buf):
    // 跳过可从页面重建的字段（如固定头部）
    // 只序列化必要字段（offset + undoPtr + diskTuple）
    return compressedSize;

// AtomicGroup 级别：若所有记录都是小记录，使用 LZ4 块压缩
if (totalSize > LZ4_THRESHOLD):
    LZ4_compress(m_buf, m_bufUsed, compressedBuf);
```

---

## 七、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| BeginAtomicWal | "开始原子 WAL" | `m_bufUsed = sizeof(header)` 作为"已开始"的唯一标志 |
| PutNewWalRecord | "写入 WAL 记录" | m_buf 按 4KB 动态扩展；压缩内联进行 |
| EndAtomicWal | "提交原子 WAL" | 返回 WalGroupPtr（endBytePos），调用方用于等待落盘 |
| WAL 顺序保证 | "流内有序" | 唯一同步点：`fetch_add(endBytePos)`，无其他锁 |
| ARM64 BatchAppend | "批量提交" | Leader-Follower 协议：Leader 合并本 NUMA 节点所有写者的数据 |
| WaitTargetPlsnPersist | "等 WAL 落盘" | 三层：无锁检查 → 短自旋 → 2048 槽位等待数组 |
| WAL Buffer | "环形队列" | 写满时 WaitForFlush；按 flushedBytePos 追踪可用空间 |
| 多流 flushedPlsn | "各流独立推进" | maxFlushedPlsn = min(所有流的 flushedBytePos)，最慢流决定 |
