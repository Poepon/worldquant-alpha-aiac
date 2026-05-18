# AIAC Master Implementation Plan — P3 整合路线图

> **文档日期**:2026-05-17
> **作者**:整合 4 份调研 / 设计 / 实测文档为统一作战图
> **整合范围**:
> 1. [`competitive_analysis_ai_alpha_mining_2026-05-17.md`](competitive_analysis_ai_alpha_mining_2026-05-17.md) — 13 个学界 / 工业系统对照,证 AIAC cascade 是孤例
> 2. [`phase15_task_schema_refactor_plan.md`](phase15_task_schema_refactor_plan.md) v2.1 — task schema 收敛 4 步 Alembic + flat search 完整设计(§13)+ R1a 启用细化(§14)
> 3. [`rd_agent_alpha_gpt_research_2026-05-16.md`](rd_agent_alpha_gpt_research_2026-05-16.md) — RD-Agent / Alpha-GPT / AlphaAgent / Hubble 调研,产出 13 项 R 路线图
> 4. [`qlib_alpha_research_2026-05-16.md`](qlib_alpha_research_2026-05-16.md) — Qlib + Alpha101/158/360 + 学术因子库调研,产出 10 项 Q 路线图
>
> **本文档定位**:
> - **战略路线图**(非战术手册)— 5 阶段 phase 划分、R/Q/phase15 任务总表(按 ROI 排序)、依赖关系图、时间线、KPI、决策记录
> - 源文档的 file:line / 代码块 / Alembic SQL / Eq 公式细节**不重复**,用 cross-reference 指引
> - 适合 1 周 review + kickoff;实施时回查对应源文档

---

## § 0 整合背景:为什么要写这一份

### 0.1 4 文档的关系(产出时间序)

```
2026-05-16  rd_agent_alpha_gpt_research.md       (架构方法学 + R1-R10)
2026-05-16  qlib_alpha_research.md               (seed 内容 + Q1-Q10)
2026-05-17  competitive_analysis.md              (13 系统对照 → flat 论据)
2026-05-17  phase15_task_schema_refactor v2.1    (schema 收敛 + R1a 细化 + flat 设计)
                  ↓ (本文整合)
2026-05-17  master_implementation_plan.md        ← 当前文档
```

### 0.2 整合冲突解决

| 冲突点 | 解决 |
|---|---|
| R3(AlphaAgent AST polynomial subtree isomorphism)vs Q8(`diversity_tracker` 加 AST distance)| **合并为 R3/Q8**(同一件事,不同视角 — R3 给数学公式,Q8 给落地文件) |
| R2(RD-Agent §2.5 Direction-level Contextual Thompson Sampling)vs Q7(bandit-arm 推广到 hypothesis 方向维度)| **合并为 R2/Q7**(完全等价) |
| R4(`ENABLE_NEGATIVE_KNOWLEDGE_NUDGE`)| **已 done**(memory:[[project_aiac_flags_on_2026_05_16]]),从路线图剔除 |
| R1c(deprecate core/ 移到 vendor/)| **phase15 v2 决策走 R1a/R1b,R1c 作为 NO-GO 备用**(rd_agent §6 R1c 列了 1 人日工程量),本路线图按 a+b 推进 |
| R1a 工程量 2 人日 vs phase15 v2 §14 详细到 file:line | 本文档列 2 人日 + cross-ref §14 |
| Phase 划分 — rd_agent doc 用 Phase 0/1/2/3,phase15 v2 用 Phase 0/1/1.5/2/3,qlib doc 用 Phase 1/2/3 | **统一为 5 Phase**:Phase 0(快赢)/ Phase 1(高 ROI)/ Phase 1.5(schema)/ Phase 2(学术验证)/ Phase 3(flat + 主循环改造) |
| phase15 v1 §0 闸门"R1a 已 ship ≥ 2 周"已被 phase15 v2 修订为"Phase 0 R1a 启用 + 2 周观察期" | 本文档以 v2 为准;v1 闸门状态不影响本路线图实施(避免读者困惑该看 v1 还是 v2)|

### 0.3 决策锚点(不可动)

- **走 R1a/R1b 路径**,R1c 作为 NO-GO 备用 — `agents/core/` 3223 行沉没成本转资产
- **保留 cascade 作为 legacy**,新 task 默认走 flat(Phase 3 切换,有 2-4 周双轨灰度)
- **不引入 Qlib RL / Alpha360 / Llama3 70B / 重写 LangGraph**(详 qlib doc § 6 + rd_agent doc § 7)
- **学术 SOTA + 工业 human review gate 并存**(P3 ops console 已 ship,Phase 3 flat 切换不去掉 PAUSE/STOP)
- **能力分类不变**:数据一致性能力(Sharpe / testPeriod)走 task 启动快照;endpoint 选择能力(multi-sim / PROD-corr)走全局 flag(CLAUDE.md 已固化)
- **本文档为新建 master plan**,4 源文档(competitive_analysis / phase15 v2.1 / rd_agent_research / qlib_research)保留作为细节参考

---

## § 1 战略方向

### 1.1 一句话总结

**AIAC 当前的 cascade T1/T2/T3 是学界 / 工业 0 先例的孤例;学术 SOTA(RD-Agent / AlphaAgent / Hubble / QuantaAlpha)已全部收敛到 "flat 生成 + hypothesis 驱动 + bandit/GP 调度";AIAC `agents/core/` 已有 3223 行 RD-Agent 兼容代码 DORMANT,激活成本 < 从零写 5×。本路线图的终态是把 AIAC 切到 flat + hypothesis-driven 主循环,沿途用 R/Q 系列改进点(共 18 项)做铺垫与价值释放。**

### 1.2 三条硬证据(反 cascade)

1. **学界**:13 个对照系统(RD-Agent / Alpha-GPT / AlphaAgent / Hubble v2 / QuantaAlpha / Chain-of-Alpha / AlphaEvolve / AlphaGen / Navigate Alpha Jungle / AlphaSAGE / Alpha-R1 / Increase Alpha / Citadel-Renaissance-Two Sigma),**0 个用 tier phase 切换**。Chain-of-Alpha 两阶段流水线已撤稿,AIAC 三阶段比它更激进。详 competitive_analysis §3 + phase15 v2 §12。
2. **数据**:RD-Agent-Quant **22-26 因子 14.21% ARR** vs Alpha158 158 因子 5.70% ARR(2.5× ARR,因子精简 86%)— 验证 flat + hypothesis 驱动 + 精简优于分层 + 暴力穷举。详 rd_agent_research § 2.4(注 caveat:数字来自 paper body Table,非 abstract)。
3. **AIAC 自家实测**:task 652 cascade resume 2026-05-16 12:20-14:12 UTC 跑 1h52m,7/13 derived alpha 来自 2 parent 同源失败,parent 7820 的 5 个 group_* wrapper **100% LOW_SUB_UNIVERSE_SHARPE FAIL**。5 个 BRAIN sim 浪费在用 group_* 救一个结构性死掉的 base signal。详 phase15 v2 §11.2。

### 1.3 工业 reality check(为什么不全 LLM-only)

- Citadel CTO 明确反对 PM 外包判断,LLM 仅做 research assistant
- JPMorgan LLM Suite 覆盖 20 万员工,**未公开**用于 alpha generation
- Renaissance / Two Sigma 零公开披露

**对 AIAC 的意义**:走学界 SOTA(flat + hypothesis-driven),但保留 P3 ops console 的 human review gate(已 ship),既追前沿又有工业一致的安全网。Phase 3 flat 切换**不去掉** `task.status PAUSED/STOPPED` 检查,只去 `_run_cascade_phase` 内部的机械软停。

### 1.4 AIAC `agents/core/` DORMANT 现状的战略含义

`backend/agents/core/` = **9 模块 3223 行**(`__init__` 149 + `experiment` 239 + `feedback` 214 + `trace` 359 + `knowledge` 327 + `scenario` 308 + `pipeline` 702 + `evolving_rag` 442 + `integration` 483)+ `ARCHITECTURE.md` 18 章节双语设计。

```
$ grep -rn "from backend.agents.core" backend/agents/graph/ \
    backend/agents/mining_agent.py backend/tasks/ backend/services/ \
    backend/routers/ backend/celery_app.py
# 0 matches
```

**production 路径零调用**,但**不是 dead code**(`integration.py:279-288` 明示 Plan v5+ §C-Phase 3 main-loop inversion entry point,DORMANT)。

**含义**:学界 SOTA 路径其实在自家代码库里。R1a(`enhance_existing_node_evaluate` shim)2 人日就能开始捕获 `AttributionType` 数据;R1b(全 Pipeline 激活 + flat 切换)Q3 2026 4-6 周改造。**沉没成本变资产**是本路线图的最大杠杆。

---

## § 2 已完成事项(Phase 0 partial done)

整合 4 文档的"已落地"信息:

| 项 | 完成时间 | 来源 | 验证 |
|---|---|---|---|
| **Bug B fix**(T1 sign-flip retry 路由经 `_evaluate_single_alpha`)| 2026-05-16,commit `a425937` | phase15 v2 §11.1 | 111 evaluate 测试 PASS + 生产 task 1330/652 13 alpha 100% 命中 `_regime_at_eval` stamp |
| **`ENABLE_NEGATIVE_KNOWLEDGE_NUDGE=True` flip** | 2026-05-16 | memory:[[project_aiac_flags_on_2026_05_16]] | typed path 全启用,`HYPOTHESIS_CENTRIC_LEVEL=2` |
| **9 P0/P1/P2 flag override ON** | 2026-05-16 | memory:[[project_aiac_flags_on_2026_05_16]] | DB FeatureFlagOverride |
| **P3 ops dashboard ship**(9 页 / 28 endpoint / 143 测试)| 2026-05-16 之前 | memory:[[reference_ops_dashboard_p3]] | 鉴权 X-Ops-Token、双源 OpsReportReader、Settings.__getattribute__ flag hook |
| **`HYPOTHESIS_CENTRIC_LEVEL=2`**(typed path 全启用)| 2026-05-16 | memory | mining_agent P2-C 注入实测跑了(task 1325 LLM 含 "balanced regime" 等字眼) |
| **P2-D negative_knowledge active injection**(`prompts/hypothesis.py` 668-680)| 2026-05-15 commit `6cae5f5` | rd_agent_research § 4 R4 已 done | `nudge_lines` 计数 trace |
| **P2-B Five Pillars 分类**(`pillar_classifier.py`)| 已 ship | rd_agent_research § 8.3 / qlib_research § 2.4 | 分类有数据 |

**Phase 0 剩余**:R1a 启用(2 人日)+ 2 周观察期 — 见 § 4.1。

---

## § 3 待做事项总表(R + Q + phase15 三系列整合)

### 3.1 任务总表(按 ROI 排序,已剔除已完成项 + 主线之外的备用项,含 R1c NO-GO 备用)

| ID | 任务 | 来源 | 工程量 | 价值 | Phase | 依赖 |
|---|---|---|---|---|---|---|
| **R1a** | 启用 `enhance_existing_node_evaluate()` shim — 捕获 `AttributionType` | rd_agent §6 + phase15 v2 §14 | 2 人日 | ★★★★★ 解 phase15 GO 闸门 dead-lock | **Phase 0** | Bug B fix(done) |
| **Q1** | Kakushadze 101 Alphas 完整移植(5→106 条)| qlib §4.3 | 1 人日 | ★★★★★ KB seed 21× 扩张 | **Phase 0** | 无 |
| **Q3** | Alpha158 表达式 × 5 窗口 ≈ 150 条 seed + 写 Qlib→BRAIN 25 算子映射器 | qlib §4.2 | 2-3 人日 | ★★★★★ ML feature seed 库 | **Phase 0** | 无 |
| **Q2** | Open Source Asset Pricing 319 predictor 一次性 import | qlib §4.4 | 2 人日 | ★★★★★ 学术 predictor 库 | **Phase 1** | Q1 完成(import 机制复用) |
| **R2/Q7** | Direction-level Contextual Thompson Sampling(arms = `{genetic_mutation, llm_generation, rag_template, knowledge_pattern}`) | rd_agent §6 R2 + qlib §4.6 | **3-4 人日** + ROI 论证(5 维 reward 横跨 column / BRAIN API / 实时计算字段三类来源,fetch + 计算路径单算 1-2 人日)| ★★★★★ 替换 cascade 机械切换 | **Phase 1** | R1a 数据(AttributionType 分布反证 arm 集) |
| **R3/Q8** | AST polynomial subtree isomorphism(Shamir-Tsur 1999, O(n²·⁵/log n))— `diversity_tracker.py` 加第 6 维 AST distance | rd_agent §6 R3 + qlib §4.7 | 3-5 人日 | ★★★★★ 反 alpha decay 原创度门 | **Phase 1** | 无(`knowledge_extraction.extract_operator_tree` 已就绪)|
| **R4'** | Dual-channel RAG 分通道渲染(Hubble v2)— `prompts/hypothesis.py` positive vs negative 视觉区分 | rd_agent §6 R4' | 1-2 人日 | ★★★★ 已有 P2-D nudge,补结构化 | **Phase 1** | R4 done(已 flip ON) |
| **Q6** | Alpha191 选 30-50 条 A 股因子作 region=CHN seed | qlib §4.5 | 1-2 人日 | ★★★ 跨 region 补充 | **Phase 1** | Q1 完成(import 机制复用) |
| **Q4** | `pillar_classifier` 加 Qlib operator alias(Mean/Std/Rank 等) | qlib §5 P3-Q4 | 0.5 人日 | ★★★ 支持 Qlib operator 命名 | **Phase 1** | Q3(算子映射器)|
| **Q5** | Five Pillars 加 `theoretical_anchor`(FF5 / q5 / BAB 显式映射)| qlib §5 P3-Q5 | 1 人日 | ★★★ pillar 与学术挂钩 | **Phase 1** | Q4 |
| **phase15-A** | Alembic Revision A:加列(`schedule` / `starting_tier` / `generation_strategy` / `runtime_state` JSONB)| phase15 v2 §3 | 2 人日 | 零风险,为 R6 / R1b 铺路 | **Phase 1.5** | R1a 数据(反证 `generation_strategy` arm 集) |
| **phase15-B** | Alembic Revision B:回填 + 双写代码部署 | phase15 v2 §3 | 3 人日 | 数据迁移 | **Phase 1.5** | phase15-A |
| **phase15-C** | Alembic Revision C:切读(高风险窗口,`ENABLE_TASK_SCHEMA_V2` flag 灰度)+ 前端展示字段适配 | phase15 v2 §3 + §4 | **3 人日 backend + 1 人日 frontend** | 路径切换(后端读路径 + 前端 Dashboard/TaskDetail 显示 cascade_phase/agent_mode/mining_mode 三处)| **Phase 1.5** | phase15-B |
| **phase15-Schema** | Pydantic `TaskConfig` — `task.config` JSONB → Pydantic strict schema(拒未知键)| phase15 v2 §3.1 + §7 | 2 人日(与 A/B 并行)| ★★★ 类型安全 | **Phase 1.5** | phase15-A |
| **phase15-Fields** | 三字段合并简化 — `mining_mode` + `agent_mode` + `cascade_phase` → `schedule` + `starting_tier`(死枚举 INTERACTIVE grep=0 后删)| phase15 v2 §3.2 + §7 | 2 人日(与 A/B 并行)| ★★★ 信息论冗余消除 | **Phase 1.5** | phase15-A |
| **R5** | Hypothesis-Alignment 双向 LLM judge(AlphaAgent Eq. 7,c₁/c₂)| rd_agent §6 R5 | 2 人日 | ★★★★ thesis ↔ expression 一致性校验 | **Phase 2** | R1a 数据(attribution 失败聚集 → judge 优先级) |
| **R6** | Trace `current_selection` + DAG 多分支(v0.8.0 MCTS)| rd_agent §6 R6 | 3 人日 | ★★★★ 替换"tier 推进线性"为 DAG | **Phase 2** | phase15-C(`runtime_state["dag"]` 字段就绪) |
| **R7** | Co-STEER `should_use_new_evo` 半接受机制(防覆盖好样本)| rd_agent §6 R7 | 1 人日 | ★★★ self_correct 节点改进 | **Phase 2** | 无 |
| **R10** | Family-cap top-k=2(Hubble v2)— `pillar_classifier` 加 hard cap | rd_agent §6 R10 | 1 人日 | ★★★ 防一族刷榜 | **Phase 2** | R3/Q8(AST distance 配合)|
| **Q9** | McLean-Pontiff Decayed Alpha 表 → `negative_knowledge.py`(50+ Decayed Alpha seed) | qlib §4.8 | 1-2 人日 | ★★★ Replication Crisis 反例库 | **Phase 2** | R4' |
| **flat-F1** | 新建 `_run_flat_iteration` + `mining_mode="FLAT_CONTINUOUS"` 路径(双轨)| phase15 v2 §13.10 | 2-3 人日 | ★★★★★ 切走 cascade 机械软停 | **Phase 3** | R1a / R2 / R3 / phase15-C 全部 ship |
| **flat-F2** | `start_session` 默认创建 FLAT mode + Alembic comment 更新 | phase15 v2 §13.10 | 0.5 人日 | 切默认 | **Phase 3** | flat-F1 + 2 周灰度 PASS |
| **flat-F3** | T2 wrapper sweep 替换为 `llm_mutate_alpha`(LLM 看 failed_tests 选 2-3 个 wrapper)| phase15 v2 §13.7 | 1-2 人日 | ★★★★ 消除盲目穷举 | **Phase 3** | flat-F2 |
| **flat-F4** | 删 cascade legacy 代码 + `CASCADE_T*_ROUNDS` settings + 测试套迁移 | phase15 v2 §13.10 | 1 人日 | 清理 | **Phase 3** | flat-F3 + 4 周稳定期 |
| **R1b** | 全 Pipeline 激活(`hypothesis_centric_variant=3` 路由)| rd_agent §6 R1b | 4-6 周大改 | ★★★★★ 学界 SOTA 终态 | **Phase 3** | flat-F1 ship(主循环改造前置) |
| **phase15-D** | Alembic Revision D:删旧列(`mining_mode` / `cascade_phase` / `cascade_round_idx` / `progress_current` / `current_iteration` / `last_alpha_persisted_at` 6 列)| phase15 v2 §3 | 1 人日 | 清理 | **Phase 3** | flat-F4 + 稳定 4 周 |
| **R8** | 4 层 Hierarchical RAG(Alpha-GPT v1.0 v2 修订)— `rag_service.py` 重构 | rd_agent §6 R8 | 5-8 人日 | ★★★ RAG 检索质量提升 | **Phase 3** | Q1 + Q2 + Q3 完成(seed 充足才值得分层)|
| **R9** | Workspace checkpoint(`simulation_cache` 表 — `cached_run()` 思路)| rd_agent §6 R9 | 3 人日 | ★★ 重复 sim 缓存(BRAIN cost 降低)| **Phase 3** | 无 |
| **Q10** | `pyqlib` pre-screen as multi-fidelity 新层(BRAIN 前的免费筛)| qlib §5 P3-Q10 | 5 人日 | ★★ 多保真新层 | **Phase 3** | 无(独立)|

**统计**:**29 项任务,Phase 0 = 3 项 / Phase 1 = 7 项 / Phase 1.5 = 5 项 / Phase 2 = 5 项 / Phase 3 = 9 项**。

### 3.2 工程量与 Phase 总结

| Phase | 工程量 | 日历周 | 关键产出 |
|---|---|---|---|
| **Phase 0**(立即可做)| **5-6 人日** | 2-3 周(R1a 14 天观察期 dominant) | R1a hook + Q1 + Q3(KB seed 5→256 条)|
| **Phase 1**(高 ROI 快赢)| **12-17 人日**(R2/Q7 上调 3-4 人日后)| 2-3 周 | R2/Q7 bandit + R3/Q8 AST + R4' dual-channel + Q2/Q4/Q5/Q6(KB seed 256→570+) |
| **Phase 1.5**(schema 收敛)| **9 人日 串行**(A 2 + B 3 + C 3 + frontend 1)**+ 4 人日并行**(Schema 2 + Fields 2,与 A/B 同步进行 → 串行人日不增,但 PR 数 +2)| 4 周(含 2 周灰度) | Alembic A→B→C + Pydantic TaskConfig + 三字段合并,新 schema 上线 |
| **Phase 2**(学术验证模式)| **8-10 人日** | 2 周 | R5 + R6 + R7 + R10 + Q9 |
| **Phase 3**(flat + 主循环)| **~12 人日 flat + R1b 4-6 周 (~20-30 人日) + 8-16 人日 R/Q 优化** | ~12 周(8-11 → 10-27,含 flat 灰度 2 周 + R1b 6 周 + 稳定 4 周)| flat-F1→F4 + R1b + R8 + R9 + Q10 + phase15-D |
| **总计** | **~73-98 人日**(Phase 0+1+1.5+2+3 加总,含 R1b 20-30)| **Q2-Q3 2026 ~23 周(2026-05-18 ~ 10-27,162 天)** | flat + hypothesis-driven 终态 |

---

## § 4 5 阶段实施路线图

### 4.1 Phase 0 — 立即可做(1-2 周,5-6 人日)

**目标**:解 phase15 GO 闸门 dead-lock + KB seed 数量级扩张为 Phase 1 LLM/RAG 提供更丰富材料。

**任务**:
- **R1a 启用**(2 人日 + 2 周观察期)
  - **详细方案**:phase15 v2 §14(file:line 级接入代码、`enhance_existing_node_evaluate` 真实 signature、`alpha.metrics["_r1a_attribution"]` 持久化、回滚 flag `ENABLE_R1A_HOOK`)
  - ⚠️ **caveat**:phase15 v2 §14.3 写的接入位置 `evaluation.py:2554` 越界(2026-05-17 实测文件 2542 行),真实位置应为 `:2538`(`return {"pending_alphas": updated_alphas, **trace_update}` 前)。实施 PR 前用 `grep -n "return {\"pending_alphas\"" backend/agents/graph/nodes/evaluation.py` 重新 verify(文件还在动)
  - **KPI**:hook 触发 **≥ 50**(数据驱动门槛 — task 652 实测 7 alpha/h × cascade 软停 8-10 次/周 resume 推算)/ metrics 非 NULL ≥ 95% / non-`unknown` attribution ≥ 70% / hook failure < 10 / 0 production crash
  - **数据消费**:`SELECT (metrics->>'_r1a_attribution'), COUNT(*) FROM alphas WHERE created_at > now() - interval '14 day' GROUP BY 1` 反证 attribution 分布
- **Q1 Kakushadze 101 移植**(1 人日)
  - 源:[`yli188/WorldQuant_alpha101_code`](https://github.com/yli188/WorldQuant_alpha101_code)(BRAIN fastexpr 风,**不需翻译**)
  - 落地:`backend/external_knowledge.py:ACADEMIC_PATTERNS` 5→106
- **Q3 Alpha158 + Qlib→BRAIN 25 算子映射器**(2-3 人日)
  - 写 `backend/qlib_translator.py` 新 module(25 行算子表 + 50 行自动展开器)
  - **关键陷阱**:`Ref(x, -N)` 符号反转 / Qlib `Rank` 是**时序** percentile / `$close` 前缀剥离(详 qlib_research § 4.1)
  - 落地:`ACADEMIC_PATTERNS` 106 → 256 条

**GO 闸门**(Phase 0 → Phase 1):
- ✅ R1a hook 触发 **≥ 50** 次,AttributionType 分布数据可查询
- ✅ `ACADEMIC_PATTERNS` Python list **长度** ≥ 256 条 seed(Kakushadze 101 + Alpha158 150 条 + 原 inline 5 条)— 注意区分:这是 `external_knowledge.py:503` 的 list 长度,不等于 `KnowledgeEntry` DB row 数(import 后去重)、也不等于 `baseline.json:kb_total_entries`(当前 59,扩张后预计 ~280-310)
- ✅ R1a 无 production crash,`baseline.json` 更新通过 `--save-baseline`(`kb_total_entries` metric 自然跟随上升,以 import 后 SQL `SELECT COUNT(*) FROM knowledge_entries` 实测为准)

**风险**:
- R1a hook 在 evaluate node 末尾接入,失败 → `try/except` 守护 + `ENABLE_R1A_HOOK` flag flip OFF(< 1 分钟)
- Q1/Q3 import 失败 → 不影响生产路径,只是 KB 没扩张;rollback = revert commit

### 4.2 Phase 1 — 高 ROI 快赢(2-3 周,12-17 人日)

**目标**:把 R1a 收集到的 AttributionType 数据 + Phase 0 扩张的 KB seed 转换为算法层改进 — bandit 调度、AST 原创度门、dual-channel RAG。

**任务**:
- **R2/Q7 Direction-level Contextual Thompson Sampling**(**3-4 人日** + ROI 论证)
  - arms 选择:由 R1a 2 周数据驱动 — 若 attribution 主要是 `hypothesis` → arms ≈ `{rag_template, knowledge_pattern, llm_generation, genetic_mutation}`;若主要是 `implementation` → arms ≈ `{llm_generation, llm_mutate, self_correct, genetic_mutation}`
  - **关键 caveat**(rd_agent §6 R2 修正):arms 是 AIAC 自定义(生成策略级),不是 RD-Agent 原版 task-direction arms;5 维 reward 横跨**三类来源** — `(is_sharpe, is_fitness, -is_turnover, -self_corr, composite_score)`,其中 `self_corr` 不是 Alpha column 而是 `CorrelationService.get_with_fallback` 实时 BRAIN API,`composite_score` 是 `EvalResult` dataclass 实时计算字段;实现需额外 fetch + 计算路径(单算 1-2 人日,故工程量上调到 3-4 人日)
  - 落地:`backend/agents/evolution_strategy.py` 新加 `DirectionBandit` class(借鉴 `backend/selection_strategy.py:59` 既有 `DatasetBandit` class,UCB1 公式在 `:135`)
- **R3/Q8 AST polynomial subtree isomorphism**(3-5 人日)
  - 算法:Shamir-Tsur 1999 `O(n²·⁵/log n)`(AIAC alpha AST n < 20,brute-force O(n²) 也跑得动 — 见 caveat)
  - **caveat**(rd_agent §4.1 + §6 R3):Shamir-Tsur 复杂度上界本次未独立 verify(DBLP socket 错误),实施前需交叉确认 DOI + 复杂度针对 rooted-ordered tree subtree isomorphism;若失实 → 降级 O(n²) brute-force(可接受,3 人日)
  - 落地:`backend/diversity_tracker.py` 加第 6 维 AST distance;`backend/knowledge_extraction.expression_to_skeleton` 扩为 distance metric
- **R4' Dual-channel RAG 分通道渲染**(1-2 人日)
  - `backend/agents/prompts/hypothesis.py` positive(SUCCESS_PATTERN)和 negative(FAILURE_PITFALL)视觉分离 — 不同 prompt 段、不同标题、不同优先级
  - 落地:Hubble v2 §4.3 dual-channel 设计,AIAC 已有 P2-D nudge active(commit `6cae5f5`),补"分通道"结构化
- **Q2 Open Source Asset Pricing 319 predictor**(2 人日)
  - 源:[`openassetpricing`](https://www.openassetpricing.com/) Python 包(2025-10 重写,数据到 2023)
  - 落地:复用 Q1 import 机制,`ACADEMIC_PATTERNS` 256 → 570+
- **Q6 Alpha191 选 30-50 条 A 股 region=CHN seed**(1-2 人日)
  - 源:[`JoinQuant/jqdatasdk/alpha191.py`](https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/alpha191.py)
  - 落地:`ACADEMIC_PATTERNS` 加 `region="CHN"` + `horizon="short"` 标注
- **Q4 pillar_classifier 加 Qlib operator alias**(0.5 人日)
- **Q5 Five Pillars 加 `theoretical_anchor`**(1 人日)

**GO 闸门**(Phase 1 → Phase 1.5):
- ✅ R2/Q7 bandit 在生产 task 跑 ≥ 1 周,arm reward 收敛(任一 arm 累积 select ≥ 30 次)
- ✅ R3/Q8 AST distance 在 `diversity_tracker.fingerprint` 第 6 维 active,无 false positive bursts(同一 task 内 distance 分布有方差)
- ✅ R4' dual-channel 在 `prompts/hypothesis.py` 渲染,positive/negative trace 可区分
- ✅ `ACADEMIC_PATTERNS` ≥ 570 条 seed,baseline 更新

**风险**:
- R2/Q7 arm 集错配 → 反 cascade 切换 → flag `ENABLE_DIRECTION_BANDIT=False` 回到当前 cascade(< 1 分钟)
- R3/Q8 AST distance 拒过严 → diversity_tracker 阻塞新 alpha → flag flip OFF 第 6 维(< 5 分钟)
- Q2 import 量大 → schema/duplicate 检测要做 — `external_knowledge.py` 已有 `ExternalKnowledgeSyncer.import_curated_patterns()` dedupe 路径

### 4.3 Phase 1.5 — Schema 收敛(4 周,9 人日串行 + 4 人日并行)

**目标**:`MiningTask` schema 收敛到 `agents/core/` 既有结构,为 Phase 2 R6 / Phase 3 R1b 铺路。

**任务**(详 phase15 v2 §3):
- **phase15-A Revision A 加列**(2 人日 + 测试 fixture 修)
  - `mining_tasks.schedule` (String, ONESHOT/CASCADE)
  - `mining_tasks.starting_tier` (Integer, 1/2/3)
  - `mining_tasks.generation_strategy` (JSONB, R2/Q7 arm 集)
  - `experiment_runs.runtime_state` (JSONB, 含 current_tier/round_idx/progress/iteration/last_persisted_at/dag)
- **phase15-B Revision B 回填 + 双写**(3 人日)
  - 回填规则:`schedule = 'CASCADE' if mining_mode='CONTINUOUS_CASCADE' else 'ONESHOT'`,`starting_tier = 1 if CASCADE else AGENT_MODE_TO_TIER[agent_mode]`,`current_tier = {T1:1, T2:2, T3:3}[cascade_phase]`
  - INTERACTIVE 任务审查(预期 0 行,phase15 v2 §10 待 SQL 二次确认)
  - TaskService.create_task 双写新旧列
- **phase15-C Revision C 切读 + 灰度**(3 人日)
  - `ENABLE_TASK_SCHEMA_V2` flag override,先 staging → 单 task → region 全量
  - 影响:`mining_tasks.py:1251-1264` cascade worker 重启路径、`session_watchdog.py` liveness 检测、router 响应、ops dashboard
- **Pydantic TaskConfig**(2 人日,与 A/B 并行)— `task.config` JSONB → Pydantic strict schema
- **三字段合并简化**(2 人日,与 A/B 并行)— `mining_mode` + `agent_mode` + `cascade_phase` → `schedule` + `starting_tier`

**GO 闸门**(Phase 1.5 → Phase 2):
- ✅ Revision C ship + 灰度 region 全量 ≥ 2 周稳定(无 cascade 重启 bug、watchdog 正常触发、ops dashboard 进度数字一致)
- ✅ `baseline.json` 无 alpha 指标变化(Phase 1.5 应零 alpha 行为影响)
- ✅ `runtime_state["dag"]` 字段存在,Phase 2 R6 可直接写 DAG

**风险**:
- Revision C 切读发现 bug → `ENABLE_TASK_SCHEMA_V2=False` flip 回旧列(< 1 分钟,代码保 fallback)
- Revision B 数据漂移 → `alembic downgrade -2` + 代码 revert(< 30 分钟)
- Revision D **不在 Phase 1.5 做** — 推到 Phase 3 末与 flat-F4 合并(单点不可回滚)

### 4.4 Phase 2 — 学术验证模式(2 周,8-10 人日)

**目标**:把 R/Q 学术派别的高 ROI 模式落地(LLM judge / DAG 多分支 / 半接受 / family-cap / 反例 KB)。

**任务**:
- **R5 Hypothesis-Alignment 双向 LLM judge**(2 人日)
  - AlphaAgent Eq. 7:`C(h, d, f) = α·c₁(h, d) + (1-α)·c₂(d, f), α=0.5`
  - `c₁(h, d)`:LLM 校验 hypothesis ↔ description;`c₂(d, f)`:LLM 校验 description ↔ expression
  - 落地:`backend/agents/feedback_agent.py` 加 judge 节点,失败 → trace 标 `attribution=AttributionType.hypothesis`
  - **依赖**:R1a 数据反证 — 若 attribution 主要是 `implementation` → R5 优先级降低;若 `hypothesis` 多 → R5 高 ROI
- **R6 Trace `current_selection` + DAG 多分支**(3 人日)
  - 来源:RD-Agent v0.8.0 MCTS policy
  - 落地:`backend/agents/core/trace.py` 扩 `current_selection` + `idx2loop_id` 字段;激活 `runtime_state["dag"]` 写入路径
  - **依赖**:phase15-C 完成(`runtime_state` JSONB 字段已就绪)
- **R7 Co-STEER `should_use_new_evo` 半接受机制**(1 人日)
  - 防 self_correct 覆盖好样本 — 比较 feedback,只在新版本 score 严格高时覆盖
  - 落地:`backend/agents/graph/nodes/self_correct.py` 加 feedback 比较
- **R10 Family-cap top-k=2**(1 人日)
  - 来源:Hubble v2 Table 1 — 同 pillar 同 family 只保留 top-2
  - 落地:`backend/pillar_classifier.py` 加 hard cap(`PILLAR_FAMILY_TOP_K=2` setting)
  - **依赖**:R3/Q8(AST distance 配合 — family 定义可基于 AST skeleton 聚类)
- **Q9 Replication Crisis Decayed Alpha → negative_knowledge**(1-2 人日)
  - 来源:McLean-Pontiff JoF 2016 -26% / -58% post-pub 衰减 + Hou-Xue-Zhang 64% anomaly 不显著 + Harvey-Liu-Zhu t-stat 阈值 3.0
  - 落地:`backend/negative_knowledge.py` 新加 50+ Decayed Alpha seed,每条带 `decay_pct` + `failure_mode` + `theoretical_anchor`

**GO 闸门**(Phase 2 → Phase 3):
- ✅ R5 LLM judge 在 ≥ 100 alpha 跑过,attribution 分布有变化(`hypothesis` vs `implementation` 比例移动 ≥ 10%)
- ✅ R6 DAG 在生产 task 写入 ≥ 5 分支(`runtime_state["dag"]` 有 branch 结构)
- ✅ R10 family-cap 限制了 ≥ 5 个 alpha(metrics_tracker 计数)
- ✅ Q9 negative_knowledge 总 entry 数 ≥ 100(P2-D + Decayed Alpha)

**风险**:
- R5 LLM judge 加倍 LLM 成本 → 加 `ENABLE_LLM_JUDGE=False` flag(< 1 分钟回滚)
- R6 DAG 写入失败 → trace 不落 DAG,回到 linear hist(`runtime_state["dag"]` 默认 NULL,下游兼容)
- R10 family-cap 误杀 → flag flip 把 K=2 调整到 K=5 或 OFF(0 工程量)

### 4.5 Phase 3 — Flat + 主循环改造(Q3 2026,~12 周;含 flat-F1 双轨 + 灰度 2 周 + R1b 4-6 周 + flat-F4 删 cascade + 稳定 4 周)

**目标**:删 cascade 机械软停 + tier phase 切换,切换到 flat + hypothesis-driven 终态;同时激活 `agents/core/` 全 Pipeline(R1b)、Hierarchical RAG(R8)、sim cache(R9)、pyqlib pre-screen(Q10);最后删 phase15 旧列(D)。

**任务**(详 phase15 v2 §13):
- **flat-F1 新建 `_run_flat_iteration` + 双轨**(2-3 人日)
  - `mining_mode="FLAT_CONTINUOUS"` 路径,老 task `CONTINUOUS_CASCADE` 保留旧路径
  - 主循环:`while task.status not in ('PAUSED','STOPPED'): pick_hyp → bandit_dataset → run_one_round → maybe_abandon`
  - `_pick_next_hypothesis` 评分:`thesis_score DESC, then (alpha_count<5 OR pass_count>=1) DESC, then created_at DESC`
  - `_maybe_abandon_hypothesis`:`alpha_count >= 5 AND pass_count = 0 → ABANDONED`
- **flat-F2 默认切换**(0.5 人日,F1 ship + 2 周灰度 PASS 后)
  - `start_session` 默认 FLAT mode
- **flat-F3 T2 wrapper sweep → `llm_mutate_alpha`**(1-2 人日)
  - LLM 看 `_failed_tests` + `_brain_failed_checks` + P2-D pitfalls,提 2-3 个 wrapper(不是 5 个 group_* 全 sweep)
  - 落地:`backend/agents/mining_agent.py` 加 `llm_mutate_alpha` method
- **flat-F4 删 cascade legacy**(1 人日,F3 + 4 周稳定期后)
  - 删 `_run_cascade_phase` (`mining_tasks.py:921-1175`)、cascade 主循环 (`mining_tasks.py:1180-1399`)、`CASCADE_T*_ROUNDS` settings、`MIN_TIER_SEED_COUNT`
  - 测试套:cascade 测试标 `@pytest.mark.legacy_cascade`,写新 flat 测试套
- **R1b 全 Pipeline 激活**(4-6 周大改,与 flat-F2/F3 并行)
  - `hypothesis_centric_variant=3` 路由:`mining_tasks.py` 根据此 variant 进入 `agents/core/pipeline.AlphaMiningPipeline`
  - 4 段 Pipeline:`LLMHypothesisGen` → `LLMHypothesis2Experiment` → `BRAINExperimentRunner` → `LLMExperiment2Feedback`
  - **替换** mining_agent 主路径,**不删** mining_agent(保留 cascade-compat 入口)
- **R8 4 层 Hierarchical RAG**(5-8 人日)
  - Alpha-GPT v1.0 v2 修订:`RAG#0 alpha 全表达式 → RAG#1 高阶类别 → RAG#2 子类别 → RAG#3 datafield`
  - 落地:`backend/agents/services/rag_service.py` 重构
  - **依赖**:Q1 + Q2 + Q3(seed 充足才值得分层 — Phase 0+1 KB 已 570+ 条)
- **R9 Workspace checkpoint(simulation_cache 表)**(3 人日)
  - 新加 `simulation_cache` 表,key = (region, universe, dataset, alpha_expression_hash),value = sim result JSONB + cached_at
  - cached_run() 思路:命中缓存 → 跳过 BRAIN sim;未命中 → BRAIN sim + 写缓存
  - 节省 BRAIN cost(重复 alpha 不打 BRAIN)
- **Q10 pyqlib pre-screen as multi-fidelity 新层**(5 人日)
  - BRAIN sim 前的免费筛 — pyqlib 跑回测,IC < threshold → 跳过 BRAIN
  - 落地:`backend/multi_fidelity_eval.py` 加 layer 0(qlib pre-screen)
- **phase15-D 删旧列**(1 人日,flat-F4 + R1b ship + 稳定 4 周后)
  - 删 6 列:`mining_mode` / `cascade_phase` / `cascade_round_idx` / `progress_current` / `current_iteration` / `last_alpha_persisted_at`
  - `agent_mode` 删除(原 phase15 v2 §3 计划保留,R1b ship 后已无 cascade tier 概念)

**GO 闸门**(Phase 3 ship 终态):
- ✅ flat 灰度 1 region 跑 2 周,PASS rate ≥ cascade(目标 > 5% vs cascade 当前 0/37 = 0%)
- ✅ flat 24h 产出 ≥ 50 alpha(vs cascade ~30/天)
- ✅ flat 至少 1 alpha `can_submit=True`(vs cascade 当前 0/37)
- ✅ hypothesis.pass_count > 0 占 ACTIVE 总数 ≥ 10%(vs cascade 当前 1/9)
- ✅ R1b `hypothesis_centric_variant=3` 路径在生产 task 跑 ≥ 1 周,无 crash
- ✅ R8 4 层 RAG 在新 task 命中率 ≥ 50%(seed 充足前提下)
- ✅ R9 simulation_cache 命中率 ≥ 30%(BRAIN cost 降 ~30%)

**风险与回滚**(详 phase15 v2 §13.9):
- flat PASS rate < cascade → 回滚 `mining_mode` default 回 CASCADE(< 5 分钟)
- R1b 主循环改造 crash → flag `ENABLE_HYPOTHESIS_CENTRIC_LEVEL=2`(降级到当前 typed path)
- phase15-D 删列后才发现遗漏读路径 → Alembic 反向加列 + `runtime_state` 回填脚本(预备好,< 2 小时)— **唯一不可回滚单点风险**

---

## § 5 关键依赖关系图

```
                                  ┌────────────────────────────────────┐
                                  │ Bug B fix (DONE 2026-05-16)        │
                                  │ ENABLE_NEGATIVE_KNOWLEDGE_NUDGE ON │
                                  │ P3 ops dashboard ship              │
                                  │ HYPOTHESIS_CENTRIC_LEVEL=2          │
                                  └────────────────┬───────────────────┘
                                                   │
                       ┌───────────────────────────┼───────────────────────────┐
                       ▼                           ▼                           ▼
              ┌──────────────────┐       ┌──────────────────┐         ┌──────────────────┐
              │  Phase 0  R1a    │       │  Phase 0  Q1     │         │  Phase 0  Q3     │
              │  hook 启用       │       │  Kakushadze 101  │         │  Alpha158 + map  │
              │  2 人日 + 2 周   │       │  1 人日          │         │  2-3 人日        │
              └────────┬─────────┘       └────────┬─────────┘         └────────┬─────────┘
                       │ AttributionType                                       │ KB 5→256
                       │ 数据                                                  │
                       └──────────────────┐ ┌──────────────────────────────────┘
                                          ▼ ▼
                       ┌────────────────────────────────────┐
                       │  Phase 1  高 ROI 快赢              │
                       │  R2/Q7 bandit (arm 集 R1a 反证)    │
                       │  R3/Q8 AST distance                │
                       │  R4' dual-channel                  │
                       │  Q2 / Q4 / Q5 / Q6 (KB 256→570+)   │
                       └────────────────┬───────────────────┘
                                        │
                                        ▼
                       ┌────────────────────────────────────┐
                       │  Phase 1.5  Alembic 切 schema      │
                       │  A 加列 → B 双写 → C 切读 (灰度)   │
                       │  runtime_state JSONB 字段就绪      │
                       └────────────────┬───────────────────┘
                                        │
                                        ▼
                       ┌────────────────────────────────────┐
                       │  Phase 2  学术验证                 │
                       │  R5 LLM judge / R6 DAG / R7 / R10  │
                       │  Q9 Decayed Alpha → negative KB    │
                       └────────────────┬───────────────────┘
                                        │
                                        ▼
              ┌─────────────────────────────────────────────────────┐
              │  Phase 3  flat + R1b + R8/R9 + Q10 + phase15-D     │
              │                                                     │
              │  flat-F1 双轨 → F2 默认 → F3 LLM mutate → F4 删     │
              │             ↓                                       │
              │       R1b 全 Pipeline 激活(并行)                  │
              │             ↓                                       │
              │       R8 4 层 RAG / R9 sim cache / Q10 pyqlib       │
              │             ↓                                       │
              │       phase15-D 删 6 列(终)                       │
              └─────────────────────────────────────────────────────┘
```

**关键依赖链**:
- **R1a → R2/Q7 arm 集设计**:R1a 2 周数据反证 attribution 分布,决定 bandit arms
- **phase15-C → R6 DAG**:`runtime_state["dag"]` JSONB 字段就绪,R6 直接写
- **flat-F1 → R1b**:flat 主循环改造前置(R1b 不能在 cascade 内激活)
- **Q1/Q2/Q3 → R8**:KB seed 充足才值得分层 RAG
- **R3/Q8 → R10**:family 定义可基于 AST skeleton 聚类
- **flat-F4 + R1b ship → phase15-D**:6 列删除前必须 cascade 完全退役

---

## § 6 时间线与里程碑

### 6.1 假设单人 full-time(实际多人分摊会更快)

```
2026-05-17 ───┐  整合 master plan ship (本文档)
              │
2026-05-18 ───┤  Phase 0 kickoff
              │   R1a hook 接入 (2 人日)
2026-05-20 ───┤   Q1 Kakushadze (1 人日)
              │   Q3 Qlib→BRAIN 映射器 (2-3 人日)
2026-05-25 ───┤
              │   ╔═══ Phase 0 ship + 2 周 R1a 观察期 ═══╗
              │
2026-06-08 ───┤  Phase 1 kickoff (Phase 0 数据驱动)
              │   R2/Q7 bandit (2 人日)
              │   R3/Q8 AST (3-5 人日)
              │   R4' dual-channel (1-2 人日)
              │   Q2 (2 人日) / Q4 (0.5 人日) / Q5 (1 人日) / Q6 (1-2 人日)
2026-06-28 ───┤
              │   ╔═══ Phase 1 ship,GO 闸门验证 ═══╗
              │
2026-06-29 ───┤  Phase 1.5 kickoff
              │   Revision A (2 人日)
              │   Revision B (3 人日,回填 + 双写)
              │   Pydantic TaskConfig + 三字段合并 (4 人日,并行)
2026-07-13 ───┤   Revision C (3 人日 backend + 1 人日 frontend,灰度 2 周)
2026-07-27 ───┤
              │   ╔═══ Phase 1.5 ship,新 schema 灰度全量 ═══╗
              │
2026-07-28 ───┤  Phase 2 kickoff
              │   R5 (2 人日) / R6 (3 人日) / R7 (1 人日) / R10 (1 人日) / Q9 (1-2 人日)
2026-08-10 ───┤
              │   ╔═══ Phase 2 ship,GO 闸门验证 ═══╗
              │
2026-08-11 ───┤  Phase 3 kickoff (Q3 2026 大改)
              │   flat-F1 (2-3 人日)
2026-08-17 ───┤   ── flat-F1 ship + 2 周灰度
              │   R1b 启动 (4-6 周大改,与 R8/R9/Q10 并行/串行混合,见下)
              │   R8 (5-8 人日) / R9 (3 人日) / Q10 (5 人日)
2026-08-31 ───┤   flat-F2 默认切换 (0.5 人日)
2026-09-07 ───┤   flat-F3 LLM mutate (1-2 人日)
2026-09-28 ───┤   R1b ship (启动 8-17 +6 周上限) + flat-F4 删 cascade (1 人日,串行 R1b 之前)
2026-10-26 ───┤   稳定 4 周
2026-10-27 ───┤   phase15-D 删 6 列 (1 人日)
              │
              │   ╔═══ Phase 3 ship,flat + hypothesis-driven 终态 ═══╗
```

### 6.2 资源估算(假设 0.5 人 full-time 投入)

| 项 | 人月 |
|---|---|
| Phase 0 | ~0.3 人月(含 2 周观察期)|
| Phase 1 | ~0.8 人月 |
| Phase 1.5 | ~0.5 人月 |
| Phase 2 | ~0.5 人月 |
| Phase 3 flat | ~0.6 人月 |
| Phase 3 R1b 大改 | ~1.5 人月 |
| Phase 3 R8/R9/Q10 + 稳定期 | ~0.7 人月 |
| **合计** | **~5 人月(Q2-Q3 2026)** |

如果 1 人 full-time 推进,大概 **2.5-3 个月日历周期**;0.5 人推 5-6 个月。

---

## § 7 风险矩阵(汇总跨 phase)

| 风险 | 触发条件 | 缓解 | 影响 phase |
|---|---|---|---|
| **R1a hook 在 evaluate node 末尾 crash** | exception 未守护 | `try/except` + `ENABLE_R1A_HOOK` flag(< 1 分钟回滚)| Phase 0 |
| **R1a 数据偏向 main-loop alpha**(flip alpha 漏)| Bug B fix 未 ship | 已 done(commit a425937),flip path 也走 `_evaluate_single_alpha` | Phase 0 |
| **R2/Q7 arm 集错配** | R1a 数据不充分(达 ≥ 50 GO 闸门但 < 100,统计意义不足)| Phase 1 中段补 50+ 数据后再 finalize R2/Q7 arm 集;或用 5 维 reward 兜底;flag `ENABLE_DIRECTION_BANDIT=False` 回 cascade | Phase 1 |
| **R3/Q8 AST distance 拒过严** | Shamir-Tsur 阈值错 | flag flip OFF 第 6 维 fingerprint;调阈值 | Phase 1 |
| **Shamir-Tsur 1999 复杂度上界失实** | 论文交叉验证失败 | 降级 O(n²) brute-force(AIAC n < 20 可接受)| Phase 1 |
| **phase15 Revision C 切读 bug** | 灰度发现读路径漏修 | `ENABLE_TASK_SCHEMA_V2=False` flip(< 1 分钟)| Phase 1.5 |
| **phase15 Revision B 数据漂移** | 双写不一致 | `alembic downgrade -2` + 代码 revert(< 30 分钟)| Phase 1.5 |
| **R5 LLM judge 加倍 LLM 成本** | 每 alpha 多 2 次 LLM call | flag `ENABLE_LLM_JUDGE=False` flip;选 lighter model | Phase 2 |
| **R6 DAG 写入失败** | runtime_state JSONB 写入异常 | DAG 默认 NULL,下游兼容 linear hist | Phase 2 |
| **R10 family-cap 误杀** | top-k=2 太严 | flag 调 K=5 或 OFF(0 工程量)| Phase 2 |
| **flat PASS rate < cascade** | 灰度 2 周后 PASS < cascade | 回滚 `mining_mode` default 回 CASCADE(< 5 分钟)| Phase 3 |
| **R1b 主循环 crash** | `agents/core/pipeline` 调用链 bug | `ENABLE_HYPOTHESIS_CENTRIC_LEVEL=2` 降级(< 1 分钟)| Phase 3 |
| **phase15-D 删列后发现遗漏读路径** | grep 不充分 | Alembic 反向加列 + `runtime_state` 回填脚本(预备好,< 2 小时) | Phase 3 ⚠️ **唯一不可回滚单点** |
| **R8 4 层 RAG 命中率低** | seed 不充足或 embedding 配置错 | 降级回单层 RAG;调 chunking | Phase 3 |
| **R9 simulation_cache 失效** | alpha hash 算法误差 | flag flip OFF cache(回 BRAIN 每次 sim)| Phase 3 |
| **Q10 pyqlib pre-screen IC 失真** | 公共 OHLCV vs BRAIN datafield 差异 | flag flip OFF layer 0;调阈值 | Phase 3 |
| **R1b 与 flat-F4 并行导致冲突** | mining_tasks.py 同时被两 PR 改 | 串行 ship — F4 先(删 cascade legacy),R1b 后(全 Pipeline 激活)| Phase 3 |

---

## § 8 决策记录(整合 4 文档)

| ID | 决策 | 选项 | 选定 | 原因 |
|---|---|---|---|---|
| D1 | R1a/R1b vs R1c | 激活 core / deprecate core | **激活** | 3223 行沉没成本变资产,Phase 3 hypothesis-as-driver 是产品方向 |
| D2 | flat search 时机 | Phase 1 / Phase 3 | **Phase 3** | flat 需 R2/R3/R6/phase15-C 全部 ship 才有数据驱动;Phase 1 切风险过高 |
| D3 | cascade 是否保留 | 完全删 / 保留 legacy | **保留 legacy(Phase 3 ship 后双轨 2-4 周)** | 已有 task 兼容,渐进切换降风险 |
| D4 | R1a 启动时机 | Phase 1 / Phase 0 | **Phase 0** | phase15 GO 闸门 dead-lock,R1a 不启动则永远 0/4 |
| D5 | KB seed 扩张时机 | Phase 3 / Phase 0-1 | **Phase 0**(Q1+Q3)+ **Phase 1**(Q2+Q4+Q5+Q6)| LLM/RAG/judge 都依赖 seed 质量;延后则 R5/R8 价值打折。Q4/Q5/Q6 提前到 Phase 1 — Q3 算子映射器一旦写好,Q4 alias 是 trivial 延续;Q5 (FF5/q5/BAB anchor)/ Q6 (CHN seed) 与 Q2 同属 KB seed 扩张范畴,顺手做(qlib_research §5 原划分把它们放 Phase 2,本路线图重排到 Phase 1)|
| D6 | Bug B fix 是否前置 R1a | 是 / 否 | **是,已 done(commit a425937)** | flip-retry alpha 跳过 `_evaluate_single_alpha` 会让 R1a hook 漏数据;**task 652 derived alpha 中 27/37(73%)是 flip 产物**(单 task 样本,推测 historical 类似但未独立 verify),采集失真 |
| D7 | `mining_mode`/`agent_mode`/`cascade_phase` 处置 | 各自保留 / 三字段合并 | **三字段合并为 `schedule` + `starting_tier`** | 信息论冗余 — CASCADE ignore agent_mode、AUTONOMOUS≡TIER1、INTERACTIVE 死枚举(grep=0)、cascade_phase 50%+ NULL |
| D8 | `generation_strategy` 与 tier 关系 | 合并 / 独立列 | **独立列** | tier 是算子组合层级,strategy 是 R2/Q7 arm 选择,两个正交概念 |
| D9 | phase15-D 删列时机 | Phase 1.5 末 / Phase 3 末 | **Phase 3 末(flat-F4 + R1b ship + 稳定 4 周后)** | 单点不可回滚,稳定期必须够长 |
| D10 | R3/Q8 算法选择 | Shamir-Tsur O(n²·⁵/log n)/ O(n²) brute | **首选 Shamir-Tsur,降级 brute-force** | 论文上界未独立 verify,AIAC n < 20 brute 可接受 |
| D11 | R2/Q7 reward 维度 | RD-Agent 8 维 / AIAC 自选 | **自选 5 维 `(is_sharpe, is_fitness, -is_turnover, -self_corr, composite_score)`** | RD-Agent 8 维多数在 AIAC 无对应;`self_corr` 是实时 BRAIN API,`composite_score` 是实时计算字段,实现需额外 fetch 路径 |
| D12 | Qlib RL 是否集成 | 是 / 否 | **否** | QlibRL 聚焦 order execution + portfolio,非 alpha discovery |
| D13 | Alpha360 是否复制 | 是 / 否 | **否** | 60×6 OHLCV flatten 给 DL 模型,与 BRAIN 单表达式范式不兼容 |
| D14 | LangGraph 是否替换为 qrun YAML | 是 / 否 | **否** | LangGraph 动态分支 / qrun 静态 ML pipeline,目标不同 |
| D15 | R1b 是否替换 mining_agent | 替换 / 并存 | **并存**(R1b 走 `hypothesis_centric_variant=3` 路由,mining_agent 保留 cascade-compat 入口) | 双轨降切换风险;mining_agent 已稳定 6+ 周 |
| D16 | 工业派 LLM 用法是否参考 | 是 / 否 | **否,但保留 human review gate** | Citadel/Renaissance/Two Sigma 零公开 LLM-alpha;P3 ops console 是 AIAC 自有的人审安全网 |

(D17 "文档新建 vs 合并" 已移到 §0.3 决策锚点,作为本文档存在性的 meta-decision。)

---

## § 9 KPI 与验证准则

### 9.1 Phase 0 KPI(2 周)

| 指标 | 目标 | 来源 |
|---|---|---|
| R1a hook 触发次数 | **≥ 50**(数据驱动门槛,统计意义需 ≥ 100 — 若 2 周达 100+ 是 nice-to-have)| `SELECT COUNT(*) FROM alphas WHERE metrics ? '_r1a_attribution'` |
| AttributionType 非 NULL 比例 | ≥ 95% | 同上 / 总 alpha 数 |
| non-`unknown` attribution 比例 | ≥ 70% | enum value counts(`hypothesis` + `implementation` + `both`)|
| R1a hook failure 数 | < 10 | trace_step output `r1a_hook_failures` 聚合 |
| `ACADEMIC_PATTERNS` Python list 长度 | ≥ 256(原 5 + Q1 101 + Q3 150 条)| `wc -l backend/external_knowledge.py` 或 `len(ACADEMIC_PATTERNS)` REPL |
| `KnowledgeEntry` DB row 总数 | ≥ baseline.json `kb_total_entries` 59 + 251 去重后 ~280-310 | `SELECT COUNT(*) FROM knowledge_entries` 实测 |
| 0 production crash | 必满足 | log warning grep + alpha 持久化数不掉 |

### 9.2 Phase 1 KPI(2-3 周)

| 指标 | 目标 | 来源 |
|---|---|---|
| R2/Q7 bandit arm 累积 select | 任一 arm ≥ 30 | `backend/agents/evolution_strategy.py:DirectionBandit.arm_history` |
| R2/Q7 reward 方差 | > 0(arm 区分有效)| 同上 |
| R3/Q8 AST distance 在 `fingerprint` 第 6 维 active | ≥ 90% alpha | `diversity_tracker.py` 计数 |
| R3/Q8 distance 分布有方差 | std > 0.1 | metrics_tracker |
| R4' dual-channel 渲染 | positive/negative trace 可区分 | prompt log grep |
| `ACADEMIC_PATTERNS` Python list 长度 | ≥ 570(+ Q2 319 + Q6 30-50)| `len(ACADEMIC_PATTERNS)` REPL |

### 9.3 Phase 1.5 KPI(4 周)

| 指标 | 目标 | 来源 |
|---|---|---|
| Revision C 灰度区域全量稳定 | ≥ 2 周 | ops dashboard / log |
| `baseline.json` alpha 指标变化 | = 0(Phase 1.5 应零影响)| `test_suite.py --regression` |
| INTERACTIVE 任务存在数 | 0(SQL 二次确认) | `SELECT id FROM mining_tasks WHERE agent_mode='INTERACTIVE'` |
| `runtime_state["dag"]` 字段存在 | 100% 新 task | SQL 抽样 |

### 9.4 Phase 2 KPI(2 周)

| 指标 | 目标 | 来源 |
|---|---|---|
| R5 LLM judge 跑过 alpha | ≥ 100 | `feedback_agent` 计数 |
| attribution 分布移动 | `hypothesis` vs `implementation` 比例变化 ≥ 10% | R1a hook 对比 |
| R6 DAG 写入分支数 | ≥ 5 / task | `runtime_state["dag"]` SQL |
| R10 family-cap 限制 alpha 数 | ≥ 5 | metrics_tracker |
| Q9 `negative_knowledge` 总 entry | ≥ 100 | `KnowledgeEntry` table count |

### 9.5 Phase 3 KPI(终态)

> ⚠️ **caveat**:本表多项 KPI 无 baseline 数据支撑,标 `(target,无 baseline,Phase 3 中期重新校准)`。Phase 3 启动后第 2 周收集首批实测数据 → 重新评估目标值是否合理 → 若偏离 ±50% 以上则在路线图 v1.2 校正。

| 指标 | 目标 | 备注 | 来源 |
|---|---|---|---|
| **flat PASS rate** | **≥ cascade**(目标 > 5%)| cascade 0% 是 task 652 单 task 数据(0/37),非 cascade 一般水平 | metrics_tracker 24h 滚动 |
| **flat 24h alpha 产出** | **≥ 50**(vs cascade ~30/天)| cascade ~30/天 是 task 652 推算 | 同上 |
| **flat can_submit 数** | **≥ 1**(vs cascade 当前 0/37) | 同上,单 task 样本 | `submission_service` log |
| hypothesis pass_count > 0 占 ACTIVE 比 | ≥ 10%(vs cascade 1/9) | cascade 11% 是 task 652 数据 | `hypothesis` table |
| R1b `hypothesis_centric_variant=3` 路径稳定 | ≥ 1 周 / 0 crash | hard 验证项 | mining_tasks log |
| R8 4 层 RAG 命中率 | ≥ 50% | **target,无 baseline** — Phase 3 中期校准 | `rag_service` 计数 |
| R9 simulation_cache 命中率 | ≥ 30% | **target,无 baseline** — 取决于 alpha 重复率 | `simulation_cache` table |
| BRAIN cost 降幅(R9 + Q10 联合)| ≥ 30% | **target,无 baseline** — 联合效果未量化 | `MAX_SIMULATIONS_PER_DAY` 用量统计 |
| phase15-D 删 6 列后 | 0 production crash | hard 验证项 | log 4 周观察 |

---

## § 10 不做的事(显式排除)

1. **不 `pip install rdagent` 整包依赖**(LiteLLM/LangChain/Prefect 与 AIAC LangGraph/Celery/FastAPI 冲突)— rd_agent_research § 7
2. **不复制 Alpha-GPT v2.0 Alpha Modeling**(组合 portfolio,与 AIAC 单 alpha BRAIN 范式不同)— rd_agent_research § 7
3. **不用 RD-Agent DockerEnv sandbox**(AIAC 跑 BRAIN 在线 simulate,无本地代码执行需求)— rd_agent_research § 7
4. **不切换 LLM provider 到 Llama3 70B**(DeepSeek/Claude 已足够)— rd_agent_research § 7
5. **不重写 LangGraph 主路径**(mining_agent 已稳定 6+ 周,激活 core/ 走渐进 R1a 钩子)— rd_agent_research § 7
6. **不做 Qlib RL toolkit 集成**(QlibRL 聚焦 order execution + portfolio,非 alpha discovery)— qlib_research § 6
7. **不做 Alpha360 复制**(60×6 OHLCV flatten 假设深度学习自学特征,与 BRAIN 单表达式范式不兼容)— qlib_research § 6
8. **不在 AIAC 内引入 Qlib ML model**(P0-P2 是 LLM-driven hypothesis,引 ML 改变范式)— qlib_research § 6
9. **不引入 Qlib 公共 OHLCV 数据**(BRAIN 已提供,公共数据精度差且不一致)— qlib_research § 6
10. **不重复 Alpha-GPT 1.0/2.0 论文方法**(AIAC 已是 Alpha-GPT 后继 + RD-Agent 范式融合)— qlib_research § 6
11. **不复制 Chain-of-Alpha 已撤稿论文方法**(arxiv 2508.06312)— qlib_research § 6 + competitive_analysis § 5.1
12. **不直接抄 Citadel/Renaissance LLM 用法**(无公开披露)— qlib_research § 6 + competitive_analysis § 7
13. **不 rm core/feedback.py**(R1a 入口 `enhance_existing_node_evaluate` 直接 import `HypothesisFeedback` + `AttributionType` class)— rd_agent_research § 7
14. **不去掉 P3 ops console PAUSE/STOP**(flat 切换只去机械软停,人工 PAUSE 必须保留作为工业 reality check 安全网)— 本文档 §1.3
15. **不在 Phase 3 之前删 cascade legacy 代码**(双轨期 + 4 周稳定期是不可压缩的)— 本文档 §4.5
16. **不引入 `WQBCredential.role` 字段**(CLAUDE.md 已固化 — BRAIN role 切换走 FeatureFlagOverride)— CLAUDE.md

---

## § 11 信息不可得(汇总跨文档)

实施时若卡在以下问题,需做额外调研或交叉验证:

- **Alpha-GPT 2.0 具体 Sharpe/IC 数表**(PDF binary fetch 失败,html 404)— rd_agent §8.1
- **Shamir-Tsur 1999 `O(n²·⁵/log n)` 上界**(DBLP/ScienceDirect socket 错误)— rd_agent §8.1,影响 R3/Q8 工程量
- **RD-Agent-Quant 14.21% / 22-26 因子 / <$10 单次成本**(来自 paper body Table,review v2 未独立 verify)— rd_agent §8.1
- **AlphaAgent CSI500 五指标**(IC=0.0212 / IR=1.488 / MDD=-9.36% / hit-ratio +81% / token +23%,PDF 文本层提取失败)— rd_agent §8.1
- **Hubble formal ablation**(作者 v2 自承缺)— rd_agent §8.1
- **AlphaAgent AST 算法伪代码 / 复杂度证明**(论文未给,需自行设计 Shamir-Tsur)— rd_agent §8.1
- **Chain-of-Alpha 撤稿具体理由**(arxiv 标记撤稿但未提供官方 retraction notice)— competitive_analysis §10
- **AlphaSAGE / Alpha² 完整实验数据**(arxiv 摘要级别)— competitive_analysis §10
- **101 Alphas 逐 alpha Sharpe/turnover**(论文 Appendix A 只给公式,不给数据)— qlib §7
- **WorldQuant 官方 "Five Pillars" 文档**(公开渠道未找到,AIAC 的 Five Pillars 是项目自定义术语)— qlib §7
- **历史 CASCADE 任务的 `starting_tier` 回填是否全为 1**(需 SQL 二次确认无例外)— phase15 §10
- **`generation_strategy` 默认 arm 集是否包含 `genetic/rag_template/knowledge_pattern`**(取决于 R2/Q7 离线实验 — Phase 1 数据回填)— phase15 §10
- **`runtime_state` 是否需要存 `arm_history` 完整序列还是只存最近 N 条**(取决于 R6 MCTS 选树深度)— phase15 §10

---

## § 12 与已有 memory / 其他文档的关系

### 12.1 memory(必读,跨 conversation 持久)

| Memory | 内容 | 与本路线图关系 |
|---|---|---|
| [[feedback_alpha_submission_criteria]] | 提交三道门 can_submit + self_corr<0.7 + IQC Δscore>0 | flat-F1 `_pick_next_hypothesis` 评分公式可借鉴 |
| [[reference_alphagbm_skills_research]] | docs/alphagbm_skills_research_2026-05-15.md + P0~P2 路线 | 与 R/Q 路线 orthogonal,P0-P2 已落地 |
| [[reference_anthropic_opus_4_7_no_temperature]] | Opus 4.7 拒 temperature,LLMService 按 prefix 跳过 | R5 LLM judge / R8 hierarchical RAG 用 Opus 时需注意 |
| [[reference_anthropic_extended_thinking]] | thinking={type,budget_tokens,display},xhigh=32000,自动 streaming | R5 judge 节点用 xhigh 时需配 streaming |
| [[feedback_thinking_effort_per_node]] | hypothesis/code_gen xhigh,self_correct=low,distill/attribution=disabled | R5 judge 节点初始用 xhigh,A/B 后再调 |
| [[reference_ops_dashboard_p3]] | 9 页 / 28 endpoint / 143 测试,JSONB-free fixture 模式 | Phase 1.5 schema 切换、Phase 3 flat KPI 都走此 dashboard |
| [[feedback_no_reflex_flag_cleanup]] | 验证通过 ≠ 关 flag,P3 设计 flag 长期常驻 | 所有 ENABLE_* flag 加好后不要反射性关 |
| [[project_aiac_flags_on_2026_05_16]] | 9 P0/P1/P2 flag override ON + HYPOTHESIS_CENTRIC_LEVEL=2 | Phase 0 起点状态 |
| [[project_bug_b_flip_retry_evaluate_skip]] | Bug B 已 fix(commit a425937)| R1a 启用的前置依赖 |

### 12.2 设计文档(实施时回查)

- `backend/agents/core/ARCHITECTURE.md` — 18 章节 RD-Agent 兼容设计(R1a/R1b 实施必读)
- `backend/CODE_STATUS.md` / `backend/REFACTORING_STATUS.md` — 分层依赖规则(phase15 实施必读)
- `CLAUDE.md` — 项目根设计文档(任何 phase 都要遵守)
- `docs/alphagbm_skills_research_2026-05-15.md` — 工程模式 + P0-P2 落地经验(已完成参考)
- `docs/retrospective_p012_2026-05-16.md` — P0-P1-P2 跨阶段回顾(避免重蹈覆辙)

### 12.3 跑量实测数据(决策依据)

- `docs/llm_op_monitor/2026-05-12.md` ~ `2026-05-16.md` — LLM 操作监控
- `docs/quality_review_mining_task_2026-05-13.md` / `2026-05-14.md` — task 质量回顾
- `docs/code_review_v27_fixes_2026-05-14.md` — code review 修复
- `docs/phase1_ab_report_2026-05-05_p1_threshold.md` — Phase 1 A/B 报告
- `docs/v26_retrospective/` — V-26 回顾

---

## § 13 实施 kickoff checklist

启动 Phase 0 之前 verify:

- [ ] Bug B fix 在 main 分支(`git log --oneline | grep a425937`)
- [ ] `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE=True`(DB FeatureFlagOverride)
- [ ] `HYPOTHESIS_CENTRIC_LEVEL=2`(env / DB)
- [ ] P3 ops dashboard 9 页面 / 28 endpoint 可访问(`X-Ops-Token` 配好)
- [ ] `backend/agents/core/` 9 模块 3223 行存在(grep verify)
- [ ] `backend/agents/core/integration.py:342-407` 存在 `enhance_existing_node_evaluate`(实测 signature 见 phase15 v2 §14.2)
- [ ] `baseline.json` 当前 `kb_total_entries` 记录(扩张前基线)
- [ ] 一份完整的 R1a 实施 PR 计划(file:line / try-except / flag / test 套)— 参考 phase15 v2 §14
- [ ] 通知 stakeholder:Phase 0 启动 + 2 周 R1a 观察期 + Phase 1 kickoff 预计日期

---

## § 14 版本历史

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-05-17 | v1.0 | 初版 — 整合 4 文档(competitive_analysis / phase15 v2.1 / rd_agent_research / qlib_research)为统一战略路线图 |
| 2026-05-18 | v1.20 | **Phase 3 R9 simulation cache ship — BRAIN sim result caching (PR1 infra)**(同 session flat-F3 ship 后接力 R9,Phase 3 进度 4/8)。commit `e432585`(716 insertions / 6 files / 15 integration tests + 225/225 cumulative PASS / ~45 min vs plan 3d for PR1 scope)。**Scope PR1**:simulation_cache 表 + helpers + wrapper API + tests;**PR2 deferred**:evaluation.py node_simulate buckets 调用站点 swap brain.simulate_batch → cached_simulate_batch when flag ON(需 plumb db session 进 bucket loop)+ production verify(SQL hit rate ≥ 30%)。**Master plan estimate**:40-60% BRAIN sim cost reduction on duplicate-heavy workloads(cascade T2/T3 wrapper variants + flat dataset cycling)。**实施清单**:`backend/alembic/versions/9a4f7e8c1d6b_phase3_r9_simulation_cache.py` simulation_cache 表(BigInt id PK + cache_key UNIQUE + region/universe/expression+hash + settings_json JSON + result_json JSON + success bool + cached_at/accessed_at/access_count + 4 indexes)applied production DB head 9a4f7e8c1d6b + `backend/models/simulation_cache.py` (NEW SQLAlchemy model extend_existing) + `backend/agents/sim_cache.py` (NEW ~220 行 pure-helpers + cached_simulate_batch wrapper:SETTINGS_KEYS canonical projection + compute_cache_key sha256[:64] stable across processes region-normalized 忽略 unknown keys + get_cached soft-fail TTL filter access-stats bump + set_cached UPSERT SIMULATION_CACHE_ONLY_SUCCESS=True guard + cached_simulate_batch 6-step partition→BRAIN-uncached→cache→reassemble,empty input→[],100% hit→0 BRAIN call,BRAIN failure→failure dicts for uncached) + `backend/config.py` ENABLE_SIMULATION_CACHE default OFF + SIMULATION_CACHE_TTL_DAYS=14 + SIMULATION_CACHE_ONLY_SUCCESS=True + `backend/services/feature_flag_service.py` 双文件注册(24 SUPPORTED_FLAGS) + `backend/tests/integration/test_r9_sim_cache.py` (NEW 15 tests pg_session cover compute_cache_key 5 cases stability/sensitivity + get/set round-trip + UPSERT + expired backdate + failures-skipped + cached_batch all-miss/all-hit/partial/BRAIN-fail/empty)。**Default flag OFF**;Alembic head 9a4f7e8c1d6b applied with idempotent down()。**Soft-fall philosophy**:any DB/BRAIN failure → fallback path,NEVER blocks a sim。**Phase 3 进度 4/8**:flat-F1 + flat-F2 + flat-F3 + R9 PR1 ✓;剩 flat-F4 / R1b / R8 / Q10 / phase15-D|
| 2026-05-18 | v1.19 | **Phase 3 flat-F3 ship — LLM-driven T2 wrapper mutation**(同 session flat-F2 ship 后接力 flat-F3,Phase 3 进度 3/8)。commit `cf273d8`(529 insertions / 5 files / 20 unit tests + 210/210 cumulative PASS / ~45 min vs plan 1-2d)。**Scope**:替换 cascade T2 path `expand_t2_strategy` 暴力 sweep (8-12 wrapper variants per seed ~70% FAIL) 为 LLM 看 (seed + _failed_tests + P2-D pitfalls + Q9 Decayed seeds) 选 top-K=3 wrappers。**Soft-fall philosophy**:LLM 失败/返空/解析错 → fall back to legacy `expand_t2_strategy`,永不阻塞 round。**关键 design**:`<SEED>` placeholder pattern (LLM 不内联 seed text,caller 替换)+ strict-JSON 防御解析 (drop items without `<SEED>`,non-dicts,missing keys)+ bounded inputs (seed 500 / failure 1500 / decay 800 chars / top_k [1,5])+ default haiku-4-5 + node_key='llm_mutate_alpha' per-node effort dispatch。**实施清单**:`backend/config.py` ENABLE_LLM_MUTATE_ALPHA default OFF + LLM_MUTATE_TOP_K=3 + LLM_MUTATE_MODEL='claude-haiku-4-5-20251001' + `backend/services/feature_flag_service.py` 双文件注册 (23 SUPPORTED_FLAGS) + `backend/agents/llm_mutate_alpha.py` (NEW ~210 行 pure-function module: MUTATE_SYSTEM/USER_TEMPLATE prompts + build_mutate_prompt + _parse_variants 防御 + _substitute_seed + async llm_mutate_alpha 主 API soft-fail empty list) + `backend/agents/graph/nodes/tier_seed.py:node_tier_wrap_one` T2 分支 flag-gated (ON → try llm_mutate_alpha → non-empty 用 LLM,empty → legacy;OFF → legacy byte-equivalent;per-seed try/except guard) + `backend/tests/unit/test_llm_mutate_alpha.py` (NEW 20 tests: prompt build / parse 8 cases / substitute / async 5 cases including empty seed / exception / happy / empty / garbage)。**Cost expectation**:haiku-4-5 ~$0.01/call/seed,**40-75% BRAIN sim cost 估算节约**(3 LLM 选 vs 8-12 sweep) + 提 PASS rate (LLM 避 known-failed patterns)。**Default flag OFF** — user-controlled rollout。**Deferred PR2**:failure_context (SQL 查 recent _failed_tests) + decay_context (Q9 KB matches) 实际填充 + integration test with dispatch + production verify + flag flip(gating: flat-F1 + 2 周灰度 PASS per §4.5)。**Phase 3 进度 3/8**:flat-F1 ✓ + flat-F2 ✓ + flat-F3 ✓;剩 flat-F4 (1d 删 cascade legacy F3+4 周稳定期后) / R1b (4-6w) / R8 (5-8d) / R9 (3d) / Q10 (5d) / phase15-D|
| 2026-05-18 | v1.18 | **Phase 3 flat-F2 ship — default mining_mode flip gated by flag**(同 session R6 全 ship 后接力 Phase 3 第一任务,决策 5A unblock by R6 DAG ship)。commit `6972e3b`(211 insertions / 4 files / 3 tests + 190/190 cumulative PASS):`backend/config.py` ENABLE_DEFAULT_FLAT_SESSION default OFF + `backend/services/feature_flag_service.py` 22 SUPPORTED_FLAGS 双文件注册 + `backend/services/task_service.py:start_session` double-flag guard delegate to start_flat_session when BOTH ENABLE_DEFAULT_FLAT_SESSION AND ENABLE_FLAT_CONTINUOUS ON(guard 防 mining_tasks dispatch branch 拒 FLAT task mark FAILED 的 broken state)+ `backend/tests/integration/test_flat_f2_default_flip.py` (NEW) 3 tests cover decision lock 三 branch(两 OFF → cascade / flat-F2 ON flat-F1 OFF → cascade guard / 两 ON → flat delegate verified mining_mode=FLAT_CONTINUOUS schedule=ONESHOT cascade_phase=None)。**Estimate 0.5 人日 / 实际 ~30 min**(scope crisp + 0 Plan agent 开销,直接实施;test 用 pg_session live PG JSONB fixture)。**Production flag default OFF — user-controlled rollout**:无自动 flip,既有 cascade tasks (task 652 USA PAUSED) 不受影响,flat-F2 仅控制 NEW session 创建。R6 DAG 已 production ON 给 flat 实证 reward-guided exploration,flat-F2 flip 仅切换 user-facing default 入口。Rollback flip OFF (<1 min) 仅影响 NEW task。**Phase 3 进度 1/8**:flat-F2 ✓ ship;剩 flat-F3 (1-2d T2 wrapper sweep → llm_mutate_alpha) / flat-F4 (1d 删 cascade legacy F3+4 周稳定期后) / R1b 全 Pipeline (4-6 周大改) / R8 Hierarchical RAG (5-8d) / R9 sim_cache (3d) / Q10 pyqlib pre-screen (5d) / phase15-D 删 6 旧列(flat-F4 + R1b ship + 稳定 4 周后)|
| 2026-05-18 | v1.17 | **Phase 2 R6 PR3 ship + Phase 2 5/5 code-complete + production verify**(同 session R6 PR1+PR2 ship 后接力 PR3 + DAG flag flip 启 production)。commit `5804e22`(424 insertions / 2 files / 9 PR3 tests + 187/187 cumulative PASS):`backend/tasks/mining_tasks.py` 添 `_dag_update_after_round(db, run, dag_state, *, round_result, tier, dataset_id, round_idx)` helper(alpha-as-node per [V1.0-A2-1] + reward + R10 family_capped propagation + prune_to_cap + flag_modified + commit,**soft-fail philosophy** 任 DAG bookkeeping 错误 logged 但 never raise)+ `_run_cascade_phase` 加 `dag_state: Optional[dict] = None` kwarg(default None preserve byte-equiv legacy)+ serial-fallback + V-20.1 pipeline 双 path 调用 `_dag_update_after_round` after `_stamp_heartbeat` per round + `_run_continuous_cascade` 初始化 `dag_state` via `load_or_init`(soft-fail init throws → dag_state=None this session)+ outer loop after `_resolve_cascade_phase` 调 `select_next_parent`(`cold_threshold` + `ucb_c` from settings)override `current_phase` if non-None + 更新 `dag_state["current_selection"]` + 3 phase 调用线程 `dag_state=dag_state` kwarg + `_run_flat_iteration` 同 pattern(DAG select dataset choice when ON,`flat_cursor` 保 dual-write fallback per [V1.0-A2-2])。`backend/tests/integration/test_dag_trace.py` (NEW) 9 tests:soft-fail (dag_state=None / run=None) / empty alphas early return / 3-alpha 增 3 子节点+ reward + n_pulls=1 / R10 family_cap propagation / prune over cap with patched DAG_MAX_NODES=10 / `_run_cascade_phase` dag_state kwarg present default None / 外层 cascade + flat signatures 不变(init internal)。**Production verify 2026-05-18 ~09:50 UTC**:DB override INSERT + backend+celery 重启 + 21 flags loaded(ENABLE_DAG_TRACE effective=True)+ task 1501 (USA FLAT, datasets=[pv1]) POST /ops/start-flat-session → celery log `[R6 dag flat] task=1501 run=809 init: nodes=1 root=n_809_0_0` ✓ DAG init LIVE 立即创建 root node。**Phase 2 进度 5/5 code-complete**(R5 + R7 + R10 + Q9 + **R6 三 PR** ✓);GO 闸门 (a)(b)(c)(e) per plan §10 production data accumulating(soft-fail 设计 + 187/187 PASS 保 (d) 0 production crashes already verified)。**Phase 3 flat-F2 unblock**(决策 5A):DAG 现两 path 共用,flat 不再 linear cursor → cascade 退役 0 regression,下 session 起 flat-F2 plan agent kickoff。**9 ENABLE flags production ON**:R1a / DUAL_RAG / AST_DIM / DIR_BANDIT / FLAT_CONTINUOUS / TASK_SCHEMA_V2 / LLM_JUDGE / FAMILY_CAP / **DAG_TRACE**;default OFF 仅 R7 SELF_CORRECT_SEMI_ACCEPT + 用户未启用项。21 total SUPPORTED_FLAGS|
| 2026-05-18 | v1.16 | **Phase 2 R6 PR1 + PR2 ship**(同 session R6 plan v1.0 ready 后立即接力实施,plan estimated 4.25 人日 共 3 PR;PR1+PR2 同 session ship,PR3 cascade/flat dispatch 推下 session)。**PR1 commit `72d5945`**(898 insertions / 5 files / 34 unit tests / 1.0 人日):`backend/agents/graph/dag_state.py` (NEW ~330 行 9 pure-function helpers `init_dag/add_node/update_reward/mark_status/select_next_parent/path_to_root/prune_to_cap/to_dict/from_dict`,UCB1-lite + Thompson cold-start hybrid 实现 plan §4.2,WRITE-SIDE hard cap [V1.0-S4] 在 add_node 强制,prune 保 root + current_selection path 不破 parent chain invariant [V1.0-S6]) + `backend/config.py` ENABLE_DAG_TRACE default OFF + 5 settings (DAG_MAX_NODES=100/DAG_MAX_DEPTH=10/DAG_UCB_EXPLORATION_C=1.4/DAG_COLD_THRESHOLD=3/DAG_PRUNE_LRU_KEEP_FRACTION=0.7) + `backend/services/feature_flag_service.py` 双文件注册 + `backend/agents/core/trace.py` +60 行 minimal R6-v1 adapter(ExperimentTrace 保 DORMANT,production R6 直接用 dag_state.py)。**PR2 commit `f06145d`**(381 insertions / 4 files / 18 new tests / scope-reduced 1.0 人日 vs plan 1.5):`backend/agents/graph/dag_state.py` +4 integration helpers(`load_or_init` 避 call site 分支 / `compute_reward_for_node` plan §4.5 3-tier composite>sharpe/4>0.5 / `add_children_for_phase` bulk-add alpha-as-node per [V1.0-A2-1] + defensive skip None / `mark_family_capped_children` R10 propagation `_r10_family_cap_dropped=True` → status='family_capped') + `backend/tasks/mining_tasks.py:_resolve_cascade_phase` 扩 3-stage fallback per plan §5.3(Stage 1 DAG > Stage 2 phase15-C current_tier > Stage 3 legacy cascade_phase,每 stage 干净 fall through,flag OFF byte-equivalent legacy + phase15-C) + `backend/tests/unit/test_dag_state.py` +14 PR2 tests + `backend/tests/integration/test_phase15_c_dual_source_reads.py` +4 R6-fallback tests(DAG priority / DAG OFF preserves / missing selection / invalid tier 全 fallback)。**Verification** 48 R6 PASS(34 PR1 + 14 PR2 + 4 fallback)+ 178/178 cumulative regression PASS;default flag OFF zero behavior change;无 Alembic(runtime_state JSONB phase15-A 已 ready)。**PR3 next session scope**:`_run_cascade_phase` 签名加 `dag_state=None` kwarg + 3 phase 调用点 add_children+update_reward+prune_to_cap+flag_modified + `_run_continuous_cascade` outer loop select_next_parent override current_phase + `_run_flat_iteration` DAG-select dataset(flat_cursor 保 fallback dual-write per [V1.0-A2-2])+ test_dag_trace.py integration tests + production verify(flag flip ON + ≥5 task dag.nodes + ≥1 branch_count≥4 + avg JSONB<256KB plan §10 GO gate)。**Phase 2 进度 4/5 ship + R6 2/3 PR ship**;R6 PR3 ship 后 Phase 2 5/5 + Phase 3 flat-F2 plan agent kickoff unblock(决策 5A)|
| 2026-05-18 | v1.15 | **Phase 2 R6 DAG plan v1.0 ready**(同 session Q9 ship 后接力 Phase 2 最后一项 plan kickoff)。Plan `~/.claude/plans/phase2-r6-dag-2026-05-18.md` v1.0(1034 行 / 67KB / §0-§14 完整 / 3 轮 fresh agent adversarial review converged):**self-audit 7 MUST + 5 SHOULD inline-fixed → fresh-agent #1 4 MUST + 4 SHOULD → fresh-agent #2 3 MUST + 3 SHOULD;cumulative 14 MUST + 12 SHOULD inline-fixed,0 outstanding**。**Scope**:替换 linear `T1→T2→T3` cascade_phase-scalar 推进为 DAG-structured multi-branch trace persisted in `experiment_runs.runtime_state["dag"]` JSONB(R6-v1 简化-MCTS,best-path expand + reward backprop;full UCT 推 R6-v2 post-observation)。**关键 design locks**:(a) DAG schema v=1:nodes dict + current_selection 单 leaf id(非 path tuple per [V1.0-A2-2])+ idx2loop_id mapping + 100-node write-side hard cap(`DAG_MAX_NODES=100`,~25 KB,256 KB GO gate 10× headroom);(b) Selection algo:UCB1-lite + Thompson cold-start(不 full UCT 不纯 Thompson),cold-start global prior 借鉴 R2/Q7 Beta-Bernoulli + R1a/R5 attribution priors;(c) 3-mode coexistence(cascade + flat + dag,同 Phase 1.5-C dual-source pattern):`_resolve_cascade_phase` 扩 3 层 fallback dag.current_selection → runtime_state.current_tier (phase15-C) → task.cascade_phase (legacy);(d) R10 family-cap 互动:dropped branches 留 DAG 标 status='inactive' analytics signal selection 时 prune 不复用;(e) flat path integration:R6 ON 时 flat 也走 DAG — flat-F2 default mining_mode 翻 unblock 关键;(f) reward 3-tier fallback:composite_score (R5+R1a) > sharpe > 0.0;(g) prune 策略 LRU + reward 加权(非 pure-LRU)保高 reward node 不淘汰 + 必保 parent chain invariant [V1.0-S6];(h) flag `ENABLE_DAG_TRACE: bool = False` 双文件注册,rollback < 1 min,flag OFF 不删 in-flight DAG 数据(保 forensic SQL)。**Effort 上调**:master plan §4.4 R6 估 3 人日 → plan v1.0 **4.25 人日**([V1.0-S1] self-audit caught 漏 serialization bound + tests + cold-start logic + flat path integration)。**三 PR 拆**:PR1(1.0d trace.py 扩 + helpers + 单测)→ PR2(1.5d _run_continuous_cascade integration + 3-layer fallback)→ PR3(1.75d _run_flat_iteration integration + size-bound prune + GO gate scripts + production verify)。**GO 闸门(Phase 2 → 3 R6 portion)**:(a) ≥ 5 task with dag.nodes non-null + current_selection (b) ≥ 1 task branch_count ≥ 4 实证 multi-branch (c) avg JSONB <256KB (d) 0 crashes (e) reward propagation 正确性 ≥ 3/5 task。**已就绪前置**:Phase 1.5-A runtime_state JSONB col(Alembic `7a3f9e1c2b8d`)+ Phase 1.5-C dual-source pattern + R1a/R5 reward 信号 + R10 inactive marker + R2/Q7 Beta-Bernoulli 借鉴。**No Alembic 需要**(runtime_state 已 ready,additive 子 key)。**Phase 2 进度**:4/5 ship + R6 plan ready(impl 下 session)。R6 ship 后 Phase 3 flat-F2 (default mining_mode 翻) plan agent kickoff|
| 2026-05-18 | v1.14 | **Phase 2 Q9 Decayed Alpha seed ship**(同 session 4 个 Phase 2 task ship 后接力 Q9 闭 4/5)。commit `9b56b08`(789 insertions / 3 files / 12 Q9 tests + 125 cumulative PASS / 53 entries)。**实施**:`backend/data/decayed_alphas_seed.json` 53 well-curated entries(McLean-Pontiff 2016 JoF + Hou-Xue-Zhang 2020 RFS + Harvey-Liu-Zhu 2016 + Chen-Zimmermann 2022),每 entry: pattern(BRAIN DSL)+ description + decay_pct + failure_mode(12 documented categories)+ theoretical_anchor + t_stat_orig + `_meta` header 记 source/criteria。**Coverage**:accruals/value/profitability/investment classics + momentum/reversal/IVOL/MAX/illiquidity + external_financing/NOA/composite_issuance + PEAD/SUE/analyst_revisions/insider + corp events(spin-off/IPO/SEO/M&A/restatement)+ quality factors + **HXZ 2020 "not replicated"** anomalies (IVOL/MAX/operating_leverage/tax_avoidance/skewness/cash_to_assets)。`scripts/seed_decayed_alphas.py` idempotent UPSERT via pattern_hash UNIQUE,写 meta_data `import_batch=phase2_q9_decayed_2026_05_18` + `decayed=true` + `pattern_operators_pending=true` forward-compat hook (per [[feedback_forward_compat_metadata_hook]])。`tests/integration/test_q9_decayed_seed.py` 12 tests cover file 结构 / 必填字段 / decay_pct 负数-and-范围 / t_stat 正 / failure_mode 在 12-category set / pattern_hash 唯一 / name 唯一 / anchor 非空。**Production import 实证**:53/53 inserted live ~0.14s,failure_mode 分布 arbitrage=28 / data_mining=9 / factor_subsumption=3 / structural_change=3 / arbitrage_speed=2 / regime_sensitive=2 / 6 other singleton;**Total FAILURE_PITFALL active 2432 ≫ GO gate ≥100 by 24×**;最强 decay 实证 accruals_sloan -85%(t=4.1)/ max_daily_return_bali -80%(t=3.4)/ tax_avoidance_dyreng -80%(t=2.5)。**Verification** 12/12 Q9 + 125/125 cumulative,0 production 行为变(纯 additive insert,no flag,no code path 改)。**Phase 2 进度 4/5** (R5 + R10 + R7 + Q9 ✓),剩 R6 DAG(3 人日,最大架构改 — 留下 session Plan agent + impl)。**R8 RAG 未做** — 实施时可 `COALESCE(meta_data->>'decayed','false') != 'true'` filter 避 decayed patterns,或 `failure_mode='regime_sensitive'` 保 BAB-like regime-dependent signals|
| 2026-05-18 | v1.13 | **Phase 2 R7 ship + 5 user decisions**(同 session R5/R10 ship 后接力)。**User 决策**:(1)R5 GO gate $0.05/call → **$0.10/call**(决策 1B,doc-only:R5 实测 c1+c2 combined ~$0.107/alpha 2.14× 原 estimate,接受 haiku 实际成本 / daily $2-20 acceptable);(2)`ENABLE_FAMILY_CAP=true` **production flip**(决策 2A,R10 commit 939e12b 验证 starts);(3)R7 self-correct 半接受 next(决策 3);(4)phase15-C frontend 6 sites adapt **下 session 一次性补 ~1.5 人日**(决策 4A,当前 UX agent_mode/cascade_phase 老字段 acceptable no break);(5)flat-F2 (默认 mining_mode 切换) **等 R6 DAG ship 后**(决策 5A,DAG 是 flat path 真正受益架构,顺序 R6 → flat-F2 → F3 → F4 cascade 退役)。**R7 实施** commit `c281831`(304 insertions / 4 files / 5 R7 tests + 113 cumulative PASS):rd_agent Co-STEER `should_use_new_evo` validation-time 适配 — 原 spec 比 score 留 R7-v2 等 R6 DAG ship 后做;本 PR R7-v1 用 finding-count。`backend/config.py` ENABLE_SELF_CORRECT_SEMI_ACCEPT default OFF + `backend/services/feature_flag_service.py` 双文件注册 + `backend/agents/graph/nodes/validation.py:node_self_correct` 在 fixed=... 之后 + updated_alpha.expression=fixed 之前加 R7 block:flag ON → 调 alpha_semantic_validator.validate_alpha_semantically(fixed, fields[:50], strict=False) re-validate;Accept 条件 new.valid OR new_err_count < orig_err_count;Reject 路径保原 expression + 标 _r7_self_correct_rejected=True + _r7_self_correct_reason + _r7_rejected_candidate[:200];_record_correction + knowledge_extracted move INSIDE accept branch 避 rejected fixes 污染 KB;Validator exception → graceful fallback accept。**Tests** 5 mock cases(flag OFF byte-equiv / new VALID → accept / new INVALID strict fewer → accept / new INVALID same+more → REJECT + _record_correction not called / validator raises → graceful)。**GO 闸门(plan §9.4 Phase 2 KPI R7 portion)**:`COUNT WHERE metrics->>'_r7_self_correct_rejected' = 'true' ≥ 5` + max_retries=3 行为不变。**Rollback**:flag flip OFF (< 1 min)。**Phase 2 进度** R5 ✓ R10 ✓ R7 ✓(3/5),剩 R6 DAG(3 人日 plan + impl)+ Q9 Decayed seed(1-2 人日)|
| 2026-05-18 | v1.12 | **Phase 2 R10 family-cap ship + R5 field-name fix**(同 session R5 ship 后两个补强)。commit `72e3833`(R5 field-name fix:R5 hook 在 evaluation.py 读 `_a.logic_explanation` 永空 — AlphaCandidate Pydantic 字段是 `explanation`,Alpha SQLAlchemy DB 列才是 `logic_explanation`,persistence.py:270 mapping。task 1463 100% skip 真因不是 plan §1.4 [V1.0-A1-2] T1 数据缺失,是字段名错误。1 line fix 改读 `_a.explanation`,unit tests 24/24 PASS — 没抓到因 tests 直接传 description 参数);+ commit `939e12b` Phase 2 R10 family-cap (Hubble v2 Table 1,1 人日,388 insertions):**design lock**:family signature = sha256 prefix of `pillar_classifier._extract_operators` 提取的 op sequence(粗粒度,与 R3/Q8 ast_distance_log 细粒度配对;比 full AST parsing 简单且复用现有 extractor);composite_score (R5+R1a) > sharpe priority;multi-pillar isolation 各组独立 cap;defensive top_k=0/neg → 1 with warning。**实施**:`backend/family_classifier.py` (NEW family_signature + apply_family_cap) / config.py `ENABLE_FAMILY_CAP=False` + `FAMILY_CAP_TOP_K=2` / feature_flag_service.py 双文件 / evaluation.py integration 在 R1a/R5 hook 之后 + return 之前(soft-fail 不阻塞 round,标 quality_status='FAIL' + _r10_family_cap_dropped=True + _r10_family_cap_top_k=K) / tests/integration/test_family_cap.py 13 tests cover signature canonicalization (same/different/order/empty/case) + cap basic + multi-pillar isolation + composite-score priority + edge cases (empty/group<=K/top_k=0→1/top_k=1/sorted return)。**Verification**:13/13 R10 + 108/108 累计 PASS(24 R5 + 13 R10 + 71 prior),Default flag OFF zero behavior change。**GO 闸门(plan §9.4 Phase 2 KPI R10 portion)**:`COUNT WHERE metrics->>'_r10_family_cap_dropped' = 'true' ≥ 5` + pillar 分布不偏。**Rollback**:flag flip OFF (< 1 min) OR FAMILY_CAP_TOP_K=5 放宽(0 impl 改)。R10 默认 OFF,production user 决定何时翻;R5 field fix 自动生效(在 ENABLE_LLM_JUDGE ON 路径)。**剩余 Phase 2**:R7 self-correct 半接受 (1 人日) / R6 DAG (3 人日,最大架构改) / Q9 Decayed Alpha seed (1-2 人日)|
| 2026-05-18 | v1.11 | **Phase 2 R5 LLM judge ship + production activation**(同 session 推进 Phase 1.5 全 ship 后 Phase 2 第一任务)。Plan `~/.claude/plans/phase2-r5-llm-judge-2026-05-18.md` v1.0(1053 行 / 3 轮 fresh agent review / 0 outstanding MUST / 3.5 人日 estimate);scope crisp + session 节奏好 → 一次 commit ship 而非 3 PR 拆。commit `762888b` master(848 insertions / 8 files / 24 R5 tests + 71 regression PASS):`backend/config.py`(`ENABLE_LLM_JUDGE` default OFF + `R5_JUDGE_MODEL=haiku-4-5-20251001` + `R5_JUDGE_LOW_CONF=0.55`) + `backend/services/feature_flag_service.py`(双文件注册) + `backend/models/r1a_attribution.py`(10 r5_* cols + 2 indexes) + `backend/alembic/versions/7e9f2a1b3c4d` head applied + `backend/agents/prompts/r5_alignment.py`(`R5_C1/C2_SYSTEM` + `build_r5_c1_prompt` / `build_r5_c2_prompt` strict-JSON 2000-char 截断) + `backend/agents/graph/r5_judge.py`(provider rate cost table / **[V1.0-A1-2]** empty desc 跳 / per-call try/except / `_composite_score` Eq. 7 / `_derive_attribution` 4-case rule) + `backend/agents/graph/nodes/evaluation.py`(R5 hook INSIDE R1a loop @ L2609 post-R1a + **[V1.0-A2-3]** R5 wins overwrite + r5_agrees_r1a 捕获原 R1a verdict + per-alpha try/except 守) + `backend/tests/integration/test_r5_judge.py`(24 tests cover math / decision matrix / cost / parsing / 6 integration mocks)。**Production verify 2026-05-18 ~01:55 UTC**:DB override INSERT + backend+celery 重启 + 18 flags loaded(ENABLE_LLM_JUDGE eff=True)+ task 1463 (USA FLAT, datasets=[pv1]) 进 EVALUATE 节点接 R5 hook 跑 c1+c2 写 10 r5_* 列(首批数据 ~10 min 因含 flip-retry 慢 BRAIN sim)。**GO 闸门(Phase 2 → 3 R5 portion)**:≥ 100 alphas 双 c1+c2 非空 / attribution distribution 移动 ≥ 10% vs R1a-only 273 行 baseline / 平均 cost ≤ $0.05/call(haiku-4-5 med ~$0.01,under 5×) / 0 production crashes(per-alpha try/except 守)。**Deferred**:THINKING_EFFORT_OVERRIDES r5_alignment_c1/c2 显式默认(plan §2.4 instance-level "med" 已足) / Phase 2 R6 DAG (3 人日,最大架构改) / R7 self-correct 半接受 (1 人日) / R10 family-cap (1 人日) / Q9 Decayed seed (1-2 人日)|
| 2026-05-18 | v1.10 | **Phase 1.5-C cut-over backend ship + production verify**(user 决 `不等 2026-05-24 Phase 1 flag ≥7d 直接推进`,override plan §9.3 (0) SQL hard guard;风险通过技术 guard + dual-write 兜底 + 0 regression + 实证双路任务 1450/652 收敛而非 obs 数据量)。commit `b6a04b7` master(406 insertions / 7 files / 8 new tests + 63 regression PASS):`backend/config.py` + `backend/services/feature_flag_service.py`(双文件 flag `ENABLE_TASK_SCHEMA_V2`,default OFF) + `backend/tasks/mining_tasks.py`(`_is_cascade_schedule` + `_resolve_cascade_phase` 2 dual-source helpers + dispatch L247 + cascade resume L1485 切 v2 path) + `backend/tasks/session_watchdog.py`(cascade probe + discrete probe Python guard + **[V1.2-B3]** cascade revive 新 ExperimentRun inherit `runtime_state={current_tier, round_idx}` 从 prior_run.runtime_state 避免 Revision D 删 cascade_phase 时静默丢 T2/T3 progress) + `backend/services/task_service.py`(**[V1.2-C5]** MiningSessionInfo dataclass 加 schedule/starting_tier/current_tier 3 Optional 字段 + `_to_session_info` 填充) + `backend/routers/mining_session.py`(MiningSessionResponse Pydantic + `_info_to_response` 透传)。**Production verify 2026-05-18 01:35 UTC**:DB override INSERT + backend+celery 重启(venv) + 17 flags loaded(`ENABLE_TASK_SCHEMA_V2 effective=True`)+ task 1450 (EUR FLAT) POST `/ops/start-flat-session` → `_is_cascade_schedule` 在 flag ON 下读 schedule='ONESHOT' → False → 走 FLAT 分支 0.25s 一轮 iteration COMPLETED + task 652 (USA cascade PAUSED, 1.5-B backfilled) GET `/mining-session` 返回 `schedule=CASCADE starting_tier=1 current_tier=1` 新字段全 populated。**Deferred**:Frontend 6 sites adapt(plan §3.3 V1.2-B1,~1.5 人日)/ `routers/tasks.py` TaskResponse 3 字段 / Revision D 删 legacy cols(Phase 3 R1b 范围)|
| 2026-05-18 | v1.9 | **flat-F1 advanced kickoff ship + 同日 production verify**(原 §4.5 Phase 3 timeline 中的 flat-F1 提前至 Phase 1.5 Block 1 后,user 询问"可以把 flat 提前吗"后 4 轮 fresh adversarial plan 收敛)。Plan: `~/.claude/plans/flat-F1-kickoff-2026-05-18.md` v1.5 SHIP-READY(v1.0 → v1.1 self / v1.1 → v1.2 fresh agent #1 / v1.2 → v1.3 fresh agent #2 + Q1 resume / v1.3 → v1.4 fresh agent #3 round 4 抓 2 NEW blocking MUST / v1.4 → v1.5 user Q1=V2 + Q2=A 决策)。**Cumulative 12 MUST + 16 SHOULD inline-fixed + 2 v1.4-MUST user-resolved + 0 outstanding**。**§4.5 提前实施**:commit `5a1634c` master(666 insertions / 6 files),`backend/config.py` + `backend/services/feature_flag_service.py`(双文件 flag) + `backend/services/task_service.py`(Q1 V2 `_dispatch_session_worker(*, inherit_runtime_state=False)` param + Q2 A `intervene_task` FLAT guard + `start_flat_session`/`resume_flat_session` 服务方法) + `backend/tasks/mining_tasks.py`(FLAT dispatch branch + `_run_flat_iteration` 主循环)+ `backend/routers/ops.py`(`POST /ops/start-flat-session` + `POST /ops/flat-sessions/{id}/resume` admin endpoints,X-Ops-Token + X-Ops-Actor 鉴权,start 含 `ENABLE_FLAT_CONTINUOUS` gate) + `backend/tests/integration/test_flat_continuous.py`(7 tests live PG fixture 全 PASS)。**关键设计 v1.9 lock**:(a) FLAT_CONTINUOUS 第二个 mining_mode 与 legacy CONTINUOUS_CASCADE 并行(§6 D3 "保留 legacy 渐进切换");(b) `ENABLE_FLAT_CONTINUOUS` 默认 OFF — flat-F2 后续 PR 翻默认值,F1 零行为变化;(c) Q1 V2 cursor 持久化在 `experiment_runs.runtime_state["flat_cursor"]`,resume 时 `inherit_runtime_state=True` 复制到新 ExperimentRun 保 cursor 跨 pause-resume 不重置;(d) Q2 A `intervene_task` 对 FLAT PAUSE/RESUME 抛 400(legacy 路径不 dispatch worker → 会卡 RUNNING-with-no-worker)— 路径分流明确;(e) 不动 default `mining_mode`(flat-F2 PR)/ 不替 T2 wrapper sweep(flat-F3)/ 不删 cascade(flat-F4)。**Production verify 2026-05-18 00:52-00:57 UTC**:POST `/ops/start-flat-session`(USA, datasets=[pv1, fundamental6])→ task **1443** RUNNING / FLAT_CONTINUOUS;celery solo worker pickup → `_run_flat_iteration` 主循环;4 min 内 iter 1 complete + iter 2 已起;**Q1 V2 cursor 持久化 LIVE** runtime_state={flat_cursor:1, flat_iterations:1};**R1a hook LIVE** r1a_attribution_log 261→**267 (+6)**;**DirectionBandit LIVE** direction_bandit_log 4→**5 (+1)**;trace 完整 RAG_QUERY×2 / HYPOTHESIS / STRATEGY_SELECT / TIER_WRAP / VALIDATE / SIMULATE / EVALUATE / HYPOTHESIS_FEEDBACK / ROUND_SUMMARY / SAVE_RESULTS / DISTILL_CONTEXT(iter 2);brain_role_snapshot 自动 stamped USER mode。**Phase 1 obs 现在有第二条连续任务源**(独立于 cascade,可同区不同 dataset 多并行)|
| 2026-05-17 | v1.8 | **Phase 1.5 Block 1 4/4 ship** (plan v1.3 §1-§5 全实施;Block 2 phase15-C 等 Phase 1 obs 闸门)。同 session plan v1.3 settled 后即接力,4 commits master:`9252fde` (Revision A) / `52ef64d` (Revision B) / `eb3a9c1` (Pydantic TaskConfig) / `37ff7bf` (Fields)。alembic head=`3b1c4e5d6a78` (Revision B);production verified 227 mining_tasks + 227 latest experiment_runs backfilled(205+14+6 ONESHOT + 2 CASCADE / current_tier 1/2/3 分别 206/15/6)。**plan v1.3 v1.2 5 MUST 全 inline 实证生效**:(a) V1.2-B4 dual-default(Python `default=` + DB `server_default=`)消除 21 test fixture 文件 0 修改 churn;(b) V1.2-B2 `_stamp_heartbeat` 从 bulk SQL `update(MiningTask)...values()` 重构为 instance-level mutation + ExperimentRun.runtime_state 双写,split-brain 风险消除;(c) V1.2-B3 cascade revive 继承 待 phase15-C 实施(本 Block 1 不触);(d) V1.2-B5 sub-model `extra='allow'` 双显式(BrainRoleSnapshot/ContextualBanditState/WatchdogReviveInfo 全显式 ConfigDict),Pydantic v2 默认 'ignore' silent-drop bug 防;(e) V1.2-C3 conftest.py `pytest_collection_modifyitems` hook 让 `requires_postgres` mark 在无 PG_TEST_DSN 时正确 skip。**MF-V1.4 4 schema-migration trap 实证**:(1) JSONB `server_default=sa.text("'X'::jsonb")` 强制 ::jsonb cast asyncpg 验证;(2) Alembic-formalize Phase 1 `direction_bandit_log` + `ast_distance_log` 用 `inspector.has_table()` guard 让 dev DB 已有表的情况 idempotent;(3) `last_alpha_persisted_at` 保留 `MiningTask` 上(MF-V1.4-4)避 watchdog N+1 join;(4) cascade phase advancement 3 sites 全 dual-write current_tier 1/2/3 + flag_modified。**Tests 累计 28 new + 100+ regression PASS**(test_phase15_a_upgrade_downgrade 13 / test_phase15_b_backfill 16 / test_phase15_pydantic_task_config 15 / test_phase15_field_consolidation 16)。**Block 2 phase15-C 前置硬依赖**:`§9.3 (0)` SQL 必须 ≥3/4 Phase 1 ENABLE flag(R2/Q7 / R3/Q8 / R4' / Q2 ANCHOR)ON 且 updated_at < now() - interval '7 days'。今日 Phase 1 flag 全 default OFF + DB feature_flag_overrides 0 行(verified)+ direction_bandit_log + ast_distance_log 各 0 rows → 需 user 在 production flip 至少 3 flag + 等 ≥ 7 天才能启 Block 2 |
| 2026-05-17 | v1.7 | **Phase 1.5 plan v1.3 ready(0 outstanding MUST)+ Block 1/Block 2 split lock**(实施推下 session)。同 session Phase 0 + Phase 1 全 ship 后即设计 Phase 1.5,Plan 文件 `~/.claude/plans/phase15-kickoff-2026-05-17.md` 1756 行 / 3 轮对抗审查:**v1.0 → v1.1** original Plan agent self-audit(7 MUST + 6 SHOULD)/ **v1.1 → v1.2** fresh independent Plan agent 抓 5 新 MUST(B1 frontend 6 sites scope underestimate / B2 _stamp_heartbeat bulk SQL vs instance-level split-brain / B3 cascade revive current_tier 静默 restart real bug / B4 17 test fixture NOT NULL pre-flight / B5 sub-model Pydantic extra='allow' lockdown)+ 5 新 SHOULD / **v1.2 → v1.3** integrate + restructure + self-audit 0 new MUST。**Cumulative** 12 MUST + 17 SHOULD inline-fixed + 9 NICE deferred + **0 outstanding MUST**。**§4.3 工程量 v1.3 上调**:A 2 + B 3 + C 3 backend + **1.5 frontend = 9.5 串行人日**(frontend +0.5 per V1.2-B1);Pydantic 2 + Fields 2 = 4 并行人日;**总 ~13.5 effective**。**§4.3 Block 1/Block 2 split lock**:phase15-A/B/Pydantic/Fields 4 PR 并行启(零 Phase 1 obs 依赖);phase15-C 单 PR 受 `§9.3 (0)` SQL precondition guard — Phase 1 4 flag(R2/Q7 + R3/Q8 + R4' + Q2 ANCHOR SQL filter)中 **≥3 ON ≥7 天**才允许 cut-over verify pass(per V1.2-C2 技术 guard 升级,不再仅 SOP)。**§4.3 GO 闸门 v1.3 修正**:`baseline.json` 无 alpha 指标变化 → `kb_total_entries` growth rate stable(V1.2-C1 — baseline.json 指标实测与 cascade scheduling order 无关,GO 闸门冗余)。**关键设计 v1.3 lock**:(a) phase15-A Alembic-formalize Phase 1 2 dedicated tables(direction_bandit_log + ast_distance_log);(b) `last_alpha_persisted_at` 保留 MiningTask 列双写 runtime_state(MF-V1.4-4 — 避免 watchdog join experiment_runs N+1);(c) `_stamp_heartbeat` bulk SQL 重写 instance-level(V1.2-B2 split-brain fix);(d) cascade revive 继承 prior_run.runtime_state.current_tier + cascade_round_idx(V1.2-B3 real bug fix);(e) 所有新 NOT NULL column 必 dual-default(Python default= + DB server_default=text("'X'::jsonb"))消除 21 test file fixture churn;(f) Pydantic sub-model(BrainRoleSnapshot / ContextualBanditState / WatchdogReviveInfo)全 explicit `extra='allow'` lockdown 防 Phase 3+ 静默丢 key;(g) conftest.py `pytest_collection_modifyitems` hook 让 `requires_postgres` mark 实际生效。**Phase 1.5 实施下 session 起 Block 1 4 PR 并行**,Block 2(phase15-C)等 Phase 1 production 观察期 ≥3/4 flag ≥7 天 ON 后启 |
| 2026-05-17 | v1.6 | **Phase 1 7/7 完成 ship + GO 闸门 PASS**。同 session Q6 + R3/Q8 接力 ship,Phase 1 全 7 任务完成。**§4.2 Q6 实证**:openassetpricing repo 实测 193 Predictors 后,Alpha191 JoinQuant 191 alphas → 137 翻译率 71.7%(plan §5.2 估 30-50,远超 — 因复用 Q3 qlib_translator + 25 UPPERCASE Alpha191 alias)→ net 131 imports(6 dedup vs Q1/Q3 pattern_hash)。**§4.2 R3/Q8 实证**:brute-force O(n²) AST Jaccard subtree overlap 落地,AIAC AST n<20 at max_depth=3 → 1280 ops/alpha 可忽略 perf。Shamir-Tsur 1999 弃用(plan v1.4 引用复杂度 `O(n²·⁵/log n)` 不严谨)。light wiring 至 generation node_code_gen 末,flag-gated INSERT 至 ast_distance_log 独立表(per R1a v1.6 lesson)。**§11 GO 闸门 PASS**:KB DB rows = 3224 ≥ 3100(v1.5 recalibrated)✓;ACADEMIC_PATTERNS = 263 ≥ 256 ✓;3 Phase 1 ENABLE flag 全双文件注册 ✓;2 dedicated tables(direction_bandit_log / ast_distance_log)metadata.create_all ready ✓。**Phase 1 ship 后 9 commits 全列表**:`04826af` (Q4+Q5+R4') / `ab6c636` (R2/Q7) / `65bccd3` (Q2 infra) / `5102f3a` (Q2 model id hotfix) / `2b907cd` (Q2 v1.4 strict_field_check) / `7458ea2` (Q2 batch 193) / `873214b` (master plan v1.5) / `34f695a` (Q6 131 rows) / `73c7a65` (R3/Q8 + log table) on master。**生产观察期 pending(Phase 1.5 kickoff 前置)**:R2/Q7 bandit ≥1周生产 + 任一arm≥30 select + 任一segment≥10 select / R3/Q8 ast_distance_log ≥1000 rows + std>0.1 / R4' dual-channel render 可见;3 flag 应按 SF-V1.2-A SOP **non-overlap flip**(R4' 先 → R3/Q8 → R2/Q7,每个独立观察 ≥100 round 数据)|
| 2026-05-17 | v1.5 | **Phase 1 partial ship + 实证修正**(集中记录,详细 6 commits + 3 plan version + 12 MUST + 8 SHOULD fix 见 `~/.claude/plans/phase1-kickoff-2026-05-17.md` v1.1-v1.3 changelog)。**§4.2 Q2 数据源 / 工程量类**:openassetpricing 实测 193 Predictors(plan §4.2 估的 300 是 outdated),Q2 工程量 8 人日 ship(plan §4.2 估 2 人日,user 选 v1.2 "full LLM 预译" 后上调到 7.5 人日,v1.3 加 0.5 人日 cost calibration)。**§4.2 R2/Q7 算法类**:user 选 contextual TS 而非 vanilla(plan §4.2 字面对应),`(region, dataset_category, recent_failure_pattern)` 3 维 segment + Beta(α,β) per (segment,arm) + cold-start global-prior fallback at threshold 5 + segment_id 字符串拼接(MF-V1.2-4 防 Python hash 跨进程不稳定);工程量 3-4 人日 → 6 人日。**§11 GO 闸门类**:`KnowledgeEntry DB rows ≥ 3550` 基于 Q2 estimate 300 calibrated,实际 193 → **下调建议 ≥ 3100(已达 3093 + Q6 ship 后 3128-3143 ≥ 3150)**。**§4.2 Q2 paradigm-mismatch fallback**:openassetpricing 是 per-stock-month signal + Python source(可 LLM 翻译,plan v1.1 "ANCHOR_METADATA-only" 判断过保守),dual-path 实证 119 SUCCESS_PATTERN + 74 ANCHOR_METADATA(61.7% 翻译率,在 plan §4.10 50-70% 区间内)。**§4.2 LLM call 实证**:Opus 4.7 high thinking 单 call $0.40(plan v1.2 估 $0.20 / v1.3 估 $0.75 — 实际中位),193 calls = $75。**Phase 1 实施 lesson(v1.4 hidden)**:(a) `AlphaSemanticValidator` default `strict_field_check=True` 在 dev 环境 DataField catalog 未 sync 会把所有 LLM-valid 字段名 hard-fail → offline 翻译 pipeline 必传 `strict_field_check=False`(commit 2b907cd Q2 v1.4 hotfix);(b) Anthropic Opus 4.7 model id 是 `claude-opus-4-7`,**无 date suffix**(只有 Haiku 4.5 是 dated)— 我 hallucinated `claude-opus-4-7-20251022` hotfix 后 commit 5102f3a;(c) `_evolve_strategy` API 通过 kwargs `task=task, round_alphas=alphas` 接收 in-memory alphas,绕开 R1a v1.6 lesson(don't DB-query for round_alphas)。**Phase 1 partial ship 状态(2026-05-17)**:6 commits `04826af` (Q4+Q5+R4' trivial bundle) / `ab6c636` (R2/Q7 Contextual TS + DirectionBanditLog table) / `65bccd3` (Q2 infra) / `5102f3a` (Q2 model id hotfix) / `2b907cd` (Q2 v1.4 validator + candidate_expr) / `7458ea2` (Q2 full batch 193 rows + KB import) on master;KB 2900 → 3093(+193 active rows);6 ENABLE flag 注册总(R1a Phase 0 + R4'/R2-Q7 Phase 1 新增 2);剩余 Q6 Alpha191 CHN seed(2 人日)+ R3/Q8 AST distance brute-force O(n²) light wiring + ast_distance_log(3 人日)推下 session 完成 Phase 1 7/7 |
| 2026-05-17 | v1.4 | **Phase 0 实施 + ship 后实证修正**(集中记录,21 项细节见 `~/.claude/plans/docs-master-implementation-plan-2026-05-compressed-shore.md` §6 + §11 v1.1-v1.5 changelog)。**§4.1 工程量类**:R1a 2 → 1.3 人日 + 观察期改数据量门槛 ≥200 hook 触发(非 14 天日历)、Q1 1 → 2 人日(漏算手工转写 + audit)、Q3 2-3 → 2.8 人日(v1.5 单版本简化后)。**§4.1 GO 闸门类**:R1a hook ≥50 → ≥200 升级统计意义;ACADEMIC_PATTERNS ≥256 / DB ≥280 保持(Q3 ship 后实测 263 / 2864 远超);ops dashboard 28+9 → 38+13。**§4.1 数据源 / 方法类**:Q3 数据源方案 B(`qlib.contrib.data.handler.Alpha158` + 静态 JSON commit + pyqlib 不进 requirements)、Q3 双版本(v1.5 实证 operators.level='ALL' 不区分 role 后简化为单版本 + `pattern_operators` 元数据钩子)、Q1 dedupe 改用 pattern_hash + 修一个 latent missing-hash bug(MF-8)、R1a hook 同步直接调用(Pydantic field,无 flag_modified 也无 executor)、R1a 监控走独立 `scripts/r1a_attribution_report.py`(不进 ops dashboard)。**§4.1 audit / 新增依赖类**:Q1 已 import patterns audit(实际 10 条 + 5 条,plan v1.1 写 24 错)、Phase 1 R8 RAG 新前置依赖(`requires_role` 元数据 + COALESCE filter)、BRAIN 算子集 user/consultant 在代码层**无隔离**(operators.level 字段全 'ALL',role 限制在 sim-time 由账号凭据决定)。**§13 杂项**:main → master(repo 无 main 分支)、`ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` / `HYPOTHESIS_CENTRIC_LEVEL=2` 状态澄清(代码 default 是 False/0,通过 DB FeatureFlagOverride ON)。**Phase 0 实施 lesson(v1.4 hidden)**:新 `ENABLE_*` flag 必须**同时**改 `backend/config.py` + `backend/services/feature_flag_service.py:SUPPORTED_FLAGS` 两文件,否则 `_load_overrides_into_cache:340` silent ignore 为 orphan,flag flip 无效 — 三轮对抗审查都没抓到这个 silent-failure 模式(commit `520a0d9` hotfix)。**Phase 0 ship 状态**:6 commits `b4872b6` (R1a) / `520a0d9` (flag hotfix) / `56bd43c` (Q1 infra) / `ebc742f` (Q1 100 patterns) / `5c4fd62` (Q3 translator) / `9df61b5` (Q3 158 patterns) on master;KB seed 59 → 2864(48× 增长);ACADEMIC_PATTERNS list 263 ≥ 256 GO;R1a flag flipped DB override 2026-05-17 04:10 UTC;剩余 R1a ≥200 触发观察期(数据量驱动,日历不定) |
| 2026-05-17 | v1.3 | 第三轮对抗审查 fixes:**MUST FIX 3 项**(NMF-1 §4.3 Phase 1.5 标题 2.5 周 → 4 周、NMF-2 §3.2 Phase 1.5 行 2.5 周 → 4 周、NMF-3 §9.3 KPI 标题 2.5 周 → 4 周;均为 v1.1 IM-2 "灰度 1 → 2 周" 的连锁漏改 — Python 实测 Phase 1.5 跨 28 天 = 4 周)+ **SHOULD FIX 1 项**(NSF-1 §3.2 总日历 ~22 周 → ~23 周,Python 实测 162 天 = 23.1 周)。**审查停止线**:第三轮发现仅 ±1 周精度 nit,继续修不会改变实施价值;剩余风险待实施时实测暴露 |
| 2026-05-17 | v1.2 | 第二轮对抗审查 fixes:**MUST FIX 3 项**(NMF-1 §6.1 R1b ship 日期 10-05 → 9-28 数学修正 + 稳定期连锁 11-02 → 10-26 / phase15-D 11-03 → 10-27、NMF-2 §3.1 加 phase15-Schema + phase15-Fields 两项 + 统计 27 → 29 项、NMF-3 §4.3 标题 8 → 9 人日 + §4.5 标题 4-8 周 → ~12 周)+ **SHOULD FIX 4 项**(NSF-1 §3.2 总计 50-60 → 73-98 人日单位统一、NSF-2/3 Phase 1 11-15 → 12-17 人日、NSF-4 §9.2 KPI evolution_strategy 加 backend/agents 前缀)+ **内部矛盾 2 项**(NIM-1 §7 风险表 R2/Q7 触发条件口径与 §4.1 KPI ≥ 50 统一、NIM-2 §3.1 表头 "替代废弃" → "主线之外的备用项 含 R1c NO-GO 备用")。注:本次 fix 起源于 v1.1 修复活动本身漏改 cross-reference(局部 fix 时 §X 数字改了 §Y 引用未跟进)|
| 2026-05-17 | v1.1 | 对抗审查 fixes:**MUST FIX 4 项**(MF-1 `evolution_strategy.py` 路径在 `backend/agents/` 不在 `backend/`、MF-2 `evaluation.py:2554` 越界,真实位置 `:2538` + caveat、MF-3 KPI 术语区分 `ACADEMIC_PATTERNS` list 长度 vs `kb_total_entries` baseline metric vs `KnowledgeEntry` DB row 数、MF-4 D6 "73% historical" → "task 652 derived 73%")+ **SHOULD FIX 6 项**(SF-1 `selection_strategy.py` class 在 `:59`、SF-2 R1a 触发门槛 ≥ 100 → ≥ 50 数据驱动、SF-3 Phase 3 多项 KPI 加"无 baseline" caveat、SF-4 §6.1 时间表数学一致化 R1b 4-6 周 + buffer、SF-5 D5 加 Q4/Q5/Q6 rationale、SF-6 D17 挪到 §0.3 锚点)+ **内部矛盾 3 项**(IM-1 R2/Q7 工程量 2 → 3-4 人日、IM-2 Phase 1.5 灰度 1 → 2 周 + Phase 2 kickoff 推到 2026-07-28、IM-3 §0.2 v1 闸门描述消歧)+ **缺漏 2 项**(MS-1 phase15-C 加 frontend 1 人日 → §3.2 Phase 1.5 总 8 → 9 人日、MS-2 R1c 描述放缓 "已放弃" → "作为 NO-GO 备用") |

---

*本文档由 2026-05-17 整合 4 份调研 / 设计 / 实测文档自动产出。源文档保留作为细节参考(file:line / Alembic SQL / Eq 公式 / 代码块在源文档)。实施 R/Q/phase15/flat 任务时 cross-reference 对应源文档。本文档承担战略路线职责,不重复源文档技术细节。每个 Phase / 任务独立 PR,可独立回滚。*
