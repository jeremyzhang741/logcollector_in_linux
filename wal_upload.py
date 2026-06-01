#!/usr/bin/env python3
"""
WAL 日志高性能上传工具

替代 shell 脚本上传，解决三大瓶颈:
1. Python 直接读文件，替代 bash while read（快 100x）
2. 内存中批量构建 JSON，无需反复 fork python 进程
3. 大批量发送（默认 5000 条/批） + gzip 压缩

用法:
  python3 wal_upload.py --register                # 注册节点
  python3 wal_upload.py --file /path/wal.log      # 上传文件
  python3 wal_upload.py --daemon                   # 守护模式（增量上传）
  python3 wal_upload.py --status                   # 查看状态
"""

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import signal
import socket
import sys
import time
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================
SERVER_URL     = os.environ.get('LOG_SERVER_URL', 'http://localhost:8080')
STATE_DIR      = os.environ.get('STATE_DIR', os.path.expanduser('~/.walcollector'))
BATCH_SIZE     = int(os.environ.get('BATCH_SIZE', '50000'))
WAL_FILE       = os.environ.get('WAL_LOG_FILE', '/var/log/wal/wal.log')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '30'))
WORKERS        = int(os.environ.get('UPLOAD_WORKERS', '4'))
UPLOAD_TIMEOUT = int(os.environ.get('UPLOAD_TIMEOUT', '120'))
MAX_RETRY      = int(os.environ.get('MAX_RETRY', '3'))


# WAL 解析正则（与服务端保持一致）
WAL_PATTERN = re.compile(
    r'dump_stream_(?P<stream>[\d_]+):.*?'
    r'plsn:(?P<plsn>\d+);.*?'
    r'xid:\s*\((?P<xid_h>\d+),\s*(?P<xid_l>\d+)\);.*?'
    r'type:(?P<type>\w+);.*?'
    r'pageId=\((?P<pg1>\d+),\s*(?P<pg2>\d+)\);.*?'
    r'page prev glsn:(?P<prev_glsn>\d+)',
    re.IGNORECASE
)


def parse_wal_line(line):
    """客户端预解析 WAL 行，减少服务端 CPU 压力"""
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
    return {'stream': None, 'plsn': None, 'xid': None, 'type': None,
            'page_id': None, 'page_prev_glsn': None, 'message': line}


# ==================== 工具 ====================
def get_hostname():
    return socket.getfqdn()


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'unknown'


def post_json(path, data, compress=True):
    """发送 JSON 请求，支持 gzip 压缩 + 指数退避重试"""
    url = f'{SERVER_URL}{path}'
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if compress and len(body) > 1024:
        body = gzip.compress(body)
        headers['Content-Encoding'] = 'gzip'
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')

    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                return result
        except urllib.error.HTTPError as e:
            if e.code == 400:
                # 客户端错误，不重试
                return {'error': f'HTTP {e.code}: {e.reason}'}
            if e.code == 429 or e.code >= 500:
                wait = min(2 ** attempt, 30)
                print(f'\r[WARN] HTTP {e.code}，{wait}s 后重试 (attempt {attempt+1}/{MAX_RETRY})')
                time.sleep(wait)
        except urllib.error.URLError as e:
            wait = min(2 ** attempt, 30)
            print(f'\r[WARN] 网络错误: {e}，{wait}s 后重试 (attempt {attempt+1}/{MAX_RETRY})')
            time.sleep(wait)

    return {'error': f'上传失败，已重试 {MAX_RETRY} 次'}


def fmt_num(n):
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.1f}K'
    return str(n)


def fmt_time(seconds):
    if seconds < 60:
        return f'{seconds:.0f}s'
    if seconds < 3600:
        return f'{seconds / 60:.1f}min'
    return f'{seconds / 3600:.1f}h'


# ==================== 状态管理 ====================
def init_state():
    os.makedirs(STATE_DIR, exist_ok=True)


def get_server_id():
    id_file = os.path.join(STATE_DIR, 'server_id')
    if os.path.exists(id_file):
        return open(id_file).read().strip()
    print('[ERROR] 未注册，请先运行: python3 wal_upload.py --register')
    sys.exit(1)


def register_server():
    print('[INFO] 注册服务器...')
    resp = post_json('/api/register', {
        'hostname': get_hostname(),
        'ip': get_ip(),
        'os_info': platform.system(),
    }, compress=False)
    sid = resp.get('server_id')
    if sid:
        with open(os.path.join(STATE_DIR, 'server_id'), 'w') as f:
            f.write(sid)
        print(f'[OK] 注册成功 - ID: {sid}')
    else:
        print(f'[ERROR] 注册失败: {resp}')
        sys.exit(1)


# ==================== 上传核心 ====================
def upload_batch(server_id, hostname, records):
    """上传一批预解析的结构化记录"""
    resp = post_json('/api/upload', {
        'server_id': server_id,
        'hostname': hostname,
        'log_type': 'wal',
        'records': records,
    })
    # 新服务端返回 'buffered'，旧版返回 'inserted'，兼容两者
    count = resp.get('buffered', resp.get('inserted', 0))
    return resp.get('success', False), count, resp.get('error', '')


def upload_file(filepath, server_id, start_pos=0):
    """
    高性能文件上传
    - 客户端预解析 WAL（省去服务端正则开销）
    - WORKERS 个线程并发发送批次（IO 并行，绕开 GIL）
    - 主线程负责读文件 + 解析，工作线程负责网络发送
    - 返回 (已上传行数, 结束字节位置)
    """
    hostname = get_hostname()
    file_size = os.path.getsize(filepath)

    if start_pos >= file_size:
        return 0, start_pos

    # 用列表做线程安全的可变计数器
    counters = {'sent': 0, 'failed_batches': 0}
    counter_lock = threading.Lock()
    t0 = time.time()
    last_report = [t0]

    def send_fn(records):
        ok, inserted, err = upload_batch(server_id, hostname, records)
        with counter_lock:
            if ok:
                counters['sent'] += inserted
            else:
                counters['failed_batches'] += 1
                print(f'\r[WARN] 批次失败: {err}')

    def print_progress(pos):
        now = time.time()
        if now - last_report[0] < 2:
            return
        last_report[0] = now
        elapsed = now - t0
        speed = counters['sent'] / elapsed if elapsed > 0 else 0
        pct = (pos - start_pos) / (file_size - start_pos) * 100 if file_size > start_pos else 100
        eta = (file_size - pos) / ((pos - start_pos) / elapsed) if pos > start_pos and elapsed > 0 else 0
        print(f'\r[进度] {pct:.1f}% | 已发送 {fmt_num(counters["sent"])} 条 | '
              f'速度 {fmt_num(int(speed))} 条/s | 剩余 {fmt_time(eta)} | '
              f'并发 {WORKERS} 线程', end='', flush=True)

    batch = []
    pending = []

    with open(filepath, 'r', errors='replace') as f:
        if start_pos > 0:
            f.seek(start_pos)

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            while True:
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                batch.append(parse_wal_line(stripped))

                if len(batch) >= BATCH_SIZE:
                    pending.append(executor.submit(send_fn, batch))
                    batch = []

                    # 收割已完成的 future，避免列表无限增长
                    pending = [fut for fut in pending if not fut.done()]

                    # 背压控制：积压超过 WORKERS*2 时等待，防止内存撑爆
                    while len(pending) >= WORKERS * 2:
                        time.sleep(0.02)
                        pending = [fut for fut in pending if not fut.done()]

                    print_progress(f.tell())

            # 发送剩余不足一批的记录
            if batch:
                pending.append(executor.submit(send_fn, batch))

            # 等待所有批次完成
            for fut in pending:
                fut.result()

        end_pos = f.tell()

    elapsed = time.time() - t0
    speed = counters['sent'] / elapsed if elapsed > 0 else 0
    fb = counters['failed_batches']
    print(f'\r[OK] 完成: 发送 {fmt_num(counters["sent"])} 条 | 耗时 {fmt_time(elapsed)} | '
          f'速度 {fmt_num(int(speed))} 条/s' + (f' | 失败批次 {fb}' if fb else ''))

    return counters['sent'], end_pos


def upload_file_incremental(filepath, server_id):
    """增量上传，记录文件位置"""
    if not os.path.exists(filepath):
        return 0

    file_hash = hashlib.md5(filepath.encode()).hexdigest()
    pos_file = os.path.join(STATE_DIR, f'{file_hash}.pos')

    start_pos = 0
    if os.path.exists(pos_file):
        try:
            start_pos = int(open(pos_file).read().strip())
        except ValueError:
            start_pos = 0

    file_size = os.path.getsize(filepath)
    if start_pos > file_size:
        start_pos = 0
    if start_pos == file_size:
        print('[INFO] 无新日志')
        return 0

    print(f'[INFO] 增量上传: {filepath} (从 {fmt_num(start_pos)} 字节)')
    sent, end_pos = upload_file(filepath, server_id, start_pos)

    with open(pos_file, 'w') as f:
        f.write(str(end_pos))

    return sent


# ==================== 守护模式 ====================
def daemon_mode(filepath, server_id):
    print(f'[INFO] 守护模式启动 (间隔: {CHECK_INTERVAL}s)')
    print(f'[INFO] 监控文件: {filepath}')

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        print('\n[INFO] 停止')
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        if os.path.exists(filepath):
            upload_file_incremental(filepath, server_id)
        time.sleep(CHECK_INTERVAL)


# ==================== 状态查看 ====================
def show_status():
    print('=== WAL 上传状态 ===')
    print(f'服务器: {SERVER_URL}')
    print(f'WAL文件: {WAL_FILE}')
    print(f'批量大小: {BATCH_SIZE}')

    id_file = os.path.join(STATE_DIR, 'server_id')
    if os.path.exists(id_file):
        print(f'节点ID: {open(id_file).read().strip()}')
        print('状态: 已注册')
    else:
        print('状态: 未注册')

    try:
        req = urllib.request.Request(f'{SERVER_URL}/api/stats', method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            print('[OK] 服务器连接正常')
    except Exception:
        print('[WARN] 无法连接服务器')


# ==================== 主程序 ====================
def main():
    parser = argparse.ArgumentParser(description='WAL 日志高性能上传工具')
    parser.add_argument('--register', action='store_true', help='注册服务器')
    parser.add_argument('--file', type=str, help='上传指定文件')
    parser.add_argument('--daemon', action='store_true', help='守护进程模式')
    parser.add_argument('--status', action='store_true', help='查看状态')
    parser.add_argument('--batch-size', type=int, help='批量大小 (默认: 5000)')
    args = parser.parse_args()

    if args.batch_size:
        global BATCH_SIZE
        BATCH_SIZE = args.batch_size

    init_state()

    if args.register:
        register_server()
    elif args.file:
        sid = get_server_id()
        if not os.path.exists(args.file):
            print(f'[ERROR] 文件不存在: {args.file}')
            sys.exit(1)
        fsize = os.path.getsize(args.file)
        print(f'[INFO] 上传: {args.file} ({fmt_num(fsize)} 字节, 批量={BATCH_SIZE})')
        upload_file(args.file, sid)
    elif args.daemon:
        sid = get_server_id()
        daemon_mode(WAL_FILE, sid)
    elif args.status:
        show_status()
    else:
        show_status()


if __name__ == '__main__':
    main()
