# Tablespace/Segment 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 HeapSegment 页面分配完整路径、AllocExtent Bitmap 管理、ExtentSize 四级策略、Segment.Extend WAL 协议、IndexSegment vs HeapSegment 差异、TempSegment、DDL 崩溃安全机制。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_heap_segment.cpp` | 1979 | HeapSegment：FSM 管理、GetNewPage、多节点 FSM 分配/回收 |
| `dstore_tablespace_wal.cpp` | 1616 | WAL Redo 分派表、所有物理/逻辑 WAL 的 Redo 函数实现 |
| `dstore_tablespace_datafile.cpp` | 1578 | TbsDataFile：Bitmap 分配/释放/扩展、文件 IO |
| `dstore_relation_space.cpp` | 1248 | ObjSpaceMgrTask 后台任务：Extend/RecycleFsm/RecycleBtree 等 |
| `dstore_tablespace_interface.cpp` | 840 | 对外统一接口：AllocSegment/DropSegment/DDL WAL 入口 |
| `dstore_index_segment.cpp` | 724 | IndexSegment：BtreeRecyclePartition 管理、GetNewPage |
| `dstore_segment.cpp` | 686 | Segment 基类：Init/Extend/Drop/ExtentScanner |
| `dstore_tablespace.cpp` | 636 | TableSpace：AllocExtent 路由、AddDataFile、FreeExtent |
| `dstore_tablespace_mgr.cpp` | 562 | TablespaceMgr：Datafile 懒加载、LWLock 管理 |
| `dstore_data_segment.cpp` | 275 | DataSegment：四级 ExtentSize 策略、PrepareFreeDataPages |
| `dstore_index_normal_segment.cpp` | 238 | IndexNormalSegment：创建时分配初始 extent + BtreeRecycle |
| `dstore_heap_normal_segment.cpp` | 84 | HeapNormalSegment：封装继承，无特殊逻辑 |
| `dstore_tablespace_diagnose.cpp` | 248 | 诊断/Dump 辅助 |

---

## 一、HeapSegment：页面分配完整流程

### 1.1 GetNewPage 主干流程

```
HeapSegment::GetNewPage(fsmMetaPageId):

  [延迟初始化] CheckAndInitFreeSpaceMap():
    m_isFsmInitialized == false → InitFreeSpaceMapInternal():
      needNewFsm = (numFsms < MAX_FSM_TREE_PER_RELATION && numFsms < nodeCount)?
      [是] AllocateNewFsmTree() → GetNewPagesForFSM(2页) → InitNewFsmTree()
      [否] ReuseExistingFsmTree() → 找持有最多 FSM 的节点 → 抢最冷 FSM
      → InitFreeSpaceMapList() → FreeSpaceMapList::LoadNewFreeSpaceMap()

  若 fsmMetaPageId == INVALID → 前台扩展
  否则 → 后台扩展（isBgExtension = true）

  fsmNode = m_fsmList->GetFreeSpaceMapForSpace()
  读 fsmMetaPage 获取 oldNumTotalPage (LW_SHARED)
  获取 Extension 锁：SetTableExtensionLockTag(pdbId, fsmMetaPageId)
  二次检查: numTotalPage != oldNumTotalPage?
    [是] 前台：先尝试 GetPageFromFsm(BLCKSZ, 0)，成功则直接返回

  → GetNewPageInternal(fsm, targetPageId, isBgExtension):
      循环直到 needExtension=false:
        Step1: PrepareFreeSlots(fsm, freeSlotCount)
               fsm->GetFsmStatus() 查询 leaf 空闲 slot
               [0个] ExtendFsmPages() → GetNewFsmExtent()
                     [4条WAL: InitExtMeta + LinkNext + AddFsmExt + EndAtomic]
               fsm->AdjustFsmTree()

        Step2: PrepareFreeDataPages(&freeDataPageCount, segMetaPageBuf)
               metaPage->GetUnassignedPageCount() > 0 → 直接返回
               [0个] DoExtend() → 四级 ExtentSize 策略（见§三）

        Step3: AddDataPagesToMetaPages()
               从 unassigned 取页，写 WAL_TBS_SEG_META_ADJUST_DATA_PAGES_INFO

        Step4: AddDataPagesToFsm()
               GetFreeFsmIndex() → 找 leaf FSM page 可用 slot
               InitNewDataPageWithFsmIndex():
                 BatchCreateNewPage + 写逻辑 WAL: WAL_TBS_INIT_MULTIPLE_DATA_PAGES
               fsm->AddMultipleNewPageToFsm() + UpdateFsmStatAfterExtend()

        needExtension = isBgExtension && !fsm->HasEnoughUnusedPages()
```

**关键设计**：
- FSM 初始化惰性触发（`m_isFsmInitialized` 标志），首次 GetNewPage 才初始化
- 二次检查（double-check）在获取 Extension 锁之后，防止多线程重复扩展
- 后台扩展线程循环扩展直到 FSM 积累足够未用页；前台每次仅扩展一批

### 1.2 GetPageFromFsm（非扩展路径）

```
GetPageFromFsm(spaceNeeded, retryTime):
  m_fsmList->GetFreeSpaceMapForSpace() → 轮询找有空间的 FSM
  fsm->ConditionalUpdateFsmAccessTimestamp()
  fsm->GetPage(segMetaPageId, spaceNeeded, retryTime, spaceInFsm, &needExtensionTask):
    搜索 FSM 树找满足 spaceNeeded 的 leaf slot
    若找到 → 返回对应 dataPageId
    若失败 → needExtensionTask=true → m_fsmList->MoveFreeSpaceMapToEnd(fsmNode)
```

### 1.3 FSM 多节点分配策略（.cpp 独有）

```
InitFreeSpaceMapInternal():
  numFsms < MAX_FSM_TREE_PER_RELATION && numFsms < nodeCount:
    → AllocateNewFsmTree()（每节点分配独立 FSM 树）

  numFsms >= 阈值:
    → ReuseExistingFsmTree()：
        选持有最多 FSM 的节点（负载均衡）
        比较 accessTimestamp，选最冷的 FSM 抢占
        极限情况：随机共享已有 FSM
```

---

## 二、AllocExtent：Bitmap 管理与文件扩展

### 2.1 整体调用链

```
SegmentInterface::AllocExtent(pdbId, tablespaceId, extentSize, newExtentPageId):

  [ACCESS_SHARE 锁] tablespace->AllocExtent(extentSize, ...):
    TbsAllocExtentContext::AllocExtent():
      从 m_lastAllocedIndex 开始轮询各 datafile
      dataFile->AllocExtent():
        AllocExtentFromExistGroups():
          从 idleGroupHints 开始扫描 bitmapGroups
          对每个 group，遍历 BITMAP_PAGES_PER_GROUP 张 bitmap 页
          AllocExtentFromBitmapPage()  ← 实际分配

  [若 TBS_ERROR_FILE_BITMAP_GROUP_USE_UP]:
    获取 TbsExtensionLockTag 锁 → AddBitmapGroup():
      文件不够大 → ExtendDataFile()
      InitBitmapPages() + DoAddBitmapGroup() [写 WAL_TBS_ADD_BITMAP_PAGES]

  [若 TBS_ERROR_FILE_SIZE_EXCEED_LIMIT]:
    ProcessFileSizeExceedLimit() → ExtendDataFile()

  [若所有 datafile 满]:
    升级为 EXCLUSIVE 锁 → AllocAndAddDataFile():
      AllocAndCreateDataFile() → WAL: WAL_TBS_CREATE_DATA_FILE + WAL_TBS_ADD_FILE_TO_TABLESPACE
      InitDatafile(): InitTbsFileMeta + InitTbsSpaceMeta + InitBitmap
```

### 2.2 AllocExtentFromBitmapPage 核心逻辑

```cpp
// Bitmap 结构：每页 63 单元 × 128 Bytes = 63 × 128 × 8 bits
// 每个 bit 对应一个 extent，extentSize 决定 bit→块的映射
// FreeBitsSearchPos 维护每张 bitmap 页的搜索起始位置

AllocExtentAsBit(mapPageNo, bitmapMeta, bitmapPage, targetBlockCount):
  startPos = m_bitmapMgr->FindExtentStartPos(bitmapPageNo)
  从 startPos 扫描找到 bit=0 的位置 bitNo
  计算 targetBlockCount = bitNo * extentSize + firstDataPageId.m_blockId
  [超过 totalBlockCount] → 返回 INVALID → 触发文件扩展
  bitmapPage->SetByBit(bitNo) + allocatedExtentCount++
  写 WAL_TBS_BITMAP_ALLOC_BIT_START（含 allocatedExtentCount 用于 Redo 校验）
  m_bitmapMgr->ForwardFreeBitsSearchPos(bitNo+1, bitmapPageNo)  // 推进搜索游标
```

### 2.3 线程安全机制

| 场景 | 锁机制 |
|------|--------|
| 普通 Bitmap 扫描 | datafile 内 bitmap 页加 LW_EXCLUSIVE |
| 添加新 BitmapGroup | `TbsExtensionLockTag`（以 BitmapMetaPage 为锁对象）|
| 文件扩展 | 同上 TbsExtensionLockTag |
| 添加新 DataFile | `LOCK_TAG_TABLESPACE_MGR_ID` 全局锁 + Tablespace ACCESS_EXCLUSIVE 锁 |
| Bitmap 内存搜索游标 | 每 fileId 独立的 `FreeBitsSearchPos` 数组（无锁，仅保护页内操作）|

**关键（.cpp 独有）**：
- `idleGroupHints` 记录上次 BitmapGroup 的扫描起始位置，避免每次从头扫描
- 文件扩展采用两档步进：`FILE_EXTEND_SMALL_STEP=128MB`，`FILE_EXTEND_BIG_STEP=1GB`（文件超过 1GB 时切换大步进）

---

## 三、ExtentSize 四级策略

### 3.1 代码实现（dstore_data_segment.cpp）

```cpp
// 四级 ExtentSize 阈值
const uint64 EXT_NUM_LINE[4]    = {0, 16, 144, 272};
const ExtentSize EXT_SIZE_LIST[4] = {EXT_SIZE_8, EXT_SIZE_128, EXT_SIZE_1024, EXT_SIZE_8192};
//                                     64KB         1MB           8MB           64MB

// PrepareFreeDataPages() / GetNewExtent() 中实时计算：
ExtentSize targetExtSize = EXT_SIZE_8192;           // 默认最大
for (int i = 0; i < EXTENT_SIZE_COUNT - 1; i++) {
    if (currentExtentCount < EXT_NUM_LINE[i + 1]) {
        targetExtSize = EXT_SIZE_LIST[i];
        break;
    }
}
// 临时段固定使用 TEMP_TABLE_EXT_SIZE = EXT_SIZE_8（固定 64KB）
```

### 3.2 四级对应关系

| Extent 序号区间 | ExtentSize | 大小（8K页） | 设计意图 |
|----------------|-----------|------------|---------|
| [0, 16) | EXT_SIZE_8 | 8 页 = 64KB | 建表初期，减少空间浪费 |
| [16, 144) | EXT_SIZE_128 | 128 页 = 1MB | 适度增长 |
| [144, 272) | EXT_SIZE_1024 | 1024 页 = 8MB | 中等表 |
| [272, ∞) | EXT_SIZE_8192 | 8192 页 = 64MB | 大表摊薄元数据开销 |

### 3.3 每种 ExtentSize 对应独立 TbsDataFile

```
TableSpace 按 ExtentSize 分成 4 个 TbsAllocExtentContext（GetIndexByType 映射 0-3）
每个 Context 管理专属的 FileId 组
系统表空间创建时为每个 ExtentSize 各建一个 datafile（共 4 个文件）
```

---

## 四、Segment.Extend WAL 协议

### 4.1 普通 Segment.Extend（3 条 WAL）

```
BeginAtomicWal(xid):
  WAL-1: WAL_TBS_INIT_EXT_META       （新 extent 的 meta page）
  WAL-2: WAL_TBS_MODIFY_EXT_META_NEXT（上一个 extent meta.next 指向新 extent，条件执行）
  WAL-3: WAL_TBS_SEG_ADD_EXT         （Segment Meta Page：extents.count/last 更新）
EndAtomicWal()
```

### 4.2 DataSegment.DoExtend（WAL-3 携带更多信息）

```
BeginAtomicWal(xid):
  WAL-1: WAL_TBS_INIT_EXT_META
  WAL-2: WAL_TBS_MODIFY_EXT_META_NEXT（条件执行）
  WAL-3: WAL_TBS_DATA_SEG_ADD_EXT    （含 addedPageId, isReUsedFlag）
EndAtomicWal()

isReUsedFlag 判断：
  UpdateHwmIfNeed():
    新 extent 起始块 + ExtentSize <= 当前 HWM → isReUse=true（旧 extent 复用）
    否则 → 推进 HWM，写 WAL_TBS_UPDATE_TBS_FILE_META_PAGE
```

### 4.3 HeapSegment GetNewPageInternal 写 WAL 总览

单次前台 GetNewPage 在同一 AtomicWal 中可能包含：

```
BeginAtomicWal:
  ① [可选] DataSegment DoExtend: WAL_TBS_DATA_SEG_ADD_EXT + WAL_TBS_INIT_EXT_META + ...
  ② WAL_TBS_SEG_META_ADJUST_DATA_PAGES_INFO（segMeta dataFirst/dataLast/addedPageId）
  ③ WAL_TBS_INIT_MULTIPLE_DATA_PAGES（逻辑 WAL，最多 1024 页）
  ④ FSM 内部 WAL：WAL_TBS_ADD_FSM_SLOT / WAL_TBS_INIT_FSM_PAGE / WAL_TBS_MODIFY_FSM_INDEX 等
  ⑤ WAL_TBS_FSM_META_UPDATE_EXTENSION_STAT / WAL_TBS_FSM_META_UPDATE_NUM_USED_PAGES
EndAtomicWal
```

---

## 五、IndexSegment vs HeapSegment

| 维度 | HeapSegment | IndexSegment |
|------|-------------|--------------|
| 空闲页管理结构 | PartitionFreeSpaceMap（FsmTree 树形）| BtreeRecyclePartition（环形队列）|
| 空闲页来源 | FSM 记录 freeSpace 比例，按 spaceNeeded 搜索 | FreeList 队列：btree 删除时直接入队 |
| GetNewPage 主路径 | GetPageFromFsm → 搜索 FSM 树 | BtreeRecyclePartition::FreeListPop() |
| 无空闲页时 | GetNewPageInternal → 扩展 DataSegment | AddNewPagesToBtreeRecycle → 扩展 DataSegment |
| 扩展后处理 | 初始化 HeapPage + 写入 FSM slot | 初始化 BtrPage + 批量推入 FreeList |
| 多节点分区 | 每节点一棵 FsmTree（Max=MAX_FSM_TREE_PER_RELATION）| 每节点一个 RecyclePartition（Max=MAX_BTR_RECYCLE_PARTITION）|
| 冷分区回收 | RecycleUnusedFsm（RECYCLE_FSM_TASK）| TryRecycleColdBtrRecyclePartition（RECLAIM_BTREE_TASK）|
| WAL 页初始化 | WAL_TBS_INIT_MULTIPLE_DATA_PAGES（逻辑 WAL，批量）| WAL_TBS_SEG_META_ADJUST_DATA_PAGES_INFO（物理 WAL）|

**IndexSegment 不使用 FSM 的原因**：索引页回收完全由 Btree 自身的删除逻辑驱动，被回收的页直接推入 FreeList，不需要"空闲空间百分比"扫描。

### 5.1 IndexSegment::GetNewPage 流程

```
IndexSegment::GetNewPage(isExtendBg):

  [延迟初始化] InitBtrRecyclePartition()
  读 segMeta 获取 oldAddedPageId
  获取 Extension 锁（以 recyclePartitionMeta 为锁对象）
  二次检查 addedPageId != oldAddedPageId:
    [有新页] FreeListPop() → 直接返回

  AddNewPagesToBtreeRecycle(&result, isExtendBg):
    PrepareFreeDataPages() → 按需扩展 DataSegment
    GetFreeDataPageIds() → 取 unassigned 页
    metaPage->AddDataPages() + 写 WAL_TBS_SEG_META_ADJUST_DATA_PAGES_INFO
    CreateNewPages() → BatchCreateNewPage 初始化 BtrPage
    FreeListBatchPushNewPages():
      [前台] 第 0 页直接返回给调用者，其余推 FreeList
      [后台] 全部推 FreeList
```

---

## 六、TempSegment

### 6.1 临时对象存储策略

| 特性 | 实现 |
|------|------|
| 所属表空间 | `TEMP_TABLE_SPACE_ID` |
| Buffer 管理器 | 线程本地 `thrd->GetTmpLocalBufMgr()`，非全局 BufMgr |
| ExtentSize | 固定 `TEMP_TABLE_EXT_SIZE = EXT_SIZE_8`（64KB），不升级 |
| WAL | 不写任何 WAL（`IsTempSegment()` 检查，所有 WAL 路径跳过）|
| Bitmap 结构 | `TbsTempBitmapPageHashTable` 内存哈希（非磁盘 bitmap 页）|

### 6.2 DropSegment 的特殊清理

普通 Segment Drop 只调用 `FreeExtent`，TempSegment Drop 额外调用 `InvalidateBufferInExtent`：

```cpp
// 逐页 invalidate 线程本地 buffer
for (uint16 i = 0; i < static_cast<uint16>(extentSize); i++) {
    m_bufMgr->InvalidateByBufTag(bufTag, false);
}
```

**原因**：临时 buffer 是线程私有的，不会通过共享内存淘汰，必须手动清理，否则复用相同 PageId 时会读到旧数据。

### 6.3 启动清理（RemoveAllTempFiles）

- 启动时扫描 `TMP_TBS_START_FILE_ID..TMP_TBS_MAX_FILE_ID`
- 三步清理：①控制文件有关联 → `FreeAndRemoveDataFile`；②仅控制文件有记录 → `FreeDataFileId`；③物理文件残留 → 直接删除

---

## 七、RelationSpace：ObjSpaceMgr 后台任务

RelationSpace（`dstore_relation_space.cpp`）的核心是 `ObjSpaceMgrTask` 后台任务体系，是 Segment 与后台线程之间的桥梁。

### 7.1 主要 Task 类型

| Task 类型 | 触发条件 | 执行内容 |
|-----------|----------|----------|
| `EXTEND_TASK` | HeapSegment FSM 设 `needExtensionTask=true` | 后台 `segment->GetNewPage(fsmMetaPageId)` |
| `RECYCLE_FSM_TASK` | FSM 长期未访问 | `segment.RecycleUnusedFsm()` 冷 FSM 重分配 |
| `RECYCLE_BTREE_TASK` | Btree 页删除积累 | `BtreePageRecycle::BatchRecycleBtreePage()` |
| `RECLAIM_BTREE_RECYCLE_PARTITION_TASK` | RecyclePartition 过冷 | 接管冷分区的 free/recycle 队列 |
| `EXTEND_INDEX_TASK` | IndexSegment FreeList 耗尽 | `AddNewPagesToBtreeRecycle()` |

**Task 去重**：`IsCurrentTask()` 比较 taskType + tablespaceId + segmentId + extraInfo，确保同一扩展任务不重复提交。

### 7.2 Segment 对象生命周期

```
Segment 对象是短生命期的栈上对象，没有全局注册表：
  HeapNormalSegment segment(pdbId, segmentId, tablespaceId, bufMgr, ctx)
  segment.InitSegment()
  segment.GetNewPage(BLCKSZ, retryTime)

持久化标识只有 segmentId（PageId），上层（RelCache）负责记录 segmentId→OID 映射
```

---

## 八、TablespaceDataFile：文件 IO 实现

### 8.1 文件创建与初始化

```
TbsDataFile::Create(initBlockCount, storeSpaceName):
  vfs->CreateFile(fileId, fileName, filePara)
  vfs->Extend(fileId, GetOffsetByBlockNo(initBlockCount))  // 预分配到 initBlockCount 大小

TbsDataFile::Init():
  InitTbsFileMeta()   → 写 TbsFileMetaPage (page 0) + WAL_TBS_INIT_TBS_FILE_META_PAGE
  InitTbsSpaceMeta()  → 写 TbsSpaceMetaPage (page 1) + WAL_TBS_INIT_TBS_SPACE_META_PAGE
  InitBitmap():
    读取当前文件大小 vfs->GetSize()
    bitmapMeta->InitBitmapMetaPage() + WAL_TBS_INIT_BITMAP_META_PAGE
    AddBitmapGroup(0)  ← 初始化第一组 bitmap 页
```

### 8.2 文件扩展两档步进策略

```
ExtendDataFile(targetBlocks):
  if (totalBlockCount >= FILE_EXTEND_BIG_STEP):  // 当前文件 >= 1GB (131072 块)
      targetBlocks = RoundUp(size, FILE_EXTEND_BIG_STEP=1GB)
  else:
      targetBlocks = RoundUp(size, FILE_EXTEND_SMALL_STEP=128MB)
      // 模板 PDB: TEMPLATE_FILE_EXTEND_SMALL_STEP=8MB

  vfs->Extend(fileId, targetBlocks * BLCKSZ)
  写 WAL_TBS_EXTEND_FILE（含 totalBlockCount）
```

### 8.3 HWM 管理的乐观并发

```
UpdateHwmIfNeed():
  先用 beforeAllocHwm 做乐观判断（无锁）
  失败才加锁读 tbsFileMetaPage->hwm
  若需推进：写 WAL_TBS_UPDATE_TBS_FILE_META_PAGE
```

---

## 九、DDL 恢复：逻辑 WAL + ddlXid

### 9.1 WAL 类型对比

| 类型 | 代表 WAL Record | Redo 方式 |
|------|----------------|-----------|
| 物理 WAL | WAL_TBS_BITMAP_ALLOC_BIT_START, WAL_TBS_SEG_ADD_EXT, ... | 直接修改页内字段 |
| 逻辑 WAL（DDL） | WAL_TBS_CREATE_TABLESPACE, WAL_TBS_CREATE_DATA_FILE, WAL_TBS_DROP_TABLESPACE, WAL_TBS_DROP_DATA_FILE | 操作 ControlFile + 物理文件 |
| 逻辑 WAL（批量） | WAL_TBS_INIT_MULTIPLE_DATA_PAGES, WAL_TBS_INIT_BITMAP_PAGES | 逐页初始化（最多1024页/条记录）|

### 9.2 CREATE TABLESPACE 崩溃安全流程

```
主节点：
  controlFile->AllocTbsId()
  → 写 WAL_TBS_CREATE_TABLESPACE（含 tablespaceId, ddlXid, tbsMaxSize, preReuseVersion）
  → AllocAndAddDataFile × 4（每个 ExtentSize 一个文件）
    └─ 写 WAL_TBS_CREATE_DATA_FILE（含 fileId, ddlXid, extentSize）
    └─ 写 WAL_TBS_ADD_FILE_TO_TABLESPACE（含 slotId, hwm, ddlXid）

Redo (WalRecordTbsCreateDataFile::Redo):
  vfs->CreateFile() + vfs->Extend()  [幂等：文件已存在则跳过创建]
  controlFile->UpdateCreateDataFile(ddlXid)
```

### 9.3 DROP TABLESPACE 崩溃安全

```
主节点：
  写 WAL_TBS_DROP_DATA_FILE × N → 写 WAL_TBS_DROP_TABLESPACE

Redo (WalRecordTbsDropDataFile::Redo):
  vfs->Close(fileId) + vfs->RemoveFile(fileName)
  controlFile->UpdateDropDataFile(ddlXid)
```

### 9.4 ddlXid 与 preReuseVersion 的防 ABA 机制

```
ddlXid：DDL 操作的事务 ID，存储在：
  ControlDataFilePageItemData.ddlXid
  TbsFileMetaPage.m_ddlXid

preReuseVersion：防 Redo 误操作
  Redo 时检查 ControlFile 中该 tablespaceId 的 reuseVersion
  若 reuseVersion 已更新（被新 DDL 复用）→ Redo 幂等跳过

逻辑 WAL Redo 特点：
  通过 ControlFile API 原子更新内存+磁盘状态
  加 Tablespace 排他锁，确保并发 Redo 安全
```

### 9.5 AllocAndCreateDataFile 的 WAL 写入顺序（关键）

```
AllocAndCreateDataFile():
  1. 分配 fileId（选 reuseVersion 最小的空闲项）
  2. 更新 datafileItem，MarkPageDirty
  3. 写 WAL（WAL_TBS_CREATE_DATA_FILE）并等待持久化  ← 先持久化 WAL
  4. 创建实际数据文件
  5. PostGroup() 写控制文件                           ← 再写控制文件

关键顺序：先 WAL 后控制文件，崩溃时可通过 WAL 重放重建控制文件状态
```

---

## 十、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| FSM 初始化 | `m_isFsmInitialized` 标志 | 首次 GetNewPage 才初始化；有 double-check 锁模式（先读后加锁再读）|
| FSM 多节点分配 | fsmInfos[] 数组 | 优先建新 FSM；超 MAX 则抢最冷 FSM（比较 accessTimestamp）；极限随机共享 |
| ExtentSize 升级 | 列举四个枚举值 | PrepareFreeDataPages 中按当前 extent 数量实时计算，每次扩展独立决策 |
| AllocExtent 线程安全 | 无说明 | BitmapGroup 满用 TbsExtensionLockTag；文件不够大用全局锁+双重检查（比较 HWM）|
| WAL 批量初始化 | WAL 接口 | WAL_TBS_INIT_MULTIPLE_DATA_PAGES 一条记录最多 1024 页；Redo 时逐页初始化 |
| IndexSegment 无 FSM | 继承自 DataSegment | 完全不使用 PartitionFreeSpaceMap；用 BtreeRecyclePartition FreeList 代替 |
| TempSegment Buffer | 线程私有 | Drop 时必须 InvalidateBufferInExtent 逐页清理，否则复用 PageId 读到脏页 |
| HWM 乐观并发 | 无说明 | 先用 beforeAllocHwm 无锁判断；失败才加锁读 hwm |
| DDL WAL 幂等 | 无说明 | preReuseVersion 检查：Redo 时 reuseVersion 不匹配则安全跳过 |
| 文件扩展步进 | 无说明 | 两档步进：<1GB 用 128MB 步进，>= 1GB 用 1GB 步进 |
| idleGroupHints | 无说明 | 记录上次 BitmapGroup 扫描起点，避免每次从头扫描（性能关键优化）|
| 后台扩展任务锁 | 无说明 | 后台 EXTEND_TASK 用 ACCESS_SHARE_LOCK（允许并发 DML）|
| PAGES_ADD_TO_FSM_PER_TIME | 常量声明 | 固定 1024 页，每次 GetNewPage 最多向 FSM 注入 1024 个新页 |
| reuseVersion 防 ABA | 有字段定义 | 分配时优先 reuseVersion=0，次选最小值，并检查文件是否实际存在 |
