# Tuple 模块 .cpp 精读

## 1. dstore_data_tuple.cpp — 公共数据层

### 1.1 模块定位

`DataTuple` 是一个**静态工具类**（所有方法均为 static 或通过模板分发），为 `HeapDiskTuple` 和 `IndexTuple` 提供共享的序列化/反序列化基础设施。它本身没有任何成员变量，所有逻辑均通过模板参数 `TupleType` 特化为具体 tuple 类型。文件末尾的显式实例化（`template void ...`）说明这是一个典型的**分离式模板编译**模式。

### 1.2 列数据大小计算：`ComputeDataSize`

两个重载：
- `ComputeDataSize(desc, values, isnull)` — 无 LOB 统计
- `ComputeDataSize(desc, values, isnull, lobSize)` — 额外输出 LOB 数据总字节数

核心分支（针对每列）：
```
if (isnull[i]) → skip
else if (AttIsPackable && DSTORE_VARATT_CAN_MAKE_SHORT) → DstoreVarAttConvertedShortSize(val)
else if (tdhaslob && AttIsLob && VarAttIs4B && size > MAX_INLINE_LOB_SIZE=2048)
    → dataLength += VARHDRSZ_EXTERNAL + sizeof(VarattLobLocator)  // locator 占位 17 字节
    → lobSize += DstoreVarSize4B(val)                             // LOB 数据单独计入 lobSize
else if (attlen==-1 && DstoreVarAttIsExternalExpanded)
    → AttAlignNominal + DstoreExpandedVarSize
else
    → AttAlignDatum + AttAddLength
```

关键点：**LOB 列在 dataSize 中只占 `VARHDRSZ_EXTERNAL + sizeof(VarattLobLocator)` = 1 + 16 = 17 字节的 locator 占位符**，实际 LOB 数据追加在 tuple 末尾单独存放。

### 1.3 序列化：`AssembleData<hasNull, TupleType>`

**内存布局构建过程**（以 `HeapDiskTuple` 为例）：
1. 指针 `tuple` 指向整个 DiskTuple 内存块起点
2. `lobValues = tuple + dataSize`（LOB 数据追加区从 dataSize 偏移开始）
3. 若 `hasNull`：取 `nullBit = diskTuple->GetNullBitmap()`，初始化为全 0
4. `tupleValues = diskTuple->GetValues()`（跳过 header + null bitmap + OID 后的数据区）
5. 逐列调用 `AssembleColumnData`，按列逻辑对 `tupleValues` 指针前进

**NULL 位图编码**：每列 1 bit，从低位到高位，`nullBit[i/8] |= (1 << (i%8))`。注意：**1 表示非 NULL，0 表示 NULL**（与 PostgreSQL 相同的约定，`DataTupleAttrIsNull` 用 `!(bit & mask)` 检查）。

### 1.4 `AssembleColumnData` — 按列存储分支

| 列类型 | 判断条件 | 写入方式 | 对齐 |
|------|---------|---------|------|
| pass-by-value | `att->attbyval == true` | `StoreAttByVal` | `AttAlignNominal` |
| expanded varlena | `DstoreVarAttIsExternalExpanded` | `DstoreformExpandedVar`（展平）| 对齐后写 |
| external/short varlena | `DstoreVarAttIsExternal` non-expanded | `memcpy_s`，调用 `SetHasExternal()` | **无对齐**（1-byte header） |
| short varlena | `DstoreVarAttIsShort` | `memcpy_s` | **无对齐** |
| 可压缩 varlena | `AttIsPackable && DSTORE_VARATT_CAN_MAKE_SHORT` | 转短头写入 | **无对齐** |
| LOB 大字段 | `AttIsLob && size > 2048` | 写 locator 占位符 + 追加到 `lobValues` | 无对齐 |
| full 4-byte varlena | 剩余情况 | `memcpy_s` | `AttAlignNominal` |
| cstring (attlen=-2) | `ATT_CSTR_LEN_TYPE` | `memcpy_s` with strlen+1 | **无对齐**（attalign 必须是 'c'） |
| fixed-length pass-by-ref | `attlen > 0, !attbyval` | `memcpy_s` | `AttAlignNominal` |

写入短 varlena 时，`DstoreSetVarSizeShort(tupleValues, dataLength)` 设置 1 字节头，然后 `memcpy_s` 数据从 `VarData(val)` 开始写，跳过原来的 4 字节头。

### 1.5 反序列化：`DisassembleData<hasnulls>`

逻辑是 AssembleData 的逆过程：
- `hasnulls`：编译期模板参数，避免运行时每次 NULL 检查
- `slow`：状态标志，一旦遇到 NULL 或 varlena，禁用 `attcacheoff` 缓存
- `DisassembleColumnData`：处理 LOB locator 的特殊情况：若 `locator->ctid == INVALID_ITEM_POINTER`（LOB 数据内联追加），从 `lobValues` 指针读取；否则从 locator 正常路径读取

### 1.6 偏移量计算：快速路径 vs 慢速路径

**快速路径 `CalculateOffset`**：仅适用于"目标列前无 NULL、无 varlena"的场景。从 `att[0]->attcacheoff=0` 出发，顺序计算所有定长列的对齐偏移并缓存，直接返回 `att[attNum]->attcacheoff`。

**慢速路径 `CalculateOffsetSlow`**：逐列遍历，对每列检查：
1. 若为 NULL 列，`usecache = false`，继续
2. 若有 `attcacheoff >= 0` 且 `usecache`，直接使用缓存
3. varlena 列：使用 `AttAlignPointer`（检查实际内容是否 1-byte header），可能禁用 usecache
4. 到达目标列 `attNum` 时 break，同时返回 `loboff`（前面 LOB 列的累计数据大小）

---

## 2. dstore_disk_tuple.cpp — DiskTuple 接口适配层

### 2.1 定位

该文件属于 **`DiskTupleInterface` 命名空间**，为 V5 SQL 引擎提供直接访问 `HeapDiskTuple` 结构的 C 风格接口（带 `GCC visibility(default)` 导出）。所有函数都是 `HeapDiskTuple` 方法的**薄包装**，主要目的是跨 ABI 边界隔离。

### 2.2 关键函数：`FillDiskTuple`

这是该文件中唯一含有实质逻辑的函数，流程：
1. `diskTup->SetNumColumn(attributeNum)` — 写列数到 `m_info.val.m_numColumn`（11-bit 字段）
2. 按需 `SetHasNull()`、`SetHasOid()`
3. 根据 `hasNull` 分支调用模板化的 `DataTuple::AssembleData<true/false, HeapDiskTuple>`
4. `DstoreSetVarSize(diskTup, diskTupSize)` — 将整个 DiskTuple 的大小写入 `m_ext_info.m_datum_info.m_len`（varlena 4-byte 头字段）
5. 写 `tdtypeid` 和 `tdtypmod`

**注意**：`DstoreSetVarSize` 写的是 `m_datum_info.m_len`，这个字段与 `m_tuple_info.m_tdId/m_lockerTdId/m_size/m_xid` 共用同一个 union `m_ext_info`。磁盘上存储的是 `TupleField`，内存中作为 Datum 操作时使用 `DatumField`。这是一个重要的**二义性 union 设计**。

### 2.3 `GetValuesOffset` 的精确计算

```
GetValuesOffset(natts, hasNull, hasOid, isLinked) =
    MAXALIGN(
        sizeof(HeapDiskTuple)                              // 固定头部 = 12 字节
      + hasNull * ceil(natts/8)                            // null bitmap
      + hasOid * sizeof(Oid)                               // OID (4 字节)
      + isLinked * (sizeof(ItemPointerData)+sizeof(uint32)) // 链式头 = 8+4=12 字节
    )
```

`sizeof(HeapDiskTuple)` = `offsetof(HeapDiskTuple, m_data)` = union(8字节) + uint32(4字节) = **12 字节**（即 `HEAP_DISK_TUP_HEADER_SIZE`）。

---

## 3. dstore_index_tuple.cpp — 索引 Tuple

### 3.1 IndexTuple 内存布局

```
[m_link: 8字节 IndexLink union][m_info: 4字节 union][可选: null bitmap][MAXALIGN填充][key values][可选尾部]
```

- `sizeof(IndexTuple) == 16`（static_assert 验证）
- `GetDataOffset(hasNull)` = `MAXALIGN(16)` = 16（无 NULL）或 `MAXALIGN(16 + GetBitmapLen(32))` = `MAXALIGN(16+4)` = 24（有 NULL）
- 位图固定按 `INDEX_MAX_KEY_NUM=32` 列分配（4字节），不按实际 natts 动态计算，这与 HeapDiskTuple 不同

### 3.2 `FormTuple` 构建流程

```cpp
hoff = IndexTuple::GetDataOffset(hasnull)           // 16 或 24
dataSize = ComputeDataSize(tupleDescriptor, values, isnull)
size = hoff + dataSize
// 检查 size <= MAX_INDEXTUPLE_SIZE_ON_BTREE_PAGE
tp = DstorePalloc0(size)                            // 必须用 Palloc0 确保标志位初始为 0
DataTuple::AssembleData<hasNull, IndexTuple>(tupleDescriptor, values, isnull, tp, hoff+dataSize)
tuple->SetSize(size)
```

**关键**：使用 `DstorePalloc0`（清零分配），因为 `m_info` 字段中多个标志位（如 `m_notPlainLeaf`、`m_tdStatus` 等）必须初始为 0 才能表示普通叶子节点状态。

### 3.3 `Compare` — 索引 Tuple 比较

用于 **Undo 回滚时 B-Tree 页面定位**，支持两种场景：
1. 普通叶子 vs Pivot：`keyNum1 >= keyNum2`（pivot 可以截断尾部列）
2. 普通叶子 vs 普通叶子：`keyNum1 == keyNum2`

比较算法：
1. 逐列比较（最多 `minKeyNum` 列），调用 `FunctionCall2Coll` 使用算子类型的比较函数
2. NULL 比较遵循 `INDEX_OPTION_NULLS_FIRST`：若设置，NULL < NOT_NULL，否则 NULL > NOT_NULL
3. 列值相等且 `keyNum1 > keyNum2` → 返回 1（tuple 位于 pivot 右侧）
4. 全局分区索引（`SYS_RELKIND_GLOBAL_INDEX`）：再按 `tableOid` 排序
5. 最终 tiebreaker：比较 `heapCtid`（`ItemPointerData::Compare`）

**方向处理**：若 `!(indoption[i] & INDEX_OPTION_DESC)`，则 `InvertCompareResult(&result)`——**未设置 DESC 时**才取反，意味着默认存储顺序是降序，取反后变升序。

### 3.4 `Truncate` — Pivot 截断

用于 B-Tree 分裂时创建 pivot 元组：
1. `CopyTupleDesc(desc)` 创建临时描述符
2. `truncdesc->natts = keepNatt` 减少列数
3. `DeformTuple` 再 `FormTuple`（重新序列化，去掉尾部列）
4. `truncated->SetHeapCtid(&m_link.heapCtid)` — 保留原 heapCtid

### 3.5 `CheckAttIsNull` — NULL 位图检查

针对给定 `attnum`，检查**前面是否有任何 NULL**（而非只检查 attnum 本身是否为 NULL）：
- 检查 `bp[byte]` 中低于 `finalbit` 的 bit 是否有 0
- 检查 `bp[0..byte-1]` 是否全为 `0xFF`

若存在前置 NULL，设 `slow = true`，强制走慢速路径计算偏移。

### 3.6 `GetAttrNocache` — 无缓存列值获取

与 `HeapTuple::GetAttr` 逻辑对称，但简化（无 LOB、无系统列、无越界检查）：
1. `attnum--`（1-based 转 0-based）
2. `CheckAttIsNull(attnum, slow)` 检查前置 NULL
3. 若 `att[attnum]->attcacheoff >= 0 && !slow` → 直接返回缓存
4. 检查 `HasVariable()` 设 slow
5. 调用 `CalculateOffsetSlow<IndexTuple>` 或 `CalculateOffset<IndexTuple>`
6. `FetchAtt(att[attnum], tp + off)`

### 3.7 Pivot 特殊字段布局（尾部追加）

Pivot tuple 的可选尾部字段，从末尾向前分配（倒序）：
- `[heap TID（8字节）]`：若 `hasCtidBreaker`，在末尾 `GetSize() - sizeof(ItemPointerData)` 处
- `[tableOid（4字节）]`：若 `hasTableOid`，在 heap TID 之前
- `GetPivotTableOid()` 计算：`offset = sizeof(Oid) + (hasCtidBreaker ? sizeof(ItemPointerData) : 0)`，从 `GetSize() - offset` 处读取

---

## 4. dstore_memheap_tuple.cpp — 内存堆 Tuple

### 4.1 HeapTuple 内存结构

```
[HeapTupleHeader m_head][HeapDiskTuple *m_diskTuple 指针(8字节)]
         ^                               |
         |                               v
         |              [实际 DiskTuple 数据（紧接在 HeapTuple 之后）]
```

`FormTuple` 中内存布局：
```
totalSize = sizeof(HeapTuple) + diskTupleSize + lobSize
tuplePointer = [    HeapTuple(约40字节)    ][  HeapDiskTuple 数据  ][  LOB 数据  ]
```

`HeapTupleHeader` 含字段：`len`（DiskTuple 大小）、`type`（固定=3）、`ctid`、`tableOid`、`datumTypmod`、`datumTypeid`、`deleteXidForDebug`、`lobTargetOid`，共约 **40 字节**。

### 4.2 `FormTuple` 完整流程

```
1. valuesOffset = HeapDiskTuple::GetValuesOffset(natts, hasNull, hasOid, false)
2. lobSize = 0; dataSize = DataTuple::ComputeDataSize(desc, values, isnull, lobSize)
3. diskTupleSize = valuesOffset + dataSize
4. totalSize = sizeof(HeapTuple) + diskTupleSize + lobSize
5. Palloc0(totalSize)  → memTuple @ start; diskTuple @ start+sizeof(HeapTuple)
6. diskTuple->SetNumColumn / SetHasNull / SetHasOid
7. memTuple->SetDiskTupleSize(diskTupleSize)  // m_head.len = diskTupleSize (不含 lobSize!)
8. DataTuple::AssembleData<hasNull, HeapDiskTuple>(desc, values, isnull, diskTuple, diskTupleSize)
9. diskTuple->SetDatumTypeId / SetDatumTypeMod
10. memTuple->m_diskTuple = diskTuple
11. memTuple->SetDatumVarSize(diskTupleSize)  // DstoreSetVarSize 写 m_datum_info.m_len
```

**关键**：`m_head.len` 只存 `diskTupleSize`，不包含 LOB 数据大小。LOB 数据物理上紧跟在 diskTuple 之后，通过 locator 的 `ctid == INVALID_ITEM_POINTER` 标志识别内联 LOB。

### 4.3 `GetAttr` — 5步取值流程

```
Step 0: attNum <= 0 → GetSysattr（系统列）
Step 0b: attNum > m_diskTuple->GetNumColumn() → GetTupInitDefVal（ADD COLUMN 后默认值）
Step 1: attNum-- (1-based → 0-based)
         若 AttIsLob && !forceReturnLobLocator && diskTuple->HasInlineLobValue()
             → needCheckLobValue = true
Step 2: CheckHasNull → 若目标列是 NULL 直接返回
        （同时扫描前置列检查是否有 NULL 设置 slow）
Step 3: 若 !slow && att[attNum]->attcacheoff >= 0
             && GetValuesOffset() + cacheoff < GetDiskTupleSize()
         → 直接 FetchAtt，若无 inline LOB 直接返回
Step 4: CheckHasVarAtt 可能设 slow
         CalculateOffsetSlow 或 CalculateOffset
         若 off 超过 tuple 大小 → GetTupInitDefVal
Step 5: FetchAtt; 若 needCheckLobValue 且 locator->relid == DSTORE_INVALID_OID
         → lobValues = diskTuple基地址 + diskTupleSize + loboff
         → 返回内联 LOB 数据指针
```

**LOB 读取的内联检测**：`locator->relid == DSTORE_INVALID_OID` 表示 LOB 数据尚未写入实际 LOB 存储，直接从内存中的追加区读取。

### 4.4 系统列（`GetSysattr`）

| attNum | 枚举 | 返回值 |
|--------|------|--------|
| -1 | `DSTORE_SELF_ITEM_POINTER_ATTRIBUTE_NUMBER` | `m_head.ctid` |
| -2 | `DSTORE_OBJECT_ID_ATTRIBUTE_NUMBER` | `m_diskTuple->GetOid()` |
| -7 | `DSTORE_TABLE_OID_ATTRIBUTE_NUMBER` | `m_head.tableOid` |
| -3 | `DSTORE_TRX_INSERT_XID_ATTRIBUTE_NUMBER` | `m_diskTuple->GetXid()` |
| -5 | `DSTORE_TRX_DELETE_XID_ATTRIBUTE_NUMBER` | `m_head.deleteXidForDebug` |

### 4.5 `DeformTuple` 全列解包

```
tupleValues = diskTuple->GetValues()
lobValues = (char*)diskTuple + diskTupleSize   // LOB 追加区
nullBits = diskTuple->GetNullBitmap()
end = min(tupleDesc->natts, GetNumAttrs())
DisassembleData<HasNull>(context)
// 若 tupleDesc->natts > 实际列数，读 initdefvals 作为默认值
```

### 4.6 `ModifyTuple` — Tuple 修改

标准的"先 Deform 再 Form"模式：
1. `DeformHeapTuple` 解包旧 tuple → values/isnull 数组
2. 遍历 `doReplace[]`，覆盖需要修改的列值
3. `FormHeapTuple` 重新打包为新 tuple
4. 从旧 tuple 复制元数据：`ctid`、`tableOid`、`tdId`、`xid`、`oid`（如有）

### 4.7 `Copy` — Tuple 拷贝的限制

两个重载：
1. **分配式 Copy**：`totalSize = m_head.len + sizeof(HeapTuple)`，分配后 `memcpy_s` 整个 diskTuple 区域。这里 `m_head.len` 是 `diskTupleSize`，**LOB 追加数据不会被拷贝**（这是已知的设计取舍）
2. **填充式 Copy**：`Copy(destTup, srcTup, isExternalMem)`，destTup 必须是已分配内存，直接复制 `m_head` 并 `memcpy_s` diskTuple 数据

### 4.8 `TupleDescData::Copy`

分配策略：单块连续内存 = `sizeof(TupleDescData)` + `natts * sizeof(Form_pg_attribute)` (MAXALIGN) + `natts * MAXALIGN(ATTRIBUTE_FIXED_PART_SIZE)`。`initdefvals` 和 `constr` 置 nullptr（不拷贝约束和默认值）。

---

## 5. MVCC 可见性：TdStatus 三状态完整路径

### 5.1 TD 结构核心字段

```cpp
struct TD {
    uint64 m_xid;          // 事务 Xid（20bit zoneId + 44bit logicSlotId）
    CommitSeqNo m_csn;     // 提交序列号
    uint64 m_undoRecPtr;   // Undo 记录指针
    uint64 m_lockerXid;    // 加锁者 Xid（与 m_xid 分离）
    CommandId m_commandId;
    uint16 m_status : 2;   // TDStatus 三态
    uint16 m_csnStatus : 2; // IS_INVALID / IS_PREV_XID_CSN / IS_CUR_XID_CSN
    uint16 m_pad : 12;
};
```

### 5.2 `TdCsnStatus` 三态语义

| 状态 | 含义 |
|------|------|
| `IS_INVALID` | CSN 无效，TD 刚被分配或已被回收 |
| `IS_PREV_XID_CSN` | CSN 属于复用前的**上一个**事务（TD 复用时保留旧 CSN，让快照判断无需回溯 undo） |
| `IS_CUR_XID_CSN` | CSN 属于当前（m_xid）事务 |

### 5.3 可见性判断实际路径（`IsTupleVisibleFlashbackCsn`）

```
TupleTdStatus = diskTuple->GetTdStatus()
│
├── DETACH_TD → visible（tupleCSN < recycleMinCsn < snapshotCsn）
│
├── ATTACH_TD_AS_NEW_OWNER
│     读 basePage->GetTd(tdId)
│     ├── td->IS_CUR_XID_CSN → csn = td->GetCsn(); invisible = (flashbackCsn <= csn)
│     └── 否则 → XidVisibleToSnapshot(snapshot, xid, txn)（查 undo zone）
│
└── ATTACH_TD_AS_HISTORY_OWNER
      undoRecPtr = td->GetUndoRecPtr()
      FetchUndoRecordByMatchedCtid(xid, &record, undoRecPtr, ctid, &csn)
      ├── 成功且 csn != INVALID → invisible = (flashbackCsn <= csn)
      ├── 成功但 csn == INVALID → XidVisibleToSnapshot(...)
      └── UNDO_ERROR_RECORD_RECYCLED → visible（undo 已回收说明历史版本够旧）
```

### 5.4 TdStatus 写入时机

TD 回收时（`RefreshTupleTdStatus<TupleType>`）：
- 若 TD slot `unused == true` → 设置元组为 `DETACH_TD`（冻结）
- 若 TD slot `unused == false` → 设置为 `ATTACH_TD_AS_HISTORY_OWNER`（CSN 已知，但 XID 不再在 TD 中）
- Inplace Update 时写：`ATTACH_TD_AS_NEW_OWNER`（在 `HeapUpdateHandler` 中）

---

## 6. TupleLock 实现机制

TupleLock **不使用独立的锁字段**，而是复用 `HeapDiskTuple` 的 `m_lockerTdId`（8 bit）字段：

```
diskTuple->SetLockerTdId(tdId)         // 写锁者 TD slot ID 到 tuple 头
page->GetTd(tdId)->SetLockerXid(xid)   // 写锁者 XID 到 TD
page->GetTd(tdId)->SetStatus(OCCUPY_TRX_IN_PROGRESS)
```

**解锁**：事务提交/回滚时，`RefreshTupleTdStatus` 中 `SetLockerTdId(INVALID_TD_SLOT)`。

**等待逻辑**：`WaitTupleIfNeed → WaitTxn(lockerXid, LW_SHARED/EXCLUSIVE)`，等待锁持有者事务结束。

---

## 7. Update/Delete 的 Tuple 链与 LiveMode

`HeapDiskTupLiveMode` 枚举记录 tuple 的 update 类型，写在 `m_info.val.m_liveMode`（3 bit）：

| LiveMode | 含义 | ctid 变化 |
|----------|------|-----------|
| `TUPLE_BY_NORMAL_INSERT` | 普通插入 | ctid 指向自身 |
| `NEW_TUPLE_BY_INPLACE_UPDATE` | 原位更新（同槽位） | `newCtid = oldCtid`（不变） |
| `NEW_TUPLE_BY_SAME_PAGE_UPDATE` | 同页更新，旧 tuple ctid 改变 | 旧 tuple ctid → 新 tuple 位置 |
| `OLD_TUPLE_BY_SAME_PAGE_UPDATE` | 同页更新的旧版本 | — |
| `NEW_TUPLE_BY_ANOTHER_PAGE_UPDATE` | 跨页更新新版本 | — |
| `OLD_TUPLE_BY_ANOTHER_PAGE_UPDATE` | 跨页更新旧版本 | — |
| `TUPLE_BY_NORMAL_DELETE` | 普通删除 | — |

**Inplace Update** 的关键：新 tuple 写入相同 ctid 槽位，`updateContext->newCtid = updateContext->oldCtid`，无需更新索引。

---

## 8. 大 Tuple 链式存储（Linked Tuple Chunks）

当 tuple 数据超出单页容量，分多个 chunk 存储：
- 每个 chunk 的 `HeapDiskTuple` 头部额外携带：`[ItemPointerData nextChunkCtid(8字节)][uint32 numChunks(4字节)]` 在 `m_data` 起始处（`LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE = 12`）
- 链式标志：`m_linkInfo` 2-bit：`TUP_NO_LINK_TYPE=0`、`TUP_LINK_FIRST_CHUNK_TYPE=1`、`TUP_LINK_NOT_FIRST_CHUNK_TYPE=2`
- NULL bitmap 在链式 tuple 中偏移：`m_data + LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE`
- `dstore_heap_page.cpp::Dump` 中的 `alignShift` 修正：`LINKED_TUP_CHUNK_EXTRA_HEADER_SIZE = 9`（实际存储大小）不满足 8 字节对齐，需负向修正才能正确定位数据区

---

## 9. 各 Tuple 类型对比表

| 特性 | DataTuple（工具类） | HeapDiskTuple（磁盘 Heap） | IndexTuple（B-Tree） | HeapTuple（内存包装） |
|------|---------|---------|------------|--------------|
| 头部大小 | 无（纯静态） | 12 字节（union m_ext_info 8B + m_info 4B） | 16 字节（m_link 8B + m_info 4B + 对齐） | 约 40 字节（HeapTupleHeader） |
| NULL 位图 | `ceil(natts/8)` | `ceil(natts/8)`，动态 | 固定 4 字节（按 INDEX_MAX_KEY_NUM=32 分配） | 委托给 diskTuple |
| NULL 编码 | 1=非NULL，0=NULL | 同左 | 同左 | 同左 |
| LOB 支持 | 通过 VarattLobLocator | 支持 inline LOB（追加区） | **不支持**（AssertFalse） | 通过 diskTuple |
| MVCC 字段 | — | `m_tdStatus`(2bit) + `m_liveMode`(3bit) + `m_tdId` + `m_lockerTdId` | `m_tdId`(8bit) + `m_tdStatus`(2bit) | 通过 diskTuple 访问 |
| 链式存储 | 不支持 | 支持（m_linkInfo 2bit + next chunk ctid） | 不支持 | 不支持 |
| ctid | — | 不含（在 HeapTupleHeader 中） | 含 `m_link.heapCtid`（指向 heap tuple） | `m_head.ctid` |
| 最大列数 | 1664（`MAX_TUPLE_ATTR`） | 2047（11-bit m_numColumn） | 32（`INDEX_MAX_KEY_NUM`） | 同 diskTuple |
| 大小字段 | — | `m_tuple_info.m_size`(16bit) 磁盘；varlena len 内存 | `m_info.val.m_tupleSize`(13bit，最大 8191字节) | `m_head.len` |

---

## 10. .h vs .cpp 新发现汇总

| 序号 | 模块 | 在 .h 中的理解 | .cpp 中的真实实现 |
|------|------|--------------|----------------|
| 1 | DataTuple | 以为是抽象基类 | 实际是纯静态工具类，无虚函数，无成员变量，通过模板参数分发给 HeapDiskTuple/IndexTuple |
| 2 | LOB 存储 | 以为 varlena 字段直接存 TOAST 指针 | 引入第三种存储：`VarattLobLocator`（tag=38），大于 2048 字节的 LOB 列写 17 字节占位符，数据追加在 tuple 末尾 `diskTupleSize` 偏移处 |
| 3 | m_head.len | 以为记录整个 tuple 总大小 | 实际只记录 `diskTupleSize`，不含 inline LOB 数据（Copy 不拷贝 LOB 区） |
| 4 | NULL 位图分配 | 以为 HeapDiskTuple 和 IndexTuple 一致 | IndexTuple 固定按 `INDEX_MAX_KEY_NUM=32` 分配 4 字节位图，HeapDiskTuple 按实际 `natts` 动态计算 |
| 5 | TupleTdStatus | 以为写在单独的字段 | 实际嵌在 `m_info.val.m_tdStatus`（2bit），与其他标志位共享同一个 uint32 |
| 6 | TupleLock | 以为有独立锁字段或锁管理器 | 通过 `m_lockerTdId`（8bit）+ TD 的 `m_lockerXid` 实现，无独立锁对象 |
| 7 | Update 后旧版本链接 | 以为通过 ctid 字段形成链表 | 通过 `HeapDiskTupLiveMode`（3bit）标记版本类型；**没有传统 PostgreSQL 的 t_ctid 前向指针链** |
| 8 | IndexTuple LOB 支持 | 以为与 HeapDiskTuple 一致 | `SetHasExternal()` 和 `SetHasInlineLobValue()` 均包含 `StorageAssert(false)`，索引列不允许 LOB |
| 9 | MVCC 可见性 | 以为有 CheckVisible 函数在 tuple 层 | tuple 层只存 `tdStatus`，真正的可见性判断在 scan 层（`IsTupleVisibleFlashbackCsn`），需要读 basePage 的 TD 数组 |
| 10 | IS_PREV_XID_CSN | 描述模糊 | 明确含义：TD 复用时保留上一个已提交事务的 CSN，标记为 `IS_PREV_XID_CSN`，让快照判断不需要回溯 undo 即可知道"此前版本已提交且 CSN 为 X" |
| 11 | m_ext_info union | 以为 DatumField/TupleField 互斥使用 | 同一内存区域分别在两种场景使用：磁盘场景用 `TupleField`（m_tdId/m_lockerTdId/m_size/m_xid），Datum 场景用 `DatumField`（m_len/m_typmod/m_typeid），切换时需显式调用 `DstoreSetVarSize` |
| 12 | IndexTuple 大小限制 | 仅知道 B-Tree 页面有限制 | `m_tupleSize`（13bit）硬限制最大 8191 字节；还受 `MAX_INDEXTUPLE_SIZE_ON_BTREE_PAGE = DATA_SIZE_ON_BTREE_PAGE / (NUMBER_HIKEY_PER_BTREE_PAGE + 1)` 运行时约束 |
