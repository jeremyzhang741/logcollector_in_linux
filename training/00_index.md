# DStore 存储引擎培训材料总目录

本培训材料由 9 个模块组成，面向数据库内核新手，结合具体代码（文件路径+行号）深入讲解 dstore 各核心模块。

---

## 模块列表

| 序号 | 模块 | 文件 | 核心主题 |
|------|------|------|---------|
| 01 | Transaction | [01_transaction.md](01_transaction.md) | XID/CSN、事务生命周期、MVCC 可见性 |
| 02 | Buffer | [02_buffer.md](02_buffer.md) | 缓冲池、脏页管理、WAL-First 协议 |
| 03 | Undo | [03_undo.md](03_undo.md) | Undo 记录结构、版本链、旧版本重建 |
| 04 | Index/BTree | [04_index_btree.md](04_index_btree.md) | BTree 结构、分裂、索引 MVCC |
| 05 | WAL | [05_wal.md](05_wal.md) | WAL 写入流程、多日志流、Redo 恢复 |
| 06 | Heap | [06_heap.md](06_heap.md) | 页面布局、TD 机制、可见性判断、读写路径 |
| 07 | 系统串联 | [07_system_integration.md](07_system_integration.md) | 写入/读取/恢复三条主线、模块间契约 |
| 08 | FSM | [08_fsm.md](08_fsm.md) | 空闲空间管理、多层树结构、插入协作 |
| 09 | Checkpoint | [09_checkpoint.md](09_checkpoint.md) | 脏页刷盘、diskRecoveryPlsn、WAL截断 |

---

## 推荐学习路径

### 阶段一：基础概念（第 1-2 周）

**目标**：理解存储引擎的基本工作方式

1. 阅读 **Transaction 模块**（01）
   - 理解 XID 结构和 CSN 的作用
   - 掌握事务从启动到提交/回滚的完整状态机
   - 重点：两阶段提交和 PENDING_COMMIT 状态

2. 阅读 **Buffer 模块**（02）
   - 理解缓冲池的组织（哈希表 + LRU）
   - 掌握 Pin/Unpin 机制和双层引用计数
   - 重点：WAL-First 协议和脏页写入顺序

### 阶段二：MVCC 深入（第 3-4 周）

**目标**：彻底理解多版本并发控制

3. 阅读 **Undo 模块**（03）
   - 理解 Undo 的两条链（事务链 + 跨事务链）
   - 掌握 FetchUndoRecordByMatchedCtid 的跨事务跳转
   - 重点：ConstructCrTuple 的旧版本重建过程

4. 回顾 **Transaction 模块** MVCC 可见性部分
   - XidVisibleToSnapshot 的完整决策树
   - XidStatus 的延迟初始化和 PENDING_COMMIT 重查

### 阶段三：存储引擎写入路径（第 5-6 周）

**目标**：理解数据修改的完整落盘过程

5. 深入 **WAL 模块**（05）
   - 掌握 BeginAtomicWal → EndAtomicWal 完整流程
   - 理解多日志流架构和 Failover 场景
   - 重点：PLSN vs GLSN 的设计意图和 BgWalWriter 刷盘

6. 串联 **Buffer + WAL** 的协作
   - MarkDirty → 脏页队列 → WriteBlock → WAL-First 检查
   - Checkpoint 与 WAL 截断的关系

### 阶段四：索引机制（第 7-8 周）

**目标**：理解 BTree 的并发写和 MVCC 读

7. 阅读 **Index/BTree 模块**（04）
   - 掌握 BTree 三层页面结构和插入/查找流程
   - 理解页面分裂的 4 种 WAL 记录类型
   - 重点：索引 MVCC 与堆表 MVCC 的差异

---

## 关键概念速查

### 事务相关

| 概念 | 简述 | 详见 |
|------|------|------|
| XID | 20bit zoneId + 44bit logicSlotId | 01_transaction §1.1 |
| CSN | 全局单调提交序号，MVCC 时间轴 | 01_transaction §1.2 |
| TransactionSlot | 事务状态、CSN、Undo 指针的持久存储 | 01_transaction §2.1 |
| XidVisibleToSnapshot | MVCC 可见性核心函数 | 01_transaction §4.1 |

### 存储相关

| 概念 | 简述 | 详见 |
|------|------|------|
| BufferDesc | 缓冲页描述符，64bit state 含引用计数+标志 | 02_buffer §1.1 |
| WAL-First | 页面落盘前必须等待 WAL 落盘 | 02_buffer §4 |
| UndoRecPtr | fileId+pageId+offset 三元组 | 03_undo §2.1 |
| TdPreInfo | Undo 记录保存的前版本 TD 快照 | 03_undo §2.3 |
| PLSN/GLSN | 流内物理偏移 / 跨流全局序号 | 05_wal §1.2 |
| WalStreamManager | 管理最多 1024 个 WAL 流 | 05_wal §4.1 |

### TD（Transaction Descriptor）相关

| 概念 | 简述 | 详见 |
|------|------|------|
| TD slot | 页面内嵌的事务描述符槽位 | 之前对话 Stage 4 |
| TupleTdStatus | DETACH/ATTACH_NEW/ATTACH_HISTORY | 之前对话 Stage 4 |
| recycleMinCsn | TD 可完全重置的 CSN 阈值 | 之前对话 Stage 4 |
| canResetTd | 有进行中事务时禁止 Reset | 之前对话 Stage 4 |

---

## 源码目录结构

```
dstore-main/
├── include/
│   ├── transaction/     # XID、CSN、事务类型定义
│   ├── buffer/          # BufferDesc、BufMgr、LRU、Checkpoint
│   ├── undo/            # UndoRecord、UndoZone、TransactionSlot
│   ├── page/            # Page、DataPage、TD、IndexPage
│   ├── tuple/           # HeapTuple、IndexTuple
│   ├── index/           # BTree 各阶段接口
│   └── wal/             # WalStream、WalRecord、WalRecovery
│
└── src/
    ├── transaction/     # 事务生命周期实现
    ├── buffer/          # 缓冲池管理实现
    ├── undo/            # Undo Zone 实现
    ├── page/            # Heap 页面操作实现
    ├── index/           # BTree 各操作实现
    └── wal/             # WAL 写入、刷盘、恢复实现
```

---

*培训材料由 Agent Team 并行分析生成，基于 dstore-main 代码库（2026年）。*
