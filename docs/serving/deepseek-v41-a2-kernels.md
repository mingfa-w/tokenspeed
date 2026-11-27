# DeepSeek V4.1 Flash 910B 算子管线

## 接口与启用

910B 路径在加载时将稠密 FP8 权重解码为 BF16，KV 使用 page-planar 字节缓存。A2 执行管线沿用此协议，不直接消费 A5 的按行打包缓存、AICPU metadata，也不额外引入 A5 激活量化。`uni_ops.deepseek_v41_runtime_a2` 提供 BF16 serving 接口；A5 DSL 兼容接口独立保留。

| 类别 | A2 执行方式 | 框架边界 |
| --- | --- | --- |
| Attention Prologue | BF16 投影、Triton RMSNorm/RoPE | 返回 QR/SWA/Q；backend 管理缓存写入 |
| Attention Epilogue | Triton inverse RoPE、BF16 分组及输出投影 | 模型层执行 TP all-reduce |
| Indexer Prologue QW | BF16 Q/weight 投影、Triton RoPE | 保留 head 分片与 weight dtype |
| Indexer Prologue K | BF16 投影、Triton RMSNorm/RoPE | 缓存写入沿用原 codec |
| Quant Lightning Indexer | 融合分页读取/FP4 解码、框架归约与 TopK | 返回原逻辑 token/block 索引 |
| Quant Sparse Lightning Indexer | 候选 block 约束下的同类管线 | 保留候选过滤、tie-break、最新 block |
| Sparse MLA | Triton 分页读取/FP8-FP4 解码、分块 FP32 BMM | SWA/global 联合 softmax，sink 只参与一次 |
| Engram Gate | AscendC norm/dot/signed sqrt/sigmoid/residual | false token mask 透传，BF16 输出 |

这些是多 kernel 管线，不等同于 A5 原生低精度 Cube 融合 kernel。必须配套安装包含 `V41ExecutionOps` 的 Tokenspeed，以及匹配的 ts-plugin、uni-ops wheel，不能只升级插件。

在实际执行 `ts serve` 的进程环境中设置：

```bash
export TOKENSPEED_DISABLE_PLUGINS=longcat
export TOKENSPEED_KERNEL_DISABLE_PLUGINS=longcat
export TS_DSV41_PIPELINES=a2
export TS_DSV41_ATTENTION_IMPL=fused
```

使用生成的 `runtime/engine.sh` 时，将设置写入对应实例的 `runtime/env.sh`。PD 的 P/D 进程使用相同 wheel 和设置。这组管线支持 `numerics=auto`，不声明 rl-bitwise 或 batch-invariance。其他 TP/EP/PP 布局需另行验证；当前在线记录为 TP8/EP8/PP1。

`TS_DSV41_PIPELINES=reference` 选择原模型管线。注意力可独立选择 `TS_DSV41_ATTENTION_IMPL=reference|batched|fused`，分别对应逐 query 参考实现、批量 BMM、批量 BMM 加 Triton 分页解码。不设置时保留原路径；无效值或指定实现不可用时直接报错，不自动回退。

## 数值审计

在候选 engine 的环境中设置限次审计，每个 rank 独立计数；`0` 表示关闭：

```bash
export TS_DSV41_ATTENTION_VERIFY_DIR=/workdir/deepseek-a2-validation
export TS_DSV41_ATTENTION_VERIFY_CALLS=4096
export TS_DSV41_PIPELINE_VERIFY_CALLS=512
export TS_DSV41_INDEXER_VERIFY_CALLS=128
```

相同输入会额外执行参考实现。审计失败中止请求，不修改候选结果。日志只包含形状与误差，不包含文本或激活张量：

- `attention-*.jsonl`：MLA 输出及可选 LSE。
- `pipelines-*.jsonl`：投影管线及 Engram Gate 的 max_abs、RMSE、reference_RMS。
- `indexer-*.jsonl`：QLI/QSLI 四个整数输出是否完全相同，及有效候选数量。

MLA 逐元素阈值为 `atol=0.002, rtol=0.02`；LSE 为 `atol=rtol=2e-5`。其余浮点管线要求 `RMSE <= max(1e-4, 0.003 * reference_RMS)` 且 `max_abs <= max(0.003, 0.02 * reference_max_abs)`。Indexer 的 token/block 索引和长度要求完全相同。

区分 warmup 与真实请求；QLI/QSLI 记录必须出现 `max_visible > 0`、`selected_count > 0`。注册成功、空候选调用不构成有效覆盖。BF16 舍入可能改变接近的 logits 对应的生成 token，因此还需服务级对照。

## 在线验证

候选和参考使用相同权重、配置、greedy sampling、输入与 token 上限，独立分配设备和端口，关闭 prefix cache。记录文本、token 数、结束原因与耗时。审计期间的耗时不用于性能结论。

配套记录在一台 16 卡 910B 服务器上，使用 0–7 卡、8020 端口作为候选，8–15 卡、8000 端口作为参考。6 组提示词覆盖算术、Python、字段提取、JSON、长材料检索和英文翻译。这是功能与数值回归，不替代 MMLU/C-Eval、长上下文质量或并发稳定性评测。

通过 `/v1/models` 获取服务注册的模型 ID，替换下方 `MODEL_ID_FROM_V1_MODELS`：

```bash
curl --noproxy '*' http://127.0.0.1:8020/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"MODEL_ID_FROM_V1_MODELS","messages":[{"role":"user","content":"请只回答数字：17乘以23等于多少？"}],"temperature":0,"max_tokens":8,"chat_template_kwargs":{"thinking":false}}'
```

## 稀疏 MLA 性能

910B2C，独占设备，BF16 Q `[T,8,512]`，FP32 BMM。2 次预热，3 组、每组 3 次，取中位数；包含 Python 和设备执行，排除输入构造与 JIT。分页场景选择 SWA 128 + global 512 条 KV；prefill 从 BF16 工作区选择 128 条 KV。

| 场景 | Query 数 | 原实现 ms | 仅批量化 ms | 融合读取 ms | 加速比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Prefill 工作区 | 1 | 0.218 | 0.217 | 0.216 | 1.01x |
| Prefill 工作区 | 16 | 3.147 | 0.220 | 0.207 | 15.22x |
| Prefill 工作区 | 128 | 22.799 | 0.880 | 0.885 | 25.77x |
| 分页缓存 | 1 | 4.701 | 4.345 | 0.541 | 8.68x |
| 分页缓存 | 16 | 72.982 | 39.953 | 4.823 | 15.13x |
| 分页缓存 | 128 | 579.374 | 275.071 | 37.217 | 15.57x |

这是 selected-attention 加速比，不是整模型吞吐倍数。后续可减少 gather program 数与重复 scale 读取，评估 KV 分块 softmax；先保持当前数值协议，再评估 Cube 路径。

## 真机与 wheel 测试

加载 CANN 和运行环境，在已分配设备上执行 uni-ops 测试：

```bash
export ASCEND_RT_VISIBLE_DEVICES=0 UNI_OPS_SKIP_NATIVE_LOAD=1
export UNI_OPS_TEST_ALL_A2=1 UNI_OPS_TEST_ENGRAM_A2=1
python -m pytest -q tests/test_selected_attention_a2.py \
  tests/test_runtime_pipelines_a2.py tests/test_engram_gate_a2.py \
  tests/test_engram_gate_precision_a2.py
python benchmarks/benchmark_selected_attention_a2.py
```

wheel 验证应将测试文件复制到仓库外，清除源码 PYTHONPATH，确认三个包的 `__file__` 均来自安装目录，并使用新的 `UNI_OPS_JIT_CACHE_DIR`、`TRITON_CACHE_DIR`。配套 wheel 独立验证为 67 项通过。

## 验证记录

[机器可读验证记录](deepseek-v41-a2-validation.json)包括输入规模、生成结果、各管线误差和 wheel SHA256。审计计数包含 warmup，不能等同于独立请求数。

| 项目 | 结果 |
| --- | --- |
| 八类管线 | 全部通过数值审计 |
| 独立 wheel | 仓库外、无源码 PYTHONPATH、新 JIT 缓存，67 项 NPU 测试通过 |
| 源码回归 | 121 项 NPU + 6 项 CPU 通过，2 项可选 TileLang 原始 provider 真机测试未运行 |
| 框架 Engram 回归 | 38 项通过、14 项按条件跳过 |
| 固定生成用例 | 6/6 与参考文本逐字相同 |
| 最终安装后生成 | 算术、英文翻译 2/2 与参考文本相同 |
| QLI/QSLI | 各 480 次非空候选审计，token/block 索引和长度完全相同 |
| Indexer 评分 | BF16/FP32 权重，1/8 head-shard 归约，4 组逐值完全一致 |

固定用例期望输出依次为 `391`、`1024`、`728491`、`true`、`5937`、`Apple`。生成一致不代替算子误差比较；审计误差阈值也不代表完整模型基准评测结论。

## 优化实现与约束

原 selected-attention 对每个 query 单独 gather、解码并执行矩阵计算。`batched` 路径按 `query_chunk_size` 分组，减少 Python 循环和 kernel launch。`fused` 路径再将分页定位、字节读取和反量化合并为 Triton kernel：

- SWA 为 FP8 E4M3 payload 和 group32 E8M0 scale；global 为 FP4 E2M1 payload 和 group16 E4M3 scale。不能套用 A5 BF16-scale 的 raw-row 布局。
- 页、slot 与 stride 的地址运算先转 int64，支持跨页、strided cache field 和无效 slot。
- gather 后仍保留 BF16 舍入，QK/PV 使用 FP32 BMM；SWA/global 联合归一化，sink 没有 Value。
- 无效 KV 在 BMM 前清零，避免被忽略的 NaN/Inf 经由 `0 * NaN` 污染结果；空候选输出为零。
- Engram Gate 对 Rsqrt、Reciprocal 进行 Newton 修正，保留 signed-zero 处理；真实模型审计的最大 NRMSE 为 `1.72e-5`。

当前主要剩余开销是分页 gather 的 program 数、重复 scale 读取与 FP32 BMM。后续优化需同时测 decode 和 prefill；降低计算 dtype 或更换 softmax/归约顺序前，先重新验证误差、索引选择和生成结果。

## 复现固定生成用例

以下命令在 Tokenspeed 仓库根目录执行。服务须使用同一模型权重和上述 sampling/缓存配置；模型 ID 从服务读取，不依赖本地权重目录名称。可分别对参考、候选服务设置 `API_BASE` 后运行，比较每条结果与 fixture 中的参考文本。

```bash
export API_BASE=http://127.0.0.1:8020
python - <<'PYCODE'
import json
import os
import urllib.request
from pathlib import Path

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = os.environ["API_BASE"].rstrip("/")
with opener.open(base + "/v1/models", timeout=10) as response:
    model = json.load(response)["data"][0]["id"]
fixture = json.loads(Path("docs/serving/deepseek-v41-a2-validation.json").read_text())
for case in fixture["generation_cases"]:
    body = {"model": model, "messages": [{"role": "user", "content": case["prompt"]}],
            "temperature": 0, "max_tokens": 8, "chat_template_kwargs": {"thinking": False}}
    request = urllib.request.Request(base + "/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=300) as response:
        actual = json.load(response)["choices"][0]["message"]["content"]
    print(case["case"], repr(actual), "exact=", actual == case["reference_text"])
PYCODE
```
