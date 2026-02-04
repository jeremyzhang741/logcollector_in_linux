#!/bin/bash
#
# Panic 监控脚本 - 检测到 panic 后触发 WAL 导出上传
# 用法: ./panic_monitor.sh [--daemon|--stop|--status]
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"

MONITOR_FILE="${MONITOR_FILE:-/test/dir/log/logfile}"
WAL_OUTPUT="${WAL_OUTPUT_DIR:-/var/log/wal}/wal.log"
PID_FILE="/tmp/panic_monitor.pid"
DEBOUNCE=60  # 防抖秒数

# 处理 panic
handle_panic() {
    log_warn "检测到 PANIC: $1"

    # 调用 waldump
    if [ -x "${SCRIPT_DIR}/waldump.sh" ]; then
        log_info "执行 waldump..."
        mkdir -p "$(dirname "$WAL_OUTPUT")"
        "${SCRIPT_DIR}/waldump.sh" >> "$WAL_OUTPUT" 2>&1 && log_ok "WAL 导出完成"
    fi

    # 上传
    if [ -x "${SCRIPT_DIR}/wal_upload.sh" ] && [ -f "$WAL_OUTPUT" ]; then
        log_info "上传 WAL..."
        "${SCRIPT_DIR}/wal_upload.sh" --file "$WAL_OUTPUT" && log_ok "上传完成"
    fi
}

# 监控循环
monitor_loop() {
    log_info "监控: $MONITOR_FILE"
    log_info "Ctrl+C 停止"

    local last_trigger=0 last_size=0

    # 等待文件
    while [ ! -f "$MONITOR_FILE" ]; do
        log_warn "等待文件: $MONITOR_FILE"
        sleep 5
    done

    last_size=$(stat -c%s "$MONITOR_FILE" 2>/dev/null || stat -f%z "$MONITOR_FILE" || echo 0)

    while true; do
        [ ! -f "$MONITOR_FILE" ] && { sleep 5; last_size=0; continue; }

        local cur_size=$(stat -c%s "$MONITOR_FILE" 2>/dev/null || stat -f%z "$MONITOR_FILE" || echo 0)
        [ "$cur_size" -lt "$last_size" ] && last_size=0

        if [ "$cur_size" -gt "$last_size" ]; then
            local content=$(tail -c +$((last_size + 1)) "$MONITOR_FILE" 2>/dev/null)
            if echo "$content" | grep -iq "panic"; then
                local now=$(date +%s)
                if [ $((now - last_trigger)) -ge $DEBOUNCE ]; then
                    handle_panic "$(echo "$content" | grep -i panic | head -1)"
                    last_trigger=$now
                fi
            fi
            last_size=$cur_size
        fi
        sleep 1
    done
}

# 后台启动
start_daemon() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null && { log_error "已在运行"; exit 1; }
    nohup "$0" --run >/dev/null 2>&1 &
    echo $! > "$PID_FILE"
    log_ok "后台启动 PID: $!"
}

stop_daemon() {
    [ ! -f "$PID_FILE" ] && { log_warn "未运行"; return; }
    local pid=$(cat "$PID_FILE")
    kill "$pid" 2>/dev/null && log_ok "已停止" || log_warn "进程不存在"
    rm -f "$PID_FILE"
}

show_status() {
    echo "监控文件: $MONITOR_FILE"
    echo "WAL输出: $WAL_OUTPUT"
    [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null \
        && echo "状态: 运行中 (PID: $(cat $PID_FILE))" \
        || echo "状态: 未运行"
}

# ==================== 主程序 ====================
trap 'rm -f "$PID_FILE"; exit 0' INT TERM

case "${1:-}" in
    --daemon)  start_daemon ;;
    --run)     echo $$ > "$PID_FILE"; monitor_loop ;;
    --stop)    stop_daemon ;;
    --status)  show_status ;;
    --help|-h) echo "用法: $0 [--daemon|--stop|--status|--help]" ;;
    "")        monitor_loop ;;
    *)         log_error "未知: $1"; exit 1 ;;
esac
