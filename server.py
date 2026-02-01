#!/usr/bin/env python3
"""
Linux日志收集与分析平台 - 后端服务
使用PostgreSQL存储，无认证模式
"""

import json
import os
import re
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2.extras import RealDictCursor
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

# 连接池
db_pool = None

# ==================== 日志解析 ====================
LOG_LEVELS = {
    'emerg': 0, 'alert': 1, 'crit': 2, 'err': 3, 'error': 3,
    'warning': 4, 'warn': 4, 'notice': 5, 'info': 6, 'debug': 7
}

SYSLOG_PATTERN = re.compile(
    r'^(?P<timestamp>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+'
    r'(?P<hostname>\S+)\s+'
    r'(?P<service>\S+?)(?:\[(?P<pid>\d+)\])?:\s+'
    r'(?P<message>.*)$'
)

AUTH_LOG_PATTERN = re.compile(
    r'(?P<action>Failed|Accepted|Invalid|session opened|session closed).*?'
    r'(?:for\s+(?:invalid\s+)?user\s+)?(?P<user>\S+)?'
    r'(?:\s+from\s+(?P<ip>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}))?'
)

KERNEL_PATTERN = re.compile(
    r'\[\s*(?P<uptime>[\d.]+)\]\s*(?P<message>.*)'
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
        
        # 日志条目表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS log_entries (
                id SERIAL PRIMARY KEY,
                server_id VARCHAR(64) NOT NULL,
                hostname VARCHAR(255),
                log_type VARCHAR(50) NOT NULL,
                timestamp VARCHAR(100),
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level VARCHAR(20),
                service VARCHAR(255),
                pid INTEGER,
                message TEXT,
                raw_line TEXT,
                metadata JSONB DEFAULT '{}'::jsonb
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
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_level ON log_entries(level)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_type ON log_entries(log_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_message ON log_entries USING gin(to_tsvector(\'simple\', message))')
        
        conn.commit()
        print("✅ 数据库初始化完成")
    except Exception as e:
        conn.rollback()
        print(f"❌ 数据库初始化失败: {e}")
        raise
    finally:
        cursor.close()
        release_db_connection(conn)


def parse_log_line(line, log_type):
    """解析单行日志"""
    result = {
        'raw_line': line,
        'level': 'info',
        'service': None,
        'pid': None,
        'message': line,
        'timestamp': None,
        'metadata': {}
    }
    
    # 尝试匹配syslog格式
    match = SYSLOG_PATTERN.match(line)
    if match:
        result.update({
            'timestamp': match.group('timestamp'),
            'service': match.group('service'),
            'pid': int(match.group('pid')) if match.group('pid') else None,
            'message': match.group('message')
        })
    
    # 检测日志级别
    line_lower = line.lower()
    for level, priority in sorted(LOG_LEVELS.items(), key=lambda x: x[1]):
        if level in line_lower:
            result['level'] = level if level not in ('err', 'warn') else ('error' if level == 'err' else 'warning')
            break
    
    # 特殊日志类型解析
    if log_type == 'auth':
        auth_match = AUTH_LOG_PATTERN.search(line)
        if auth_match:
            result['metadata'] = {
                'action': auth_match.group('action'),
                'user': auth_match.group('user'),
                'ip': auth_match.group('ip')
            }
            if 'failed' in line_lower or 'invalid' in line_lower:
                result['level'] = 'warning'
    
    elif log_type == 'kern':
        kern_match = KERNEL_PATTERN.search(line)
        if kern_match:
            result['metadata']['uptime'] = kern_match.group('uptime')
            result['message'] = kern_match.group('message')
    
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
        """处理POST请求"""
        parsed = urlparse(self.path)
        path = parsed.path
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json({'error': 'Invalid JSON'}, 400)
            return
        
        routes = {
            '/api/upload': lambda: self.upload_logs(data),
            '/api/register': lambda: self.register_server(data),
        }
        
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_json({'error': 'Not Found'}, 404)
    
    def upload_logs(self, data):
        """接收并存储日志（无需认证）"""
        server_id = data.get('server_id', 'unknown')
        hostname = data.get('hostname', 'unknown')
        log_type = data.get('log_type', 'syslog')
        logs = data.get('logs', [])
        
        if not logs:
            self.send_json({'error': 'No logs provided'}, 400)
            return
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            inserted = 0
            stats = defaultdict(int)
            
            for line in logs:
                if not line.strip():
                    continue
                
                parsed = parse_log_line(line, log_type)
                
                cursor.execute('''
                    INSERT INTO log_entries 
                    (server_id, hostname, log_type, timestamp, level, service, pid, message, raw_line, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    server_id, hostname, log_type,
                    parsed['timestamp'], parsed['level'], parsed['service'],
                    parsed['pid'], parsed['message'], parsed['raw_line'],
                    json.dumps(parsed['metadata'])
                ))
                inserted += 1
                stats[parsed['level']] += 1
            
            # 更新或插入服务器记录
            cursor.execute('''
                INSERT INTO servers (id, hostname, last_seen, total_logs)
                VALUES (%s, %s, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    total_logs = servers.total_logs + EXCLUDED.total_logs
            ''', (server_id, hostname, inserted))
            
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
        """获取统计数据"""
        hours = int(params.get('hours', ['24'])[0])
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            # 总体统计
            cursor.execute('''
                SELECT 
                    COUNT(*) as total,
                    COUNT(DISTINCT server_id) as servers,
                    COUNT(DISTINCT log_type) as log_types
                FROM log_entries 
                WHERE received_at > NOW() - INTERVAL '%s hours'
            ''', (hours,))
            overview = cursor.fetchone()
            
            # 按级别统计
            cursor.execute('''
                SELECT level, COUNT(*) as count 
                FROM log_entries 
                WHERE received_at > NOW() - INTERVAL '%s hours'
                GROUP BY level ORDER BY count DESC
            ''', (hours,))
            by_level = {row['level']: row['count'] for row in cursor.fetchall()}
            
            # 按类型统计
            cursor.execute('''
                SELECT log_type, COUNT(*) as count 
                FROM log_entries 
                WHERE received_at > NOW() - INTERVAL '%s hours'
                GROUP BY log_type ORDER BY count DESC
            ''', (hours,))
            by_type = {row['log_type']: row['count'] for row in cursor.fetchall()}
            
            # 按服务器统计
            cursor.execute('''
                SELECT server_id, hostname, COUNT(*) as count 
                FROM log_entries 
                WHERE received_at > NOW() - INTERVAL '%s hours'
                GROUP BY server_id, hostname ORDER BY count DESC LIMIT 10
            ''', (hours,))
            by_server = [{'id': r['server_id'], 'hostname': r['hostname'], 'count': r['count']} 
                        for r in cursor.fetchall()]
            
            # 按服务统计
            cursor.execute('''
                SELECT service, COUNT(*) as count 
                FROM log_entries 
                WHERE received_at > NOW() - INTERVAL '%s hours' AND service IS NOT NULL
                GROUP BY service ORDER BY count DESC LIMIT 10
            ''', (hours,))
            by_service = {row['service']: row['count'] for row in cursor.fetchall()}
            
            self.send_json({
                'overview': {
                    'total_logs': overview['total'],
                    'active_servers': overview['servers'],
                    'log_types': overview['log_types']
                },
                'by_level': by_level,
                'by_type': by_type,
                'by_server': by_server,
                'by_service': by_service,
                'period_hours': hours
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            cursor.close()
            release_db_connection(conn)
    
    def get_logs(self, params):
        """查询日志"""
        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            # 构建查询
            conditions = ['1=1']
            values = []
            
            if 'server_id' in params:
                conditions.append('server_id = %s')
                values.append(params['server_id'][0])
            
            if 'level' in params:
                conditions.append('level = %s')
                values.append(params['level'][0])
            
            if 'log_type' in params:
                conditions.append('log_type = %s')
                values.append(params['log_type'][0])
            
            if 'search' in params:
                conditions.append('message ILIKE %s')
                values.append(f"%{params['search'][0]}%")
            
            if 'since' in params:
                conditions.append('received_at > %s')
                values.append(params['since'][0])
            
            limit = min(int(params.get('limit', ['100'])[0]), 1000)
            offset = int(params.get('offset', ['0'])[0])
            
            query = f'''
                SELECT id, server_id, hostname, log_type, timestamp, received_at, 
                       level, service, message, metadata
                FROM log_entries 
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
                    'level': row['level'],
                    'service': row['service'],
                    'message': row['message'],
                    'metadata': row['metadata'] if row['metadata'] else {}
                })
            
            # 获取总数
            count_query = f'''
                SELECT COUNT(*) as total FROM log_entries WHERE {' AND '.join(conditions)}
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
        """获取告警（错误和警告）"""
        hours = int(params.get('hours', ['24'])[0])
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute('''
                SELECT id, server_id, hostname, log_type, timestamp, received_at, 
                       level, service, message
                FROM log_entries 
                WHERE level IN ('error', 'crit', 'alert', 'emerg', 'warning')
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
                    'level': row['level'],
                    'service': row['service'],
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


def main():
    """主入口"""
    global db_pool
    
    print("🚀 启动日志分析平台...")
    print(f"📦 数据库: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    
    # 创建数据库连接池
    try:
        db_pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
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
    
    # 启动HTTP服务器
    server = HTTPServer((HOST, PORT), LogHandler)
    print(f"\n✅ 服务器启动成功")
    print(f"📊 访问仪表板: http://localhost:{PORT}/dashboard")
    print(f"📤 日志上传: POST http://localhost:{PORT}/api/upload")
    print(f"🖥️  服务器注册: POST http://localhost:{PORT}/api/register")
    print("\n⚠️  注意: 已禁用API密钥认证，请确保网络安全\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 正在关闭服务器...")
        server.shutdown()
        db_pool.closeall()
        print("✅ 服务器已关闭")


if __name__ == '__main__':
    main()
