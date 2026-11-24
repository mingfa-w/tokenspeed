---
name: tokenspeed-deepseek-v41-deploy
description: 使用Tokenspeed现有部署入口在Ascend 910B运行DeepSeek V4.1 Flash，配置P/D分离、同机混合或P/D合并布局，准备uni-ops与ts-plugin wheel，执行启停、生成和KV传输验收。用于该模型的开发部署、环境检查或重启，不替代底层算子迁移。
---

# Tokenspeed DeepSeek V4.1 Flash 部署

复用仓库的部署程序完成环境准备、布局、服务管理和验收，不另建一套SSH批量启动脚本。

## 定位入口与任务

定位Tokenspeed源码根目录，记为 `TS_SRC`。读取适用仓库指令及 `docs/serving/deepseek-v41-910b.md`，核对当前检出分支、提交和部署程序。skill安装目录不是源码目录。

部署文件位于 `tools/deploy/deepseek_v41/`：

- `run_deepseek_v41_flash.sh`：开发者入口，集中配置并协调各节点。
- `deployment.py`：布局校验、实例端口分配、运行配置生成，由入口调用。
- `abc.txt`：入口默认读取的SSH密码文件，也可指定 `SSH_PASSWORD_FILE` 或使用密钥认证。只检查文件是否可读、非空及权限，不输出内容，不把密码写入skill或报告。

先区分用户要求查看、规划、新建、更新还是重启。读取现有实例的 `runtime/node.json` 和用户指定配置，明确模式、节点、运行标记、模型、依赖包及可操作的服务范围。沿用会话中已确认的选择，不把文档示例机器或历史运行标记当作新任务的固定配置。

## 准备环境与依赖

按开发指南的“环境准备”检查各节点CANN、Torch/TorchNPU、Tokenspeed engine/kernel/scheduler、SMG、Supervisor、NPU Mooncake及SHMEM的匹配关系。源码目录不提供现成的 `runtime/env.sh`：基础环境脚本由环境准备生成，实例脚本在部署准备阶段生成。

完整模型需包含config、权重索引及其指向的文件、Engram资源和tokenizer。`preflight` 只检查SSH、工具和部分路径，不能证明权重完整、ABI匹配或设备空闲。

优先使用匹配的已有release。依赖变更或新环境需要发布包时，按 [布局与发布](references/layout-and-release.md) 通过同一入口构建、发布、校验；不把候选wheel安装回在线基础venv，不绕过安装器的包版本或导入来源检查。

## 决定布局

先核对目标设备数量、HCCS拓扑和可用内存。仓库的910B配方每实例固定TP8/EP8/PP1，每台16设备按0–7、8–15两个HCCS组放置。三台机器的2P4D使用16设备P和32设备D；实例数量不是每实例EP大小，也不代表吞吐最优。

- 自动布局：设置 `HOSTS`、`PREFILL_INSTANCES`、`DECODE_INSTANCES`；P/D均至少一个，P+D必须等于所配置主机的8设备组总数。
- 显式布局：用 `PD_LAYOUT` 或 `PD_LAYOUT_FILE` 指定ID、角色、节点、设备组，适合同机P/D混合。两个输入互斥，设置后覆盖自动布局。
- 合并模式：`SERVING_MODE=merged`，自动布局的所有设备组作为regular实例；显式布局角色使用regular。

默认 `REQUIRE_FULL_NODE=1`，完整分配所引用主机的设备；用户要求部分设备测试时，显式设为0并保留完整8设备组。根据实际硬件与模型约束判断可行性，不将950的8设备布局直接套到910B，也不自行放开TP/PP限制。

先执行 `plan` 核对角色数量、设备无重叠、网关及端口。更多配置和运行标记规则见 [布局与发布](references/layout-and-release.md)。

## 执行部署或重启

在一个具备到全部目标节点SSH访问的协调节点执行入口；协调节点可以是P或D节点，不需要在每台机器重复执行start。各节点预置相同路径的基础环境和完整模型，source模式还需三个源码树。

先检查现有服务、设备和端口占用。用户要求重启或停止指定旧服务时，使用该布局的完整配置执行 `stop`，等待Supervisor退出并确认目标设备释放。仅要求检查或规划时不停止服务；不通过广泛pkill、清空缓存或删除环境来腾资源。

新布局、模型或运行参数使用新的RUN_TAG/PD_ROOT。运行标记隔离目录，不隔离设备和端口；启用新目录前仍需释放目标资源。保存并在每个命令中复用同一组环境变量。

执行顺序：`plan`、`preflight`、`start`、`status`、`check`。start自动分发工具和缺失release、准备全部实例、启动网关归属实例及其余P/D，最后执行smoke；它不是幂等重启命令。复用已安装实例会核对保存的布局及运行参数，不应修改保存文件绕过差异检查。

启动失败或部分实例已运行时，先读取状态和日志，区分安装、端口、设备、模型加载和传输问题；保留现场后修复具体原因，不反复盲目start或扩大停止范围。排查方法见 [验收与排查](references/verification.md)。

## 验收与交付

按 [验收与排查](references/verification.md) 检查所有worker健康、真实生成、顺序与并发、流式响应及P/D逐rank传输。在新运行目录或按请求ID和时间范围筛选证据，不能用旧日志的成功记录作新版本结论。

source开发使用三个源码目录和受支持的forward热重载；native库、schema、基础依赖等变更需重建并重新启动。重启前后分别验证，不能只检查热重载返回值。

交付实际使用的源码提交、release、模式、RUN_TAG、节点/设备/角色、网关、管理命令、日志位置和验收结果。明确已完成与未覆盖的测试，区分功能验收和吞吐/长期稳定性结论。只有用户要求修改代码或发布变更时才执行相应提交、推送或服务变更；创建skill本身不触发部署。
