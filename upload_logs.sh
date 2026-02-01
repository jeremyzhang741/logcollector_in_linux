#!/bin/bash
#
# LogVista 日志上传脚本
# 用于将Linux服务器日志发送到日志分析平台
#
# 使用方法:
#   1. 首次使用需要注册服务器获取API Key
#   2. 配置脚本中的SERVER_URL和API_KEY
#   3. 运行脚本上传日志
#
# 示例:
#   ./upload_logs.sh                    # 上传默认日志
#   ./upload_logs.sh /var/log/syslog    # 上传指定日志文件
#   ./upload_logs.sh --register         # 注册服务器获取API Key
#   ./upload_logs.sh --daemon           # 后台持续监控模式
#

set -e

# ==================== 配置区域 ====================
# 日志分析平台地址
SERVER_URL="${LOG_SERVER_URL:-http://localhost:8080}"

# API密钥（注册后填写）
API_KEY="${LOG_API_KEY:-}"

# 默认监控的日志文件
DEFAULT_LOGS=(
    "/var/log/syslog:syslog"
    "/var/log/auth.log:auth"
    "/var/log/kern.log:kern"
    "/var/log/messages:syslog"
    "/var/log/secure:auth"
    "/var/log/dmesg:kern"
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
    echo "║       LogVista 日志上传工具 v1.0         ║"
    echo "║   Linux服务器日志收集与分析平台          ║"
    echo "╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

check_dependencies() {
    local missing=()
    
    for cmd in curl jq hostname; do
        if ! command -v $cmd &> /dev/null; then
            missing+=($cmd)
        fi
    done
    
    if [ ${#missing[@]} -gt 0 ]; then
        log_error "缺少必要依赖: ${missing[*]}"
        echo "请安装: sudo apt-get install ${missing[*]}"
        exit 1
    fi
}

init_state_dir() {
    mkdir -p "$STATE_DIR"
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
    
    if echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        local api_key=$(echo "$response" | jq -r '.api_key')
        local server_id=$(echo "$response" | jq -r '.server_id')
        
        log_success "服务器注册成功！"
        echo ""
        echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║${NC} 服务器ID: ${YELLOW}${server_id}${NC}"
        echo -e "${GREEN}║${NC} API密钥:  ${YELLOW}${api_key}${NC}"
        echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo "请保存以上API密钥，并设置环境变量:"
        echo -e "  ${CYAN}export LOG_API_KEY=\"${api_key}\"${NC}"
        echo "或编辑此脚本，填写 API_KEY 变量"
        echo ""
        
        # 保存到配置文件
        echo "$api_key" > "${STATE_DIR}/api_key"
        chmod 600 "${STATE_DIR}/api_key"
        log_info "API密钥已保存到 ${STATE_DIR}/api_key"
    else
        log_error "注册失败: $response"
        exit 1
    fi
}

# 加载API密钥
load_api_key() {
    if [ -n "$API_KEY" ]; then
        return 0
    fi
    
    if [ -f "${STATE_DIR}/api_key" ]; then
        API_KEY=$(cat "${STATE_DIR}/api_key")
        return 0
    fi
    
    log_error "未找到API密钥"
    echo "请先运行: $0 --register"
    exit 1
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
    local hostname=$(get_hostname)
    shift
    local logs=("$@")
    
    if [ ${#logs[@]} -eq 0 ]; then
        return 0
    fi
    
    # 构建JSON数组
    local json_logs=$(printf '%s\n' "${logs[@]}" | jq -R -s -c 'split("\n") | map(select(length > 0))')
    
    local payload=$(jq -n \
        --arg hostname "$hostname" \
        --arg log_type "$log_type" \
        --argjson logs "$json_logs" \
        '{hostname: $hostname, log_type: $log_type, logs: $logs}')
    
    local response=$(curl -s -X POST "${SERVER_URL}/api/upload" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: ${API_KEY}" \
        -d "$payload")
    
    if echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        local inserted=$(echo "$response" | jq -r '.inserted')
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
                upload_logs "$log_type" "${batch[@]}"
                batch=()
            fi
        fi
    done < <(tail -c +$((start_pos + 1)) "$file")
    
    # 上传剩余的日志
    if [ ${#batch[@]} -gt 0 ]; then
        upload_logs "$log_type" "${batch[@]}"
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
    
    for entry in "${DEFAULT_LOGS[@]}"; do
        local file="${entry%%:*}"
        local type="${entry##*:}"
        
        if [ -f "$file" ]; then
            process_log_file "$file" "$type" "$incremental"
        fi
    done
}

# 守护进程模式
daemon_mode() {
    log_info "启动守护进程模式 (间隔: ${CHECK_INTERVAL}秒)"
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
    
    if [ ! -f "$file" ]; then
        log_error "文件不存在: $file"
        exit 1
    fi
    
    log_info "实时跟踪: $file"
    log_info "按 Ctrl+C 停止"
    
    trap 'log_info "停止跟踪"; exit 0' INT TERM
    
    local batch=()
    
    tail -F "$file" 2>/dev/null | while IFS= read -r line; do
        if [ -n "$line" ]; then
            batch+=("$line")
            
            if [ ${#batch[@]} -ge 10 ]; then
                upload_logs "$log_type" "${batch[@]}"
                batch=()
            fi
        fi
    done
}

# 显示帮助
show_help() {
    print_banner
    echo "用法: $0 [选项] [日志文件]"
    echo ""
    echo "选项:"
    echo "  --register       注册服务器并获取API密钥"
    echo "  --daemon         后台守护进程模式，持续监控日志"
    echo "  --tail <file>    实时跟踪指定日志文件"
    echo "  --type <type>    指定日志类型 (syslog|auth|kern|app)"
    echo "  --all            上传所有默认日志文件"
    echo "  --help           显示此帮助信息"
    echo ""
    echo "环境变量:"
    echo "  LOG_SERVER_URL   日志服务器地址 (默认: http://localhost:8080)"
    echo "  LOG_API_KEY      API密钥"
    echo ""
    echo "示例:"
    echo "  $0 --register                    # 首次使用，注册服务器"
    echo "  $0 --all                         # 上传所有默认日志"
    echo "  $0 /var/log/nginx/access.log     # 上传指定日志"
    echo "  $0 --type app /var/log/myapp.log # 指定类型上传"
    echo "  $0 --daemon                      # 守护进程模式"
    echo "  $0 --tail /var/log/syslog        # 实时跟踪"
    echo ""
    echo "默认监控的日志文件:"
    for entry in "${DEFAULT_LOGS[@]}"; do
        local file="${entry%%:*}"
        local type="${entry##*:}"
        echo "  $file ($type)"
    done
}

# 显示状态
show_status() {
    print_banner
    
    echo "配置信息:"
    echo "  服务器地址: $SERVER_URL"
    
    if [ -n "$API_KEY" ]; then
        echo "  API密钥: ${API_KEY:0:8}...${API_KEY: -8}"
    else
        echo "  API密钥: 未配置"
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
}

# ==================== 主程序 ====================

main() {
    check_dependencies
    init_state_dir
    
    local log_type="syslog"
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
            --type)
                log_type="$2"
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
            load_api_key
            show_status
            ;;
        daemon)
            load_api_key
            daemon_mode
            ;;
        tail)
            load_api_key
            tail_mode "$target_file" "$log_type"
            ;;
        all)
            load_api_key
            upload_all_logs "false"
            ;;
        file)
            load_api_key
            if [ -n "$target_file" ]; then
                process_log_file "$target_file" "$log_type" "false"
            else
                log_error "请指定日志文件"
                exit 1
            fi
            ;;
        *)
            # 默认行为：上传所有日志
            load_api_key
            upload_all_logs "false"
            ;;
    esac
}

main "$@"
