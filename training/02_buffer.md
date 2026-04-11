# DStore Buffer 模块培训材料

## 第一部分：缓冲池基本概念

### 1.1 BufferDesc：核心页面描述符

`include/buffer/dstore_buf.h`（第 650-993 行）：

```cpp
struct BufferDesc {
    BufBlock bufBlock;                          // 指向实际的 8KB 页面数据块
    BufferTag bufTag;                           // 页面标识（pdbId, pageId）
    gs_atomic_uint64 state;                     // 64 位状态变量
    LruNode lruNode;                            // LRU 链表节点
    LWLock contentLwLock;                       // 内容读写锁
    std::atomic<BufferDesc *> nextDirtyPagePtr; // 脏页队列指针
    PageVersion pageVersionOnDisk;              // 磁盘页面版本（GLSN/PLSN）
};
```

#### state 变量（64位原子）的含义：

| 位 | 标志名 | 含义 |
|----|--------|------|
| 低32位 | refcount | 引用计数（多少个会话在使用） |
| 33 | BUF_CONTENT_DIRTY | 强脏标记，必须写回磁盘 |
| 34 | BUF_VALID | 数据有效，可安全读取 |
| 36 | BUF_IO_IN_PROGRESS | I/O 操作进行中 |
| 38 | BUF_HINT_DIRTY | 弱脏标记 |
| 41 | BUF_IS_WRITING_WAL | 正在写 WAL |

### 1.2 缓冲池组织方式

#### 哈希表快速查找（BufTable）

`include/buffer/dstore_buf_table.h`（第 26-116 行）：

- 4096 个哈希分区，每个分区有独立的 LWLock
- 平均 O(1) 查找，支持千万级页面的并发查询
- **快速路径无锁**：命中时几乎无竞争

#### LRU 三层淘汰

`include/buffer/dstore_buf_lru.h`（第 33-150 行）：

```
新分配 → HOT_LIST（频繁访问）
              ↓（冷却）
          LRU_LIST（温冷）
              ↓（继续冷却）
        CANDIDATE_LIST（待淘汰）
              ↓（被新页驱逐）
            FREE
```

### 1.3 Pin/Unpin：引用计数与页面驻留

DStore 采用**双层引用计数**设计：

```cpp
// 层次1：线程本地计数（零开销）
PrivateRefCountEntry *privateRef = thrd->GetBufferPrivateRefCount();
privateRef->refcount++;

// 层次2：全局共享计数（仅首次 pin 时原子操作）
state += BUF_REFCOUNT_ONE;
```

**核心规则**：只有全局共享计数为 0 时，页面才能被 LRU 淘汰。

---

## 第二部分：页面读取路径

### 2.1 Read() 完整流程

`src/buffer/dstore_buf_mgr.cpp`（第 596-687 行）：

```
BufMgr::Read(pdbId, pageId, LW_EXCLUSIVE)
  ↓
1. LookupBuffer()  ──→ FOUND：跳到第4步
  ↓ MISS
2. AllocBufferForBaseBuffer()  ← 从 LRU 获取可淘汰页面
  ↓
3. StartIo() → VFS::ReadPageSync() → TerminateIo()
  ↓（标记 BUF_VALID）
4. LWLockAcquire(contentLwLock, mode)
  ↓
返回已 pin、已加锁的 BufferDesc
```

### 2.2 StartIo / TerminateIo：I/O 并发控制

`src/buffer/dstore_buf_mgr.cpp`（第 3641-3717 行）：

**StartIo** 核心逻辑：
```cpp
// 1. 获取页面级 I/O 排他锁
DstoreLWLockAcquire(ioInProgressLwLock, LW_EXCLUSIVE);
// 2. 检查 BUF_IO_IN_PROGRESS，若已在进行 → 等待
// 3. 设置 BUF_IO_IN_PROGRESS 标志
```

**TerminateIo** 核心逻辑：
```cpp
// 1. 清除 BUF_IO_IN_PROGRESS
// 2. 成功时：清除 BUF_CONTENT_DIRTY，设置 BUF_VALID
// 3. 释放 I/O 锁，唤醒等待线程
```

---

## 第三部分：页面写入路径

### 3.1 MarkDirty()：脏页标记

`src/buffer/dstore_buf_mgr.cpp`（第 874-932 行）：

```cpp
RetStatus BufMgr::MarkDirty(BufferDesc *bufferDesc) {
    // 前提：必须持有 contentLwLock 独占锁
    
    // 1. 原子设置脏标志
    state |= (BUF_CONTENT_DIRTY | BUF_HINT_DIRTY);
    
    // 2. 将脏页推入后台写入队列
    bgPageWriterMgr->PushDirtyPageToQueue(bufferDesc, bgWriterSlotId);
    
    // 3. 记录文件版本（用于文件删除检查）
    bufferDesc->SetFileVersion(WalUtils::GetFileVersion(...));
}
```

**脏页队列（DirtyPageQueue）**：
- MPSC（多生产者单消费者）设计
- 脏页按生成顺序入队，保证磁盘 I/O 的 WAL 顺序
- 每个 WAL 流对应一个独立的脏页队列

### 3.2 WriteBlock()：页面写回磁盘

`src/buffer/dstore_buf_mgr.cpp`（第 1042-1145 行）：

```cpp
RetStatus BufMgr::WriteBlock(BufferDesc *bufferDesc) {
    // 【WAL-First 关键步骤】
    PrepareCheckPageBeforeStartIo(bufferDesc);  // 等待 WAL 先落盘
    
    // 获取 I/O 锁
    if (!StartIo(bufferDesc, false)) return DSTORE_SUCC; // 已被他人写
    
    // 更新校验和
    bufferDesc->GetPage()->SetChecksum();
    
    // 同步写入磁盘
    vfs->WritePageSync(pageId, bufferDesc->GetPage());
    
    // 清除脏标记
    TerminateIo(bufferDesc, true, 0);
}
```

---

## 第四部分：WAL-First 协议

### 4.1 核心原则

**Write-Ahead Logging-First**：WAL 必须先于数据页落盘。

保证：即使宕机，WAL 中的修改记录也能完整重放恢复数据。

### 4.2 PrepareCheckPageBeforeStartIo()

`src/buffer/dstore_buf_mgr.cpp`（第 934-964 行）：

```cpp
// 等待页面对应的 WAL 已持久化到磁盘
walStreamMgr->GetWritingWalStream()->WaitTargetPlsnPersist(page->GetPlsn());
```

**完整时序保证**：

```
应用线程：修改页面 → 写 WAL（PLSN=100）→ MarkDirty()
后台线程：WriteBlock()
  └─ PrepareCheckPageBeforeStartIo()
      └─ WaitTargetPlsnPersist(100)  ← 阻塞到 WAL 落盘
      └─ VFS::WritePageSync()        ← 页面数据落盘

结论：页面落盘时，对应 WAL 一定已落盘 ✓
```

### 4.3 LSN 体系

| 术语 | 含义 | 用途 |
|------|------|------|
| GLSN | 全局 LSN，跨流单调递增 | 多流顺序、MVCC 排序 |
| PLSN | 流内物理偏移 | 定位 WAL 文件中的位置 |
| Recovery PLSN | 脏页标记时的 WAL 位置 | 确保恢复完整性 |

**关系约束**：
```
脏页 Recovery PLSN < 页面实际 PLSN ≤ WAL 已落盘 PLSN → 允许写页面
```

---

## 第五部分：Checkpoint 机制

### 5.1 Checkpoint 的三大作用

1. **截断 WAL**：清除已完全持久化的旧 WAL 文件
2. **建立恢复点**：故障后只需从最近的 checkpoint 开始重放
3. **减少恢复时间**：无 checkpoint 需重放全部 WAL

### 5.2 Checkpoint 主流程

`include/buffer/dstore_checkpointer.h`（第 143-250 行）：

```
CheckpointerMain()
  ↓
1. 等待 checkpoint 请求（定时或手动触发）
  ↓
2. bufMgr->FlushAll()  ← 将所有脏页刷到磁盘
  ↓
3. walMgr->FlushWAL()  ← 将 WAL 刷到磁盘
  ↓
4. 写 WalCheckPoint 记录（含 maxFlushPlsn）
  ↓
5. FinishCheckpoint()  ← 通知等待线程
```

### 5.3 恢复时的 Checkpoint 使用

```
读最后一个 Checkpoint → 获取 maxFlushPlsn = X
         ↓
PLSN ≤ X 的页面：已刷盘，无需重做
PLSN > X 的页面：从 WAL 重放（Recovery 阶段处理）
```

---

## 第六部分：关键文件速查

| 功能 | 头文件 | 源文件 | 行号 |
|------|--------|--------|------|
| 核心数据结构 | dstore_buf.h | - | 650-993 |
| BufferDesc 操作 | dstore_buf.h | dstore_buf_desc.cpp | 177-326 |
| 读取流程 | dstore_buf_mgr.h | dstore_buf_mgr.cpp | 596-687 |
| MarkDirty | dstore_buf_mgr.h | dstore_buf_mgr.cpp | 874-932 |
| WriteBlock | dstore_buf_mgr.h | dstore_buf_mgr.cpp | 1042-1145 |
| WAL-First 检查 | - | dstore_buf_mgr.cpp | 934-964 |
| 哈希表 | dstore_buf_table.h | dstore_buf_table.cpp | 26-116 |
| LRU | dstore_buf_lru.h | dstore_buf_lru.cpp | 33-150 |
| 脏页队列 | dstore_bg_page_writer_mgr.h | dstore_bg_page_writer_mgr.cpp | 73-212 |
| Checkpoint | dstore_checkpointer.h | dstore_checkpointer.cpp | 143-250 |
