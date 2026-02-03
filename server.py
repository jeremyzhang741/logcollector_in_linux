#!/usr/bin/env python3
"""
WAL 日志收集与分析平台 - 后端服务
支持大规模节点的高效上传和存储
"""

import json
import os
import re
import gzip
import io
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2 import pool

# ==================== 配置 ====================
# PostgreSQL连接配置
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'database': os.environ.get('DB_NAME', 'logvista'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'postgres'),
}

# 服务器配置
HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
PORT = int(os.environ.get('SERVER_PORT', 8080))

# 连接池配置（支持大规模并发）
POOL_MIN_CONN = int(os.environ.get('POOL_MIN_CONN', 5))
POOL_MAX_CONN = int(os.environ.get('POOL_MAX_CONN', 50))

# 连接池
db_pool = None

# ==================== WAL 日志解析 ====================
# WAL 日志格式解析
# 格式: dump_stream_9_0_0:Wal record @ record end plsn:123; xid: (412, 12321);type:WAL_HEAP_INPLACE_UPDATE; len:122; ...
WAL_LOG_PATTERN = re.compile(
    r'^(?P<stream>\w+):Wal record @ record end plsn:(?P<plsn>\d+);\s*'
    r'xid:\s*\((?P<xid_high>\d+),\s*(?P<xid_low>\d+)\);\s*'
    r'type:(?P<wal_type>\w+);\s*'
    r'len:(?P<len>\d+);\s*'
    r'pageId=\((?P<page_id_1>\d+),(?P<page_id_2>\d+)\);\s*'
    r'(?P<extra>.*)',
    re.IGNORECASE
)

# 简化的 WAL 模式（更宽松的匹配）
WAL_SIMPLE_PATTERN = re.compile(
    r'^(?P<stream>\w+):.*type:(?P<wal_type>\w+);.*',
    re.IGNORECASE
)


def get_db_connection():
    """从连接池获取数据库连接"""
    return db_pool.getconn()


def release_db_connection(conn):
    """释放数据库连接回连接池"""
    db_pool.putconn(conn)


def init_database():
    """初始化PostgreSQL数据库表"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # WAL 日志条目表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS log_entries (
                id SERIAL PRIMARY KEY,
                server_id VARCHAR(64) NOT NULL,
                hostname VARCHAR(255),
                log_type VARCHAR(50) NOT NULL DEFAULT 'wal',
                timestamp VARCHAR(100),
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                plsn TEXT,
                xid TEXT,
                type VARCHAR(128),
                page_id TEXT,
                message TEXT
            )
        ''')

        # 服务器注册表
        cursor.execute('''
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

        # 创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_server ON log_entries(server_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_received ON log_entries(received_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_plsn ON log_entries(plsn)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_xid ON log_entries(xid)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_wal_type ON log_entries(type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_page_id ON log_entries(page_id)')
        
        conn.commit()
        print("✅ 数据库初始化完成")
    except Exception as e:
        conn.rollback()
        print(f"❌ 数据库初始化失败: {e}")
        raise
    finally:
        cursor.close()
        release_db_connection(conn)


def parse_log_line(line, log_type='wal'):
    """解析 WAL 日志行"""
    result = {
        'plsn': None,
        'xid': None,
        'type': None,
        'page_id': None,
        'message': line,  # 存储完整的原始日志
        'timestamp': datetime.now().strftime('%b %d %H:%M:%S')
    }

    # WAL 日志格式解析
    wal_match = WAL_LOG_PATTERN.match(line)
    if wal_match:
        plsn = wal_match.group('plsn')
        xid_high = wal_match.group('xid_high')
        xid_low = wal_match.group('xid_low')
        wal_type = wal_match.group('wal_type')
        page_id_1 = wal_match.group('page_id_1')
        page_id_2 = wal_match.group('page_id_2')

        result.update({
            'plsn': plsn,
            'xid': f"({xid_high},{xid_low})",
            'type': wal_type,
            'page_id': f"({page_id_1},{page_id_2})",
            'message': line  # 完整原始日志
        })

        return result

    # 尝试简化匹配提取 type
    simple_match = WAL_SIMPLE_PATTERN.match(line)
    if simple_match:
        result.update({
            'type': simple_match.group('wal_type'),
            'message': line
        })
        return result

    # 无法匹配的日志，保持原样
    return result


class LogHandler(BaseHTTPRequestHandler):
    """HTTP请求处理器"""
    
    def log_message(self, format, *args):
        """自定义日志格式"""
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}")
    
    def send_json(self, data, status=200):
        """发送JSON响应"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode('utf-8'))
    
    def send_html(self, html, status=200):
        """发送HTML响应"""
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    
    def do_OPTIONS(self):
        """处理CORS预检请求"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        """处理GET请求"""
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        
        routes = {
            '/': self.serve_dashboard,
            '/dashboard': self.serve_dashboard,
            '/api/stats': lambda: self.get_stats(params),
            '/api/logs': lambda: self.get_logs(params),
            '/api/servers': self.get_servers,
            '/api/trends': lambda: self.get_trends(params),
            '/api/alerts': lambda: self.get_alerts(params),
        }
        
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_json({'error': 'Not Found'}, 404)
    
    def do_POST(self):
        """处理POST请求（支持 gzip 压缩）"""
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get('Content-Length', 0))
        raw_body = self.rfile.read(content_length)

        # 支持 gzip 压缩的请求体
        content_encoding = self.headers.get('Content-Encoding', '')
        if content_encoding == 'gzip':
            try:
                raw_body = gzip.decompress(raw_body)
            except Exception as e:
                self.send_json({'error': f'Gzip decompress failed: {e}'}, 400)
                return

        body = raw_body.decode('utf-8')

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json({'error': 'Invalid JSON'}, 400)
            return

        routes = {
            '/api/upload': lambda: self.upload_logs(data),
            '/api/upload/batch': lambda: self.upload_batch_logs(data),  # 批量上传多节点
            '/api/register': lambda: self.register_server(data),
        }

        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_json({'error': 'Not Found'}, 404)

    def upload_batch_logs(self, data):
        """批量上传多个节点的日志（一次请求包含多个节点）"""
        nodes = data.get('nodes', [])
        if not nodes:
            self.send_json({'error': 'No nodes provided'}, 400)
            return

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            total_inserted = 0
            node_stats = {}

            for node in nodes:
                server_id = node.get('server_id', 'unknown')
                hostname = node.get('hostname', 'unknown')
                ip_address = node.get('ip', '')
                log_type = node.get('log_type', 'wal')
                logs = node.get('logs', [])

                batch_data = []
                stats = defaultdict(int)

                for line in logs:
                    if not line.strip():
                        continue
                    parsed = parse_log_line(line, log_type)
                    batch_data.append((
                        server_id, hostname, log_type,
                        parsed['timestamp'], parsed['plsn'], parsed['xid'],
                        parsed['type'], parsed['page_id'], parsed['message']
                    ))
                    if parsed['type']:
                        stats[parsed['type']] += 1

                if batch_data:
                    execute_values(
                        cursor,
                        '''INSERT INTO log_entries
                           (server_id, hostname, log_type, timestamp, plsn, xid, type, page_id, message)
                           VALUES %s''',
                        batch_data,
                        page_size=1000
                    )

                inserted = len(batch_data)
                total_inserted += inserted
                node_stats[server_id] = {'inserted': inserted, 'stats': dict(stats)}

                # 更新服务器记录
                cursor.execute('''
                    INSERT INTO servers (id, hostname, ip_address, last_seen, total_logs)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        hostname = EXCLUDED.hostname,
                        ip_address = COALESCE(EXCLUDED.ip_address, servers.ip_address),
                        last_seen = CURRENT_TIMESTAMP,
                        total_logs = servers.total_logs + EXCLUDED.total_logs
                ''', (server_id, hostname, ip_address, inserted))

            conn.commit()

            self.send_json({
                'success': True,
                'total_inserted': total_inserted,
                'nodes': len(nodes),
                'node_stats': node_stats
            })
        except Exception as e:
            conn.rollback()
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def upload_logs(self, data):
        """接收并存储 WAL 日志（批量插入优化）"""
        server_id = data.get('server_id', 'unknown')
        hostname = data.get('hostname', 'unknown')
        ip_address = data.get('ip', '')
        log_type = data.get('log_type', 'wal')
        logs = data.get('logs', [])

        if not logs:
            self.send_json({'error': 'No logs provided'}, 400)
            return

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            # 准备批量插入数据
            batch_data = []
            stats = defaultdict(int)

            for line in logs:
                if not line.strip():
                    continue

                parsed = parse_log_line(line, log_type)
                batch_data.append((
                    server_id, hostname, log_type,
                    parsed['timestamp'], parsed['plsn'], parsed['xid'],
                    parsed['type'], parsed['page_id'], parsed['message']
                ))
                if parsed['type']:
                    stats[parsed['type']] += 1

            # 使用 execute_values 批量插入（比逐条插入快 10-100 倍）
            if batch_data:
                execute_values(
                    cursor,
                    '''INSERT INTO log_entries
                       (server_id, hostname, log_type, timestamp, plsn, xid, type, page_id, message)
                       VALUES %s''',
                    batch_data,
                    page_size=1000  # 每批 1000 条
                )

            inserted = len(batch_data)

            # 更新或插入服务器记录
            cursor.execute('''
                INSERT INTO servers (id, hostname, ip_address, last_seen, total_logs)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (id) DO UPDATE SET
                    hostname = EXCLUDED.hostname,
                    ip_address = COALESCE(EXCLUDED.ip_address, servers.ip_address),
                    last_seen = CURRENT_TIMESTAMP,
                    total_logs = servers.total_logs + EXCLUDED.total_logs
            ''', (server_id, hostname, ip_address, inserted))

            conn.commit()

            self.send_json({
                'success': True,
                'inserted': inserted,
                'stats': dict(stats)
            })
        except Exception as e:
            conn.rollback()
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def register_server(self, data):
        """注册新服务器（无需认证）"""
        hostname = data.get('hostname')
        if not hostname:
            self.send_json({'error': 'Hostname required'}, 400)
            return
        
        # 使用hostname生成简单的server_id
        import hashlib
        server_id = hashlib.sha256(f"{hostname}-{data.get('ip', '')}".encode()).hexdigest()[:16]
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO servers (id, hostname, ip_address, os_info)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    hostname = EXCLUDED.hostname,
                    ip_address = EXCLUDED.ip_address,
                    os_info = EXCLUDED.os_info,
                    last_seen = CURRENT_TIMESTAMP
            ''', (server_id, hostname, data.get('ip'), data.get('os_info')))
            
            conn.commit()
            
            self.send_json({
                'success': True,
                'server_id': server_id,
                'message': 'Server registered successfully'
            })
        except Exception as e:
            conn.rollback()
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def get_stats(self, params):
        """获取 WAL 日志统计数据"""
        hours = int(params.get('hours', ['24'])[0])

        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)

            # 总体统计
            cursor.execute('''
                SELECT
                    COUNT(*) as total,
                    COUNT(DISTINCT server_id) as servers,
                    COUNT(DISTINCT type) as wal_types
                FROM log_entries
                WHERE received_at > NOW() - INTERVAL '%s hours'
            ''', (hours,))
            overview = cursor.fetchone()

            # 按 WAL 类型统计
            cursor.execute('''
                SELECT type, COUNT(*) as count
                FROM log_entries
                WHERE received_at > NOW() - INTERVAL '%s hours' AND type IS NOT NULL
                GROUP BY type ORDER BY count DESC
            ''', (hours,))
            by_wal_type = {row['type']: row['count'] for row in cursor.fetchall()}

            # 按服务器统计
            cursor.execute('''
                SELECT server_id, hostname, COUNT(*) as count
                FROM log_entries
                WHERE received_at > NOW() - INTERVAL '%s hours'
                GROUP BY server_id, hostname ORDER BY count DESC LIMIT 10
            ''', (hours,))
            by_server = [{'id': r['server_id'], 'hostname': r['hostname'], 'count': r['count']}
                        for r in cursor.fetchall()]

            # 按 pageId 统计（Top 10）
            cursor.execute('''
                SELECT page_id, COUNT(*) as count
                FROM log_entries
                WHERE received_at > NOW() - INTERVAL '%s hours' AND page_id IS NOT NULL
                GROUP BY page_id ORDER BY count DESC LIMIT 10
            ''', (hours,))
            by_page_id = {row['page_id']: row['count'] for row in cursor.fetchall()}

            self.send_json({
                'overview': {
                    'total_logs': overview['total'],
                    'active_servers': overview['servers'],
                    'wal_types': overview['wal_types']
                },
                'by_wal_type': by_wal_type,
                'by_server': by_server,
                'by_page_id': by_page_id,
                'period_hours': hours
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def get_logs(self, params):
        """查询 WAL 日志"""
        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)

            # 构建查询
            conditions = ['1=1']
            values = []
            use_join = False

            if 'server_id' in params:
                conditions.append('l.server_id = %s')
                values.append(params['server_id'][0])

            if 'type' in params:
                conditions.append('l.type = %s')
                values.append(params['type'][0])

            if 'plsn' in params:
                conditions.append('l.plsn = %s')
                values.append(params['plsn'][0])

            if 'xid' in params:
                conditions.append('l.xid = %s')
                values.append(params['xid'][0])

            if 'search' in params:
                search_term = params['search'][0]
                # 搜索 server_id, hostname, ip_address 或 message
                conditions.append('''(
                    l.server_id ILIKE %s OR
                    l.hostname ILIKE %s OR
                    l.message ILIKE %s OR
                    s.ip_address ILIKE %s
                )''')
                search_pattern = f"%{search_term}%"
                values.extend([search_pattern, search_pattern, search_pattern, search_pattern])
                use_join = True

            if 'since' in params:
                conditions.append('l.received_at > %s')
                values.append(params['since'][0])

            limit = min(int(params.get('limit', ['100'])[0]), 1000)
            offset = int(params.get('offset', ['0'])[0])

            # 根据是否需要 JOIN 构建不同的查询
            if use_join:
                query = f'''
                    SELECT DISTINCT l.id, l.server_id, l.hostname, l.log_type, l.timestamp, l.received_at,
                           l.plsn, l.xid, l.type, l.page_id, l.message
                    FROM log_entries l
                    LEFT JOIN servers s ON l.server_id = s.id
                    WHERE {' AND '.join(conditions)}
                    ORDER BY l.received_at DESC
                    LIMIT %s OFFSET %s
                '''
            else:
                query = f'''
                    SELECT id, server_id, hostname, log_type, timestamp, received_at,
                           plsn, xid, type, page_id, message
                    FROM log_entries l
                    WHERE {' AND '.join(conditions)}
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                '''

            cursor.execute(query, values + [limit, offset])

            logs = []
            for row in cursor.fetchall():
                logs.append({
                    'id': row['id'],
                    'server_id': row['server_id'],
                    'hostname': row['hostname'],
                    'log_type': row['log_type'],
                    'timestamp': row['timestamp'],
                    'received_at': row['received_at'],
                    'plsn': row['plsn'],
                    'xid': row['xid'],
                    'type': row['type'],
                    'page_id': row['page_id'],
                    'message': row['message']
                })

            # 获取总数
            if use_join:
                count_query = f'''
                    SELECT COUNT(DISTINCT l.id) as total
                    FROM log_entries l
                    LEFT JOIN servers s ON l.server_id = s.id
                    WHERE {' AND '.join(conditions)}
                '''
            else:
                count_query = f'''
                    SELECT COUNT(*) as total FROM log_entries l WHERE {' AND '.join(conditions)}
                '''
            cursor.execute(count_query, values)
            total = cursor.fetchone()['total']

            self.send_json({
                'logs': logs,
                'total': total,
                'limit': limit,
                'offset': offset
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def get_servers(self):
        """获取服务器列表"""
        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute('''
                SELECT id, hostname, ip_address, os_info, first_seen, last_seen, total_logs
                FROM servers ORDER BY last_seen DESC
            ''')
            
            servers = []
            for row in cursor.fetchall():
                servers.append({
                    'id': row['id'],
                    'hostname': row['hostname'],
                    'ip': row['ip_address'],
                    'os_info': row['os_info'],
                    'first_seen': row['first_seen'],
                    'last_seen': row['last_seen'],
                    'total_logs': row['total_logs']
                })
            
            self.send_json({'servers': servers})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def get_trends(self, params):
        """获取趋势数据"""
        hours = int(params.get('hours', ['24'])[0])
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute('''
                SELECT 
                    TO_CHAR(received_at, 'YYYY-MM-DD HH24:00') as hour,
                    level,
                    COUNT(*) as count
                FROM log_entries 
                WHERE received_at > NOW() - INTERVAL '%s hours'
                GROUP BY hour, level
                ORDER BY hour
            ''', (hours,))
            
            trends = defaultdict(lambda: defaultdict(int))
            for row in cursor.fetchall():
                trends[row['hour']][row['level']] = row['count']
            
            self.send_json({
                'trends': dict(trends),
                'hours': hours
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def get_alerts(self, params):
        """获取告警（危险的 WAL 操作：DELETE、TRUNCATE、ABORT 等）"""
        hours = int(params.get('hours', ['24'])[0])

        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)

            cursor.execute('''
                SELECT id, server_id, hostname, log_type, timestamp, received_at,
                       plsn, xid, type, page_id, message
                FROM log_entries
                WHERE type ILIKE ANY(ARRAY['%%DELETE%%', '%%TRUNCATE%%', '%%ABORT%%', '%%ERROR%%'])
                AND received_at > NOW() - INTERVAL '%s hours'
                ORDER BY received_at DESC
                LIMIT 100
            ''', (hours,))

            alerts = []
            for row in cursor.fetchall():
                alerts.append({
                    'id': row['id'],
                    'server_id': row['server_id'],
                    'hostname': row['hostname'],
                    'log_type': row['log_type'],
                    'timestamp': row['timestamp'],
                    'received_at': row['received_at'],
                    'plsn': row['plsn'],
                    'xid': row['xid'],
                    'type': row['type'],
                    'page_id': row['page_id'],
                    'message': row['message']
                })

            self.send_json({'alerts': alerts})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def serve_dashboard(self):
        """提供仪表板HTML"""
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            dashboard_path = os.path.join(script_dir, 'dashboard.html')
            
            with open(dashboard_path, 'r', encoding='utf-8') as f:
                self.send_html(f.read())
        except FileNotFoundError:
            self.send_html('<h1>Dashboard not found</h1><p>Please ensure dashboard.html is in the same directory.</p>', 404)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """支持多线程的 HTTP 服务器，用于处理并发请求"""
    daemon_threads = True


def main():
    """主入口"""
    global db_pool

    print("🚀 启动 WAL 日志分析平台...")
    print(f"📦 数据库: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    print(f"🔗 连接池: {POOL_MIN_CONN} - {POOL_MAX_CONN} 连接")

    # 创建数据库连接池（支持大规模并发）
    try:
        db_pool = pool.ThreadedConnectionPool(
            minconn=POOL_MIN_CONN,
            maxconn=POOL_MAX_CONN,
            **DB_CONFIG
        )
        print("✅ 数据库连接池创建成功")
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        print("\n请确保PostgreSQL正在运行，并设置以下环境变量：")
        print("  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD")
        return

    # 初始化数据库表
    init_database()

    # 启动多线程 HTTP 服务器
    server = ThreadedHTTPServer((HOST, PORT), LogHandler)
    print(f"\n✅ 服务器启动成功（多线程模式）")
    print(f"📊 访问仪表板: http://localhost:{PORT}/dashboard")
    print(f"📤 单节点上传: POST http://localhost:{PORT}/api/upload")
    print(f"📤 批量上传:   POST http://localhost:{PORT}/api/upload/batch")
    print(f"🖥️  服务器注册: POST http://localhost:{PORT}/api/register")
    print(f"\n💡 支持 gzip 压缩上传（Content-Encoding: gzip）")
    print("⚠️  注意: 已禁用API密钥认证，请确保网络安全\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 正在关闭服务器...")
        server.shutdown()
        db_pool.closeall()
        print("✅ 服务器已关闭")


if __name__ == '__main__':
    main()
