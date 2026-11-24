#!/usr/bin/env bash
# Copyright (c) 2026 LightSeek Foundation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# 配置式批量部署入口；每行明确指定一个TP8实例的角色、主机和设备组。
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_SCRIPT="$SCRIPT_DIR/deploy_runtime.sh"
usage() {
    cat <<'EOF'
用法：SERVING_MODE=pd|merged CODE_MODE=wheel|source PD_LAYOUT="..." bash deploy_pd.sh <命令>
布局每行：实例ID prefill|decode|regular 主机IP 逗号分隔的8个设备ID
可用PD_LAYOUT_FILE读取同格式文件或JSON；PD模式至少1P1D，可同机混合P/D。

plan                 校验并打印实例、P/D卡数、端口和网关规划，不连接远端
start/startp/startd   全部/仅Prefill/仅Decode准备并启动；start最后执行真实生成
stop/stopp/stopd      全部/仅Prefill/仅Decode停止；同机其他实例不受影响
status               查看全部实例状态
shell/code           分发部署工具/工具和wheel发布包，不覆盖已有发布包或源码
check/smoke-remote    通过唯一网关执行完整/快速生成验收
evidence-remote      收集各实例的KV传输与完成事件
log/log_plog         按主机收集所有实例的服务/Ascend日志

REQUIRE_FULL_NODE=1 默认要求显式布局将每台16个设备分配完整，0允许部分设备。
GATEWAY_INSTANCE可指定网关归属实例，默认第一个P（merged默认第一个regular）。
RUN_TAG隔离目录；基础环境、模型、SSH认证需预先准备。SSH_PORT默认22。
旧DS_IPLIST/DEVICE_POOLS布局仍兼容，但不能与显式布局同时设置。
兼容本机入口：build publish check-release install up cluster-up smoke verify evidence；
本机进程操作：local-start/local-stop/local-status。详见中文部署文档。
EOF
}
action="${1:-help}"
case "$action" in
    help|-h|--help) usage; exit 0 ;;
    build|publish|check-release|install|up|cluster-up|smoke|verify|evidence)
        exec bash "$RUNTIME_SCRIPT" "$@" ;;
    local-start|local-stop|local-status)
        exec bash "$RUNTIME_SCRIPT" "${action#local-}" "${@:2}" ;;
esac
[[ $# -eq 1 ]] || { echo '此命令仅接受一个动作参数' >&2; exit 2; }
# ----------------------- 任务与节点配置 -----------------------
: "${SERVING_MODE:?显式设置 SERVING_MODE=merged 或 pd}"
: "${CODE_MODE:?显式设置 CODE_MODE=wheel 或 source}"
RUN_TAG="${RUN_TAG:-dev}"
TEST_ROOT="${TEST_ROOT:-/workdir/deepseek-v41-test}"
PD_ROOT="${PD_ROOT:-$TEST_ROOT/run_$RUN_TAG}"
REMOTE_TOOLS="${REMOTE_TOOLS:-$TEST_ROOT/tools_$RUN_TAG}"
LOCAL_LOG_ROOT="${LOCAL_LOG_ROOT:-$PWD/logs/deepseek-v41}"
# 示例：merged填1个本机/远端IP；pd填P、D1、D2三个IP，允许空格或换行。
DS_IPLIST="${DS_IPLIST:-}"
MODEL_PATH="${MODEL_PATH:-/models/DeepSeek-V4.1-Flash}"
BASE_ROOT="${BASE_ROOT:-/workdir/deepseek-v41-base}"
RELEASE_ROOT="${RELEASE_ROOT:-/workdir/releases/deepseek-v41-dev}"
TS_SRC="${TS_SRC:-$SCRIPT_DIR/../../..}"
UNI_SRC="${UNI_SRC:-$TS_SRC/../uni-ops}"
PLUGIN_SRC="${PLUGIN_SRC:-$TS_SRC/../ts-plugin}"
PD_LAYOUT="${PD_LAYOUT:-}"
PD_LAYOUT_FILE="${PD_LAYOUT_FILE:-}"
DEVICE_POOLS="${DEVICE_POOLS:-}"
POOL_PORT_STRIDE="${POOL_PORT_STRIDE:-100}"
BASE_PD_ROOT="$PD_ROOT"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
# ----------------------- P/D参数配置 -----------------------
MAX_MODEL_LEN="${MAX_MODEL_LEN:-512}"
MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-1024}"
CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-512}"
PREFILL_MEM_FRACTION="${PREFILL_MEM_FRACTION:-0.85}"
DECODE_MEM_FRACTION="${DECODE_MEM_FRACTION:-0.85}"
PREFILL_MAX_NUM_SEQS="${PREFILL_MAX_NUM_SEQS:-1}"
DECODE_MAX_NUM_SEQS="${DECODE_MAX_NUM_SEQS:-1}"
HTTP_PORT="${HTTP_PORT:-8010}"
ENGINE_PORT="${ENGINE_PORT:-8120}"
METRICS_PORT="${METRICS_PORT:-8121}"
GATEWAY_METRICS_PORT="${GATEWAY_METRICS_PORT:-8122}"
HANDSHAKE_PORT="${HANDSHAKE_PORT:-9010}"
DIST_PORT="${DIST_PORT:-9600}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ "$CODE_MODE" == wheel || "$CODE_MODE" == source ]] || fail 'CODE_MODE无效'
[[ "$RUN_TAG" =~ ^[a-zA-Z0-9._-]+$ ]] || fail 'RUN_TAG无效'
for directory in "$TEST_ROOT" "$PD_ROOT" "$REMOTE_TOOLS"; do
    [[ "$directory" == /* && "$directory" != *[[:space:]]* ]] || fail '工作目录必须为无空白的绝对路径'
done
export SERVING_MODE PD_LAYOUT PD_LAYOUT_FILE DS_IPLIST DEVICE_POOLS POOL_PORT_STRIDE
export HTTP_PORT ENGINE_PORT METRICS_PORT GATEWAY_METRICS_PORT HANDSHAKE_PORT DIST_PORT BOOTSTRAP_PORT
plan_file="$(mktemp)"
trap 'rm -f "$plan_file"' EXIT
python3 "$SCRIPT_DIR/pd_layout.py" > "$plan_file"
LAYOUT_JSON="$(cat "$plan_file")"
LAYOUT_FINGERPRINT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "$plan_file")"
gateway_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_instance"])' "$plan_file")"
ids=(); roles=(); instance_nodes=(); devices=(); slots=(); nodes=()
gateway_index=0
while IFS=$'\t' read -r name role node mask slot; do
    [[ "$name" != "$gateway_id" ]] || gateway_index="${#ids[@]}"
    ids+=("$name"); roles+=("$role"); instance_nodes+=("$node"); devices+=("$mask"); slots+=("$slot")
    case " ${nodes[*]:-} " in *" $node "*) ;; *) nodes+=("$node") ;; esac
done < <(python3 "$SCRIPT_DIR/pd_layout.py" rows "$plan_file")
selected=(); selected_nodes=()
for index in "${!ids[@]}"; do
    case "$action" in
        startp|stopp) [[ "${roles[$index]}" == prefill ]] || continue ;;
        startd|stopd) [[ "${roles[$index]}" == decode ]] || continue ;;
    esac
    selected+=("$index")
    node="${instance_nodes[$index]}"
    case " ${selected_nodes[*]:-} " in *" $node "*) ;; *) selected_nodes+=("$node") ;; esac
done
case "$action" in startp|startd|stopp|stopd) [[ "$SERVING_MODE" == pd ]] || fail '角色命令仅用于pd' ;; esac
# Gateway owner first, then other P/regular instances, then Decode.
ordered=()
for index in "${selected[@]}"; do [[ "$index" != "$gateway_index" ]] || ordered+=("$index"); done
for desired in prefill regular decode; do
    for index in "${selected[@]}"; do
        if [[ "$index" != "$gateway_index" && "${roles[$index]}" == "$desired" ]]; then ordered+=("$index"); fi
    done
done
ssh_cmd=(ssh -p "$SSH_PORT"); scp_cmd=(scp -P "$SSH_PORT")
if [[ "$action" != plan && -n "${SSH_PASSWORD_FILE:-}" ]]; then
    [[ -r "$SSH_PASSWORD_FILE" ]] || fail 'SSH_PASSWORD_FILE不可读'
    command -v sshpass >/dev/null || fail '密码文件方式需要sshpass'
    ssh_cmd=(sshpass -f "$SSH_PASSWORD_FILE" "${ssh_cmd[@]}")
    scp_cmd=(sshpass -f "$SSH_PASSWORD_FILE" "${scp_cmd[@]}")
else
    ssh_cmd+=(-o BatchMode=yes); scp_cmd+=(-o BatchMode=yes)
fi
remote() {
    local node="$1" command
    shift
    printf -v command '%q ' "$@"
    "${ssh_cmd[@]}" "$SSH_USER@$node" "$command"
}
run_instance() {
    local index="$1" task="$2" count=0 other
    for other in "${instance_nodes[@]}"; do [[ "$other" != "${instance_nodes[$index]}" ]] || count=$((count+1)); done
    remote "${instance_nodes[$index]}" env "SERVING_MODE=$SERVING_MODE" "CODE_MODE=$CODE_MODE" \
        "BASE_ROOT=$BASE_ROOT" "RELEASE_ROOT=$RELEASE_ROOT" "MODEL_PATH=$MODEL_PATH" \
        "PD_ROOT=$BASE_PD_ROOT/${ids[$index]}" "NODE_IP=${instance_nodes[$index]}" "INSTANCE_ID=${ids[$index]}" \
        "LAYOUT_JSON=$LAYOUT_JSON" "LAYOUT_FINGERPRINT=$LAYOUT_FINGERPRINT" \
        "DEVICES=${devices[$index]}" "POOL_INDEX=${slots[$index]}" "POOL_COUNT=$count" \
        "TS_SRC=$TS_SRC" "UNI_SRC=$UNI_SRC" "PLUGIN_SRC=$PLUGIN_SRC" \
        "HTTP_PORT=$HTTP_PORT" "ENGINE_PORT=$ENGINE_PORT" "METRICS_PORT=$METRICS_PORT" \
        "GATEWAY_METRICS_PORT=$GATEWAY_METRICS_PORT" "HANDSHAKE_PORT=$HANDSHAKE_PORT" \
        "DIST_PORT=$DIST_PORT" "BOOTSTRAP_PORT=$BOOTSTRAP_PORT" "POOL_PORT_STRIDE=$POOL_PORT_STRIDE" \
        "MAX_MODEL_LEN=$MAX_MODEL_LEN" "MAX_TOTAL_TOKENS=$MAX_TOTAL_TOKENS" \
        "CHUNKED_PREFILL_SIZE=$CHUNKED_PREFILL_SIZE" "PREFILL_MEM_FRACTION=$PREFILL_MEM_FRACTION" \
        "DECODE_MEM_FRACTION=$DECODE_MEM_FRACTION" "PREFILL_MAX_NUM_SEQS=$PREFILL_MAX_NUM_SEQS" \
        "DECODE_MAX_NUM_SEQS=$DECODE_MAX_NUM_SEQS" bash "$REMOTE_TOOLS/deploy_runtime.sh" "$task"
}
stage_tools() {
    local node="$1" guide="$SCRIPT_DIR/../../../docs/serving/deepseek-v41-910b.md"
    [[ -f "$guide" ]] || guide="$SCRIPT_DIR/DEPLOYMENT.md"
    remote "$node" mkdir -p "$REMOTE_TOOLS"
    "${scp_cmd[@]}" "$SCRIPT_DIR/deploy_pd.sh" "$RUNTIME_SCRIPT" "$SCRIPT_DIR/configure.py" "$SCRIPT_DIR/pd_layout.py" "$SSH_USER@$node:$REMOTE_TOOLS/"
    "${scp_cmd[@]}" "$guide" "$SSH_USER@$node:$REMOTE_TOOLS/DEPLOYMENT.md"
}
stage_release() {
    local node="$1" status=0 index
    remote "$node" test -d "$RELEASE_ROOT" || status=$?
    if [[ "$status" == 1 ]]; then
        [[ -f "$RELEASE_ROOT/manifest.json" ]] || fail "本地发布包不存在: $RELEASE_ROOT"
        remote "$node" mkdir -p "$(dirname "$RELEASE_ROOT")"
        "${scp_cmd[@]}" -r "$RELEASE_ROOT" "$SSH_USER@$node:$(dirname "$RELEASE_ROOT")/"
    elif [[ "$status" != 0 ]]; then return "$status"; fi
    for index in "${!ids[@]}"; do
        if [[ "${instance_nodes[$index]}" == "$node" ]]; then run_instance "$index" check-release; break; fi
    done
}
case "$action" in
    plan)
        printf 'tag=%s root=%s\n' "$RUN_TAG" "$BASE_PD_ROOT"
        python3 "$SCRIPT_DIR/pd_layout.py" summary "$plan_file" ;;
    shell|code)
        for node in "${selected_nodes[@]}"; do stage_tools "$node"; [[ "$action" != code ]] || stage_release "$node"; done ;;
    start|startp|startd)
        for node in "${selected_nodes[@]}"; do stage_tools "$node"; stage_release "$node"; done
        for index in "${selected[@]}"; do run_instance "$index" prepare; done
        for index in "${ordered[@]}"; do run_instance "$index" start; done
        [[ "$action" != start ]] || run_instance "$gateway_index" smoke ;;
    stop|stopp|stopd|status)
        result=0
        for ((position=${#ordered[@]}-1; position>=0; position--)); do
            task=status; [[ "$action" == status ]] || task=stop
            run_instance "${ordered[$position]}" "$task" || result=1
        done
        exit "$result" ;;
    check|smoke-remote)
        task=verify; [[ "$action" != smoke-remote ]] || task=smoke
        run_instance "$gateway_index" "$task" ;;
    evidence-remote)
        for index in "${selected[@]}"; do run_instance "$index" evidence; done ;;
    log|log_plog)
        mkdir -p "$LOCAL_LOG_ROOT/$RUN_TAG"
        for node in "${nodes[@]}"; do
            paths=()
            for index in "${!ids[@]}"; do
                [[ "${instance_nodes[$index]}" == "$node" ]] || continue
                name="${ids[$index]}"
                if [[ "$action" == log ]]; then paths+=("$name/logs" "$name/validation" "$name/runtime/node.json")
                else paths+=("$name/ascend-log"); fi
            done
            remote "$node" tar -C "$BASE_PD_ROOT" -czf - "${paths[@]}" > "$LOCAL_LOG_ROOT/$RUN_TAG/$node-$action.tar.gz"
        done ;;
    *) usage; fail "未知命令: $action" ;;
esac
