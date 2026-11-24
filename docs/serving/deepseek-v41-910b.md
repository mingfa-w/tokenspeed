# DeepSeek V4.1 Flash 在 Ascend 910B 上的开发部署

[部署脚本](../../tools/deploy/deepseek_v41/deploy_pd.sh)支持单机 P/D 合并部署（`SERVING_MODE=merged`）和三节点 1P2D 部署（`SERVING_MODE=pd`），两者均使用标准 headless 引擎与 SMG 网关。

本方案适用于 TP8/EP8、eager、纯文本测试和开发。脚本依赖已准备好的基础运行环境与模型，不负责安装裸机系统。PD 是否提升吞吐，需要使用实际负载单独验证。

## 环境准备

每个节点需要 Linux x86_64、Python 3.11、八个空闲的 64 GiB 910B 设备、完整本地模型（包括 Engram 表和 tokenizer），以及已验证的 Ascend 基础运行环境。用 `BASE_ROOT` 指定该环境，其目录必须包含：

- `venv`：安装相互兼容的 Torch/TorchNPU、Tokenspeed engine/kernel/scheduler、SMG、Supervisor 和 Ascend Mooncake。
- `runtime/env.sh`：激活基础 venv，并设置 CANN、驱动和 SHMEM 动态库路径。
- `runtime/guard/sitecustomize.py`：拦截旧算子库导入。
- `env.sh` 引用的 SHMEM 库，以及匹配的 CANN 安装。

参考软件组合为 CANN 9.0、Torch 2.9.0、TorchNPU 2.9.0、Tokenspeed 0.1.1、engine 0.1.0、scheduler 0.1.57、kernel-npu 0.1.0、SMG 1.16.0 和 Mooncake NPU 0.3.13。构建环境的**完整版本号**保存在 `build-info.json` 中，安装时会逐项检查。普通 CUDA Mooncake wheel 不能替代 NPU 版本；native wheel 还必须匹配 Python、机器架构、Torch ABI 和 CANN。

安装过程会创建新的 venv，通过 `.pth` 文件读取基础 venv 的依赖，新环境中安装的 wheel 优先。必须保留基础环境，不要删除、移动或原地升级它。这种方式避免直接复制 venv 后残留旧 shebang；若需完全独立的运行包，应另行导出全部依赖并验证。

## 构建与发布 wheel

在软件栈匹配的 Ascend 构建机上执行，依赖安装必须在 venv 中进行。只有 Python 改动时，可以复用已编译的 dav-2201 算子；需要重新编译 native 算子时，选择 `NATIVE_MODE=rebuild`，构建在独立暂存目录中进行。

```bash
source /path/to/validated-runtime/runtime/env.sh
export BASE_ROOT=/path/to/validated-runtime
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
