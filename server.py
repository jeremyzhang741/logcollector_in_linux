#!/usr/bin/env python3
"""
Linux日志收集与分析平台 - 后端服务
支持多服务器日志收集、解析、统计和查询
"""

import json
import os
import re
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

# 配置
DB_PATH = "logs.db"
API_KEYS_FILE = "api_keys.json"
HOST = "0.0.0.0"
PORT = 8080

# 日志级别定义
LOG_LEVELS = {
    'emerg': 0, 'alert': 1, 'crit': 2, 'err': 3, 'error': 3,
    'warning': 4, 'warn': 4, 'notice': 5, 'info': 6, 'debug': 7
}

# 常见日志格式正则
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


def init_database():
    """初始化SQLite数据库"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 日志条目表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS log_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id TEXT NOT NULL,
            hostname TEXT,
            log_type TEXT NOT NULL,
            timestamp TEXT,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            level TEXT,
            service TEXT,
            pid INTEGER,
            message TEXT,
            raw_line TEXT,
            metadata TEXT
        )
    ''')
    
    # 服务器注册表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            id TEXT PRIMARY KEY,
            hostname TEXT,
            ip_address TEXT,
            os_info TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            total_logs INTEGER DEFAULT 0
        )
    ''')
    
    # 统计摘要表（按小时聚合）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hourly_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id TEXT,
            hour TEXT,
            log_type TEXT,
            level TEXT,
            count INTEGER DEFAULT 0,
            UNIQUE(server_id, hour, log_type, level)
        )
    ''')
    
    # 创建索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_server ON log_entries(server_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_timestamp ON log_entries(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_level ON log_entries(level)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_type ON log_entries(log_type)')
    
    conn.commit()
    conn.close()


def load_api_keys():
    """加载API密钥"""
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_api_keys(keys):
    """保存API密钥"""
    with open(API_KEYS_FILE, 'w') as f:
        json.dump(keys, f, indent=2)


def generate_api_key():
    """生成新的API密钥"""
    return secrets.token_urlsafe(32)


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
    
    api_keys = load_api_keys()
    lock = threading.Lock()
    
    def log_message(self, format, *args):
        """禁用默认日志"""
        pass
    
    def send_json(self, data, status=200):
        """发送JSON响应"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-API-Key')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def send_html(self, html, status=200):
        """发送HTML响应"""
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    
    def verify_api_key(self):
        """验证API密钥"""
        api_key = self.headers.get('X-API-Key')
        if not api_key:
            return None
        return self.api_keys.get(api_key)
    
    def do_OPTIONS(self):
        """处理CORS预检请求"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-API-Key')
        self.end_headers()
    
    def do_GET(self):
        """处理GET请求"""
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        
        if path == '/' or path == '/dashboard':
            self.serve_dashboard()
        elif path == '/api/stats':
            self.get_stats(params)
        elif path == '/api/logs':
            self.get_logs(params)
        elif path == '/api/servers':
            self.get_servers()
        elif path == '/api/trends':
            self.get_trends(params)
        elif path == '/api/alerts':
            self.get_alerts(params)
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
        
        if path == '/api/upload':
            self.upload_logs(data)
        elif path == '/api/register':
            self.register_server(data)
        elif path == '/api/keys/generate':
            self.generate_key(data)
        else:
            self.send_json({'error': 'Not Found'}, 404)
    
    def upload_logs(self, data):
        """接收并存储日志"""
        server_info = self.verify_api_key()
        if not server_info:
            self.send_json({'error': 'Invalid API Key'}, 401)
            return
        
        server_id = server_info.get('server_id', 'unknown')
        hostname = data.get('hostname', server_info.get('hostname', 'unknown'))
        log_type = data.get('log_type', 'syslog')
        logs = data.get('logs', [])
        
        if not logs:
            self.send_json({'error': 'No logs provided'}, 400)
            return
        
        conn = sqlite3.connect(DB_PATH)
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                server_id, hostname, log_type,
                parsed['timestamp'], parsed['level'], parsed['service'],
                parsed['pid'], parsed['message'], parsed['raw_line'],
                json.dumps(parsed['metadata'])
            ))
            inserted += 1
            stats[parsed['level']] += 1
        
        # 更新服务器状态
        cursor.execute('''
            UPDATE servers SET last_seen = CURRENT_TIMESTAMP, 
            total_logs = total_logs + ? WHERE id = ?
        ''', (inserted, server_id))
        
        conn.commit()
        conn.close()
        
        self.send_json({
            'success': True,
            'inserted': inserted,
            'stats': dict(stats)
        })
    
    def register_server(self, data):
        """注册新服务器"""
        hostname = data.get('hostname')
        if not hostname:
            self.send_json({'error': 'Hostname required'}, 400)
            return
        
        server_id = hashlib.sha256(f"{hostname}-{datetime.now().isoformat()}".encode()).hexdigest()[:16]
        api_key = generate_api_key()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO servers (id, hostname, ip_address, os_info)
            VALUES (?, ?, ?, ?)
        ''', (server_id, hostname, data.get('ip'), data.get('os_info')))
        
        conn.commit()
        conn.close()
        
        # 保存API密钥
        with self.lock:
            self.api_keys[api_key] = {
                'server_id': server_id,
                'hostname': hostname,
                'created': datetime.now().isoformat()
            }
            save_api_keys(self.api_keys)
        
        self.send_json({
            'success': True,
            'server_id': server_id,
            'api_key': api_key,
            'message': 'Server registered successfully. Save your API key!'
        })
    
    def generate_key(self, data):
        """生成新的API密钥（管理功能）"""
        # 简单的管理密码验证
        if data.get('admin_password') != os.environ.get('ADMIN_PASSWORD', 'admin123'):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        
        hostname = data.get('hostname', 'manual')
        server_id = data.get('server_id') or hashlib.sha256(
            f"{hostname}-{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]
        
        api_key = generate_api_key()
        
        with self.lock:
            self.api_keys[api_key] = {
                'server_id': server_id,
                'hostname': hostname,
                'created': datetime.now().isoformat()
            }
            save_api_keys(self.api_keys)
        
        self.send_json({
            'api_key': api_key,
            'server_id': server_id
        })
    
    def get_stats(self, params):
        """获取统计数据"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 时间范围
        hours = int(params.get('hours', ['24'])[0])
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        # 总体统计
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                COUNT(DISTINCT server_id) as servers,
                COUNT(DISTINCT log_type) as log_types
            FROM log_entries WHERE received_at > ?
        ''', (since,))
        overview = cursor.fetchone()
        
        # 按级别统计
        cursor.execute('''
            SELECT level, COUNT(*) as count 
            FROM log_entries WHERE received_at > ?
            GROUP BY level ORDER BY count DESC
        ''', (since,))
        by_level = {row[0]: row[1] for row in cursor.fetchall()}
        
        # 按类型统计
        cursor.execute('''
            SELECT log_type, COUNT(*) as count 
            FROM log_entries WHERE received_at > ?
            GROUP BY log_type ORDER BY count DESC
        ''', (since,))
        by_type = {row[0]: row[1] for row in cursor.fetchall()}
        
        # 按服务器统计
        cursor.execute('''
            SELECT server_id, hostname, COUNT(*) as count 
            FROM log_entries WHERE received_at > ?
            GROUP BY server_id ORDER BY count DESC LIMIT 10
        ''', (since,))
        by_server = [{'id': r[0], 'hostname': r[1], 'count': r[2]} for r in cursor.fetchall()]
        
        # 按服务统计
        cursor.execute('''
            SELECT service, COUNT(*) as count 
            FROM log_entries WHERE received_at > ? AND service IS NOT NULL
            GROUP BY service ORDER BY count DESC LIMIT 10
        ''', (since,))
        by_service = {row[0]: row[1] for row in cursor.fetchall()}
        
        conn.close()
        
        self.send_json({
            'overview': {
                'total_logs': overview[0],
                'active_servers': overview[1],
                'log_types': overview[2]
            },
            'by_level': by_level,
            'by_type': by_type,
            'by_server': by_server,
            'by_service': by_service,
            'period_hours': hours
        })
    
    def get_logs(self, params):
        """查询日志"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 构建查询
        conditions = ['1=1']
        values = []
        
        if 'server_id' in params:
            conditions.append('server_id = ?')
            values.append(params['server_id'][0])
        
        if 'level' in params:
            conditions.append('level = ?')
            values.append(params['level'][0])
        
        if 'log_type' in params:
            conditions.append('log_type = ?')
            values.append(params['log_type'][0])
        
        if 'search' in params:
            conditions.append('message LIKE ?')
            values.append(f"%{params['search'][0]}%")
        
        if 'since' in params:
            conditions.append('received_at > ?')
            values.append(params['since'][0])
        
        limit = min(int(params.get('limit', ['100'])[0]), 1000)
        offset = int(params.get('offset', ['0'])[0])
        
        query = f'''
            SELECT id, server_id, hostname, log_type, timestamp, received_at, 
                   level, service, message, metadata
            FROM log_entries 
            WHERE {' AND '.join(conditions)}
            ORDER BY received_at DESC
            LIMIT ? OFFSET ?
        '''
        
        cursor.execute(query, values + [limit, offset])
        
        logs = []
        for row in cursor.fetchall():
            logs.append({
                'id': row[0],
                'server_id': row[1],
                'hostname': row[2],
                'log_type': row[3],
                'timestamp': row[4],
                'received_at': row[5],
                'level': row[6],
                'service': row[7],
                'message': row[8],
                'metadata': json.loads(row[9]) if row[9] else {}
            })
        
        # 获取总数
        count_query = f'''
            SELECT COUNT(*) FROM log_entries WHERE {' AND '.join(conditions)}
        '''
        cursor.execute(count_query, values)
        total = cursor.fetchone()[0]
        
        conn.close()
        
        self.send_json({
            'logs': logs,
            'total': total,
            'limit': limit,
            'offset': offset
        })
    
    def get_servers(self):
        """获取服务器列表"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, hostname, ip_address, os_info, first_seen, last_seen, total_logs
            FROM servers ORDER BY last_seen DESC
        ''')
        
        servers = []
        for row in cursor.fetchall():
            servers.append({
                'id': row[0],
                'hostname': row[1],
                'ip': row[2],
                'os_info': row[3],
                'first_seen': row[4],
                'last_seen': row[5],
                'total_logs': row[6]
            })
        
        conn.close()
        self.send_json({'servers': servers})
    
    def get_trends(self, params):
        """获取趋势数据"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        hours = int(params.get('hours', ['24'])[0])
        
        # 按小时统计
        cursor.execute('''
            SELECT 
                strftime('%Y-%m-%d %H:00', received_at) as hour,
                level,
                COUNT(*) as count
            FROM log_entries 
            WHERE received_at > datetime('now', ?)
            GROUP BY hour, level
            ORDER BY hour
        ''', (f'-{hours} hours',))
        
        trends = defaultdict(lambda: defaultdict(int))
        for row in cursor.fetchall():
            trends[row[0]][row[1]] = row[2]
        
        conn.close()
        
        self.send_json({
            'trends': dict(trends),
            'hours': hours
        })
    
    def get_alerts(self, params):
        """获取告警（错误和警告）"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        hours = int(params.get('hours', ['24'])[0])
        
        cursor.execute('''
            SELECT id, server_id, hostname, log_type, timestamp, received_at, 
                   level, service, message
            FROM log_entries 
            WHERE level IN ('error', 'crit', 'alert', 'emerg', 'warning')
            AND received_at > datetime('now', ?)
            ORDER BY received_at DESC
            LIMIT 100
        ''', (f'-{hours} hours',))
        
        alerts = []
        for row in cursor.fetchall():
            alerts.append({
                'id': row[0],
                'server_id': row[1],
                'hostname': row[2],
                'log_type': row[3],
                'timestamp': row[4],
                'received_at': row[5],
                'level': row[6],
                'service': row[7],
                'message': row[8]
            })
        
        conn.close()
        self.send_json({'alerts': alerts})
    
    def serve_dashboard(self):
        """提供仪表板HTML"""
        with open('dashboard.html', 'r') as f:
            self.send_html(f.read())


def main():
    """主入口"""
    print("🚀 初始化日志分析平台...")
    init_database()
    
    server = HTTPServer((HOST, PORT), LogHandler)
    print(f"✅ 服务器启动在 http://{HOST}:{PORT}")
    print(f"📊 访问仪表板: http://localhost:{PORT}/dashboard")
    print("📤 日志上传端点: POST /api/upload")
    print("🔑 服务器注册: POST /api/register")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 服务器关闭")
        server.shutdown()


if __name__ == '__main__':
    main()
