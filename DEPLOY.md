# LogVista 服务端一键部署指南

## 快速开始

### 1. 执行部署脚本

在服务器上运行以下命令：

```bash
sudo bash deploy_server.sh
```

### 2. 脚本会自动完成以下操作

- ✅ 检测操作系统类型（Ubuntu/Debian/CentOS/RHEL/macOS）
- ✅ 安装 PostgreSQL 数据库
- ✅ 安装 Python 3 和依赖包（psycopg2）
- ✅ 创建数据库和用户
- ✅ 初始化数据库表结构
- ✅ 创建环境配置文件（.env）
- ✅ 创建启动脚本（start_server.sh）
- ✅ 创建 systemd 服务（仅 Linux）

### 3. 安装过程中的交互

脚本会提示您输入以下信息：

```
数据库名 [logvista]:
数据库用户 [logvista]:
数据库密码 [logvista123]:
```

直接按回车使用默认值，或输入自定义值。

## 启动服务器

### 方式一：手动启动

```bash
bash start_server.sh
```

### 方式二：使用 systemd（推荐，仅 Linux）

```bash
# 启动服务
sudo systemctl start logvista-server

# 查看状态
sudo systemctl status logvista-server

# 开机自启
sudo systemctl enable logvista-server

# 查看日志
sudo journalctl -u logvista-server -f
```

### 方式三：后台运行

```bash
nohup bash start_server.sh > logvista.log 2>&1 &
```

## 访问仪表板

部署完成后，在浏览器中访问：

- 本地访问: http://localhost:8080/dashboard
- 远程访问: http://YOUR_SERVER_IP:8080/dashboard

## 配置文件说明

### .env - 环境配置文件

```bash
DB_HOST=localhost           # 数据库主机
DB_PORT=5432               # 数据库端口
DB_NAME=logvista           # 数据库名
DB_USER=logvista           # 数据库用户
DB_PASSWORD=logvista123    # 数据库密码
SERVER_HOST=0.0.0.0        # 服务器监听地址
SERVER_PORT=8080           # 服务器端口
```

修改配置后需要重启服务。

## 验证安装

### 1. 检查 PostgreSQL 服务

```bash
# Ubuntu/Debian
sudo systemctl status postgresql

# CentOS/RHEL
sudo systemctl status postgresql

# macOS
brew services list | grep postgresql
```

### 2. 测试数据库连接

```bash
psql -h localhost -U logvista -d logvista
```

### 3. 测试 API 接口

```bash
# 获取统计信息
curl http://localhost:8080/api/stats

# 获取服务器列表
curl http://localhost:8080/api/servers
```

## 防火墙配置

### Ubuntu/Debian (ufw)

```bash
sudo ufw allow 8080/tcp
sudo ufw reload
```

### CentOS/RHEL (firewalld)

```bash
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

## 常见问题

### 1. PostgreSQL 连接失败

**问题**: `psycopg2.OperationalError: could not connect to server`

**解决方案**:
```bash
# 检查 PostgreSQL 状态
sudo systemctl status postgresql

# 重启 PostgreSQL
sudo systemctl restart postgresql

# 检查 pg_hba.conf 配置
sudo vim /etc/postgresql/*/main/pg_hba.conf
# 添加: host logvista logvista 127.0.0.1/32 md5
```

### 2. 端口被占用

**问题**: `Address already in use`

**解决方案**:
```bash
# 查看占用端口的进程
sudo lsof -i :8080

# 修改 .env 中的 SERVER_PORT
vim .env
```

### 3. 权限错误

**问题**: `Permission denied`

**解决方案**:
```bash
# 使用 sudo 运行部署脚本
sudo bash deploy_server.sh

# 检查文件权限
ls -la deploy_server.sh
chmod +x deploy_server.sh
```

### 4. Python 依赖安装失败

**问题**: `psycopg2` 安装失败

**解决方案**:
```bash
# Ubuntu/Debian
sudo apt-get install -y python3-dev libpq-dev gcc

# CentOS/RHEL
sudo yum install -y python3-devel postgresql-devel gcc

# 重新安装
pip3 install psycopg2-binary
```

## 安全加固

### 1. 修改默认密码

```bash
# 编辑 .env 文件
vim .env

# 修改数据库密码
psql -U postgres
ALTER USER logvista WITH PASSWORD 'new_strong_password';
```

### 2. 限制访问 IP

修改 `server.py` 中的 `HOST` 配置：

```bash
# 仅本地访问
export SERVER_HOST=127.0.0.1

# 特定网段访问（需要配合防火墙）
export SERVER_HOST=0.0.0.0
```

### 3. 配置反向代理（推荐）

使用 Nginx 作为反向代理：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### 4. 启用 SSL/TLS

```bash
# 使用 Let's Encrypt
sudo certbot --nginx -d your-domain.com
```

## 卸载

```bash
# 停止服务
sudo systemctl stop logvista-server
sudo systemctl disable logvista-server

# 删除 systemd 服务
sudo rm /etc/systemd/system/logvista-server.service
sudo systemctl daemon-reload

# 删除数据库
sudo -u postgres psql -c "DROP DATABASE logvista;"
sudo -u postgres psql -c "DROP USER logvista;"

# 删除文件
rm -rf /path/to/logcollector_in_linux
```

## 技术支持

如遇到问题，请检查：

1. 系统日志: `sudo journalctl -u logvista-server -f`
2. 应用日志: `tail -f logvista.log`
3. PostgreSQL 日志: `/var/log/postgresql/`

## 下一步

- 配置客户端: 使用 `upload_logs.sh` 脚本
- 配置告警: 自定义告警规则
- 数据备份: 定期备份 PostgreSQL 数据库
