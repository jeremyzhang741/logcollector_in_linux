#!/bin/bash
#
# WAL 日志批量上传脚本
# 支持并发上传、gzip 压缩、多节点批量上传
#
# 使用方法:
#   ./batch_upload.sh --register         # 注册当前节点
#   ./batch_upload.sh /path/to/wal.log   # 上传单个文件
#   ./batch_upload.sh --dir /path/to/logs --parallel 10  # 并发上传目录下所有日志
#

set -e

# ==================== 配置 ====================
SERVER_URL="${LOG_SERVER_URL:-http://localhost:8080}"
BATCH_SIZE="${BATCH_SIZE:-1000}"       # 每批上传行数
PARALLEL="${PARALLEL:-5}"              # 并发上传数
USE_GZIP="${USE_GZIP:-true}"           # 是否使用 gzip 压缩
STATE_DIR="${HOME}/.wal_collector"

# ==================== 颜色 ====================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

print_banner() {
    echo -e "${CYAN}"
    echo "╔══════════════════════════════════════════╗"
    echo "║     WAL 日志批量上传工具 v2.0            ║"
    echo "║   支持并发上传 | gzip压缩 | 批量处理     ║"
    echo "╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

check_dependencies() {
    local missing=()
    for cmd in curl gzip python3; do
        command -v $cmd &>/dev/null || missing+=($cmd)
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log_error "缺少依赖: ${missing[*]}"
        exit 1
    fi
}

init_state_dir() {
    mkdir -p "$STATE_DIR"
}

get_hostname() {
    hostname -f 2>/dev/null || hostname
}

get_ip() {
    hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown"
}

get_server_id() {
    local id_file="${STATE_DIR}/server_id"
    if [ -f "$id_file" ]; then
        cat "$id_file"
    else
        log_warning "未注册，请先运行: $0 --register"
        exit 1
    fi
}

# 注册服务器
register_server() {
    log_info "正在注册服务器..."
    local hostname=$(get_hostname)
    local ip=$(get_ip)
    local os_info=$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'"' -f2 || uname -a)

    local response=$(curl -s -X POST "${SERVER_URL}/api/register" \
        -H "Content-Type: application/json" \
        -d "{\"hostname\": \"${hostname}\", \"ip\": \"${ip}\", \"os_info\": \"${os_info}\"}")

    local server_id=$(echo "$response" | python3 -c "import json,sys; print(json.load(sys.stdin).get('server_id',''))" 2>/dev/null)

    if [ -n "$server_id" ]; then
        echo "$server_id" > "${STATE_DIR}/server_id"
        log_success "注册成功！服务器ID: $server_id"
    else
        log_error "注册失败: $response"
        exit 1
    fi
}

# 上传单个文件（支持压缩）
upload_file() {
    local file="$1"
    local server_id="$2"
    local hostname=$(get_hostname)
    local ip=$(get_ip)

    if [ ! -f "$file" ]; then
        log_warning "文件不存在: $file"
        return 1
    fi

    log_info "处理文件: $file"

    # 读取文件并分批上传
    local total_lines=$(wc -l < "$file")
    local uploaded=0
    local batch_num=0

    while IFS= read -r line || [ -n "$line" ]; do
        batch+=("$line")

        if [ ${#batch[@]} -ge $BATCH_SIZE ]; then
            ((batch_num++))
            upload_batch "$server_id" "$hostname" "$ip" "${batch[@]}"
            uploaded=$((uploaded + ${#batch[@]}))
            echo -ne "\r  已上传: $uploaded / $total_lines 行 (批次 $batch_num)"
            batch=()
        fi
    done < "$file"

    # 上传剩余数据
    if [ ${#batch[@]} -gt 0 ]; then
        ((batch_num++))
        upload_batch "$server_id" "$hostname" "$ip" "${batch[@]}"
        uploaded=$((uploaded + ${#batch[@]}))
    fi

    echo ""
    log_success "完成: $uploaded 行 ($batch_num 批次)"
}

# 上传一批日志
upload_batch() {
    local server_id="$1"
    local hostname="$2"
    local ip="$3"
    shift 3
    local logs=("$@")

    # 构建 JSON
    local payload=$(python3 -c "
import json
logs = '''$(printf '%s\n' "${logs[@]}")'''.strip().split('\n')
logs = [l for l in logs if l.strip()]
print(json.dumps({
    'server_id': '$server_id',
    'hostname': '$hostname',
    'ip': '$ip',
    'log_type': 'wal',
    'logs': logs
}))
")

    # 上传（可选压缩）
    if [ "$USE_GZIP" = "true" ]; then
        echo "$payload" | gzip | curl -s -X POST "${SERVER_URL}/api/upload" \
            -H "Content-Type: application/json" \
            -H "Content-Encoding: gzip" \
            --data-binary @- > /dev/null
    else
        curl -s -X POST "${SERVER_URL}/api/upload" \
            -H "Content-Type: application/json" \
            -d "$payload" > /dev/null
    fi
}

# 并发上传目录下所有文件
upload_directory() {
    local dir="$1"
    local parallel="$2"
    local server_id=$(get_server_id)

    if [ ! -d "$dir" ]; then
        log_error "目录不存在: $dir"
        exit 1
    fi

    local files=($(find "$dir" -type f -name "*.log" 2>/dev/null))
    local total=${#files[@]}

    if [ $total -eq 0 ]; then
        log_warning "未找到 .log 文件"
        return
    fi

    log_info "找到 $total 个日志文件，并发数: $parallel"

    # 使用 xargs 并发上传
    printf '%s\n' "${files[@]}" | xargs -P "$parallel" -I {} bash -c "
        source '$0'
        upload_file '{}' '$server_id'
    "

    log_success "全部上传完成！"
}

# 显示帮助
show_help() {
    print_banner
    echo "用法: $0 [选项] [WAL日志文件]"
    echo ""
    echo "选项:"
    echo "  --register           注册当前服务器"
    echo "  --dir <path>         上传目录下所有 .log 文件"
    echo "  --parallel <n>       并发上传数 (默认: 5)"
    echo "  --batch-size <n>     每批上传行数 (默认: 1000)"
    echo "  --no-gzip            禁用 gzip 压缩"
    echo "  --status             显示状态"
    echo "  --help               显示帮助"
    echo ""
    echo "环境变量:"
    echo "  LOG_SERVER_URL       服务器地址 (默认: http://localhost:8080)"
    echo "  BATCH_SIZE           每批行数 (默认: 1000)"
    echo "  PARALLEL             并发数 (默认: 5)"
    echo "  USE_GZIP             是否压缩 (默认: true)"
    echo ""
    echo "示例:"
    echo "  $0 --register                           # 注册服务器"
    echo "  $0 /var/log/wal/wal.log                 # 上传单个文件"
    echo "  $0 --dir /var/log/wal --parallel 10    # 并发上传目录"
    echo "  BATCH_SIZE=5000 $0 /path/to/wal.log    # 大批量上传"
}

show_status() {
    print_banner
    echo "配置:"
    echo "  服务器地址: $SERVER_URL"
    echo "  批量大小: $BATCH_SIZE"
    echo "  并发数: $PARALLEL"
    echo "  压缩: $USE_GZIP"
    echo ""

    if [ -f "${STATE_DIR}/server_id" ]; then
        echo "  服务器ID: $(cat ${STATE_DIR}/server_id)"
        echo "  状态: 已注册"
    else
        echo "  状态: 未注册"
    fi
}

# ==================== 主程序 ====================
main() {
    check_dependencies
    init_state_dir

    local action=""
    local target=""
    local dir=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --register)
                action="register"
                shift
                ;;
            --dir)
                action="dir"
                dir="$2"
                shift 2
                ;;
            --parallel)
                PARALLEL="$2"
                shift 2
                ;;
            --batch-size)
                BATCH_SIZE="$2"
                shift 2
                ;;
            --no-gzip)
                USE_GZIP="false"
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
            *)
                target="$1"
                action="file"
                shift
                ;;
        esac
    done

    print_banner

    case $action in
        register)
            register_server
            ;;
        status)
            show_status
            ;;
        dir)
            upload_directory "$dir" "$PARALLEL"
            ;;
        file)
            if [ -n "$target" ]; then
                local server_id=$(get_server_id)
                upload_file "$target" "$server_id"
            else
                show_help
            fi
            ;;
        *)
            show_status
            ;;
    esac
}

# 如果不是被 source，则执行 main
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
