#!/bin/bash
#
# WAL 日志上传工具
# 用法: ./wal_upload.sh [--register|--daemon|--file <path>|--help]
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"

WAL_FILE="${WAL_LOG_FILE:-/var/log/wal/wal.log}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"

show_help() {
    cat << EOF
WAL 日志上传工具

用法: $0 [选项]

选项:
  --register       注册服务器（首次使用）
  --daemon         守护进程模式，持续上传
  --file <path>    上传指定文件
  --status         查看状态
  --help           显示帮助

环境变量:
  LOG_SERVER_URL   服务器地址 (默认: http://localhost:8080)
  WAL_LOG_FILE     WAL文件路径 (默认: /var/log/wal/wal.log)
  BATCH_SIZE       批量大小 (默认: 100)
  CHECK_INTERVAL   检查间隔秒数 (默认: 30)

示例:
  $0 --register              # 注册
  $0 --file /path/wal.log    # 上传文件
  $0 --daemon                # 守护模式
EOF
}

show_status() {
    echo "=== WAL 上传状态 ==="
    echo "服务器: $SERVER_URL"
    echo "WAL文件: $WAL_FILE"
    if [ -f "${STATE_DIR}/server_id" ]; then
        echo "节点ID: $(cat ${STATE_DIR}/server_id)"
        echo "状态: 已注册"
    else
        echo "状态: 未注册"
    fi
    echo ""
    if curl -sf --connect-timeout 3 "${SERVER_URL}/api/stats" >/dev/null 2>&1; then
        log_ok "服务器连接正常"
    else
        log_warn "无法连接服务器"
    fi
}

daemon_mode() {
    local server_id=$(get_server_id)
    log_info "守护模式启动 (间隔: ${CHECK_INTERVAL}s)"
    log_info "监控文件: $WAL_FILE"
    trap 'log_info "停止"; exit 0' INT TERM

    while true; do
        [ -f "$WAL_FILE" ] && upload_file_incremental "$WAL_FILE" "$server_id"
        sleep $CHECK_INTERVAL
    done
}

# ==================== 主程序 ====================
main() {
    check_deps
    init_state

    case "${1:-}" in
        --register)
            register_server
            ;;
        --daemon)
            daemon_mode
            ;;
        --file)
            [ -z "$2" ] && { log_error "请指定文件"; exit 1; }
            upload_file "$2" "$(get_server_id)"
            ;;
        --status)
            show_status
            ;;
        --help|-h)
            show_help
            ;;
        "")
            show_status
            ;;
        *)
            # 直接传文件路径
            if [ -f "$1" ]; then
                upload_file "$1" "$(get_server_id)"
            else
                log_error "未知选项: $1"
                show_help
                exit 1
            fi
            ;;
    esac
}

main "$@"
