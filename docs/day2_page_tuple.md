# Day 2：Page 结构 + Tuple 格式

## 学习目标
理解所有数据在磁盘上的物理布局：从字节级别的页面头，到 TD 数组、ItemId 数组、Tuple 数据区，再到每一行的 HeapDiskTuple 格式。

---

## 1. 关键类型定义（物理地址的基础）

在理解页面之前，先明确定位一个页面/行所需的基础类型。

### 1.1 物理地址类型

```cpp
// interface/common/dstore_common_utils.h
using BlockNumber = uint32_t;   // 文件内块号
using FileId      = uint16_t;   // 文件ID（一个表空间可有多个文件）
```

```cpp
// interface/page/dstore_page_struct.h
struct PageId {
    FileId      m_fileId;    // 所在文件
    BlockNumber m_blockId;   // 文件内块号
};
```

```cpp
// interface/page/dstore_itemptr.h
union ItemPointerData {   // 64bit，原子可读写
    uint64_t m_placeHolder;
    struct Value {
        PageId       m_pageid;   // 16bit fileId + 32bit blockId
        OffsetNumber m_offset;   // 16bit 槽位号（从1开始）
    } val;
};
```

**一行的物理地址 = ItemPointerData**，即 `fileId(16) + blockId(32) + offset(16)` = 64 bit。

这也是 `ctid` 的底层表示。

### 1.2 PageType 枚举

```cpp
// interface/page/dstore_page_struct.h
enum class PageType : uint8_t {
    INVALID_PAGE_TYPE = 0,
    HEAP_PAGE_TYPE,          // 普通数据页
    INDEX_PAGE_TYPE,         // BTree 索引页
    TRANSACTION_SLOT_PAGE,   // 事务槽页（Undo 相关）
    UNDO_PAGE_TYPE,          // Undo 页
    FSM_PAGE_TYPE,           // 空闲空间管理页
    FSM_META_PAGE_TYPE,      // FSM 元页
    DATA_SEGMENT_META_PAGE_TYPE,
    HEAP_SEGMENT_META_PAGE_TYPE,
    // ... 其他类型
};
```

页面 `ftruncate` 扩展出的新页初始为全零，`PageType == INVALID_PAGE_TYPE(0)`，用于判断页面是否已分配。

---

## 2. 通用页面头（Page）

### 2.1 PageHeader 结构

```cpp
// include/page/dstore_page.h
struct Page {
    struct PACKED PageHeader {
        uint16 m_checksum;         // 页面校验和
        struct {
            uint16 m_needCrc : 1;  // special 区域是否需要 CRC 校验
            uint16 m_offset  : 14; // special 区域在页面中的偏移
            uint16 m_reserved: 1;
        } m_special;
        uint64 m_glsn;             // Global LSN（跨流全局顺序）
        uint64 m_plsn;             // Physical LSN（WAL 流内字节偏移）
        WalId  m_walId;            // 产生最后修改的 WAL 流 ID
        uint16 m_flags;            // 标志位（PAGE_HAS_FREE_LINES 等）
        uint16 m_lower;            // ItemId 数组末尾（向右增长）
        uint16 m_upper;            // Tuple 数据区起始（向左增长）
        uint16 m_type;             // PageType 枚举值
        PageId m_myself;           // 页面自身的 PageId（自描述）
    };
    PageHeader m_header;
    // ...
};
```

**关键字段解释**：

| 字段 | 位宽 | 作用 |
|------|------|------|
| `m_plsn` / `m_walId` | 64+64 | WAL-First 检查：必须先将 WAL 刷盘才能写页面 |
| `m_glsn` | 64 | 跨流排序，用于崩溃恢复时确定重放顺序 |
| `m_lower` | 16 | 指向 ItemId 数组的末尾（新增 ItemId 时向右移动） |
| `m_upper` | 16 | 指向 Tuple 数据区的开头（写入 Tuple 时向左移动） |
| `m_flags` | 16 | 包含 `PAGE_HAS_FREE_LINES`, `PAGE_TUPLE_PRUNABLE` 等 |
| `m_myself` | 48 | 页面的自我描述，用于校验读到的页面是否与预期一致 |

### 2.2 Free Space 的计算

```
                    m_lower                 m_upper
                       ↓                       ↓
[PageHeader|DataHeader|TD数组|ItemId数组|  空闲区  |Tuple数据]
                                       ←—free→
```

**Free space = `m_upper - m_lower`**，两个指针向中间逼近。当 `m_upper <= m_lower` 时页面满。

### 2.3 SetLsn — WAL-First 的体现

```cpp
void Page::SetLsn(WalId walId, uint64 plsn, uint64 glsn, bool newPage = false)
```

每次修改数据页后必须调用 `SetLsn`，将对应 WAL 记录的 LSN 写入页头。Buffer Manager 在将脏页写回磁盘前会检查：
- `page->m_plsn` 对应的 WAL 记录必须已经刷盘
- 否则禁止写页（WAL-First 协议的强制执行）

---

## 3. 数据页（DataPage）

`DataPage` 继承自 `Page`，是 Heap 页和 Index 页的共同基类。

### 3.1 页面头层次

```
Page (sizeof = 48B，含 PageHeader)
└── DataPage
      ├── DataPageHeader (sizeof = 16B)
      │     - tdCount      : uint8   — TD 槽位数量
      │     - versionNum   : uint32  — 页面版本号（Debug 用）
      │     - headerOffset : uint16  — 数据区起始偏移
      │     - segmentCreateXid       — 建 Segment 时的 XID
      └── （Heap：HeapPageHeader，Index：BtrPageHeader）
            └── m_data[BLCKSZ - HEAP_PAGE_DATA_OFFSET]
```

### 3.2 DataPageHeader 的 headerOffset

`headerOffset` 存储了数据区（TD 数组开始处）相对于页面起始的偏移。

```cpp
// include/page/dstore_data_page.h
constexpr uint32 DATA_PAGE_HEADER_SIZE  = sizeof(Page) + sizeof(DataPageHeader);   // ≈ 64B
constexpr uint32 HEAP_PAGE_DATA_OFFSET  = DATA_PAGE_HEADER_SIZE + sizeof(HeapPageHeader);  // 88B

inline char *DataPage::GetDataOffset()
{
    return reinterpret_cast<char *>(this) + dataHeader.headerOffset;
}
```

### 3.3 m_flags 标志位

```cpp
const uint16 PAGE_HAS_FREE_LINES      = (1);       // 有可复用的 ItemId 槽位
const uint16 PAGE_TUPLE_PRUNABLE      = (1 << 1);  // 有可剪枝的已删除 Tuple
const uint16 PAGE_ITEM_PRUNABLE       = (1 << 2);  // 有可剪枝的 redirect ItemId
const uint16 PAGE_IS_NEW_PAGE         = (1 << 3);  // 新分配的页面
const uint16 PAGE_IS_EXTEND_CR_PAGE   = (1 << 4);  // CR 扩展页
```

---

## 4. TD（Transaction Descriptor）数组

### 4.1 TD 结构（每个 TD 槽 = 48 字节）

```cpp
// include/page/dstore_td.h
struct TD {
    uint64      m_xid;         // 占用此 TD 的事务 ID
    CommitSeqNo m_csn;         // 该事务的提交 CSN（未提交时为 INVALID_CSN）
    uint64      m_undoRecPtr;  // 该事务在本页最新 Undo 记录的位置
    uint64      m_lockerXid;   // 行锁持有者（死锁检测用）
    CommandId   m_commandId;   // 命令号（同一事务内多条 SQL 的序号）
    uint16      m_status   : 2;    // TDStatus
    uint16      m_csnStatus: 2;    // TdCsnStatus
    uint16      m_pad      : 12;
};
```

### 4.2 TDStatus 枚举

```cpp
enum class TDStatus {
    UNOCCUPY_AND_PRUNEABLE = 0,  // 空闲，可分配
    OCCUPY_TRX_IN_PROGRESS,      // 被进行中的事务占用（不准确，不实时更新）
    OCCUPY_TRX_END,              // 上一个事务已结束（提交或回滚）
};
```

**OCCUPY_TRX_IN_PROGRESS 是"不准确"状态**：事务提交后 TD 的 status 不会立刻改为 `OCCUPY_TRX_END`（避免性能开销），需要通过查询 XID 状态来确认真实情况。

### 4.3 TdCsnStatus 枚举

```cpp
enum TdCsnStatus : uint8 {
    IS_INVALID       = 0,  // m_csn 无效（事务未提交）
    IS_PREV_XID_CSN,        // m_csn 属于前一个事务（TD 被复用时保留）
    IS_CUR_XID_CSN          // m_csn 属于当前事务（正常已提交状态）
};
```

**IS_PREV_XID_CSN 的设计意图**：

当一个 TD 槽被新事务占用（`SetTd()`），旧事务留下的 CSN 并不直接清零，而是设为 `IS_PREV_XID_CSN`。这样，仍在运行的旧快照扫描到仍以旧 TD 指向的 Tuple 时，能直接从 TD 读出旧事务的 CSN 并完成可见性判断，无需再回溯 Undo 链。

```cpp
// 在 SetTd() 中的关键逻辑（dstore_data_page.h:158-178）
inline void DataPage::SetTd(uint8 tdId, Xid xid, UndoRecPtr undoPtr, CommandId commandId) {
    TD *td = GetTd(tdId);
    if (td->GetXid() != INVALID_XID && td->GetXid() != xid) {
        // TD 被新事务占用，旧 CSN 保留为 IS_PREV_XID_CSN
        if (td->TestCsnStatus(IS_CUR_XID_CSN)) {
            td->SetCsnStatus(IS_PREV_XID_CSN);   // ← 保留旧 CSN
        }
    }
    td->SetXid(xid);
    td->SetUndoRecPtr(undoPtr);
    td->SetStatus(TDStatus::OCCUPY_TRX_IN_PROGRESS);
}
```

### 4.4 TD 数组常数

```cpp
// include/page/dstore_td.h
constexpr uint8 MIN_TD_COUNT     = 2;    // 最少 2 个（Pivot 页不需要）
constexpr uint8 DEFAULT_TD_COUNT = 4;    // 初始分配 4 个
constexpr uint8 MAX_TD_COUNT     = 128;  // 最多 128 个
constexpr uint8 EXTEND_TD_NUM    = 2;    // 每次扩展 2 个
```

TD 数组紧跟在页头之后。每当所有 TD 都被占用时，可以在页面中扩展（前提是有足够的空闲空间）。扩展会消耗 Free Space，同时记录 WAL。

---

## 5. ItemId 数组

### 5.1 ItemId 结构（4 字节）

```cpp
// include/page/dstore_itemid.h
struct ItemId {
    union {
        ItemType m_placeHolder;   // uint32
        struct {                  // 正常状态（Normal/Unused）
            uint32 m_flags  : 2;  // ItemIdState
            uint32 m_offset : 15; // Tuple 在页面中的字节偏移
            uint32 m_len    : 15; // Tuple 长度（字节）
        } direct;
        struct {                  // NoStorage 状态（Tuple 数据已被回收）
            uint32 m_flags    : 2;
            uint32 m_tdId     : 8;  // 替代存储 TD ID
            uint32 m_tdStatus : 2;  // 替代存储 TupleTdStatus
            uint32 m_tupLiveMode : 3;
            uint32 m_unused   : 17;
        } redirect;
    };
};
```

### 5.2 ItemIdState 枚举

```cpp
enum ItemIdState : uint8 {
    ITEM_ID_UNUSED = 0,               // 未使用，可立即重用
    ITEM_ID_NORMAL,                   // 正常，指向有效 Tuple 数据
    ITEM_ID_UNREADABLE_RANGE_HOLDER,  // 已回滚的 Tuple，仅保留键范围占位（BTree 用）
    ITEM_ID_NO_STORAGE                // Tuple 数据已被压缩回收，ItemId 保存 TD 信息
};
```

**ITEM_ID_NO_STORAGE 状态**：

当 Tuple 的数据空间被回收（Prune）后，ItemId 从 `direct` 格式切换为 `redirect` 格式：
- `m_offset` 和 `m_len` 字段消失
- 改为存储 `m_tdId` + `m_tdStatus`（保留 MVCC 可见性判断所需信息）
- 读取该 Tuple 时需要从 Undo 链重建

### 5.3 ItemId 在页面中的位置

```
GetDataOffset()          → TD数组开头
GetItemIdPtr(offset)     → 第 offset 个 ItemId（从 1 开始）
GetItemIdArrayStartPtr() → ItemId 数组开头
GetItemIdArrayEndPtr()   → m_lower 指针处
```

计算公式：
```cpp
itemId位置 = GetDataOffset() + TdDataSize() + (offset - 1) * sizeof(ItemId)
```

即 `TD数组之后，按 slot 号线性排列`。

---

## 6. Heap 页（HeapPage）

### 6.1 完整布局

```
HeapPage（BLCKSZ = 8192B）:
┌────────────────────────────────────────────────────────────┐  offset 0
│  Page::PageHeader                             ~48B         │  checksum/lsn/lower/upper/type
├────────────────────────────────────────────────────────────┤
│  DataPage::DataPageHeader                     ~16B         │  tdCount/headerOffset
├────────────────────────────────────────────────────────────┤  = DATA_PAGE_HEADER_SIZE(64B)
│  HeapPage::HeapPageHeader                     ~24B         │
│    - potentialDelSize   : uint16                           │  可删除行总大小（Prune 估算）
│    - fsmIndex           : FsmIndex                         │  该页在 FSM 树中的索引
│    - recentDeadTupleMinCsn : uint64                        │  最近死亡 Tuple 的最小 CSN
├────────────────────────────────────────────────────────────┤  = HEAP_PAGE_DATA_OFFSET(88B)
│  m_data[]                                                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ TD[0], TD[1], TD[2], TD[3]  （各48B）                 │  ← m_lower 初始 = 88 + 4*48 = 280B
│  ├──────────────────────────────────────────────────────┤  │
│  │ ItemId[1], ItemId[2], ...（各4B，向右增长）            │  ← m_lower 随新 ItemId 增大
│  ├──────────────────────────────────────────────────────┤  │
│  │                   Free Space                          │  │
│  ├──────────────────────────────────────────────────────┤  │
│  │ ... Tuple[n], ..., Tuple[2], Tuple[1]（向左增长）      │  ← m_upper 随新 Tuple 减小
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘  offset 8192B
```

### 6.2 HeapPageHeader 字段说明

```cpp
struct PACKED HeapPageHeader {
    uint16 potentialDelSize;       // 可删除行的估计大小（用于提前触发 Prune）
    FsmIndex fsmIndex;             // 该页在 FSM 中的位置（level-0 索引）
    uint64 recentDeadTupleMinCsn;  // 页面中最近死亡 Tuple 的最小 CSN（Prune 触发判断）
    uint8 reserved[5];
};
```

**recentDeadTupleMinCsn 的作用**：Prune 时需要判断是否有快照仍引用已删除的行版本。如果 `recentDeadTupleMinCsn > recycleMinCsn`，说明仍有活跃快照可能需要这些行的旧版本，不能清除。

### 6.3 HeapPage 核心方法

```cpp
// include/page/dstore_heap_page.h
OffsetNumber HeapPage::AddTuple(const HeapDiskTuple *tuple, uint16 size, OffsetNumber specifyOffset);
void         HeapPage::DelTuple(OffsetNumber offset);
void         HeapPage::UpdateTuple(OffsetNumber offset, HeapDiskTuple *diskTuple, uint32 diskTupleSize);
void         HeapPage::GetTuple(HeapTuple *tuple, OffsetNumber offset);
HeapTuple   *HeapPage::GetVisibleTuple(PdbId pdbId, Transaction *txn, ItemPointerData &ctid, Snapshot snapshot, bool is_lob);
```

**AddTuple() 的关键步骤**：
1. 检查 Free Space 是否足够：`m_upper - m_lower >= sizeof(ItemId) + MAXALIGN(size)`
2. 写入 Tuple 数据：`m_upper -= MAXALIGN(size)`，数据写在 `m_upper` 处
3. 写入 ItemId：在 `m_lower` 处添加 `ItemId{Normal, offset=m_upper, len=size}`，`m_lower += sizeof(ItemId)`
4. 返回 `OffsetNumber`（新 ItemId 的槽位号）

### 6.4 页面容量常数

```cpp
// include/page/dstore_heap_page.h
static uint32 MaxDefaultTupleSpace();
// = BLCKSZ - MAXALIGN(HEAP_PAGE_DATA_OFFSET + DEFAULT_TD_COUNT * sizeof(TD) + sizeof(ItemId))
// ≈ 8192 - 88 - 4*48 - 4 = 7908B（~7.7KB）
// 超过此大小的 Tuple 触发 BigTuple 分块

static uint32 MaxPossibleTupleSpace();
// 使用 MIN_TD_COUNT(2) 时的最大值
```

---

## 7. HeapDiskTuple：每一行的磁盘格式

### 7.1 HeapDiskTuple 结构

```cpp
// include/tuple/dstore_heap_tuple.h
struct HeapDiskTuple : public DataTuple {
    // ---- ext_info（8B）：TupleField 和 DatumField 共用同一内存 ----
    union {
        struct TupleField {    // 写入/读取页面时使用
            uint8  m_tdId;         // 关联的 TD 槽位 ID（0-127）
            uint8  m_lockerTdId;   // 持有行锁的事务 TD ID
            uint16 m_size;         // Tuple 在页面中的总大小
            Xid    m_xid;          // 事务 ID（冗余，加速 MVCC 判断）
        } m_tuple_info;
        struct DatumField {    // SQL 引擎形成 Tuple 时使用
            int32 m_len;           // varlena 头（不可直接访问）
            int32 m_typmod;
            Oid   m_typeid;
        } m_datum_info;
    } m_ext_info;

    // ---- m_info（4B）：各种标志位 ----
    union {
        uint32 m_info;
        struct Value {
            uint32 m_hasNull      : 1;   // 是否有 NULL 列
            uint32 m_hasVarwidth  : 1;   // 是否有变长列
            uint32 m_hasExternal  : 1;   // 是否有外部存储列（DLOB）
            uint32 m_hasOid       : 1;   // 是否有 OID 列
            uint32 m_tdStatus     : 2;   // TupleTdStatus（MVCC 核心）
            uint32 m_liveMode     : 3;   // HeapDiskTupLiveMode（操作类型）
            uint32 m_linkInfo     : 2;   // 是否为 BigTuple 分块
            uint32 m_numColumn    : 11;  // 列数（最多 2047 列）
            uint32 m_HasInlineLobValue: 1;
            uint32 m_reserved_for_sql : 1;
            uint32 m_unused       : 8;
        } val;
    } m_info;

    char m_data[];  // 可变长数据（NULL bitmap + OID + 列值）
};

constexpr uint8 HEAP_DISK_TUP_HEADER_SIZE = sizeof(HeapDiskTuple);  // 固定头大小
```

### 7.2 m_data 区域布局

`m_data[]` 的内容按以下顺序排列：

```
[BigTuple链接头，若 IsLinked()]  ← 8B（NextChunkCtid 4B fileId/blockId + 4B offset + NumChunks 4B）
  仅当 m_linkInfo != TUP_NO_LINK_TYPE 时存在

[NULL bitmap]       ← ceil(m_numColumn / 8) 字节，若 m_hasNull == 1
[OID]               ← sizeof(Oid) 字节，若 m_hasOid == 1
[MAXALIGN padding]  ← 对齐填充
[列值数据...]       ← 变长/定长列值紧密排列
```

**GetValuesOffset() 计算**：

```cpp
uint32 offset = GetHeaderSize()                           // HeapDiskTuple 头大小
              + hasNull  * DataTuple::GetBitmapLen(attnum) // NULL bitmap
              + hasOid   * sizeof(Oid)                    // OID
              + isLinked * LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE; // BigTuple 链接头
return MAXALIGN(offset);
```

### 7.3 TupleTdStatus：MVCC 的核心标志

```cpp
// include/tuple/dstore_data_tuple.h
enum TupleTdStatus : uint8 {
    ATTACH_TD_AS_NEW_OWNER     = 0,  // 当前版本，TD 中存放该行的事务信息
    ATTACH_TD_AS_HISTORY_OWNER = 1,  // 历史版本，TD 已被新事务占用
    DETACH_TD                  = 2,  // 已与 TD 解耦，可见性无需查 TD
};
```

**三种状态的 MVCC 行为**：

| 状态 | 事务信息来源 | 可见性判断方式 |
|------|------------|--------------|
| `ATTACH_TD_AS_NEW_OWNER` | TD 槽位（通过 m_tdId 定位） | 读 `TD.m_xid` 判断提交状态 |
| `ATTACH_TD_AS_HISTORY_OWNER` | TD.m_csn（IS_PREV_XID_CSN）或 Undo | 先看 TD.m_csn，若无效则查 Undo |
| `DETACH_TD` | 无需 TD | 直接可见（已提交且旧快照已过期） |

**状态转换时机**：
```
INSERT                → ATTACH_TD_AS_NEW_OWNER
TD 被新事务占用时      → ATTACH_TD_AS_HISTORY_OWNER   （RefreshTupleTdStatus()）
recycleMinCsn 超过后  → DETACH_TD                     （RefreshTupleTdStatus()）
```

### 7.4 m_liveMode：行的操作类型

```cpp
enum class HeapDiskTupLiveMode {
    TUPLE_BY_NORMAL_INSERT = 0,          // 普通插入
    NEW_TUPLE_BY_INPLACE_UPDATE,         // 就地更新的新版本
    NEW_TUPLE_BY_SAME_PAGE_UPDATE,       // 同页更新的新版本
    OLD_TUPLE_BY_SAME_PAGE_UPDATE,       // 同页更新的旧版本
    OLD_TUPLE_BY_ANOTHER_PAGE_UPDATE,    // 跨页更新的旧版本（在原页）
    NEW_TUPLE_BY_ANOTHER_PAGE_UPDATE,    // 跨页更新的新版本（在新页）
    TUPLE_BY_NORMAL_DELETE,              // 已删除
};
```

`m_liveMode` 记录了 DML 操作的类型，用于 Undo 回滚时选择正确的撤销逻辑。

### 7.5 m_linkInfo：BigTuple 标记

```cpp
enum class HeapDiskTupLinkInfoType {
    TUP_NO_LINK_TYPE          = 0,  // 普通 Tuple（不分块）
    TUP_LINK_FIRST_CHUNK_TYPE = 1,  // BigTuple 的第一个分块
    TUP_LINK_NOT_FIRST_CHUNK_TYPE = 2, // BigTuple 的非第一个分块
};
```

当 `IsLinked() == true` 时，`m_data` 的头 12 字节是：
```
[NextChunkCtid(8B)][NumTupChunks(4B)]
```
指向下一个分块的 ItemPointerData，以及总分块数。

---

## 8. 继承关系与页面类型对应

```
Page                      ← 所有页面的基类
└── DataPage              ← 数据页基类（含 TD + ItemId 管理）
      ├── HeapPage        ← 普通数据页（本 Day 重点）
      └── DataPage（用于 BTree）
            └── BtrPage   ← BTree 页面
```

不同页面类型在 TD 操作上共用 `DataPage` 的模板方法，通过模板参数 `PageType` 区分行为：

```cpp
template<PageType page_type> TdId DataPage::AllocTd(TDAllocContext &context);
// HeapPage 调用：DataPage::AllocTd<PageType::HEAP_PAGE_TYPE>(context)
// BTree 调用：   DataPage::AllocTd<PageType::INDEX_PAGE_TYPE>(context)
```

---

## 9. 完整页面生命周期（时序图）

```
新页面分配
   │
   ▼
HeapPage::InitHeapPage()
   ├── Page::Init(specialSize=0, type=HEAP_PAGE_TYPE, selfPageId)
   │     └── m_header.m_lower = sizeof(PageHeader) + sizeof(DataPageHeader) + sizeof(HeapPageHeader)
   │         m_header.m_upper = BLCKSZ
   └── DataPage::AllocateTdSpace(DEFAULT_TD_COUNT=4)
         └── m_lower += 4 * sizeof(TD)   // m_lower 推进到 88 + 192 = 280B

插入一行
   │
   ▼
HeapPage::AddTuple(diskTuple, size)
   ├── m_upper -= MAXALIGN(size)        // Tuple 数据写在 m_upper 处
   ├── 写 Tuple 数据到 m_upper
   ├── ItemId.SetNormal(m_upper, size)  // 记录指针
   └── m_lower += sizeof(ItemId)        // m_lower 右移 4B

查询一行
   │
   ▼
GetItemIdPtr(offset) → ItemId
   └── GetRowData(ItemId) → HeapDiskTuple*
         └── GetVisibleTuple() → MVCC 可见性判断
```

---

## 10. Day 2 核心速查表

| 概念 | 位置 | 一句话 |
|------|------|--------|
| `ItemPointerData` | `interface/page/dstore_itemptr.h:50` | 行物理地址：fileId(16) + blockId(32) + offset(16) |
| `PageId` | `interface/page/dstore_page_struct.h:56` | fileId + blockId |
| `PageType` | `interface/page/dstore_page_struct.h:34` | 页面类型枚举（HEAP/INDEX/UNDO/FSM...） |
| `Page::PageHeader` | `include/page/dstore_page.h:49` | 通用头：m_plsn/m_glsn/m_walId/m_lower/m_upper |
| `DataPageHeader` | `include/page/dstore_data_page.h:123` | tdCount + headerOffset |
| `HeapPageHeader` | `include/page/dstore_data_page.h:114` | potentialDelSize + fsmIndex + recentDeadTupleMinCsn |
| `TD` | `include/page/dstore_td.h:164` | xid + csn + undoRecPtr + status(2bit) + csnStatus(2bit) |
| `TDStatus` | `include/page/dstore_td.h:49` | UNOCCUPY/IN_PROGRESS/END（中间状态不准确） |
| `TdCsnStatus` | `include/page/dstore_td.h:62` | INVALID/PREV_XID/CUR_XID（PREV 是 TD 复用保留） |
| `ItemId` | `include/page/dstore_itemid.h:55` | 4B，flags(2)+offset(15)+len(15)；NoStorage 时存 tdId |
| `HeapDiskTuple` | `include/tuple/dstore_heap_tuple.h:58` | 磁盘行格式，含 tdId/tdStatus/liveMode/linkInfo |
| `TupleTdStatus` | `include/tuple/dstore_data_tuple.h` | NEW_OWNER/HISTORY_OWNER/DETACH — MVCC 核心 |
| `HEAP_PAGE_DATA_OFFSET` | `include/page/dstore_data_page.h:132` | = 88B，HeapPage 数据区起始位置 |
| `DEFAULT_TD_COUNT` | `include/page/dstore_td.h:40` | = 4，初始 TD 槽位数量 |
| `MaxDefaultTupleSpace()` | `include/page/dstore_heap_page.h:183` | ≈ 7908B，超出触发 BigTuple 分块 |

---

## 11. 与 Day 3 的衔接

Day 2 学到的是数据在**磁盘上的格式**——HeapPage 就是写在磁盘上的字节序列。但任何读写操作都不能直接操作磁盘；必须通过 Buffer Manager 先将页面加载到内存缓冲区，修改内存中的页面后再异步写回。

Day 3 将深入 Buffer Manager：
- `BufferDesc`：如何管理缓冲页面的状态（DIRTY/VALID/引用计数）
- `ReadBuffer()` / `MarkDirty()` / `WriteBlock()` 的完整链路
- LRU 替换策略与 WAL-First 的强制检查
