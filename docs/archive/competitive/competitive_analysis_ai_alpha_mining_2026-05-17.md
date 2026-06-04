# 竞品分析 — 顶级 AI Alpha 挖矿系统对比

> **文档日期**:2026-05-17
> **来源**:对抗性思考 + 学界论文 + AIAC 跑量实测对照
> **关联文档**:
> - [`phase15_task_schema_refactor_plan.md`](phase15_task_schema_refactor_plan.md)(本文 §3-§4 是 phase15 v2 §12 的独立版,可单独引用)
> - [`rd_agent_alpha_gpt_research_2026-05-16.md`](rd_agent_alpha_gpt_research_2026-05-16.md)(深入的 RD-Agent / Alpha-GPT 单系统调研)
> - [`qlib_alpha_research_2026-05-16.md`](qlib_alpha_research_2026-05-16.md)(Qlib + Alpha101/158/360 + 2024-2026 LLM-alpha 学术调研)
> **决策影响**:为 phase15 §13 flat search 完整设计 + §14 R1a 启用提供学术 / 工业先例论据

---

## 1. 关键发现

**几乎没有任何主流 AI alpha 挖矿系统采用 cascade T1/T2/T3 分层** — AIAC 是孤例。学术 SOTA + 工业实践都收敛到 **flat hypothesis-driven + 演化/bandit 调度** 架构。

具体三条:
- 13 个学界 / 工业系统对照,**0 个使用 tier phase 切换**
- AIAC `agents/core/` 已有 3223 行 RD-Agent 兼容代码 DORMANT — 学界 SOTA 路径其实在自家代码库里
- 工业派(Citadel / Renaissance / Two Sigma)CTO 明确反对 PM 把判断外包给 LLM,LLM 仅做 research assistant

---

## 2. 调研方法

| 信息源 | 数量 | 来源 |
|---|---|---|
| 学界论文 | 9 | arxiv / NeurIPS / KDD / EMNLP / ACM |
| 工业实践 | 3 | Citadel / JPMorgan / Renaissance(公开信息)|
| 开源实现 | 5 | microsoft/RD-Agent / microsoft/qlib / QuantaAlpha/QuantaAlpha / RndmVariableQ/AlphaAgent / 等 |
| AIAC 自调研 | 3 docs | 本仓库 docs/ 下 alphagbm / qlib / rd_agent 三份 |

---

## 3. 主流系统架构对比矩阵

| 系统 | 年份 | 生成机制 | 调度/分层 | 反馈机制 | 抗 decay | 状态 |
|---|---|---|---|---|---|---|
| **AIAC**(当前)| 2026 | LLM + typed hypothesis | **cascade T1/T2/T3 phase 切换** | self-correct + KB | negative_knowledge + pillar | 生产中 |
| **RD-Agent-Quant**(MSRA, NeurIPS 2025)⭐ | 2025 | LLM 生成假设森林 → Co-STEER DAG 代码演化 | **flat + multi-armed bandit 调度方向**(无 tier) | bandit reward(实际通过率) | 70% 因子精简 | 学术 SOTA,2.5× ARR vs Alpha158 |
| **Alpha-GPT** v1/v2(HKUST, 2023-2025)| 2025 | LLM seed + **GP 邻域演化** | **3 阶段 flat**:Ideation → Impl → Review;4 层 Hierarchical RAG | natural language analyst | 多轮 human-in-loop | EMNLP 2025 Demos / IQC 2024 top-10 |
| **AlphaAgent**(KDD 2025)| 2025 | LLM flat 生成 | **flat + 三正则化**(AST 相似 / 假设对齐 / 复杂度)| 单流回测 | **AST subtree isomorphism 原创度门** | KDD 2025 接收,IR=1.488 |
| **Hubble v2**(arxiv 2604.09601, 2026-04)| 2026 | LLM + DSL flat | **flat + Family-cap top-k=2** | dual-channel RAG | **negative-channel "avoid like" 模板** | 学术,与 AIAC 80% 重合但更严 |
| **QuantaAlpha**(arxiv 2602.07085, 2026-02)| 2026 | LLM + **trajectory-level mutation/crossover** | flat **进化** + 轨迹拓扑 | trajectory replay | semantic consistency + crowding 控制 | 学术新作 |
| **Chain-of-Alpha**(arxiv 2508.06312)⚠️ | 2025 | **dual-chain**:Generation + Optimization | 两链迭代 generate→evaluate→refine | backtest + prior knowledge | — | **已撤稿**(审稿争议)|
| **AlphaEvolve** | — | **GP + 参数学习 + 矩阵运算** | flat | GP fitness | 内置 | 非 LLM 流派 |
| **AlphaGen**(2023)| 2023 | **DRL** formulaic mining | 单 agent flat | combination model reward | RL exploration | 早期 baseline |
| **Navigate Alpha Jungle**(arxiv 2505.11122)| 2025 | LLM + **MCTS** | tree search | UCB rollout | tree pruning | parallelizability 弱 |
| **AlphaSAGE** | 2025 | **GFlowNet** | flow-based sampling | flow consistency | inherent diversity | 实验性 |
| **Alpha-R1**(arxiv 2512.23515)| 2025 | LLM reasoning + RL screening | RL post-hoc filter | reward model | — | 实验性,做 screening 而非 mining |
| **Citadel/Renaissance/Two Sigma**(工业)| — | **未公开使用 LLM 做 alpha generation** | — | — | — | CTO 立场:LLM 仅做 research assistant |

---

## 4. AIAC 在矩阵中的位置 — 异类

| 维度 | AIAC cascade | 主流竞品 |
|---|---|---|
| 分层切换 | **T1→T2→T3 phase 机械切换**(基于 round budget + MIN_TIER_SEED_COUNT 门)| 全部 flat;调度靠 bandit / GP / MCTS / trajectory mutation |
| wrapper 处理 | T2 phase 才加 wrapper("后补"二等公民)| LLM 一次生成完整 alpha(含 wrapper),不分阶段 |
| hypothesis 与 tier 关系 | **正交两套系统**(typed hypothesis 不知道自己被哪个 tier 处理)| **统一**:假设森林 + bandit 选下一个挖什么(RD-Agent)|
| 软停机制 | run 跑一段就 mark PAUSED 等人手 resume | 持续后台跑(RD-Agent 单实验 < $10 全自动)|
| 学界先例 | **0**(没有任何学术论文采用 T1/T2/T3 cascade)| 假设森林 / dual-chain / trajectory mutation 都有论文支撑 |

---

## 5. 反 cascade 硬证据

### 5.1 学术撤稿
- **Chain-of-Alpha 因 dual-chain 设计争议被撤稿**(arxiv 2508.06312 v2)— 学界对"两阶段流水线"持保留。AIAC T1/T2/T3 三阶段比 dual-chain 更激进,理论风险更高

### 5.2 数据反驳
- **RD-Agent 用 22-26 因子达到 14.21% ARR**(Alpha158 158 因子 5.70% ARR)— 验证 **flat + 假设驱动 + 精简** 优于 **分层 + 暴力穷举**
- **AIAC task 652 跑量实测**(2026-05-16 12:20-14:12 UTC):7/7 derived alpha 来自 2 个 parent 同源失败 — cascade T2 wrapper sweep 是盲目穷举的工程实证。Parent 7820 衍生 5 个 group_* wrapper(`group_scale/sector / industry / subindustry`、`group_neutralize/sector`、`group_rank/subindustry`)**全部 LOW_SUB_UNIVERSE_SHARPE FAIL**。5 个 BRAIN sim 浪费在用 group_* 救一个结构性死掉的 base signal

### 5.3 自家代码反证
- AIAC `backend/agents/core/` 已有 **3223 行 RD-Agent 兼容代码**(Hypothesis / Experiment / EvoStep / Trace / EvolvingKnowledge / Feedback),**生产路径 0 调用**(grep `from backend.agents.core` in `backend/agents/graph/ ...` = 0 matches)
- 学界 SOTA 路径其实就在自家代码库,只是 DORMANT — 切换的工程量比从零写小 5×

---

## 6. 顶级竞品共性 — reference architecture

RD-Agent + AlphaAgent + Hubble v2 共同特征:

| # | 共性 | RD-Agent 实现 | AlphaAgent 实现 | Hubble v2 实现 |
|---|---|---|---|---|
| 1 | **假设作为一等公民驱动调度** | hypothesis forest + bandit | 假设对齐正则化 | DSL + 假设映射 |
| 2 | **flat 生成 + 演化优化** | Co-STEER DAG | LLM flat + AST 子树相似度 | DSL flat + 拓扑校验 |
| 3 | **反 decay 用正则化 / 拥挤防御** | 因子精简 | 三正则化(原创/对齐/复杂度) | family-cap top-k=2 |
| 4 | **持续运行无需人手 resume** | trajectory 自动闭环 | 单实验完整跑完 | 持续生产模式 |
| 5 | **单次实验成本 < $10** | $10(RD-Agent 公开)| — | — |
| 6 | **AIAC typed hypothesis 已有但被 cascade tier 架空** | — | — | — |

---

## 7. 工业实践 reality check

- **Citadel CTO 明确反对 PM 把判断外包给 LLM** — LLM 仅做 research assistant([eFinancialCareers 报道](https://www.efinancialcareers.com/news/hedge-fund-citadel-hired-goldman-sachs-code-cracking-quant-md-for-its-power-trading-team))
- **JPMorgan LLM Suite** 覆盖 20 万员工,**未公开用于 alpha generation**
- **Renaissance / Two Sigma** 无 LLM 内部使用公开披露
- **学术与工业 gap 明显** — 学术全跑 flat + LLM,工业 conservative

**对 AIAC 的意义**:走学术 SOTA 路径(flat + hypothesis-driven),但保留 human review gate(P3 ops console 已有),既能 keep up with research frontier 又有工业一致的安全网。

---

## 8. 学术分类(轴向归纳)

### 8.1 按生成机制

| 机制 | 代表 |
|---|---|
| LLM-only flat | AlphaAgent, Hubble v2 |
| LLM + GP | Alpha-GPT, AlphaEvolve |
| LLM + 假设森林 | RD-Agent |
| LLM + trajectory mutation | QuantaAlpha |
| LLM + MCTS | Navigate Alpha Jungle |
| LLM + dual-chain | Chain-of-Alpha(撤稿)|
| 纯 DRL | AlphaGen |
| GFlowNet | AlphaSAGE |
| LLM-cascade tier | **AIAC**(孤例)|

### 8.2 按调度机制

| 调度 | 代表 | AIAC 现状 |
|---|---|---|
| Multi-armed bandit(direction) | RD-Agent | 未用(`agents/core/` 已有 Contextual Thompson Sampling, DORMANT)|
| Bandit(dataset) | — | ✅ 已用(`selection_strategy.py:135` UCB1)|
| Hypothesis forest + bandit 融合 | RD-Agent | 未用 |
| AST 相似度正则化 | AlphaAgent | 未用(R3 路线图)|
| Family-cap top-k | Hubble v2 | 未用(R10 路线图)|
| **cascade tier phase** | **AIAC** | **孤例,无学界支持** |

### 8.3 按反馈机制

| 反馈 | 代表 | AIAC 现状 |
|---|---|---|
| Bandit reward(实测通过率) | RD-Agent | 未用 |
| Self-correct(LLM 多轮)| AIAC + Alpha-GPT v2 | ✅ 已用 |
| Dual-channel RAG(positive + negative) | Hubble v2 | ⚠️ 半用(P2-D negative_knowledge 已 active,但未分通道)|
| Hypothesis ↔ Implementation 双向 LLM judge | AlphaAgent | 未用(R5 路线图)|
| Trajectory replay | QuantaAlpha | 未用 |

### 8.4 按抗 decay 机制

| 机制 | 代表 | AIAC 现状 |
|---|---|---|
| AST subtree isomorphism 原创度门 | AlphaAgent(KDD 2025 Eq. 5)| 未用(R3 路线图,polynomial 可解非 NP-hard)|
| Family-cap top-k=2 | Hubble v2 | 未用(R10 路线图)|
| Crowding 控制 + semantic consistency | QuantaAlpha | 未用 |
| 三正则化(原创 + 对齐 + 复杂度) | AlphaAgent Eq. 4 | ⚠️ 部分:复杂度 ✅、对齐 ❌、原创 ❌ |
| Pillar 分类(5 维) | AIAC | ✅ 已用(P2-B)|

---

## 9. 对 AIAC 的设计建议

### 9.1 短期(Phase 0 / R1a, 2-4 周)

激活 `agents/core/` 既有 3223 行 RD-Agent 兼容代码 — 详见 [`phase15 §14`](phase15_task_schema_refactor_plan.md#14-r1a-启用细化v2-新增phase-0-主菜)

### 9.2 中期(Phase 2, 7-9 人日)

参考竞品引入:
- **R5**:AlphaAgent 双向 LLM judge(2 人日)
- **R6**:RD-Agent v0.8.0 Trace DAG(3 人日)
- **R10**:Hubble v2 family-cap top-k=2(1 人日)

### 9.3 长期(Phase 3 / R1b, Q3 2026, 4-6 周)

**flat search 切换** — 删 cascade tier,采用 RD-Agent style 假设森林 + bandit 调度。详见 [`phase15 §13`](phase15_task_schema_refactor_plan.md#13-flat-search-完整设计v2-新增r1b-reference-技术路径)

---

## 10. 信息不可得

- **Alpha-GPT 2.0 具体 Sharpe/IC 数表**(PDF binary fetch 失败,html 404)
- **Alpha-GPT 2.0 LLM 模型版本**(推测 Llama3 70B,未确认)
- **AlphaAgent vs Alpha-GPT 直接对比**(baseline 不含)
- **Shamir-Tsur 1999 `O(n^2.5/log n)` 上界**(DBLP/ScienceDirect socket 错误,引用前需交叉验证)
- **RD-Agent-Quant 14.21% / 22-26 因子 / <$10 单次成本**(来自 paper body Table,未从 abstract 提取确认)
- **AlphaAgent CSI500 五指标 IC=0.0212 / IR=1.488 / MDD=-9.36% / hit-ratio +81% / token +23%**(PDF 文本层提取失败,引用以 KDD 2025 ACM 版 Table 为准)
- **Hubble formal ablation**(作者 v2 自承缺)
- **Chain-of-Alpha 撤稿具体理由**(arxiv 标记撤稿但未提供官方 retraction notice)
- **AlphaSAGE / Alpha² 完整实验数据**(arxiv 摘要级别)

---

## 11. Sources

### 11.1 学术论文

- [AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration (arxiv 2502.16789)](https://arxiv.org/abs/2502.16789)
- [AlphaAgent KDD 2025 ACM](https://dl.acm.org/doi/10.1145/3711896.3736838)
- [QuantaAlpha: An Evolutionary Framework for LLM-Driven Alpha Mining (arxiv 2602.07085)](https://arxiv.org/abs/2602.07085)
- [Chain-of-Alpha: Dual-Chain LLM Framework (arxiv 2508.06312, 撤稿)](https://arxiv.org/abs/2508.06312)
- [Chain-of-Alpha alphaXiv overview](https://www.alphaxiv.org/overview/2508.06312v2)
- [Navigating Alpha Jungle: MCTS Framework (arxiv 2505.11122)](https://arxiv.org/html/2505.11122v2)
- [Alpha-R1: LLM Reasoning + RL Screening (arxiv 2512.23515)](https://arxiv.org/html/2512.23515)
- [RD-Agent-Quant NeurIPS 2025 (arxiv 2505.15155)](https://arxiv.org/abs/2505.15155)
- [Increase Alpha: AI-Driven Trading Framework (arxiv 2509.16707)](https://arxiv.org/html/2509.16707v1)
- [Alpha-GPT v1 (arxiv 2308.00016)](https://arxiv.org/abs/2308.00016) / [v2 修订 EMNLP 2025 Demos](https://aclanthology.org/2025.emnlp-demos.14/)
- [Alpha-GPT 2.0 (arxiv 2402.09746)](https://arxiv.org/abs/2402.09746)
- [Hubble v2 (arxiv 2604.09601)](https://arxiv.org/abs/2604.09601)

### 11.2 开源实现

- [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent)
- [microsoft/qlib](https://github.com/microsoft/qlib)
- [QuantaAlpha/QuantaAlpha](https://github.com/QuantaAlpha/QuantaAlpha)
- [RndmVariableQ/AlphaAgent](https://github.com/RndmVariableQ/AlphaAgent)
- [PyPI: rdagent v0.8.0](https://pypi.org/project/rdagent/)

### 11.3 文档 / blog / 工业立场

- [rdagent.readthedocs.io/en/stable/](https://rdagent.readthedocs.io/en/stable/)
- [MSR 官方 Co-STEER 解释](https://www.microsoft.com/en-us/research/articles/rd-agent-an-open-source-solution-for-smarter-rd/)
- [eFinancialCareers — Citadel CTO 立场](https://www.efinancialcareers.com/news/hedge-fund-citadel-hired-goldman-sachs-code-cracking-quant-md-for-its-power-trading-team)
- [NeurIPS 2025 poster 121804 — RD-Agent-Quant](https://neurips.cc/virtual/2025/poster/121804)

### 11.4 AIAC 内部对照源

- `backend/agents/core/`(3223 行,9 模块,DORMANT)
- `backend/agents/core/ARCHITECTURE.md`(18 章节设计文档)
- `backend/agents/core/integration.py:342-407`(`enhance_existing_node_evaluate` hook,R1a 接入点)
- `backend/agents/core/feedback.py:17-22`(`AttributionType` enum)
- `backend/selection_strategy.py:135`(既有 DatasetBandit UCB1,与 RD-Agent Thompson 对照)

---

*本文档独立于 phase15 plan,可单独引用作为竞品对比 reference。原始 §12 内容仍保留在 phase15 plan 内作为整合视图。后续 AIAC P3/P4 路线决策应 cross-reference 本文 §6(顶级共性)+ §9(三阶段建议)+ phase15 §13(flat search)。*
