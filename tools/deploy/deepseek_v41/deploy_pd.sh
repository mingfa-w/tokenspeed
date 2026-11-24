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

# DeepSeek V4.1 Flash 910B: build -> publish -> install -> start -> verify.
# Merged or 1P2D serving; existing services are never stopped automatically.
# Publish checksummed wheel artifacts; optionally upload to a configured index.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_ROOT="${BASE_ROOT:-/workdir/tokenspeed-dsv41-uni-ops}"
UNI_SRC="${UNI_SRC:-$SCRIPT_DIR/../../../../uni-ops}"
PLUGIN_SRC="${PLUGIN_SRC:-$SCRIPT_DIR/../../../../ts-plugin}"
TS_SRC="${TS_SRC:-$SCRIPT_DIR/../../..}"
RELEASE_ID="${RELEASE_ID:-deepseek-v41-dev}"
RELEASE_ROOT="${RELEASE_ROOT:-/workdir/releases/$RELEASE_ID}"
PD_ROOT="${PD_ROOT:-/workdir/deepseek-v41-dev}"
SERVING_MODE="${SERVING_MODE:-}"
CODE_MODE="${CODE_MODE:-}"
HTTP_PORT="${HTTP_PORT:-8010}"
ENGINE_PORT="${ENGINE_PORT:-8120}"
METRICS_PORT="${METRICS_PORT:-8121}"
GATEWAY_METRICS_PORT="${GATEWAY_METRICS_PORT:-8122}"
HANDSHAKE_PORT="${HANDSHAKE_PORT:-9010}"
DIST_PORT="${DIST_PORT:-9600}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
MODEL_PATH="${MODEL_PATH:-/workdir/tokenspeed-dsv41/models/DeepSeek-V4.1-Flash}"
P_IP="${P_IP:-}"
D1_IP="${D1_IP:-}"
D2_IP="${D2_IP:-}"
NODE_IP="${NODE_IP:-}"
DEVICES="${DEVICES:-0,1,2,3,4,5,6,7}"
NATIVE_MODE="${NATIVE_MODE:-}"
export BASE_ROOT UNI_SRC PLUGIN_SRC TS_SRC RELEASE_ROOT RELEASE_ID PD_ROOT MODEL_PATH P_IP D1_IP D2_IP NODE_IP DEVICES
export SERVING_MODE CODE_MODE HTTP_PORT ENGINE_PORT METRICS_PORT GATEWAY_METRICS_PORT HANDSHAKE_PORT DIST_PORT BOOTSTRAP_PORT
fail() { echo "ERROR: $*" >&2; exit 1; }
base_env() {
    [[ -f "$BASE_ROOT/runtime/env.sh" ]] || fail "先准备已验收的 CANN/TorchNPU/Mooncake/SHMEM 基础运行环境: $BASE_ROOT"
    set +u
    source "$BASE_ROOT/runtime/env.sh"
    set -u
}
usage() {
    cat <<'EOF'
Usage: bash deploy_pd.sh build|publish|check-release|install|up|cluster-up|start|status|stop|smoke|verify|evidence

install/up require SERVING_MODE=merged|pd and CODE_MODE=wheel|source.
merged: one TP8 engine + SMG; pd: one TP8 P and two TP8 D engines.
Set NODE_IP; pd additionally requires P_IP, D1_IP, D2_IP on all nodes.
up installs a fresh PD_ROOT if absent, starts this node and runs a smoke request
on the merged/P node. Start the P first, then run up on both D nodes in parallel.
For PD, use start on P, up on D1/D2, then smoke/verify on P.

build: build uni-ops/ts-plugin wheels; NATIVE_MODE=reuse|rebuild.
publish: publish file artifacts to new RELEASE_ROOT; optional TWINE_REPOSITORY_URL.
install: create a fresh venv using the validated BASE_ROOT dependencies.
start/status/stop: manage only this PD_ROOT; start refuses busy devices/ports.
smoke: wait for workers and check one real generation; verify also runs 6+2 requests.
evidence: retain KV-transfer/completion log events, without claiming acceptance.

See docs/serving/deepseek-v41-910b.md (or released DEPLOYMENT.md).
BASE_ROOT, model files and a compatible wheel release are required; not a bare-OS installer.
EOF
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
    case "$NATIVE_MODE" in
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
    cp "$SCRIPT_DIR/deploy_pd.sh" "$SCRIPT_DIR/configure.py" "$BUILD_ROOT/"
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
    cp -a "$BUILD_ROOT/wheels" "$BUILD_ROOT/build-info.json" "$BUILD_ROOT/deploy_pd.sh" "$BUILD_ROOT/configure.py" "$BUILD_ROOT/DEPLOYMENT.md" "$staging/"
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
    if [[ "$SERVING_MODE" == pd ]]; then
        [[ -n "$P_IP" && -n "$D1_IP" && -n "$D2_IP" ]] || fail 'PD requires P_IP/D1_IP/D2_IP'
        [[ "$NODE_IP" == "$P_IP" || "$NODE_IP" == "$D1_IP" || "$NODE_IP" == "$D2_IP" ]] || fail 'NODE_IP is not in PD topology'
    fi
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
    python3 "$SCRIPT_DIR/configure.py"
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
assert node['role'] in ('prefill','regular'),'run on merged/P node'
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
            expected={'prefill':1,'decode':2} if node['mode']=='pd' else {'regular':1}
            ok=len(active)==sum(expected.values()) and all(w['status']=='ready' and w['is_healthy'] for w in active)
            ok=ok and all(sum(w['worker_type']==kind for w in active)==count for kind,count in expected.items())
            if ok:break
        except Exception:pass
        if time.monotonic()>deadline:raise TimeoutError('1P2D readiness timeout')
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
cluster_up() {
    [[ "$SERVING_MODE" == pd ]] || fail 'cluster-up requires SERVING_MODE=pd'
    [[ -n "$P_IP" && -n "$D1_IP" && -n "$D2_IP" ]] || fail 'set P_IP/D1_IP/D2_IP'
    local node action command remote_script
    remote_script="${REMOTE_SCRIPT:-$SCRIPT_DIR/deploy_pd.sh}"
    for action in install start; do
        for node in "$P_IP" "$D1_IP" "$D2_IP"; do
            printf -v command '%q ' env "SERVING_MODE=$SERVING_MODE" "CODE_MODE=$CODE_MODE" \
                "BASE_ROOT=$BASE_ROOT" "RELEASE_ROOT=$RELEASE_ROOT" "MODEL_PATH=$MODEL_PATH" \
                "PD_ROOT=$PD_ROOT" "NODE_IP=$node" "P_IP=$P_IP" "D1_IP=$D1_IP" "D2_IP=$D2_IP" \
                "DEVICES=$DEVICES" "HTTP_PORT=$HTTP_PORT" "ENGINE_PORT=$ENGINE_PORT" \
                "METRICS_PORT=$METRICS_PORT" "GATEWAY_METRICS_PORT=$GATEWAY_METRICS_PORT" \
                "HANDSHAKE_PORT=$HANDSHAKE_PORT" "DIST_PORT=$DIST_PORT" "BOOTSTRAP_PORT=$BOOTSTRAP_PORT" \
                "TS_SRC=$TS_SRC" "UNI_SRC=$UNI_SRC" "PLUGIN_SRC=$PLUGIN_SRC" bash "$remote_script" "$action"
            ssh -o BatchMode=yes "${SSH_USER:-root}@$node" "$command"
        done
    done
    printf -v command '%q ' env "PD_ROOT=$PD_ROOT" bash "$remote_script" smoke
    ssh -o BatchMode=yes "${SSH_USER:-root}@$P_IP" "$command"
}

case "${1:-help}" in
    help|-h|--help) usage ;;
    build) build ;;
    publish) publish ;;
    check-release) check_release ;;
    install) install ;;
    cluster-up) cluster_up ;;
    start) start ;;
    status|stop)
        service_env
        if [[ "$1" == stop ]]; then
            python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" stop all
            python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" shutdown
        else python -m supervisor.supervisorctl -c "$PD_ROOT/runtime/supervisord.conf" status; fi ;;
    smoke|verify)
        export VERIFY_SCOPE="$1"
        verify ;;
    up)
        if [[ ! -e "$PD_ROOT" ]]; then install; fi
        python3 - <<'PYCHECK'
import json,os
node=json.load(open(os.environ['PD_ROOT']+'/runtime/node.json'))
for key,env in [('mode','SERVING_MODE'),('code_mode','CODE_MODE')]:
    value=os.environ[env]
    if value and value!=node[key]:raise SystemExit(f'{env} differs from saved config; use a new PD_ROOT')
PYCHECK
        start
        if python3 -c 'import json,os,sys; sys.exit(json.load(open(os.environ["PD_ROOT"]+"/runtime/node.json"))["role"]=="decode")'; then
            export VERIFY_SCOPE=smoke
            verify
        fi ;;
    evidence) evidence ;;
    *) usage; exit 2 ;;
esac
