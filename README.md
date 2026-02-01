# LogVista - Linux日志收集与分析平台

一个轻量级的Linux系统日志收集、分析和可视化平台。使用PostgreSQL存储，支持多服务器日志聚合、实时监控、统计分析等功能。

## 🌟 功能特性

### 服务端
- 📊 实时日志统计仪表板
- 🔍 全文搜索和过滤（PostgreSQL ILIKE）
- 📈 日志趋势分析
- 🖥️ 多服务器管理
- ⚠️ 错误/警告告警
- 🚀 PostgreSQL数据库存储
- 🔄 连接池管理

### 客户端
- 📤 批量日志上传
- 🔄 增量同步（只上传新日志）
- 👀 实时跟踪模式
- 🤖 守护进程模式
- 📁 支持多种日志类型

## 📁 项目结构

```
log-platform/
├── server/
│   ├── server.py          # 后端服务器 (PostgreSQL)
│   └── dashboard.html     # Web仪表板
├── client/
│   ├── upload_logs.sh     # 日志上传脚本
│   └── logvista.service   # systemd服务文件
└── README.md              # 本文档
```

## 🚀 快速开始

### 1. 安装和配置PostgreSQL数据库

#### Ubuntu/Debian 系统

```bash
# 1. 更新包列表
sudo apt-get update

# 2. 安装PostgreSQL
sudo apt-get install -y postgresql postgresql-contrib

# 3. 启动PostgreSQL服务
sudo systemctl start postgresql
sudo systemctl enable postgresql  # 设置开机自启

# 4. 检查服务状态
sudo systemctl status postgresql
```

#### CentOS/RHEL/Rocky Linux 系统

```bash
# 1. 安装PostgreSQL
sudo dnf install -y postgresql-server postgresql-contrib

# 2. 初始化数据库
sudo postgresql-setup --initdb

# 3. 启动服务
sudo systemctl start postgresql
sudo systemctl enable postgresql

# 4. 检查状态
sudo systemctl status postgresql
```

#### macOS 系统

```bash
# 使用Homebrew安装
brew install postgresql@15

# 启动服务
brew services start postgresql@15

# 或手动启动
pg_ctl -D /usr/local/var/postgres start
```

#### Docker 方式（推荐用于测试）

```bash
# 快速启动PostgreSQL容器
docker run -d \
  --name logvista-postgres \
  -e POSTGRES_DB=logvista \
  -e POSTGRES_USER=logvista \
  -e POSTGRES_PASSWORD=your_password \
  -p 5432:5432 \
  -v pgdata:/var/lib/postgresql/data \
  postgres:15

# 检查容器状态
docker ps | grep logvista-postgres

# 查看日志
docker logs logvista-postgres
```

#### 创建数据库和用户

```bash
# 切换到postgres用户
sudo -u postgres psql

# 在psql中执行以下SQL命令：
```

```sql
-- 创建数据库
CREATE DATABASE logvista;

-- 创建用户（请修改密码）
CREATE USER logvista WITH PASSWORD 'your_secure_password';

-- 授予权限
GRANT ALL PRIVILEGES ON DATABASE logvista TO logvista;

-- PostgreSQL 15+ 需要额外授权
\c logvista
GRANT ALL ON SCHEMA public TO logvista;

-- 退出
\q
```

#### 配置远程访问（可选，生产环境按需配置）

```bash
# 1. 编辑 postgresql.conf，允许监听所有地址
sudo vim /etc/postgresql/15/main/postgresql.conf
# 找到并修改：
# listen_addresses = '*'

# 2. 编辑 pg_hba.conf，允许远程连接
sudo vim /etc/postgresql/15/main/pg_hba.conf
# 添加以下行（根据实际网段修改）：
# host    logvista    logvista    10.0.0.0/8       scram-sha-256
# host    logvista    logvista    192.168.0.0/16   scram-sha-256

# 3. 重启PostgreSQL
sudo systemctl restart postgresql
```

#### 验证安装

```bash
# 测试本地连接
psql -h localhost -U logvista -d logvista -c "SELECT version();"

# 如果提示输入密码，输入创建用户时设置的密码
# 成功后会显示PostgreSQL版本信息
```

#### 常见问题排查

```bash
# 查看PostgreSQL日志
sudo tail -f /var/log/postgresql/postgresql-15-main.log

# 检查端口是否监听
sudo ss -tlnp | grep 5432

# 检查防火墙（如果远程访问）
sudo ufw allow 5432/tcp  # Ubuntu
sudo firewall-cmd --add-port=5432/tcp --permanent  # CentOS
```

### 2. 部署服务端

#### 系统要求
- Python 3.8+
- PostgreSQL 12+
- psycopg2

#### 安装步骤

```bash
# 1. 创建目录
mkdir -p /opt/logvista/server
cd /opt/logvista/server

# 2. 安装Python依赖
pip3 install psycopg2-binary

# 3. 复制服务端文件
cp server.py dashboard.html ./

# 4. 设置数据库连接环境变量
export DB_HOST="localhost"
export DB_PORT="5432"
export DB_NAME="logvista"
export DB_USER="logvista"
export DB_PASSWORD="your_password"

# 5. 启动服务器
python3 server.py
```

服务器将在 `http://0.0.0.0:8080` 启动。

#### 使用 Docker Compose 部署（推荐）

```yaml
# docker-compose.yml
version: '3.8'

services:
  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: logvista
      POSTGRES_USER: logvista
      POSTGRES_PASSWORD: your_password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  logvista:
    build: ./server
    ports:
      - "8080:8080"
    environment:
      DB_HOST: postgres
      DB_PORT: 5432
      DB_NAME: logvista
      DB_USER: logvista
      DB_PASSWORD: your_password
    depends_on:
      - postgres

volumes:
  postgres_data:
```

```dockerfile
# server/Dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN pip install psycopg2-binary

COPY server.py dashboard.html ./

EXPOSE 8080

CMD ["python3", "server.py"]
```

```bash
# 启动服务
docker-compose up -d
```

#### 使用 systemd 管理

```ini
# /etc/systemd/system/logvista-server.service
[Unit]
Description=LogVista Log Analysis Server
After=network.target postgresql.service

[Service]
Type=simple
User=logvista
WorkingDirectory=/opt/logvista/server
ExecStart=/usr/bin/python3 server.py
Restart=always
Environment="DB_HOST=localhost"
Environment="DB_PORT=5432"
Environment="DB_NAME=logvista"
Environment="DB_USER=logvista"
Environment="DB_PASSWORD=your_password"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable logvista-server
sudo systemctl start logvista-server
```

### 3. 部署客户端

在每台需要收集日志的Linux服务器上执行：

```bash
# 1. 安装依赖
sudo apt-get update
sudo apt-get install -y curl jq

# 2. 创建目录并复制脚本
sudo mkdir -p /opt/logvista
sudo cp upload_logs.sh /opt/logvista/
sudo chmod +x /opt/logvista/upload_logs.sh
cd /opt/logvista

# 3. 设置服务器地址
export LOG_SERVER_URL="http://your-server:8080"

# 4. 注册服务器
./upload_logs.sh --register

# 5. 测试上传
./upload_logs.sh --all
```

#### 配置持久化

```bash
# /etc/logvista/config
LOG_SERVER_URL=http://your-server:8080
```

#### 安装为系统服务

```bash
# 1. 复制服务文件
sudo cp logvista.service /etc/systemd/system/

# 2. 编辑服务文件，设置正确的服务器地址
sudo vim /etc/systemd/system/logvista.service

# 3. 启用服务
sudo systemctl daemon-reload
sudo systemctl enable logvista
sudo systemctl start logvista

# 4. 检查状态
sudo systemctl status logvista
sudo journalctl -u logvista -f
```

## 📖 使用指南

### 客户端命令

```bash
# 显示帮助
./upload_logs.sh --help

# 注册服务器（首次使用）
./upload_logs.sh --register

# 查看当前状态
./upload_logs.sh --status

# 上传所有默认日志
./upload_logs.sh --all

# 上传指定日志文件
./upload_logs.sh /var/log/nginx/access.log

# 指定日志类型上传
./upload_logs.sh --type app /var/log/myapp.log

# 实时跟踪模式
./upload_logs.sh --tail /var/log/syslog

# 守护进程模式（持续监控）
./upload_logs.sh --daemon
```

### API 接口

#### 注册服务器
```bash
curl -X POST http://server:8080/api/register \
  -H "Content-Type: application/json" \
  -d '{"hostname": "web-server-01", "ip": "192.168.1.10"}'
```

响应:
```json
{
  "success": true,
  "server_id": "a1b2c3d4e5f67890",
  "message": "Server registered successfully"
}
```

#### 上传日志
```bash
curl -X POST http://server:8080/api/upload \
  -H "Content-Type: application/json" \
  -d '{
    "server_id": "a1b2c3d4e5f67890",
    "hostname": "web-server-01",
    "log_type": "syslog",
    "logs": [
      "Feb  1 10:23:45 web-server-01 sshd[1234]: Accepted publickey for user",
      "Feb  1 10:24:01 web-server-01 cron[5678]: (root) CMD (/usr/bin/backup)"
    ]
  }'
```

#### 查询接口
```bash
# 获取统计数据
curl "http://server:8080/api/stats?hours=24"

# 获取日志列表
curl "http://server:8080/api/logs?level=error&limit=50"

# 搜索日志
curl "http://server:8080/api/logs?search=failed"

# 获取服务器列表
curl "http://server:8080/api/servers"

# 获取告警
curl "http://server:8080/api/alerts?hours=24"

# 获取趋势数据
curl "http://server:8080/api/trends?hours=24"
```

### 支持的日志类型

| 类型 | 描述 | 默认文件 |
|------|------|----------|
| syslog | 系统日志 | /var/log/syslog, /var/log/messages |
| auth | 认证日志 | /var/log/auth.log, /var/log/secure |
| kern | 内核日志 | /var/log/kern.log, /var/log/dmesg |
| app | 应用日志 | 自定义 |

### 日志级别

- `emerg` - 系统不可用
- `alert` - 必须立即处理
- `crit` - 严重错误
- `error` - 错误
- `warning` - 警告
- `notice` - 重要通知
- `info` - 信息
- `debug` - 调试

## 🔧 配置说明

### 服务端环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| DB_HOST | localhost | PostgreSQL主机地址 |
| DB_PORT | 5432 | PostgreSQL端口 |
| DB_NAME | logvista | 数据库名称 |
| DB_USER | postgres | 数据库用户 |
| DB_PASSWORD | postgres | 数据库密码 |
| SERVER_HOST | 0.0.0.0 | HTTP服务监听地址 |
| SERVER_PORT | 8080 | HTTP服务端口 |

### 客户端环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| LOG_SERVER_URL | http://localhost:8080 | 日志服务器地址 |

### 客户端配置（脚本内）

```bash
BATCH_SIZE=100        # 批量上传大小
CHECK_INTERVAL=30     # 守护进程检查间隔（秒）
```

## 🔒 安全建议

⚠️ **注意**: 此版本已移除API密钥认证，请确保在安全的网络环境中使用。

1. **使用 HTTPS**: 在生产环境中使用 Nginx/Caddy 反向代理并配置 SSL
2. **网络隔离**: 将日志服务器部署在内网，不暴露到公网
3. **防火墙**: 限制只允许可信IP访问
4. **数据库安全**: 使用强密码，限制数据库访问来源

### Nginx 反向代理配置（带SSL）

```nginx
server {
    listen 443 ssl;
    server_name logs.example.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    # 限制访问来源
    allow 10.0.0.0/8;
    allow 192.168.0.0/16;
    deny all;
    
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 📊 数据库表结构

### log_entries
```sql
CREATE TABLE log_entries (
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
);
```

### servers
```sql
CREATE TABLE servers (
    id VARCHAR(64) PRIMARY KEY,
    hostname VARCHAR(255),
    ip_address VARCHAR(45),
    os_info TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_logs BIGINT DEFAULT 0
);
```

## 🐛 故障排除

### 数据库连接失败
```bash
# 检查PostgreSQL服务
sudo systemctl status postgresql

# 测试连接
psql -h localhost -U logvista -d logvista

# 检查pg_hba.conf配置
sudo vim /etc/postgresql/15/main/pg_hba.conf
```

### 客户端无法连接服务器
```bash
# 检查网络连通性
curl -v http://server:8080/api/stats

# 查看状态
./upload_logs.sh --status
```

### 权限不足
```bash
# 添加读取日志权限
sudo usermod -a -G adm $USER
# 或使用 root 运行
sudo ./upload_logs.sh --all
```

## 📈 性能优化

### PostgreSQL优化
```sql
-- 定期清理旧日志
DELETE FROM log_entries WHERE received_at < NOW() - INTERVAL '30 days';

-- 分析表
ANALYZE log_entries;

-- 查看表大小
SELECT pg_size_pretty(pg_total_relation_size('log_entries'));
```

### 添加分区（大数据量推荐）
```sql
-- 按月分区示例
CREATE TABLE log_entries_partitioned (
    LIKE log_entries INCLUDING ALL
) PARTITION BY RANGE (received_at);

CREATE TABLE log_entries_2024_01 PARTITION OF log_entries_partitioned
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
```

## 📄 License

MIT License
