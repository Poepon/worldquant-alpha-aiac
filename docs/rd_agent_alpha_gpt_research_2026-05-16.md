# Alpha-GPT + RD-Agent + AlphaAgent + Hubble 深度调研 — 对 AIAC 的可迁移知识

> 调研日期:2026-05-16
> 调研对象:Microsoft RD-Agent v0.8.0 / Alpha-GPT 系列 / AlphaAgent KDD 2025 / Hubble v2
> 目的:对照 AIAC `backend/agents/core/` 现状,产出 P3 路线图 R 系列
> 配套文档:
> - [`alphagbm_skills_research_2026-05-15.md`](alphagbm_skills_research_2026-05-15.md)(工程模式调研)
> - [`qlib_alpha_research_2026-05-16.md`](qlib_alpha_research_2026-05-16.md)(seed 内容调研,§ 3 已 partial 覆盖 Alpha-GPT)
> - [`retrospective_p012_2026-05-16.md`](retrospective_p012_2026-05-16.md)(P0-P2 完成回顾)

> **审查状态**:1 轮 Plan + 3 Explore + 1 轮红队对抗审查,catch **4 个 MUST FIX**(R4 整个基于错误前提 / Co-STEER 第二口径幻觉 / Hubble v1-v2 区分 / Alpha-GPT v1-v2 修订区分)+ 5 SHOULD FIX,全部修正。

---

## § 0 项目定性与调研边界

### 0.1 调研对象

| 项目 | 角色 | 核心价值 |
|---|---|---|
| **microsoft/RD-Agent** v0.8.0 (2025-11-03) | AIAC `agents/core/` **同源团队** | Co-STEER + Contextual Thompson Sampling + multi-trace |
| **Alpha-GPT v1.0** (arxiv:2308.00016, **v2 修订 2025-09**) | LLM-alpha 鼻祖,HKUST+IDEA 沈向洋系 | Hierarchical RAG + 三阶段流水线 |
| **Alpha-GPT 2.0** (arxiv:2402.09746, 2024) | v1 扩展 3 阶段 agent | Alpha Modeling + Alpha Analysis(范式扩展) |
| **AlphaAgent** (arxiv:2502.16789, KDD 2025) | 反 alpha decay 三正则化 | AST subtree isomorphism + LLM judge |
| **Hubble v2** (arxiv:2604.09601, 2026-04-14) | DSL + sandbox + dual-channel RAG | 与 AIAC 架构最近邻(80% 重合) |

### 0.2 与 Qlib 调研的关系

Qlib 调研偏 **seed 内容**(Alpha101/158/360 + Open Source Asset Pricing 319 predictor),本调研偏 **架构与方法学**(LLM-alpha 范式 + 评估/校验/选择算法)。两者 **orthogonal 不重复**。

### 0.3 调研边界

**不做**:
- ❌ `pip install rdagent` 整包依赖(LiteLLM/LangChain/Prefect 与 AIAC LangGraph/Celery/FastAPI 冲突)
- ❌ Alpha Modeling 复制(组合 portfolio,与 AIAC 单 alpha 提交 BRAIN 范式不同)
- ❌ DockerEnv sandbox(AIAC 跑 BRAIN 在线 simulate,无本地代码执行需求)
- ❌ 切换 LLM provider 到 Llama3 70B(AIAC 用 DeepSeek/Claude 已足够)
- ❌ 重写 LangGraph 主路径

---

## § 1 RD-Agent v0.8.0 全景

### 1.1 项目状态

| 项 | 数据 |
|---|---|
| 项目 | [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) |
| 团队 | MSRA — Weiqing Liu / Jiang Bian(**与 Qlib 同团队**) |
| 最新 release | **v0.8.0 (2025-11-03)** |
| Python | ≥ 3.10(3.10/3.11 CI) |
| License | **MIT** |
| PyPI | `pip install rdagent` |
| 文档 | [rdagent.readthedocs.io](https://rdagent.readthedocs.io/en/stable/) |
| Co-STEER 全名 | **Collaborative Knowledge-STudying-Enhanced Evolution by Retrieval**([MSR 官方](https://www.microsoft.com/en-us/research/articles/rd-agent-an-open-source-solution-for-smarter-rd/)) |

> 注:本调研第一稿 Explore agent 报告有 "Co-STEER 第二口径 'Collaborative Scheduling and Task Execution Engine for Quant Research'",对抗审查 catch 为幻觉(arxiv/MSR/Google 全 0 匹配),已删除。

### 1.2 项目结构 + CLI 入口

```
rdagent/
  core/                              # 13 模块:conf/developer/evaluation/evolving_agent/
                                    #   evolving_framework/exception/experiment/interactor/
                                    #   knowledge_base/prompts/proposal/scenario/utils
  components/coder/CoSTEER/         # Co-STEER 代码生成核心
  scenarios/qlib/                    # 联合 Qlib 平台
  scenarios/kaggle/                  # Kaggle/MLE-bench
  scenarios/data_science/
  web/                               # Vue UI
  docs/ / test/ / requirements/
```

**CLI 入口**:
- `rdagent fin_quant` — 联合因子 + 模型(即 RD-Agent-Quant)
- `rdagent fin_factor` / `rdagent fin_model` / `rdagent fin_factor_report`
- `rdagent general_model` / `rdagent data_science` / `rdagent llm_finetune`
- `rdagent ui` (Streamlit) / `rdagent server_ui` / `rdagent health_check`

### 1.3 数据结构 6 类(对照 AIAC `backend/agents/core/`)

| RD-Agent 类(字段) | AIAC 等价类(字段) | 差异要点 |
|---|---|---|
| `Hypothesis(hypothesis, reason, concise_*)` | `experiment.Hypothesis(statement, rationale, expected_signal, key_fields, suggested_operators, confidence, novelty, concise_*)` | AIAC 多领域字段(operators/fields/signal type);RD-Agent 全文本 |
| `Experiment(hypothesis, sub_tasks, sub_workspace_list, based_experiments, ...)` | `experiment.AlphaExperiment` | AIAC 缺 sub_tasks DAG + Workspace ckpt |
| `EvoStep(evolvable_subjects, queried_knowledge, feedback)` | `experiment.EvoStep` | 字段同构 |
| `Trace(scen, hist, dag_parent, idx2loop_id, knowledge_base, current_selection)` + `get_sota_*/get_parents/get_children` | `trace.ExperimentTrace(dataset_id, region, universe, hist, dag_parent, knowledge_base)` | AIAC 缺 `current_selection` + `idx2loop_id`(MCTS 选树需)+ branch SOTA 检索 |
| `HypothesisFeedback(reason, decision, code_change_summary, observations, hypothesis_evaluation, new_hypothesis, acceptable, exception)` | `feedback.HypothesisFeedback(observations, hypothesis_evaluation, hypothesis_supported, attribution, decision, should_continue/retry/modify/abandon, knowledge_extracted, invalid_conclusions, exception)` | **AIAC 增 `AttributionType`(hypothesis vs implementation)+ `should_*` 显式动作建议 + `invalid_conclusions`**;AIAC 这一层比 RD-Agent **更结构化** |
| `EvolvingKnowledgeBase / RAGStrategy` | `knowledge.EvolvingKnowledge` + `evolving_rag.AlphaRAGStrategy` | 同构 |

### 1.4 Multi-armed bandit = Contextual Thompson Sampling(关键算法)

RD-Agent-Quant 论文 §2.5 + 附录 A.2:

```
Arms          A = {factor, model}                                  # 方向级,不是因子族
Reward        r = wᵀ xₜ                                           # 线性
Reward 向量    xₜ ∈ ℝ⁸ = (IC, ICIR, Rank-IC, Rank-ICIR, ARR, IR, -MDD, SR)
后验          每 arm 独立 Bayesian linear regression
              Gaussian 先验 μ⁽ᵃ⁾=0, P⁽ᵃ⁾=τ⁻²I
              θ̃⁽ᵃ⁾ ~ 𝒩(μ⁽ᵃ⁾, (P⁽ᵃ⁾)⁻¹)
选择          argmax_a θ̃⁽ᵃ⁾ᵀ xₜ
```

**实现位置**:`rdagent/scenarios/qlib/proposal/quant_proposal.py:QlibQuantHypothesisGen.prepare_context()`,三模式 `bandit | llm | random`,由 `QuantTrace.env_controller` 维护 arm 状态。配置键 `QLIB_QUANT_ACTION_SELECTION`,默认 `bandit`。

**消融**(CSI300 ARR):
| 选择策略 | ARR |
|---|---|
| **bandit (Thompson)** | **14.21%** |
| LLM-select | 10.09% |
| random | 8.97% |

### 1.5 v0.8.0 (2025-11) 46 项 highlights

**新增**:
- **Pydantic AI agent + 7 个 MCP capabilities**(对接 Claude/Cursor MCP 协议)
- **MCTS policy based on trace scheduler**(选 trace 分支的新算法,bandit 之外的第二条腿)
- **MCP cache 一键开关**
- async 多 trace **diversity injection**
- **RAG-MCP** in proposal
- `enable_finetune_llm`(模型微调闭环)
- **LLM-based hypothesis selection with time-aware prompting**
- **Meta planner**

**修复 / Breaking**:JSON 响应 fallback / `DSProposalV2ExpGen` 漏 `self` / proposal 系统重构以适配 MCP

### 1.6 Sandbox 设计

`rdagent/utils/env.py`:`Env`(Generic 基类)+ `DockerEnv` / `DockerConf`(image/mount_path/extra_volumes/mem_limit/enable_gpu)+ `LocalEnv` 备选,统一 `run()/cached_run()` 接口,`EnvResult(stdout, exit_code, running_time)`。**强制 Linux + Docker 免 sudo**。

**对照 AIAC**:AIAC 跑 BRAIN 在线 simulate,**无本地代码执行需求**,此层不必移植。但 `cached_run()` 的缓存幂等思路可借鉴(AIAC 现每次 simulate 都走 BRAIN,无本地缓存 → R9 候选)。

### 1.7 依赖差异分析

| 维度 | RD-Agent | AIAC |
|---|---|---|
| LLM 抽象 | **LiteLLM**(v0.4.0 起统一) | DeepSeek SDK + 可选 Anthropic |
| Agent 框架 | LangChain + LangChain-community | **LangGraph** |
| Async 调度 | **Prefect** | **Celery** |
| Web UI | Streamlit + Vue | React + Vite |
| DB ORM | (内置 SQLite) | **SQLAlchemy + asyncpg** |
| API server | (无) | **FastAPI** |

**结论**:**不能 `pip install rdagent` 直接复用**,要 vendor/copy 关键类(`rdagent/core/proposal.py` + `evolving_framework.py`),**保持 AIAC 现有栈不动**。

---

## § 2 RD-Agent-Quant NeurIPS 2025 论文细节

### 2.1 元信息

- 论文:[arxiv:2505.15155](https://arxiv.org/abs/2505.15155) / [v2 html](https://arxiv.org/html/2505.15155v2)
- 作者:Yuante Li, Xu Yang, Xiao Yang, Minrui Xu, Xisen Wang, Weiqing Liu, Jiang Bian(MSRA-MIIC)
- v1 提交 2025-05-21,v2 修订 2025-09-25
- NeurIPS 2025 接收([poster 121804](https://neurips.cc/virtual/2025/poster/121804))
- 42 页 11 图

### 2.2 5 单元闭环架构

```
Specification 𝒮 = (ℬ, 𝒟, ℱ, ℳ)
        ↓
Synthesis G(ℋₜ, ℱₜ)                             # 假设森林选下一假设
        ↓
Implementation (Co-STEER 用 DAG πₛ + 知识库 𝒦)  # 代码生成 + 演化
        ↓
Validation (IC 相关 ≥ 0.99 去重 + Qlib 回测)
        ↓
Analysis (更新 SOTA + bandit reward 更新)
        ↺ (回到 Synthesis)
```

### 2.3 Research → Development → Feedback 三段时序

| 阶段 | 内容 |
|---|---|
| **Research** | 规范化 + 从历史森林选假设 |
| **Development** | Co-STEER 生码 + 演化迭代:`π*ᵢ = argmax_π 𝔼[∑ⱼ Rᵢ(cⱼ)]`,DAG 拓扑 + 复杂度权重 αⱼ |
| **Feedback** | 回测 + bandit 更新次轮方向 |

每轮 hypothesis/code/result 都持久化到知识库三元组 `(tⱼ, cⱼ, fⱼ)`。

### 2.4 实验结果(CSI300)

| Item | 数据 |
|---|---|
| **RD-Agent(Q) o3-mini ARR** | **14.21%** |
| Alpha158 ARR | 5.70% |
| 提升倍数 | **~2.5×**(论文 abstract 说"2×") |
| 使用因子数 | **22-26** vs Alpha158 的 **158**(~86% 减少) |
| 单次实验成本 | **< $10** |

**消融**(joint > factor-only > model-only):
| 模式 | ARR |
|---|---|
| joint | 14.21% |
| factor-only | 11.84% |
| model-only | 10.99% |

**泛化**:CSI500 + NASDAQ100(Test 2024–2025/06)同样有效。

---

## § 3 Alpha-GPT 系列深入

### 3.1 v1.0(arxiv:2308.00016)

**版本史与 caveat** ⚠️:
- **v1 原版 (2023-07-31)**:未明确 LLM(可能 GPT-3.5/4),无 Llama3 / BGE-M3(后两者 2024 才发布)
- **v2 修订 (2025-09-20,EMNLP 2025 Demos 版)**:**明确写 Llama3 70B + BGE-M3 + 4 层 Hierarchical RAG**

本节内容**主要源自 v2 修订版**。

**三阶段流水线**(v2 § 2):

| 阶段 | Agent | 功能 |
|---|---|---|
| **Ideation** | "Trading Idea Polisher" | 用 RAG 把研究员想法 + 文献 + datafield specs 编译为结构化 prompt |
| **Implementation** | "Quant Developer" | 生成 seed alpha,再用 **genetic programming 在 LLM 邻域 evolve**(seed + GP 混合)+ structured output validation |
| **Review** | "Analyst" | 跑回测 → 自然语言报告 → human-in-loop 多轮(**非固定 N 轮**) |

**Hierarchical 4 层 RAG**(v2 § 3.2,Figure 3):

```
RAG#0 (已有 alpha 全表达式)
  ↓
RAG#1 (高阶类别)
  ↓
RAG#2 (子类别)
  ↓
RAG#3 (datafield)
```

知识库 = **Kakushadze 101-Alphas + proprietary alpha base**,sub-expression + Faiss。作者明示:"只在 idea 与 alpha base 对齐时才用外部记忆"(按需 RAG)。

### 3.2 v1.0 实验数据(v2 修订)

| Table | 内容 |
|---|---|
| Table 2 | IQC 2024:**81 qualified alphas / 总分 48,866 / 全球 41k 队 top-10** |
| Table 3 | 翻译一致性 Alpha-GPT 8.16 vs 初级人类 6.81,**win rate 86.60%** |
| Table 4 | IC 递进:Seed-only 0.58% → +Search 1.23% → +Interaction+SE **2.23%**(~4× 提升) |
| Table 5 | 高频:14% return / Sharpe **5.47** / MDD 2.36% |

**未与 Alpha158/Alpha101 直接对比**。

### 3.3 Alpha-GPT 2.0(arxiv:2402.09746,2024)

**v2.0 核心 delta**:从单阶段 mining 扩展为 3 阶段 agent:

| 阶段 | v1.0 | v2.0 |
|---|---|---|
| Alpha Mining | ✅ | 保留 |
| **Alpha Modeling** | ❌ | ✅ NEW(把多 alpha 组合成 portfolio-level predictor) |
| **Alpha Analysis** | ❌ | ✅ NEW(事后归因/风险报告) |

每阶段 LLM agent + Human-in-the-Loop。

**实验数字**:**信息不可得**(arxiv PDF binary fetch 失败、html 404,abstract 无 Sharpe/IC 表)。

### 3.4 EMNLP 2025 Demos

[aclanthology.org/2025.emnlp-demos.14](https://aclanthology.org/2025.emnlp-demos.14/),pp. 196-206:**是 v1 修订扩充版,不是 v2.0**。与 arxiv:2308.00016 **v2 同源全文**。

作者:Saizhuo Wang, Hang Yuan, Leon Zhou, Lionel Ni, Heung-Yeung Shum, Jian Guo(HKUST + IDEA Research)。

### 3.5 Alpha-GPT vs RD-Agent 谱系对比

| 维度 | Alpha-GPT | RD-Agent |
|---|---|---|
| 团队 | HKUST + IDEA(沈向洋系) | MSRA(Bian Jiang 系) |
| 首发 | 2023-07 v1 | 2024-08 v0.1 |
| 侧重 | **人机对话 + WorldQuant BRAIN 兼容** | **全自动 + Qlib 回测** |
| LLM | Llama3 70B(v2) | LiteLLM 多 provider |
| 互相引用 | 不引用 | 不引用 |

**AIAC 思想是融合两者** — 用 BRAIN 平台(像 Alpha-GPT)+ Co-STEER feedback loop(像 RD-Agent)+ 自研多保真 + 遗传优化(CLAUDE.md L13 已明示)。

---

## § 4 AlphaAgent KDD 2025 + Hubble v2 深入

### 4.1 AlphaAgent 三正则数学公式

[arxiv:2502.16789](https://arxiv.org/abs/2502.16789) / [KDD 2025 ACM](https://dl.acm.org/doi/10.1145/3711896.3736838)

**核心公式**(arxiv v2 § 3):

```
f* = arg max L(f(X), y) − λ · R_g(f, h)                  … Eq. 2  目标
R_g = α₁ · SL(f) + α₂ · PC(f) + α₃ · ER(f, h)             … Eq. 4  三正则化
s(f_i, f_j) = max{|t_i| : t_i ⊆ T(f_i), t_i ≅ t_j}        … Eq. 5  最大公共同构子树
S(f) = max_{φ ∈ Z} s(f, φ)                                … Eq. 6  原创度 vs alpha zoo
C(h, d, f) = α · c₁(h, d) + (1-α) · c₂(d, f),  α=0.5      … Eq. 7  双向 LLM judge
ER(f, h) = β₁ · S(f) + β₂ · C(h, d, f) + β₃ · log(1+|F_f|) … Eq. 8  原创+对齐+复杂度
```

**关键细节**:
- **Eq. 5 不是** tree edit distance / kernel,而是 **pairwise subtree isomorphism 节点计数**
- alpha AST 是 **rooted-ordered**(operator args 有序)→ **polynomial 可解**(Shamir-Tsur 1999, O(n^2.5/log n)),**不是 NP-hard MCIS**
- `c₁/c₂` 都由 LLM judge:`c₁(h, d)` 校验 hypothesis ↔ description;`c₂(d, f)` 校验 description ↔ expression

### 4.2 AlphaAgent 实验

| Metric | CSI 500 |
|---|---|
| IC | **0.0212** |
| ICIR | 0.1938 |
| 年化收益 | 11% |
| **IR** | **1.488** |
| MDD | -9.36% |
| **hit-ratio** | **+81%**(0.29 vs 0.16) |
| **token 效率** | **+23%** |

**Baseline**(不与 Alpha-GPT 直接对比):LSTM/Transformer/LightGBM/TRA/StockMixer/AlphaForge/RD-Agent/DeepSeek-R1/OpenAI-o1。

**论文未给 AST subtree 算法伪代码**(信息不可得 § 8)。

### 4.3 Hubble v2 5 组件(arxiv:2604.09601, 2026-04-14)

⚠️ **版本注明**:
- **v1 (2026-03-09)**:单通道 RAG + error categorization feedback,**无** dual-channel / family-cap
- **v2 (2026-04-14)**:**新增 Dual-channel RAG + family-cap top-k=2**

本节内容**专指 v2**:

| 组件 | 内容 |
|---|---|
| **DSL 生成器** | 简化 alpha 语法,operators 已枚举:TS_SMA/STD/VAR/LOGRET, CS_RANK/ZSCORE, IF |
| **Dual-channel RAG**(v2 新) | Positive(拉代表性公式鼓励探索)+ **Negative**(拉拥挤模板作 **avoid-like reference**) |
| **3 层 AST sandbox** | structural 白名单 / **complexity depth+node 硬上限** / semantic operator-arity 校验 |
| **Deterministic evaluation engine** | 确定性回测 |
| **Family-cap top-k=2**(v2 新) | 防一族刷榜,Table 1 |

**LLM 配置**:nvidia/nemotron-3-super-120b(main)+ openrouter/hunter-alpha(robustness)

### 4.4 Hubble v2 实验

| Metric | S&P 500 |
|---|---|
| Stocks | 501 |
| Discovery 期 | 2022-01 ~ 2025-05 |
| OOS 期 | 195 days |
| **OOS crash 数** | **0** |
| Range factor OOS HAC t-stat | **2.98 / 3.01** |

**作者自承无 formal ablation**(信息不可得 § 8)。

**与 AIAC 架构 80% 重合**:DSL/AST sandbox(structural+semantic)、Positive RAG、deterministic eval、persistence。差距:
- AST sandbox **complexity 层弱**(AIAC 现无 depth/node-count 硬上限)
- AIAC `negative_knowledge.py` 已 active injection(P2-D `6cae5f5`,M1 修正),但**未分通道**(positive 与 negative 同 channel)— Hubble v2 是显式 dual-channel
- ❌ **Family-cap**:`pillar_classifier.py` 有 Five Pillars 分类但无 top-k hard cap

---

## § 5 AIAC `backend/agents/core/` 现状评估 — DORMANT(计划性待激活)⚠️

### 5.1 完整模块结构

`backend/agents/core/` = **3223 行 Python 代码,13 模块**:

| 文件 | 行数 | 关键 export |
|---|---|---|
| `__init__.py` | 149 | 统一导出 48 个符号 |
| `experiment.py` | 240 | `Hypothesis` / `AlphaExperiment` / `EvoStep` |
| `feedback.py` | 215 | `HypothesisFeedback` + `AttributionType` enum |
| `trace.py` | 362 | `ExperimentTrace` DAG + `TraceNode` |
| `knowledge.py` | 299 | `KnowledgeRule` / `QueriedKnowledge` / `EvolvingKnowledge` |
| `scenario.py` | 200+ | `Scenario` / `AlphaMiningScenario` |
| `pipeline.py` | 700+ | 4 段 Pipeline(`LLMHypothesisGen` → `LLMHypothesis2Experiment` → `BRAINExperimentRunner` → `LLMExperiment2Feedback`)+ `AlphaMiningPipeline` |
| `evolving_rag.py` | 300+ | `AlphaRAGStrategy` / `EnhancedQueriedKnowledge` |
| `integration.py` | 450+ | 工厂函数 + 既有系统适配器 |
| `ARCHITECTURE.md` | — | 18 章节完整中英双语设计文档 |

### 5.2 数据结构 6 类对照表(已有 vs RD-Agent v0.8.0 差距)

(详见 § 1.3)

**AttributionType 字符串渗透 production**(S5 修正):
- `AttributionType` enum 类**只**在 core/ 内 + tests + integration.py import
- **但其 enum value 字符串**(`'HYPOTHESIS_FAILURE'` / `'IMPLEMENTATION_FAILURE'`)作为 `KnowledgeEntry.entry_type` 约定**已被 production 使用**(`backend/metrics_tracker.py:440`)
- `agents/graph/early_stop.py:105` 是**字符串注释引用**("matching backend.agents.core.feedback.AttributionType .value strings"),不是真 import
- **不能 rm -rf core/feedback.py** — value 字符串语义已渗透

### 5.3 零生产路径调用证据

**grep verify**(对抗审查 V1 确认):
```bash
$ grep -rn "from backend.agents.core" backend/agents/graph/ backend/agents/mining_agent.py \
    backend/tasks/ backend/services/ backend/routers/ backend/celery_app.py
# 0 matches in production code
```

只在以下地方出现:
- `backend/agents/core/` 自身互引
- `backend/agents/core/__init__.py` 导出
- `backend/tests/integration/test_core_integration.py` 测试(mock 实现,未连真实 BRAIN/LLM)

### 5.4 `run_enhanced_mining()` DORMANT 标注

`backend/agents/core/integration.py:279`(原文):
```
"Status (2026-05-06): DORMANT — not wired into Celery / mining_tasks.
 Plan v5 Final §三轮精简 pushed Phase 3 to Q3 (2026-07-09).
 Activation gated by task.config.hypothesis_centric_variant=3 flag."
```

**这是计划性 dormant,不是 dead code**(S1 修正)。

### 5.5 `enhance_existing_node_evaluate()` 是渐进迁移钩子

`integration.py:342-407` — 设计为 **evaluate 节点 shim**,可注入现有 `node_evaluate` 节点,生成归因感知的反馈,**不需要重写整个 workflow**。

P3 推荐:**R1a 优先启用此钩子**(2 人日轻活)而非 R1b 全 Pipeline 激活(Q3 2026 大改 4-6 周)。

### 5.6 测试覆盖

- 仅 1 个 integration test:`backend/tests/integration/test_core_integration.py`
- 覆盖 `TestIntegrationHelpers`(create_scenario / create_trace / experiment_to_alpha_result / enhance_existing_node_evaluate)+ `TestEndToEndFlow`(完整 Pipeline 流程)
- **使用 mock objects,未连接真实 BRAIN / LLM**

### 5.7 P3 决策意义

RD-Agent 借鉴有三条路:
- **(a) 激活既有 core/**(R1a/R1b,沉没成本 3223 行,推荐)
- **(b) 再借鉴 v0.8.0 新模式**(MCP / MCTS / Direction Bandit 等)
- **(c) deprecate core/ 移到 vendor/**(R1c,放弃沉没成本,减少维护)

推荐 **(a) 渐进 + (b) 选择性借鉴**。

---

## § 6 十项可迁移设计模式 — AIAC P3 路线图(按修正后 ROI 排序)

| 优先级 | 模式 | 来源 | AIAC 位置 | 工程量 |
|---|---|---|---|---|
| ✅ **R4** | **启用 `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE=True`**(M1 修正:P2-D 已实现 active injection)| Hubble v2 启发 | `config.py:428`(flag flip) | **0 人日** + 1-2 周 A/B 验证 |
| 🔴 **R1a ★★★★★** | **启用 `enhance_existing_node_evaluate()`**(evaluate 节点 shim)| AIAC 沉没成本(core/)| `agents/graph/nodes/evaluation.py` 接入 | **2 人日**(轻活) |
| 🔴 **R2 ★★★★★** | **Direction-level Contextual Thompson Sampling**(arms = `{genetic_mutation, llm_generation, rag_template, knowledge_pattern}`)| RD-Agent-Quant §2.5 | `evolution_strategy.py` 新加 `DirectionBandit` | **2 人日** + ROI 论证 |
| 🔴 **R3 ★★★★★** | **AST polynomial subtree isomorphism**(AlphaAgent Eq. 5,**非 NP-hard**)| AlphaAgent KDD 2025 | `diversity_tracker.py` + `knowledge_extraction.expression_to_skeleton` 扩 | **3-5 人日**(S2 修正) |
| 🟡 R4' ★★★★ | **Dual-channel RAG 分通道渲染**(Hubble v2)| Hubble v2 | `prompts/hypothesis.py` positive vs negative 视觉区分 | **1-2 人日** |
| 🟡 R5 ★★★★ | **Hypothesis-Alignment 双向 LLM judge**(AlphaAgent Eq. 7)| AlphaAgent | `feedback_agent.py` 加 c₁/c₂ 校验 | **2 人日** |
| 🟡 R6 ★★★★ | **Trace `current_selection` + DAG 多分支**(v0.8.0 MCTS)| RD-Agent v0.8.0 | `agents/core/trace.py` 扩 + 激活路径 | **3 人日** |
| 🟢 R7 ★★★ | **Co-STEER `should_use_new_evo` 半接受机制**(防覆盖好样本)| RD-Agent | `node_self_correct` 加 feedback 比较 | **1 人日** |
| 🟢 R8 ★★★ | **4 层 Hierarchical RAG**(Alpha-GPT v1.0 v2 修订)| Alpha-GPT | `rag_service.py` 重构 | **5-8 人日** |
| 🟢 R9 ★★ | **Workspace checkpoint**(`cached_run()` 思路 — `simulation_cache` 表)| RD-Agent | 新加 simulation_cache 表 | **3 人日** |
| 🟢 R10 ★★ | **Family-cap top-k=2**(Hubble v2)| Hubble v2 | `pillar_classifier` 加 hard cap | **1 人日** |
| 🟢 R1b ★★ | **全 Pipeline 激活**(Plan v5 Q3 2026)| AIAC core/ | 重写 `hypothesis_centric_variant=3` 路由 | **4-6 周大改**(S4 修正) |
| 🔵 R1c | **替代:deprecate core/ 移到 vendor/**(减少维护负担)| AIAC | rm `agents/core/` → `vendor/rdagent_style/` | 1 人日 |

### Phase 分组

**Phase 0 — 立即可做(零工程量)**:
- **R4**:flip `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE=True` + 1-2 周 A/B 验证

**Phase 1(2-3 周,8-12 人日)— 高 ROI 快赢**:
- R1a(2 人日)+ R2(2 人日)+ R3(3-5 人日)+ R4'(1-2 人日)

**Phase 2(2 周,7-9 人日)— 学术验证模式**:
- R5 + R6 + R7 + R10

**Phase 3(Q3 2026 大改 + 优化)**:
- R1b(4-6 周)+ R8(5-8 人日)+ R9(3 人日)
- 或 R1c(替代:deprecate core/)

### R 路线图 caveat 与 ROI 论证(S3 修正)

**R2 的 AIAC 适配**:
- RD-Agent 8 维 reward (IC/ICIR/Rank-IC/Rank-ICIR/ARR/IR/-MDD/SR) 在 AIAC **无对应数据**(`Alpha` 只有 sharpe/fitness/turnover/correlation/composite_score 5 维)
- AIAC 用 **5 维替代**:`(is_sharpe, is_fitness, -is_turnover, -self_correlation, composite_score)`
- arms 是 **AIAC 自定义**(生成策略级),不是 RD-Agent 原版 task-direction arms
- **arm 语义独立性未验证**(AIAC 现是单 LLM 路径,需先拆分才能 multi-arm)— 这是 R2 隐含成本
- 既有 `selection_strategy.py:135` 已用 **UCB1**(`math.sqrt(2 * math.log(...))`),Thompson Sampling 升级 ROI 在 AIAC 场景(sparse pass_rate)未必显著好

**R3 的算法选择**:
- alpha AST 是 **rooted-ordered**(operator args 有序)→ **polynomial 可解**(Shamir-Tsur 1999, O(n^2.5/log n)),**不是 NP-hard MCIS**
- AIAC `knowledge_extraction.extract_operator_tree:100-117` 已有 rooted-ordered tree
- AIAC 当前用 `expression_to_skeleton` 字符串 skeleton + `diversity_tracker.fingerprint` md5(skeleton + 5 组件)— 两者不同
- AlphaAgent 论文**未给伪代码**(信息不可得 § 8),需自行实现 Shamir-Tsur 算法

---

## § 7 不做的事

1. **不 `pip install rdagent` 整包依赖**(LiteLLM/LangChain/Prefect 与 AIAC LangGraph/Celery/FastAPI 冲突)
2. **不复制 Alpha-GPT v2.0 Alpha Modeling**(组合 portfolio,与 AIAC 单 alpha 提交 BRAIN 范式不同)
3. **不用 RD-Agent 的 DockerEnv sandbox**(AIAC 跑 BRAIN 在线 simulate,无本地代码执行需求)
4. **不切换 LLM provider 到 Llama3 70B**(AIAC 用 DeepSeek/Claude 已足够)
5. **不重写 LangGraph 主路径**(`mining_agent.py` 已稳定 6+ 周,激活 core/ 走渐进 R1a 钩子)
6. **不立即落地 R 项**(本期产出路线图,实施另开 PR)
7. **不承诺 R 项时间表**(路线图仅作 ROI 排序)
8. **不 rm core/feedback.py**(S5:AttributionType value 字符串已渗透 production)

---

## § 8 信息不可得 + 附录 URL

### 8.1 信息不可得(对抗审查明示)

- **Alpha-GPT 2.0 具体 Sharpe/IC 数表**(PDF binary fetch 失败,html 404)
- **Alpha-GPT 2.0 LLM 模型版本**(推测延续 Llama3 70B,未确认)
- **EMNLP 2025 demo video URL**(Anthology 无)
- **"Evolution of Alpha" survey 2505.14727 taxonomy 具体表**(33pp PDF 不可解)
- **AlphaAgent vs Alpha-GPT 直接对比**(baseline 不含)
- **AlphaAgent AST 算法实现伪代码 / 复杂度证明**(论文未给,需自行设计 Shamir-Tsur)
- **Hubble formal ablation**(作者 v2 自承缺)

### 8.2 附录 URL 索引

#### 📚 学术论文

- [arxiv:2308.00016 — Alpha-GPT v1.0](https://arxiv.org/abs/2308.00016)(v2 修订 2025-09)
- [arxiv:2402.09746 — Alpha-GPT 2.0](https://arxiv.org/abs/2402.09746)
- [aclanthology.org/2025.emnlp-demos.14](https://aclanthology.org/2025.emnlp-demos.14/) — EMNLP 2025 Demos
- [arxiv:2502.16789 — AlphaAgent (KDD 2025)](https://arxiv.org/abs/2502.16789) / [ACM KDD 2025](https://dl.acm.org/doi/10.1145/3711896.3736838)
- [arxiv:2505.15155 — RD-Agent-Quant (NeurIPS 2025)](https://arxiv.org/html/2505.15155v2)
- [arxiv:2604.09601 — Hubble v2 (2026-04-14)](https://arxiv.org/abs/2604.09601)
- [arxiv:2407.18690 — RD-Agent 原论文](https://arxiv.org/abs/2407.18690)
- [neurips.cc/virtual/2025/poster/121804](https://neurips.cc/virtual/2025/poster/121804) — RD-Agent-Quant poster

#### 💻 开源实现

- [github.com/microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) — 主仓库
- [github.com/microsoft/qlib](https://github.com/microsoft/qlib) — 同团队 Qlib
- [github.com/microsoft/RD-Agent/releases](https://github.com/microsoft/RD-Agent/releases) — v0.4.0 → v0.8.0 release notes
- [PyPI: rdagent](https://pypi.org/project/rdagent/)

#### 📊 文档 / blog

- [rdagent.readthedocs.io/en/stable/](https://rdagent.readthedocs.io/en/stable/) — v0.8.0 文档
- [rdagent.readthedocs.io/en/stable/scens/quant_agent_fin.html](https://rdagent.readthedocs.io/en/stable/scens/quant_agent_fin.html) — quant agent 文档
- [microsoft.com/research/articles/rd-agent](https://www.microsoft.com/en-us/research/articles/rd-agent-an-open-source-solution-for-smarter-rd/) — MSR 官方 Co-STEER 全名解释
- [saulius.io/blog/automated-quant-research-ai-agents-rd-agent](https://saulius.io/blog/automated-quant-research-ai-agents-rd-agent) — 第三方架构解读

#### 🏢 AIAC 内部对照源

- `backend/agents/core/` 全集(13 文件 3223 行)
- `backend/agents/core/ARCHITECTURE.md`(18 章节设计文档)
- `backend/agents/core/integration.py:265-407`(`run_enhanced_mining` DORMANT + `enhance_existing_node_evaluate` 钩子)
- `backend/agents/core/experiment.py:31-91`(Hypothesis 字段对照来源)
- `backend/agents/core/feedback.py:17-22`(AttributionType enum)
- `backend/selection_strategy.py:135`(既有 DatasetBandit UCB1,与 RD-Agent Thompson 对照)
- `backend/agents/graph/nodes/generation.py:442-524, 676-680`(P2-D negative knowledge active injection,M1 反证位置)
- `backend/knowledge_extraction.py:100-130`(rooted-ordered tree + skeleton 现状,R3 起点)
- `backend/metrics_tracker.py:440`(AttributionType value 字符串渗透位置,S5 反证)

---

## § 9 总结

**调研产出**:
- **RD-Agent v0.8.0 + RD-Agent-Quant NeurIPS 2025** 全披露,**AIAC `agents/core/` 是 DORMANT(计划性待激活),3223 行高质量但生产路径零调用**
- **Alpha-GPT v2 修订**(2025-09)明确写 Llama3 70B + BGE-M3 + 4 层 Hierarchical RAG;v1 原版 (2023-07) 未指定
- **AlphaAgent KDD 2025 三正则公式**(Eq 5 polynomial AST subtree isomorphism)
- **Hubble v2 (2026-04-14)** 5 组件含 Dual-channel RAG + Family-cap top-k=2(v1 无)
- **AIAC 高 ROI gap**:AST isomorphism / Hypothesis-Alignment LLM judge / Family-cap / 4 层 hierarchical RAG / Trace MCTS

**13 项 R 路线图**(R1a/R1b/R1c/R2-R10),按 ROI 分 4 个 Phase:

| Phase | 项 | 工程量 | 价值 |
|---|---|---|---|
| **Phase 0** | R4 启用 P2-D nudge | 0 人日 + A/B | flip flag 即可,验证 active injection 效果 |
| **Phase 1** | R1a + R2 + R3 + R4' | 8-12 人日 | 渐进激活 core + bandit + AST + dual-channel |
| **Phase 2** | R5 + R6 + R7 + R10 | 7-9 人日 | LLM judge + DAG + 半接受 + family-cap |
| **Phase 3** | R1b + R8 + R9 / R1c | 4-6 周大改 | 全 Pipeline 激活 OR deprecated 二选一 |

**关键决策**:R1a(2 人日激活 evaluate shim)+ R4(0 人日 flip flag)是**最优快赢组合**,可在 1 周内完成 + 2 周 A/B 验证。

**与既有调研对照**:
- AlphaGBM 调研 = 工程模式 + nudge(已落地 P0-P2)
- Qlib 调研 = seed 内容 + 学术理论(P3-Q1~Q10 路线图)
- **本调研 = 架构方法学 + AIAC core 现状评估**(P3-R1~R10 路线图)
- 三者 **orthogonal 可并行推进**

---

*本文档由 Plan + 3 Explore + 1 对抗审查 4 段工作流自动生成,与既有调研流程同款。对抗审查 catch 4 MUST FIX + 5 SHOULD FIX 全部并入。实施 P3-R 系列时另开 PR。*
