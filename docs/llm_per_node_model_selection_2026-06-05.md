# Per-node LLM 选型 — 重设计基线 + 方法论 + runbook（2026-06-05）

平台所有 LLM 调用经 `resolve_model_for(node_key)` 按**功能块（node_key）**路由模型。本文记录把选型基线
重设计为「**可用性 + 成本筛**」的方法、实证结论，以及离线筛 → 在线 A/B → 审计落地的完整 runbook，供团队复用。

- 工具：`scripts/benchmark_llm_per_node.py`（基线 v2）、`scripts/_probe_coding_plan_models.py`（只读目录探针）、`scripts/phase_c_llm_routing_ab.py`（在线 A/B evaluator）
- 落地：`commit ba7ef8c`（基线 v2 + Phase B 遥测）、`f24a2aa`（config seed 对齐 live）
- 产物：`docs/llm_per_node_benchmark_2026-06-05_full.json`、`docs/coding_plan_catalog_2026-06-05.json`、`docs/phase_c_hypothesis_ab_2026-06-05.json`

---

## 1. 为什么重设计（旧基线的 4 个致命缺陷）

1. **离线再排「质量」= 测噪声，且答案线上已定**。历史 FINAL 数据里 expr 节点 `valid_rate`/`diversity` 全饱和=1.0，
   唯一变化项 `p_pass` 是窄带噪声（AUC 0.81 对 QUALITY 标签、非线上 Sharpe；首特征是 MD5 哈希记忆）。
   2026-06-01 在线 A/B 已证 reasoning 模型不赢 kimi、还更贵。→ **离线不该决定质量。**
2. **成本/推理 token 未核算**。reasoning 模型白烧的 `reasoning_content` token 被 `LLMService` 丢弃，旧基线只报扁平 token 数，
   让净负的 reasoning 模型越级。
3. **端点错指**。旧基线用 `provider="openai"` 裸跑会落到 `api.openai.com`（空 key=死端点），根本打不到 coding.dashscope。
4. **覆盖缺口**。`distill_context`（FLAT 热路径）未覆盖。

**重设计原则**：离线只交付**它能有效测的东西** — 可达性、可用性（parse/validity 筛/截断率@生产 max_tokens）、
成本（quota-token 含推理溢价）、可靠性。**质量交在线 A/B 作安全否决**（在线闸 1.5% PASS 基率 + 100-sim 门，只能否决回归不能确认提升）。

---

## 2. 基线 v2 测什么（`benchmark_llm_per_node.py`）

对每个生产 node 驱动其**真实 prompt** + **生产 max_tokens**，输出独立列（不再塌缩成单一分数）：

| 列 | 含义 |
|---|---|
| `reliability` | 1 − call_fail/calls（API 失败=缺失数据，剔除出均值，不当 score-0） |
| `parse_rate` | 合法 JSON / 成功调用 |
| `validity_rate` | **筛子非排序器**：真实算子（`reject_unknown_operators`）+ arity（解析算子 `definition` 串，`param_count` 全 0 不可用）+ group-as-value（有类型 fields）。断言锁定：5 个 BAD_EXPRS 必判 invalid |
| `truncation_rate` | `finish_reason∈{length}` 或 parse-fail，在**生产** max_tokens 下测 |
| `quota_tokens` | 主成本轴 = mean total tokens/call（Coding Plan 固定配额订阅，每 token 含被丢弃 reasoning 都耗配额） |
| `reasoning_share` | reasoning_tokens / completion_tokens — **推理溢价诊断**（旧基线缺的关键维度） |
| `p_pass` / `diversity` | **仅诊断列**，不进排名 |

成本轴=配额 token（不做 USD：coding-plan 模型在 `LLM_PRICING` 无价）。reasoning 类**按实测 `reasoning_share` 判**，不靠目录标签
（kimi-k2.5 目录标"深度思考"但实测 reasoning_tokens=None=非推理）。

### Phase B 生产遥测（`llm_service.py`，零迁移）
`LLMResponse` 新增 `prompt_tokens`/`completion_tokens`/`reasoning_tokens`/`finish_reason`/`truncated`
（`+=` 累加于 parse-retry；reasoning 取自 `usage.completion_tokens_details.reasoning_tokens`；仅 service 层不碰 append-only 协议/mocks）。

---

## 3. 怎么跑（含安全门）

> **⚠ 共享配额**：基线与生产挖掘**共用同一 Coding-Plan key/配额/Redis 熔断**。跑前**停生产挖掘**（无 RUNNING/PENDING MiningTask）或用独立 key。

```bash
# 1) 只读目录核验（低配额）：确认端点重指向 + 哪些目录模型可达 + incumbent 可达性
venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --verify-catalog
#    或更轻量的独立探针：
venv/Scripts/python.exe scripts/_probe_coding_plan_models.py

# 2) 冒烟（1 模型 1 节点）：验证字段落位 + 产出全量 token 预估
venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --smoke

# 3) 全量筛查（需显式确认共享配额）
venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --i-understand-quota
#    → docs/llm_per_node_benchmark_<date>_full.json（per-node 记分卡 + 决策 + 在线 A/B payload）
```

内置安全门：硬 call/token 预算 + 到顶 abort、并发=1 + call 间 sleep、**遇 429 立即 HALT**（不让 SDK @retry 继续烧）、
`--reset-circuits` 默认 OFF（不清生产共享熔断）。`models.list()` 在 coding.dashscope **返 404 → 自动退 per-model 探针**。

---

## 4. 决策规则（默认保便宜现役，别优化噪声）

- **默认 KEEP INCUMBENT**（线上验证过的便宜模型）。
- 仅以下才标"待审"：① incumbent 在新端点**不可达/坏** → 强制换可达模型；② **reasoning 节点**高推理溢价 **且** 非推理候选可用
  且 `quota_tokens` 显著更低（>20%）→ 在线 A/B；③ expr 节点新非推理 coder 模型 usability≥incumbent 且更便宜 → 在线 A/B。
- incumbent 来源**读 `_flag_override_cache["LLM_FUNCTION_MODEL_MAP"]`**（先 `FeatureFlagService.load_overrides_into_cache` 暖缓存），
  **绝不** `settings.X`（`__getattribute__` 只认 `ENABLE_` 前缀）。

---

## 5. 实证结论（2026-06-05，coding.dashscope，260 calls）

**头号发现 — 推理 token 溢价**：唯一在用的推理模型 `qwen3.6-plus` 在**每个节点**烧 `reasoning_share` 0.79–0.97，
quota 成本是 `kimi-k2.5` 的 **1.8–5.1×**，而可用性指标**完全相同**（parse/schema_ok/grounding/correct/stability 全 1.0、截断 0）：

| 节点 | kimi-k2.5 qtok | qwen3.6-plus qtok | 溢价 | qwen rshare |
|---|---|---|---|---|
| llm_crossover | 911 | 4667 | 5.1× | 0.944 |
| distill_context | 398 | 1891 | 4.7× | 0.90 |
| llm_mutate | 916 | 3675 | 4.0× | 0.935 |
| r1b_mutate | 934 | 3539 | 3.8× | 0.905 |
| r1b_retry | 1290 | 4346 | 3.4× | 0.972 |
| r5_c1 / c2 / attribution | ~430 | ~1360 | ~3.1× | 0.94 |
| hypothesis | 2696 | 7118 | 2.6× | 0.788 |
| code_gen | 4249 | 9432 | 2.2× | 0.867 |

**结论**：9 节点 KEEP kimi（已最优）；`hypothesis` + `distill_context`（经 `__default__`）原走 qwen3.6-plus，付大溢价无可用性收益 → 换 kimi。
新非推理 coder 模型（`qwen3-coder-next/plus`）expr 节点可用性≥kimi、更快，但成本≈kimi（非 >20% 更便宜）→ KEEP kimi。

---

## 6. 离线 → 在线 A/B runbook（单 worker 约束下=串行）

**关键约束**：Windows celery `--pool=solo` + FLAT 串行化 → **同一时间只有一个 FLAT task 真正执行**（并发双臂会饿死一臂）。
故并发双臂 A/B 不可行，改**串行**（代价=时间漂移混入；对"不变差"回归检查可接受）。

```bash
# 前置（暖缓存后读真实值）：ENABLE_FLAT_CONTINUOUS / ENABLE_COST_TELEMETRY(落 LLMCallLog 供 validity 核对)
#                         / ENABLE_PER_FUNCTION_LLM_ROUTING 均需 ON；ops 无 OPS_API_TOKEN=未鉴权可直调
# datasets 必须显式且两臂一致（取生产最常挖的，如 pv1+fundamental6）

# 1) control（默认模型）
curl -X POST http://localhost:8001/api/v1/ops/start-flat-session -H "Content-Type: application/json" \
  -d '{"region":"USA","universe":"TOP3000","datasets":["pv1","fundamental6"],"delay":1}'

# 2) treatment（仅覆盖目标 node；provider_ref 会被 _validate_model_entry 展开）
curl -X POST http://localhost:8001/api/v1/ops/start-flat-session -H "Content-Type: application/json" \
  -d '{"region":"USA","universe":"TOP3000","datasets":["pv1","fundamental6"],"delay":1,
       "llm_overrides":{"hypothesis":{"model":"kimi-k2.5","provider_ref":"aliyun_coding_plan"}}}'

# 串行执行（单 worker）：先暂停 treatment，control 独占跑满，再切：
curl -X POST .../ops/flat-sessions/<TREAT>/pause     # status→PAUSED，保 flat_cursor
#  ... control 跑到 ~80-100 real_sims（注意 FLAT 单 dispatch 在 daily_goal≈20-25 即 COMPLETED，需多次 resume 累积）...
curl -X POST .../ops/flat-sessions/<CTRL>/pause
curl -X POST .../ops/flat-sessions/<TREAT>/resume    # treatment 独占跑同量

# 3) 评估（只读）
python scripts/phase_c_llm_routing_ab.py --control-task <CTRL> --treatment-task <TREAT> --node <node> \
  --out docs/phase_c_<node>_ab_<date>.json
```

**判读**（evaluator <100 sim → in-sample sharpe 退化 + 标 `insufficient_sample`；正 Sharpe 封顶 PARTIAL）：
- `validity` 必须 OK（`routed_share=1.0`，treatment 真跑覆盖模型）；
- verdict **NO-GO / 显著负** → 保留现役；**GO / PARTIAL / 非显著** → 换便宜模型（在线没否决=可换）。

**注意**：① distill_context 这类节点，在线 PASS-per-sim 隔多层是**差代理**，离线 `grounding`（直接定 focused_fields）更相关 → 可基于离线证据直接切；
② hypothesis 这类直接驱动 alpha 质量的节点，在线 A/B 更有意义（但本次 25/臂仍 PARTIAL/噪声）。

---

## 7. 落地（审计路径，**不手改 DB**）

map 变更走 `PATCH /ops/flags/{name}`（= LLMRoutingConsole 用的审计端点），写 DB override + audit log + 刷缓存：

```bash
# body = {"value": <完整新 map dict>, "note": "<理由>"}；类型须配 flag_type；LLM_FUNCTION_MODEL_MAP ∈ SUPPORTED_FLAGS
curl -X PATCH http://localhost:8001/api/v1/ops/flags/LLM_FUNCTION_MODEL_MAP \
  -H "Content-Type: application/json" -H "X-Ops-Actor: <who>" --data-binary @body.json
curl -X POST  http://localhost:8001/api/v1/ops/flags/refresh-all -H "X-Ops-Actor: <who>"   # 广播到 worker 进程
```
- 构造 body 时**只改目标键**：先读当前 `_flag_override_cache["LLM_FUNCTION_MODEL_MAP"]`（暖缓存后），改目标 node，整 map 回写。
- **cache-cold 硬化**：把 `config.py` 的启动 seed（`_load_llm_function_model_map`）**对齐 live**，否则 override 被清会落到失效模型；
  并保证每个 node 的 model ∈ 其 provider 的 `_PROVIDER_MODEL_CATALOG`（`test_llm_provider_catalog` 守卫）。

---

## 8. 最终态（2026-06-05）

生产 routing map **全节点 kimi-k2.5**（非推理、便宜、在线已验证）；推理 token 溢价全清；运行时 override 与启动 seed 一致；
候选目录 + 守卫测试防失效模型复发。唯一例外曾是 `__default__`（catch-all），现也已切 kimi。

**复用提示**：菜单换模型 / 新增 provider 时，重跑 §3 的 `--verify-catalog` + `--smoke` + 全量筛查，按 §4 决策规则取 top-1/top-2，
热点或新模型按 §6 串行 A/B，按 §7 审计落地 + seed reconcile。**核心心法**：离线测可用性+成本（确定信号），质量交在线否决（别用离线噪声决定质量）。
