# dstore 诊断缓冲区设计文档

**日期**：2026-06-01  
**状态**：已评审，待实现

---

## 背景与目标

dstore 存储引擎存在因可见性判断 bug 或 buffer 层读取页面版本不正确导致的 SELECT/UPDATE 数据不一致问题，表现为客户端查询结果与预期不符。由于 dstore 层无法感知"预期值"，数据不一致只能在 SQL 层结果比较时发现。

**目标**：设计一个飞行记录仪式的诊断缓冲区，在关键代码路径上持续采集 XID、CTID、TD 状态、buffer tag 等信息，问题发生后可通过系统函数回溯查询。

---

## 设计约束

| 约束 | 决策 |
|------|------|
| 生产环境不能有常驻开销 | 默认关闭，动态开关 |
| 信息量控制 | 按 relation OID 过滤，只记录目标表 |
| 诊断覆盖面 | 读路径（heap scan + BTree index scan）+ 写路径同时采集 |
| 不侵入核心函数签名 | `XidVisibleToSnapshot` 不做修改，在上层调用点采集 |

---

## 整体架构

每个 PDB 内挂载一个 `DstoreDiagContext`，包含读路径和写路径两个独立环形缓冲区，挂在 `StoragePdb` 生命周期下。

```
StoragePdb
└── DstoreDiagContext
      ├── control: {enabled, target_rel_oid, read_capacity, write_capacity}
      ├── ReadRing:  [DstoreDiagRecord × N]  ← 原子写入索引
      └── WriteRing: [DstoreDiagRecord × N]  ← 原子写入索引
```

---

## 数据结构

### op_type 枚举

```cpp
enum DstoreDiagOpType : uint8 {
    // 读路径
    DIAG_GET_BUFFER_READ    = 0x01,
    DIAG_GET_VISIBLE_TUPLE  = 0x02,
    DIAG_BTREE_VISIBLE      = 0x03,
    // 写路径
    DIAG_SET_TD             = 0x11,
    DIAG_INSERT_UNDO        = 0x12,
    DIAG_MARK_DIRTY         = 0x13,
};
```

### 统一记录结构（目标 ≤ 128 字节）

```cpp
struct DstoreDiagRecord {
    int64            timestamp;         // 微秒时间戳
    DstoreDiagOpType op_type;
    uint8            td_status;         // TDStatus (UNOCCUPY/IN_PROGRESS/END)
    uint8            td_csn_status;     // TdCsnStatus (IS_INVALID/IS_PREV/IS_CUR)
    uint8            td_id;             // TD slot 编号
    XID              xid;               // 当前事务 XID
    uint64           td_csn;            // TD 中记录的 CSN
    uint64           snapshot_csn;      // 当前快照 CSN（读路径有效）
    ItemPointerData  ctid;              // tuple 物理位置
    BufTag           buf_tag;           // relOid + forkNum + blockNum
    uint64           page_lsn;          // 页面当时的 PLSN
    UndoRecPtr       undo_rec_ptr;      // InsertUndoRecord 时有效
    bool             visibility_result; // 可见性判断结果（读路径有效）
    uint8            _pad[3];
};
```

### 环形缓冲区

```cpp
struct DstoreDiagRing {
    uint32              capacity;       // 必须为 2 的幂次，默认 256K
    std::atomic<uint64> write_idx;      // 全局单调递增，取模寻址
    DstoreDiagRecord*   records;        // 共享内存中的数组
};

struct DstoreDiagContext {
    bool             enabled;
    Oid              target_rel_oid;    // 0 表示未启用
    DstoreDiagRing   read_ring;
    DstoreDiagRing   write_ring;
};
```

### 无锁写入

```cpp
void DiagWrite(DstoreDiagRing& ring, const DstoreDiagRecord& rec) {
    uint64 idx = ring.write_idx.fetch_add(1, std::memory_order_relaxed);
    ring.records[idx & (ring.capacity - 1)] = rec;
}
```

每个线程通过 `fetch_add` 独立获得唯一 slot，互不干扰，旧记录被自然覆盖。

### 过滤宏

```cpp
#define DIAG_SHOULD_RECORD(pdb, buf_tag) \
    (pdb->diagCtx.enabled && \
     (buf_tag).relOid == pdb->diagCtx.target_rel_oid)
```

未启用时短路求值，开销仅一次 bool 读取。

---

## 控制接口

```sql
-- 启用：指定 PDB 和目标表
SELECT dstore_diag_enable(
    pdb_id         int,
    rel_oid        oid,
    read_capacity  int  DEFAULT 262144,  -- 256K 条
    write_capacity int  DEFAULT 262144
);

-- 停用（保留数据，停止新写入）
SELECT dstore_diag_disable(pdb_id int);

-- 清空并释放内存
SELECT dstore_diag_reset(pdb_id int);
```

### 启用流程

```
dstore_diag_enable(pdb_id, rel_oid)
  ├── 检查 DstoreDiagContext 是否已存在
  ├── 若不存在：DstorePalloc 两个 ring 数组（PDB 内存上下文）
  ├── 设置 context.target_rel_oid = rel_oid
  └── 原子写 context.enabled = true   ← 最后一步，确保内存就绪后才开始采集
```

### 停用流程

```
dstore_diag_disable(pdb_id)
  ├── 原子写 context.enabled = false  ← 立即停止新写入
  ├── 短暂 spin 等待正在执行的 DiagWrite 完成（< 1μs）
  └── 保留 ring 数据供后续查询
```

内存在显式调用 `dstore_diag_reset` 前保留；PDB 关闭时随 PDB 内存上下文自动释放。

---

## 插桩点

### 读路径（写入 ReadRing）

**1. `GetBuffer`（页面读取）**

- 位置：`src/buffer/dstore_buf_mgr.cpp`，`GetBuffer()` 返回 buffer 之后
- 采集字段：`buf_tag`、`page_lsn`、`xid`
- 诊断价值：记录每次页面被读入时的 PLSN，判断是否读到旧版本页面

**2. `GetVisibleTuple`（heap 可见性判断）**

- 位置：`src/heap/dstore_heap_scan.cpp`，三条可见性分支各自记录
- 采集字段：`ctid`、`td_id`、`td_status`、`td_csn_status`、`td_csn`、`snapshot_csn`、`visibility_result`、`buf_tag`
- 诊断价值：TD 三态路径选择和 CSN 比较的完整现场；`XidVisibleToSnapshot` 不做修改，结果通过此点上报

**3. BTree 索引扫描可见性判断**

- 位置：`src/index/dstore_btree_scan.cpp`，index tuple 可见性决策完成后
- 采集字段：`ctid`（`m_link` = heapCtid）、`td_id`（`m_tdId`）、`td_status`、`td_csn_status`、`td_csn`、`snapshot_csn`、`visibility_result`、`buf_tag`（BTree 页面）
- 诊断价值：索引层 MVCC 判断路径，覆盖 BTree 索引扫描场景

### 写路径（写入 WriteRing）

**4. `SetTd`（TD 槽写入）**

- 位置：`src/page/dstore_data_page.cpp`，写入 TD 槽之后
- 采集字段：`xid`、`ctid`、`td_id`、`td_status`、`td_csn_status`、`td_csn`、`buf_tag`
- 诊断价值：记录 TD 槽被占用/复用/释放的时刻，发现 `IS_PREV_XID_CSN` 设置错误或提前 Reset

**5. `InsertUndoRecord`（Undo 写入）**

- 位置：`src/undo/dstore_undo_record.cpp`，写入完成后
- 采集字段：`xid`、`ctid`、`undo_rec_ptr`、`buf_tag`
- 诊断价值：验证 `TD.undoRecPtr` 与实际写入位置是否一致

**6. `MarkDirty`（页面标脏）**

- 位置：`src/buffer/dstore_buf_mgr.cpp`，`MarkDirty()` 内
- 采集字段：`xid`、`buf_tag`、`page_lsn`
- 诊断价值：记录页面被标脏时的 PLSN，验证 WAL-First 约束

### 插桩点汇总

| # | 插桩点 | 所在文件 | Ring | 核心诊断字段 |
|---|--------|---------|------|------------|
| 1 | `GetBuffer` | `dstore_buf_mgr.cpp` | 读 | buf_tag, page_lsn |
| 2 | `GetVisibleTuple` | `dstore_heap_scan.cpp` | 读 | ctid, td_status, td_csn_status, td_csn, snapshot_csn, visibility_result |
| 3 | BTree scan 可见性 | `dstore_btree_scan.cpp` | 读 | ctid(heapCtid), td_id, td_status, td_csn, snapshot_csn, visibility_result |
| 4 | `SetTd` | `dstore_data_page.cpp` | 写 | xid, ctid, td_id, td_status, td_csn_status |
| 5 | `InsertUndoRecord` | `dstore_undo_record.cpp` | 写 | xid, ctid, undo_rec_ptr |
| 6 | `MarkDirty` | `dstore_buf_mgr.cpp` | 写 | buf_tag, page_lsn |

---

## 查询接口

### 系统函数签名

```sql
SELECT * FROM dstore_diag_query(
    pdb_id         int,
    start_time     timestamptz DEFAULT NULL,
    end_time       timestamptz DEFAULT NULL,
    op_type_filter text[]      DEFAULT NULL,  -- NULL 表示返回全部类型
    path_filter    text        DEFAULT 'both' -- 'read' / 'write' / 'both'
);
```

### 返回列

| 列名 | 类型 | 说明 |
|------|------|------|
| `seq` | int8 | ring 内单调写入序号，用于同 ring 内排序 |
| `timestamp` | timestamptz | 记录时间 |
| `op_type` | text | `GET_BUFFER_READ` / `GET_VISIBLE_TUPLE` / `BTREE_VISIBLE` / `SET_TD` / `INSERT_UNDO` / `MARK_DIRTY` |
| `xid` | int8 | 事务 XID |
| `ctid` | tid | tuple 物理位置 |
| `td_id` | int2 | TD slot 编号 |
| `td_status` | text | `UNOCCUPY` / `IN_PROGRESS` / `END` |
| `td_csn_status` | text | `IS_INVALID` / `IS_PREV_XID_CSN` / `IS_CUR_XID_CSN` |
| `td_csn` | int8 | TD 中记录的 CSN |
| `snapshot_csn` | int8 | 当前快照 CSN（读路径有效，写路径为 0）|
| `visibility_result` | bool | 可见性判断结果（读路径有效）|
| `rel_oid` | oid | 页面所属 relation |
| `block_num` | int8 | 页面块号 |
| `page_lsn` | int8 | 页面当时的 PLSN |
| `undo_rec_ptr` | int8 | Undo 记录指针（`INSERT_UNDO` 时有效）|

### 查询实现

扫描读 ring 和写 ring 各一次，按 `timestamp` 归并排序后输出：

```
dstore_diag_query(pdb_id, start, end, ...)
  ├── 定位 pdb->diagCtx
  ├── 扫描 read_ring：过滤时间范围 + op_type_filter
  ├── 扫描 write_ring：过滤时间范围 + op_type_filter
  ├── 按 timestamp 归并两个结果集
  └── 以 SRF（Set Returning Function）形式返回
```

### 典型诊断查询

**场景一：重建某 ctid 的完整操作时序**
```sql
SELECT seq, op_type, xid, td_status, td_csn, snapshot_csn, visibility_result
FROM dstore_diag_query(1, '2026-06-01 10:00:00', '2026-06-01 10:01:00')
WHERE ctid = '(42,3)'
ORDER BY timestamp, seq;
```

**场景二：找出所有可见性判断为 false 的 heap tuple**
```sql
SELECT *
FROM dstore_diag_query(1, path_filter => 'read',
                        op_type_filter => ARRAY['GET_VISIBLE_TUPLE'])
WHERE visibility_result = false
ORDER BY timestamp;
```

**场景三：检查某页面的 WAL-First 违反**
```sql
SELECT seq, op_type, xid, block_num, page_lsn
FROM dstore_diag_query(1, path_filter => 'read',
                        op_type_filter => ARRAY['GET_BUFFER_READ'])
WHERE block_num = 42
ORDER BY timestamp;
-- 同一 block_num 的 page_lsn 若出现回退，则为 WAL-First 违反
```

---

## 内存规格

| 项 | 数值 |
|----|------|
| 单条记录大小 | ≤ 128 字节 |
| 默认 ring 容量 | 256K 条 |
| 单个 ring 内存 | 32 MB |
| 两个 ring 合计（per PDB）| 64 MB |
| 最大可配置容量 | 由 `dstore_diag_enable` 参数控制 |

---

## 实现文件清单

| 文件 | 说明 |
|------|------|
| `include/diag/dstore_diag.h` | `DstoreDiagRecord`、`DstoreDiagRing`、`DstoreDiagContext` 定义，`DiagWrite` inline 函数，`DIAG_SHOULD_RECORD` 宏 |
| `src/diag/dstore_diag.cpp` | `dstore_diag_enable` / `dstore_diag_disable` / `dstore_diag_reset` 实现 |
| `src/diag/dstore_diag_query.cpp` | `dstore_diag_query` SRF 实现 |
| `src/buffer/dstore_buf_mgr.cpp` | 插桩点 1（GetBuffer）、插桩点 6（MarkDirty）|
| `src/heap/dstore_heap_scan.cpp` | 插桩点 2（GetVisibleTuple）|
| `src/index/dstore_btree_scan.cpp` | 插桩点 3（BTree 可见性）|
| `src/page/dstore_data_page.cpp` | 插桩点 4（SetTd）|
| `src/undo/dstore_undo_record.cpp` | 插桩点 5（InsertUndoRecord）|
