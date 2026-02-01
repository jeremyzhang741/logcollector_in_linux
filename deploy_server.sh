#!/bin/bash
#
# LogVista 服务端一键部署脚本
# 支持 Ubuntu/Debian, CentOS/RHEL, macOS
#
# 使用方法:
#   sudo bash deploy_server.sh
#

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检测操作系统
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            OS=$ID
            VER=$VERSION_ID
        elif [ -f /etc/redhat-release ]; then
            OS="centos"
        else
            OS="unknown"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
    else
        OS="unknown"
    fi

    log_info "检测到操作系统: $OS"
}

# 检查是否以 root 运行（Linux）
check_root() {
    if [[ "$OS" != "macos" ]] && [[ $EUID -ne 0 ]]; then
        log_error "此脚本需要 root 权限运行"
        log_info "请使用: sudo bash $0"
        exit 1
    fi
}

# 安装 PostgreSQL
install_postgresql() {
    log_info "开始安装 PostgreSQL..."

    case $OS in
        ubuntu|debian)
            apt-get update
            apt-get install -y postgresql postgresql-contrib
            systemctl start postgresql
            systemctl enable postgresql
            ;;
        centos|rhel|fedora)
            if command -v dnf &> /dev/null; then
                dnf install -y postgresql-server postgresql-contrib
            else
                yum install -y postgresql-server postgresql-contrib
            fi
            postgresql-setup --initdb 2>/dev/null || postgresql-setup initdb
            systemctl start postgresql
            systemctl enable postgresql
            ;;
        macos)
            if ! command -v brew &> /dev/null; then
                log_error "未找到 Homebrew，请先安装: https://brew.sh"
                exit 1
            fi
            brew install postgresql@14
            brew services start postgresql@14
            ;;
        *)
            log_error "不支持的操作系统: $OS"
            exit 1
            ;;
    esac

    log_success "PostgreSQL 安装完成"
}

# 检查 PostgreSQL 是否已安装
check_postgresql() {
    if command -v psql &> /dev/null; then
        log_info "PostgreSQL 已安装"
        return 0
    else
        log_warning "PostgreSQL 未安装"
        return 1
    fi
}

# 安装 Python3 和 pip
install_python() {
    log_info "检查 Python3 环境..."

    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version | awk '{print $2}')
        log_info "Python3 已安装: $PYTHON_VERSION"
    else
        log_info "开始安装 Python3..."
        case $OS in
            ubuntu|debian)
                apt-get install -y python3 python3-pip python3-dev
                ;;
            centos|rhel|fedora)
                if command -v dnf &> /dev/null; then
                    dnf install -y python3 python3-pip python3-devel
                else
                    yum install -y python3 python3-pip python3-devel
                fi
                ;;
            macos)
                brew install python3
                ;;
        esac
        log_success "Python3 安装完成"
    fi

    # 安装 pip
    if ! command -v pip3 &> /dev/null; then
        log_info "安装 pip3..."
        python3 -m ensurepip --upgrade
    fi
}

# 安装 Python 依赖
install_python_deps() {
    log_info "安装 Python 依赖..."

    # 安装编译依赖（psycopg2 需要）
    case $OS in
        ubuntu|debian)
            apt-get install -y libpq-dev gcc
            ;;
        centos|rhel|fedora)
            if command -v dnf &> /dev/null; then
                dnf install -y postgresql-devel gcc
            else
                yum install -y postgresql-devel gcc
            fi
            ;;
        macos)
            # macOS 通常已有必要的编译工具
            :
            ;;
    esac

    # 安装 psycopg2
    pip3 install psycopg2-binary

    log_success "Python 依赖安装完成"
}

# 配置 PostgreSQL 数据库
setup_database() {
    log_info "配置 PostgreSQL 数据库..."

    # 数据库配置
    DB_NAME="${DB_NAME:-logvista}"
    DB_USER="${DB_USER:-logvista}"
    DB_PASSWORD="${DB_PASSWORD:-logvista123}"

    # 根据操作系统选择 PostgreSQL 用户
    if [[ "$OS" == "macos" ]]; then
        PG_USER=$(whoami)
    else
        PG_USER="postgres"
    fi

    # 创建数据库和用户
    if [[ "$OS" == "macos" ]]; then
        # macOS
        psql postgres -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null || log_warning "用户 $DB_USER 可能已存在"
        psql postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || log_warning "数据库 $DB_NAME 可能已存在"
        psql postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
    else
        # Linux
        sudo -u $PG_USER psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null || log_warning "用户 $DB_USER 可能已存在"
        sudo -u $PG_USER psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || log_warning "数据库 $DB_NAME 可能已存在"
        sudo -u $PG_USER psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
    fi

    # 配置 pg_hba.conf 允许本地密码连接
    if [[ "$OS" != "macos" ]]; then
        PG_HBA_CONF=$(sudo -u $PG_USER psql -t -P format=unaligned -c 'SHOW hba_file;')
        if ! grep -q "host.*$DB_NAME.*$DB_USER.*md5" "$PG_HBA_CONF"; then
            echo "host    $DB_NAME    $DB_USER    127.0.0.1/32    md5" >> "$PG_HBA_CONF"
            echo "host    $DB_NAME    $DB_USER    ::1/128         md5" >> "$PG_HBA_CONF"
            systemctl reload postgresql
            log_info "已更新 pg_hba.conf"
        fi
    fi

    log_success "数据库配置完成"
    log_info "数据库名: $DB_NAME"
    log_info "用户名: $DB_USER"
    log_info "密码: $DB_PASSWORD"
}

# 创建环境配置文件
create_env_file() {
    log_info "创建环境配置文件..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ENV_FILE="$SCRIPT_DIR/.env"

    cat > "$ENV_FILE" <<EOF
# LogVista 服务端配置
DB_HOST=localhost
DB_PORT=5432
DB_NAME=${DB_NAME:-logvista}
DB_USER=${DB_USER:-logvista}
DB_PASSWORD=${DB_PASSWORD:-logvista123}
SERVER_HOST=0.0.0.0
SERVER_PORT=8080
EOF

    chmod 600 "$ENV_FILE"
    log_success "环境配置文件已创建: $ENV_FILE"
}

# 创建启动脚本
create_start_script() {
    log_info "创建启动脚本..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    START_SCRIPT="$SCRIPT_DIR/start_server.sh"

    cat > "$START_SCRIPT" <<'EOF'
#!/bin/bash
#
# LogVista 服务端启动脚本
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# 加载环境变量
if [ -f "$ENV_FILE" ]; then
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

# 启动服务器
cd "$SCRIPT_DIR"
python3 server.py
EOF

    chmod +x "$START_SCRIPT"
    log_success "启动脚本已创建: $START_SCRIPT"
}

# 创建 systemd 服务（仅 Linux）
create_systemd_service() {
    if [[ "$OS" == "macos" ]]; then
        log_info "macOS 不支持 systemd，跳过服务创建"
        return
    fi

    log_info "创建 systemd 服务..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SERVICE_FILE="/etc/systemd/system/logvista-server.service"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=LogVista Log Collection Server
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=/usr/bin/python3 $SCRIPT_DIR/server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log_success "systemd 服务已创建: $SERVICE_FILE"
    log_info "启动服务: systemctl start logvista-server"
    log_info "开机自启: systemctl enable logvista-server"
}

# 测试数据库连接
test_database_connection() {
    log_info "测试数据库连接..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # 加载环境变量
    if [ -f "$SCRIPT_DIR/.env" ]; then
        export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
    fi

    # 使用 Python 测试连接
    python3 - <<PYEOF
import psycopg2
import os

try:
    conn = psycopg2.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', 5432)),
        database=os.environ.get('DB_NAME', 'logvista'),
        user=os.environ.get('DB_USER', 'logvista'),
        password=os.environ.get('DB_PASSWORD', 'logvista123')
    )
    conn.close()
    print("✅ 数据库连接成功")
except Exception as e:
    print(f"❌ 数据库连接失败: {e}")
    exit(1)
PYEOF

    if [ $? -eq 0 ]; then
        log_success "数据库连接测试通过"
    else
        log_error "数据库连接测试失败"
        exit 1
    fi
}

# 初始化数据库表
init_database_tables() {
    log_info "初始化数据库表..."

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # 加载环境变量
    if [ -f "$SCRIPT_DIR/.env" ]; then
        export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
    fi

    # 使用 Python 初始化表
    python3 - <<'PYEOF'
import psycopg2
import os

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'database': os.environ.get('DB_NAME', 'logvista'),
    'user': os.environ.get('DB_USER', 'logvista'),
    'password': os.environ.get('DB_PASSWORD', 'logvista123'),
}

try:
    conn = psycopg2.connect(**DB_CONFIG)
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
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_level ON log_entries(level)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_type ON log_entries(log_type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_entries_message ON log_entries USING gin(to_tsvector(\'simple\', message))')

    conn.commit()
    print("✅ 数据库表初始化完成")

    cursor.close()
    conn.close()
except Exception as e:
    print(f"❌ 数据库表初始化失败: {e}")
    exit(1)
PYEOF

    if [ $? -eq 0 ]; then
        log_success "数据库表初始化成功"
    else
        log_error "数据库表初始化失败"
        exit 1
    fi
}

# 显示部署完成信息
show_completion_info() {
    echo ""
    echo "========================================"
    log_success "LogVista 服务端部署完成！"
    echo "========================================"
    echo ""
    log_info "数据库信息:"
    echo "  - 数据库名: ${DB_NAME:-logvista}"
    echo "  - 用户名: ${DB_USER:-logvista}"
    echo "  - 密码: ${DB_PASSWORD:-logvista123}"
    echo ""
    log_info "启动服务器:"

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ "$OS" == "macos" ]]; then
        echo "  bash $SCRIPT_DIR/start_server.sh"
    else
        echo "  方式1（手动启动）: bash $SCRIPT_DIR/start_server.sh"
        echo "  方式2（systemd）: systemctl start logvista-server"
        echo "  开机自启: systemctl enable logvista-server"
    fi

    echo ""
    log_info "访问仪表板:"
    echo "  http://localhost:8080/dashboard"
    echo "  http://$(hostname -I | awk '{print $1}' 2>/dev/null || echo "YOUR_IP"):8080/dashboard"
    echo ""
    log_info "配置文件:"
    echo "  - 环境配置: $SCRIPT_DIR/.env"
    echo "  - 服务端代码: $SCRIPT_DIR/server.py"
    echo ""
    log_warning "安全提示:"
    echo "  - 请修改 .env 中的数据库密码"
    echo "  - 生产环境请启用防火墙并限制访问"
    echo "  - 建议配置反向代理（Nginx/Apache）"
    echo ""
}

# 主函数
main() {
    echo "========================================"
    echo "   LogVista 服务端一键部署脚本"
    echo "========================================"
    echo ""

    # 检测操作系统
    detect_os

    # 检查 root 权限
    check_root

    # 询问是否安装 PostgreSQL
    if ! check_postgresql; then
        read -p "是否安装 PostgreSQL? [Y/n] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
            install_postgresql
        else
            log_error "PostgreSQL 是必需的，退出安装"
            exit 1
        fi
    fi

    # 等待 PostgreSQL 启动
    log_info "等待 PostgreSQL 启动..."
    sleep 3

    # 安装 Python
    install_python

    # 安装 Python 依赖
    install_python_deps

    # 配置数据库
    read -p "是否配置数据库? [Y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        # 获取数据库配置
        read -p "数据库名 [logvista]: " input_db_name
        DB_NAME=${input_db_name:-logvista}

        read -p "数据库用户 [logvista]: " input_db_user
        DB_USER=${input_db_user:-logvista}

        read -sp "数据库密码 [logvista123]: " input_db_password
        echo
        DB_PASSWORD=${input_db_password:-logvista123}

        setup_database
        create_env_file
        test_database_connection
        init_database_tables
    fi

    # 创建启动脚本
    create_start_script

    # 创建 systemd 服务
    if [[ "$OS" != "macos" ]]; then
        read -p "是否创建 systemd 服务? [Y/n] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
            create_systemd_service
        fi
    fi

    # 显示完成信息
    show_completion_info
}

# 执行主函数
main
