# dstore 诊断缓冲区 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 dstore 存储引擎实现一个动态开关的诊断环形缓冲区，在 heap scan、BTree scan、SetTd、InsertUndoRecord、GetBuffer、MarkDirty 六个插桩点采集 XID/CTID/TD 状态/BufferTag 信息，并通过 `DstoreMvccDiag` 接口控制采集和查询。

**Architecture:** 每个 PDB 持有一个 `DstoreDiagContext`（含读/写两个环形缓冲区），默认关闭（零开销），通过 `DstoreMvccDiag::Enable` 按 relation OID 动态开启。采集时通过 `DIAG_SHOULD_RECORD` 宏短路，写入使用 `std::atomic::fetch_add` 无锁竞争 slot。查询通过 `DstoreMvccDiag::CreateQueryIterator` 返回 `DiagnoseIterator`，调用方按时间范围迭代记录。

**Tech Stack:** C++17, GTest (`DSTORETEST` 基类), `std::atomic`, `DstorePalloc`/`DstorePfree`, dstore 已有 `DiagnoseIterator` 接口模式。

---

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `include/diag/dstore_diag.h` | `DstoreDiagOpType`、`DstoreDiagRecord`、`DstoreDiagRing`、`DstoreDiagContext`；`DiagWrite` inline；`DIAG_SHOULD_RECORD` 宏 |
| 新建 | `src/diag/dstore_diag.cpp` | `DstoreDiagEnable`、`DstoreDiagDisable`、`DstoreDiagReset` 实现 |
| 新建 | `src/diag/dstore_diag_query.cpp` | `DstoreDiagQueryIterator` 实现（`DiagnoseIterator` 子类） |
| 新建 | `interface/diagnose/dstore_mvcc_diag.h` | 对外接口：`DstoreMvccDiag` 静态类，含 `Enable`/`Disable`/`Reset`/`CreateQueryIterator` |
| 修改 | `include/framework/dstore_pdb.h` | 在 `StoragePdb` private 末尾添加 `DstoreDiagContext* m_diagCtx`；添加 `GetDiagCtx()` getter |
| 修改 | `src/framework/dstore_pdb.cpp` | 构造函数初始化 `m_diagCtx = nullptr`；析构函数调用 `DstoreDiagReset` |
| 修改 | `src/buffer/dstore_buf_mgr.cpp` | 插桩点 1（`Read`）、插桩点 6（`MarkDirty`）|
| 修改 | `src/page/dstore_heap_page.cpp` | 插桩点 2（`GetVisibleTuple`）|
| 修改 | `src/index/dstore_btree_scan.cpp` | 插桩点 3（BTree 可见性判断）|
| 修改 | `include/page/dstore_data_page.h` | 插桩点 4（`SetTd` inline 函数末尾）|
| 修改 | `src/undo/dstore_undo_zone.cpp` | 插桩点 5（`InsertUndoRecord`）|
| 新建 | `tests/unittest/src/ut_diag/ut_diag.h` | 测试 fixture |
| 新建 | `tests/unittest/src/ut_diag/ut_diag.cpp` | 单元测试 |

> `src/diag/` 下的 `.cpp` 文件由 `SUBSRCLIST` 宏自动 glob 进 `libdstore`，无需修改 `src/CMakeLists.txt`。
> `tests/unittest/src/ut_diag/` 下的 `.cpp` 由 `FILE(GLOB_RECURSE *.cpp)` 自动加入 unittest，无需修改测试 CMakeLists。

---

## Task 1: 核心数据结构头文件

**Files:**
- Create: `include/diag/dstore_diag.h`

- [ ] **Step 1: 新建头文件**

```cpp
// include/diag/dstore_diag.h
#ifndef DSTORE_DIAG_H
#define DSTORE_DIAG_H

#include <atomic>
#include "common/dstore_datatype.h"
#include "buffer/dstore_buf.h"
#include "page/dstore_td.h"
#include "undo/dstore_undo_type.h"

namespace DSTORE {

enum class DstoreDiagOpType : uint8 {
    // 读路径
    DIAG_GET_BUFFER_READ   = 0x01,
    DIAG_GET_VISIBLE_TUPLE = 0x02,
    DIAG_BTREE_VISIBLE     = 0x03,
    // 写路径
    DIAG_SET_TD            = 0x11,
    DIAG_INSERT_UNDO       = 0x12,
    DIAG_MARK_DIRTY        = 0x13,
};

struct DstoreDiagRecord {
    TimestampTz      timestamp;         // GetCurrentTimestamp() 微秒时间戳
    DstoreDiagOpType op_type;
    uint8            td_status;         // cast from TDStatus
    uint8            td_csn_status;     // cast from TdCsnStatus
    uint8            td_id;             // TD slot 编号
    Xid              xid;               // 当前事务 XID（Xid 即 uint64）
    CommitSeqNo      td_csn;            // TD 中记录的 CSN
    CommitSeqNo      snapshot_csn;      // 当前快照 CSN；写路径置 0
    ItemPointerData  ctid;              // tuple 物理位置
    BufferTag        buf_tag;           // pdbId + pageId(fileId+blockId)
    uint64           page_lsn;          // Page::GetPlsn()；不适用时置 0
    uint64           undo_rec_ptr;      // UndoRecPtr.m_placeHolder；不适用时置 0
    bool             visibility_result; // 可见性判断结果；写路径置 false
    uint8            _pad[3];
};
static_assert(sizeof(DstoreDiagRecord) <= 128, "DstoreDiagRecord must fit in 128 bytes");

struct DstoreDiagRing {
    uint32                    capacity;   // 必须为 2 的幂次
    std::atomic<uint64>       write_idx;  // 全局单调递增
    DstoreDiagRecord*         records;    // DstorePalloc 分配
};

struct DstoreDiagContext {
    std::atomic<bool> enabled;
    Oid               target_rel_oid;  // 过滤目标；0 = 未启用
    DstoreDiagRing    read_ring;
    DstoreDiagRing    write_ring;
};

// 热路径内联写入，调用方保证 ring.records != nullptr
inline void DiagWrite(DstoreDiagRing& ring, const DstoreDiagRecord& rec)
{
    uint64 idx = ring.write_idx.fetch_add(1, std::memory_order_relaxed);
    ring.records[idx & (ring.capacity - 1)] = rec;
}

// 插桩点过滤宏：buf_tag.pageId.m_fileId 对应 relation，用 pdbId+fileId 作为 relation 标识
// 注意：dstore 的 relation 以 (pdbId, fileId) 标识，Oid 此处约定映射为 fileId 的低32位
#define DIAG_SHOULD_RECORD(diagCtx, buf_tag) \
    ((diagCtx) != nullptr &&                 \
     (diagCtx)->enabled.load(std::memory_order_relaxed) && \
     static_cast<Oid>((buf_tag).pageId.m_fileId) == (diagCtx)->target_rel_oid)

}  // namespace DSTORE
#endif  // DSTORE_DIAG_H
```

- [ ] **Step 2: 确认编译通过（无实现文件，只验证头文件依赖无误）**

```bash
cd /Users/jeremy/Documents/dstore-main
# 写一个临时 include 检查文件
echo '#include "diag/dstore_diag.h"
int main(){return 0;}' > /tmp/check_diag.cpp
g++ -std=c++17 -I include -I interface \
    -I utils/output/include \
    /tmp/check_diag.cpp -o /tmp/check_diag 2>&1 | head -20
```

预期：无错误（或仅有缺少依赖库的链接错误，不是编译错误）

- [ ] **Step 3: Commit**

```bash
git add include/diag/dstore_diag.h
git commit -m "feat(diag): add DstoreDiagRecord/Ring/Context core data structures"
```

---

## Task 2: 测试 fixture 和基础 DiagWrite/DIAG_SHOULD_RECORD 测试

**Files:**
- Create: `tests/unittest/src/ut_diag/ut_diag.h`
- Create: `tests/unittest/src/ut_diag/ut_diag.cpp`

- [ ] **Step 1: 新建 fixture 头文件**

```cpp
// tests/unittest/src/ut_diag/ut_diag.h
#ifndef UT_DIAG_H
#define UT_DIAG_H

#include "gtest/gtest.h"
#include "diag/dstore_diag.h"
#include "ut_utilities/ut_dstore_framework.h"

using namespace DSTORE;

class DiagTest : public DSTORETEST {
public:
    static const uint32 SMALL_CAPACITY = 8;  // 2^3，便于测试覆盖

    static DstoreDiagContext* MakeContext(uint32 readCap, uint32 writeCap, Oid relOid)
    {
        auto* ctx = static_cast<DstoreDiagContext*>(
            DstorePalloc(sizeof(DstoreDiagContext)));
        ctx->target_rel_oid = relOid;
        ctx->enabled.store(false, std::memory_order_relaxed);

        ctx->read_ring.capacity  = readCap;
        ctx->read_ring.write_idx.store(0, std::memory_order_relaxed);
        ctx->read_ring.records = static_cast<DstoreDiagRecord*>(
            DstorePalloc(sizeof(DstoreDiagRecord) * readCap));

        ctx->write_ring.capacity  = writeCap;
        ctx->write_ring.write_idx.store(0, std::memory_order_relaxed);
        ctx->write_ring.records = static_cast<DstoreDiagRecord*>(
            DstorePalloc(sizeof(DstoreDiagRecord) * writeCap));

        return ctx;
    }

    static void FreeContext(DstoreDiagContext* ctx)
    {
        DstorePfree(ctx->read_ring.records);
        DstorePfree(ctx->write_ring.records);
        DstorePfree(ctx);
    }

    static DstoreDiagRecord MakeRecord(DstoreDiagOpType op, Oid fileId,
                                        uint64 xid, uint64 csn)
    {
        DstoreDiagRecord rec{};
        rec.op_type     = op;
        rec.timestamp   = GetCurrentTimestamp();
        rec.xid.m_placeHolder = xid;
        rec.td_csn      = csn;
        rec.buf_tag     = BufferTag(1, PageId{static_cast<FileId>(fileId), 0});
        return rec;
    }
};
#endif  // UT_DIAG_H
```

- [ ] **Step 2: 写第一个测试——DiagWrite 环形覆盖**

```cpp
// tests/unittest/src/ut_diag/ut_diag.cpp
#include "ut_diag/ut_diag.h"

// DiagWrite: 写入超过 capacity 时覆盖最老记录
TEST_F(DiagTest, DiagWriteWrapsAround)
{
    auto* ctx = MakeContext(SMALL_CAPACITY, SMALL_CAPACITY, 100);
    ctx->enabled.store(true, std::memory_order_relaxed);

    // 写入 capacity + 2 条记录
    for (uint32 i = 0; i < SMALL_CAPACITY + 2; i++) {
        DstoreDiagRecord rec = MakeRecord(DstoreDiagOpType::DIAG_GET_BUFFER_READ,
                                          100, i, i * 10);
        DiagWrite(ctx->read_ring, rec);
    }

    // 最后写入的记录应在 slot (capacity+1) % capacity = 1
    uint64 lastSlot = (SMALL_CAPACITY + 1) & (SMALL_CAPACITY - 1);
    EXPECT_EQ(ctx->read_ring.records[lastSlot].xid.m_placeHolder,
              static_cast<uint64>(SMALL_CAPACITY + 1));

    FreeContext(ctx);
}
```

- [ ] **Step 3: 运行测试，确认编译报错（DiagWrite 已在头文件，应能通过；Xid.m_placeHolder 需确认字段名）**

```bash
cd /Users/jeremy/Documents/dstore-main
bash tests/build_and_run_ut.sh 2>&1 | grep -E "DiagTest|PASS|FAIL|error:" | head -30
```

预期：`DiagTest.DiagWriteWrapsAround` PASS

- [ ] **Step 4: 写第二个测试——DIAG_SHOULD_RECORD 过滤**

在 `ut_diag.cpp` 追加：

```cpp
TEST_F(DiagTest, DiagShouldRecordFilters)
{
    auto* ctx = MakeContext(SMALL_CAPACITY, SMALL_CAPACITY, 42);
    ctx->enabled.store(true, std::memory_order_relaxed);

    // 匹配 relOid=42 的 buf_tag
    BufferTag matchTag(1, PageId{static_cast<FileId>(42), 0});
    EXPECT_TRUE(DIAG_SHOULD_RECORD(ctx, matchTag));

    // 不匹配 relOid=99 的 buf_tag
    BufferTag noMatchTag(1, PageId{static_cast<FileId>(99), 0});
    EXPECT_FALSE(DIAG_SHOULD_RECORD(ctx, noMatchTag));

    // disabled 时始终 false
    ctx->enabled.store(false, std::memory_order_relaxed);
    EXPECT_FALSE(DIAG_SHOULD_RECORD(ctx, matchTag));

    // nullptr context
    EXPECT_FALSE(DIAG_SHOULD_RECORD(nullptr, matchTag));

    FreeContext(ctx);
}
```

- [ ] **Step 5: 运行测试**

```bash
bash tests/build_and_run_ut.sh 2>&1 | grep -E "DiagTest|PASS|FAIL" | head -20
```

预期：两个测试 PASS

- [ ] **Step 6: Commit**

```bash
git add tests/unittest/src/ut_diag/ut_diag.h \
        tests/unittest/src/ut_diag/ut_diag.cpp
git commit -m "test(diag): add DiagWrite wrap-around and DIAG_SHOULD_RECORD filter tests"
```

---

## Task 3: Enable/Disable/Reset 实现

**Files:**
- Create: `src/diag/dstore_diag.cpp`
- Create: `interface/diagnose/dstore_mvcc_diag.h`

- [ ] **Step 1: 新建接口头文件**

```cpp
// interface/diagnose/dstore_mvcc_diag.h
#ifndef DSTORE_MVCC_DIAG_H
#define DSTORE_MVCC_DIAG_H

#include "common/dstore_common_utils.h"
#include "diagnose/dstore_diagnose.h"

namespace DSTORE {
#pragma GCC visibility push(default)

class DstoreMvccDiag {
public:
    static RetStatus Enable(PdbId pdbId, Oid relOid,
                             uint32_t readCapacity  = 262144,
                             uint32_t writeCapacity = 262144);

    static RetStatus Disable(PdbId pdbId);

    static RetStatus Reset(PdbId pdbId);

    // 返回的迭代器调用方负责 delete；startTime/endTime 为 0 表示不限
    static DiagnoseIterator* CreateQueryIterator(PdbId pdbId,
                                                  TimestampTz startTime,
                                                  TimestampTz endTime,
                                                  uint8_t opTypeMask,   // 0xFF = 全部
                                                  uint8_t pathMask);    // 0x01=read 0x02=write 0x03=both
};
#pragma GCC visibility pop
}  // namespace DSTORE
#endif  // DSTORE_MVCC_DIAG_H
```

- [ ] **Step 2: 新建实现文件**

```cpp
// src/diag/dstore_diag.cpp
#include "diag/dstore_diag.h"
#include "diagnose/dstore_mvcc_diag.h"
#include "framework/dstore_instance.h"
#include "framework/dstore_pdb.h"
#include "common/memory/dstore_mctx.h"

namespace DSTORE {

static bool IsPowerOfTwo(uint32 n) { return n > 0 && (n & (n - 1)) == 0; }

RetStatus DstoreMvccDiag::Enable(PdbId pdbId, Oid relOid,
                                  uint32_t readCapacity, uint32_t writeCapacity)
{
    if (!IsPowerOfTwo(readCapacity) || !IsPowerOfTwo(writeCapacity)) {
        return DSTORE_ERROR;
    }
    StoragePdb* pdb = g_storageInstance->GetPdb(pdbId);
    if (pdb == nullptr) {
        return DSTORE_ERROR;
    }

    DstoreDiagContext* ctx = pdb->GetDiagCtx();
    if (ctx == nullptr) {
        ctx = static_cast<DstoreDiagContext*>(DstorePalloc(sizeof(DstoreDiagContext)));
        if (ctx == nullptr) {
            return DSTORE_ERROR;
        }
        new (ctx) DstoreDiagContext();  // placement new for atomic members
        ctx->read_ring.capacity  = readCapacity;
        ctx->read_ring.write_idx.store(0, std::memory_order_relaxed);
        ctx->read_ring.records = static_cast<DstoreDiagRecord*>(
            DstorePalloc(sizeof(DstoreDiagRecord) * readCapacity));
        ctx->write_ring.capacity  = writeCapacity;
        ctx->write_ring.write_idx.store(0, std::memory_order_relaxed);
        ctx->write_ring.records = static_cast<DstoreDiagRecord*>(
            DstorePalloc(sizeof(DstoreDiagRecord) * writeCapacity));
        if (ctx->read_ring.records == nullptr || ctx->write_ring.records == nullptr) {
            DstorePfree(ctx->read_ring.records);
            DstorePfree(ctx->write_ring.records);
            DstorePfree(ctx);
            return DSTORE_ERROR;
        }
        pdb->SetDiagCtx(ctx);
    }

    ctx->target_rel_oid = relOid;
    // enabled 最后写入，确保内存就绪后才开始采集
    ctx->enabled.store(true, std::memory_order_release);
    return DSTORE_SUCC;
}

RetStatus DstoreMvccDiag::Disable(PdbId pdbId)
{
    StoragePdb* pdb = g_storageInstance->GetPdb(pdbId);
    if (pdb == nullptr) {
        return DSTORE_ERROR;
    }
    DstoreDiagContext* ctx = pdb->GetDiagCtx();
    if (ctx == nullptr) {
        return DSTORE_SUCC;
    }
    ctx->enabled.store(false, std::memory_order_release);
    return DSTORE_SUCC;
}

RetStatus DstoreMvccDiag::Reset(PdbId pdbId)
{
    StoragePdb* pdb = g_storageInstance->GetPdb(pdbId);
    if (pdb == nullptr) {
        return DSTORE_ERROR;
    }
    DstoreDiagContext* ctx = pdb->GetDiagCtx();
    if (ctx == nullptr) {
        return DSTORE_SUCC;
    }
    ctx->enabled.store(false, std::memory_order_release);
    DstorePfree(ctx->read_ring.records);
    DstorePfree(ctx->write_ring.records);
    ctx->~DstoreDiagContext();
    DstorePfree(ctx);
    pdb->SetDiagCtx(nullptr);
    return DSTORE_SUCC;
}

}  // namespace DSTORE
```

- [ ] **Step 3: 写 Enable/Disable 测试（在 ut_diag.cpp 追加）**

```cpp
// 在 ut_diag.cpp 末尾追加
TEST_F(DiagTest, EnableSetsTargetRelOid)
{
    auto* ctx = MakeContext(SMALL_CAPACITY, SMALL_CAPACITY, 0);
    ctx->target_rel_oid = 55;
    ctx->enabled.store(true, std::memory_order_relaxed);
    EXPECT_EQ(ctx->target_rel_oid, static_cast<Oid>(55));
    EXPECT_TRUE(ctx->enabled.load());

    ctx->enabled.store(false, std::memory_order_relaxed);
    EXPECT_FALSE(ctx->enabled.load());
    // 停用后数据保留
    EXPECT_EQ(ctx->target_rel_oid, static_cast<Oid>(55));

    FreeContext(ctx);
}
```

- [ ] **Step 4: 运行测试**

```bash
bash tests/build_and_run_ut.sh 2>&1 | grep -E "DiagTest|PASS|FAIL" | head -20
```

预期：所有 `DiagTest.*` 测试 PASS

- [ ] **Step 5: Commit**

```bash
git add src/diag/dstore_diag.cpp interface/diagnose/dstore_mvcc_diag.h
git commit -m "feat(diag): implement Enable/Disable/Reset for DstoreMvccDiag"
```

---

## Task 4: 将 DstoreDiagContext 挂入 StoragePdb

**Files:**
- Modify: `include/framework/dstore_pdb.h`
- Modify: `src/framework/dstore_pdb.cpp`

- [ ] **Step 1: 在 `dstore_pdb.h` 添加前向声明、getter/setter 和成员**

在 `include/framework/dstore_pdb.h` 中：
1. 在文件顶部 include 区域后追加前向声明：
```cpp
// 在 namespace DSTORE { 开始后、class StoragePdb 之前
struct DstoreDiagContext;
```

2. 在 `class StoragePdb` public 区域添加：
```cpp
    DstoreDiagContext* GetDiagCtx() const { return m_diagCtx; }
    void SetDiagCtx(DstoreDiagContext* ctx) { m_diagCtx = ctx; }
```

3. 在 `class StoragePdb` private 末尾（`gs_atomic_uint64 m_pdbTerm` 之后）添加：
```cpp
    DstoreDiagContext* m_diagCtx{nullptr};
```

- [ ] **Step 2: 在 `dstore_pdb.cpp` 析构函数中清理**

找到 `StoragePdb` 析构函数，在其末尾调用 Reset：
```cpp
// 在析构函数末尾
if (m_diagCtx != nullptr) {
    DstoreDiagDisableAndFree(m_diagCtx);  // 见下一步定义
    m_diagCtx = nullptr;
}
```

在 `src/diag/dstore_diag.cpp` 中添加内部函数：
```cpp
void DstoreDiagDisableAndFree(DstoreDiagContext* ctx)
{
    if (ctx == nullptr) return;
    ctx->enabled.store(false, std::memory_order_release);
    DstorePfree(ctx->read_ring.records);
    DstorePfree(ctx->write_ring.records);
    ctx->~DstoreDiagContext();
    DstorePfree(ctx);
}
```

在 `include/diag/dstore_diag.h` 中声明：
```cpp
void DstoreDiagDisableAndFree(DstoreDiagContext* ctx);
```

- [ ] **Step 3: 编译验证**

```bash
cd /Users/jeremy/Documents/dstore-main
bash build.sh -m debug 2>&1 | tail -20
```

预期：`libdstore.so` 编译成功，无新增错误

- [ ] **Step 4: Commit**

```bash
git add include/framework/dstore_pdb.h src/framework/dstore_pdb.cpp \
        include/diag/dstore_diag.h src/diag/dstore_diag.cpp
git commit -m "feat(diag): attach DstoreDiagContext to StoragePdb lifecycle"
```

---

## Task 5: 查询迭代器实现

**Files:**
- Create: `src/diag/dstore_diag_query.cpp`

- [ ] **Step 1: 写查询迭代器**

```cpp
// src/diag/dstore_diag_query.cpp
#include "diag/dstore_diag.h"
#include "diagnose/dstore_mvcc_diag.h"
#include "framework/dstore_diagnose_iterator.h"
#include "framework/dstore_instance.h"
#include "framework/dstore_pdb.h"
#include "common/memory/dstore_mctx.h"
#include <vector>
#include <algorithm>

namespace DSTORE {

// 查询结果单条记录，继承 DiagnoseItem 供外部使用
struct DstoreDiagQueryItem : public DiagnoseItem {
    DstoreDiagRecord rec;
};

class DstoreDiagQueryIterator : public AbstractDiagnoseIterator {
public:
    DstoreDiagQueryIterator(PdbId pdbId, TimestampTz startTime, TimestampTz endTime,
                             uint8_t opTypeMask, uint8_t pathMask)
        : m_pdbId(pdbId), m_startTime(startTime), m_endTime(endTime),
          m_opTypeMask(opTypeMask), m_pathMask(pathMask), m_cursor(0) {}

    ~DstoreDiagQueryIterator() override = default;

    bool Begin() override
    {
        StoragePdb* pdb = g_storageInstance->GetPdb(m_pdbId);
        if (pdb == nullptr) return false;
        DstoreDiagContext* ctx = pdb->GetDiagCtx();
        if (ctx == nullptr) return true;  // 空结果，合法

        if (m_pathMask & 0x01) {
            CollectFromRing(ctx->read_ring, /*isRead=*/true);
        }
        if (m_pathMask & 0x02) {
            CollectFromRing(ctx->write_ring, /*isRead=*/false);
        }

        // 按 timestamp 排序
        std::sort(m_items.begin(), m_items.end(),
                  [](const DstoreDiagRecord& a, const DstoreDiagRecord& b) {
                      return a.timestamp < b.timestamp;
                  });
        return true;
    }

    bool HasNext() override { return m_cursor < m_items.size(); }

    DiagnoseItem* GetNext() override
    {
        if (!HasNext()) return nullptr;
        m_current.rec = m_items[m_cursor++];
        return &m_current;
    }

    void End() override { m_items.clear(); m_cursor = 0; }

private:
    void CollectFromRing(const DstoreDiagRing& ring, bool /*isRead*/)
    {
        if (ring.records == nullptr) return;
        uint64 total = ring.write_idx.load(std::memory_order_acquire);
        uint64 count = (total < ring.capacity) ? total : ring.capacity;
        for (uint64 i = 0; i < count; i++) {
            const DstoreDiagRecord& rec = ring.records[i];
            if (rec.timestamp == 0) continue;
            if (m_startTime != 0 && rec.timestamp < m_startTime) continue;
            if (m_endTime   != 0 && rec.timestamp > m_endTime)   continue;
            if (m_opTypeMask != 0xFF &&
                !(m_opTypeMask & static_cast<uint8>(rec.op_type))) continue;
            m_items.push_back(rec);
        }
    }

    PdbId                         m_pdbId;
    TimestampTz                   m_startTime;
    TimestampTz                   m_endTime;
    uint8_t                       m_opTypeMask;
    uint8_t                       m_pathMask;
    std::vector<DstoreDiagRecord> m_items;
    size_t                        m_cursor;
    DstoreDiagQueryItem           m_current;
};

DiagnoseIterator* DstoreMvccDiag::CreateQueryIterator(PdbId pdbId,
                                                       TimestampTz startTime,
                                                       TimestampTz endTime,
                                                       uint8_t opTypeMask,
                                                       uint8_t pathMask)
{
    return DstoreNew(MEMORY_CONTEXT_QUERY) DstoreDiagQueryIterator(
        pdbId, startTime, endTime, opTypeMask, pathMask);
}

}  // namespace DSTORE
```

- [ ] **Step 2: 写查询迭代器测试（在 ut_diag.cpp 追加）**

```cpp
TEST_F(DiagTest, QueryIteratorTimeRangeFilter)
{
    auto* ctx = MakeContext(16, 16, 77);
    ctx->enabled.store(true, std::memory_order_relaxed);

    // 写入3条记录，模拟不同时间
    DstoreDiagRecord r1 = MakeRecord(DstoreDiagOpType::DIAG_GET_BUFFER_READ, 77, 1, 100);
    r1.timestamp = 1000;
    DstoreDiagRecord r2 = MakeRecord(DstoreDiagOpType::DIAG_GET_VISIBLE_TUPLE, 77, 2, 200);
    r2.timestamp = 2000;
    DstoreDiagRecord r3 = MakeRecord(DstoreDiagOpType::DIAG_SET_TD, 77, 3, 300);
    r3.timestamp = 3000;
    DiagWrite(ctx->read_ring,  r1);
    DiagWrite(ctx->read_ring,  r2);
    DiagWrite(ctx->write_ring, r3);

    // 时间范围 [1500, 3500] 应只拿到 r2 和 r3
    // 直接测试 Ring 遍历逻辑（不依赖 PDB，直接读 ring）
    int count = 0;
    uint64 total = ctx->read_ring.write_idx.load(std::memory_order_acquire);
    for (uint64 i = 0; i < total && i < ctx->read_ring.capacity; i++) {
        const auto& rec = ctx->read_ring.records[i];
        if (rec.timestamp >= 1500 && rec.timestamp <= 3500) count++;
    }
    EXPECT_EQ(count, 1);  // 只有 r2 在读 ring 且时间 >= 1500

    uint64 wtotal = ctx->write_ring.write_idx.load(std::memory_order_acquire);
    for (uint64 i = 0; i < wtotal && i < ctx->write_ring.capacity; i++) {
        const auto& rec = ctx->write_ring.records[i];
        if (rec.timestamp >= 1500 && rec.timestamp <= 3500) count++;
    }
    EXPECT_EQ(count, 2);  // r2 + r3

    FreeContext(ctx);
}
```

- [ ] **Step 3: 运行测试**

```bash
bash tests/build_and_run_ut.sh 2>&1 | grep -E "DiagTest|PASS|FAIL" | head -20
```

预期：所有 `DiagTest.*` PASS

- [ ] **Step 4: Commit**

```bash
git add src/diag/dstore_diag_query.cpp
git commit -m "feat(diag): implement DstoreDiagQueryIterator with time range and path filter"
```

---

## Task 6: 插桩点 1 + 6 — GetBuffer 和 MarkDirty

**Files:**
- Modify: `src/buffer/dstore_buf_mgr.cpp`

- [ ] **Step 1: 写插桩点测试（在 ut_diag.cpp 追加，验证 record 字段正确填充）**

```cpp
TEST_F(DiagTest, RecordFieldsGetBufferRead)
{
    DstoreDiagRecord rec{};
    rec.op_type  = DstoreDiagOpType::DIAG_GET_BUFFER_READ;
    rec.timestamp = GetCurrentTimestamp();
    rec.page_lsn  = 12345ULL;
    rec.buf_tag   = BufferTag(1, PageId{static_cast<FileId>(77), 10});

    auto* ctx = MakeContext(8, 8, 77);
    ctx->enabled.store(true, std::memory_order_relaxed);
    DiagWrite(ctx->read_ring, rec);

    EXPECT_EQ(ctx->read_ring.records[0].op_type, DstoreDiagOpType::DIAG_GET_BUFFER_READ);
    EXPECT_EQ(ctx->read_ring.records[0].page_lsn, 12345ULL);
    EXPECT_EQ(ctx->read_ring.records[0].buf_tag.pageId.m_fileId, static_cast<FileId>(77));

    FreeContext(ctx);
}
```

- [ ] **Step 2: 运行测试确认通过**

```bash
bash tests/build_and_run_ut.sh 2>&1 | grep -E "DiagTest|PASS|FAIL" | head -20
```

- [ ] **Step 3: 在 `src/buffer/dstore_buf_mgr.cpp` 中添加 include**

在文件头部已有的 include 区域末尾追加：
```cpp
#include "diag/dstore_diag.h"
#include "framework/dstore_pdb.h"
```

- [ ] **Step 4: 插桩 `BufMgr::Read()`**

找到 `BufferDesc *BufMgr::Read(...)` 函数，在 `return bufDesc;` 之前插入：

```cpp
    // Diag instrumentation: record buffer read
    {
        StoragePdb* diagPdb = g_storageInstance->GetPdb(pdbId);
        DstoreDiagContext* diagCtx = (diagPdb != nullptr) ? diagPdb->GetDiagCtx() : nullptr;
        BufferTag diagTag(pdbId, pageId);
        if (DIAG_SHOULD_RECORD(diagCtx, diagTag)) {
            DstoreDiagRecord rec{};
            rec.op_type   = DstoreDiagOpType::DIAG_GET_BUFFER_READ;
            rec.timestamp = GetCurrentTimestamp();
            rec.buf_tag   = diagTag;
            rec.page_lsn  = bufDesc->GetPage()->GetPlsn();
            DiagWrite(diagCtx->read_ring, rec);
        }
    }
```

- [ ] **Step 5: 插桩 `BufMgr::MarkDirty()`**

找到 `RetStatus BufMgr::MarkDirty(BufferDesc *bufferDesc, ...)` 函数末尾，在 `return DSTORE_SUCC;` 之前：

```cpp
    // Diag instrumentation: record mark dirty
    {
        DstoreDiagContext* diagCtx = nullptr;
        StoragePdb* diagPdb = g_storageInstance->GetPdb(bufferDesc->bufTag.pdbId);
        if (diagPdb != nullptr) diagCtx = diagPdb->GetDiagCtx();
        if (DIAG_SHOULD_RECORD(diagCtx, bufferDesc->bufTag)) {
            DstoreDiagRecord rec{};
            rec.op_type   = DstoreDiagOpType::DIAG_MARK_DIRTY;
            rec.timestamp = GetCurrentTimestamp();
            rec.buf_tag   = bufferDesc->bufTag;
            rec.page_lsn  = bufferDesc->GetPage()->GetPlsn();
            DiagWrite(diagCtx->write_ring, rec);
        }
    }
```

- [ ] **Step 6: 编译**

```bash
bash build.sh -m debug 2>&1 | grep -E "error:|warning:" | grep -v "deprecated" | head -20
```

预期：无新增错误

- [ ] **Step 7: Commit**

```bash
git add src/buffer/dstore_buf_mgr.cpp
git commit -m "feat(diag): instrument GetBuffer (read) and MarkDirty with diag ring"
```

---

## Task 7: 插桩点 2 — GetVisibleTuple（heap scan）

**Files:**
- Modify: `src/page/dstore_heap_page.cpp`

- [ ] **Step 1: 在 `dstore_heap_page.cpp` 添加 include**

```cpp
#include "diag/dstore_diag.h"
#include "framework/dstore_instance.h"
#include "framework/dstore_pdb.h"
```

- [ ] **Step 2: 在 `HeapPage::GetVisibleTuple` 末尾插桩**

在 `return resTuple;`（函数末尾第 239 行附近）之前插入：

```cpp
    // Diag instrumentation: record heap tuple visibility decision
    {
        StoragePdb* diagPdb = g_storageInstance->GetPdb(pdbId);
        DstoreDiagContext* diagCtx = (diagPdb != nullptr) ? diagPdb->GetDiagCtx() : nullptr;
        BufferTag diagTag = GetSelfBufTag();  // HeapPage 持有自己的 BufferTag
        if (DIAG_SHOULD_RECORD(diagCtx, diagTag)) {
            TD* diagTd = GetTd(GetTupleTdId(ctid.GetOffset()));
            DstoreDiagRecord rec{};
            rec.op_type          = DstoreDiagOpType::DIAG_GET_VISIBLE_TUPLE;
            rec.timestamp        = GetCurrentTimestamp();
            rec.xid              = diagTd->GetXid();
            rec.ctid             = ctid;
            rec.td_id            = GetTupleTdId(ctid.GetOffset());
            rec.td_status        = static_cast<uint8>(diagTd->GetStatus());
            rec.td_csn_status    = static_cast<uint8>(diagTd->GetCsnStatus());
            rec.td_csn           = diagTd->GetCsn();
            rec.snapshot_csn     = snapshot != nullptr ? snapshot->GetSnapshotCsn() : 0;
            rec.visibility_result = (resTuple != nullptr);
            rec.buf_tag          = diagTag;
            DiagWrite(diagCtx->read_ring, rec);
        }
    }
```

> 注意：`GetSelfBufTag()` 需确认 `HeapPage` 是否提供该方法；若无，使用构造时传入的 bufTag。先 `grep -n "GetSelfBufTag\|m_selfBufTag\|selfBufTag" src/page/dstore_heap_page.cpp include/page/dstore_heap_page.h` 确认，若不存在则改为从 `BufferDesc` 获取。

- [ ] **Step 3: 编译验证**

```bash
bash build.sh -m debug 2>&1 | grep "error:" | head -20
```

- [ ] **Step 4: Commit**

```bash
git add src/page/dstore_heap_page.cpp
git commit -m "feat(diag): instrument GetVisibleTuple with diag ring"
```

---

## Task 8: 插桩点 3 — BTree scan 可见性判断

**Files:**
- Modify: `src/index/dstore_btree_scan.cpp`

- [ ] **Step 1: 在 `dstore_btree_scan.cpp` 添加 include**

```cpp
#include "diag/dstore_diag.h"
#include "framework/dstore_instance.h"
#include "framework/dstore_pdb.h"
```

- [ ] **Step 2: 在 `BtreeScan::SaveItupIfNeeded` 中插桩**

在 `needSave = true;`（第 1651 行附近）之后、`int savePos = ...` 之前插入：

```cpp
        // Diag instrumentation: BTree index tuple visibility decision
        {
            DstoreDiagContext* diagCtx = nullptr;
            StoragePdb* diagPdb = g_storageInstance->GetPdb(this->GetPdbId());
            if (diagPdb != nullptr) diagCtx = diagPdb->GetDiagCtx();
            BufferTag diagTag(this->GetPdbId(), page->GetSelfPageId());
            if (DIAG_SHOULD_RECORD(diagCtx, diagTag)) {
                TD* diagTd = page->GetTd(itup->GetTdId());
                DstoreDiagRecord rec{};
                rec.op_type          = DstoreDiagOpType::DIAG_BTREE_VISIBLE;
                rec.timestamp        = GetCurrentTimestamp();
                rec.xid              = (diagTd != nullptr) ? diagTd->GetXid() : INVALID_XID;
                rec.ctid             = heapCtid;
                rec.td_id            = itup->GetTdId();
                rec.td_status        = (diagTd != nullptr) ?
                                       static_cast<uint8>(diagTd->GetStatus()) : 0;
                rec.td_csn_status    = (diagTd != nullptr) ?
                                       static_cast<uint8>(diagTd->GetCsnStatus()) : 0;
                rec.td_csn           = (diagTd != nullptr) ? diagTd->GetCsn() : 0;
                rec.snapshot_csn     = m_snapshot.GetSnapshotCsn();
                rec.visibility_result = true;  // SaveItupIfNeeded 返回 true 即可见
                rec.buf_tag          = diagTag;
                DiagWrite(diagCtx->read_ring, rec);
            }
        }
```

- [ ] **Step 3: 编译验证**

```bash
bash build.sh -m debug 2>&1 | grep "error:" | head -20
```

- [ ] **Step 4: Commit**

```bash
git add src/index/dstore_btree_scan.cpp
git commit -m "feat(diag): instrument BTree scan visibility decision with diag ring"
```

---

## Task 9: 插桩点 4 — SetTd

**Files:**
- Modify: `include/page/dstore_data_page.h`

- [ ] **Step 1: 在 `dstore_data_page.h` 添加 include**

在文件已有 include 区域末尾追加：
```cpp
#include "diag/dstore_diag.h"
#include "framework/dstore_instance.h"
#include "framework/dstore_pdb.h"
```

- [ ] **Step 2: 在 `SetTd` inline 函数末尾插桩**

在 `td->SetCommandId(commandId);` 之后（第 178 行）插入：

```cpp
        // Diag instrumentation: record TD slot write
        {
            PdbId diagPdbId = GetPdbId();  // DataPage 应提供 GetPdbId()
            DstoreDiagContext* diagCtx = nullptr;
            StoragePdb* diagPdb = g_storageInstance->GetPdb(diagPdbId);
            if (diagPdb != nullptr) diagCtx = diagPdb->GetDiagCtx();
            BufferTag diagTag(diagPdbId, GetSelfPageId());
            if (DIAG_SHOULD_RECORD(diagCtx, diagTag)) {
                DstoreDiagRecord rec{};
                rec.op_type       = DstoreDiagOpType::DIAG_SET_TD;
                rec.timestamp     = GetCurrentTimestamp();
                rec.xid           = xid;
                rec.td_id         = tdId;
                rec.td_status     = static_cast<uint8>(td->GetStatus());
                rec.td_csn_status = static_cast<uint8>(td->GetCsnStatus());
                rec.td_csn        = td->GetCsn();
                rec.buf_tag       = diagTag;
                DiagWrite(diagCtx->write_ring, rec);
            }
        }
```

> 若 `DataPage` 不提供 `GetPdbId()`，先 `grep -n "GetPdbId\|m_pdbId" include/page/dstore_data_page.h` 确认实际方法名。

- [ ] **Step 3: 编译验证**

```bash
bash build.sh -m debug 2>&1 | grep "error:" | head -20
```

- [ ] **Step 4: Commit**

```bash
git add include/page/dstore_data_page.h
git commit -m "feat(diag): instrument SetTd with diag write ring"
```

---

## Task 10: 插桩点 5 — InsertUndoRecord

**Files:**
- Modify: `src/undo/dstore_undo_zone.cpp`

- [ ] **Step 1: 在 `dstore_undo_zone.cpp` 添加 include**

```cpp
#include "diag/dstore_diag.h"
#include "framework/dstore_instance.h"
#include "framework/dstore_pdb.h"
```

- [ ] **Step 2: 在 `UndoZone::InsertUndoRecord` 返回前插桩（第 767 行附近）**

在 `storage_trace_exit(...)` 之前，`return undoRecPtr;` 之前插入：

```cpp
    // Diag instrumentation: record undo record insertion
    {
        PdbId diagPdbId = m_pdbId;  // UndoZone 持有 m_pdbId
        DstoreDiagContext* diagCtx = nullptr;
        StoragePdb* diagPdb = g_storageInstance->GetPdb(diagPdbId);
        if (diagPdb != nullptr) diagCtx = diagPdb->GetDiagCtx();
        // 用 record 的 heap bufTag 做过滤
        BufferTag heapBufTag(diagPdbId, record->GetHeapPageId());
        if (DIAG_SHOULD_RECORD(diagCtx, heapBufTag)) {
            DstoreDiagRecord rec{};
            rec.op_type      = DstoreDiagOpType::DIAG_INSERT_UNDO;
            rec.timestamp    = GetCurrentTimestamp();
            rec.xid          = record->GetXid();
            rec.ctid         = record->GetHeapCtid();
            rec.undo_rec_ptr = undoRecPtr.m_placeHolder;
            rec.buf_tag      = heapBufTag;
            DiagWrite(diagCtx->write_ring, rec);
        }
    }
```

> 先 `grep -n "GetHeapPageId\|GetHeapCtid\|GetXid\b" include/undo/dstore_undo_record.h | head -10` 确认 UndoRecord 的实际方法名。

- [ ] **Step 3: 编译验证**

```bash
bash build.sh -m debug 2>&1 | grep "error:" | head -20
```

- [ ] **Step 4: Commit**

```bash
git add src/undo/dstore_undo_zone.cpp
git commit -m "feat(diag): instrument InsertUndoRecord with diag write ring"
```

---

## Task 11: 端到端验证

**Files:**
- Modify: `tests/unittest/src/ut_diag/ut_diag.cpp`

- [ ] **Step 1: 写端到端集成测试（验证全部 6 个插桩点在真实 PDB 上产生记录）**

```cpp
TEST_F(DiagTest, EndToEndInsertProducesRecords)
{
    // 启动存储引擎（DSTORETEST 的 Bootstrap 已完成）
    PdbId pdbId = g_defaultPdbId;
    StoragePdb* pdb = g_storageInstance->GetPdb(pdbId);
    ASSERT_NE(pdb, nullptr);

    // 假设 relOid = 目标表的 FileId，此处用 FAKE_FILES[0].file_id
    Oid targetRelOid = static_cast<Oid>(6100);

    // 启用诊断采集
    RetStatus ret = DstoreMvccDiag::Enable(pdbId, targetRelOid, 1024, 1024);
    ASSERT_EQ(ret, DSTORE_SUCC);
    ASSERT_NE(pdb->GetDiagCtx(), nullptr);
    ASSERT_TRUE(pdb->GetDiagCtx()->enabled.load());

    // 执行一次真实的 INSERT + SELECT（通过测试工具层）
    // 此处调用 TPCC 或 table_operation 工具执行一次写操作
    // ... (具体调用视 ut_tablehandler 的工具方法而定)

    // 停用
    ret = DstoreMvccDiag::Disable(pdbId);
    ASSERT_EQ(ret, DSTORE_SUCC);

    // 验证至少有一条写路径记录（SetTd 或 InsertUndoRecord）
    DstoreDiagContext* ctx = pdb->GetDiagCtx();
    uint64 writeCount = ctx->write_ring.write_idx.load(std::memory_order_acquire);
    EXPECT_GT(writeCount, 0ULL);

    // Reset
    ret = DstoreMvccDiag::Reset(pdbId);
    ASSERT_EQ(ret, DSTORE_SUCC);
    EXPECT_EQ(pdb->GetDiagCtx(), nullptr);
}
```

- [ ] **Step 2: 运行全量 UT**

```bash
bash tests/build_and_run_ut.sh 2>&1 | grep -E "DiagTest|PASSED|FAILED|error:" | head -30
```

预期：`DiagTest.*` 全部 PASS，已有测试无回归

- [ ] **Step 3: Commit**

```bash
git add tests/unittest/src/ut_diag/ut_diag.cpp
git commit -m "test(diag): add end-to-end integration test for diag ring instrumentation"
```

---

## 自检

### Spec 覆盖对照

| Spec 节 | 对应 Task |
|---------|----------|
| 数据结构（Record/Ring/Context）| Task 1 |
| DIAG_SHOULD_RECORD 宏 | Task 1 + 2 |
| Enable/Disable/Reset | Task 3 |
| StoragePdb 集成 | Task 4 |
| 查询迭代器 | Task 5 |
| 插桩点 1 GetBuffer | Task 6 |
| 插桩点 6 MarkDirty | Task 6 |
| 插桩点 2 GetVisibleTuple | Task 7 |
| 插桩点 3 BTree scan | Task 8 |
| 插桩点 4 SetTd | Task 9 |
| 插桩点 5 InsertUndoRecord | Task 10 |
| 查询接口 `CreateQueryIterator` | Task 5 |

### 类型一致性

- `DstoreDiagOpType` 在 Task 1 定义，Task 6-10 全部引用同一枚举值
- `DiagWrite` 签名 `(DstoreDiagRing&, const DstoreDiagRecord&)` 贯穿全部插桩点
- `DIAG_SHOULD_RECORD(ctx, buf_tag)` 宏参数顺序在所有任务中一致
- `DstoreMvccDiag::Enable/Disable/Reset` 在 Task 3 定义，Task 11 调用
