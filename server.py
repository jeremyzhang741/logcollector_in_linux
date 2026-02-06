#!/usr/bin/env python3
"""WAL 日志收集分析平台 - 后端服务"""

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
from psycopg2.extras import RealDictCursor, execute_values
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

# WAL 日志解析正则
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


def init_db():
    """初始化数据库"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS log_entries (
                id SERIAL PRIMARY KEY,
                server_id VARCHAR(64) NOT NULL,
                hostname VARCHAR(255),
                log_type VARCHAR(50) DEFAULT 'wal',
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stream VARCHAR(32),
                plsn TEXT, xid TEXT, type VARCHAR(128), page_id TEXT,
                page_prev_glsn BIGINT,
                message TEXT
            )
        ''')
        # 添加新列（如果不存在）
        cur.execute('''
            DO $$ BEGIN
                ALTER TABLE log_entries ADD COLUMN IF NOT EXISTS stream VARCHAR(32);
                ALTER TABLE log_entries ADD COLUMN IF NOT EXISTS page_prev_glsn BIGINT;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$;
        ''')
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
        for idx in ['server_id', 'plsn', 'xid', 'type', 'page_id', 'received_at', 'stream', 'page_prev_glsn']:
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{idx} ON log_entries({idx})')
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
            '/api/wal-types': lambda: self._wal_types(q),
            '/api/servers': self._servers,
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
        logs = data.get('logs', [])
        if not logs:
            return self._send({'error': 'No logs'}, 400)

        rows = []
        for line in logs:
            if not line.strip():
                continue
            p = parse_wal(line)
            rows.append((server_id, hostname, 'wal', p['stream'], p['plsn'], p['xid'], p['type'], p['page_id'], p['page_prev_glsn'], p['message']))

        with get_db() as conn:
            cur = conn.cursor()
            execute_values(cur, '''
                INSERT INTO log_entries (server_id, hostname, log_type, stream, plsn, xid, type, page_id, page_prev_glsn, message)
                VALUES %s
            ''', rows, page_size=500)
            cur.execute('''
                INSERT INTO servers (id, hostname, last_seen, total_logs)
                VALUES (%s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    total_logs = servers.total_logs + EXCLUDED.total_logs
            ''', (server_id, hostname, len(rows)))
            cur.close()

        self._send({'success': True, 'inserted': len(rows)})

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

        field_map = {'xid': 'xid', 'pageid': 'page_id', 'plsn': 'plsn', 'type': 'type', 'stream': 'stream'}
        field = field_map.get(group_by, 'xid')

        conds, vals = [f'{field} IS NOT NULL'], []
        if server_id: conds.append('server_id = %s'); vals.append(server_id)
        if wal_type: conds.append('type = %s'); vals.append(wal_type)
        where = ' AND '.join(conds)

        # 按分组类型设置排序规则
        if group_by == 'pageid':
            order_by = "page_prev_glsn ASC NULLS LAST, CAST(plsn AS BIGINT) ASC"
        elif group_by in ('stream', 'xid'):
            order_by = "CAST(plsn AS BIGINT) ASC"
        else:
            order_by = "received_at DESC"

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(f"SELECT {field} as key, COUNT(*) as count FROM log_entries WHERE {where} GROUP BY {field} ORDER BY count DESC LIMIT 50", vals)
            groups = []
            for row in cur.fetchall():
                cur.execute(f"SELECT * FROM log_entries WHERE {where} AND {field} = %s ORDER BY {order_by} LIMIT 10",
                            vals + [row['key']])
                groups.append({'key': row['key'], 'count': row['count'], 'items': cur.fetchall()})
            cur.close()

        self._send({'groups': groups, 'group_by': group_by})

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
