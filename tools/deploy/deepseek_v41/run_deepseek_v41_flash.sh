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

# ==================== 常用配置：通常只改这一区域 ====================
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export SERVING_MODE="${SERVING_MODE:-pd}"        # pd分离；merged合并（共用同一套流程）
export CODE_MODE="${CODE_MODE:-wheel}"          # wheel安装包；source源码开发
export HOSTS="${HOSTS:-10.255.76.138 10.255.111.50 10.255.170.42}"
export PREFILL_INSTANCES="${PREFILL_INSTANCES:-2}"
export DECODE_INSTANCES="${DECODE_INSTANCES:-4}"
# 每主机自动划分0–7、8–15两个TP8实例；前PREFILL_INSTANCES组做P，其余做D。
# merged将所有组作为合并实例；特殊放置使用PD_LAYOUT/PD_LAYOUT_FILE覆盖自动布局。
export RUN_TAG="${RUN_TAG:-dsv41_flash_dev}"
export TEST_ROOT="${TEST_ROOT:-/workdir/deepseek-v41-test}"
export PD_ROOT="${PD_ROOT:-$TEST_ROOT/run_$RUN_TAG}"
export BASE_ROOT="${BASE_ROOT:-/workdir/tokenspeed-dsv41-uni-ops}"
export MODEL_PATH="${MODEL_PATH:-/workdir/tokenspeed-dsv41/models/DeepSeek-V4.1-Flash}"
export RELEASE_ID="${RELEASE_ID:-dsv41-pd-20261124}"
export RELEASE_ROOT="${RELEASE_ROOT:-/workdir/releases/$RELEASE_ID}"
export TS_SRC="${TS_SRC:-/home/mingfa/code/tokenspeed}"
export UNI_SRC="${UNI_SRC:-/home/mingfa/code/uni-ops}"
export PLUGIN_SRC="${PLUGIN_SRC:-/home/mingfa/code/ts-plugin}"
export SSH_USER="${SSH_USER:-root}" SSH_PORT="${SSH_PORT:-8022}"
export SSH_AUTH_MODE="${SSH_AUTH_MODE:-password}"
export SSH_HOST_KEY_CHECKING="${SSH_HOST_KEY_CHECKING:-accept-new}"
export SSH_PASSWORD_FILE="${SSH_PASSWORD_FILE:-$SCRIPT_DIR/abc.txt}"
# 以下为可选调优项，常规测试无需修改；每个实例固定TP8/EP8/PP1。
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

export REQUIRE_FULL_NODE="${REQUIRE_FULL_NODE:-1}"
REMOTE_TOOLS="${REMOTE_TOOLS:-$TEST_ROOT/tools_$RUN_TAG}"
LOCAL_LOG_ROOT="${LOCAL_LOG_ROOT:-$PWD/logs/deepseek-v41}"
fail() { echo "ERROR: $*" >&2; exit 1; }
usage() {
    cat <<'EOF'
用法：bash run_deepseek_v41_flash.sh <命令>
常用：plan → preflight → start → status → check；stop停止本次布局。
默认三台910B、2P4D、48设备。顶部设置HOSTS、P/D数量、路径和abc.txt即可。
SERVING_MODE=merged支持P/D合并；PD_LAYOUT/PD_LAYOUT_FILE支持逐实例放置。

plan/preflight       离线规划 / SSH与基础环境检查
start/stop/status    准备并启动 / 停止 / 状态（仅操作本次RUN_TAG）
check/smoke          完整 / 快速真实生成验证
startp/startd        只启动P / D；stopp/stopd只停止对应角色
shell/code           分发本工具 / 工具和wheel发布包
build/publish        在当前节点构建 / 发布uni-ops、ts-plugin wheel
check-release        校验当前节点的wheel发布包
log/log_plog         收集服务 / Ascend日志
evidence             收集各实例逐rank的KV传输证据

build时必须设置NATIVE_MODE=reuse|rebuild；基础环境与模型需事先准备。
abc.txt随代码提供，默认读取同目录文件；使用前chmod 600 abc.txt。
EOF
}
# --local仅供远程协调器调用；开发者不需要手动选择节点级入口。
local_action=""
if [[ "${1:-}" == --local ]]; then
    [[ $# -eq 2 ]] || fail '--local需要一个动作'
    local_action="$2"
else
    [[ $# -le 1 ]] || fail '仅接受一个动作参数'
fi
action="${local_action:-${1:-help}}"
case "$action" in help|-h|--help) usage; exit 0 ;; esac

# ==================== 单实例运行与wheel发布 ====================
base_env() {
    [[ -f "$BASE_ROOT/runtime/env.sh" ]] || fail "先准备已验收的 CANN/TorchNPU/Mooncake/SHMEM 基础运行环境: $BASE_ROOT"
    set +u
    source "$BASE_ROOT/runtime/env.sh"
    set -u
}
check_release() {
    python3 - "$RELEASE_ROOT" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
manifest=json.loads((root/'manifest.json').read_text())
for item in manifest['files']:
    p=root/item['path']
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:
        raise SystemExit(f'checksum mismatch: {p}')
print('release-checksums-ok',manifest['release_id'])
PY
}
repo_git() {
    local repository="$1" owner
    shift
    owner="$(stat -c %U "$repository")"
    if [[ $(id -u) == 0 && "$owner" != root ]]; then
        runuser -u "$owner" -- git -C "$repository" "$@"
    else
        git -C "$repository" "$@"
    fi
}
build() {
    base_env
    [[ "$RELEASE_ID" =~ ^[a-zA-Z0-9._-]+$ ]] || fail 'invalid RELEASE_ID'
    export BUILD_ROOT="${BUILD_ROOT:-/workdir/build-$RELEASE_ID}"
    [[ ! -e "$BUILD_ROOT" ]] || fail "build 目录已存在；请设置新的 BUILD_ROOT: $BUILD_ROOT"
    for src in "$UNI_SRC" "$PLUGIN_SRC" "$TS_SRC"; do
        local source_status
        source_status="$(repo_git "$src" status --porcelain --untracked-files=no)" || fail "无法读取源码仓库: $src"
        [[ -z "$source_status" ]] || fail "有未提交修改: $src"
    done
    mkdir -p "$BUILD_ROOT/uni" "$BUILD_ROOT/plugin" "$BUILD_ROOT/wheels"
    repo_git "$UNI_SRC" archive HEAD | tar -x -C "$BUILD_ROOT/uni"
    repo_git "$PLUGIN_SRC" archive HEAD | tar -x -C "$BUILD_ROOT/plugin"
    case "${NATIVE_MODE:-}" in
        reuse)
            # 两处优化只有 Python 改动；复用已有编译产物而非宣称全量重新编译。
            compgen -G "$UNI_SRC/src/uni_ops/libuni_ops_*_dav_2201.so" >/dev/null || fail '缺少 dav-2201 native 库；使用 NATIVE_MODE=rebuild'
            cp "$UNI_SRC"/src/uni_ops/*.so "$BUILD_ROOT/uni/src/uni_ops/"
            [[ ! -d "$UNI_SRC/src/uni_ops/_opp" ]] || cp -a "$UNI_SRC/src/uni_ops/_opp" "$BUILD_ROOT/uni/src/uni_ops/"
            ;;
        rebuild)
            (cd "$BUILD_ROOT/uni"; bash build_ops_ascendc.sh --arch dav-2201 -j "${JOBS:-8}")
            ;;
        *) fail 'NATIVE_MODE 必须是 reuse 或 rebuild' ;;
    esac
    export NATIVE_MODE
    export UNI_COMMIT="$(repo_git "$UNI_SRC" rev-parse HEAD)"
    export PLUGIN_COMMIT="$(repo_git "$PLUGIN_SRC" rev-parse HEAD)"
    export TS_COMMIT="$(repo_git "$TS_SRC" rev-parse HEAD)"
    python - <<'PY'
import hashlib,json,os,pathlib,re,subprocess,sys,importlib.metadata as m
b=pathlib.Path(os.environ['BUILD_ROOT'])
commits={name:os.environ[var] for name,var in [('uni_ops','UNI_COMMIT'),('ts_plugin','PLUGIN_COMMIT'),('tokenspeed','TS_COMMIT')]}
versions={}
for name,folder,key in [('mt-uni-ops','uni','uni_ops'),('tokenspeed-longcat','plugin','ts_plugin')]:
    p=b/folder/'pyproject.toml';text=p.read_text()
    old=re.search(r'^version = "([^"]+)"',text,re.M).group(1)
    version=old.split('+')[0]+'.post0+g'+commits[key][:8]
    p.write_text(re.sub(r'^version = "[^"]+"',f'version = "{version}"',text,count=1,flags=re.M))
    versions[name]=version
    subprocess.run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(b/'wheels'),str(b/folder)],check=True)
# Tokenspeed Python and its native/kernel packages must remain the matched validated set.
import shutil,zipfile
base=pathlib.Path(os.environ['BASE_ROOT'])
ts=list((base/'runtime/wheels').glob('tokenspeed-0.1.1-*.whl'))
assert len(ts)==1, 'missing validated Tokenspeed 0.1.1 wheel'
with zipfile.ZipFile(ts[0]) as z:
    source=pathlib.Path(os.environ['TS_SRC'])/'python/tokenspeed'
    for item in z.namelist():
        if item.startswith('tokenspeed/') and item.endswith('.py'):
            assert (source/item.removeprefix('tokenspeed/')).read_bytes()==z.read(item), f'Tokenspeed wheel/source mismatch: {item}'
shutil.copy2(ts[0],b/'wheels'/ts[0].name)
versions['tokenspeed']='0.1.1'
native=[]
for p in sorted((b/'uni/src/uni_ops').rglob('*')):
    if p.is_file() and (p.suffix=='.so' or '_opp' in p.parts):
        native.append({'path':str(p.relative_to(b/'uni/src/uni_ops')),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
packages=['torch','torch-npu','tokenspeed-kernel','tokenspeed-kernel-npu','tokenspeed-engine','tokenspeed-scheduler','mooncake-transfer-engine-npu','tokenspeed-smg','supervisor']
report={'release_id':os.environ['RELEASE_ID'],'commits':commits,'versions':versions,'native_mode':os.environ['NATIVE_MODE'],
        'native_files':native,'base_packages':{n:m.version(n) for n in packages},'python':sys.version,'platform':sys.platform}
(b/'build-info.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'versions':versions,'commits':commits},indent=2))
PY
    mkdir -p "$BUILD_ROOT/validation"
    cp "$UNI_SRC/tests/test_dsv41_optimizations.py" "$UNI_SRC/tests/test_flash_refresh_reference.py" "$BUILD_ROOT/validation/"
    cp "$SCRIPT_DIR/run_deepseek_v41_flash.sh" "$SCRIPT_DIR/deployment.py" "$BUILD_ROOT/"
    local guide="$SCRIPT_DIR/../../../docs/serving/deepseek-v41-910b.md"
    [[ -f "$guide" ]] || guide="$SCRIPT_DIR/DEPLOYMENT.md"
    cp "$guide" "$BUILD_ROOT/DEPLOYMENT.md"
    echo "BUILD_ROOT=$BUILD_ROOT; 然后使用相同 BUILD_ROOT 执行 publish"
}
publish() {
    export BUILD_ROOT="${BUILD_ROOT:-/workdir/build-$RELEASE_ID}"
    [[ -f "$BUILD_ROOT/build-info.json" ]] || fail '先执行 build'
    [[ ! -e "$RELEASE_ROOT" ]] || fail "release 已存在，禁止覆盖: $RELEASE_ROOT"
    mkdir -p "$(dirname "$RELEASE_ROOT")"
    local staging
    staging="$(mktemp -d "${RELEASE_ROOT}.staging.XXXXXX")"
    cp -a "$BUILD_ROOT/wheels" "$BUILD_ROOT/build-info.json" "$BUILD_ROOT/run_deepseek_v41_flash.sh" "$BUILD_ROOT/deployment.py" "$BUILD_ROOT/DEPLOYMENT.md" "$staging/"
    [[ ! -d "$BUILD_ROOT/validation" ]] || cp -a "$BUILD_ROOT/validation" "$staging/"
    python3 - "$staging" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]);info=json.loads((root/'build-info.json').read_text())
files=[{'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(root.rglob('*')) if p.is_file()]
(root/'manifest.json').write_text(json.dumps({'release_id':info['release_id'],'files':files},indent=2)+'\n')
(root/'SHA256SUMS').write_text(''.join(f"{p['sha256']}  {p['path']}\n" for p in files))
PY
    mv "$staging" "$RELEASE_ROOT"
    check_release
    if [[ -n "${TWINE_REPOSITORY_URL:-}" ]]; then
        base_env
        # Local-version wheels are for internal repositories supporting PEP440 local versions.
        python -m twine check "$RELEASE_ROOT"/wheels/*.whl
        python -m twine upload --non-interactive --repository-url "$TWINE_REPOSITORY_URL" "$RELEASE_ROOT"/wheels/mt_uni_ops-*.whl "$RELEASE_ROOT"/wheels/tokenspeed_longcat-*.whl
    fi
    echo "已发布文件制品: $RELEASE_ROOT"
}
install() {
    [[ $(id -u) == 0 ]] || fail 'install 请以 root 执行'
    case "$SERVING_MODE" in merged|pd) ;; *) fail 'set SERVING_MODE=merged|pd' ;; esac
    case "$CODE_MODE" in wheel|source) ;; *) fail 'set CODE_MODE=wheel|source' ;; esac
    [[ -n "$NODE_IP" ]] || fail 'set NODE_IP'
    if [[ "$CODE_MODE" == source ]]; then
        [[ -d "$TS_SRC/python/tokenspeed" && -d "$UNI_SRC/src/uni_ops" && -d "$PLUGIN_SRC/tokenspeed_longcat" ]] || fail 'source mode requires TS_SRC/UNI_SRC/PLUGIN_SRC'
    fi
    ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$NODE_IP" || fail 'NODE_IP 不是本机地址'
    [[ "$PD_ROOT" == /* && "$PD_ROOT" != *[[:space:]]* ]] || fail "PD_ROOT 必须是无空白的绝对路径"
    [[ ! -e "$PD_ROOT" ]] || fail "不覆盖已有 PD_ROOT: $PD_ROOT"
    [[ -f "$MODEL_PATH/config.json" && -f "$MODEL_PATH/model.safetensors.index.json" ]] || fail '模型缺少 config/index；先准备完整权重'
    check_release
    base_env
    python - "$RELEASE_ROOT/build-info.json" <<'PY'
import importlib.metadata as m,json,sys
info=json.load(open(sys.argv[1]))
assert sys.version_info[:2]==(3,11), 'requires Python 3.11'
for name,version in info['base_packages'].items():
    assert m.version(name)==version,(name,m.version(name),version)
import torch,torch_npu,mooncake.engine
assert torch.npu.device_count() >= 8
PY
    mkdir -p "$PD_ROOT/runtime" "$PD_ROOT/logs" "$PD_ROOT/validation"
    python -m venv --system-site-packages "$PD_ROOT/venv"
    # 基础环境只读提供依赖；本 venv 的三个 wheel 覆盖同名包，不修改基础 venv。
    local base_site target_site
    base_site="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    target_site="$("$PD_ROOT/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    printf '%s\n' "$base_site" > "$target_site/validated-base.pth"
    "$PD_ROOT/venv/bin/python" -m pip install --no-index --no-deps --force-reinstall "$RELEASE_ROOT"/wheels/*.whl
    # Supervisor console script must use the new venv, not an inherited shebang.
    cp "$BASE_ROOT/runtime/guard/sitecustomize.py" "$target_site/sitecustomize.py"
    cp "$RELEASE_ROOT/build-info.json" "$PD_ROOT/runtime/installed-release.json"
    python3 "$SCRIPT_DIR/deployment.py" render
    set +u; source "$PD_ROOT/runtime/env.sh"; set -u
    python - <<'PY'
import importlib.metadata as m,json,os,pathlib,uni_ops,tokenspeed,tokenspeed_longcat
r=pathlib.Path(os.environ['PD_ROOT']);info=json.loads((r/'runtime/installed-release.json').read_text())
if os.environ['CODE_MODE']=='wheel':
    for name,version in info['versions'].items():
        assert m.version(name)==version,(name,m.version(name),version)
expected=[r/'venv']*3 if os.environ['CODE_MODE']=='wheel' else [pathlib.Path(os.environ['UNI_SRC'])/'src',pathlib.Path(os.environ['TS_SRC'])/'python',pathlib.Path(os.environ['PLUGIN_SRC'])]
for mod,parent in zip([uni_ops,tokenspeed,tokenspeed_longcat],expected):
    assert pathlib.Path(mod.__file__).resolve().is_relative_to(parent.resolve()),mod.__file__
from tokenspeed_longcat.deepseek_v41 import register
register()
print('wheel imports/versions OK; no NPU allocation or service start performed')
PY
    touch "$PD_ROOT/runtime/install-complete"
    echo "安装完成: $PD_ROOT；确认停用旧实例并释放目标卡/端口后执行 start"
}
service_env() {
    [[ -f "$PD_ROOT/runtime/install-complete" ]] || fail 'install is incomplete; use a fresh PD_ROOT'
    set +u; source "$PD_ROOT/runtime/env.sh"; set -u
}
start() {
    service_env
    python - <<'PY'
import json,os,pathlib,re,subprocess
r=pathlib.Path(os.environ['PD_ROOT']);node=json.loads((r/'runtime/node.json').read_text())
ports=node['local_ports']
text=subprocess.check_output(['ss','-H','-ant'],text=True)
for line in text.splitlines():
    fields=line.split()
    if len(fields)>=4:
        local=fields[3]
        if any(local.endswith(':'+str(port)) for port in ports):
            raise SystemExit(f'端口占用（包含非LISTEN连接）: {line}')
devices=node['devices'].split(',');assert len(devices)==8 and len(set(devices))==8
result=subprocess.run(['npu-smi','info'],capture_output=True,text=True)
for dev in devices:
    if result.returncode or not re.search(r'No running processes found in NPU\s+'+re.escape(dev)+r'\b',result.stdout,re.I):
        raise SystemExit(f'NPU {dev} 未确认空闲，请检查 npu-smi info: {result.stdout} {result.stderr}')
PY
    # python -m avoids using Supervisor's inherited console-script shebang.
    python -m supervisor.supervisord -c "$PD_ROOT/runtime/supervisord.conf"
    python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" status
}
verify() {
    service_env
    python - <<'PY'
import concurrent.futures,json,os,pathlib,time,urllib.request
r=pathlib.Path(os.environ['PD_ROOT']);node=json.loads((r/'runtime/node.json').read_text())
assert node.get('owns_gateway',node['role'] in ('prefill','regular')),'run on gateway owner instance'
base='http://'+node['gateway_host']+':'+str(node['http_port']);opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
report={'started_at':time.time(),'checks':[],'passed':False}
path=r/'validation'/('generation-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
def save():path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
def get_workers():
    with opener.open(base+'/workers',timeout=10) as response:return json.load(response)
try:
    deadline=time.monotonic()+1800
    while True:
        try:
            workers=get_workers();active=workers['workers']
            expected=node.get('worker_counts',{'prefill':1,'decode':2} if node['mode']=='pd' else {'regular':1})
            ok=len(active)==sum(expected.values()) and all(w['status']=='ready' and w['is_healthy'] for w in active)
            ok=ok and all(sum(w['worker_type']==kind for w in active)==count for kind,count in expected.items())
            if ok:break
        except Exception:pass
        if time.monotonic()>deadline:raise TimeoutError(f'worker readiness timeout: {expected}')
        time.sleep(5)
    report['workers']=workers;save()
    cases=[('只回答一个数字：1+1=多少？','2',4),('中国的首都是哪里？只回答城市名。','北京',8),('请逐行输出数字1到5，每行一个数字，不要其他文字。','1\n2\n3\n4\n5',32)]
    def request(case):
        prompt,expected,budget=case
        body={'model':node['model'],'messages':[{'role':'user','content':prompt}],'temperature':0,'max_tokens':budget,'stream':False,'chat_template_kwargs':{'enable_thinking':False}}
        start=time.time();req=urllib.request.Request(base+'/v1/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        # Each thread owns its opener.
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(req,timeout=1800) as response:status=response.status;payload=json.load(response)
        choice=payload.get('choices',[{}])[0];text=(choice.get('message',{}).get('content') or '').strip().rstrip('。.!！')
        return {'request':body,'response':payload,'status':status,'started_at':start,'finished_at':time.time(),'passed':status==200 and text==expected and choice.get('finish_reason')=='stop'}
    smoke=os.environ['VERIFY_SCOPE']=='smoke'
    for repeat in range(1 if smoke else 2):
        for case in cases[:1] if smoke else cases:
            result=request(case);result['repeat']=repeat;report['checks'].append(result);save()
            assert result['passed'],result
    report['concurrent_checks']=[]
    if not smoke:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            for future in concurrent.futures.as_completed([pool.submit(request,cases[i]) for i in [2,1]]):
                report['concurrent_checks'].append(future.result());save()
    assert all(c['passed'] for c in report['concurrent_checks'])
    report['workers_after']=get_workers();report['passed']=True
except BaseException as error:
    report['error']=repr(error);raise
finally:
    report['completed_at']=time.time();save();print(path)
print('generation checks passed; PD additionally needs per-rank transfer evidence')
PY
}
evidence() {
    service_env
    python - <<'PY'
import json,os,pathlib,time
r=pathlib.Path(os.environ['PD_ROOT']);events=[]
for p in sorted((r/'logs').glob('engine.log*')):
    for line in p.read_text(errors='replace').splitlines():
        if 'ts_decode_kv_transfer_success' in line or 'ts_request_finished' in line:
            events.append({'file':p.name,'line':line})
path=r/'validation'/('transfer-events-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
path.write_text(json.dumps({'node':json.loads((r/'runtime/node.json').read_text()),'events':events},ensure_ascii=False,indent=2))
print(path,'events:',len(events))
PY
}
if [[ -n "$local_action" || "$action" == build || "$action" == publish || "$action" == check-release ]]; then
    case "$action" in
        build) build ;;
        publish) publish ;;
        check-release) check_release ;;
        prepare)
            if [[ ! -e "$PD_ROOT" ]]; then install; else
                service_env
                python3 "$SCRIPT_DIR/deployment.py" saved
            fi ;;
        start) start ;;
        status|stop)
            service_env
            if [[ "$action" == stop ]]; then
                python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" stop all
                python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" shutdown
            else python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" status; fi ;;
        smoke|verify) export VERIFY_SCOPE="$action"; verify ;;
        evidence) evidence ;;
        *) fail "未知节点动作: $action" ;;
    esac
    exit 0
fi
case "$action" in
    plan|preflight|start|startp|startd|stop|stopp|stopd|status|shell|code|check|smoke|evidence|log|log_plog) ;;
    *) usage; fail "未知命令: $action" ;;
esac

# ==================== 多节点协调 ====================
[[ "$CODE_MODE" == wheel || "$CODE_MODE" == source ]] || fail 'CODE_MODE无效'
[[ "$RUN_TAG" =~ ^[a-zA-Z0-9._-]+$ ]] || fail 'RUN_TAG无效'
for directory in "$TEST_ROOT" "$PD_ROOT" "$REMOTE_TOOLS"; do
    [[ "$directory" == /* && "$directory" != *[[:space:]]* ]] || fail '工作目录必须为无空白的绝对路径'
done
BASE_PD_ROOT="$PD_ROOT"
plan_file="$(mktemp)"
trap 'rm -f "$plan_file"' EXIT
python3 "$SCRIPT_DIR/deployment.py" plan > "$plan_file"
LAYOUT_JSON="$(cat "$plan_file")"
LAYOUT_FINGERPRINT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "$plan_file")"
gateway_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_instance"])' "$plan_file")"
ids=(); roles=(); instance_nodes=(); devices=(); slots=(); nodes=()
gateway_index=0
while IFS=$'\t' read -r name role node mask slot; do
    [[ "$name" != "$gateway_id" ]] || gateway_index="${#ids[@]}"
    ids+=("$name"); roles+=("$role"); instance_nodes+=("$node"); devices+=("$mask"); slots+=("$slot")
    case " ${nodes[*]:-} " in *" $node "*) ;; *) nodes+=("$node") ;; esac
done < <(python3 "$SCRIPT_DIR/deployment.py" rows "$plan_file")
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
SSH_AUTH_MODE="${SSH_AUTH_MODE:-$([[ -n "${SSH_PASSWORD_FILE:-}" ]] && echo password || echo key)}"
SSH_HOST_KEY_CHECKING="${SSH_HOST_KEY_CHECKING:-accept-new}"
case "$SSH_AUTH_MODE" in password|key) ;; *) fail 'SSH_AUTH_MODE必须为password或key' ;; esac
case "$SSH_HOST_KEY_CHECKING" in yes|accept-new|no) ;; *) fail 'SSH_HOST_KEY_CHECKING必须为yes、accept-new或no' ;; esac
ssh_cmd=(ssh -p "$SSH_PORT" -o "StrictHostKeyChecking=$SSH_HOST_KEY_CHECKING" -o ConnectTimeout=15)
scp_cmd=(scp -P "$SSH_PORT" -o "StrictHostKeyChecking=$SSH_HOST_KEY_CHECKING" -o ConnectTimeout=15)
if [[ "$action" != plan && "$SSH_AUTH_MODE" == password ]]; then
    [[ -r "${SSH_PASSWORD_FILE:-}" && -s "$SSH_PASSWORD_FILE" ]] || fail 'SSH_PASSWORD_FILE必须为可读且非空的密码文件'
    command -v sshpass >/dev/null || fail '密码文件方式需要sshpass（系统包：sshpass）'
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
    local index="$1" task="$2" key
    local shared=()
    for key in SERVING_MODE CODE_MODE BASE_ROOT RELEASE_ROOT MODEL_PATH TS_SRC UNI_SRC PLUGIN_SRC \
        HTTP_PORT ENGINE_PORT METRICS_PORT GATEWAY_METRICS_PORT HANDSHAKE_PORT DIST_PORT BOOTSTRAP_PORT \
        POOL_PORT_STRIDE MAX_MODEL_LEN MAX_TOTAL_TOKENS CHUNKED_PREFILL_SIZE PREFILL_MEM_FRACTION \
        DECODE_MEM_FRACTION PREFILL_MAX_NUM_SEQS DECODE_MAX_NUM_SEQS; do
        shared+=("$key=${!key}")
    done
    remote "${instance_nodes[$index]}" env "${shared[@]}" \
        "PD_ROOT=$BASE_PD_ROOT/${ids[$index]}" "NODE_IP=${instance_nodes[$index]}" "INSTANCE_ID=${ids[$index]}" \
        "LAYOUT_JSON=$LAYOUT_JSON" "LAYOUT_FINGERPRINT=$LAYOUT_FINGERPRINT" \
        "DEVICES=${devices[$index]}" bash "$REMOTE_TOOLS/run_deepseek_v41_flash.sh" --local "$task"
}

stage_tools() {
    local node="$1" staging guide="$SCRIPT_DIR/../../../docs/serving/deepseek-v41-910b.md"
    [[ -f "$guide" ]] || guide="$SCRIPT_DIR/DEPLOYMENT.md"
    remote "$node" mkdir -p "$REMOTE_TOOLS"
    staging="$(remote "$node" mktemp -d "$REMOTE_TOOLS/.stage.XXXXXX")"
    "${scp_cmd[@]}" "$SCRIPT_DIR/run_deepseek_v41_flash.sh" "$SCRIPT_DIR/deployment.py" "$SSH_USER@$node:$staging/"
    if [[ -f "$guide" ]]; then "${scp_cmd[@]}" "$guide" "$SSH_USER@$node:$staging/DEPLOYMENT.md"; fi
    # 先传到临时目录，再原子替换，避免协调节点向自身分发时截断源脚本。
    remote "$node" bash -c 'set -e; mv -- "$1"/* "$2"/; rmdir -- "$1"' bash "$staging" "$REMOTE_TOOLS"
    if [[ "$SSH_AUTH_MODE" == password ]]; then
        # 输送到stdin，避免密码出现在命令参数或日志中；创建时即限制权限。
        remote "$node" bash -c 'set -e; umask 077; temporary=$(mktemp "$1.tmp.XXXXXX"); trap '"'"'rm -f "$temporary"'"'"' EXIT; cat > "$temporary"; chmod 600 "$temporary"; mv -f "$temporary" "$1"; trap - EXIT' bash "$REMOTE_TOOLS/abc.txt" < "$SSH_PASSWORD_FILE"
    fi
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
preflight() {
    local node
    for node in "${selected_nodes[@]}"; do
        remote "$node" bash -c '
            set -eu
            command -v bash >/dev/null
            command -v python3 >/dev/null
            if [ "$1" = password ]; then command -v sshpass >/dev/null; fi
            test -r "$2/runtime/env.sh"
            test -d "$3"
            if [ "$4" = source ]; then
                test -d "$5/python/tokenspeed"
                test -d "$6"
                test -d "$7"
            fi
            printf "preflight OK: %s\n" "$8"
        ' bash "$SSH_AUTH_MODE" "$BASE_ROOT" "$MODEL_PATH" "$CODE_MODE" "$TS_SRC" "$UNI_SRC" "$PLUGIN_SRC" "$node"
    done
}
case "$action" in
    preflight) preflight ;;
    plan)
        printf 'tag=%s root=%s\n' "$RUN_TAG" "$BASE_PD_ROOT"
        python3 "$SCRIPT_DIR/deployment.py" summary "$plan_file" ;;
    shell|code)
        for node in "${selected_nodes[@]}"; do stage_tools "$node"; [[ "$action" != code ]] || stage_release "$node"; done ;;
    start|startp|startd)
        preflight
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
    check|smoke)
        task=verify; [[ "$action" != smoke ]] || task=smoke
        run_instance "$gateway_index" "$task" ;;
    evidence)
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
