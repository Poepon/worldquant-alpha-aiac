# 流水线 per-功能块 LLM 模型路由 plan v2.1

- 日期: 2026-05-29
- 状态: **GO-with-conditions(可开 PR1)** · v1 经 3-lens 对抗审查→v2 · v2 经 verification round→v2.1(6 放行条件)
- 目标: 让流水线不同功能块 (node) 使用不同 LLM 模型 —— 热路径质量/性价比最优,辅助路径压便宜;**配置在前端可随时改、即时生效、带审计**
- 设计核心: ① 路由挂现成 `node_key` 轴 ② 配置复用 `FeatureFlagOverride`,但 **map 本体直读 `_flag_override_cache`(不走 `settings.X` —— 见 §0 P0-1)**;均不另造系统
- 用户已拍板: ① 热路径基调 = **多样性优先** ② 下一步 = 先落 plan ③ 接入 = **阿里云百炼为主 + 原厂端点兜底 (混合)**

---

## 0. v1 → v2 修正 (3-lens fresh-agent review 收敛)

三个独立 fresh agent 各自亲读源码核验,**收敛出 2 个 P0(会让方案静默失效或硬崩)** 与一批 P1。已亲验确认,全部纳入 v2。

### P0-1 【配置载体机制不成立 —— 头号需求假死】(3 agent 独立命中)
v1 §1.4 称 "`Settings.__getattribute__` 让任意属性读 override"。**错**。亲验 `config.py:1847-1856`: hook **只拦 `name.startswith("ENABLE_")`**。`LLM_FUNCTION_MODEL_MAP` 非 `ENABLE_` 前缀 → `settings.LLM_FUNCTION_MODEL_MAP` 永远返回 Pydantic 启动默认值,前端改了 `refresh-all` 也读不到 → 路由从不生效(且总开关能动,易误判"功能正常")。
- v1 引为先例的 `REGIME_STAGE`/`QLIB_PRESCREEN_MODE` **恰是反证**: `load_overrides_into_cache` 把它们写进 `_flag_override_cache`(feature_flag_service.py:1219-1230),但消费侧 `getattr(settings, "QLIB_PRESCREEN_MODE")`(qlib_prescreen.py:367)/ `self.REGIME_STAGE`(config.py:1808)走 `settings.X` → hook 不命中 → **override 写得进 DB、读不出 settings**(仓库既存潜伏 bug,测试用 monkeypatch 直接赋值掩盖了)。
- **修**(v2 采纳方案①,最小侵入): `resolve_model_for` **不读 `settings.LLM_FUNCTION_MODEL_MAP`,直接 `from backend.config import _flag_override_cache` 读 `_flag_override_cache.get("LLM_FUNCTION_MODEL_MAP")`** + env 默认兜底(feature_flag_service.py:1042 已有同款先例)。总开关 `ENABLE_PER_FUNCTION_LLM_ROUTING`(ENABLE_ 前缀)可正常走 hook。**必须配跑通真实 cache 的集成测试,不能全 mock**(memory: 全 mock 掩盖真实失效)。

### P0-2 【per-call 切 anthropic 时 `anthropic_client is None` → AttributeError 硬崩】(lens2 深挖)
亲验 `llm_service.py:295-328`: `self.anthropic_client` **只在构造时 `provider=='anthropic'` 才实例化**;默认 provider=openai 的单例 `anthropic_client=None`。一旦 routing 把某 node 切到 anthropic,`call()` 走 `self.anthropic_client.messages.stream` → `None` 崩。v1 "多 provider 已支持" 仅指**构造时**,per-call 切 provider 无 client 可用。
- **修**: PR2 必须 **model + provider + client 三者原子一起切换**;openai→anthropic 需 lazy 构造并缓存 anthropic client(与 openai client 分两套缓存,SDK 类型不同)。

### P1 修正清单 (全部纳入)
1. **thinking 连带打开**: 路由到 `claude-opus-4-7*` 会强制开 thinking 流式(llm_service.py:582,延迟/token 剧增,且丢弃 temperature),benchmark 选型没算这笔。`_resolve_effort` 在 effective provider≠anthropic 时短路;map entry 支持显式 `thinking_effort:"disabled"`。
2. **swap hack 不能分批 ship**: PR2 同一 PR 内既加 per-call model 又删 `llm_mutate_alpha.py:220-260` swap hack —— 中间态(新 map 让更多 node 切 model + 旧 swap 仍在)比现状更危险。
3. **client LRU 改凭证不失效**: `invalidate_credentials_cache()`(llm_service.py:379)只翻标志不清 client 缓存 → 旧 key 仍被命中 → 401 → 误触 `LLM_API_CIRCUIT` 熔断。修: invalidate 时同步清 client LRU + TTL 兜底(类比 BRAIN session 每 8 轮重建);缓存 key 用 `(provider, base_url, sha256(api_key)[:16])` 不留明文。
4. **cost/metrics 落点漏**: `llm_service.py:720/723` 的 `_cost_record(model=self.model)` 改记 effective model,否则 G2 cost telemetry / `/ops/cost/telemetry` 把路由后调用全记成默认 model。
5. **熔断粒度**: `LLM_API_CIRCUIT` 是全局单例(llm_service.py:470),DeepSeek 抖动会 fast-fail 掉 anthropic/qwen 的 node。多 provider 下熔断应 per-(provider, endpoint)。
6. **运行时降级缺失**(用户点名): 单 provider 模型调用失败/限流 → `call()` 降级到 `self.model`(默认 provider)重试一次 + telemetry;百炼上 kimi 挂不应拖垮 deepseek node。进 §5 + 单测。
7. **node_key 事实修正**: §3 表 `r5_judge` **不存在**,真实 key 是 `r5_alignment_c1`/`r5_alignment_c2`(r5_judge.py:219/238);补声明 `strategy`/`failure_analysis`/`round_analysis`/`attribution`/`llm_crossover_alpha`/`g10_distill`(有 key,不进 map 即走默认);`macro_narrative_extract.py:183`、`hypothesis_health_service.py:706` **无 node_key**(天然不被路由,接受)。文件路径修正: `r1b_loop.py`→`backend/agents/graph/nodes/`,`r5_judge.py`→`backend/agents/graph/`。
8. **路径区分**(lens3)→ **2026-05-29 已被串行移除大幅简化**: FLAT 串行 `_run_one_round_inline` 已删,`_run_flat_iteration`=流水线唯一路径,hypothesis(dsv4-pro 76s)在流水线下可与 sim 重叠。仅剩 ONESHOT(低频离散,非流水线)若被 76s 拖累 → 经 PR5 task 级覆盖退快模型。**routing 不需"路径模式轴",`resolve_model_for` 的 `path_mode` 形参删除**(盲点 A)。
9. **灰度归因**(用户点名): 一次翻 map = 同时换多 node,无法归因。Phase C 改**单 node 单变量**(每次只改一个 node 的 model,其余留默认)→ PR5 task 级覆盖**提前为 Phase C 前置**。
10. **成本上限 $ 口径**: `MAX_TOKENS_PER_DAY` 是单一 token 阈值,多模型 token 不可比(kimi 2k vs dsv4-pro 21k 且单价不同)→ 转**成本($)口径**统一预算。
11. **rate-limit per-provider**: 百炼对 DeepSeek/Kimi/Qwen 是否共享 DashScope QPS、原厂端点独立配额 —— 退避/计数按 per-(provider,endpoint)。
12. **Pydantic dict 字段 env 畸形会构造崩**: `LLM_FUNCTION_MODEL_MAP` 别声明成 Pydantic dict 字段(畸形 env 让 `Settings()` 启动崩),仿 `_load_thinking_overrides`(config.py:53-56)用 module-level 容错 helper。
13. **resolve_model_for 返回 copy**: `_flag_override_cache["..."]` 是共享引用,返回前浅拷贝构造新 `{model,provider,...}`,防调用方 mutation 污染全局缓存。
14. **benchmark hedge 加强**: `r5_judge`(要判官一致性,benchmark 没测)、`self_correct` 取代 haiku(Claude 仅单 run + haiku 熔断不可用,缺反方数据)→ 标注**无 benchmark 支撑,Phase C 必测,不默认切**;dsv4-pro 多样性单 run 方差大(0.80~0.96)。
15. **工时 6d → 9-10d**: 见 §4(呼应 memory "低估 50%" 教训)。

---

## 0.5 Verification round 结论 (v2→v2.1) — GO-with-conditions

2-agent verification 亲读源码:**2 P0 + 核心 P1(#2/#3/#6/#7/#9/#12/#13)全闭环、代码核验成立**。P0-1 三点实证无隐患(`load_overrides_into_cache` 用 `.clear()+.update()` 原地 mutate → import 引用永远有效, feature_flag_service.py:1229-1230;worker `worker_process_init`→`start_sync_refresher` warm-cache, celery_app.py:41 + feature_flag_runtime.py:152;先例 feature_flag_service.py:1042 真实)。判定 **GO-with-conditions**,下列 6 条开工前/PR 内消化:

### 放行条件
1. **【开工前·必做·实证】benchmark 头牌落空对冲(盲点 D)**: `deepseek-v4-pro`/`kimi-k2.6`/`deepseek-v4-flash` **只在单个 `2026-05-22_x3_6m.json` 出现**,最新 05-29 run 不含,**当前 benchmark 脚本 MODELS 列表里没有**(可能已从 DashScope 下线)。→ 开工前先把这几个 id 加回脚本跑一次确认 DashScope 返 200;任一不可用,多样性档**当场退 `qwen3-coder-plus`/`kimi-k2.5`**(脚本现存、多 run 验证)。否则整个多样性优先档建在不存在的模型上。**工具**: `scripts/benchmark_llm_per_node.py --models deepseek-v4-pro,kimi-k2.6,...` 一次同时验证可用性 + 产出 per-node 选型(见下)。
2. **【PR1·已定】删 `path_mode` 形参(盲点 A·P1#8)**: 串行路线 2026-05-29 已移除,FLAT=流水线唯一路径,无串/并行之分 → `resolve_model_for(node_key, region=None)` 不要 `path_mode`。仅 ONESHOT(低频离散)若需差异化经 PR5 per-task map,不进 routing 签名。
3. **【PR2·必补】降级覆盖 anthropic 构造异常 + 目标 provider≠失败 provider(P0-2/P1#6)**: ① anthropic lazy 构造在 `ANTHROPIC_API_KEY` env 空时抛 RuntimeError(llm_service.py:304),且 anthropic key 经 `api_key_ref` 在 PR5 才接通 → PR2 的逐-entry 校验要查 **key 可达性**(不只 `provider∈{openai,anthropic}`),降级路径要兜住构造异常。② `@retry`(llm_service.py:428)因 `call()` 内 broad-except(:742 return 不 raise)**实际 inert,从不触发** → 降级重试需在 except 块内显式实现(不依赖也不冲突 @retry,plan 原"重试风暴"担忧不存在)。③ 熔断改 per-(provider,endpoint) 后,降级目标必须是**不同 provider**,否则熔断 open 时回同 provider 默认 model 空转。
4. **【PR3·必补】telemetry 全量记 effective model(盲点 C·P1#4 扩面)**: 不只 cost_record(真实在 llm_service.py:**719/766** 非 720/723),还需 `LLMResponse(model=)`(:735/780)、`_emit_metrics`(:709/756)、**metrics_tracker / experiment_tracker 的 trace step model 标注** —— 否则前端轨迹把所有 node 显示成默认 model。→ PR3 在 try 顶部算一个 `eff_model` 局部变量统一替换所有 `self.model` 读点。
5. **【PR2·补测 + 纠正并发模型】(盲点 B)**: §1.2 误述"feedback 与 producer 并发"——实测 feedback 是 `mark_primary_done()` 后的**串行 drain**(producer.py:66 `_drain_feedback`,handler 全 producer-side)。**真并发风险 = stage 2 多个 code-producer 共享 `get_llm_service()` 单例**。→ PR2 并发测试针对"多 code-producer 同 node_key 不污染 self.model"(per-call eff 不改 self 恰好覆盖);§6 同时点名补 effort 短路(#1)、cost effective-model(#4)、熔断粒度(#5)三项缺失测试。
6. **【排期·协调】与 serial_to_pipeline 迁移(盲点 E)**: **2026-05-29 串行移除已执行**(`_run_one_round_inline` 删,FLAT=流水线唯一)→ ① 串行 hypothesis 档已消解(§3 仅一行 hypothesis,自动适配);② **若迁移的 first soak(≥24h)仍在进行,路由 Phase C A/B 仍需错开窗口**,否则归因混淆(pipeline 自身是否稳定 vs 新 model 是否提升)。迁移留有过时清理项(`_run_flat_iteration` docstring 仍写"flag OFF byte-identical legacy",test 残留 `_run_one_round_inline` 引用)——非本 plan 范围。

### 工时再修
PR2 按 4d 计(lazy client+双 LRU+熔断 per-provider+降级+并发测试,BRAIN session thrash 同类历史反复翻车)→ **合计 11d**。`llm_mutate_alpha.py` 真实路径 `backend/agents/`(非 graph/nodes/)。

---

## 1. 真实现状 (代码实测 + review 亲验 2026-05-29)

### 1.1 `LLMService.call()` 无 per-call `model` 参数 ✅
`llm_service.py:433-443` 签名无 `model`/`provider`;openai 分支 `model=self.model`(:624),anthropic 分支 `"model": self.model`(:533)。

### 1.2 唯一真切换 = swap `self.model` hack,并发不安全 ✅
`llm_mutate_alpha.py:220-260` swap+`finally` 恢复。`llm_service` 是进程级单例(:872-881)。**热路径 hypothesis/code_gen 是单生产者串行(producer.py),swap 中毒不发生;但 `llm_mutate`/G5 crossover 在 consumer/feedback 路径可能与 producer 并发 → 真会让 producer 读到被改走的 `self.model`** → 故 P1#2 要求 PR2 一次性退役 swap。

### 1.3 `*_MODEL` 配置只估成本不切换 ✅ (v1 这条纠正正确)
`r1b_loop.py:155-157/431-433`、`r5_judge.py:123` 的 `R1B_*_MODEL`/`R5_JUDGE_MODEL` 仅作 `_estimate_cost`/日志的 fallback 名;`.call()` 不传 model,实跑永远是 `llm_service.model`。

### 1.4 现成可复用底子 (含 review 更正)
- **`node_key` 轴已铺好**(全集): `hypothesis`(generation.py:1095) · `code_gen`(:1661) · `self_correct`(validation.py:659) · `distill_context`(:249) · `r1b_retry`/`r1b_mutate`(r1b_loop.py:212/520) · `llm_mutate_alpha`(:239) · `llm_crossover_alpha` · `strategy` · `failure_analysis` · `round_analysis` · `attribution` · `r5_alignment_c1`/`c2` · `g10_distill`。**2 处无 node_key**: macro_narrative_extract.py:183 / hypothesis_health_service.py:706(不被路由,接受)。
- **`_resolve_effort(node_key,...)`(llm_service.py:382-404)** 是 node_key→参数的现成范式,`resolve_model_for` 镜像它。
- **多 provider 仅构造时支持**(:295-328 anthropic client lazy init) → **per-call 切 provider 需新构造 client**(P0-2)。
- **凭证走 DB** `CredentialsService`(:343-377)+ env fallback。
- **FeatureFlagOverride** `flag_type` 支持 json(models/config.py:101)+ 60s refresher + `/ops/flags/refresh-all`(worker_process_init 内生效, celery_app.py:41)+ `FeatureFlagAudit`。**但 `settings.X` 读 override 仅限 `ENABLE_` 前缀**(P0-1)→ map 直读 `_flag_override_cache`。
- **dormant**: `LLMProvider` 表(models/config.py:57-74)无人用,不碰。

---

## 2. 开工前必决 (blocking)

### #1 接入 = 阿里云百炼为主 + 原厂端点兜底 (已定)
百炼 OpenAI-compat 端点 `https://dashscope.aliyuncs.com/compatible-mode/v1`,集成部分第三方(DeepSeek/Kimi)+ 自家 Qwen;没有的走原厂。value schema `{model, provider, base_url?, api_key_ref?}` 天然支持: 百炼覆盖的省略后两项(走默认 `OPENAI_*`),没有的显式补端点+`api_key_ref`(`CredentialsService` 加 key)。

### #2 benchmark 代号 → 真实 model id + 接入途径对照表 (待填,可边跑边填)
有了前端编辑页,此表从"开工阻塞"降级为运维动作: 先上空映射(flag OFF),前端逐 node 试填、保存、观察。

| 选中模型 (代号) | 百炼 model id | 百炼有? | 否则原厂 |
|---|---|---|---|
| deepseek-v4-pro (hypothesis, pipeline) | ? | ? | DeepSeek 官方 |
| kimi-k2.6 (code_gen, hypothesis-serial) | ? | ? | Moonshot 官方 |
| qwen3-coder-plus (self_correct/r1b/distill) | ? | 大概率有 | — |
| deepseek-v4-flash (strategy/feedback) | ? | ? | DeepSeek 官方 |

### #3 per-model 成本价目表 ($口径)
`_estimate_cost` + cost/metrics(llm_service.py:720/723)+ `MAX_TOKENS_PER_DAY`→成本预算,全部按 effective model + 是否开 thinking 取价。

---

## 3. 模型分配 (per-node 实测, 2026-05-29)

> **数据来源**: `scripts/benchmark_llm_per_node.py` 实跑 9 模型,每 node 用其**真实 build_*_prompt + 代表性 fixture** + node 专属离线口径打分。**code_gen/self_correct/r1b_retry = runs=3 均值**(top-2 接近,复核),其余 = runs=1。结果 `docs/llm_per_node_benchmark_2026-05-29_FINAL.json`。**取代了 v2 基于端到端旧 benchmark 的推断表。**
> **9 模型全部在阿里云百炼可用、零 fail** → 放行条件 #1(头牌可用性)实证解除。
> **⚠️ runs=1→runs=3 推翻了 3 个 node 的最优**(dsv4-flash code_gen std 0.32 时好时坏,runs=1 抽到好的)→ 其余 runs=1 node 的 Phase C A/B 必须用真实 p_pass 复核。

| 功能块 (真实 node_key) | 类型 | runs | 🥇定稿 | score | lat | 选型依据 |
|---|---|---|---|---|---|---|
| **hypothesis** | 半结构 | 1 | **deepseek-v4-pro** | 0.90 | 76s | pillar_div 0.80 **唯一**领先 → 多样性优先实证支持;次优 kimi-k2.6(0.80/40s)备用 |
| **code_gen** | 产表达式 | **3** | **qwen3.6-plus** | 0.735±0.05 | 137s | **质量优先**(用户拍板);glm-5.1 0.63±0.002 极稳次之;dsv4-flash runs=1 的 0.60 是噪声(runs=3 仅 0.34±0.32) |
| **self_correct** | 产表达式 | **3** | **deepseek-v4-flash** | 0.435±0.0 | 12.5s | 最高+最稳(std0)+最快;runs=1 的 qwen3.7-max 被推翻 |
| **r1b_retry** | 产表达式 | **3** | **qwen3.6-flash** | 0.415±0.04 | 17s | runs=3 翻盘(runs=1 glm-5 降到第 4) |
| **llm_crossover_alpha** | 产表达式 | 1 | **kimi-k2.5** | 0.94 | 5.4s | dsv4-flash 0.96 仅高 1.6% 但慢 4.7× → tie-break 选快 |
| **llm_mutate_alpha** | 产表达式 | 1 | **kimi-k2.5** | 0.41 | 8s | kimi 双子座领先且最快最省 |
| **r1b_mutate** | 半结构 | 1 | **kimi-k2.6** | 1.0 | 9s | 9 模型并列满分 → tie-break 选最快 |
| **r5_alignment_c1** | 一致性 | 1 | **deepseek-v4-flash** | 1.0 | 3.4s | 7 并列满分 → 最快;⚠️ glm-5/5.1 仅 0.75(判错半数) |
| **r5_alignment_c2** | 一致性 | 1 | **kimi-k2.6** | 1.0 | 3.5s | 8 并列满分 → 最快 |
| **attribution** | 一致性 | 1 | **kimi-k2.5** | 1.0 | 2.7s | 7 并列满分 → 最快;⚠️ glm 系列 stability 偏低 |
| distill_context / strategy / failure_analysis / round_analysis / g10_distill | 未测 | — | 不进 map = 走默认 | — | — | 主观/未覆盖,显式走默认 |

### 实测结论(runs=3 修订)
1. **runs=1 不可靠**: 3 个 expr node 加跑 runs=3 后最优全变(dsv4-flash code_gen std 0.32)→ **runs=1 的其余 node 必须 Phase C A/B 实证复核,不可当定论**。
2. **deepseek-v4-flash 仍是 self_correct 王**(0.435 std0,稳),hypothesis 仍 dsv4-pro。
3. **kimi 系列 = 性价比王**: 结构-判断类(r1b_mutate/r5_c2/attribution/mutate/crossover)选 kimi(2-9s 最便宜,质量并列或近)。
4. **glm 不适合 judge**: r5_c1 correct 0.5、attribution stability 偏低 → judge 排除 glm。

### 多样性优先的延迟代价 (2026-05-29 串行移除后)
- **FLAT 串行路线已移除** (`_run_one_round_inline` 删,`_run_flat_iteration`→`run_flat_pipeline_session` 成唯一连续挖掘路径) → hypothesis 76s(dsv4-pro)在流水线下可与 consumer sim 重叠。但"被 sim 完全掩盖"**仅当 producer throughput ≥ consumer sim**;未量化前不当定论(若 producer 成瓶颈,退次优 kimi-k2.6 40s/0.80)。
- **ONESHOT**(一次性离散任务,`MiningAgent.run_evolution_loop`,仍非流水线)低频,若 76s 拖累可经 PR5 task 级覆盖退 kimi-k2.6 —— **不再需要 routing 的"路径模式轴"**(原 `path_mode` 形参删,见盲点 A)。

---

## 4. 接缝与 PR 拆分 (工时已按 review 上修)

### 配置载体: FeatureFlagOverride flag(map 直读 cache)
| flag_name | type | 读法 | 作用 |
|---|---|---|---|
| `ENABLE_PER_FUNCTION_LLM_ROUTING` | bool | `settings.X`(ENABLE_ 前缀,hook 命中 ✓) | 总开关,默认 OFF |
| `LLM_FUNCTION_MODEL_MAP` | json | **`_flag_override_cache.get(...)` 直读**(P0-1) | node_key→{model,provider,base_url?,api_key_ref?,thinking_effort?} |
| `LLM_AVAILABLE_MODELS` (可选) | json | 同上直读 | 前端下拉清单 |

### PR1 — 注册 flag + 路由解析器 (~1.5d)
- `feature_flag_service.SUPPORTED_FLAGS`: 加 FlagSpec,`group="LLM-Routing"`。
- `config.py`: 总开关声明为 `ENABLE_*` bool;map 默认值用 **module-level 容错 helper**(仿 `_load_thinking_overrides`,P1#12),**不**声明成 Pydantic dict 字段。
- `resolve_model_for(node_key, region=None) -> dict|None`(无 `path_mode` —— 串行已移除,见盲点 A): flag OFF / 无映射 → `None`(走默认,字节级不变);**直读 `_flag_override_cache`**;**逐 entry 校验**(非 dict / 缺 model / provider∉{openai,anthropic} → 该 node `None`,绝不抛异常);**返回浅拷贝**(P1#13)。

### PR2 — per-call model+provider+client 原子切换 + 退役 swap hack (~3d)
- `call(..., *, node_key, thinking_effort, model=None, provider=None)`: `eff = resolve_model_for(...) or {model:self.model, provider:self.provider}`,**请求构造用 eff 不改 self**。
- **model+provider+client 原子**(P0-2): per-call provider≠构造 provider 时 lazy 构造对应 SDK client;**client LRU 缓存** key=`(provider, base_url, sha256(api_key)[:16])`,openai/anthropic 分两套;缓存计算在 `await _ensure_credentials_loaded()` **之后**。
- **effort 短路**(P1#1): effective provider≠anthropic → 不传 thinking;entry 可显式 `thinking_effort:disabled`。
- **退役 swap hack**(P1#2,同 PR)。
- **运行时降级**(P1#6): eff-model 调用失败 → 降级 `self.model` 重试一次 + telemetry。
- **熔断 per-(provider,endpoint)**(P1#5)。
- 并发安全测试(两协程不同 node_key 不串)+ flag OFF 等价测试。

### PR3 — 死配置接通 + 成本 $口径 (~2d)
- `r1b_loop`/`r5_judge` `*_MODEL` 变真路由;`_estimate_cost` + `llm_service.py:720/723` cost/metrics 记 **effective model + thinking**(P1#4);`MAX_TOKENS_PER_DAY`→$预算(P1#10);`/ops/r1b/telemetry`、cost guard 校准。

### PR4 — 前端编辑页 (~2d, 核心需求)
- 复用 `FeatureFlagsConsole.jsx` 表格+审计模板(若 P0-1 选直读 cache,可降级为"现成 flag 编辑器 + model 下拉增强",省半工时)。
- 总开关 Switch + 主表(每行 node_key | 当前 model | Select | provider | base_url?)+ "保存映射"(PATCH json flag)+ "立即生效"(refresh-all)+ 审计 Timeline。
- **前端 schema 校验**(必含 model / provider 合法 / model∈AVAILABLE)= §5 坑7 第一道防线。
- `App.jsx` 加 `/ops/llm-routing`;**eslint 无配置 → `npm run build` 验证**。

### PR5 — task 级覆盖 (Phase C 前置, 非可选) (~1.5d)
- `task.config["llm_overrides"]`(JSONB,无 ORM 改) → 支撑 **Phase C 单 node 单变量归因**(P1#9)+ 串行/pipeline 路径区分(P1#8)+ region 覆盖。
- 阿里云没有的模型: `CredentialsService` 加 `CredentialKey`(MOONSHOT/ZHIPU),`api_key_ref` 引用。

**合计 ≈ PR1 1.5 + PR2 3 + PR3 2 + PR4 2 + PR5 1.5 = 10d**(+集成/A-B 脚本/回归另算),约为 v1 估算 1.6×。

---

## 5. 不能踩的坑

1. **配置载体读法**(P0-1): 非 `ENABLE_` flag 经 `settings.X` 读不到 override(hook 只拦 ENABLE_ 前缀);`REGIME_STAGE`/`QLIB_PRESCREEN_MODE` 是同源既存潜伏 bug,**别引为先例**。map 直读 `_flag_override_cache` + 真实 cache 集成测试。
2. **provider 原子切换**(P0-2): per-call 切 anthropic 而 client=None 会硬崩;model+provider+client 必须一起换。
3. **swap `self.model` 并发不安全** → per-call model,且 PR2 一次性删 swap(中间态更危险)。
4. **client 别每 call 新建 + 改凭证要清缓存**(BRAIN thrash 教训): invalidate_credentials_cache 同步清 client LRU + TTL 兜底;key 不留明文 api_key。
5. **flag OFF 字节级不变**: `resolve_model_for` 短路返 None;baseline 0 漂移 + mock 断言 model 不变;覆盖"flag ON 但 map 空/无此 key"也等价 OFF。
6. **成本口径**: cost/metrics(:720/723)+ `_estimate_cost` 记 effective model+thinking,否则 telemetry 全记默认 model;熔断 per-provider 否则一厂抖动 brown-out 全部。
7. **前端可编辑 = 畸形配置**: 两道防线 —— 前端 schema 校验 + 后端 `resolve_model_for` 逐 entry 校验,单 entry 非法只回退该 node、绝不炸 round;审计记被拒改动。畸形 json 进不了 cache(load_overrides try/except 吞),但**结构合法语义畸形**(漏 model/provider 拼错)只能靠 resolve 拦。
8. **运行时降级**: 单 provider 失败 → 回默认 model 重试一次(P1#6)。
9. **thinking 连带**: 路由到 opus 会开 thinking 流式(延迟/成本剧增),按 effective provider 短路 effort。
10. **benchmark 已 per-node 实测**(§3): runs=1,expr 类方差未测 → Phase C 对 top-2 接近项(self_correct/r1b_retry/code_gen)加 runs=3 复核;judge/结构类已满分+稳定可直接用。
11. **路径区分**(2026-05-29 串行移除后简化): FLAT=流水线唯一路径,hypothesis dsv4-pro 76s 与 sim 重叠;仅 ONESHOT 低频离散若被拖累才经 task 级覆盖退快模型。

---

## 6. 测试 / 验证

- **单测**: 路由 priority(直读 cache 命中)/ 逐 entry 畸形校验(非 dict/缺 model/provider 错/整体非 dict → 不抛异常、只回退对应 node)/ client 缓存命中+失效 / 并发(两协程不同 model 不串)/ flag OFF + flag ON 空 map 等价 / per-call 切 anthropic lazy client / 运行时降级。
- **集成(必须真跑 cache,不全 mock)**: 前端改 flag → refresh-all → `resolve_model_for` 读到新值(直击 P0-1);FLAT pipeline 端到端 hypothesis≠code_gen 用不同 model(mock LLM 按 model 分流)。
- **per-node 选型**: `scripts/benchmark_llm_per_node.py`(已建)—— 每个真实 node_key 用其**真实 build_*_prompt + 代表性 fixture** 跑候选模型,按 node 专属离线口径打分(产表达式→validator/p_pass/diversity;hypothesis/r1b_mutate→schema+pillar;r5/attribution→verdict 一致性+correctness),直接输出可填进 `LLM_FUNCTION_MODEL_MAP` 的推荐。10 node smoke 全过。
- **A/B**: 复用上脚本 + `rag_ab_report.py`,**单 node 单变量**(P1#9),对比 p_pass/成本($)/延迟。
- **回归**: `test_suite.py --regression` baseline 0 漂移。

---

## 7. 灰度路线

- **Phase A** (PR1-3, flag OFF): 配置+per-call+死配置+成本校准。全向后兼容。
- **Phase B** (PR4-5): 前端编辑页 + task 级覆盖(归因前置)+ 集成/端到端冒烟。worker 重启。
- **Phase C** (5-7d 观察): **单 node 单变量**翻 map,A/B 对比真实 p_pass 与成本;先验证热路径(code_gen=dsv4-flash / hypothesis=dsv4-pro,§3 实测)是否真提升线上转化,再推广其余 node。

---

## 附: 数据来源
`docs/llm_alpha_quality_benchmark_*.json`(2026-05-21~05-29)综合排名: 质量 top kimi-k2.6(0.271)>glm-4.7(0.268)>deepseek-v4-pro(0.266);多样性 top dsv4-pro 0.90(方差大),glm-4.7 垫底 0.42;最省+快 qwen3-coder-plus/kimi-k2.5(~2200 token/7s)。取多 run 均值;Claude 仅单 run,haiku 熔断不可用。
