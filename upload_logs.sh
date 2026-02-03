#!/bin/bash
#
# WAL 日志上传脚本
# 用于将数据库 WAL 日志发送到日志分析平台
#
# 使用方法:
#   ./upload_logs.sh --register         # 注册服务器（首次使用）
#   ./upload_logs.sh /path/to/wal.log   # 上传指定 WAL 日志文件
#   ./upload_logs.sh --daemon           # 后台持续监控模式
#

set -e

# ==================== 配置区域 ====================
# 日志分析平台地址
SERVER_URL="${LOG_SERVER_URL:-http://localhost:8080}"

# WAL 日志文件路径（可通过环境变量配置）
WAL_LOG_FILE="${WAL_LOG_FILE:-/var/log/wal/wal.log}"

# 默认监控的 WAL 日志文件
DEFAULT_LOGS=(
    "${WAL_LOG_FILE}:wal"
)

# 批量上传大小（行数）
BATCH_SIZE=100

# 守护进程检查间隔（秒）
CHECK_INTERVAL=30

# 状态文件目录
STATE_DIR="${HOME}/.logvista"

# ==================== 颜色定义 ====================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ==================== 工具函数 ====================

log_info() {
    echo -e "${CYAN}[INFO]${NC} $1"
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

print_banner() {
    echo -e "${CYAN}"
    echo "╔══════════════════════════════════════════╗"
    echo "║       WAL 日志上传工具 v2.0              ║"
    echo "║   数据库 WAL 日志收集与分析平台          ║"
    echo "╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

check_dependencies() {
    local missing=()

    # 检查 curl 和 hostname
    for cmd in curl hostname; do
        if ! command -v $cmd &> /dev/null; then
            missing+=($cmd)
        fi
    done

    # 检查 Python（优先 python3，其次 python）
    if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
        missing+=("python3或python")
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "缺少必要依赖: ${missing[*]}"
        echo "请安装: sudo apt-get install ${missing[*]}"
        exit 1
    fi
}

init_state_dir() {
    mkdir -p "$STATE_DIR"
}

# 获取可用的 Python 命令
get_python_cmd() {
    if command -v python3 &> /dev/null; then
        echo "python3"
    elif command -v python &> /dev/null; then
        echo "python"
    else
        echo ""
    fi
}

get_hostname() {
    hostname -f 2>/dev/null || hostname
}

get_os_info() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$PRETTY_NAME"
    else
        uname -a
    fi
}

get_ip() {
    hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown"
}

# 获取或生成server_id
get_server_id() {
    local id_file="${STATE_DIR}/server_id"
    
    if [ -f "$id_file" ]; then
        cat "$id_file"
    else
        # 首次运行，需要注册
        log_warning "服务器尚未注册，请先运行: $0 --register"
        exit 1
    fi
}

# ==================== 核心功能 ====================

# 注册服务器
register_server() {
    log_info "正在注册服务器..."
    
    local hostname=$(get_hostname)
    local os_info=$(get_os_info)
    local ip=$(get_ip)
    
    local response=$(curl -s -X POST "${SERVER_URL}/api/register" \
        -H "Content-Type: application/json" \
        -d "{
            \"hostname\": \"${hostname}\",
            \"ip\": \"${ip}\",
            \"os_info\": \"${os_info}\"
        }")

    # 使用 Python 解析 JSON 响应
    local PYTHON=$(get_python_cmd)
    local success=$($PYTHON -c "import json,sys; data=json.loads('''$response'''); print('true' if data.get('success') else 'false')" 2>/dev/null)

    if [ "$success" = "true" ]; then
        local server_id=$($PYTHON -c "import json,sys; data=json.loads('''$response'''); print(data.get('server_id', ''))")
        
        log_success "服务器注册成功！"
        echo ""
        echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║${NC} 主机名:    ${YELLOW}${hostname}${NC}"
        echo -e "${GREEN}║${NC} 服务器ID:  ${YELLOW}${server_id}${NC}"
        echo -e "${GREEN}║${NC} IP地址:    ${YELLOW}${ip}${NC}"
        echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        
        # 保存server_id
        echo "$server_id" > "${STATE_DIR}/server_id"
        chmod 600 "${STATE_DIR}/server_id"
        log_info "服务器ID已保存到 ${STATE_DIR}/server_id"
        
        echo ""
        log_info "现在可以开始上传日志了:"
        echo "  $0 --all      # 上传所有默认日志"
        echo "  $0 --daemon   # 守护进程模式"
    else
        log_error "注册失败: $response"
        exit 1
    fi
}

# 获取文件的上次读取位置
get_file_position() {
    local file="$1"
    local state_file="${STATE_DIR}/$(echo "$file" | md5sum | cut -d' ' -f1).pos"
    
    if [ -f "$state_file" ]; then
        cat "$state_file"
    else
        echo "0"
    fi
}

# 保存文件的读取位置
save_file_position() {
    local file="$1"
    local position="$2"
    local state_file="${STATE_DIR}/$(echo "$file" | md5sum | cut -d' ' -f1).pos"
    
    echo "$position" > "$state_file"
}

# 上传日志内容
upload_logs() {
    local log_type="$1"
    local server_id="$2"
    local hostname=$(get_hostname)
    shift 2
    local logs=("$@")
    
    if [ ${#logs[@]} -eq 0 ]; then
        return 0
    fi

    # 使用 Python 构建 JSON payload
    local PYTHON=$(get_python_cmd)
    local payload=$($PYTHON -c "
import json
import sys

logs = []
for line in '''$(printf '%s\n' "${logs[@]}")'''.split('\n'):
    if line.strip():
        logs.append(line)

data = {
    'server_id': '''$server_id''',
    'hostname': '''$hostname''',
    'log_type': '''$log_type''',
    'logs': logs
}

print(json.dumps(data))
")

    local response=$(curl -s -X POST "${SERVER_URL}/api/upload" \
        -H "Content-Type: application/json" \
        -d "$payload")

    # 使用 Python 解析响应
    local success=$($PYTHON -c "import json,sys; data=json.loads('''$response'''); print('true' if data.get('success') else 'false')" 2>/dev/null)

    if [ "$success" = "true" ]; then
        local inserted=$($PYTHON -c "import json,sys; data=json.loads('''$response'''); print(data.get('inserted', 0))")
        log_success "上传成功: ${inserted} 条日志 (类型: ${log_type})"
        return 0
    else
        log_error "上传失败: $response"
        return 1
    fi
}

# 读取并上传单个日志文件
process_log_file() {
    local file="$1"
    local log_type="$2"
    local incremental="$3"
    local server_id="$4"
    
    if [ ! -f "$file" ]; then
        log_warning "文件不存在: $file"
        return 1
    fi
    
    if [ ! -r "$file" ]; then
        log_warning "无法读取文件: $file (权限不足)"
        return 1
    fi
    
    log_info "处理日志文件: $file (类型: $log_type)"
    
    local start_pos=0
    if [ "$incremental" = "true" ]; then
        start_pos=$(get_file_position "$file")
    fi
    
    local current_size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null)
    
    # 文件被截断，从头开始
    if [ "$start_pos" -gt "$current_size" ]; then
        start_pos=0
    fi
    
    # 没有新内容
    if [ "$start_pos" -eq "$current_size" ]; then
        log_info "没有新日志"
        return 0
    fi
    
    # 读取新内容
    local batch=()
    local count=0
    
    while IFS= read -r line; do
        if [ -n "$line" ]; then
            batch+=("$line")
            ((count++))
            
            # 达到批量大小，上传
            if [ ${#batch[@]} -ge $BATCH_SIZE ]; then
                upload_logs "$log_type" "$server_id" "${batch[@]}"
                batch=()
            fi
        fi
    done < <(tail -c +$((start_pos + 1)) "$file")
    
    # 上传剩余的日志
    if [ ${#batch[@]} -gt 0 ]; then
        upload_logs "$log_type" "$server_id" "${batch[@]}"
    fi
    
    # 保存位置
    if [ "$incremental" = "true" ]; then
        save_file_position "$file" "$current_size"
    fi
    
    log_info "共处理 $count 行日志"
}

# 上传所有默认日志
upload_all_logs() {
    local incremental="${1:-false}"
    local server_id=$(get_server_id)
    
    for entry in "${DEFAULT_LOGS[@]}"; do
        local file="${entry%%:*}"
        local type="${entry##*:}"
        
        if [ -f "$file" ]; then
            process_log_file "$file" "$type" "$incremental" "$server_id"
        fi
    done
}

# 守护进程模式
daemon_mode() {
    local server_id=$(get_server_id)
    
    log_info "启动守护进程模式 (间隔: ${CHECK_INTERVAL}秒)"
    log_info "服务器ID: $server_id"
    log_info "按 Ctrl+C 停止"
    echo ""
    
    # 捕获中断信号
    trap 'log_info "正在停止守护进程..."; exit 0' INT TERM
    
    while true; do
        upload_all_logs "true"
        echo ""
        log_info "等待 ${CHECK_INTERVAL} 秒..."
        sleep $CHECK_INTERVAL
    done
}

# 实时跟踪模式
tail_mode() {
    local file="$1"
    local log_type="${2:-syslog}"
    local server_id=$(get_server_id)
    
    if [ ! -f "$file" ]; then
        log_error "文件不存在: $file"
        exit 1
    fi
    
    log_info "实时跟踪: $file"
    log_info "服务器ID: $server_id"
    log_info "按 Ctrl+C 停止"
    
    trap 'log_info "停止跟踪"; exit 0' INT TERM
    
    local batch=()
    
    tail -F "$file" 2>/dev/null | while IFS= read -r line; do
        if [ -n "$line" ]; then
            batch+=("$line")
            
            if [ ${#batch[@]} -ge 10 ]; then
                upload_logs "$log_type" "$server_id" "${batch[@]}"
                batch=()
            fi
        fi
    done
}

# 显示帮助
show_help() {
    print_banner
    echo "用法: $0 [选项] [WAL日志文件]"
    echo ""
    echo "选项:"
    echo "  --register       注册服务器（首次使用必须执行）"
    echo "  --daemon         后台守护进程模式，持续监控 WAL 日志"
    echo "  --tail <file>    实时跟踪指定 WAL 日志文件"
    echo "  --all            上传默认 WAL 日志文件"
    echo "  --status         显示当前状态"
    echo "  --help           显示此帮助信息"
    echo ""
    echo "环境变量:"
    echo "  LOG_SERVER_URL   日志服务器地址 (默认: http://localhost:8080)"
    echo "  WAL_LOG_FILE     WAL日志文件路径 (默认: /var/log/wal/wal.log)"
    echo ""
    echo "示例:"
    echo "  $0 --register                    # 首次使用，注册服务器"
    echo "  $0 /path/to/wal.log              # 上传 WAL 日志"
    echo "  $0 --daemon                      # 守护进程模式"
    echo "  $0 --tail /var/log/wal/wal.log   # 实时跟踪 WAL 日志"
    echo ""
    echo "WAL 日志格式:"
    echo "  dump_stream_9_0_0:Wal record @ record end plsn:123; xid: (412, 12321);type:WAL_HEAP_UPDATE; ..."
    echo ""
    echo "默认监控的 WAL 日志文件:"
    echo "  ${WAL_LOG_FILE}"
}

# 显示状态
show_status() {
    print_banner
    
    echo "配置信息:"
    echo "  服务器地址: $SERVER_URL"
    
    if [ -f "${STATE_DIR}/server_id" ]; then
        local server_id=$(cat "${STATE_DIR}/server_id")
        echo "  服务器ID: $server_id"
        echo "  状态: ✓ 已注册"
    else
        echo "  状态: ✗ 未注册 (请运行 $0 --register)"
    fi
    
    echo "  状态目录: $STATE_DIR"
    echo ""
    
    echo "日志文件状态:"
    for entry in "${DEFAULT_LOGS[@]}"; do
        local file="${entry%%:*}"
        local type="${entry##*:}"
        
        if [ -f "$file" ]; then
            local size=$(ls -lh "$file" | awk '{print $5}')
            local pos=$(get_file_position "$file")
            echo -e "  ${GREEN}✓${NC} $file ($type) - 大小: $size, 已处理: $pos bytes"
        else
            echo -e "  ${RED}✗${NC} $file ($type) - 不存在"
        fi
    done
    
    # 测试服务器连接
    echo ""
    echo "测试服务器连接..."
    if curl -s --connect-timeout 5 "${SERVER_URL}/api/stats" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} 服务器连接正常"
    else
        echo -e "  ${RED}✗${NC} 无法连接到服务器"
    fi
}

# ==================== 主程序 ====================

main() {
    check_dependencies
    init_state_dir

    local log_type="wal"  # 固定为 wal 类型
    local action=""
    local target_file=""

    # 解析参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --register)
                action="register"
                shift
                ;;
            --daemon)
                action="daemon"
                shift
                ;;
            --tail)
                action="tail"
                target_file="$2"
                shift 2
                ;;
            --all)
                action="all"
                shift
                ;;
            --status)
                action="status"
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            -*)
                log_error "未知选项: $1"
                show_help
                exit 1
                ;;
            *)
                target_file="$1"
                action="file"
                shift
                ;;
        esac
    done
    
    print_banner
    
    # 执行操作
    case $action in
        register)
            register_server
            ;;
        status)
            show_status
            ;;
        daemon)
            daemon_mode
            ;;
        tail)
            tail_mode "$target_file" "$log_type"
            ;;
        all)
            upload_all_logs "false"
            ;;
        file)
            if [ -n "$target_file" ]; then
                local server_id=$(get_server_id)
                process_log_file "$target_file" "$log_type" "false" "$server_id"
            else
                log_error "请指定日志文件"
                exit 1
            fi
            ;;
        *)
            # 默认行为：显示状态
            show_status
            ;;
    esac
}

main "$@"
