#!/bin/bash
# 公共函数库 - WAL 日志收集平台
# 被其他脚本 source 使用

# ==================== 配置 ====================
export SERVER_URL="${LOG_SERVER_URL:-http://localhost:8080}"
export STATE_DIR="${STATE_DIR:-$HOME/.walcollector}"
export BATCH_SIZE="${BATCH_SIZE:-100}"

# ==================== 颜色 ====================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

# ==================== 日志函数 ====================
log_info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
log_ok()      { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== 工具函数 ====================
get_hostname() { hostname -f 2>/dev/null || hostname; }
get_ip()       { hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown"; }

get_python() {
    command -v python3 &>/dev/null && echo "python3" || echo "python"
}

check_deps() {
    local missing=()
    for cmd in curl "$@"; do
        command -v $cmd &>/dev/null || missing+=($cmd)
    done
    [ ${#missing[@]} -gt 0 ] && { log_error "缺少依赖: ${missing[*]}"; exit 1; }
}

init_state() {
    mkdir -p "$STATE_DIR"
}

# ==================== 服务器注册 ====================
get_server_id() {
    local id_file="${STATE_DIR}/server_id"
    [ -f "$id_file" ] && cat "$id_file" || { log_warn "未注册，请先运行: $0 --register"; exit 1; }
}

register_server() {
    log_info "注册服务器..."
    local resp=$(curl -sf -X POST "${SERVER_URL}/api/register" \
        -H "Content-Type: application/json" \
        -d "{\"hostname\":\"$(get_hostname)\",\"ip\":\"$(get_ip)\",\"os_info\":\"$(uname -s)\"}")

    local id=$(echo "$resp" | $(get_python) -c "import json,sys;print(json.load(sys.stdin).get('server_id',''))" 2>/dev/null)

    if [ -n "$id" ]; then
        echo "$id" > "${STATE_DIR}/server_id"
        log_ok "注册成功 - ID: $id"
    else
        log_error "注册失败: $resp"
        exit 1
    fi
}

# ==================== 日志上传 ====================
upload_logs() {
    local server_id="$1"; shift
    local logs=("$@")
    [ ${#logs[@]} -eq 0 ] && return 0

    local py=$(get_python)
    local payload=$($py -c "
import json
logs = '''$(printf '%s\n' "${logs[@]}")'''.strip().split('\n')
print(json.dumps({'server_id':'$server_id','hostname':'$(get_hostname)','log_type':'wal','logs':[l for l in logs if l.strip()]}))")

    local resp=$(curl -sf -X POST "${SERVER_URL}/api/upload" \
        -H "Content-Type: application/json" -d "$payload")

    local ok=$($py -c "import json;print(json.loads('''$resp''').get('success',''))" 2>/dev/null)
    [ "$ok" = "True" ] && return 0 || { log_error "上传失败: $resp"; return 1; }
}

# 处理并上传文件
upload_file() {
    local file="$1" server_id="$2"
    [ ! -f "$file" ] && { log_warn "文件不存在: $file"; return 1; }

    log_info "上传: $file"
    local batch=() count=0

    while IFS= read -r line; do
        [ -n "$line" ] && { batch+=("$line"); ((count++)); }
        if [ ${#batch[@]} -ge $BATCH_SIZE ]; then
            upload_logs "$server_id" "${batch[@]}"
            batch=()
        fi
    done < "$file"

    [ ${#batch[@]} -gt 0 ] && upload_logs "$server_id" "${batch[@]}"
    log_ok "完成: $count 行"
}

# 增量上传（记录位置）
upload_file_incremental() {
    local file="$1" server_id="$2"
    [ ! -f "$file" ] && return 1

    local pos_file="${STATE_DIR}/$(echo "$file" | md5sum | cut -d' ' -f1).pos"
    local start_pos=0; [ -f "$pos_file" ] && start_pos=$(cat "$pos_file")
    local cur_size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file")

    [ "$start_pos" -gt "$cur_size" ] && start_pos=0
    [ "$start_pos" -eq "$cur_size" ] && { log_info "无新日志"; return 0; }

    local batch=() count=0
    while IFS= read -r line; do
        [ -n "$line" ] && { batch+=("$line"); ((count++)); }
        if [ ${#batch[@]} -ge $BATCH_SIZE ]; then
            upload_logs "$server_id" "${batch[@]}"
            batch=()
        fi
    done < <(tail -c +$((start_pos + 1)) "$file")

    [ ${#batch[@]} -gt 0 ] && upload_logs "$server_id" "${batch[@]}"
    echo "$cur_size" > "$pos_file"
    log_ok "增量上传: $count 行"
}
