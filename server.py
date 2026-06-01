#!/usr/bin/env python3
"""WAL 日志收集分析平台 - 后端服务（DuckDB 版）"""

import gzip
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

import duckdb

# ==================== 配置 ====================
BUFFER_DIR           = os.environ.get('BUFFER_DIR', './buffer')
DB_PATH              = os.environ.get('DB_PATH', './data/logvista.duckdb')
HOST                 = os.environ.get('SERVER_HOST', '0.0.0.0')
PORT                 = int(os.environ.get('SERVER_PORT', 8080))

IMPORT_WORKERS       = 1          # DuckDB 单写者，固定 1
IMPORT_BATCH_ROWS    = 200_000    # 单次 INSERT 行数上限（按文件数估算）
IMPORT_BATCH_TIMEOUT = 5.0        # 秒，超时即刷入
QUEUE_MAX_SIZE       = 500        # 背压上限（文件数）

buffer_queue: queue.Queue = None  # type: ignore

_import_stats = {'imported': 0, 'files': 0, 'last_import': None}
_import_stats_lock = threading.Lock()

# WAL 日志解析正则（兼容旧格式上传）
WAL_PATTERN = re.compile(
    r'dump_stream_(?P<stream>[\d_]+):.*?'
    r'plsn:(?P<plsn>\d+);.*?'
    r'xid:\s*\((?P<xid_h>\d+),\s*(?P<xid_l>\d+)\);.*?'
    r'type:(?P<type>\w+);.*?'
    r'pageId=\((?P<pg1>\d+),\s*(?P<pg2>\d+)\);.*?'
    r'page prev glsn:(?P<prev_glsn>\d+)',
    re.IGNORECASE
)


# ==================== DuckDB 工具 ====================
def get_read_con():
    """创建 DuckDB 查询连接（供 HTTP 查询线程使用，用后立即关闭）
    注意：DuckDB 同进程内不允许 read_only 连接与 read-write 连接共存，
    因此不使用 read_only=True，DuckDB 内部保证并发安全。"""
    return duckdb.connect(DB_PATH)


def rows_to_dicts(result):
    """将 DuckDB execute 结果转为 list[dict]"""
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


def init_db():
    """初始化 DuckDB 数据库和表结构"""
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            server_id VARCHAR PRIMARY KEY,
            hostname  VARCHAR,
            ip        VARCHAR,
            os_info   VARCHAR,
            registered_at TIMESTAMP DEFAULT now(),
            last_seen     TIMESTAMP,
            log_count     BIGINT DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS log_entries (
            id             BIGINT,
            server_id      VARCHAR NOT NULL,
            hostname       VARCHAR,
            stream         VARCHAR,
            plsn           VARCHAR,
            xid            VARCHAR,
            log_type       VARCHAR,
            page_id        VARCHAR,
            page_prev_glsn BIGINT,
            message        TEXT,
            created_at     TIMESTAMP DEFAULT now()
        )
    """)
    con.execute("CREATE SEQUENCE IF NOT EXISTS log_entries_id_seq")
    con.execute("CREATE INDEX IF NOT EXISTS idx_le_server  ON log_entries(server_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_le_created ON log_entries(created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_le_xid     ON log_entries(xid)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_le_pageid  ON log_entries(page_id)")
    con.close()
    print("DuckDB 初始化完成")


# ==================== 缓冲区管理 ====================
def init_buffer_dir():
    """创建 BUFFER_DIR，验证写权限"""
    os.makedirs(BUFFER_DIR, exist_ok=True)
    test = os.path.join(BUFFER_DIR, '.write_test')
    with open(test, 'w') as f:
        f.write('ok')
    os.remove(test)
    print(f"缓冲区目录就绪: {BUFFER_DIR}")


def write_buffer_file(server_id, hostname, records):
    """
    将 records 写入 gzip JSONL 缓冲文件（.tmp → fsync → rename 保证原子性）。
    返回最终文件路径。
    """
    server_dir = os.path.join(BUFFER_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    uid = uuid.uuid4().hex[:8]
    filename = f'{ts}_{uid}.jsonl.gz'
    final_path = os.path.join(server_dir, filename)
    tmp_path = final_path + '.tmp'

    with gzip.open(tmp_path, 'wt', encoding='utf-8') as f:
        for r in records:
            entry = dict(r)
            entry['server_id'] = server_id
            entry['hostname'] = hostname
            json.dump(entry, f, ensure_ascii=False)
            f.write('\n')

    # fsync 确保数据落盘
    with open(tmp_path, 'rb') as f:
        os.fsync(f.fileno())

    os.rename(tmp_path, final_path)
    return final_path


def scan_buffer_on_startup():
    """启动时扫描残留缓冲文件重入队，清理 .tmp 残留"""
    recovered = 0
    for root, _dirs, files in os.walk(BUFFER_DIR):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            if fname.endswith('.tmp'):
                os.remove(fpath)
                print(f"  清理残留 tmp: {fpath}")
            elif fname.endswith('.jsonl.gz'):
                try:
                    buffer_queue.put_nowait(fpath)
                    recovered += 1
                except queue.Full:
                    print(f"  队列已满，跳过: {fpath}")
                    break
    if recovered:
        print(f"恢复 {recovered} 个缓冲文件")


# ==================== Import Worker ====================
def _do_bulk_insert(file_paths, con):
    """
    直接让 DuckDB read_json_auto 读取 gzip JSONL 缓冲文件批量导入。
    用 TEMP TABLE 单次读取，INSERT 和 GROUP BY 共享同一份数据，避免二次磁盘 IO。
    """
    valid_files = [fp for fp in file_paths if os.path.exists(fp)]
    if not valid_files:
        return

    file_list_sql = "[" + ", ".join(f"'{fp}'" for fp in valid_files) + "]"

    # 单次读入临时表，后续 INSERT 和 GROUP BY 均从内存中读取
    con.execute(f"CREATE TEMP TABLE _batch AS SELECT * FROM read_json_auto({file_list_sql})")
    try:
        base_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM log_entries").fetchone()[0]

        con.execute(f"""
            INSERT INTO log_entries
                (id, server_id, hostname, stream, plsn, xid, log_type,
                 page_id, page_prev_glsn, message, created_at)
            SELECT
                {base_id} + row_number() OVER () AS id,
                server_id, hostname, stream, plsn, xid,
                type AS log_type,
                page_id,
                TRY_CAST(page_prev_glsn AS BIGINT) AS page_prev_glsn,
                message,
                now() AS created_at
            FROM _batch
        """)

        # GROUP BY 复用已在内存的临时表，无额外磁盘读
        row_counts = con.execute("""
            SELECT server_id, hostname, COUNT(*) as cnt
            FROM _batch
            GROUP BY server_id, hostname
        """).fetchall()
    finally:
        con.execute("DROP TABLE IF EXISTS _batch")

    total_rows = sum(r[2] for r in row_counts)

    con.execute("BEGIN")
    try:
        for sid, hostname, cnt in row_counts:
            con.execute("""
                INSERT INTO servers (server_id, hostname, last_seen, log_count)
                VALUES (?, ?, now(), ?)
                ON CONFLICT (server_id) DO UPDATE SET
                    last_seen = now(),
                    log_count = servers.log_count + excluded.log_count
            """, [sid, hostname, cnt])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    for fp in valid_files:
        try:
            os.remove(fp)
        except OSError:
            pass

    with _import_stats_lock:
        _import_stats['imported'] += total_rows
        _import_stats['files'] += len(valid_files)
        _import_stats['last_import'] = datetime.now().isoformat()

    print(f"  导入 {total_rows:,} 条 from {len(valid_files)} 文件")


def import_worker_loop():
    """Import Worker 主循环：积累文件 → 触发条件满足 → 批量导入"""
    con = duckdb.connect(DB_PATH)  # 独占写连接，整个生命周期持有
    pending_files = []
    last_flush = time.time()

    print("  Import Worker 启动")

    while True:
        try:
            timeout = max(0.1, IMPORT_BATCH_TIMEOUT - (time.time() - last_flush))
            try:
                fp = buffer_queue.get(timeout=timeout)
                pending_files.append(fp)
            except queue.Empty:
                pass

            # 触发条件：文件数 >= 4（约 200000 行）OR 超时且有待处理文件
            elapsed = time.time() - last_flush
            should_flush = (
                len(pending_files) >= 4 or
                (pending_files and elapsed >= IMPORT_BATCH_TIMEOUT)
            )

            if should_flush and pending_files:
                files_to_import = pending_files[:]
                pending_files = []
                try:
                    _do_bulk_insert(files_to_import, con)
                except Exception as e:
                    print(f"  导入失败: {e}，文件重新入队")
                    for fp in files_to_import:
                        try:
                            buffer_queue.put_nowait(fp)
                        except queue.Full:
                            print(f"  队列满，文件保留在磁盘: {fp}")
                    time.sleep(5)
                finally:
                    last_flush = time.time()  # 无论成败，插入完成后才重置计时器

        except Exception as e:
            print(f"  Import Worker 异常: {e}")
            time.sleep(1)


def start_import_worker():
    """初始化 buffer_queue，启动 Import Worker 线程"""
    global buffer_queue
    buffer_queue = queue.Queue(maxsize=QUEUE_MAX_SIZE)
    t = threading.Thread(target=import_worker_loop, daemon=True, name='ImportWorker')
    t.start()
    print("Import Worker 启动")


def parse_wal(line):
    """解析 WAL 日志行（服务端兼容旧格式上传）"""
    m = WAL_PATTERN.search(line)
    if m:
        return {
            'stream': m.group('stream'),
            'plsn': m.group('plsn'),
            'xid': f"({m.group('xid_h')},{m.group('xid_l')})",
            'type': m.group('type'),
            'page_id': f"({m.group('pg1')},{m.group('pg2')})",
            'page_prev_glsn': int(m.group('prev_glsn')),
            'message': line,
        }
    return {
        'stream': None, 'plsn': None, 'xid': None, 'type': None,
        'page_id': None, 'page_prev_glsn': None, 'message': line,
    }


# ==================== HTTP 处理器 ====================
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{datetime.now():%H:%M:%S}] {args[0]}")

    def _send(self, data, status=200, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(data.encode('utf-8'))

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        routes = {
            '/': self._dashboard,
            '/dashboard': self._dashboard,
            '/api/stats': lambda: self._stats(q),
            '/api/logs': lambda: self._logs(q),
            '/api/logs/grouped': lambda: self._logs_grouped(q),
            '/api/logs/group-detail': lambda: self._logs_in_group(q),
            '/api/wal-types': lambda: self._wal_types(q),
            '/api/servers': self._servers,
            '/api/buffer_status': self._buffer_status,
            '/api/analysis/hot-pages': lambda: self._analysis_hot_pages(q),
            '/api/analysis/tx-integrity': lambda: self._analysis_tx_integrity(q),
        }
        handler = routes.get(p.path)
        if handler:
            handler()
        else:
            self._send({'error': 'Not Found'}, 404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        if self.headers.get('Content-Encoding') == 'gzip':
            body = gzip.decompress(body)
        try:
            data = json.loads(body.decode('utf-8')) if body else {}
        except json.JSONDecodeError:
            return self._send({'error': 'Invalid JSON'}, 400)

        routes = {
            '/api/upload': lambda: self._upload(data),
            '/api/register': lambda: self._register(data),
        }
        handler = routes.get(urlparse(self.path).path)
        if handler:
            handler()
        else:
            self._send({'error': 'Not Found'}, 404)

    # ==================== API 实现 ====================

    def _register(self, data):
        hostname = data.get('hostname')
        if not hostname:
            return self._send({'error': 'Hostname required'}, 400)
        # server_id 是确定性哈希，无需写 DB（首次上传时自动创建 servers 表记录）
        sid = hashlib.sha256(f"{hostname}-{data.get('ip','')}".encode()).hexdigest()[:16]
        self._send({'success': True, 'server_id': sid})

    def _upload(self, data):
        server_id = data.get('server_id', 'unknown')
        hostname = data.get('hostname', 'unknown')

        records = data.get('records')
        if records is None:
            # 兼容旧格式：原始日志行列表
            logs = data.get('logs', [])
            if not logs:
                return self._send({'error': 'No logs'}, 400)
            records = []
            for line in logs:
                if not line.strip():
                    continue
                p = parse_wal(line)
                p['message'] = line
                records.append(p)

        if not records:
            return self._send({'error': 'No valid logs'}, 400)

        try:
            fp = write_buffer_file(server_id, hostname, records)
        except Exception as e:
            print(f"  写缓冲文件失败: {e}")
            return self._send({'error': 'Buffer write failed'}, 500)

        try:
            buffer_queue.put_nowait(fp)
        except queue.Full:
            # 队列满时文件已安全落盘，scan_buffer_on_startup 重启后自动恢复。
            # 直接 ACK 而非 429，避免客户端重试风暴加剧服务端压力。
            pass

        self._send({'success': True, 'buffered': len(records)})

    def _buffer_status(self):
        disk_files = sum(
            1 for root, _dirs, files in os.walk(BUFFER_DIR)
            for f in files if f.endswith('.jsonl.gz')
        )
        with _import_stats_lock:
            stats = dict(_import_stats)
        self._send({
            'queue_depth': buffer_queue.qsize() if buffer_queue else 0,
            'disk_files': disk_files,
            'imported_rows': stats['imported'],
            'imported_files': stats['files'],
            'last_import': stats['last_import'],
        })

    def _stats(self, q):
        server_id = q.get('server_id', [None])[0]

        base_conds = []
        base_params = []
        if server_id:
            base_conds.append('server_id = ?')
            base_params.append(server_id)
        base_where = ('WHERE ' + ' AND '.join(base_conds)) if base_conds else ''

        con = get_read_con()
        try:
            res = con.execute(
                f"SELECT COUNT(*) as total, COUNT(DISTINCT log_type) as types FROM log_entries {base_where}",
                base_params)
            ov = rows_to_dicts(res)[0]

            xid_conds = base_conds + ['xid IS NOT NULL']
            xid_where = 'WHERE ' + ' AND '.join(xid_conds)
            res = con.execute(
                f"SELECT xid, COUNT(*) as c FROM log_entries {xid_where} GROUP BY xid ORDER BY c DESC LIMIT 20",
                base_params)
            by_xid = {r['xid']: r['c'] for r in rows_to_dicts(res)}

            pg_conds = base_conds + ['page_id IS NOT NULL']
            pg_where = 'WHERE ' + ' AND '.join(pg_conds)
            res = con.execute(
                f"SELECT page_id, COUNT(*) as c FROM log_entries {pg_where} GROUP BY page_id ORDER BY c DESC LIMIT 20",
                base_params)
            by_page = {r['page_id']: r['c'] for r in rows_to_dicts(res)}
        finally:
            con.close()

        self._send({
            'overview': {'total_logs': ov['total'], 'wal_types': ov['types']},
            'by_xid': by_xid,
            'by_page_id': by_page,
        })

    def _logs(self, q):
        server_id = q.get('server_id', [None])[0]
        xid      = q.get('xid', [None])[0]
        page_id  = q.get('page_id', [None])[0]
        plsn     = q.get('plsn', [None])[0]
        wal_type = q.get('type', [None])[0]
        stream   = q.get('stream', [None])[0]
        limit    = min(int(q.get('limit', ['100'])[0]), 1000)
        offset   = int(q.get('offset', ['0'])[0])

        conds, vals = ['1=1'], []
        if server_id: conds.append('server_id = ?'); vals.append(server_id)
        if xid:       conds.append('xid = ?');       vals.append(xid)
        if page_id:   conds.append('page_id = ?');   vals.append(page_id)
        if plsn:      conds.append('plsn = ?');      vals.append(plsn)
        if wal_type:  conds.append('log_type = ?');  vals.append(wal_type)
        if stream:    conds.append('stream = ?');    vals.append(stream)
        where = ' AND '.join(conds)

        con = get_read_con()
        try:
            res = con.execute(
                f"SELECT * FROM log_entries WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                vals + [limit, offset])
            logs = rows_to_dicts(res)
            res = con.execute(f"SELECT COUNT(*) as c FROM log_entries WHERE {where}", vals)
            total = rows_to_dicts(res)[0]['c']
        finally:
            con.close()

        self._send({'logs': logs, 'total': total, 'limit': limit, 'offset': offset})

    def _logs_grouped(self, q):
        server_id = q.get('server_id', [None])[0]
        group_by  = q.get('group_by', ['xid'])[0]
        wal_type  = q.get('type', [None])[0]
        limit     = min(int(q.get('limit', ['20'])[0]), 200)
        offset    = int(q.get('offset', ['0'])[0])

        field_map = {'xid': 'xid', 'pageid': 'page_id', 'plsn': 'plsn',
                     'type': 'log_type', 'stream': 'stream'}
        field = field_map.get(group_by, 'xid')

        conds, vals = [f'{field} IS NOT NULL'], []
        if server_id: conds.append('server_id = ?'); vals.append(server_id)
        if wal_type:  conds.append('log_type = ?');  vals.append(wal_type)
        where = ' AND '.join(conds)

        con = get_read_con()
        try:
            res = con.execute(
                f"SELECT COUNT(DISTINCT {field}) as c FROM log_entries WHERE {where}", vals)
            total_groups = rows_to_dicts(res)[0]['c']

            res = con.execute(
                f"SELECT {field} as key, COUNT(*) as count FROM log_entries WHERE {where} "
                f"GROUP BY {field} ORDER BY count DESC LIMIT ? OFFSET ?",
                vals + [limit, offset])
            groups = [{'key': r['key'], 'count': r['count']} for r in rows_to_dicts(res)]
        finally:
            con.close()

        self._send({'groups': groups, 'group_by': group_by,
                    'total_groups': total_groups, 'limit': limit, 'offset': offset})

    def _logs_in_group(self, q):
        server_id = q.get('server_id', [None])[0]
        group_by  = q.get('group_by', ['xid'])[0]
        group_key = q.get('key', [None])[0]
        wal_type  = q.get('type', [None])[0]
        limit     = min(int(q.get('limit', ['50'])[0]), 500)
        offset    = int(q.get('offset', ['0'])[0])

        field_map = {'xid': 'xid', 'pageid': 'page_id', 'plsn': 'plsn',
                     'type': 'log_type', 'stream': 'stream'}
        field = field_map.get(group_by, 'xid')

        conds, vals = [f'{field} = ?'], [group_key]
        if server_id: conds.append('server_id = ?'); vals.append(server_id)
        if wal_type:  conds.append('log_type = ?');  vals.append(wal_type)
        where = ' AND '.join(conds)

        if group_by == 'pageid':
            order_by = "page_prev_glsn ASC NULLS LAST, CAST(plsn AS BIGINT) ASC"
        elif group_by in ('stream', 'xid'):
            order_by = "CAST(plsn AS BIGINT) ASC"
        else:
            order_by = "created_at DESC"

        con = get_read_con()
        try:
            res = con.execute(
                f"SELECT * FROM log_entries WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                vals + [limit, offset])
            items = rows_to_dicts(res)
            res = con.execute(f"SELECT COUNT(*) as c FROM log_entries WHERE {where}", vals)
            total = rows_to_dicts(res)[0]['c']
        finally:
            con.close()

        self._send({'items': items, 'total': total, 'limit': limit, 'offset': offset})

    def _wal_types(self, q):
        server_id = q.get('server_id', [None])[0]
        con = get_read_con()
        try:
            if server_id:
                res = con.execute(
                    "SELECT DISTINCT log_type FROM log_entries "
                    "WHERE server_id = ? AND log_type IS NOT NULL ORDER BY log_type",
                    [server_id])
            else:
                res = con.execute(
                    "SELECT DISTINCT log_type FROM log_entries WHERE log_type IS NOT NULL ORDER BY log_type")
            types = [r[0] for r in res.fetchall()]
        finally:
            con.close()
        self._send({'types': types})

    def _servers(self):
        con = get_read_con()
        try:
            res = con.execute("SELECT * FROM servers ORDER BY last_seen DESC")
            rows = rows_to_dicts(res)
            servers = [{'id': r['server_id'], 'hostname': r['hostname'], 'ip': r['ip'],
                        'total_logs': r['log_count'], 'last_seen': r['last_seen']}
                       for r in rows]
        finally:
            con.close()
        self._send({'servers': servers})

    def _analysis_hot_pages(self, q):
        server_id  = q.get('server_id', [None])[0]
        time_range = int(q.get('time_range', ['60'])[0])
        limit      = int(q.get('limit', ['50'])[0])
        if limit <= 0:
            limit = None
        elif limit > 5000:
            limit = 5000

        conds, vals = [], []
        if server_id:
            conds.append('server_id = ?')
            vals.append(server_id)
        if time_range > 0:
            # time_range is already int, safe to embed as literal
            conds.append(f"created_at > NOW() - INTERVAL {time_range} MINUTE")
        where = ' AND '.join(conds) if conds else '1=1'
        limit_clause = '' if limit is None else f'LIMIT {limit}'

        con = get_read_con()
        try:
            res = con.execute(f"""
                SELECT
                    page_id,
                    COUNT(*) as modify_count,
                    COUNT(DISTINCT xid) as tx_count,
                    array_agg(DISTINCT log_type) as op_types,
                    MIN(created_at) as first_seen,
                    MAX(created_at) as last_seen,
                    COUNT(DISTINCT server_id) as server_count
                FROM log_entries
                WHERE {where} AND page_id IS NOT NULL
                GROUP BY page_id
                ORDER BY modify_count DESC
                {limit_clause}
            """, vals)
            hot_pages = []
            for r in rows_to_dicts(res):
                op = r['op_types']
                hot_pages.append({
                    'page_id': r['page_id'],
                    'modify_count': r['modify_count'],
                    'tx_count': r['tx_count'],
                    'op_types': list(op) if op else [],
                    'first_seen': str(r['first_seen']),
                    'last_seen': str(r['last_seen']),
                    'server_count': r['server_count'],
                })
        finally:
            con.close()

        self._send({'hot_pages': hot_pages, 'time_range_minutes': time_range, 'total': len(hot_pages)})

    def _analysis_tx_integrity(self, q):
        server_id     = q.get('server_id', [None])[0]
        min_wal_count = int(q.get('min_wal_count', ['10'])[0])
        limit         = min(int(q.get('limit', ['100'])[0]), 500)

        base_conds = []
        base_vals  = []
        if server_id:
            base_conds.append('server_id = ?')
            base_vals.append(server_id)
        base_where = ('AND ' + ' AND '.join(base_conds)) if base_conds else ''

        con = get_read_con()
        try:
            # 孤儿事务（无 COMMIT/ABORT 记录）
            res = con.execute(f"""
                WITH tx_stats AS (
                    SELECT
                        xid,
                        COUNT(*) as wal_count,
                        MIN(created_at) as start_time,
                        MAX(created_at) as end_time,
                        bool_or(log_type LIKE '%COMMIT%' OR log_type LIKE '%ABORT%') as has_commit,
                        array_agg(DISTINCT log_type) as op_types,
                        COUNT(DISTINCT page_id) as page_count,
                        server_id,
                        hostname
                    FROM log_entries
                    WHERE xid IS NOT NULL {base_where}
                    GROUP BY xid, server_id, hostname
                    HAVING COUNT(*) >= ?
                )
                SELECT * FROM tx_stats
                WHERE has_commit = FALSE
                ORDER BY wal_count DESC
                LIMIT ?
            """, base_vals + [min_wal_count, limit])
            orphan_txs = []
            for r in rows_to_dicts(res):
                op = r['op_types']
                orphan_txs.append({
                    'xid': r['xid'],
                    'wal_count': r['wal_count'],
                    'start_time': str(r['start_time']),
                    'end_time': str(r['end_time']),
                    'op_types': list(op) if op else [],
                    'page_count': r['page_count'],
                    'server_id': r['server_id'],
                    'hostname': r['hostname'],
                })

            # 高 WAL 事务（记录数异常多）
            res = con.execute(f"""
                WITH tx_stats AS (
                    SELECT
                        xid,
                        COUNT(*) as wal_count,
                        MIN(created_at) as start_time,
                        MAX(created_at) as end_time,
                        array_agg(DISTINCT log_type) as op_types,
                        COUNT(DISTINCT page_id) as page_count,
                        server_id,
                        hostname
                    FROM log_entries
                    WHERE xid IS NOT NULL {base_where}
                    GROUP BY xid, server_id, hostname
                )
                SELECT * FROM tx_stats
                ORDER BY wal_count DESC
                LIMIT ?
            """, base_vals + [limit])
            high_wal_txs = []
            for r in rows_to_dicts(res):
                op = r['op_types']
                high_wal_txs.append({
                    'xid': r['xid'],
                    'wal_count': r['wal_count'],
                    'start_time': str(r['start_time']),
                    'end_time': str(r['end_time']),
                    'op_types': list(op) if op else [],
                    'page_count': r['page_count'],
                    'server_id': r['server_id'],
                    'hostname': r['hostname'],
                })
        finally:
            con.close()

        self._send({
            'orphan_transactions': orphan_txs,
            'high_wal_transactions': high_wal_txs,
            'total_orphan': len(orphan_txs),
            'total_high_wal': len(high_wal_txs),
        })

    def _dashboard(self):
        try:
            with open(os.path.join(os.path.dirname(__file__), 'dashboard.html'), 'r') as f:
                self._send(f.read(), content_type='text/html; charset=utf-8')
        except FileNotFoundError:
            self._send('<h1>Dashboard not found</h1>', 404, 'text/html')


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    print(f"WAL 日志分析平台（DuckDB 版）")
    print(f"数据库: {DB_PATH}")
    print(f"缓冲区: {BUFFER_DIR}")

    init_db()
    init_buffer_dir()
    start_import_worker()
    scan_buffer_on_startup()

    server = ThreadedServer((HOST, PORT), Handler)
    print(f"服务启动: http://localhost:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭服务")
        server.shutdown()


if __name__ == '__main__':
    main()
