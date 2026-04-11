# DStore Undo 模块培训材料

## 第一部分：Undo 的双重使命

### 1.1 事务回滚（Rollback）

事务失败或用户主动回滚时，按逆序回放 Undo 记录，撤销已做的修改，恢复到初始状态。

### 1.2 MVCC 旧版本重建

读操作需要访问历史版本时，通过链式遍历 Undo 记录，重建满足快照可见性的旧数据。

```
当前行：(新数据, xid=T3)
           ↓ TdPreUndoPtr
(旧数据, xid=T2)  ← 来自 Undo
           ↓ TdPreUndoPtr
(初始数据, xid=T1) ← 来自 Undo
```

---

## 第二部分：Undo 记录结构

### 2.1 UndoRecPtr：物理地址

`include/undo/dstore_undo_types.h`（第 61-70 行）：

```
UndoRecPtr = fileID(16bit) + pageID(32bit) + offset(16bit)
```

64 位紧凑编码，唯一定位 Undo 日志在存储中的物理位置。

### 2.2 UndoRecordHeader：核心元数据

`include/undo/dstore_undo_record.h`（第 93-157 行）：

```cpp
struct UndoRecordHeader {
    UndoType m_undoType;       // 操作类型（INSERT/DELETE/UPDATE）
    CommandId m_cid;           // 命令 ID（事务内操作顺序）
    UndoTdInfo m_tdPreInfo;    // 【关键】前一个版本的 TD 信息
    uint64 m_txnPreUndoPtr;    // 同事务内前一条 Undo 的指针（事务链）
    uint64 m_ctid;             // 修改的行号（用于匹配查找）
    uint64 m_fileVersion;      // 文件版本
};
```

### 2.3 UndoTdInfo：前一版本 TD 快照

`include/undo/dstore_undo_record.h`（第 44-91 行）：

```cpp
struct UndoTdInfo {
    uint64 m_undoRecPtr;   // 前一版本的 Undo 地址（跨事务链入口）
    uint64 m_xid;          // 前一版本的事务 ID
    CommitSeqNo m_csn;     // 前一版本的 CSN
    uint8 m_tdId;          // 前一版本的 TD 槽位 ID
    TdCsnStatus m_csnStatus;
};
```

**两条链的含义**：

| 字段 | 链类型 | 用途 |
|------|--------|------|
| `m_txnPreUndoPtr` | 同事务链 | 事务回滚时按逆序回放 |
| `m_tdPreUndoPtr`（在 UndoTdInfo） | 跨事务链 | MVCC 读时跳转到前一事务 |

### 2.4 Undo 类型枚举

`include/undo/dstore_undo_types.h`（第 35-59 行）：

```cpp
enum UndoType : uint8 {
    UNDO_HEAP_INSERT,                           // INSERT
    UNDO_HEAP_DELETE,                           // DELETE
    UNDO_HEAP_INPLACE_UPDATE,                   // 原地 UPDATE
    UNDO_HEAP_SAME_PAGE_APPEND_UPDATE,         // 同页扩展 UPDATE
    UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_OLD_PAGE, // 跨页 UPDATE（旧页）
    UNDO_HEAP_ANOTHER_PAGE_APPEND_UPDATE_NEW_PAGE, // 跨页 UPDATE（新页）
};
```

---

## 第三部分：Undo 写入流程

### 3.1 整体调用链

```
事务执行修改
  → 构建 UndoRecord（设置类型/ctid/cid/TdPreInfo）
  → TransactionMgr::InsertUndoRecord()
      → undoRec->SetTxnPreUndoPtr(curTailPtr)  ← 链接事务链
      → UndoZone::InsertUndoRecord()
          → 写入 Undo 页缓冲区
          → 生成 WAL（GenerateWalForUndoRec）
          → 更新 m_nextAppendUndoPtr
  → 返回 UndoRecPtr，事务槽更新尾指针
```

### 3.2 InsertUndoRecord() 关键步骤

`src/undo/dstore_undo_zone.cpp`（第 637-769 行）：

1. **获取写入位置**：`m_nextAppendUndoPtr`
2. **锁定 Undo 页**：`bufMgr->Read(pageId, LW_EXCLUSIVE)`（缓存当前页，减少 buffer 操作）
3. **序列化 Undo 记录**：`record->Serialize()`（使用 Varint 压缩）
4. **写入 Undo 页并生成 WAL**：循环处理，支持跨页写入
5. **更新写入位置**：`m_nextAppendUndoPtr.SetOffset(newOffset)`

### 3.3 事务链形成

```
T1 的 TransactionSlot
  └─ tailUndoPtr → [UndoRecord3]
                      m_txnPreUndoPtr → [UndoRecord2]
                                          m_txnPreUndoPtr → [UndoRecord1]
                                                              m_txnPreUndoPtr → NULL
```

---

## 第四部分：Undo 读取（旧版本重建）

### 4.1 FetchUndoRecordByMatchedCtid()：跨事务链跳转

`src/transaction/dstore_transaction_mgr.cpp`（第 825-877 行）：

```cpp
while (true) {
    // 1. 读取当前 Undo 记录
    UndoZone::FetchUndoRecord(pdbId, undoRec, curPtr, curXid, bufMgr);
    
    // 2. 检查 ctid 是否匹配
    if (undoRec->IsMatchedCtid(ctid)) {
        xid = curXid;
        return DSTORE_SUCC;  // 找到目标版本
    }
    
    // 3. 跨事务跳转
    Xid preXid = undoRec->GetTdPreXid();
    if (preXid != curXid) {
        *matchedCsn = undoRec->GetTdPreCsn();  // 记录前版本 CSN
    }
    
    // 4. 沿链遍历
    curXid = preXid;
    curPtr = undoRec->GetTdPreUndoPtr();
}
```

**跨事务示例**：
```
当前：行数据=v3, TD.xid=T3, 查询快照 csn=45（T2已提交csn=50，T3未提交）

[U3] → ctid 匹配，但 xid=T3（未提交，不可见）→ 继续
[U2] → ctid 匹配，TdPreXid=T2，TdPreCsn=50
      → csn=50 > snapshot.csn=45 → 不可见 → 继续
[U1] → ctid 匹配，TdPreXid=INVALID（初始版本）
      → 初始版本总是可见 → 返回
```

### 4.2 ConstructCrTuple()：完整版本重建

`src/page/dstore_heap_page.cpp`（第 875-950 行）：

```cpp
// 复制页面 TD 数组（不修改原页面）
TD *crTd = DstorePalloc(sizeof(TD) * GetTdCount());
for (uint8 i = 0; i < GetTdCount(); i++) {
    crTd[i] = *GetTd(i);
}

// 回溯版本链
for (;;) {
    xid = crTd[tupleTdId].GetXid();
    undoRecPtr = crTd[tupleTdId].GetUndoRecPtr();
    
    // 跨事务获取前版本 TD 信息
    FetchUndoRecordByMatchedCtid(xid, &record, undoRecPtr, ctid, &csn);
    
    // 回退 TD 到前一版本
    crTd[tupleTdId].RollbackTdInfo(&record);
    
    // 可见性判断
    txnVisible = (csn == INVALID_CSN)
        ? XidVisibleToSnapshot(snapshot, xid, txn)
        : XidVisibleToSnapshot(snapshot, csn);
    
    if (txnVisible) {
        ConstructTupleFromXxxUpdate(&record, resTuple);
        break;
    }
}
```

**为何需要复制 TD（crTd）**：页面上只存最新版本 TD，重建旧版本时不能修改原页面，需临时结构。

---

## 第五部分：Undo 空间管理

### 5.1 UndoZone 结构

```
Undo Segment
  ├─ Meta Page
  ├─ Transaction Slot Pages（XID → Undo 指针映射）
  │   TransactionSlot { xid, startUndoPtr, tailUndoPtr, status }
  └─ Undo Record Pages（循环链表）
      Page0 → Page1 → ... → PageN → Page0
```

### 5.2 空间扩展

`src/undo/dstore_undo_zone.cpp`（第 593-635 行）：

```
当前页剩余空间不足？
  ├─ 否：直接写入
  └─ 是：检查下一页是否有空间
            ├─ 有：继续写入
            └─ 无：AllocNewUndoPages() → ExtendUndoPageRing()
```

### 5.3 Undo 回收机制

`src/undo/dstore_undo_zone.cpp`（第 196-228 行）：

**回收条件**：
```
事务已提交/回滚 AND 事务 CSN < recycleMinCsn
```

**recycleMinCsn 的含义**：
```
活跃事务最小快照 CSN = 1000
  → recycleMinCsn = 1000
  → CSN < 1000 的事务 Undo 可安全回收
  → CSN ≥ 1000 的事务 Undo 不能回收（可能被某个事务需要）
```

**回收操作**：更新 `m_undoRecyclePageId`，后台线程清空回收范围内的 Undo 页。

---

## 第六部分：关键文件速查

| 功能 | 文件 | 行号 |
|------|------|------|
| UndoRecPtr 定义 | dstore_undo_types.h | 61-70 |
| UndoTdInfo | dstore_undo_record.h | 44-91 |
| UndoRecordHeader | dstore_undo_record.h | 93-157 |
| InsertUndoRecord | dstore_undo_zone.cpp | 637-769 |
| FetchUndoRecordByMatchedCtid | dstore_transaction_mgr.cpp | 825-877 |
| ConstructCrTuple | dstore_heap_page.cpp | 875-950 |
| UndoZone 结构 | dstore_undo_zone.h | 163-182 |
| Recycle | dstore_undo_zone.cpp | 196-228 |
