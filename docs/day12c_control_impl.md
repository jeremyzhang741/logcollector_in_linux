# Control File & Checkpoint 模块 .cpp 实现精读

> **目标**：从头文件的"是什么"推进到 .cpp 的"怎么做"，重点梳理 ControlFile 物理结构、双文件崩溃安全机制、PdbInfo/WalInfo/CsnInfo 的持久化细节、以及 Checkpoint 与崩溃恢复的完整联动。

---

## 文件速览

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `dstore_control_pdbinfo.cpp` | 1965 | PDB 元数据的增删改查，哈希索引加速查找，多集群 Standby 信息管理 |
| `dstore_control_tablespace.cpp` | 1747 | 表空间/数据文件的分配、释放、WAL 记录联动 |
| `dstore_control_file_mgr.cpp` | 1298 | 控制文件的页面缓存、CRC 校验、双文件写入与恢复核心引擎 |
| `dstore_control_walinfo.cpp` | 581 | WAL 流的增删改查，Checkpoint 写入 diskRecoveryPlsn |
| `dstore_control_file.cpp` | 547 | ControlFile 门面类，串联所有 Group 的 Init/Create/Load 流程 |
| `dstore_control_group.cpp` | 388 | 所有 Group 的公共基类，实现 LoadGroup/PostGroup/AddOneItem |
| `dstore_control_relmap.cpp` | 293 | 系统表 OID→PageId 映射，Shared/Local 两张表 |
| `dstore_control_logicrep.cpp` | 197 | 逻辑复制槽的持久化（增删改查） |
| `dstore_control_csninfo.cpp` | 183 | 最大保留 CSN 与 Undo Zone Map 段 ID 的持久化 |
| `dstore_control_disk_file.cpp` | 171 | 底层磁盘文件的创建/打开/读写页面/Fsync，封装 VFS |
| `dstore_control_file_lock.cpp` | 118 | 进程内 pthread_rwlock（生产）/ 文件 flock（单元测试） |

---

## 一、ControlFile 物理结构

### 1.1 整体布局

```
控制文件（database_control_1 / database_control_2）
总大小：3840 × 8KB = 30MB（按 1MB 对齐）

Block 偏移   区域                             说明
──────────────────────────────────────────────────────────
0            FileMetaPage                     控制文件整体元数据（magic、totalPageCount 等）
1            Tablespace MetaPage              表空间 Group 的控制元数据
2            WalStream MetaPage               WAL 流 Group 的控制元数据
3            CsnInfo MetaPage                 CSN Group 的控制元数据
4            RelMap MetaPage                  RelMap Group 的控制元数据
5            PdbInfo MetaPage                 PDB Group 的控制元数据
6            LogicRep MetaPage                逻辑复制 Group 的控制元数据
7~63         (保留，最多支持 64 个 Group)
──────────────────────────────────────────────────────────
64~1303      Tablespace Data Pages (1240 页)
             ├── TbsItems：按 tablespaceId 直接寻址
             └── DataFile Items：按 fileId 直接寻址
1304~3224    WalStream Data Pages (1921 页)   WAL 流条目（线性扫描）
3225         CSN Data Page (1 页)             MetaPage 内嵌，无独立 DataPage
3226~3230    RelMap Data Pages (5 页)
             ├── Shared Page（block 3226）：共享系统表 OID→PageId
             └── Local Page（block 3227）：本地系统表 OID→PageId
3231~3263    PdbInfo Data Pages (33 页)       所有 PDB 槽位（固定预分配）
3264~3775    LogicRep Data Pages (513 页)     逻辑复制槽条目
3776~3839    (未使用，对齐到 30MB)
```

### 1.2 每页通用页头（ControlPageHeader）

```cpp
struct ControlPageHeader {
    uint32 m_checksum;    // CRC32 校验和（校验时此字段置 0 后计算）
    uint32 m_magic;       // 魔术数，区分 MetaPage / DataPage
    uint16 m_pageType;    // 页面类型枚举
    uint16 m_dataOffset;  // 数据区起始偏移
    uint32 m_nextPage;    // 链表指针（DataPage 链表用）
    uint32 m_version;
    uint16 m_writeOffset; // 当前写入高水位线
    uint16 m_reserved;
};
```

**关键**：checksum 是第一个成员（偏移 0），CRC 计算时先将该字段清零，对整页计算 CRC32，再回填。这避免了 checksum 字段参与自身校验的循环依赖。

### 1.3 MetaPage 扩展结构（ControlMetaHeader）

```cpp
struct ControlMetaHeader {
    uint64 m_term;        // 写入轮次（每次成功写入 +1，用于选最新副本）
    uint32 m_lastPageId;  // 当前 Group 最后一个已用 DataPage 块号
    uint32 m_version;
    uint8  m_flag;        // 0=写完成, 1=正在写（Writing 标记）
    uint8  m_reserved[7];
    ControlPageRange m_pageRange[8];  // 最多 8 个不连续 DataPage 区间
};
```

---

## 二、控制文件崩溃安全机制

### 2.1 三重保护叠加

```
┌─────────────────────────────────────────────────────┐
│  Level 1：双文件（database_control_1 / _2）          │
│  Level 2：每页 CRC32 校验                            │
│  Level 3：MetaPage Writing 标记 + Term 版本号        │
└─────────────────────────────────────────────────────┘
```

### 2.2 CRC 校验实现

```cpp
// 写入时：
UpdateControlPageCrc(&ctrlPage->m_pageHeader.m_checksum, page):
    *checksum = 0;
    *checksum = CompChecksum(page, BLCKSZ, CHECKSUM_CRC);

// 验证时（CheckPageCrc）：
uint32 curChecksum = *checksum;
*checksum = 0;                          // 暂存并清零
uint32 newChecksum = CompChecksum(...); // 重算
*checksum = curChecksum;                // 恢复
return curChecksum == newChecksum;
```

### 2.3 PostPageHandle：原子写协议（最核心的设计）

`PostPageHandle` 是所有 Group 写入的统一出口：

```
PostPageHandle(pageHandle, metaBlock):

  [Step 1] 如果两副本不一致，先恢复落后的那一份

  [Step 2] 对 File1 执行写入序列：
    (a) MetaPage.MarkWriting()        → 设置 m_flag = 1
    (b) WritePage(metaBlock)          → 写 MetaPage 到 file1
    (c) Fsync(file1)                  → 持久化 Writing 标记
    (d) 写所有脏 DataPage 到 file1
    (e) Fsync(file1)                  → 持久化数据
    (f) MetaPage.MarkWriteFinished()  → 设置 m_flag = 0
    (g) MetaPage.SetTerm(term + 1)    → 版本号 +1
    (h) WritePage(metaBlock)          → 写最终 MetaPage
    (i) Fsync(file1)                  → 持久化完成状态

  [Step 3] 对 File2 重复 (a)~(i)

  崩溃时序分析：
    崩溃于 (a)-(c)：file1 有 Writing 标记 → 启动时视为无效
    崩溃于 (d)-(e)：file1 DataPage 部分写 → CRC 失败 → 视为无效
    崩溃于 (f)-(i)：file1 term 未推进或 CRC 错 → 选 file2（term 旧但一致）
    崩溃于 Step3：file1 完整，file2 损坏 → 从 file1 恢复 file2
```

**关键（.h 不可见）**：term 在 Step2(g) 才推进。File2 写入时先设 flag=1/旧 term，最终 flag=0/term+1——确保两份文件最终 term 相同。

---

## 三、ControlGroup：多副本冗余

### 3.1 GetValidMetaPage 选主策略

```
GetValidMetaPage(metaBlock):
  各读 file1/file2 的 MetaPage，验证 CRC + !Writing:

  两份都有效：
    term1 == term2 → BOTH_VALID（两份一致，任选）
    term1 != term2 → 选 term 更大者（最后写入的）

  只有一份有效  → 选该份；PostGroup 时恢复另一份
  两份都无效：
    两份都在 Writing → DSTORE_FAIL（不 PANIC）
    否则            → PANIC
```

### 3.2 ReadOnePage 容错降级

```
ReadOnePage(pageHandle, blockNumber):
  读取页面并验证 CRC；
  若失败 && 状态为 BOTH_META_PAGES_ARE_VALID:
      切换到另一份文件重试
      成功 → 更新 pageHandle->file
      失败 → PANIC
```

### 3.3 锁设计

- **生产环境**：`pthread_rwlock_t`，进程内读写锁
- **单元测试**：`fcntl(F_SETLKW)`，文件锁，支持多进程并发
- 每个 Group 独立持有一把读写锁，不同 Group 的写操作互不阻塞

---

## 四、PdbInfo：PDB 状态持久化

### 4.1 存储内容

每个 PDB 在控制文件占一个固定槽位，包含：

| 字段 | 说明 |
|------|------|
| pdbId / pdbName / pdbUuid | 基本身份信息 |
| pdbStatus | UNCREATED / CREATING / OPENED_READ_WRITE / CLOSED / DROPPING |
| vfsName | 对应的 VFS 名称 |
| pdbRoleMode | 主/备角色模式 |
| pdbSwitchStatus | 主备切换状态 |
| pdbReplicaStatus | 副本重建进度 |
| isFullRepair / isInRestoreFromBackup | 修复/恢复状态标记 |
| pdbRecycleCsnMin | CSN 回收下界 |
| standbyPdbInfo[MAX_DR_CLUSTER_COUNT+1] | 各备集群的同步模式、VFS、storeSpace 信息 |

### 4.2 预分配槽位（.cpp 独有发现）

创建时将 PDB_START_ID～PDB_MAX_ID **全部预填充**为 UNCREATED，非动态增长：

```cpp
// Create() 时：
for (PdbId j = PDB_START_ID; j <= PDB_MAX_ID; j++) {
    ControlPdbInfoPageItemData::Init(&pdb, j, "", PDB_STATUS_UNCREATED, "", "");
    page->AddItem(&pdb, sizeof(ControlPdbInfoPageItemData));
}
```

这使得 PdbInfo DataPage 是**定长数组**，可按 pdbId 整除取余直接寻址。

### 4.3 内存哈希索引（.cpp 独有）

`LoadControlFile()` 时构建内存哈希表 `m_pdbHashIndex`：

```
m_pdbHashIndex: PdbId → { pdbName, pdbStatus, pdbItemPointer{blkno, offset} }

查询：hash_search(pdbId) → (blkno, offset) → GetPdbInfoItemByCtid() → O(1)
写后：PdbIndexUpdateItem() 同步更新哈希表
```

### 4.4 PDB 状态机

```
UNCREATED → CREATING（AllocPdbId）
          → OPENING → OPENED_READ_WRITE（SetOpenedFlag）
          → CLOSED
          → DROPPING（SetDeleteFlag）
          → UNCREATED（FreePdbId，归还槽位）
```

**防重名**：AllocPdbId 先遍历所有条目，若存在同名且 status != UNCREATED 则直接报错。

---

## 五、WalInfo：WAL 检查点信息

### 5.1 每个 WAL 流条目关键字段

| 字段 | 含义 |
|------|------|
| walId / streamState | WAL 流 ID 与状态 |
| walMinRecoveryPlsn | 该流的最小恢复点 |
| **lastCheckpointPLsn** | 最近 Checkpoint 的 WAL 记录起始位置（Redo 扫描起点） |
| **lastWalCheckpoint.diskRecoveryPlsn** | 最近 Checkpoint 时已持久化的最小 PLSN（Redo 终点） |
| lastWalCheckpoint.time | Checkpoint 时间戳 |
| barrier | barrierCsn、barrierEndPlsn、barrierSyncMode（DR 场景） |

### 5.2 Checkpoint 更新控制文件的精确流程

```cpp
UpdateWalStreamForCheckPoint(walId, lastCheckpointPLsn, checkPoint):
    ExclusiveLock(walInfoGroup);
    LoadGroup();                          // 从最新副本读 MetaPage
    找到 walId 条目，更新：
        itemData->lastCheckpointPLsn = lastCheckpointPLsn;
        itemData->lastWalCheckpoint  = checkPoint;   // 含 diskRecoveryPlsn
    MarkPageDirty;
    PostGroup();   // 双文件原子写
    UnlockGroup;
```

**关键**：Checkpoint 只更新两个 PLSN：
- `lastCheckpointPLsn`：Redo 扫描起点（从哪里扫 WAL）
- `diskRecoveryPlsn`：Redo 终点（磁盘已持久化，之前的修改无需重放）

### 5.3 WAL 流的动态增减

- **创建**：`CreateAndAllocateOneWalStream`，幂等；walId = metadata.m_maxWalId + 1
- **删除**：`DeleteWalStreamInfo`，通过页内数据后移（RemoveItem）实现，收缩高水位指针
- **更新**：`UpdateWalStreamInternal`，先删除旧条目再追加新条目，保持页内紧凑

---

## 六、CheckpointMgr：三计数器协议

控制文件在 Checkpoint 中的角色：

```
Checkpoint 流程（控制文件视角）：

1. CheckpointMgr 触发（checkpointStart 计数器 +1）

2. 等待 BgDiskPageWriter 推进 minDiskRecoveryPlsn

3. 调用 ControlWalInfo::UpdateWalStreamForCheckPoint:
   ┌──────────────────────────────────────────────────────────┐
   │  ExclusiveLock(walInfoGroup)                             │
   │  更新：                                                  │
   │    lastCheckpointPLsn  = 本次 CheckpointWalRecord.plsn  │
   │    diskRecoveryPlsn    = BgDiskPageWriter.minPlsn       │
   │  PostGroup()：双文件原子写（Writing 标记保护）            │
   └──────────────────────────────────────────────────────────┘

4. checkpointDone 计数器 +1
   → Backend 检测到 Done == Start，Checkpoint 完成

Backend 不轮询等待：只检查计数器条件，不参与控制文件写入。
```

---

## 七、崩溃恢复：从控制文件到 Redo 入口

### 7.1 Init 时的 CRC 恢复顺序

```cpp
ControlFile::Init():
  1. InitFileMgrAndGroup()      // 创建所有 Group 对象
  2. OpenControlFiles()         // 打开 file1/file2
  3. LoadControlFile()          // 分配内存（不读磁盘）
  4. CheckCrcAndRecovery(FileMetaPage)
  5. CheckCrcAndRecovery(PdbInfo)    // 顺序：PDB → RelMap → CsnInfo
  6. CheckCrcAndRecovery(RelMap)     //     → Tablespace → WalInfo
  7. CheckCrcAndRecovery(CsnInfo)    //     → LogicRep
  8. CheckCrcAndRecovery(Tablespace)
  9. CheckCrcAndRecovery(WalInfo)
  10. CheckCrcAndRecovery(LogicRep)
```

每个 `CheckCrcAndRecovery` 的决策树：

```
读 file1/file2 各一份 MetaPage：

BOTH_VALID      → 无需修复
FIRST_VALID     → GroupPagesRecovery(file2 ← file1)
SECOND_VALID    → GroupPagesRecovery(file1 ← file2)
NO_VALID        → PANIC

GroupPagesRecovery：
  按 MetaPage 的 pageRange 逐页检查 CRC
  SinglePageRecovery：从好副本覆盖坏副本
  最后修复 MetaPage 本身
```

### 7.2 恢复决策信息读取

```
上层 Recovery 从控制文件读取：

ControlWalInfo::GetWalStreamInfo(walId):
  → lastCheckpointPLsn:   WAL Redo 扫描起点
  → diskRecoveryPlsn:     已持久化终点（之前无需重放）
  → streamState:          是否 SYNC_DONE（可跳过）

ControlCsnInfo::GetMaxReservedCSN:
  → 恢复后 CSN 从此值起分配，防止重复

ControlCsnInfo::GetUndoZoneMapSegmentId:
  → Undo 恢复的段 ID

ControlRelmap::GetSysTableItem:
  → 系统表文件位置（恢复时打开关键系统表）

ControlPdbInfo（通过 BuildPdbHashIndex）:
  → 枚举 OPENED_READ_WRITE / CLOSED 的 PDB 执行恢复
  → CREATING / DROPPING 状态的 PDB：未完成 DDL，特殊处理
```

### 7.3 L2 Restore 后的 PdbInfo 修复（.cpp 独有）

`RectifyPdbInfoAfterL2Restore()` 的专项逻辑：
- 将所有非 OPENED_READ_WRITE 的 PDB 重置为 UNCREATED
- 探测 VFS 实际存在性（MountVfs），不存在则重置
- 对 template0/template1/rootpdb 等系统 PDB，按配置文件修正 vfsName

---

## 八、RelMap：OID→FileId 映射

### 8.1 两张表

```
CONTROLFILE_PAGEMAP_RELMAP_START     → Shared 系统表（全局，如 pg_class）
CONTROLFILE_PAGEMAP_RELMAP_START + 1 → Local 系统表（各 PDB 私有）

初始化时：sharedPage->SetNextPage(localPage)
查找顺序：先查 Shared，再查 Local
```

### 8.2 条目格式

```cpp
struct ControlSysTableItemData {
    Oid    sysTableOid;  // 系统表 OID
    PageId segmentId;    // { m_fileId, m_blockId }
};
```

### 8.3 全量重写策略

```
WriteAllSysTableItem(type, items[], count):
    controlPage->InitDataPage(pageType);   // 清空现有内容
    controlPage->AddItem(items, count);    // 批量追加
    PostGroup()
```

**原因**：系统表 OID 映射在初始化时一次性写入，后续极少变更，全量覆盖比增量更新更简单可靠。

---

## 九、CsnInfo 与 TablespaceInfo

### 9.1 CsnInfo：最简单的 Group

所有数据内嵌在 MetaPage，无独立 DataPage：

```cpp
struct ControlCsnPageData {
    uint32        m_version;
    CommitSeqNo   m_csn;       // 最大已保留 CSN（单调递增）
    PageId        m_segmentId; // Undo Zone Map 的存储位置
};
```

**只增不减**（代码强制）：
```cpp
SetMaxReservedCSN(csn):
    if (csn <= current->m_csn) return DSTORE_SUCC;  // 禁止回退
    current->m_csn = csn;
```

### 9.2 TablespaceInfo：O(1) 直接寻址

```cpp
// 计算物理位置（不需要扫描）：
GetTbsPageItemCtid(tablespaceId):
    offset = tablespaceId % MAX_TABLESPACE_ITEM_CNT_PER_PAGE
    blkno  = tablespaceId / MAX_TABLESPACE_ITEM_CNT_PER_PAGE + DEFAULT_TABLESPACE_PAGE

GetDataFilePageItemCtid(fileId):
    offset = fileId % MAX_DATAFILE_ITEM_CNT_PER_PAGE
    blkno  = fileId / MAX_DATAFILE_ITEM_CNT_PER_PAGE + DEFAULT_DATAFILE_PAGE
```

#### 关键字段

| 字段 | 含义 |
|------|------|
| fileIds[MAX_SPACE_FILE_COUNT] | 该表空间包含的所有 DataFile ID 数组 |
| hwm | fileIds 数组的高水位线 |
| reuseVersion | 重用计数（防 ID 复用时旧引用混淆） |
| ddlXid | 最近一次 DDL 操作的事务 ID |

#### WAL 与控制文件的联动顺序（.cpp 独有）

```
AllocAndCreateDataFile():
  1. 分配 fileId（选 reuseVersion 最小的空闲项）
  2. 更新 datafileItem，MarkPageDirty
  3. 写 WAL（WAL_TBS_CREATE_DATA_FILE）并等待持久化
  4. 创建实际数据文件
  5. PostGroup() 写控制文件

FreeTbsId/FreeDataFileId:
  1. ResetItem(ddlXid)（reuseVersion++）
  2. 写 WAL（WAL_TBS_DROP_TABLESPACE/DATA_FILE）
  3. PostGroup()
```

**关键顺序**：先持久化 WAL，再写控制文件——崩溃恢复时可通过 WAL 重放重建控制文件状态。

#### reuseVersion 的防 ABA 设计

分配空闲 fileId 时优先选 `reuseVersion == 0`（从未使用），其次选最小值，并检查文件名是否实际存在（`vfs->FileExists`），防止 ID 回绕后旧文件残留引发冲突。

---

## 十、关键发现总结（.h vs .cpp 差异）

| 概念 | .h 层描述 | .cpp 实现细节 |
|------|---------|------------|
| 文件冗余 | 存在 file1/file2 | PostPageHandle 的 7 步写入时序（MarkWriting→写数据→MarkDone→推 term） |
| CRC | 有 checksum 字段 | checksum 为页面首字节，计算前清零自身；写入和验证均独立读磁盘 |
| term 选主 | 有 m_term 字段 | GetValidMetaPage 用 term 选最新副本；两份 term 相等才 BOTH_VALID |
| PdbInfo 槽位 | 有 AllocPdbId/FreePdbId | 创建时预填充全部槽位为 UNCREATED；内存哈希索引加速 ID 查找（O(1)） |
| WalInfo 更新 | UpdateWalStream 接口 | 内部先删除再追加（保持紧凑），非原地更新 |
| Tablespace 寻址 | GetTbsPageItemPtr | tablespaceId/fileId 整除取余直接计算 blkno+offset，O(1) |
| Tablespace WAL | 无说明 | AllocAndCreateDataFile 先写 WAL 并等持久化，再写控制文件 |
| reuseVersion | 有字段定义 | 分配时优先 reuseVersion=0，次选最小值，并检查文件实际存在防 ABA |
| CsnInfo 只增 | 无限制说明 | SetMaxReservedCSN 代码层强制 csn <= current 时直接返回 |
| RelMap 批量写 | WriteAllSysTableItem 签名 | 内部先 InitDataPage 清空，再批量 AddItem（全量覆盖） |
| 恢复顺序 | Init/CheckCrcAndRecovery | 按 PdbInfo→RelMap→CsnInfo→Tablespace→WalInfo→LogicRep 顺序恢复 |
| L2 Restore | 无 | RectifyPdbInfoAfterL2Restore 探测 VFS 实际存在性并修正 vfsName |
| 锁类型 | SHARE/EXCLUSIVE | 生产用 pthread_rwlock，UT 用 fcntl 文件锁 |
| Checkpoint 计数器 | 三计数器协议 | Backend 不轮询；只检查 checkpointDone == checkpointStart 条件 |
