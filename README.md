# LogVista - Linux日志收集与分析平台

一个轻量级的Linux系统日志收集、分析和可视化平台。支持多服务器日志聚合、实时监控、统计分析等功能。

## 🌟 功能特性

### 服务端
- 📊 实时日志统计仪表板
- 🔍 全文搜索和过滤
- 📈 日志趋势分析
- 🖥️ 多服务器管理
- ⚠️ 错误/警告告警
- 🔐 API密钥认证

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
│   ├── server.py          # 后端服务器
│   └── dashboard.html     # Web仪表板
├── client/
│   ├── upload_logs.sh     # 日志上传脚本
│   └── logvista.service   # systemd服务文件
└── README.md              # 本文档
```

## 🚀 快速开始

### 1. 部署服务端

#### 系统要求
- Python 3.8+
- SQLite3

#### 安装步骤

```bash
# 1. 创建目录
mkdir -p /opt/logvista/server
cd /opt/logvista/server

# 2. 复制服务端文件
cp server.py dashboard.html ./

# 3. 设置管理员密码（可选）
export ADMIN_PASSWORD="your-secure-password"

# 4. 启动服务器
python3 server.py
```

服务器将在 `http://0.0.0.0:8080` 启动。

#### 使用 Docker 部署（推荐）

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY server.py dashboard.html ./

EXPOSE 8080

CMD ["python3", "server.py"]
```

```bash
# 构建并运行
docker build -t logvista-server .
docker run -d -p 8080:8080 -v logvista-data:/app/data logvista-server
```

#### 使用 systemd 管理

```ini
# /etc/systemd/system/logvista-server.service
[Unit]
Description=LogVista Log Analysis Server
After=network.target

[Service]
Type=simple
User=logvista
WorkingDirectory=/opt/logvista/server
ExecStart=/usr/bin/python3 server.py
Restart=always
Environment="ADMIN_PASSWORD=your-password"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable logvista-server
sudo systemctl start logvista-server
```

### 2. 部署客户端

#### 安装步骤

在每台需要收集日志的Linux服务器上执行：

```bash
# 1. 安装依赖
sudo apt-get update
sudo apt-get install -y curl jq

# 2. 创建目录
sudo mkdir -p /opt/logvista
cd /opt/logvista

# 3. 复制上传脚本
sudo cp upload_logs.sh ./
sudo chmod +x upload_logs.sh

# 4. 设置服务器地址
export LOG_SERVER_URL="http://your-server:8080"

# 5. 注册服务器获取API Key
./upload_logs.sh --register

# 6. 设置API Key
export LOG_API_KEY="your-api-key-from-registration"

# 7. 测试上传
./upload_logs.sh --all
```

#### 配置持久化

编辑 `/etc/environment` 或创建 `/etc/logvista/config`：

```bash
# /etc/logvista/config
LOG_SERVER_URL=http://your-server:8080
LOG_API_KEY=your-api-key
```

#### 安装为系统服务

```bash
# 1. 编辑服务文件，设置正确的环境变量
sudo vim /etc/systemd/system/logvista.service

# 2. 启用服务
sudo systemctl daemon-reload
sudo systemctl enable logvista
sudo systemctl start logvista

# 3. 检查状态
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

# 查看状态
./upload_logs.sh --status
```

### API 接口

#### 注册服务器
```bash
curl -X POST http://server:8080/api/register \
  -H "Content-Type: application/json" \
  -d '{"hostname": "web-server-01", "ip": "192.168.1.10"}'
```

#### 上传日志
```bash
curl -X POST http://server:8080/api/upload \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "hostname": "web-server-01",
    "log_type": "syslog",
    "logs": [
      "Feb  1 10:23:45 web-server-01 sshd[1234]: Accepted publickey for user",
      "Feb  1 10:24:01 web-server-01 cron[5678]: (root) CMD (/usr/bin/backup)"
    ]
  }'
```

#### 查询统计
```bash
# 获取统计数据
curl http://server:8080/api/stats?hours=24

# 获取日志列表
curl "http://server:8080/api/logs?level=error&limit=50"

# 搜索日志
curl "http://server:8080/api/logs?search=failed"

# 获取服务器列表
curl http://server:8080/api/servers

# 获取告警
curl http://server:8080/api/alerts?hours=24
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

### 服务端配置

编辑 `server.py` 头部的配置：

```python
DB_PATH = "logs.db"           # 数据库路径
API_KEYS_FILE = "api_keys.json"  # API密钥存储
HOST = "0.0.0.0"              # 监听地址
PORT = 8080                   # 监听端口
```

### 客户端配置

编辑 `upload_logs.sh` 头部的配置：

```bash
SERVER_URL="http://localhost:8080"  # 服务器地址
API_KEY=""                          # API密钥
BATCH_SIZE=100                      # 批量上传大小
CHECK_INTERVAL=30                   # 守护进程检查间隔
```

## 🔒 安全建议

1. **使用 HTTPS**: 在生产环境中使用 Nginx/Caddy 反向代理并配置 SSL
2. **限制访问**: 使用防火墙限制API访问来源
3. **定期轮换**: 定期更换API密钥
4. **日志脱敏**: 敏感信息在上传前进行脱敏处理

### Nginx 反向代理配置

```nginx
server {
    listen 443 ssl;
    server_name logs.example.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 📊 仪表板功能

访问 `http://server:8080/dashboard` 查看：

- **总览统计**: 日志总数、活跃服务器、警告/错误数量
- **实时日志流**: 最新日志实时展示
- **服务器状态**: 各服务器在线状态和日志数量
- **级别分布**: 各日志级别占比
- **服务统计**: 热门服务排行
- **类型分布**: 日志类型分布

## 🐛 故障排除

### 客户端无法连接服务器
```bash
# 检查网络连通性
curl -v http://server:8080/api/stats

# 检查防火墙
sudo iptables -L -n | grep 8080
```

### 权限不足
```bash
# 添加读取日志权限
sudo usermod -a -G adm $USER
# 或使用 root 运行
sudo ./upload_logs.sh --all
```

### 日志上传失败
```bash
# 检查API密钥
./upload_logs.sh --status

# 重新注册
./upload_logs.sh --register
```

## 📄 License

MIT License
