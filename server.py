#!/usr/bin/env python3
"""WAL 日志收集分析平台 - 后端服务"""

import io
import json
import os
import re
import gzip
from datetime import datetime
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

# ==================== 配置 ====================
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'database': os.environ.get('DB_NAME', 'logvista'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'postgres'),
}
HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
PORT = int(os.environ.get('SERVER_PORT', 8080))

db_pool = None
_known_partitions = set()  # 内存缓存已知 server_id，避免每次查 pg_class


def _pg_escape(v):
    """PostgreSQL TEXT 格式转义：NULL 用 \\N，特殊字符转义"""
    if v is None:
        return r'\N'
    return str(v).replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')


# WAL 日志解析正则（保留用于兼容旧格式上传）
WAL_PATTERN = re.compile(
    r'dump_stream_(?P<stream>[\d_]+):.*?'
    r'plsn:(?P<plsn>\d+);.*?'
    r'xid:\s*\((?P<xid_h>\d+),\s*(?P<xid_l>\d+)\);.*?'
    r'type:(?P<type>\w+);.*?'
    r'pageId=\((?P<pg1>\d+),\s*(?P<pg2>\d+)\);.*?'
    r'page prev glsn:(?P<prev_glsn>\d+)',
    re.IGNORECASE
)


# ==================== 数据库工具 ====================
@contextmanager
def get_db():
    """数据库连接上下文管理器"""
    conn = db_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


def ensure_partition(cur, server_id):
    """为 server_id 创建分区（如果不存在），内存缓存避免重复查 pg_class"""
    if server_id in _known_partitions:
        return
    safe_name = server_id.replace('-', '_')
    partition_name = f'log_entries_{safe_name}'
    cur.execute("SELECT 1 FROM pg_class WHERE relname = %s", (partition_name,))
    if not cur.fetchone():
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {partition_name}
            PARTITION OF log_entries FOR VALUES IN (%s)
        ''', (server_id,))
        # 在分区上创建复合索引
        for idx_cols, idx_suffix in [
            ('server_id, received_at', 'rcv'),
            ('server_id, xid', 'xid'),
            ('server_id, page_id', 'pgid'),
            ('server_id, type', 'type'),
            ('server_id, stream', 'strm'),
            ('server_id, plsn', 'plsn'),
            ('page_id, page_prev_glsn', 'pg_glsn'),
        ]:
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{safe_name}_{idx_suffix} ON {partition_name}({idx_cols})')
        print(f"  📂 创建分区: {partition_name} (server_id={server_id})")
    _known_partitions.add(server_id)


def init_db():
    """初始化数据库 - 使用 LIST 分区表"""
    with get_db() as conn:
        cur = conn.cursor()

        # 检查 log_entries 是否已经是分区表
        cur.execute("""
            SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = 'log_entries' AND n.nspname = 'public'
        """)
        row = cur.fetchone()

        if row and row[0] == 'p':
            # 已经是分区表，跳过迁移
            print("  ✅ log_entries 已是分区表")
        elif row:
            # 存在旧的非分区表，执行迁移
            print("  🔄 迁移: 将 log_entries 转换为分区表...")
            cur.execute("ALTER TABLE log_entries RENAME TO log_entries_old")
            cur.execute("DROP INDEX IF EXISTS idx_server_id, idx_plsn, idx_xid, idx_type, idx_page_id, idx_received_at, idx_stream, idx_page_prev_glsn")
            # 创建分区主表
            cur.execute('''
                CREATE TABLE log_entries (
                    id SERIAL,
                    server_id VARCHAR(64) NOT NULL,
                    hostname VARCHAR(255),
                    log_type VARCHAR(50) DEFAULT 'wal',
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    stream VARCHAR(32),
                    plsn TEXT, xid TEXT, type VARCHAR(128), page_id TEXT,
                    page_prev_glsn BIGINT,
                    message TEXT
                ) PARTITION BY LIST (server_id)
            ''')
            # 为已有的 server_id 创建分区并迁移数据
            cur.execute("SELECT DISTINCT server_id FROM log_entries_old WHERE server_id IS NOT NULL")
            existing_ids = [r[0] for r in cur.fetchall()]
            for sid in existing_ids:
                ensure_partition(cur, sid)
            # 迁移数据
            cur.execute('''
                INSERT INTO log_entries (id, server_id, hostname, log_type, received_at, stream, plsn, xid, type, page_id, page_prev_glsn, message)
                SELECT id, server_id, hostname, log_type, received_at, stream, plsn, xid, type, page_id, page_prev_glsn, message
                FROM log_entries_old
            ''')
            cur.execute("SELECT setval('log_entries_id_seq', (SELECT COALESCE(MAX(id), 0) FROM log_entries))")
            # 创建默认分区
            cur.execute('''
                CREATE TABLE IF NOT EXISTS log_entries_default
                PARTITION OF log_entries DEFAULT
            ''')
            cur.execute("DROP TABLE log_entries_old")
            print(f"  ✅ 迁移完成: {len(existing_ids)} 个分区")
        else:
            # 全新安装，直接创建分区表
            cur.execute('''
                CREATE TABLE log_entries (
                    id SERIAL,
                    server_id VARCHAR(64) NOT NULL,
                    hostname VARCHAR(255),
                    log_type VARCHAR(50) DEFAULT 'wal',
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    stream VARCHAR(32),
                    plsn TEXT, xid TEXT, type VARCHAR(128), page_id TEXT,
                    page_prev_glsn BIGINT,
                    message TEXT
                ) PARTITION BY LIST (server_id)
            ''')
            # 创建默认分区，接收未知 server_id
            cur.execute('''
                CREATE TABLE IF NOT EXISTS log_entries_default
                PARTITION OF log_entries DEFAULT
            ''')
            print("  ✅ 创建分区表: log_entries (LIST by server_id)")

        # servers 表不变
        cur.execute('''
            CREATE TABLE IF NOT EXISTS servers (
                id VARCHAR(64) PRIMARY KEY,
                hostname VARCHAR(255),
                ip_address VARCHAR(45),
                os_info TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_logs BIGINT DEFAULT 0
            )
        ''')

        # 预热分区缓存
        cur.execute("SELECT id FROM servers")
        for row in cur.fetchall():
            _known_partitions.add(row[0])

        # 打印已有分区
        cur.execute("""
            SELECT inhrelid::regclass::text FROM pg_inherits
            WHERE inhparent = 'log_entries'::regclass
        """)
        partitions = [r[0] for r in cur.fetchall()]
        if partitions:
            print(f"  📊 已有分区: {', '.join(partitions)}")

        cur.close()
    print("✅ 数据库初始化完成")


def parse_wal(line):
    """解析 WAL 日志行"""
    m = WAL_PATTERN.search(line)
    if m:
        return {
            'stream': m.group('stream'),
            'plsn': m.group('plsn'),
            'xid': f"({m.group('xid_h')},{m.group('xid_l')})",
            'type': m.group('type'),
            'page_id': f"({m.group('pg1')},{m.group('pg2')})",
            'page_prev_glsn': int(m.group('prev_glsn')),
            'message': line
        }
    return {'stream': None, 'plsn': None, 'xid': None, 'type': None, 'page_id': None, 'page_prev_glsn': None, 'message': line}


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
        if isinstance(data, dict):
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

        import hashlib
        sid = hashlib.sha256(f"{hostname}-{data.get('ip','')}".encode()).hexdigest()[:16]

        with get_db() as conn:
            cur = conn.cursor()
            # 注册时自动创建分区
            ensure_partition(cur, sid)
            cur.execute('''
                INSERT INTO servers (id, hostname, ip_address, os_info)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    hostname = EXCLUDED.hostname, last_seen = CURRENT_TIMESTAMP
            ''', (sid, hostname, data.get('ip'), data.get('os_info')))
            cur.close()

        self._send({'success': True, 'server_id': sid})

    def _upload(self, data):
        server_id = data.get('server_id', 'unknown')
        hostname = data.get('hostname', 'unknown')

        # 新格式：客户端预解析的结构化字段列表
        # 旧格式（兼容）：原始日志行列表
        records = data.get('records')
        if records is None:
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

        # 构建 COPY 数据（PostgreSQL TEXT 格式，tab 分隔，\N 表示 NULL）
        buf = io.StringIO()
        for r in records:
            cols = [
                server_id, hostname, 'wal',
                r.get('stream'), r.get('plsn'), r.get('xid'),
                r.get('type'), r.get('page_id'), r.get('page_prev_glsn'),
                r.get('message', ''),
            ]
            buf.write('\t'.join(_pg_escape(v) for v in cols) + '\n')
        buf.seek(0)

        with get_db() as conn:
            cur = conn.cursor()
            ensure_partition(cur, server_id)
            cur.copy_expert(
                'COPY log_entries (server_id, hostname, log_type, stream, plsn, xid, type, page_id, page_prev_glsn, message) FROM STDIN',
                buf
            )
            cur.execute('''
                INSERT INTO servers (id, hostname, last_seen, total_logs)
                VALUES (%s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    total_logs = servers.total_logs + EXCLUDED.total_logs
            ''', (server_id, hostname, len(records)))
            cur.close()

        self._send({'success': True, 'inserted': len(records)})

    def _stats(self, q):
        server_id = q.get('server_id', [None])[0]
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            where = "WHERE server_id = %s" if server_id else ""
            params = (server_id,) if server_id else ()

            cur.execute(f"SELECT COUNT(*) as total, COUNT(DISTINCT type) as types FROM log_entries {where}", params)
            ov = cur.fetchone()

            cur.execute(f"SELECT xid, COUNT(*) as c FROM log_entries {where} AND xid IS NOT NULL GROUP BY xid ORDER BY c DESC LIMIT 20".replace("AND", "WHERE" if not server_id else "AND"), params)
            by_xid = {r['xid']: r['c'] for r in cur.fetchall()}

            cur.execute(f"SELECT page_id, COUNT(*) as c FROM log_entries {where} AND page_id IS NOT NULL GROUP BY page_id ORDER BY c DESC LIMIT 20".replace("AND", "WHERE" if not server_id else "AND"), params)
            by_page = {r['page_id']: r['c'] for r in cur.fetchall()}

            cur.close()

        self._send({
            'overview': {'total_logs': ov['total'], 'wal_types': ov['types']},
            'by_xid': by_xid,
            'by_page_id': by_page
        })

    def _logs(self, q):
        server_id = q.get('server_id', [None])[0]
        xid = q.get('xid', [None])[0]
        page_id = q.get('page_id', [None])[0]
        plsn = q.get('plsn', [None])[0]
        wal_type = q.get('type', [None])[0]
        stream = q.get('stream', [None])[0]
        limit = min(int(q.get('limit', ['100'])[0]), 1000)
        offset = int(q.get('offset', ['0'])[0])

        conds, vals = ['1=1'], []
        if server_id: conds.append('server_id = %s'); vals.append(server_id)
        if xid: conds.append('xid = %s'); vals.append(xid)
        if page_id: conds.append('page_id = %s'); vals.append(page_id)
        if plsn: conds.append('plsn = %s'); vals.append(plsn)
        if wal_type: conds.append('type = %s'); vals.append(wal_type)
        if stream: conds.append('stream = %s'); vals.append(stream)

        where = ' AND '.join(conds)

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f"SELECT * FROM log_entries WHERE {where} ORDER BY received_at DESC LIMIT %s OFFSET %s",
                        vals + [limit, offset])
            logs = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) as c FROM log_entries WHERE {where}", vals)
            total = cur.fetchone()['c']
            cur.close()

        self._send({'logs': logs, 'total': total, 'limit': limit, 'offset': offset})

    def _logs_grouped(self, q):
        server_id = q.get('server_id', [None])[0]
        group_by = q.get('group_by', ['xid'])[0]
        wal_type = q.get('type', [None])[0]
        limit = min(int(q.get('limit', ['20'])[0]), 200)
        offset = int(q.get('offset', ['0'])[0])

        field_map = {'xid': 'xid', 'pageid': 'page_id', 'plsn': 'plsn', 'type': 'type', 'stream': 'stream'}
        field = field_map.get(group_by, 'xid')

        conds, vals = [f'{field} IS NOT NULL'], []
        if server_id: conds.append('server_id = %s'); vals.append(server_id)
        if wal_type: conds.append('type = %s'); vals.append(wal_type)
        where = ' AND '.join(conds)

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f"SELECT COUNT(DISTINCT {field}) as c FROM log_entries WHERE {where}", vals)
            total_groups = cur.fetchone()['c']
            cur.execute(f"SELECT {field} as key, COUNT(*) as count FROM log_entries WHERE {where} GROUP BY {field} ORDER BY count DESC LIMIT %s OFFSET %s",
                        vals + [limit, offset])
            groups = [{'key': row['key'], 'count': row['count']} for row in cur.fetchall()]
            cur.close()

        self._send({'groups': groups, 'group_by': group_by, 'total_groups': total_groups, 'limit': limit, 'offset': offset})

    def _logs_in_group(self, q):
        """获取某个分组内的全部记录（带分页）"""
        server_id = q.get('server_id', [None])[0]
        group_by = q.get('group_by', ['xid'])[0]
        group_key = q.get('key', [None])[0]
        wal_type = q.get('type', [None])[0]
        limit = min(int(q.get('limit', ['50'])[0]), 500)
        offset = int(q.get('offset', ['0'])[0])

        field_map = {'xid': 'xid', 'pageid': 'page_id', 'plsn': 'plsn', 'type': 'type', 'stream': 'stream'}
        field = field_map.get(group_by, 'xid')

        conds, vals = [f'{field} = %s'], [group_key]
        if server_id: conds.append('server_id = %s'); vals.append(server_id)
        if wal_type: conds.append('type = %s'); vals.append(wal_type)
        where = ' AND '.join(conds)

        if group_by == 'pageid':
            order_by = "page_prev_glsn ASC NULLS LAST, CAST(plsn AS BIGINT) ASC"
        elif group_by in ('stream', 'xid'):
            order_by = "CAST(plsn AS BIGINT) ASC"
        else:
            order_by = "received_at DESC"

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f"SELECT * FROM log_entries WHERE {where} ORDER BY {order_by} LIMIT %s OFFSET %s",
                        vals + [limit, offset])
            items = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) as c FROM log_entries WHERE {where}", vals)
            total = cur.fetchone()['c']
            cur.close()

        self._send({'items': items, 'total': total, 'limit': limit, 'offset': offset})

    def _wal_types(self, q):
        server_id = q.get('server_id', [None])[0]
        with get_db() as conn:
            cur = conn.cursor()
            if server_id:
                cur.execute("SELECT DISTINCT type FROM log_entries WHERE server_id = %s AND type IS NOT NULL ORDER BY type", (server_id,))
            else:
                cur.execute("SELECT DISTINCT type FROM log_entries WHERE type IS NOT NULL ORDER BY type")
            types = [r[0] for r in cur.fetchall()]
            cur.close()
        self._send({'types': types})

    def _servers(self):
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM servers ORDER BY last_seen DESC")
            servers = [{'id': r['id'], 'hostname': r['hostname'], 'ip': r['ip_address'],
                        'total_logs': r['total_logs'], 'last_seen': r['last_seen']} for r in cur.fetchall()]
            cur.close()
        self._send({'servers': servers})

    def _analysis_hot_pages(self, q):
        """热点页面检测 - 分析最频繁修改的页面"""
        server_id = q.get('server_id', [None])[0]
        time_range = int(q.get('time_range', ['60'])[0])  # 分钟，0 表示全部时间
        limit = min(int(q.get('limit', ['50'])[0]), 200)

        conds, vals = [], []
        if server_id:
            conds.append('server_id = %s')
            vals.append(server_id)
        if time_range > 0:
            conds.append("received_at > NOW() - INTERVAL '%s minutes'")
            vals.append(time_range)
        where = ' AND '.join(conds) if conds else '1=1'

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 热点页面统计
            cur.execute(f'''
                SELECT
                    page_id,
                    COUNT(*) as modify_count,
                    COUNT(DISTINCT xid) as tx_count,
                    ARRAY_AGG(DISTINCT type ORDER BY type) as op_types,
                    MIN(received_at) as first_seen,
                    MAX(received_at) as last_seen,
                    COUNT(DISTINCT server_id) as server_count
                FROM log_entries
                WHERE {where} AND page_id IS NOT NULL
                GROUP BY page_id
                ORDER BY modify_count DESC
                LIMIT %s
            ''', vals + [limit])
            hot_pages = []
            for r in cur.fetchall():
                hot_pages.append({
                    'page_id': r['page_id'],
                    'modify_count': r['modify_count'],
                    'tx_count': r['tx_count'],
                    'op_types': r['op_types'] or [],
                    'first_seen': str(r['first_seen']),
                    'last_seen': str(r['last_seen']),
                    'server_count': r['server_count'],
                })
            cur.close()

        self._send({
            'hot_pages': hot_pages,
            'time_range_minutes': time_range,
            'total': len(hot_pages)
        })

    def _analysis_tx_integrity(self, q):
        """事务完整性分析 - 检测孤儿事务、长事务、异常事务"""
        server_id = q.get('server_id', [None])[0]
        min_wal_count = int(q.get('min_wal_count', ['10'])[0])
        limit = min(int(q.get('limit', ['100'])[0]), 500)

        where = ''
        base_vals = []
        if server_id:
            where = 'AND server_id = %s'
            base_vals = [server_id]

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)

            # 1. 孤儿事务（无 COMMIT/ABORT 记录的事务）
            cur.execute(f'''
                WITH tx_stats AS (
                    SELECT
                        xid,
                        COUNT(*) as wal_count,
                        MIN(received_at) as start_time,
                        MAX(received_at) as end_time,
                        BOOL_OR(type LIKE '%%COMMIT%%' OR type LIKE '%%ABORT%%') as has_commit,
                        ARRAY_AGG(DISTINCT type ORDER BY type) as op_types,
                        COUNT(DISTINCT page_id) as page_count,
                        server_id,
                        hostname
                    FROM log_entries
                    WHERE xid IS NOT NULL {where}
                    GROUP BY xid, server_id, hostname
                    HAVING COUNT(*) >= %s
                )
                SELECT * FROM tx_stats
                WHERE has_commit = FALSE
                ORDER BY wal_count DESC
                LIMIT %s
            ''', base_vals + [min_wal_count, limit])
            orphan_txs = []
            for r in cur.fetchall():
                orphan_txs.append({
                    'xid': r['xid'],
                    'wal_count': r['wal_count'],
                    'start_time': str(r['start_time']),
                    'end_time': str(r['end_time']),
                    'op_types': r['op_types'] or [],
                    'page_count': r['page_count'],
                    'server_id': r['server_id'],
                    'hostname': r['hostname'],
                })

            # 2. 高WAL事务（WAL记录数异常多）
            cur.execute(f'''
                WITH tx_stats AS (
                    SELECT
                        xid,
                        COUNT(*) as wal_count,
                        MIN(received_at) as start_time,
                        MAX(received_at) as end_time,
                        ARRAY_AGG(DISTINCT type ORDER BY type) as op_types,
                        COUNT(DISTINCT page_id) as page_count,
                        server_id,
                        hostname
                    FROM log_entries
                    WHERE xid IS NOT NULL {where}
                    GROUP BY xid, server_id, hostname
                )
                SELECT * FROM tx_stats
                ORDER BY wal_count DESC
                LIMIT %s
            ''', base_vals + [limit])
            high_wal_txs = []
            for r in cur.fetchall():
                high_wal_txs.append({
                    'xid': r['xid'],
                    'wal_count': r['wal_count'],
                    'start_time': str(r['start_time']),
                    'end_time': str(r['end_time']),
                    'op_types': r['op_types'] or [],
                    'page_count': r['page_count'],
                    'server_id': r['server_id'],
                    'hostname': r['hostname'],
                })

            cur.close()

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
    global db_pool
    print(f"🚀 WAL 日志分析平台")
    print(f"📦 数据库: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")

    try:
        db_pool = pool.ThreadedConnectionPool(2, 20, **DB_CONFIG)
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        return

    init_db()
    server = ThreadedServer((HOST, PORT), Handler)
    print(f"✅ 服务启动: http://localhost:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 关闭服务")
        server.shutdown()
        db_pool.closeall()


if __name__ == '__main__':
    main()
