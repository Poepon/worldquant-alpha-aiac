# Phase C — per-功能块 LLM 路由单 node A/B runbook

- 日期: 2026-05-30
- 配套: `docs/per_function_llm_routing_plan_2026-05-29.md` §6(A/B)+ §7(Phase C 灰度)
- 工具: 启动端点 `POST /api/v1/ops/start-flat-session`(已接 `llm_overrides`)+ evaluator `scripts/phase_c_llm_routing_ab.py`
- 首发 node: **code_gen**(§7 点名先验证热路径;§3 runs=3 定稿 `qwen3.6-plus`)

## 设计:单 node 单变量 + 双 arm 并发

A/B = 两个 FLAT task,**同 region/universe/datasets、同时启动并发跑**:
- **control**:不带 `llm_overrides` → code_gen 用默认模型(`deepseek-chat`)
- **treatment**:`task.config["llm_overrides"] = {"code_gen": {"model":"qwen3.6-plus","provider":"openai"}}` → 只 code_gen 改模型,其余 node 全默认

为什么并发同窗口:plan 放行条件 6② + memory 记录 **Phase B 的 ≥24h pipeline soak 被跳过** → 无法区分"流水线本身漂移"和"新模型提升"。**并发跑双 arm 让时间/数据漂移对两臂等同抵消**,这是当前唯一干净的归因方式。串行先后跑会把 pipeline 漂移混进结论。

`ENABLE_PER_FUNCTION_LLM_ROUTING` **保持 OFF**:task 级 `llm_overrides` 经 `resolve_model_for` 独立于全局 flag 生效(只影响 treatment 这一个 task),全局其它 task 不受影响。

## 前置

1. worker / beat 在线(`run.bat --start`),BRAIN 凭证可用。
2. **`ENABLE_COST_TELEMETRY=ON`**:否则 `llm_call_log` 无行,evaluator 的 cost/$ 段与 A/B-validity(模型核对)失效。
3. 确认 `qwen3.6-plus` 在 DashScope 可达(§3 实测 9 模型零 fail;若改用别的模型,先 `scripts/benchmark_llm_per_node.py --models <id>` 验证返 200)。
4. **不要把 treatment 路由到 anthropic / 易抖动的原厂端点**:当前熔断仍是全局单例(见 [[project_llm_routing_integration_tests_2026_05_30]] review 缺口①),routed provider 抖动会跳全局闸、brown-out 健康默认 provider 并掐掉降级。A/B 期间 treatment 留在 DashScope openai-compat。

## 步骤

### 1. 启动双 arm(同参数,仅 treatment 带 override)

```bash
# control(默认模型)
curl -X POST http://localhost:8001/api/v1/ops/start-flat-session \
  -H "X-Ops-Token: $OPS_TOKEN" -H "Content-Type: application/json" \
  -d '{"region":"USA","universe":"TOP3000","datasets":["pv1","fundamental6"],"delay":1}'

# treatment(code_gen → qwen3.6-plus)
curl -X POST http://localhost:8001/api/v1/ops/start-flat-session \
  -H "X-Ops-Token: $OPS_TOKEN" -H "Content-Type: application/json" \
  -d '{"region":"USA","universe":"TOP3000","datasets":["pv1","fundamental6"],"delay":1,
       "llm_overrides":{"code_gen":{"model":"qwen3.6-plus","provider":"openai"}}}'
```

记下两个返回的 `task_id`(= control_task / treatment_task)。**两个 datasets 列表必须一致**(显式给,别用 AUTO,否则 bandit 选不同集→混入数据混杂)。

### 2. 观察期

- 让两 task 并发跑 **5–7d**(§7),或直到任一臂 `real_sims ≥ 100`(PASS-per-sim 二项基率 ~1.5%,见 evaluator 的 insufficient 提示)。
- 期间别动全局 flag、别改这两个 task 的 config。

### 3. 读 evaluator

```bash
python scripts/phase_c_llm_routing_ab.py \
  --control-task <C> --treatment-task <T> --node code_gen \
  --out docs/phase_c_code_gen_ab_2026-06-XX.json
```

输出每臂:`passes / real_sims / pass_rate`、连续 `is_sharpe`(高功效)、code_gen 的 `node_models / cost_usd / latency`、整 task `total_cost / cost_per_pass`;以及 verdict。

## 决策矩阵(evaluator `verdict.decision`)

| decision | 含义 | 动作 |
|---|---|---|
| **INVALID** | treatment 的 code_gen 模型 == control(override 没生效)或该 node 没调用 | 重启 treatment arm,确认 payload 带 `llm_overrides`;别下结论 |
| **GO** | PASS-per-sim 效应 >0 且 bootstrap CI 下界 > floor(默认 −0.10pct-pts),cost 未显著恶化 | 把该 node 写进 `LLM_FUNCTION_MODEL_MAP`(经前端 LLMRoutingConsole),再推下一 node |
| **NO-GO** | 效应 < floor 或 CI 上界 <0 | 保留默认模型,不切 |
| **PARTIAL** | 不显著 / 样本不足且 sharpe 也不显著 / 质量升但 `cost_per_pass` 涨 >20%(cost flag=WORSE 把 GO 降级) | 继续观察或权衡成本;cost-WORSE 的 GO 是运维取舍(质量 vs $) |

- **样本不足**(任一臂 `real_sims < 100`):evaluator 自动退到**高功效的 in-sample-sharpe**信号(Welch t + Cohen's d)出决策,并标 `insufficient_sample=true`。
- **cost guardrail**:`cost_per_pass` treatment 比 control 涨超 `--cost-tolerance`(默认 20%)→ flag `WORSE` → 即便质量 GO 也降级 PARTIAL,交人判。

## 推广顺序(§7)

code_gen ✓ → hypothesis(`deepseek-v4-pro`,**runs=1 方差大 0.80~0.96 + 76s 延迟**,最该实证复核)→ 其余 runs=1 node(self_correct/r1b 等)逐个单变量验。每个 node 一轮独立 A/B,**永远单 node 单变量**,不要一次翻多个。

## 注意

- evaluator 是**只读**,不启动/不改任何东西。
- 决策落地(写 map)走前端 LLMRoutingConsole(PR4)的审计路径,不要手改 DB。
- runs=1 的 §3 选型是 offline proxy,**线上 p_pass 才是 ground truth**——A/B 结论优先于 benchmark。
