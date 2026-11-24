# 布局与发布

完整环境搭建以目标Tokenspeed仓库的 `docs/serving/deepseek-v41-910b.md` 为准；本参考只补充部署决策与配置之间的约束。

## 配置与目录

`TS_SRC` 为源码根目录，`BASE_ROOT` 为兼容基础环境，`MODEL_PATH` 为完整模型，`RELEASE_ROOT` 为含manifest的wheel发布目录。

```text
源码：TS_SRC/tools/deploy/deepseek_v41/
基础环境：BASE_ROOT/runtime/env.sh、BASE_ROOT/venv/
节点工具：TEST_ROOT/tools_RUN_TAG/（可用REMOTE_TOOLS覆盖）
实例：PD_ROOT/<ID>/，PD_ROOT默认TEST_ROOT/run_RUN_TAG
```

实例的runtime包含env.sh、node.json、engine.sh、gateway.sh和supervisord.conf，另有venv、logs、validation及Ascend缓存/日志目录。不要混用源码、基础venv和实例venv。

`RUN_TAG` 默认值与已有服务的运行标记可能不同。管理已有服务时读取其node.json和部署记录，显式export实际标记；改变HOSTS、P/D数量、路径等环境变量后，后续status/stop也必须保留相同配置。切换RUN_TAG时检查PD_ROOT和REMOTE_TOOLS是否仍被旧的环境变量覆盖。

各协调节点需要相同配置。Shell进程中的变量不会自动持久化到其他节点；可传入同一组环境变量，或修改入口顶部的共同配置后分发。

## 自动与显式布局

三台16设备节点的自动布局共有六个8设备组，按主机顺序逐组分配：前P个为prefill，其余为decode。例如2P4D会在第一台放置两个P，其余两台各两个D。不要根据主机数量直接推断P/D实例数。

```bash
: "${TS_SRC:?指定Tokenspeed源码根目录}"
export SERVING_MODE=pd CODE_MODE=wheel
export RUN_TAG=flash_development
# HOSTS、路径等由任务配置；更换布局前释放相同设备和端口。
export PREFILL_INSTANCES=2 DECODE_INSTANCES=4
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" plan
```

同机1P1D的显式布局示例，替换IP为任务节点：

```bash
export SERVING_MODE=pd CODE_MODE=wheel RUN_TAG=flash_mixed
unset PD_LAYOUT_FILE
export PD_LAYOUT='
p0 prefill 192.0.2.10 0,1,2,3,4,5,6,7
d0 decode  192.0.2.10 8,9,10,11,12,13,14,15
'
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" plan
```

文件布局也支持JSON实例列表或带instances的对象。每行ID唯一，设备组不可重叠。合并模式设置 `SERVING_MODE=merged`；自动布局不使用P/D数量，显式布局用regular角色。切换回自动布局前取消PD_LAYOUT和PD_LAYOUT_FILE。

网关默认归属第一个P，merged默认归属第一个regular，可用GATEWAY_INSTANCE指定。整个布局只有一个SMG；所有实例的握手端口绑定在网关主机，不应把D节点地址拼成握手地址。

默认HTTP端口8010，本地引擎/metrics端口8120/8121、rendezvous9600、P bootstrap8998。同机第二实例的本地端口偏移100；握手端口从9010按全局实例顺序分配。plan和node.json是实际端口的依据，启动器还检查派生端口和非LISTEN连接。

## 环境和wheel

基础环境需匹配Torch/TorchNPU、CANN、engine/kernel/scheduler、SMG及NPU Mooncake；不能用CUDA Mooncake代替。基础venv的具体创建和离线安装步骤在开发指南“环境准备”中，不从旧venv复制site-packages冒充独立环境。

build要求三个源码仓库无未提交的已跟踪修改，并显式选择NATIVE_MODE。reuse只适用于native产物与源码、架构和ABI匹配的情况；更改native源码或工具链后用rebuild，不能以复用产物宣称重编译。

```bash
: "${BASE_ROOT:?指定兼容基础环境}"
: "${RELEASE_ID:?指定新的发布标记}"
source "$BASE_ROOT/runtime/env.sh"
export BUILD_ROOT="/workdir/build-$RELEASE_ID"
export RELEASE_ROOT="/workdir/releases/$RELEASE_ID"
NATIVE_MODE=reuse bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" build
# 文件制品发布；用户要求上传包仓库时才配置TWINE_REPOSITORY_URL。
env -u TWINE_REPOSITORY_URL bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" publish
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" check-release
```

发布包含mt-uni-ops、tokenspeed-longcat及匹配的Tokenspeed Python wheel，另有部署程序、build-info和SHA256清单。engine/kernel/scheduler来自基础环境；build对比Tokenspeed wheel与源码，install核对基础包版本及三个导入来源，不能绕过不匹配错误。

BUILD_ROOT和RELEASE_ROOT应为新目录，publish拒绝覆盖已有release。start检查已有远端release或从协调节点复制缺失目录，不复制基础环境、模型或源码。source模式的TS_SRC、UNI_SRC、PLUGIN_SRC应在所有目标节点存在并保持一致。

## SSH与资源

SSH用户、端口和认证由任务配置。密码模式需要可读非空的SSH_PASSWORD_FILE和sshpass，默认文件为入口旁abc.txt；使用chmod 600，不打印文件内容。密钥模式设置SSH_AUTH_MODE=key。使用现有密码文件，不把具体密码写进示例或额外的配置文件。

首次连接默认accept-new，保留已知主机密钥校验；仅在用户环境明确需要时调整SSH_HOST_KEY_CHECKING。分发使用临时目录原子替换，允许协调节点同时也是目标节点，不必绕开该节点重新实现一套scp逻辑。

stop仅作用于所选运行布局，退出可能等待Supervisor的stopwaitsecs。确认引擎、调度进程和NPU占用均已释放；不用启动检查失败来代替资源释放检查。启动前设置的独立Ascend缓存按实例隔离，不删除其他任务缓存。
