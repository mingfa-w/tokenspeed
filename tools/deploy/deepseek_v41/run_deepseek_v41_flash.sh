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

# DeepSeek V4.1 Flash / 910B：参考 run_26b_pp.sh 的顶部集中配置方式。
# 命令：plan | start | startp | startd | stop | stopp | stopd | status | log | check
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-help}" in
    help|-h|--help)
        cat <<'EOF'
用法：bash run_deepseek_v41_flash.sh plan|preflight|start|startp|startd|stop|stopp|stopd|status|log|check
顶部修改PREFILL_INSTANCES、DECODE_INSTANCES及DEVICE_SLOTS即可决定P/D卡数和位置。
默认2P4D，P用16卡、D用32卡，三台共48卡；每实例TP8/EP8/PP1。
示例：PREFILL_INSTANCES=1 DECODE_INSTANCES=5 bash run_deepseek_v41_flash.sh plan
先preflight检查连接与环境，再start启动。默认使用同目录abc.txt及sshpass。
EOF
        exit 0 ;;
esac

# ==================== P/D用卡规划 ====================
# 每个实例固定TP8/EP8/PP1，使用一个HCCS八设备组。
# P卡数=PREFILL_INSTANCES*8；D卡数=DECODE_INSTANCES*8。
export SERVING_MODE=pd
export CODE_MODE="${CODE_MODE:-wheel}"   # wheel：安装包测试；source：源码开发
PREFILL_INSTANCES="${PREFILL_INSTANCES:-2}"  # 2P=16卡
DECODE_INSTANCES="${DECODE_INSTANCES:-4}"    # 4D=32卡
# 按下面顺序，前PREFILL_INSTANCES个组分给P，其余分给D。
# 可以调整顺序，控制哪些机器/设备组做P；每台当前16设备均需覆盖。
DEVICE_SLOTS=(
    "10.255.76.138|0,1,2,3,4,5,6,7"
    "10.255.76.138|8,9,10,11,12,13,14,15"
    "10.255.111.50|0,1,2,3,4,5,6,7"
    "10.255.111.50|8,9,10,11,12,13,14,15"
    "10.255.170.42|0,1,2,3,4,5,6,7"
    "10.255.170.42|8,9,10,11,12,13,14,15"
)

# ==================== 目录与任务 ====================
export RUN_TAG="${RUN_TAG:-dsv41_flash_dev}"
export TEST_ROOT="${TEST_ROOT:-/workdir/deepseek-v41-test}"
export MODEL_PATH="${MODEL_PATH:-/workdir/tokenspeed-dsv41/models/DeepSeek-V4.1-Flash}"
export BASE_ROOT="${BASE_ROOT:-/workdir/tokenspeed-dsv41-uni-ops}"
export RELEASE_ROOT="${RELEASE_ROOT:-/workdir/releases/dsv41-pd-20261124}"
export TS_SRC="${TS_SRC:-/home/mingfa/code/tokenspeed}"
export UNI_SRC="${UNI_SRC:-/home/mingfa/code/uni-ops}"
export PLUGIN_SRC="${PLUGIN_SRC:-/home/mingfa/code/ts-plugin}"
export REQUIRE_FULL_NODE=1

# ==================== P/D运行参数 ====================
export PREFILL_MEM_FRACTION="${PREFILL_MEM_FRACTION:-0.85}"
export DECODE_MEM_FRACTION="${DECODE_MEM_FRACTION:-0.85}"
export PREFILL_MAX_NUM_SEQS="${PREFILL_MAX_NUM_SEQS:-1}"
export DECODE_MAX_NUM_SEQS="${DECODE_MAX_NUM_SEQS:-1}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-512}"
export MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-1024}"
export CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-512}"
# 统一网关及各主机第一实例的基础端口；第二实例的本地端口自动偏移100。
export HTTP_PORT="${HTTP_PORT:-8010}"
export ENGINE_PORT="${ENGINE_PORT:-8120}"
export METRICS_PORT="${METRICS_PORT:-8121}"
export GATEWAY_METRICS_PORT="${GATEWAY_METRICS_PORT:-8122}"
export HANDSHAKE_PORT="${HANDSHAKE_PORT:-9010}"
export DIST_PORT="${DIST_PORT:-9600}"
export BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
export POOL_PORT_STRIDE="${POOL_PORT_STRIDE:-100}"

# ==================== SSH ====================
export SSH_USER="${SSH_USER:-root}"
export SSH_PORT="${SSH_PORT:-8022}"
export SSH_AUTH_MODE="${SSH_AUTH_MODE:-password}"
export SSH_HOST_KEY_CHECKING="${SSH_HOST_KEY_CHECKING:-accept-new}"
# abc.txt第一行存root密码；同目录优先，兼容现有FluentLLM密码文件。
if [[ "$SSH_AUTH_MODE" == password && -z "${SSH_PASSWORD_FILE:-}" ]]; then
    if [[ -f "$SCRIPT_DIR/abc.txt" ]]; then
        export SSH_PASSWORD_FILE="$SCRIPT_DIR/abc.txt"
    else
        export SSH_PASSWORD_FILE=/home/mingfa/code/fluentllm/npu_test/flash26b/abc.txt
    fi
fi

# ==================== 根据数量生成明确的角色/设备映射 ====================
[[ "$PREFILL_INSTANCES" =~ ^[1-9][0-9]*$ && "$DECODE_INSTANCES" =~ ^[1-9][0-9]*$ ]] || {
    echo 'ERROR: P/D实例数必须为正整数' >&2; exit 2;
}
python3 - "$PREFILL_INSTANCES" "$DECODE_INSTANCES" "${#DEVICE_SLOTS[@]}" <<'PYCOUNTS'
import sys
p, d, slots = map(int, sys.argv[1:])
if p + d != slots:
    raise SystemExit(f"ERROR: P+D实例数必须等于DEVICE_SLOTS组数（{slots}）")
PYCOUNTS
# 此入口负责生成布局；任意手工逐实例映射请直接使用deploy_pd.sh的PD_LAYOUT。
if [[ -n "${PD_LAYOUT:-}" || -n "${PD_LAYOUT_FILE:-}" ]]; then
    echo 'ERROR: 此脚本按实例数生成布局，请勿同时传入PD_LAYOUT/PD_LAYOUT_FILE' >&2; exit 2
fi
unset DS_IPLIST DEVICE_POOLS
PD_LAYOUT=""
for index in "${!DEVICE_SLOTS[@]}"; do
    slot="${DEVICE_SLOTS[$index]}"
    host="${slot%%|*}"
    mask="${slot#*|}"
    if (( index < PREFILL_INSTANCES )); then
        instance="p$index"; role=prefill
    else
        instance="d$((index-PREFILL_INSTANCES))"; role=decode
    fi
    PD_LAYOUT+="$instance $role $host $mask"$'\n'
done
export PD_LAYOUT

# 支持在仓库工具目录或完整交付包根目录执行，也可显式指定DEPLOY_ENTRY。
if [[ -z "${DEPLOY_ENTRY:-}" ]]; then
    if [[ -f "$SCRIPT_DIR/deploy_pd.sh" ]]; then DEPLOY_ENTRY="$SCRIPT_DIR/deploy_pd.sh"
    elif [[ -f "$SCRIPT_DIR/tools/deploy/deepseek_v41/deploy_pd.sh" ]]; then DEPLOY_ENTRY="$SCRIPT_DIR/tools/deploy/deepseek_v41/deploy_pd.sh"
    else DEPLOY_ENTRY="$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh"; fi
fi
[[ -f "$DEPLOY_ENTRY" ]] || { echo 'ERROR: 请将入口与完整部署工具一起分发，或设置DEPLOY_ENTRY' >&2; exit 2; }
# 与参考脚本相同，最后交给批量执行器；无动作时仅显示帮助。
if (( $# == 0 )); then set -- help; fi
exec bash "$DEPLOY_ENTRY" "$@"
