# DeepSeek V4.1 Flash 910B 开发部署指南

部署入口为 `tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh`，支持P/D分离、同机混合P/D和P/D合并部署。每个实例使用TP8/EP8/PP1；三台16设备910B的默认布局为2P4D，共48个设备。

## 代码、实例与镜像

| 组件 | 代码链接 | 分支 | 运行代码基线 |
|---|---|---|---|
| Tokenspeed | [仓库](https://github.com/mingfa-w/tokenspeed) | `uni-ops/dpk-flash-v41` | `913449a39227d95c43525235a5643c5d0a566215` |
| uni-ops | [仓库](https://dev.sankuai.com/code/repo-detail/mptech-llmp/uni-ops/file/list) | `deepseek-v4.1-flash` | `25e26b43242df67dacf5821d5b73090ba90f8978` |
| ts-plugin | [仓库](https://dev.sankuai.com/code/repo-detail/mptech-llmp/ts-plugin/file/list) | `deepseek-v4.1-flash` | `b22ddb94b7cb35132fd0301303c086ab87912472` |

部署工具版本通过 `git rev-parse HEAD` 查询；运行包版本和源码提交记录在发布包的 `build-info.json` 中。ts-plugin发行包名为 `tokenspeed-longcat`，uni-ops发行包名为 `mt-uni-ops`。

获取源码时使用相应分支：

```bash
export CODE_ROOT=/home/mingfa/code
mkdir -p "$CODE_ROOT"
git clone --branch uni-ops/dpk-flash-v41 --single-branch https://github.com/mingfa-w/tokenspeed.git "$CODE_ROOT/tokenspeed"
git clone --branch deepseek-v4.1-flash --single-branch ssh://git@git.sankuai.com/mptech-llmp/uni-ops.git "$CODE_ROOT/uni-ops"
git clone --branch deepseek-v4.1-flash --single-branch ssh://git@git.sankuai.com/mptech-llmp/ts-plugin.git "$CODE_ROOT/ts-plugin"
```

开发实例：[MLP psxbwxrtsp7nea](https://mlp.sankuai.com/ml/#/run/detail?jobId=psxbwxrtsp7nea&tab=implement)。使用镜像：

```text
registry-offlinebiz.sankuai.com/custom_prod/com.sankuai.data.hadoop.gpu/efficient-llm/serving_tokenspeed_npu_3b_custom_20261103_ts_f8cb94a1b_ops_f600e37fd_fix01_3d7e4906:1.0.2
```

开发节点为10.255.76.138、10.255.111.50、10.255.170.42，SSH端口8022。使用其他镜像时，按“环境准备”中的版本要求检查软件栈。

## 开发环境与快速开始

将以下文件放在同一目录，通过Shell入口执行部署命令：

| 文件 | 用途 |
|---|---|
| `run_deepseek_v41_flash.sh` | 部署配置、wheel构建发布、文件分发、服务管理及验收 |
| `deployment.py` | 布局校验与运行配置生成，由Shell入口调用 |
| `abc.txt` | 随代码提供的root密码文件，默认从入口同目录读取 |

开发环境使用以下目录和服务配置。管理现有实例时设置 `RUN_TAG=dsv41_flash_remote_152b4e8`；新建实例时自行指定RUN_TAG，脚本默认值为 `dsv41_flash_dev`。

| 项目 | 配置 |
|---|---|
| 源码入口目录 | `/home/mingfa/code/tokenspeed/tools/deploy/deepseek_v41` |
| 当前运行标记 | `dsv41_flash_remote_152b4e8` |
| 节点侧工具目录 | `/workdir/deepseek-v41-test/tools_dsv41_flash_remote_152b4e8` |
| 实例根目录 | `/workdir/deepseek-v41-test/run_dsv41_flash_remote_152b4e8` |
| 基础环境 `BASE_ROOT` | `/workdir/tokenspeed-dsv41-uni-ops` |
| 完整模型 `MODEL_PATH` | `/workdir/tokenspeed-dsv41/models/DeepSeek-V4.1-Flash` |
| wheel发布包 `RELEASE_ROOT` | `/workdir/releases/dsv41-pd-20261124` |
| 网关 | `http://10.255.76.138:8010` |
| SSH | `root`，端口 `8022`，`sshpass -f abc.txt` |

三台910B均使用16设备，每实例TP8/EP8/PP1。`HOSTS` 按主机顺序分成0–7、8–15两组，前两个实例做P，其余做D：

| 节点 | 实例 | 设备 | 角色 |
|---|---|---|---|
| 10.255.76.138 | p0 / p1 | 0–7 / 8–15 | 2P，16设备 |
| 10.255.111.50 | d0 / d1 | 0–7 / 8–15 | 2D，16设备 |
| 10.255.170.42 | d2 / d3 | 0–7 / 8–15 | 2D，16设备 |

### 现有实例管理

在任一配置节点检查整套服务：

```bash
cd /workdir/deepseek-v41-test/tools_dsv41_flash_remote_152b4e8
export RUN_TAG=dsv41_flash_remote_152b4e8
chmod 600 abc.txt
bash run_deepseek_v41_flash.sh status
bash run_deepseek_v41_flash.sh smoke
# 完整验收：6次顺序请求＋2次并发请求，再收集各实例KV传输事件。
bash run_deepseek_v41_flash.sh check
bash run_deepseek_v41_flash.sh evidence
```

### 首次部署：准备、规划、启动

在三台节点准备相同的基础环境、完整模型；源码模式还需三个源码树。执行节点应能读取wheel发布包，或所有目标节点已存在同一发布包。基础环境和wheel的完整准备方法见后文。

在协调节点以root账号执行。启动前停止占用目标设备和端口的服务；已有实例运行时，使用上一节的查看与验证命令。

```bash
cd /home/mingfa/code/tokenspeed/tools/deploy/deepseek_v41
chmod 600 abc.txt
command -v sshpass  # 缺少时由管理员通过系统包管理器安装sshpass
# 设置脚本顶部的HOSTS、P/D数量和目录。
bash run_deepseek_v41_flash.sh plan
bash run_deepseek_v41_flash.sh preflight
bash run_deepseek_v41_flash.sh start
bash run_deepseek_v41_flash.sh status
bash run_deepseek_v41_flash.sh check
```

`plan` 仅在本机校验布局；`preflight` 检查SSH、基础环境脚本和模型目录，不检查完整模型可用性。`start` 自动分发工具、校验或复制wheel发布包、准备所有实例，再依次启动网关归属实例、其余P和D，最后等待全部worker健康并执行真实问答。生成验收使用 `smoke` 或 `check`。

需要停止整套当前布局时，单独执行：

```bash
bash run_deepseek_v41_flash.sh stop
```

重启时先执行 `stop`，待设备和端口释放后再执行 `start`。切换布局、模型或运行参数时使用新的 `RUN_TAG`，同时确认旧布局已停止；新目录不会自动避让旧布局占用的设备和端口。

### 任一节点作为协调机

`start` 已包含分发步骤；若暂不启动，可单独执行 `shell`（只分发工具和密码文件）或 `code`（再分发并校验wheel发布包）。分发后的入口、辅助程序、说明文档与 `abc.txt` 位于各节点 `$TEST_ROOT/tools_$RUN_TAG`，按上表运行标记执行时即对应当前节点侧工具目录。下载包中的部署文件可放在任一节点的同一目录执行。

`abc.txt` 第一行用于SSH密码认证。其他环境可修改该文件或设置 `SSH_PASSWORD_FILE=/absolute/path/to/password`。分发使用SSH标准输入和临时文件原子替换，远端密码文件权限为600，密码不进入命令参数和日志。

所有节点须已有bash、python3和sshpass；模型、基础环境和源码不会自动分发。首次连接使用 `StrictHostKeyChecking=accept-new`，后续主机密钥变化会拒绝连接。预置known_hosts时可设 `SSH_HOST_KEY_CHECKING=yes`；关闭主机密钥校验时显式设为 `no`。使用密钥认证时设置 `SSH_AUTH_MODE=key`，不会使用或分发密码文件。

环境变量覆盖仅作用于当前命令进程。使用非默认主机、数量、路径或 `REMOTE_TOOLS` 时，各协调节点必须设置同一组变量；也可把共同配置写入入口顶部后重新分发。环境变量不会持久化到其他节点。

## 调整布局与运行参数

### 自动P/D布局

常用配置为 `HOSTS`、`PREFILL_INSTANCES`、`DECODE_INSTANCES` 和路径。默认三台主机共六个8设备组，P/D数量之和必须为6；数量决定实例数，不是每实例的EP大小。

| 参数 | 含义 |
|---|---|
| `HOSTS` | 按顺序填写主机IP，空格或换行分隔；每主机自动分为两个8设备组 |
| `PREFILL_INSTANCES` / `DECODE_INSTANCES` | 默认2/4，即16设备P＋32设备D；每实例固定TP8/EP8/PP1 |
| `RUN_TAG` / `TEST_ROOT` | 默认 `dsv41_flash_dev` / `/workdir/deepseek-v41-test` |
| `PD_ROOT` / `REMOTE_TOOLS` | 默认 `$TEST_ROOT/run_$RUN_TAG` / `$TEST_ROOT/tools_$RUN_TAG` |
| `MODEL_PATH` / `BASE_ROOT` / `RELEASE_ROOT` | 完整模型、基础环境和wheel发布包目录 |
| `CODE_MODE` | `wheel`测试安装包；`source`加载本地源码并启用开发热重载 |
| `PREFILL_MEM_FRACTION` / `DECODE_MEM_FRACTION` | P/D显存比例，默认均0.85 |
| `PREFILL_MAX_NUM_SEQS` / `DECODE_MAX_NUM_SEQS` | 每实例并发，默认均1 |
| `MAX_MODEL_LEN` / `MAX_TOTAL_TOKENS` / `CHUNKED_PREFILL_SIZE` | 默认512/1024/512，增大后重新验收 |
| `SSH_USER` / `SSH_PORT` / `SSH_AUTH_MODE` | 默认root / 8022 / password |

例如1P5D：

```bash
PREFILL_INSTANCES=1 DECODE_INSTANCES=5 RUN_TAG=flash_1p5d \
  bash run_deepseek_v41_flash.sh plan
# 释放目标资源后，用相同环境变量将plan改为start；后续status/stop也保留这些变量。
```

### 显式布局：同机混合P/D

需要逐实例安排位置时，设置 `PD_LAYOUT` 或 `PD_LAYOUT_FILE` 覆盖自动布局。仍使用同一入口，此时不会按HOSTS和P/D数量生成布局。每行格式为 `实例ID 角色 IP 设备组`：

```bash
export SERVING_MODE=pd CODE_MODE=wheel RUN_TAG=flash_3p3d
unset PD_LAYOUT_FILE
export PD_LAYOUT='
p0 prefill 10.255.76.138 0,1,2,3,4,5,6,7
d0 decode  10.255.76.138 8,9,10,11,12,13,14,15
p1 prefill 10.255.111.50 0,1,2,3,4,5,6,7
d1 decode  10.255.111.50 8,9,10,11,12,13,14,15
p2 prefill 10.255.170.42 0,1,2,3,4,5,6,7
d2 decode  10.255.170.42 8,9,10,11,12,13,14,15
'
bash run_deepseek_v41_flash.sh plan
# 释放目标资源后执行start，后续命令保留同一布局配置。
```

该配置每台1P＋1D，共3P3D。P/D设备数分别为对应行数×8。也可将行内容保存到本地文件，用 `PD_LAYOUT_FILE=/path/to/my.layout` 读取，同时取消 `PD_LAYOUT`。文件还支持JSON实例列表或 `{"instances":[...]}`，每实例包含 `id/role/node/devices`；devices为逗号分隔字符串或数字列表。文件只作为数据解析，不执行shell命令或变量展开。

默认 `REQUIRE_FULL_NODE=1` 要求每台被引用机器的0–15设备完整分配、无重叠；明确测试部分设备时可设为0。每实例必须使用0–7或8–15完整HCCS组。实例ID必须唯一，修改布局后使用新的RUN_TAG/PD_ROOT。

### P/D合并部署

使用 `SERVING_MODE=merged`，HOSTS中的每个8设备组均作为一个合并实例，不使用P/D数量参数。单机16设备部署两个合并实例示例：

```bash
unset PD_LAYOUT PD_LAYOUT_FILE
SERVING_MODE=merged HOSTS=10.255.76.138 RUN_TAG=flash_merged \
  bash run_deepseek_v41_flash.sh plan
# 释放该主机的设备和端口后，用相同环境变量将plan改为start。
```

合并模式的显式布局使用 `regular` 角色。两种模式共用设备/端口校验、安装、Supervisor和生成验收流程。

## 环境文件与目录

运行环境使用以下两类环境脚本：

- `$BASE_ROOT/runtime/env.sh`：按下一章步骤3创建，激活基础venv并设置CANN/SHMEM。
- `$PD_ROOT/<实例ID>/runtime/env.sh`：准备该实例时生成，设置部署venv、设备、模型及源码路径。

实例目录还包含 `venv/`、`runtime/node.json`、`runtime/supervisord.conf`、`logs/` 和 `validation/`。源码目录、基础环境和实例目录不可互换；先生成环境文件，再执行source。

## 环境准备

每个节点需要Linux x86_64、Python 3.11、完整本地模型（包括Engram表和tokenizer）、CANN/驱动及匹配的NPU软件栈。当前配方为eager、纯文本、numerics=auto、greedy。以下步骤创建独立的基础venv，部署实例再以只读方式继承其依赖。

### 1. 明确基础镜像与安装材料

基础venv使用Python 3.11和 `--system-site-packages`：Torch、TorchNPU和Mooncake NPU由基础镜像提供，Tokenspeed等包安装到venv。新建venv通过共享系统包访问镜像中的依赖。

先准备以下材料，并在每个节点使用相同版本：

| 材料 | 要求 |
|---|---|
| 基础 Python | Linux x86_64、Python 3.11；推荐使用已验证 Ascend 镜像中的 Python |
| Torch / TorchNPU | 参考组合为 Torch `2.9.0+cpu`、TorchNPU `2.9.0.post2` 系列；定制构建的完整版本号以环境快照为准 |
| Mooncake NPU | `mooncake-transfer-engine-npu==0.3.13`，提供 `mooncake.engine` |
| Tokenspeed | `tokenspeed==0.1.1`、engine `0.1.0`、scheduler `0.1.57`、kernel-npu `0.1.0`；kernel 的完整开发版本必须来自同一已验证构建 |
| 网关与进程管理 | `tokenspeed-smg` 的已验证构建、匹配的 grpc-proto/grpc-servicer，以及 `supervisor==4.2.5` |
| Triton | `triton==3.2.0`、`triton-ascend==3.2.1`；安装顺序见下文 |
| 离线 wheel 目录 | 上述非镜像包及其传递依赖，包括 build、wheel、setuptools、网络库、模型/tokenizer 库等；仅有两个业务依赖 wheel 不够 |
| CANN / SHMEM | 已安装 CANN 9.0 与驱动；准备与 native 包匹配的 SHMEM SDK 或运行库 |

按表中版本准备离线wheel和传递依赖，使用NPU版Mooncake。

从参考环境导出安装材料时，`REFERENCE_ROOT` 指向包含venv和wheel的部署目录，`BASE_ROOT` 指向待创建的基础环境目录。若已有完整的 `runtime/` 安装材料，将其复制到 `$BASE_ROOT/runtime/` 后，从步骤2开始：

```bash
export REFERENCE_ROOT=/path/to/validated-runtime
export BASE_ROOT=/workdir/deepseek-v41-base
export SYSTEM_PYTHON=/path/to/ascend-image/bin/python3.11
export SHMEM_SDK=/path/to/matching-shmem-sdk
export CANN_SET_ENV=/usr/local/Ascend/ascend-toolkit/set_env.sh

test ! -e "$BASE_ROOT"
mkdir -p "$BASE_ROOT/runtime/wheels" "$BASE_ROOT/runtime/guard"
cp -a "$REFERENCE_ROOT/runtime/wheels/." "$BASE_ROOT/runtime/wheels/"
mkdir -p "$BASE_ROOT/runtime/shmem"
cp -aL "$SHMEM_SDK/." "$BASE_ROOT/runtime/shmem/"
# 读取参考环境时使用其 Python 和动态库，不要求已有 runtime/env.sh。
source "$CANN_SET_ENV"
export REFERENCE_PYTHON="$REFERENCE_ROOT/venv/bin/python"
REFERENCE_PYTHON_LIB="$("$REFERENCE_PYTHON" -c 'import sys; print(sys.base_prefix + "/lib")')"
export LD_LIBRARY_PATH="$REFERENCE_PYTHON_LIB:/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:$REFERENCE_ROOT/runtime/shmem/lib:$REFERENCE_ROOT/runtime/shmem/lib64:${LD_LIBRARY_PATH:-}"
"$REFERENCE_PYTHON" - <<'PY'
import importlib.metadata as m
import json
import os
from pathlib import Path
import torch
import torch_npu

names = ["torch", "torch-npu", "mooncake-transfer-engine-npu",
         "tokenspeed", "tokenspeed-kernel", "tokenspeed-kernel-npu",
         "tokenspeed-engine", "tokenspeed-scheduler", "tokenspeed-smg", "supervisor"]
info = {"packages": {name: m.version(name) for name in names},
        "cxx11_abi": bool(torch._C._GLIBCXX_USE_CXX11_ABI)}
Path(os.environ["BASE_ROOT"], "runtime/base-environment.json").write_text(
    json.dumps(info, indent=2) + "\n")
PY
"$REFERENCE_PYTHON" -m pip list --format=freeze > "$BASE_ROOT/runtime/reference-packages.txt"
"$REFERENCE_PYTHON" -m pip check > "$BASE_ROOT/runtime/reference-pip-check.txt" 2>&1 || true
```

材料导出只需在已验收环境上做一次。部署到新节点时，将导出的 `runtime/` 目录复制到该节点的 `$BASE_ROOT/runtime/`，重新设置 `BASE_ROOT`、`SYSTEM_PYTHON` 和 `CANN_SET_ENV`，再从步骤 2 开始；新节点不需要保留 `REFERENCE_ROOT`。

导出材料包含wheel、SHMEM和版本记录。`reference-packages.txt` 用于版本核对，缺失wheel需从对应构建发布方获取；基础镜像应提供相同版本的系统包。

### 2. 创建 venv 并安装离线包

先确认所选基础 Python 已经可以导入镜像提供的三个组件。若尚未配置动态库，可先加载 CANN，并按基础镜像要求将其 Python 的 `lib` 目录和驱动库加入 `LD_LIBRARY_PATH`：

```bash
source "$CANN_SET_ENV"
export LD_LIBRARY_PATH="$("$SYSTEM_PYTHON" -c 'import sys; print(sys.base_prefix + "/lib")'):/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:${LD_LIBRARY_PATH:-}"
"$SYSTEM_PYTHON" - <<'PY'
import sys
import torch
import torch_npu
import mooncake.engine
assert sys.version_info[:2] == (3, 11)
print("Torch:", torch.__version__, "TorchNPU:", torch_npu.__version__)
PY

"$SYSTEM_PYTHON" -m venv --system-site-packages "$BASE_ROOT/venv"
source "$BASE_ROOT/venv/bin/activate"
python - <<'PY'
import os
from pathlib import Path
import subprocess
import sys

wheel_dir = Path(os.environ["BASE_ROOT"], "runtime/wheels")
wheels = sorted(wheel_dir.glob("*.whl"))
assert wheels, "离线 wheel 目录为空"
# uni-ops 和 ts-plugin 由后续发布包安装到部署 venv，不在此安装旧版本。
base = [p for p in wheels if not p.name.startswith(("mt_uni_ops-", "tokenspeed_longcat-"))]
regular = [str(p) for p in base if not p.name.startswith("triton_ascend-")]
ascend = [str(p) for p in base if p.name.startswith("triton_ascend-")]
assert len(ascend) == 1, "需要且只能保留一个匹配的 triton-ascend wheel"
for group in [regular, ascend]:
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                    "--force-reinstall", *group], check=True)
PY
```

`--no-index --no-deps` 使用离线版本集合安装，不解析或补充传递依赖。安装完成后执行版本、导入和依赖检查。

Triton-Ascend 会覆盖上游 Triton 的部分文件，因此先安装其余 wheel，最后安装 `triton-ascend`；之后不要单独重装上游 Triton。

**如果使用未预装 Torch/NPU/Mooncake 的普通 Python：**需要事先取得匹配的 Torch CPU、TorchNPU、Mooncake NPU wheel 和完整传递依赖。创建不带 `--system-site-packages` 的独立 venv，然后把这些 wheel 也纳入上面的离线安装集合。核心包的安装形式如下，路径需替换成实际文件：

```bash
# 使用普通Python创建独立venv时，替代上述创建和安装步骤。
test ! -e "$BASE_ROOT/venv"
"$SYSTEM_PYTHON" -m venv "$BASE_ROOT/venv"
source "$BASE_ROOT/venv/bin/activate"
python -m pip install --no-index --no-deps \
  "/path/to/wheels/torch-<完整版本>-cp311-cp311-<平台>.whl" \
  "/path/to/wheels/torch_npu-<完整版本>-cp311-cp311-linux_x86_64.whl" \
  "/path/to/wheels/mooncake_transfer_engine_npu-0.3.13-cp311-cp311-linux_x86_64.whl"
```

独立venv还需安装其余离线包和传递依赖，并完成步骤4的版本、ABI及导入检查。

### 3. 生成环境脚本和算子导入配置

将下面内容写入新基础环境的 `runtime/env.sh`。脚本先保留 CANN 设置的 PATH，再激活 venv，避免嵌套激活清掉 `bisheng` 等编译器路径：

```bash
cat > "$BASE_ROOT/runtime/env.sh" <<'SH'
#!/usr/bin/env bash
TS_RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS_DEPLOY_ROOT="$(dirname "$TS_RUNTIME_DIR")"
source "${CANN_SET_ENV:-/usr/local/Ascend/ascend-toolkit/set_env.sh}"
TS_CANN_PATH="$PATH"
source "$TS_DEPLOY_ROOT/venv/bin/activate"
export PATH="$TS_DEPLOY_ROOT/venv/bin:$TS_CANN_PATH"
unset TS_CANN_PATH
export SHMEM_HOME="$TS_RUNTIME_DIR/shmem"
TS_PYTHON_LIB="$(python -c 'import sys; print(sys.base_prefix + "/lib")')"
export LD_LIBRARY_PATH="$TS_PYTHON_LIB:/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:$SHMEM_HOME/lib:$SHMEM_HOME/lib64:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$TS_RUNTIME_DIR/guard:${PYTHONPATH:-}"
export TOKENSPEED_DISABLE_PLUGINS=longcat TOKENSPEED_KERNEL_DISABLE_PLUGINS=longcat
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
export NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1
export TS_MODEL_PATH="${MODEL_PATH:-/models/DeepSeek-V4.1-Flash}"
export TS_RUNTIME_DIR TS_DEPLOY_ROOT
SH

cat > "$BASE_ROOT/runtime/guard/sitecustomize.py" <<'PY'
import importlib.abc
import sys

class UniOpsOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"flash_ops", "flash_npu_kernel", "custom_ops", "fluent_sfa1_ops"}:
            raise ImportError(f"{fullname} is disabled: this deployment uses uni_ops")

sys.meta_path.insert(0, UniOpsOnly())
PY
source "$BASE_ROOT/runtime/env.sh"
```

`SHMEM_HOME` 必须包含实际匹配的动态库。用于运行的库目录不等于完整开发 SDK；若选择重新编译需要 SHMEM 的算子，还需准备相应头文件和构建依赖。

### 4. 版本、动态库与导入自检

```bash
command -v python
command -v bisheng
python - <<'PY'
import importlib.metadata as m
import json
import os
from pathlib import Path
import torch
import torch_npu
import mooncake.engine
import tokenspeed
import tokenspeed_engine
import tokenspeed_scheduler
import tokenspeed_kernel
import supervisor

expected = json.loads(Path(os.environ["BASE_ROOT"], "runtime/base-environment.json").read_text())
for name, version in expected["packages"].items():
    actual = m.version(name)
    assert actual == version, (name, actual, version)
assert bool(torch._C._GLIBCXX_USE_CXX11_ABI) == expected["cxx11_abi"]
print("基础包版本、ABI 和导入检查通过")
PY
python -m smg --help
python -m supervisor.supervisorctl --help
python -m pip check > "$BASE_ROOT/runtime/base-pip-check.txt" 2>&1 || true
cat "$BASE_ROOT/runtime/base-pip-check.txt"
```

核对 `pip check` 输出与参考环境的差异。参考镜像的已知元数据差异包括：`tokenspeed-mooncake` 声明与NPU替代发行包名称不同，Triton-Ascend的pytest版本约束，以及TorchAir与protobuf的版本约束。保留检查记录，解决新增冲突和导入错误后再启动模型。本部署使用eager模式，不启用TorchAir图编译；Mooncake应保持NPU版本。

最后在空闲设备上执行部署后的 `smoke`，确认模型加载和真实生成；仅导入通过不能证明服务通过验收。基础环境准备完成后，继续下文的wheel构建与部署。


## 构建与发布wheel

当前已有可用发布包时可直接部署，无需重复构建。依赖代码变更后，在匹配的构建机上构建新发布包；依赖安装必须在venv中，三个源代码树须无未提交的已跟踪修改，BUILD_ROOT和RELEASE_ROOT须使用尚不存在的新目录：

```bash
export BASE_ROOT=/workdir/tokenspeed-dsv41-uni-ops
source "$BASE_ROOT/runtime/env.sh"
export TS_SRC=/home/mingfa/code/tokenspeed
export UNI_SRC=/home/mingfa/code/uni-ops
export PLUGIN_SRC=/home/mingfa/code/ts-plugin
export RELEASE_ID=deepseek-v41-dev
export BUILD_ROOT=/workdir/build-$RELEASE_ID
export RELEASE_ROOT=/workdir/releases/$RELEASE_ID
NATIVE_MODE=reuse bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" build
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" publish
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" check-release
```

`reuse`打包源码树现有dav-2201 native产物并记录哈希；需要重编译则选 `NATIVE_MODE=rebuild`，在隔离暂存目录运行AscendC构建。构建需要build、wheel、setuptools>=77；重编译还需CANN SDK、CMake和编译器，脚本不自动升级它们。

发布目录含两个新依赖wheel、已验证Tokenspeed Python wheel、两个部署文件、本文、版本及SHA256清单。依赖wheel使用 `.post0+g<commit>` 版本，不改原检出目录。Tokenspeed Python wheel与源码逐文件比对后复用；engine/kernel/scheduler由基础环境提供。先安装其他包，最后安装Triton-Ascend，不能事后单独重装上游Triton。

发布包包含部署程序和文档，SSH密码文件单独提供。从发布包执行入口时，将abc.txt放在同目录或设置SSH_PASSWORD_FILE；密钥认证使用SSH_AUTH_MODE=key。

开发环境使用的发布包为 `dsv41-pd-20261124`，包含 `mt-uni-ops==0.1.0.post20261124+g25e26b43`、`tokenspeed-longcat==0.1.0.post20261124+gb22ddb94` 和 `tokenspeed==0.1.1`。节点侧部署程序通过源码入口分发，依赖wheel从发布包安装。

publish采用暂存目录后重命名，拒绝覆盖已有release。整个发布目录需在控制机可读；start会校验远端已有目录，缺失时从控制机分发。它不会复制模型、基础环境或源码。source模式的三个源码路径需在各节点准备好并保持一致。

若需上传包仓库，首次publish时显式提供 `TWINE_REPOSITORY_URL`，凭据由环境或凭据管理器提供；仓库需支持PEP440本地版本。该动作只上传uni-ops和ts-plugin。上传失败保留文件制品，可用 `python -m twine upload` 重试，不覆盖release。文件发布本身不等于上传公共PyPI。

## 启停、端口与验收

所有动作均由 `run_deepseek_v41_flash.sh` 执行：

| 命令 | 行为 |
|---|---|
| `plan` | 离线校验并显示各角色卡数、设备、端口和网关 |
| `preflight` | 检查所有目标节点SSH、协调工具、基础环境和模型目录，不启动服务 |
| `build/publish/check-release` | 在执行节点构建wheel、发布不可覆盖的文件制品、校验SHA256清单 |
| `start` | 分发工具及缺失的release，全部准备完成后先启动网关归属实例，再其他P，最后D；执行真实生成检查 |
| `startp/startd` | 只准备并启动对应角色，不等待另一角色就绪 |
| `stop/stopp/stopd` | 按启动逆序停止全部/P/D实例，不操作其他任务 |
| `status` | 查看全部实例Supervisor状态 |
| `check/smoke` | 通过网关执行完整/快速真实生成检查 |
| `evidence` | 收集每实例KV传输和请求完成事件 |
| `shell/code` | 分发工具/工具及wheel发布包，不替换源码或覆盖已有release |
| `log/log_plog` | 按主机收集服务及验收日志/Ascend日志 |

默认网关归属第一个P，合并模式归属第一个regular，可用 `GATEWAY_INSTANCE` 指定。只启动一个SMG，统一注册全部实例。停止网关归属实例时，其网关也会停止，同机其他实例的Supervisor不受影响。

默认HTTP8010、引擎8120、引擎metrics8121、网关metrics8122、rendezvous9600、P bootstrap8998。同机第二实例的本地端口偏移 `POOL_PORT_STRIDE=100`；握手端口从9010按布局全局顺序分配，六实例即9010–9015，全部绑定在网关主机。启动前检查设备和TCP端口，包含非LISTEN连接及rendezvous派生端口。Mooncake/ZMQ还需节点间数据面连接互通，使用可信内网。

部署器保存布局指纹。复用已安装实例时核对布局、模型、设备和角色参数，避免混用新旧配置。不同目录不自动避让设备和端口。

日志收集目录为 `${LOCAL_LOG_ROOT:-$PWD/logs/deepseek-v41}/$RUN_TAG/`。每主机生成一个压缩包，包含各实例的logs、validation和node.json；Ascend日志保存在实例的ascend-log目录。

## 源码开发

设置 `CODE_MODE=source`，提供TS_SRC、UNI_SRC、PLUGIN_SRC。部署venv中的命令入口仍来自wheel，三个源码树放到PYTHONPATH前部，并启用allow-reload-forward与网关开发控制API。

在网关归属节点激活该实例的环境后执行：

```bash
source /path/to/run_tag/p0/runtime/env.sh
# 修改受支持的模型/forward模块后执行。
GATEWAY_URL="$(python -c 'import json,os; d=json.load(open(os.environ["TS_RUNTIME_DIR"]+"/node.json")); print("http://"+d["gateway_host"]+":"+str(d["http_port"]))')"
ts reload-forward --url "$GATEWAY_URL"
```

支持范围见[forward热重载](../guides/forward-reload.md)。检查重载返回的模块报告；native库及不支持热重载的插件修改应重新构建或重启。PD各节点源码应一致，迭代结束后在新启动的实例上验收。source模式仅用于开发网络。

## 910B布局约束

910B节点按两个HCCS组划分设备：0–7和8–15，组间经过PIX/PHB/SYS路径。每个TP8实例使用一个完整HCCS组。

- 主模型40层路由专家权重及scale约268.945GiB；EP4下每设备仅此项约67.236GiB，超过64GiB容量。
- EP8下每设备约33.618GiB，还需预留其他权重、KV缓存和临时张量空间。
- TP16不满足模型o_groups=8的head TP整除要求。
- 模型要求PP1；TP8＋DP2＋EP16不满足attention TP等于MoE TP×EP的约束。

部署配方使用TP8/EP8/PP1，三台机器形成六个独立实例。2P4D用于测试开发，P/D比例按输入长度、输出长度和并发负载调整。该配置不启用attention DP。

每台主机需容纳两个实例的Engram主机表及加载临时内存。完整Engram表约188.83GiB，双实例逻辑表约377.67GiB；还需按实际RSS、共享映射和NUMA分配检查主机内存。默认上下文512、每实例并发1、显存比例0.85；扩大参数后重新验收。

## 切换与回退

不同任务可能使用同一设备和端口。先准备新发布包与环境，再显式停止旧布局，确认资源释放后启动新布局。脚本不会自动停止其他任务；失败时保留日志用于诊断。

回退时先停止新布局，确认资源释放，再用旧配置启动保留的旧环境。不要删除基础venv、完整模型或旧配置，也不要把候选wheel直接安装回基础venv。控制脚本更新和 `shell` 分发本身不会重启模型服务。

## 验收与排查

修改模型、环境、布局、上下文长度或并发参数后，执行以下检查：

```bash
bash run_deepseek_v41_flash.sh status
bash run_deepseek_v41_flash.sh smoke
bash run_deepseek_v41_flash.sh check
bash run_deepseek_v41_flash.sh evidence
```

| 检查项 | 通过条件 |
|---|---|
| 服务进程 | 每实例一个引擎、整个布局一个网关，Supervisor状态为RUNNING |
| 网关worker | `/workers`中的角色和数量与布局一致，全部ready且healthy |
| 快速生成 | `smoke`返回HTTP200、预期文本及stop结束原因 |
| 顺序与并发生成 | `check`的6次顺序请求和2次并发请求全部通过 |
| P/D覆盖 | 每个D均处理请求，相关P/D日志使用同一请求ID关联 |
| KV传输 | 各P的8个rank有发送事件，各D的8个rank有 `ts_decode_kv_transfer_success`，传输后有 `ts_request_finished`；传输协议为ascend_direct |
| 设备与内存 | `npu-smi info`显示设备健康，无非预期进程；HBM和主机内存有可用余量 |

检查流式接口：

```bash
curl --noproxy '*' -N -sS http://10.255.76.138:8010/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"/workdir/tokenspeed-dsv41/models/DeepSeek-V4.1-Flash","messages":[{"role":"user","content":"只回答一个数字：3+4=多少？"}],"temperature":0,"max_tokens":4,"stream":true,"chat_template_kwargs":{"enable_thinking":false}}'
```

预期内容为 `7`，`finish_reason` 为 `stop`，事件流以 `[DONE]` 结束。其他环境需替换网关地址和模型路径。

原始生成请求、响应和传输事件保存在 `$PD_ROOT/<实例ID>/validation/`；引擎和网关日志位于对应实例的 `logs/`。通过 `log` 收集各节点服务日志，通过 `log_plog` 收集Ascend日志。功能验收完成后，根据目标负载单独进行性能和稳定性测试。

在Tokenspeed仓库根目录运行无需NPU的启动器回归测试：

```bash
source .venv/bin/activate
python -m pytest -q test/cli/test_deepseek_v41_deployment.py
```
