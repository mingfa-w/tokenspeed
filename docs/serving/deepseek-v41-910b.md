# DeepSeek V4.1 Flash：910B 部署、开发与验证

统一入口为 `tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh`。支持按实例数量生成P/D布局，或使用显式布局配置同机混合P/D。每个实例保持TP8/EP8/PP1，当前三台机器默认2P4D、共48个设备。

## 代码、实例与镜像

| 组件 | 代码链接 | 分支 | 已验证运行基线 |
|---|---|---|---|
| Tokenspeed | [仓库](https://github.com/mingfa-w/tokenspeed) | `uni-ops/dpk-flash-v41` | `913449a39227d95c43525235a5643c5d0a566215` |
| uni-ops | [仓库](https://dev.sankuai.com/code/repo-detail/mptech-llmp/uni-ops/file/list) | `deepseek-v4.1-flash` | `25e26b43242df67dacf5821d5b73090ba90f8978` |
| ts-plugin | [仓库](https://dev.sankuai.com/code/repo-detail/mptech-llmp/ts-plugin/file/list) | `deepseek-v4.1-flash` | `b22ddb94b7cb35132fd0301303c086ab87912472` |

Tokenspeed运行代码基线与部署工具版本分开记录：工具版本使用当前检出分支的 `git rev-parse HEAD`，wheel来源使用发布包中的 `build-info.json`。ts-plugin发行包名为 `tokenspeed-longcat`，uni-ops发行包名为 `mt-uni-ops`。

获取源码时使用相应分支：

```bash
export CODE_ROOT=/home/mingfa/code
mkdir -p "$CODE_ROOT"
git clone --branch uni-ops/dpk-flash-v41 --single-branch https://github.com/mingfa-w/tokenspeed.git "$CODE_ROOT/tokenspeed"
git clone --branch deepseek-v4.1-flash --single-branch ssh://git@git.sankuai.com/mptech-llmp/uni-ops.git "$CODE_ROOT/uni-ops"
git clone --branch deepseek-v4.1-flash --single-branch ssh://git@git.sankuai.com/mptech-llmp/ts-plugin.git "$CODE_ROOT/ts-plugin"
```

当前参考实例：[MLP psxbwxrtsp7nea](https://mlp.sankuai.com/ml/#/run/detail?jobId=psxbwxrtsp7nea&tab=implement)。使用镜像：

```text
registry-offlinebiz.sankuai.com/custom_prod/com.sankuai.data.hadoop.gpu/efficient-llm/serving_tokenspeed_npu_3b_custom_20261103_ts_f8cb94a1b_ops_f600e37fd_fix01_3d7e4906:1.0.2
```

当前目标节点为10.255.76.138、10.255.111.50、10.255.170.42，SSH端口已验证为8022。新实例仍需检查镜像中的软件版本，不应仅凭镜像名称判断兼容性。

## 一键部署

先按后文准备基础环境、完整模型、wheel发布包和SSH认证。在控制机修改入口顶部的目录、IP和参数；在Tokenspeed仓库根目录执行：

```bash
bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh plan
bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh preflight
bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh start
bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh status
bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh log
bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh stop
```

交付包根目录也提供同名入口，执行时去掉 `tools/deploy/deepseek_v41/` 前缀。配置区的主要参数：

| 参数 | 含义 |
|---|---|
| `PREFILL_INSTANCES` / `DECODE_INSTANCES` | P/D实例数，各实例8设备；默认2/4，即16卡P＋32卡D |
| `DEVICE_SLOTS` | 按顺序列出“IP\|设备组”；前PREFILL_INSTANCES组做P，其余做D |
| `RUN_TAG` / `TEST_ROOT` | 本次任务标记和远端工作根目录 |
| `MODEL_PATH` / `BASE_ROOT` / `RELEASE_ROOT` | 模型、基础环境、wheel发布包路径 |
| `CODE_MODE` | `wheel`导入安装包；`source`加载三个本地源码树并启用开发热重载 |
| `PREFILL_MEM_FRACTION` / `DECODE_MEM_FRACTION` | P/D显存比例，默认均0.85 |
| `PREFILL_MAX_NUM_SEQS` / `DECODE_MAX_NUM_SEQS` | 每实例并发，默认均1 |
| `MAX_MODEL_LEN` / `MAX_TOTAL_TOKENS` / `CHUNKED_PREFILL_SIZE` | 默认512/1024/512，增大后需重新验收 |
| `SSH_USER` / `SSH_PORT` | 默认root/8022 |

例如改为1P5D并使用新任务目录：

```bash
PREFILL_INSTANCES=1 DECODE_INSTANCES=5 RUN_TAG=flash_1p5d \
  bash tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh plan
# 确认规划后，将plan改为start。
```

### root密码与任一节点启动

参考FluentLLM的 `sshpass -f abc.txt`，入口默认 `SSH_AUTH_MODE=password`、`SSH_USER=root`、`SSH_PORT=8022`。优先使用入口同目录的 `abc.txt`，未找到时兼容 `/home/mingfa/code/fluentllm/npu_test/flash26b/abc.txt`；可用 `SSH_PASSWORD_FILE` 指定其他绝对路径。文件第一行填写实际root密码：

```bash
cd /home/mingfa/code/tokenspeed/tools/deploy/deepseek_v41
# 已有密码文件可直接复制，无需在终端命令中输入密码。
install -m 600 /home/mingfa/code/fluentllm/npu_test/flash26b/abc.txt ./abc.txt
command -v sshpass  # 缺少时，由管理员通过系统包管理器安装sshpass
bash run_deepseek_v41_flash.sh preflight
bash run_deepseek_v41_flash.sh shell
```

实际 `abc.txt` 通过Git忽略；仓库提供 `abc.txt.example` 格式示例。`shell`、`code` 和 `start` 将入口、部署依赖、说明文档和权限为600的密码文件分发至每个配置节点的 `$TEST_ROOT/tools_$RUN_TAG`。密码经SSH标准输入写入，不进入命令参数、Git或日志。所有节点须已有 `bash`、`python3`、`sshpass`、基础环境和完整模型；源码模式还要求三个源码目录存在。wheel发布包在 `start` 时校验，缺失时从执行节点复制，因此执行节点须持有发布包，或所有目标节点已具备同一发布包。

完成分发后，在任一配置节点执行以下命令，都由该节点通过root SSH协调完整布局（不局限于启动本机实例）：

```bash
cd /workdir/deepseek-v41-test/tools_dsv41_flash_dev
bash run_deepseek_v41_flash.sh preflight
bash run_deepseek_v41_flash.sh start
bash run_deepseek_v41_flash.sh status
bash run_deepseek_v41_flash.sh check
```

路径随 `TEST_ROOT` / `RUN_TAG` 改变；使用非默认配置时，在各协调节点设置相同环境变量，或将共同配置写进入口顶部后重新执行 `shell` 分发。首次连接采用 `StrictHostKeyChecking=accept-new`，后续主机密钥变更会拒绝连接。需要已预置known_hosts时设 `SSH_HOST_KEY_CHECKING=yes`；参考脚本式关闭校验可显式设 `SSH_HOST_KEY_CHECKING=no`。密钥认证设置 `SSH_AUTH_MODE=key`，不会使用或分发密码文件。

`preflight` 仅检查连接和路径；`start` 会先检查全部节点，再分发、准备和依次启动实例，最后执行真实生成验收。检查通过不等于完整模型服务已验收。

## 显式配置任意P/D放置

需要逐实例决定位置时，直接使用 `deploy_pd.sh`，不要同时使用上述自动生成布局的入口。每行格式为 `实例ID 角色 IP 设备组`：

```bash
export SERVING_MODE=pd CODE_MODE=wheel
unset DS_IPLIST DEVICE_POOLS PD_LAYOUT_FILE
export PD_LAYOUT='
p0 prefill 10.255.76.138 0,1,2,3,4,5,6,7
d0 decode  10.255.76.138 8,9,10,11,12,13,14,15
p1 prefill 10.255.111.50 0,1,2,3,4,5,6,7
d1 decode  10.255.111.50 8,9,10,11,12,13,14,15
p2 prefill 10.255.170.42 0,1,2,3,4,5,6,7
d2 decode  10.255.170.42 8,9,10,11,12,13,14,15
'
export RUN_TAG=flash_3p3d
# 还需设置BASE_ROOT、MODEL_PATH、RELEASE_ROOT、SSH等环境变量。
bash tools/deploy/deepseek_v41/deploy_pd.sh plan
bash tools/deploy/deepseek_v41/deploy_pd.sh start
```

这份配置为每台1P＋1D，共3P3D。P卡数=`prefill行数×8`，D卡数=`decode行数×8`。同样可配置1P5D、2P4D，或更多主机。合并模式使用 `SERVING_MODE=merged`，各行角色改为 `regular`。

也可以把行内容保存到本地文件，用 `PD_LAYOUT_FILE=/path/to/my.layout` 读取，并先取消 `PD_LAYOUT`。文件还支持JSON实例列表，或 `{"instances":[...]}`；每个实例包含 `id/role/node/devices`，devices为逗号分隔字符串或数字列表。文件作为数据解析，不执行shell命令或变量展开。

显式布局默认 `REQUIRE_FULL_NODE=1`，要求每台被引用机器的0–15设备完整分配且无重叠。明确进行部分设备测试时，可设置为0。每实例必须使用0–7或8–15完整HCCS组。实例ID唯一，修改布局后使用新的RUN_TAG/PD_ROOT。

## 工具组成与环境文件

只保留一个配置入口和四个执行依赖，分发时不能遗漏：

| 文件 | 用途 |
|---|---|
| `run_deepseek_v41_flash.sh` | 当前机器配置、实例数量与一键入口 |
| `deploy_pd.sh` | 批量分发、按角色启停、状态及日志收集 |
| `deploy_runtime.sh` | 单实例安装、wheel构建发布、Supervisor与真实生成检查 |
| `configure.py` | 生成各实例的启动命令、环境和进程配置 |
| `pd_layout.py` | 布局校验、卡数统计、端口与网关分配 |

源码没有预置的 `runtime/env.sh`：

- `$BASE_ROOT/runtime/env.sh`：按下文环境准备步骤3创建，负责基础venv及CANN/SHMEM。
- `$PD_ROOT/<实例ID>/runtime/env.sh`：由部署器安装该实例时生成，负责设备、模型和源码路径。

默认任务目录为 `$TEST_ROOT/run_$RUN_TAG/<实例ID>/`，根目录可用 `PD_ROOT` 覆盖。先生成文件，再执行source，不要在Tokenspeed源码的runtime目录寻找它们。

## 环境准备

每个节点需要Linux x86_64、Python 3.11、完整本地模型（包括Engram表和tokenizer）、CANN/驱动及匹配的NPU软件栈。当前配方为eager、纯文本、numerics=auto、greedy。以下步骤创建独立的基础venv，部署实例再以只读方式继承其依赖。

### 1. 明确基础镜像与安装材料

已验证环境使用 Python 3.11 创建 `--system-site-packages` venv：Torch、TorchNPU 和 Mooncake NPU 由基础镜像提供，Tokenspeed 等包安装到新 venv。不能把“镜像已安装的依赖”误当成 venv 自动下载的依赖。

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

不要只执行不带版本的 `pip install tokenspeed torch_npu mooncake`。同名包的最新版本不一定匹配本方案；尤其不能将 NPU Mooncake 替换为 CUDA 版本。

若从已验收环境导出安装材料，在该环境上执行以下命令。`REFERENCE_ROOT` 是可访问的已验收部署目录（包含 venv 和 wheel），不是 Tokenspeed 源码目录。此段仅用于从参考环境导出材料；若已取得发布方提供的 `runtime/` 材料，复制到新 `$BASE_ROOT/runtime/` 后从步骤 2 开始。`BASE_ROOT` 是新建目录，不要指向正在运行的环境：

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

这里复制的是 wheel、SHMEM 和环境记录，不是原 venv。`reference-packages.txt` 是版本核对清单，不能直接当作“所有包都能从公网下载”的安装保证。基础镜像中已有的包仍需要相同镜像提供；若 wheel 缺失，应从对应构建发布方补齐。旧安装元数据中的 `file://...` 只是历史安装路径，不是可用下载地址。

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

`--no-index --no-deps` 使用已准备好的版本集合，不让解析器替换基础 Torch/NPU 包。它也意味着安装器不会替你补齐遗漏依赖，后面的版本、导入和依赖检查必须执行。

Triton-Ascend 会覆盖上游 Triton 的部分文件，因此先安装其余 wheel，最后安装 `triton-ascend`；之后不要单独重装上游 Triton。

**如果使用未预装 Torch/NPU/Mooncake 的普通 Python：**需要事先取得匹配的 Torch CPU、TorchNPU、Mooncake NPU wheel 和完整传递依赖。创建不带 `--system-site-packages` 的独立 venv，然后把这些 wheel 也纳入上面的离线安装集合。核心包的安装形式如下，路径需替换成实际文件：

```bash
# 这是普通 Python 的替代路径；不要在上面已经建好的基础 venv 上重复执行。
test ! -e "$BASE_ROOT/venv"
"$SYSTEM_PYTHON" -m venv "$BASE_ROOT/venv"
source "$BASE_ROOT/venv/bin/activate"
python -m pip install --no-index --no-deps \
  "/path/to/wheels/torch-<完整版本>-cp311-cp311-<平台>.whl" \
  "/path/to/wheels/torch_npu-<完整版本>-cp311-cp311-linux_x86_64.whl" \
  "/path/to/wheels/mooncake_transfer_engine_npu-0.3.13-cp311-cp311-linux_x86_64.whl"
```

这条路径还需安装前述其余离线包和传递依赖，完成独立环境验收。已有运行环境不一定保留这些原始 wheel；缺少材料时，不能仅复制 `site-packages` 就声称完成了独立环境打包。当前已执行的部署验证采用的是前一种基础镜像继承方式。

### 3. 生成环境脚本与旧算子导入拦截

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

`pip check` 的非零结果必须核对，不能直接忽略。已验收镜像中存在需要记录的元数据例外：Tokenspeed 声明的 `tokenspeed-mooncake` 依赖与实际使用的 NPU 替代发行包不同；Triton-Ascend 的 pytest 固定版本与测试环境可能不同；镜像中的 TorchAir 还可能与 protobuf 版本冲突。保留参考环境和新环境的检查输出，逐项确认。不能为清除第一项提示而安装 CUDA Mooncake；TorchAir 相关图编译路径也不在本 eager 方案的验收范围内。新增冲突或导入失败应先解决，再启动模型。

最后在空闲设备上执行部署后的 `smoke`，确认模型加载和真实生成；仅导入通过不能证明服务通过验收。基础环境准备完成后，继续下文的wheel构建与部署。

本章的基础镜像继承流程已在新目录实际执行，版本、ABI、导入和命令入口检查通过；新环境的 `pip check` 输出与参考环境一致，保留了上述三类例外。普通 Python 的独立环境路径未在本次验证中执行。本次环境搭建检查未启动模型或修改在线服务。

## 构建与发布wheel

在匹配的构建机上执行，依赖安装必须在venv中。源代码树须无未提交的已跟踪修改：

```bash
source "$BASE_ROOT/runtime/env.sh"
export TS_SRC=/home/mingfa/code/tokenspeed
export UNI_SRC=/home/mingfa/code/uni-ops
export PLUGIN_SRC=/home/mingfa/code/ts-plugin
export RELEASE_ID=deepseek-v41-dev
export BUILD_ROOT=/workdir/build-$RELEASE_ID
export RELEASE_ROOT=/workdir/releases/$RELEASE_ID
NATIVE_MODE=reuse bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" build
bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" publish
bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" check-release
```

`reuse`打包源码树现有dav-2201 native产物并记录哈希；需要重编译则选 `NATIVE_MODE=rebuild`，在隔离暂存目录运行AscendC构建。构建需要build、wheel、setuptools>=77；重编译还需CANN SDK、CMake和编译器，脚本不自动升级它们。

发布目录含两个新依赖wheel、已验证Tokenspeed Python wheel、五个部署工具、本文、版本及SHA256清单。依赖wheel使用 `.post0+g<commit>` 版本，不改原检出目录。Tokenspeed Python wheel与源码逐文件比对后复用；engine/kernel/scheduler由基础环境提供。先安装其他包，最后安装Triton-Ascend，不能事后单独重装上游Triton。

publish采用暂存目录后重命名，拒绝覆盖已有release。整个发布目录需在控制机可读；start会校验远端已有目录，缺失时从控制机分发。它不会复制模型、基础环境或源码。source模式的三个源码路径需在各节点准备好并保持一致。

若需上传包仓库，首次publish时显式提供 `TWINE_REPOSITORY_URL`，凭据由环境或凭据管理器提供；仓库需支持PEP440本地版本。该动作只上传uni-ops和ts-plugin。上传失败保留文件制品，可用 `python -m twine upload` 重试，不覆盖release。文件发布本身不等于上传公共PyPI。

## 启停、端口与验收

通用批量入口的动作如下，自动配置入口也会透传这些动作：

| 命令 | 行为 |
|---|---|
| `plan` | 离线校验并显示各角色卡数、设备、端口和网关 |
| `start` | 分发工具及缺失的release，全部准备完成后先启动网关归属实例，再其他P，最后D；执行真实生成检查 |
| `startp/startd` | 只准备并启动对应角色，不等待另一角色就绪 |
| `stop/stopp/stopd` | 按启动逆序停止全部/P/D实例，不操作其他任务 |
| `status` | 查看全部实例Supervisor状态 |
| `check/smoke-remote` | 通过网关执行完整/快速真实生成检查 |
| `evidence-remote` | 收集每实例KV传输和请求完成事件 |
| `shell/code` | 分发工具/工具及wheel发布包，不替换源码或覆盖已有release |
| `log/log_plog` | 按主机收集服务及验收日志/Ascend日志 |

默认网关归属第一个P，合并模式归属第一个regular，可用 `GATEWAY_INSTANCE` 指定。只启动一个SMG，统一注册全部实例。停止网关归属实例时，其网关也会停止，同机其他实例的Supervisor不受影响。

默认HTTP8010、引擎8120、引擎metrics8121、网关metrics8122、rendezvous9600、P bootstrap8998。同机第二实例的本地端口偏移 `POOL_PORT_STRIDE=100`；握手端口从9010按布局全局顺序分配，六实例即9010–9015，全部绑定在网关主机。启动前检查设备和TCP端口，包含非LISTEN连接及rendezvous派生端口。Mooncake/ZMQ还需节点间数据面连接互通，使用可信内网。

部署器保存布局指纹。复用已安装实例时核对布局、模型、设备和角色参数，避免混用新旧配置。不同目录不自动避让设备和端口。

smoke要求实际角色数量的worker均ready/healthy，并检查一次真实问答；check执行六条顺序和两条并发请求，检查HTTP200、预期文本与stop结束原因。原始请求/响应保存在各实例validation目录，不能仅凭RUNNING或/workers判定通过。

PD还需按本次请求ID/时间对齐所有P/D日志，确认全部Decode实际接收请求，逐rank记录 `ts_decode_kv_transfer_success`、`ts_request_finished`，并确认 `ascend_direct`。固定八条问答不保证覆盖任意规模的全部worker，应补充请求直至覆盖。日志收集器不把历史成功记录当作当前验收。

本地日志位于 `${LOCAL_LOG_ROOT:-$PWD/logs/deepseek-v41}/$RUN_TAG/`，每主机一个包，包含各实例日志、validation和node.json；Ascend日志在实例ascend-log目录。

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

## 910B布局依据

主要参考 `/home/mingfa/code/fluentllm/npu_test/flash26b/run_26b_pp.sh`。已读取其配置：hidden6144、路由专家512、专家中间维度2048；DeepSeek分别为5120、384、2304，二者均64个attention heads。其有权重路由专家参数量估算约541.17B，与DeepSeek约543.58B接近；参考配置的identity专家不计入权重。不能把该W8A8 checkpoint的PP/显存参数直接套用于DeepSeek的MXFP4路径。

三台目标机器均实测为两个HCCS八设备组：0–7与8–15。组间为PIX/PHB/SYS路径，不是16设备统一HCCS。

- DeepSeek主模型40层路由专家权重及scale共268.945GiB；EP4每设备仅此项就67.236GiB，超过64GiB。
- EP8每设备约33.618GiB，已有TP8运行验证；还需保留其他权重、KV和临时张量空间。
- TP16不满足当前o_groups=8的head TP整除限制；TP8＋DP2＋EP16不满足当前attention TP等于MoE TP×EP的约束。
- TP8＋DP2＋EP8的离线mapping可行，但没有消除权重复制，尚需验证NPU多DP路径。当前V4.1模型也明确要求PP1。

因此测试开发与稳定性优先时，以六个独立TP8/EP8/PP1实例作为48设备基线，2P4D是初始比例，不是吞吐最优的实测结论。P/D比例由负载决定，配置支持调整。所有设备被分配也不等于低并发时计算核心持续满载。

三台主机均约2TiB内存；Engram表约188.83GiB/完整副本，双实例逻辑表约377.67GiB，仍需检查实测RSS、共享映射和NUMA。当前保持512上下文和每引擎并发1；扩大参数、启用其他数值模式前另行验证。算术FP4解码在已测MoE场景增加约450MiB临时张量峰值，不应把显存比例硬设为1.0。

## 切换、回退与检查

现有旧PD可能占用相同卡和端口。先准备新环境，在维护窗口显式停止旧实例，确认资源释放后运行新任务；脚本不会自动停止旧服务。新任务失败时保留日志用于诊断。

回退时先停止新任务，确认资源释放，再启动保留的旧环境。不要删除基础venv、模型或旧配置，也不要通过将候选wheel安装回基础venv来回退。

无NPU可运行启动器回归：

```bash
source .venv/bin/activate
python -m pytest -q test/cli/test_deepseek_v41_deployment.py
```

2026-11-24已完成三节点48设备的2P4D实机端到端功能验收：先关闭三台机器旧Supervisor服务，再从10.255.76.138的 `/workdir/deepseek-v41-test/tools_dsv41_flash_dev` 执行 `bash run_deepseek_v41_flash.sh start`，退出码为0。该机P0使用0–7、P1使用8–15；10.255.111.50运行D0/D1，10.255.170.42运行D2/D3，两台D机器各按0–7、8–15分组。

六个worker全部为ready且healthy，六个引擎和唯一网关均为RUNNING。启动自动smoke通过，随后 `check` 的6次顺序请求及2次并发请求全部通过，覆盖数字问答、城市问答和多行输出。含smoke在内，P0/P1分别完成5/4次请求，D0/D1/D2/D3分别完成2/2/2/3次请求；每个P的8个rank均记录到KV发送事件，每个D的8个rank均记录到KV接收成功事件。三台机器的16个设备均由npu-smi检查为健康；这是功能验收，不是吞吐最优或长期稳定性结论。

启动日志位于协调节点 `/workdir/deepseek-v41-test/start-2p4d.log`，完整检查日志为同目录 `check-2p4d.log`，逐次生成结果和传输事件保存在各实例的 `validation` 子目录。当前服务保持运行。扩大上下文、并发、调整P/D比例或修改环境后需重新验收。
