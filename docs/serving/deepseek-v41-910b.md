# DeepSeek V4.1 Flash 在 Ascend 910B 上的开发部署

[部署脚本](../../tools/deploy/deepseek_v41/deploy_pd.sh)支持单机 P/D 合并部署（`SERVING_MODE=merged`）和三节点 1P2D 部署（`SERVING_MODE=pd`），两者均使用标准 headless 引擎与 SMG 网关。

本方案适用于 TP8/EP8、eager、纯文本测试和开发。脚本依赖已准备好的基础运行环境与模型，不负责安装裸机系统。PD 是否提升吞吐，需要使用实际负载单独验证。

## 先区分源码目录与部署目录

Tokenspeed 源码仓库中没有预置的 `runtime/env.sh`。本文有两个**部署时生成**的环境文件：

| 文件 | 何时生成 | 用途 |
|---|---|---|
| `$BASE_ROOT/runtime/env.sh` | 按“环境准备”步骤 3 的 `cat > ...` 命令创建 | 激活基础 venv，配置 CANN、驱动和 SHMEM |
| `$PD_ROOT/runtime/env.sh` | 执行 `deploy_pd.sh install` 或首次 `up` 时自动生成 | 在基础环境上激活当前部署 venv，设置设备、模型和源码路径 |

`TS_SRC` 是 Tokenspeed 源码目录；`BASE_ROOT` 和 `PD_ROOT` 是源码目录之外的部署目录。不要在 `$TS_SRC/runtime/` 中寻找这些文件。首次搭建从下文“环境准备”开始，完成步骤 3 后才能 `source "$BASE_ROOT/runtime/env.sh"`；执行部署安装后才能 `source "$PD_ROOT/runtime/env.sh"`。

## 环境准备

每个节点需要 Linux x86_64、Python 3.11、八个空闲的 64 GiB 910B 设备、完整本地模型（包括 Engram 表和 tokenizer），以及已验证的 Ascend 基础运行环境。用 `BASE_ROOT` 指定该环境，其目录必须包含：

- `venv`：按下文步骤创建，并安装或继承经过版本核对的 Torch/TorchNPU、Tokenspeed engine/kernel/scheduler、SMG、Supervisor 和 Ascend Mooncake。
- `runtime/env.sh`：在下文步骤 3 中创建，用于激活基础 venv，并设置 CANN、驱动和 SHMEM 动态库路径。
- `runtime/guard/sitecustomize.py`：拦截旧算子库导入。
- `env.sh` 引用的 SHMEM 库，以及匹配的 CANN 安装。

参考软件组合为 CANN 9.0、Torch 2.9.0、TorchNPU 2.9.0、Tokenspeed 0.1.1、engine 0.1.0、scheduler 0.1.57、kernel-npu 0.1.0、SMG 1.16.0 和 Mooncake NPU 0.3.13。构建环境的**完整版本号**保存在 `build-info.json` 中，安装时会逐项检查。普通 CUDA Mooncake wheel 不能替代 NPU 版本；native wheel 还必须匹配 Python、机器架构、Torch ABI 和 CANN。

安装过程会创建新的 venv，通过 `.pth` 文件读取基础 venv 的依赖，新环境中安装的 wheel 优先。必须保留基础环境，不要删除、移动或原地升级它。这种方式避免直接复制 venv 后残留旧 shebang；若需完全独立的运行包，应另行导出全部依赖并验证。

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

最后在空闲设备上执行部署后的 `smoke`，确认模型加载和真实生成；仅导入通过不能证明服务通过验收。基础环境准备完成后，继续下文的 wheel 构建、发布和部署步骤。

本章的基础镜像继承流程已在新目录实际执行，版本、ABI、导入和命令入口检查通过；新环境的 `pip check` 输出与参考环境一致，保留了上述三类例外。普通 Python 的独立环境路径未在本次验证中执行。本次环境搭建检查未启动模型或修改在线服务。

## 构建与发布 wheel

在软件栈匹配的 Ascend 构建机上执行，依赖安装必须在 venv 中进行。只有 Python 改动时，可以复用已编译的 dav-2201 算子；需要重新编译 native 算子时，选择 `NATIVE_MODE=rebuild`，构建在独立暂存目录中进行。

```bash
: "${BASE_ROOT:?请先完成上文环境准备并设置 BASE_ROOT}"
test -f "$BASE_ROOT/runtime/env.sh"
source "$BASE_ROOT/runtime/env.sh"
export TS_SRC=/path/to/tokenspeed
export UNI_SRC=/path/to/uni-ops
export PLUGIN_SRC=/path/to/ts-plugin
export RELEASE_ID=deepseek-v41-dev
export BUILD_ROOT=/workdir/build-$RELEASE_ID
export RELEASE_ROOT=/workdir/releases/$RELEASE_ID
NATIVE_MODE=reuse bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" build
bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" publish
```

源码树中不能有未提交的已跟踪文件修改。构建需要 `build`、`wheel` 和 setuptools >=77；重新编译 native 算子还需要 CANN SDK、CMake 和编译器。脚本不会自动下载或升级这些依赖。

发布包包含 `mt-uni-ops`、`tokenspeed-longcat`（ts-plugin 的发行包名）、已验证的 Tokenspeed Python wheel，以及源码提交号、native 文件哈希、SHA256 清单、部署脚本和回归测试。两个依赖 wheel 的版本后缀为 `.post0+g<commit>`，不会修改原源码树的版本文件。复用缓存的 Tokenspeed Python wheel 前，会将其中代码与源码树比较；其 native 依赖仍由基础环境提供。

`publish` 创建新的文件制品目录并生成校验清单，拒绝覆盖已有目录。将**整个发布目录**复制到各节点，然后在节点上校验：

```bash
RELEASE_ROOT=/path/to/release bash tools/deploy/deepseek_v41/deploy_pd.sh check-release
```

如需上传包仓库，可在首次 `publish` 时显式设置 `TWINE_REPOSITORY_URL`，仅上传两个依赖 wheel。凭据应由环境变量或凭据管理工具提供，包仓库必须支持 PEP440 本地版本号。上传失败后文件制品仍会保留，使用 `python -m twine upload` 重试，不要覆盖原发布目录。发布文件制品不会自动上传公共包仓库。

## 单机 P/D 合并模式：一键启动

在已准备好的测试节点上使用 root shell。将以下配置保存在 Git 仓库之外的本地文件中，按实际环境修改路径和本机地址：

```bash
export BASE_ROOT=/path/to/validated-runtime
export RELEASE_ROOT=/path/to/release
export MODEL_PATH=/models/DeepSeek-V4.1-Flash
export PD_ROOT=/workdir/deepseek-v41-merged-dev
export NODE_IP=192.0.2.10  # 文档示例地址，必须替换成本机实际 IP
export SERVING_MODE=merged
export CODE_MODE=wheel
export DEVICES=0,1,2,3,4,5,6,7
```

加载上述环境变量后，在 Tokenspeed 仓库根目录执行：

```bash
bash tools/deploy/deepseek_v41/deploy_pd.sh up
```

`up` 在需要时安装新环境，启动引擎和网关，等待一个普通 worker 达到 ready/healthy，然后执行真实的 `1+1` 生成检查。仅有 Supervisor 的 RUNNING 状态不会被视为启动成功。也可以用 `install` 单独完成安装。就绪检查失败时会保留日志和进程，方便排查，不会停止其他部署。

默认 HTTP 端口为 8010。如果要与其他实例同时运行，必须在**安装之前**选择新的 `PD_ROOT`、空闲设备，以及未占用的 `HTTP_PORT`、`ENGINE_PORT`、`METRICS_PORT`、`GATEWAY_METRICS_PORT`、`HANDSHAKE_PORT` 和 `DIST_PORT`。

生成的配置会持久保存；修改配置时，应先停止旧环境，再安装到新目录。`up` 不会重写已有配置。启动检查会拒绝占用中的设备和本机 TCP 端口，也会检查非 LISTEN 连接。

## 源码开发与 forward 热重载

开发时，在 `up` 前显式设置 `CODE_MODE=source`，并将 `TS_SRC`、`PLUGIN_SRC` 和 `UNI_SRC` 指向本地源码树。该模式仍使用 wheel 提供安装元数据和命令入口，同时把三个源码目录加入 `PYTHONPATH` 前部；wheel 模式则直接导入已安装包。

源码模式会增加 `--allow-reload-forward` 和 SMG 的 `--enable-rl`，仅用于开发网络。

```bash
source "$PD_ROOT/runtime/env.sh"
# 修改模型或 forward 代码后，通过网关执行热重载。
ts reload-forward --url "http://$NODE_IP:${HTTP_PORT:-8010}"
bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" smoke
```

支持热重载的模块范围见 [forward 热重载说明](../guides/forward-reload.md)。不能假定所有插件或 native 库修改都支持热重载：应检查重载返回的模块报告，超出支持范围的修改需要重新构建或重启。

开发迭代结束后，必须在重新启动的实例上验证。PD 源码模式下，三个节点的源码应保持一致，并通过 P 节点网关执行重载。

## 三节点 1P2D 模式：启动流程

每个角色使用 TP8/EP8，共需 24 个设备。低负载开发可先使用合并模式；需要研究阶段间干扰和并发时再选择 PD。容量规划时，应与相同卡数的合并部署多副本比较。

三台节点使用相同的路径、设备编号和端口配置，相关路径必须在各节点本地存在。显式设置拓扑地址，以下均为文档示例地址，需替换为实际 IP：

```bash
export SERVING_MODE=pd CODE_MODE=wheel
export P_IP=192.0.2.10 D1_IP=192.0.2.11 D2_IP=192.0.2.12
export PD_ROOT=/workdir/deepseek-v41-pd-dev
export NODE_IP="$P_IP"  # 在对应 Decode 节点上改为 D1_IP 或 D2_IP
```

### 一键启动整个集群

首次部署时，可在具有三节点 SSH 密钥访问权限的控制机上执行：

```bash
export SSH_USER=root
# 三台节点必须在相同路径下准备好源码/脚本、发布包、基础环境和模型。
# 如有需要，通过 REMOTE_SCRIPT 指定远端脚本路径。
bash tools/deploy/deepseek_v41/deploy_pd.sh cluster-up
```

该命令依次安装三个节点，先启动 P，再启动 D1/D2，最后通过 P 执行真实生成检查。SSH 使用 BatchMode；遇到第一个错误即中止，并保留已完成的安装和日志用于诊断。

命令不会分发权重、源码或发布包，不会配置认证，也不会停止已有服务。必须使用新的 `PD_ROOT`；已安装的集群应使用下文的分节点启动命令。

### 分节点启动

先在 P 节点执行：

```bash
bash tools/deploy/deepseek_v41/deploy_pd.sh install
bash tools/deploy/deepseek_v41/deploy_pd.sh start
```

在 D1、D2 节点分别执行 `bash tools/deploy/deepseek_v41/deploy_pd.sh up`。

随后在 P 节点执行 `bash tools/deploy/deepseek_v41/deploy_pd.sh smoke` 或 `verify`。P 节点先单独启动，是为了避免尚未启动 D 节点就开始等待全部 worker，导致顺序部署阻塞。脚本不包含 SSH 密码，模型分发和远端认证需要提前准备。

P 节点绑定 `HANDSHAKE_PORT` 及其后两个端口，分别用于 P、D1、D2。**三个握手地址都属于 P 节点**，不是 Decode 节点地址。P 使用可路由的 `DIST_PORT` rendezvous 地址，以发布正确的 bootstrap 地址。每个 D 节点使用本机 loopback 的 `DIST_PORT`；针对当前小缓存配置，Decode 的主机快照空间设置为 0.125 GiB。

默认端口：HTTP 8010、引擎 8120、引擎指标 8121、网关指标 8122、握手 9010–9012、rendezvous 9600、P bootstrap 8998。Mooncake 和 ZMQ 还会建立数据面连接，节点间必须保证这些连接也能互通，不能只开放列出的固定端口；相关通道应放在可信网络中。

握手和传输语义见 [PD 分离服务说明](disaggregation.md)。

## 验证、停止与回退

在合并模式节点或 P 节点上执行生成检查；`status`、`evidence` 和 `stop` 按需在各节点执行：

```bash
bash tools/deploy/deepseek_v41/deploy_pd.sh status
bash tools/deploy/deepseek_v41/deploy_pd.sh smoke
bash tools/deploy/deepseek_v41/deploy_pd.sh verify
bash tools/deploy/deepseek_v41/deploy_pd.sh evidence
bash tools/deploy/deepseek_v41/deploy_pd.sh stop
```

`smoke` 检查一次真实生成。`verify` 要求合并模式有一个普通 worker，或 PD 模式有一个 Prefill 和两个 Decode，均为 ready/healthy；随后发送六条顺序请求和两条并发请求，检查 HTTP 200、预期文本和 `stop` 结束原因，并将原始请求、响应保存到 `validation/`。这些是功能检查，不代表持续吞吐测试。

PD 验收还需按本次请求的时间戳和 ID 对齐两个 Decode 的日志，检查八个 rank 的 `ts_decode_kv_transfer_success`、`ts_request_finished`，以及 `ascend_direct` 初始化记录。`evidence` 只收集日志事件，不能把历史成功记录当成本次验收通过。如果网关需要供远端访问，还应从外部客户端验证真实生成。

当前配置为上下文 512、每引擎并发 1、网关并发 2、`numerics=auto`、greedy 采样和 Engram 主机表。更长上下文、持续负载、其他数值模式或不同的 P/D 配比，需要另行验证。算术 FP4 解码在已测 MoE 场景中增加约 450 MiB 临时张量峰值，提高并发前应检查端到端显存余量。

`stop` 停止当前部署并关闭其 Supervisor，不会停止其他实例，也不会删除权重或发布包。回退时，先停止新部署，确认设备和端口已释放，再启动保留的旧环境。不要通过把候选 wheel 安装回基础 venv 的方式回退。

无需 NPU 即可运行启动器回归测试：

```bash
source .venv/bin/activate
python -m pytest -q test/cli/test_deepseek_v41_deployment.py
```
