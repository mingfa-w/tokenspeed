# 验收与排查

所有命令使用同一组SERVING_MODE、CODE_MODE、RUN_TAG、布局和路径变量。以下命令中的TS_SRC应指向目标Tokenspeed源码；也可在节点侧工具目录直接调用同名入口。

## 服务与生成

```bash
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" status
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" smoke
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" check
bash "$TS_SRC/tools/deploy/deepseek_v41/run_deepseek_v41_flash.sh" evidence
```

验收分层判断：

| 层次 | 证据 |
|---|---|
| 安装 | 各实例runtime/install-complete，release校验及包版本/导入路径通过 |
| 进程 | 各实例引擎及唯一网关的Supervisor状态为RUNNING |
| worker | /workers的角色、数量与布局一致，全部ready且healthy |
| smoke | 一次真实问答正确，HTTP200且finish_reason=stop |
| check | 6次顺序和2次并发请求全部通过，原始响应中的文本和结束原因正确 |
| P/D | 所有D实际完成请求，对应P/D请求ID关联，各TP rank记录KV发送/接收成功 |
| 资源 | npu-smi设备健康、进程属于目标实例，HBM及主机内存有余量 |

start自带smoke，check仍需单独执行。check当前包含数字、城市及多行输出，不包含流式检查；若用户要求完整接口流程，补充流式请求，检查内容、finish_reason和[DONE]。请求的model值从node.json或网关模型列表取得，不假设worker一定使用友好别名。

从生成记录读取passed、checks、concurrent_checks及workers_after；校验响应后再判断通过。错误记录、超时或部分worker健康不能表述为部署成功。

## 逐rank证据

node.json提供gateway_host、http_port、worker_counts、设备和端口信息。按本轮请求ID与时间范围关联日志：

- P日志：ts_prefill_kv_transfer_queued及对应完成事件。
- D日志：ts_decode_kv_transfer_success、ts_request_finished。
- 核对各实例的8个rank和所有D覆盖，并确认传输使用ascend_direct。

evidence命令收集D端传输成功和请求完成事件，不自动筛除旧请求，也不收集所有P端queued事件。需要同时读取engine.log及滚动日志，不能只看evidence文件的事件数。固定8条问答不保证覆盖任意规模布局；补充请求直到目标worker覆盖，避免把累计历史计数当成本次覆盖。

合并模式没有P/D KV传输要求，应验证regular实例健康和真实生成，不机械要求其出现decode传输事件。

## 日志与失败定位

每实例日志在 `PD_ROOT/<ID>/logs/`，生成与传输记录在validation/，运行配置在runtime/node.json。`log` 按主机收集服务和验证记录，`log_plog` 收集Ascend日志。

| 现象 | 检查方向 |
|---|---|
| SSH失败或preflight中断 | 用户、端口、密码文件权限/非空、sshpass、主机密钥；不输出凭据 |
| 基础环境或wheel不匹配 | BASE_ROOT/runtime/env.sh、build-info、基础包版本、ABI及SHA256；创建兼容环境或新release |
| prepare拒绝已有目录 | 目录是否安装完整、保存的布局指纹/参数是否一致；不删除标记或改node.json绕过检查 |
| 设备或端口占用 | 目标服务是否完全退出、是否有其他工作负载、同机端口偏移和TIME_WAIT等连接 |
| worker长期pending | 各实例安装/引擎日志、模型加载进度、退出重试、OOM、握手与数据面连通性 |
| 生成失败或只有部分D可用 | 请求ID、KV传输、Mooncake/ascend_direct、返回内容、结束原因及各rank错误 |

启动器的readiness等待是有界的。加载仍在正常推进时观察进度，不额外重复start；明确失败时保留日志并诊断。部分实例已启动后，按授权范围处理已启动实例，再修复配置或环境重试，不用强制清空基础环境掩盖问题。

## 源码开发与回退

CODE_MODE=source启用三个源码树及支持的forward热重载。按开发指南激活网关归属实例的runtime/env.sh，执行 `ts reload-forward --url <gateway>` 并检查模块报告。native库、schema或基础依赖变更应重建并重新启动；最终代码在新启动实例上验收。

更新依赖时保留原release和配置。失败回退先停止候选布局并释放资源，再以原配置启动原环境，不将候选wheel反向安装到在线基础venv。

## 离线回归

只改部署工具或文档时，先运行与改动对应的检查；启动器回归不需要NPU：

```bash
cd "$TS_SRC"
source .venv/bin/activate
python -m pytest -q test/cli/test_deepseek_v41_deployment.py
```

程序测试、导入或构建通过不等于模型部署通过。创建或编辑skill时校验结构、引用、命令及离线布局即可，不为验证skill而自动重启在线服务。
