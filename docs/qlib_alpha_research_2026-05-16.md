# Qlib + Alpha101/158/360 + 学术因子库调研 — 对 AIAC 的可迁移知识

> 调研日期:2026-05-16
> 调研对象:Microsoft Qlib(开源量化平台)+ Alpha101 / Alpha158 / Alpha360 因子库 + 2024-2026 LLM-alpha 学术前沿
> 目的:为 AIAC `external_knowledge.py:ACADEMIC_PATTERNS` 扩充 + P3 路线图设计提供素材
> 配套文档:
> - 上一轮调研:[`alphagbm_skills_research_2026-05-15.md`](alphagbm_skills_research_2026-05-15.md)
> - 跨阶段回顾:[`retrospective_p012_2026-05-16.md`](retrospective_p012_2026-05-16.md)

---

## § 0 项目定性与调研边界

### 0.1 Qlib 是什么

[microsoft/qlib](https://github.com/microsoft/qlib) 是 Microsoft Research Asia(MSRA)2020 年开源的 **AI 导向量化投资平台**,2026-05 当前 **~43k stars**(2025-2026 因与 RD-Agent 整合月增 6k+)。最新稳定 release **v0.9.7(2025-08-15)**。README 自我定位:

> "AI-oriented quant platform … now equipped with **RD-Agent** to automate R&D process"

它本质是**离线 ML + 公共 OHLCV** 量化研究平台:

| 维度 | Qlib | AIAC |
|---|---|---|
| 数据源 | 公共 OHLCV(US/CN/HK)+ 自家撮合 | WorldQuant BRAIN simulate(数千 datafields) |
| 范式 | 离线 ML model 训练 + 离线 backtest | LLM-driven hypothesis 生成 → 在线 simulate → 自演化 KB |
| 生成 alpha | 手工特征 + DL 模型权重学习 | LLM agent 生成表达式 |
| 工作流 | qrun YAML 静态 pipeline | LangGraph 动态分支 + self-correct |
| 知识沉淀 | 论文 + 文档 | KB seed + P2-A macro narrative + P2-D negative pitfall |

**结论**:Qlib 与 AIAC 是**互补**关系,不是替代。Qlib 的因子库 + RD-Agent 架构借鉴是高 ROI 方向。

### 0.2 调研边界

**调研对象**:
- ✅ Qlib 核心架构(5 层)+ Alpha158 + Alpha361(暂不评估 Alpha360)
- ✅ Kakushadze 101 Alphas(arxiv 1601.00991)+ Alpha191 + Open Source Asset Pricing
- ✅ 2024-2026 LLM-alpha 学术前沿(RD-Agent-Quant / AlphaAgent / Hubble 等)
- ✅ Replication Crisis 反例知识源(McLean-Pontiff / Hou-Xue-Zhang)

**不调研**:
- ❌ Qlib RL toolkit(`QlibRL` 聚焦 order execution + portfolio construction,**非 alpha discovery**)
- ❌ Alpha360 具体实现(60×6 OHLCV flatten 设计给深度学习用,与 BRAIN 单表达式范式不兼容)
- ❌ 重复 Alpha-GPT 1.0/2.0 论文(AIAC 已是其后继 + RD-Agent 融合)
- ❌ Chain-of-Alpha 撤稿论文(arxiv 2508.06312)
- ❌ Citadel/Renaissance/Two Sigma LLM 内部使用(无公开披露)

**调研期**:2026-05-16 当日(WebSearch/WebFetch 拿 2026-05 最新数据)

---

## § 1 Qlib 项目全景

### 1.1 项目状态

| 项 | 数据 |
|---|---|
| Stars | **~43,000**(2026-05) |
| 最新 release | **v0.9.7(2025-08-15)** |
| 主分支 commits | 2,065(2026-05) |
| 维护团队 | Microsoft Research Asia — Weiqing Liu / Jiang Bian(同 RD-Agent 团队) |
| PyPI 包 | `pyqlib` 0.9.7,Python 3.8-3.12 兼容,Windows x86-64 / macOS / manylinux2014 wheel |
| 文档 | [qlib.readthedocs.io](https://qlib.readthedocs.io/en/stable/) |
| 论文 | [arxiv:2009.11189](https://arxiv.org/abs/2009.11189) "Qlib: An AI-oriented Quantitative Investment Platform" |

### 1.2 5 层核心架构

```
┌─────────────────────────────────────────────────┐
│  Strategy & NestedExecutor                       │
│  (TopkDropout / WeightStrategy / RL-driven)      │
├─────────────────────────────────────────────────┤
│  Learning Framework                              │
│  • SL (Supervised Learning)                      │
│  • RL (Reinforcement Learning - QlibRL toolkit)  │
├─────────────────────────────────────────────────┤
│  Workflow (qrun YAML driven)                     │
│  qrun benchmarks/LightGBM/workflow_alpha158.yaml │
├─────────────────────────────────────────────────┤
│  Model Zoo (25+ 模型)                            │
│  GBDT: LightGBM / XGBoost / CatBoost / DEnsemble │
│  DL: LSTM/GRU/ALSTM/GATs/Transformer/TFT/TCTS    │
│      TCN/Localformer/IGMTF/ADD/HIST/ADARNN       │
├─────────────────────────────────────────────────┤
│  Data Layer                                      │
│  • Provider (Expression Engine,运行时计算)      │
│  • offline + qlib-server 模式                    │
│  • Public OHLCV (US/CN/HK,日频 + 1 分钟)        │
└─────────────────────────────────────────────────┘
```

来源:[qlib.readthedocs.io/en/stable/component/workflow.html](https://qlib.readthedocs.io/en/stable/component/workflow.html)

### 1.3 Alpha158 设计哲学

**158 个手工特征,4 大类**(`qlib/contrib/data/loader.py`):

| 类别 | 数量 | 例子 |
|---|---|---|
| **KBAR**(K 线形态) | 9 | `KMID = ($close-$open)/$open` / `KLEN` / `KUP` / `KLOW` / `KSFT` |
| **Price**(价格序列) | ~10 | 多窗口价格变化 |
| **Volume**(成交量) | ~10 | 量价 ratio + log |
| **Rolling**(滚动算子)× 5 窗口 | ~130 | `ROC` / `MA` / `STD` / `BETA` / `RSQR` / `RESI` / `MAX/MIN` / `QTLU/QTLD` / `RANK` / `RSV` / `IMAX/IMIN` / `CORR/CORD` / `CNTP/CNTN` / `SUMP/SUMN/SUMD` / `VMA/VSTD/WVMA` |

**默认时序窗口**:`[5, 10, 20, 30, 60]` 共 5 个尺度。

**典型表达式**(BRAIN 等价见 § 4.1):

```python
# Qlib 原版
RESI5  = Resi($close, 5) / $close                                          # 残差比率
WVMA5  = Std(Abs($close/Ref($close,1)-1) * $volume, 5) / (Mean(...) + 1e-12)  # 量价波动率
STD5   = Std($close, 5) / $close                                            # 5 日波动率比
BETA20 = Slope($close, 20) / $close                                         # 20 日线性回归斜率
KMID   = ($close - $open) / $open                                           # K 线主体比
```

**特点**:
- **纯时序**,无 cross-sectional 操作(rank / group_neutralize 等)
- Label 用 `Ref($close,-2)/Ref($close,-1)-1` 因为 A 股 T+1
- 设计给 GBDT / 浅 DL 模型用(非端到端)

来源:[qlib.readthedocs.io/en/stable/advanced/alpha.html](https://qlib.readthedocs.io/en/stable/advanced/alpha.html)

### 1.4 Alpha360 设计

**60 天 × 6 OHLCV 字段拉平**:CLOSE0-59 + OPEN0-59 + HIGH0-59 + LOW0-59 + VWAP0-59 + VOLUME0-59,共 360 维。

- index 0 = 当日,index 59 = 60 日前
- 用 `$close` / `$volume` 各自最新值归一化
- **几乎不做 feature engineering**,设计交给 **LSTM / GRU / Transformer / TFT** 等深度学习模型自学时序特征
- 与 BRAIN 单表达式范式不兼容(BRAIN 要 alpha 表达式生成单个 vector,而不是 360 维 input)
- **AIAC 不评估迁移此架构**(0.2 调研边界已说明)

来源:[qlib.readthedocs.io/en/latest/component/data.html](https://qlib.readthedocs.io/en/latest/component/data.html)

### 1.5 Model Zoo(25+ 模型)

| 流派 | 代表模型 |
|---|---|
| **GBDT** | LightGBM, XGBoost, CatBoost, DoubleEnsemble |
| **时序 DL** | LSTM, GRU, ALSTM, GATs, SFM, TabNet, Transformer, **TFT**, TCTS, TCN, Localformer, IGMTF, ADD, KRNN, Sandwich, **HIST**, ADARNN |
| **RL** | QlibRL toolkit(order execution + portfolio construction,非 alpha discovery) |

来源:[github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)

**与 AIAC 互补性**:
- Qlib 擅长**特征矩阵权重学习**(Alpha158 输入 → GBDT/Transformer 学权重)
- AIAC 擅长**生成新表达式**(LLM hypothesis → BRAIN simulate)
- 两者 orthogonal,可叠加(P3-Q10 候选:`pyqlib` pre-screen 作为 multi-fidelity 新层)

### 1.6 qrun YAML vs LangGraph

```bash
# Qlib:静态 ML pipeline
qrun benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml
# dataset → train → backtest → eval
```

```python
# AIAC:动态分支 + LLM 循环
node_rag_query → node_hypothesis → node_code_gen → node_validate
              → node_simulate → node_evaluate (multi-fidelity)
              → node_self_correct (loop)
```

**Qlib YAML 不能替代 LangGraph**(目标不同),反之亦然。

### 1.7 数据集

| 数据 | 范围 |
|---|---|
| US 股票 OHLCV | 2005+ 日频 + 1 分钟 |
| CN 股票 OHLCV(CSI300/500/100/1000) | 2008+ 日频 + 1 分钟 |
| HK 股票 OHLCV | 部分 |
| **预计算因子值** | ❌ **无**(Qlib 是 expression engine 运行时计算,BRAIN 提供数千预计算字段) |

需 `scripts/get_data.py` 拉取本地。

来源:[qlib.readthedocs.io/en/latest/component/data.html](https://qlib.readthedocs.io/en/latest/component/data.html)

### 1.8 RD-Agent — Qlib 团队的 LLM 继任项目 🌟

[microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) 由 **Qlib 同团队**(MSRA, Weiqing Liu / Jiang Bian)出品,2024-08 首发,**v0.8.0(2025-11-03)**。

**R&D-Agent-Quant NeurIPS 2025**([arxiv:2505.15155](https://arxiv.org/html/2505.15155v2)):
> 报告比传统因子库 **~2× ARR**(年化收益率)+ 用 **70% 更少因子**

架构:
```
Research → Development (Co-STEER 代码生成 agent) → Feedback (multi-armed bandit 调度)
```

支持 OpenAI / Azure / DeepSeek / LiteLLM。

> **🌟 关键发现**:AIAC `backend/agents/core/` 的 RD-Agent-style core(`Hypothesis` / `AlphaExperiment` / `EvoStep` / `ExperimentTrace` / `EvolvingKnowledge` / `HypothesisFeedback`)**直接源于此项目**。回顾 `CLAUDE.md` 第 13 行:
>
> > "AIAC 2.0 is a Human-AI collaborative alpha-mining platform built on the **Alpha-GPT** paradigm fused with **RD-Agent**'s CoSTEER feedback loop."

---

## § 2 学术 Alpha Factor 库

### 2.1 Kakushadze 101 Formulaic Alphas(2016)

[arxiv:1601.00991](https://arxiv.org/abs/1601.00991)(Wilmott Magazine 2016,22 页):

| 项 | 数据 |
|---|---|
| 公式数 | 101 |
| 平均持有期 | 0.6 - 6.4 天 |
| 两两相关均值 | 15.9% |
| 来源 | WorldQuant 公开的真实生产 alpha |
| 完整列表 | 论文 Appendix A |

**AIAC 现状**:[`backend/external_knowledge.py:L503-L549`](../backend/external_knowledge.py) `ACADEMIC_PATTERNS` 已 inline 5 条引用(Alpha#1 / Alpha#2 / Alpha#5 / 价量相关衰减 / EPS revision)。

**开源实现**(可直接 import):
- [`yli188/WorldQuant_alpha101_code`](https://github.com/yli188/WorldQuant_alpha101_code) — Python 完整 101 实现
- [`stefan-jansen/machine-learning-for-trading`](https://github.com/stefan-jansen/machine-learning-for-trading/blob/main/24_alpha_factor_library/03_101_formulaic_alphas.ipynb) ch24 — Jupyter 笔记本完整 101
- [`popbo/alphas`](https://github.com/popbo/alphas) — 多语言实现

**Qlib 与 Alpha101 关系**:Qlib **官方未内置** Alpha101;Alpha158 是独立 ML feature 设计,非 Kakushadze 系。两者平行。

**信息不可得**:论文只给聚合统计(平均 sharpe / 相关性),**未提供逐 alpha 的 sharpe/turnover**。

### 2.2 国泰君安 Alpha191

[2017 中文研报](https://www.gtja.com/),A 股价量短周期 191 个公式。

**开源实现**:
- [`JoinQuant/jqdatasdk/alpha191.py`](https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/alpha191.py) — JoinQuant 平台主流实现
- [`DolphinDBModules/gtja191Alpha`](https://github.com/DolphinDBModules/gtja191Alpha) — DolphinDB 实现
- [`wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191`](https://github.com/wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191) — 与 Alpha101 对照

**与 Alpha101 重合度**:约 30-50% 重合,剩 50% 是 A 股特有(如涨跌停限制 / 委托盘口数据)。

### 2.3 Open Source Asset Pricing(Chen-Zimmermann) 🌟

[openassetpricing.com](https://www.openassetpricing.com/) / [github.com/OpenSourceAP/CrossSection](https://github.com/OpenSourceAP/CrossSection)

> **319 个学术 predictor 复刻**

| 项 | 数据 |
|---|---|
| Predictor 数 | **319 个**(从顶刊论文复刻) |
| Python 包 | `openassetpricing`(2025-10 重写) |
| 数据范围 | 到 2023 年 |
| 数据来源 | CRSP + Compustat(美股) |
| 论文 | "Open Source Cross-Sectional Asset Pricing" |

**对 AIAC 价值**:**最大宝藏** — 单独可让 KB ≥ 300 条 high-quality seed,且每个 predictor 都有学术原文 + 复刻代码 + 统计性质,远胜 Alpha101 的 101 条。

### 2.4 经典 risk-factor 模型 → Five Pillars 映射

| 经典模型 | 因子 | AIAC Pillar 锚 |
|---|---|---|
| **Fama-French 5(2015)** | Market / Size / Value(HML)/ Profitability(RMW)/ Investment(CMA) | momentum / value / quality |
| **Carhart 4(1997)** | + Momentum(UMD) | momentum |
| **Hou-Xue-Zhang q5(2021)** | Market / Size / Investment / ROE / Expected Growth | quality / value |
| **Frazzini-Pedersen BAB(2014)+ 2025 升级 "BAB-Bad-Beta"** | Low-beta vs High-beta | volatility |

最新更新:
- **Hou-Xue-Zhang q5(RoF 2021)** + 2024 数据扩展:[academic.oup.com/rof/article-abstract/25/1/1/5727769](https://academic.oup.com/rof/article-abstract/25/1/1/5727769)
- **Betting Against Bad Beta(Quant Finance 2025)** Sharpe **1.09**:[tandfonline.com/doi/full/10.1080/14697688.2025.2517270](https://www.tandfonline.com/doi/full/10.1080/14697688.2025.2517270)

**WorldQuant 官方 "Five Pillars" 文档**:公开渠道**未找到**。AIAC 的 Five Pillars(momentum/value/quality/volatility/sentiment/other,P2-B 落地)**判定为 AlphaGBM/skills `compare` 调研的项目自定义术语**,与 WQ 官方无直接关系。

### 2.5 Replication Crisis — 反例知识源 🌟

> 学术 alpha 在公开发表后失效的现象,与 AIAC P2-D negative knowledge 反例分支高度互补。

**关键论文**:

| 论文 | 关键数据 |
|---|---|
| **McLean-Pontiff JoF 2016** [DOI 10.1111/jofi.12365](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365) | anomaly **out-of-sample 收益降 26%**,**post-publication 降 58%** |
| **Harvey-Liu-Zhu** | 建议 anomaly 显著性 t-stat 阈值从 2.0 → **3.0** |
| **Hou-Xue-Zhang "Replicating Anomalies"** | **64% anomaly 不显著** |
| **Jensen-Kelly-Pedersen JoF 2023**([doi 10.1111/jofi.13249](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13249)) | 反驳上述,认为大多数 anomaly 仍可复现 |

**对 AIAC 价值**:用于喂 P2-D `negative_knowledge.py` 反例 KB,建立 **"Decayed Alpha" seed list** — 每条带衰减程度(post-pub -58%)+ 失败原因。AIAC 当前最缺这类知识。

---

## § 3 2024-2026 LLM-Alpha 学术前沿

### 3.1 RD-Agent-Quant(NeurIPS 2025)🌟

[arxiv:2505.15155v2](https://arxiv.org/html/2505.15155v2) — **AIAC `agents/core/` 直接来源**(已 § 1.8 详)

**核心架构**(三段):
1. **Research**:LLM 生成 hypothesis
2. **Development**:Co-STEER 代码生成 agent(Sandbox-Test-Evaluate-Refine 循环)
3. **Feedback**:**multi-armed bandit 调度**(arms = 因子方向,reward = 真实通过率)

**结果**:
- vs 传统因子库 **~2× ARR**
- 用 **70% 更少因子**(因子精简)

### 3.2 AlphaAgent(KDD 2025)

[arxiv:2502.16789](https://arxiv.org/abs/2502.16789) / [ACM KDD 2025](https://dl.acm.org/doi/10.1145/3711896.3736838)

**核心**:三正则化反 alpha decay:
1. **AST 相似度正则** — 防生成与既有 alpha 重复结构
2. **假设对齐正则** — 文本假设与代码实现一致性
3. **复杂度正则** — 简洁优先(Occam)

**验证**:CSI500 + S&P500 4 年数据。

**repo**:[`RndmVariableQ/AlphaAgent`](https://github.com/RndmVariableQ/AlphaAgent)

### 3.3 Hubble(arxiv 2604.09601,2026)🌟

[arxiv:2604.09601](https://arxiv.org/abs/2604.09601) — **与 AIAC 架构最近邻**

**架构**:
- **DSL**(domain-specific language)alpha expression 语法
- **AST sandbox**(静态校验 + 安全执行)
- **dual-channel RAG**(success patterns + failure pitfalls,与 P2-D 同款)

**结论**:AIAC 已包含 80%+ Hubble 架构(P0-P2 路线图自然涵盖)。可借鉴的剩余 20% 是 AST sandbox 严格化。

### 3.4 Alpha-GPT 系列

- **v1.0**:[arxiv:2308.00016](https://arxiv.org/abs/2308.00016)(2023)— AIAC 原范式来源之一
- **v2.0**:[arxiv:2402.09746](https://arxiv.org/abs/2402.09746)(2024)— EMNLP 2025 Demos
- **信息不可得**:具体 Sharpe / IC(abstract 未给,需读 PDF)

**AIAC 相对位置**:已是 v1.0 后继 + RD-Agent 融合,v2.0 主要新增 multi-turn refinement(AIAC `node_self_correct` 已实现)。

### 3.5 非 LLM 流派概览

| 项目 | 方法 | arxiv |
|---|---|---|
| **AlphaForge** | NN 生成 alpha | [2406.18394](https://arxiv.org/abs/2406.18394) |
| **RiskMiner** | DRL 因子挖矿 | [2402.07080](https://arxiv.org/abs/2402.07080) |
| **Navigate Alpha Jungle** | MCTS | [2505.11122](https://arxiv.org/abs/2505.11122) |
| **AlphaSAGE** | GFlowNet | [2509.25055](https://arxiv.org/html/2509.25055v1) |
| **Alpha²** | 公式 + DL 混合 | — |

**对 AIAC 意义**:这些是 `genetic_optimizer.py` 设计的学术对照系。GFlowNet / MCTS 可作为 P4+ 长期方向考虑。

### 3.6 综述与失败案例

- **"Evolution of Alpha" 综述 2025**:[arxiv:2505.14727](https://arxiv.org/abs/2505.14727) — 全面覆盖 1996-2025 alpha 挖矿方法学
- **Frontiers ITEE LLM-alpha 综述 2025-11**:[springer DOI](https://link.springer.com/article/10.1631/FITEE.2500386)
- **Chain-of-Alpha 撤稿**:[arxiv:2508.06312](https://arxiv.org/abs/2508.06312) — 警示工业实践审慎
- **CogAlpha**:[arxiv:2511.18850](https://arxiv.org/abs/2511.18850) — 认知 alpha 挖矿
- **QuantaAlpha**:[arxiv:2602.07085](https://arxiv.org/html/2602.07085v1) — 2026 量子启发因子挖矿

**工业实践 LLM-alpha 现状**:
- **Citadel** 立场:LLM 仅做 research assistant,**CTO 明确反对 PM 外包判断**([eFinancialCareers](https://www.efinancialcareers.com/news/hedge-fund-citadel-hired-goldman-sachs-code-cracking-quant-md-for-its-power-trading-team))
- **JPMorgan LLM Suite**:覆盖 20 万员工,但**未公开用于 alpha generation**
- **Renaissance / Two Sigma**:无 LLM 内部使用公开披露
- **学术与工业 gap 明显**

---

## § 4 八大可迁移知识模块

### 4.1 Qlib → BRAIN 算子映射表(核心迁移工具)

约 25 个核心算子,可机器翻译 Alpha158 绝大多数表达式。

| Qlib 算子 | BRAIN 等价 | 备注 |
|---|---|---|
| `Mean(x, N)` | `ts_mean(x, N)` | 直接 |
| `Std(x, N)` | `ts_std_dev(x, N)` | 直接 |
| `Var(x, N)` | `ts_std_dev(x, N) * ts_std_dev(x, N)` | 平方 |
| `Sum(x, N)` | `ts_sum(x, N)` | 直接 |
| `Min(x, N)` / `Max(x, N)` | `ts_min(x, N)` / `ts_max(x, N)` | 直接 |
| `Median(x, N)` | `ts_median(x, N)` | 直接 |
| `Quantile(x, N, q)` | `ts_quantile(x, q, N)` | 参数顺序变 |
| **`Ref(x, -N)`** | **`ts_delay(x, N)`** | **符号反转!** |
| `Slope(x, N)` | `ts_regression(x, ts_step(N), N)` | 多参数 |
| `Corr(x, y, N)` | `ts_corr(x, y, N)` | 直接 |
| `Cov(x, y, N)` | `ts_covariance(x, y, N)` | 直接 |
| `Resi(x, N)` | 合成:`subtract(x, ts_regression(x, ts_step(N), N) * ts_step(N))` | 复合 |
| `RSV(x, N)` | `divide(subtract(x, ts_min(x, N)), subtract(ts_max(x, N), ts_min(x, N)))` | 复合 |
| **`Rank(x, N)`**(时序!) | **`ts_rank(x, N)`** | ⚠ **Qlib `Rank` 是时序 percentile,不是 cross-sectional!** |
| `IMAX(x, N)` / `IMIN(x, N)` | `ts_arg_max(x, N)` / `ts_arg_min(x, N)` | 直接 |
| `CORD(x, N)` | 合成 | 复合 |
| `WMA(x, N)` | `ts_decay_linear(x, N)` | 直接 |
| `EMA(x, N)` | `ts_decay_exp_window(x, N)` | 直接 |
| `Abs(x)` | `abs(x)` | 直接 |
| `Log(x)` | `log(x)` | 直接 |
| `Power(x, p)` | `signed_power(x, p)` 或 `power(x, p)` | 双版本 |
| `If(cond, t, f)` | `if_else(cond, t, f)` | 直接 |
| `Greater(x, y)` / `Less(x, y)` | `max(x, y)` / `min(x, y)` | 直接 |
| **`$close`** | **`close`** | **`$` 前缀剥离!** |
| **`$volume`** | **`volume`** | 同 |

**关键陷阱**:
1. ⚠ `Ref(x, -N)` 在 Qlib 是"取过去 N 天的值",BRAIN `ts_delay(x, N)` 同义但**符号反转**(N 正)
2. ⚠ Qlib `Rank(x, N)` 是**时序**百分位,**不是** BRAIN `rank(x)` 的 cross-sectional rank
3. ⚠ Label `Ref($close, -2) / Ref($close, -1) - 1`(T+1 调整)在 BRAIN 是 `returns` 字段,**不需要手工算**

### 4.2 Alpha158 KBAR + Rolling 表达式作为 seed(~150 条)

合成方案:**写一张 Qlib → BRAIN 映射表 → 自动展开 158 表达式 × 5 窗口 ≈ 150+ 条高质量种子 alpha**。

数据流:
```
Qlib Alpha158 字面 (Mean/Std/Rank...)
  → Qlib→BRAIN 算子翻译器 (qlib_translator.py 新建)
  → BRAIN fastexpr ✅
  → ExternalKnowledge(source="qlib_alpha158", ...)
  → ExternalKnowledgeSyncer.import_curated_patterns()
  → KnowledgeEntry (entry_type="SUCCESS_PATTERN", ...) 
```

预期 1-2 人日 + ~25 行算子表 + ~50 行自动展开器 = 150 条 seed。

### 4.3 Kakushadze 101 Appendix A 直抄(106 条)

已是 WorldQuant 体系内公式,**不需要翻译**(Kakushadze 论文用的就是 BRAIN fastexpr 风)。

源:[`yli188/WorldQuant_alpha101_code`](https://github.com/yli188/WorldQuant_alpha101_code)

数据流:
```
yli188/WorldQuant_alpha101_code/Alphas101.py
  → 提取 101 个 alpha 表达式字符串
  → ExternalKnowledge(source="kakushadze_2016", ...)
  → import_curated_patterns()
```

预期 1 人日 = 101 条 seed。

### 4.4 Open Source Asset Pricing 319 predictor 🌟

[openassetpricing.com](https://www.openassetpricing.com/) Python 包 `openassetpricing`(2025-10 重写),数据到 2023。

预期 2 人日 = 200+ 条高质量学术 predictor seed。

**与 4.2 + 4.3 互补**:Qlib 是 ML feature(粗),Kakushadze 是 production alpha(中),Open Source AP 是学术 predictor(精)。

### 4.5 Alpha191 选 30-50 条 A 股因子作为 region=CHN seed

Alpha191 与 Alpha101 有 30-50% 重合;选**与 101 不重复且对应 BRAIN CHN region 数据存在**的因子,标注 `region="CHN"` + `horizon="short"`。

预期 1-2 人日 = 30-50 条 CHN seed。

### 4.6 RD-Agent bandit-arm scheduling(arxiv 2505.15155 §3-4)

> AIAC `backend/dataset_selector.py` 已是 bandit(P0-P2 之前的设计),**推广 bandit 到 hypothesis 方向维度**(arms = 挖矿方向,reward = 真实通过率)

**同源同团队同范式**(RD-Agent-Quant 来自 AIAC `agents/core/` 同源)。

预期 3-5 人日 = 升级 `dataset_selector.py` 到双层 bandit。

### 4.7 AlphaAgent AST 相似度正则(`backend/diversity_tracker.py` 加 AST 距离)

[AlphaAgent KDD 2025](https://arxiv.org/abs/2502.16789) 三正则的核心**反 alpha decay**(防新 alpha 与既有过度相似)。

- AIAC `diversity_tracker.py` 当前 5 维(dataset/field/operator/settings/pillar)
- 新增**第 6 维:AST 相似度距离**(`expression_to_skeleton` 已有,扩成 distance)

预期 3 人日。

### 4.8 Replication Crisis 反例知识(`negative_knowledge.py` 扩充)

将 McLean-Pontiff 26% / 58% 衰减 + Hou-Xue-Zhang 64% 不显著 + Harvey-Liu-Zhu 阈值升级 写成 **Decayed Alpha seed list**,喂入 P2-D `negative_knowledge.py` 反例分支。

每条带:
- `decay_pct`(post-publication 衰减幅度)
- `failure_mode`(reverse / sample-selection / data-mining)
- `theoretical_anchor`(原论文 DOI)

预期 1-2 人日 = 50+ Decayed Alpha seed。

---

## § 5 AIAC P3 落地优先级路线图

| 优先级 | 项 | 来源 | 落地文件 | 工程量 |
|---|---|---|---|---|
| 🔴 **P3-Q1** | **Kakushadze 101 完整移植**(5→106) | Kakushadze 2016 | `external_knowledge.py:L503` | 1 人日 |
| 🔴 **P3-Q2** | **Open Source Asset Pricing 319 predictor 一次性 import** | Chen-Zimmermann | `external_knowledge.py` + 新 importer | 2 人日 |
| 🔴 **P3-Q3** | **Alpha158 表达式 × 5 窗口 ≈ 150 条 seed**(写 Qlib→BRAIN 映射器) | Qlib | `external_knowledge.py` + 新 `qlib_translator.py` | 2-3 人日 |
| 🟡 P3-Q4 | `pillar_classifier` 加 Qlib operator alias(Mean/Std/Rank 等) | Qlib | `pillar_classifier.py:OPERATOR_TO_PILLAR` | 0.5 人日 |
| 🟡 P3-Q5 | Five Pillars 加 `theoretical_anchor`(FF5/q5/BAB 显式映射) | 经典 risk-factor | `pillar_classifier.py` + `macro_narratives.py` | 1 人日 |
| 🟡 P3-Q6 | Alpha191 选 30-50 条 A 股因子作为 region=CHN seed | 国泰君安 | `external_knowledge.py` | 1-2 人日 |
| 🟢 P3-Q7 | bandit-arm 推广到 hypothesis 方向维度 | RD-Agent-Quant | `dataset_selector.py` 推广 | 3-5 人日 |
| 🟢 P3-Q8 | AST 相似度 anti-decay 正则(AlphaAgent 三正则) | AlphaAgent | `diversity_tracker.py` 加 AST distance | 3 人日 |
| 🟢 P3-Q9 | McLean-Pontiff Decayed Alpha 表 → negative KB | Replication Crisis | `negative_knowledge.py` + 新 seed | 1-2 人日 |
| 🟢 P3-Q10 | `pyqlib` pre-screen as multi-fidelity 新层 | Qlib | `multi_fidelity_eval.py` | 5 人日 |

### Phase 1(最高 ROI,1-2 周内可上线)

**Q1 + Q2 + Q3 = 400+ 条 KB seed**

- 直接扩 `ACADEMIC_PATTERNS` 现有 5 条 → ~400 条(80× 扩张)
- 后续 P2-A macro_narrative LLM 批生成有更丰富材料启动
- 后续 P2-D negative knowledge 有更多对照 reference
- `baseline.json:kb_total_entries` 自然从 59 → 460+,导入后 `--save-baseline` 刷新即可

### Phase 2(中 ROI)

**Q4 + Q5 + Q6 = 完善 pillar 与 theoretical anchor + 跨 region seed**

- pillar_classifier 完备(支持 Qlib operator 命名)
- Five Pillars 与 FF5/q5 学术挂钩,prompt 可引用
- CHN region seed 补全(目前只有 USA 重点)

### Phase 3(架构借鉴,3-5 周)

**Q7 + Q8 + Q9 + Q10 = 高阶能力升级**

- bandit-arm 双层(已有 dataset 维度 + 新 hypothesis 方向维度)
- AST 距离正则反 decay
- Replication Crisis 喂 P2-D
- `pyqlib` pre-screen 作为 multi-fidelity 新层(BRAIN simulate 前的免费筛)

---

## § 6 不做的事

1. **不做 Qlib RL toolkit 集成** — `QlibRL` 聚焦 order execution + portfolio construction,**非 alpha discovery**,与 AIAC 痛点不直接相关
2. **不做 Alpha360 复制** — 60×6 OHLCV flatten 假设深度学习自学特征,与 BRAIN 单表达式范式不兼容
3. **不替换 LangGraph 为 qrun YAML** — 目标不同,LangGraph 动态分支 / qrun 静态 ML pipeline
4. **不在 AIAC 内引入 Qlib ML model** — P0-P2 是 LLM-driven hypothesis,引 ML 改变范式
5. **不引入 Qlib 公共 OHLCV 数据** — BRAIN 已提供,公共数据精度差且不一致
6. **不重复 Alpha-GPT 1.0/2.0 论文方法** — AIAC 已是 Alpha-GPT 后继 + RD-Agent 范式融合
7. **不复制 Chain-of-Alpha 已撤稿论文方法**(arxiv 2508.06312)
8. **不直接抄 Citadel/Renaissance LLM 用法**(无公开披露)

---

## § 7 信息不可得(明示)

- **101 Alphas 逐 alpha Sharpe/turnover**:论文 Appendix A 只给公式,**未给逐 alpha 性能数据**(只有聚合统计:平均持有期 0.6-6.4 天 / 平均相关 15.9%)
- **Alpha-GPT 2.0 具体 Sharpe/IC**:abstract 未给,需读 PDF 全文
- **Renaissance / Two Sigma LLM 内部使用**:无任何公开披露
- **WorldQuant 官方 "Five Pillars" 文档**:公开渠道未找到。AIAC 的 Five Pillars(P2-B)**判定为项目自定义术语,源于 AlphaGBM/skills `compare` 调研而非 WQ 官方分类**

---

## § 8 附录 URL 索引

### 📚 学术论文

- [arxiv:1601.00991 — Kakushadze 101 Formulaic Alphas](https://arxiv.org/abs/1601.00991)
- [arxiv:2009.11189 — Qlib paper](https://arxiv.org/abs/2009.11189)
- [arxiv:2308.00016 — Alpha-GPT v1](https://arxiv.org/abs/2308.00016)
- [arxiv:2402.09746 — Alpha-GPT 2.0](https://arxiv.org/abs/2402.09746)
- [arxiv:2502.16789 — AlphaAgent (KDD 2025)](https://arxiv.org/abs/2502.16789)
- [arxiv:2505.15155 — RD-Agent-Quant (NeurIPS 2025)](https://arxiv.org/html/2505.15155v2)
- [arxiv:2604.09601 — Hubble (2026)](https://arxiv.org/abs/2604.09601)
- [arxiv:2406.18394 — AlphaForge](https://arxiv.org/abs/2406.18394)
- [arxiv:2402.07080 — RiskMiner](https://arxiv.org/abs/2402.07080)
- [arxiv:2505.11122 — Navigate Alpha Jungle](https://arxiv.org/abs/2505.11122)
- [arxiv:2509.25055 — AlphaSAGE](https://arxiv.org/html/2509.25055v1)
- [arxiv:2511.18850 — CogAlpha](https://arxiv.org/abs/2511.18850)
- [arxiv:2602.07085 — QuantaAlpha (2026)](https://arxiv.org/html/2602.07085v1)
- [arxiv:2505.14727 — Evolution of Alpha 综述](https://arxiv.org/abs/2505.14727)
- [Frontiers ITEE LLM-alpha 综述 2025-11](https://link.springer.com/article/10.1631/FITEE.2500386)
- [JoF 2016 — McLean-Pontiff](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365)
- [JoF 2023 — Jensen-Kelly-Pedersen (反驳 Replication Crisis)](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13249)
- [RoF 2021 — Hou-Xue-Zhang q5](https://academic.oup.com/rof/article-abstract/25/1/1/5727769)
- [Quant Finance 2025 — Betting Against Bad Beta](https://www.tandfonline.com/doi/full/10.1080/14697688.2025.2517270)

### 💻 开源实现

- [microsoft/qlib](https://github.com/microsoft/qlib) — 主仓库
- [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) — LLM 继任项目
- [yli188/WorldQuant_alpha101_code](https://github.com/yli188/WorldQuant_alpha101_code) — Alpha101 Python
- [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) — ch24 Alpha101 notebook
- [popbo/alphas](https://github.com/popbo/alphas) — 多语言 Alpha101 实现
- [JoinQuant/jqdatasdk/alpha191.py](https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/alpha191.py) — Alpha191 主流实现
- [DolphinDBModules/gtja191Alpha](https://github.com/DolphinDBModules/gtja191Alpha) — DolphinDB Alpha191
- [wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191](https://github.com/wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191) — Alpha101 + Alpha191 对照
- [OpenSourceAP/CrossSection](https://github.com/OpenSourceAP/CrossSection) — 319 学术 predictor 复刻
- [RndmVariableQ/AlphaAgent](https://github.com/RndmVariableQ/AlphaAgent) — AlphaAgent 实现

### 🏢 工业实践

- [Citadel LLM 立场(eFinancialCareers)](https://www.efinancialcareers.com/news/hedge-fund-citadel-hired-goldman-sachs-code-cracking-quant-md-for-its-power-trading-team)
- [LLMQuant 2025 综述](https://llmquant.substack.com/p/2025-the-year-quant-finance-stopped)

### 📊 数据 / 文档

- [Open Source Asset Pricing 主站](https://www.openassetpricing.com/)
- [Hou-Xue-Zhang q5 全球数据](http://global-q.org/factors.html)
- [PyPI: pyqlib](https://pypi.org/project/pyqlib/)
- [Qlib 文档](https://qlib.readthedocs.io/en/stable/)
- [Qlib alpha builder 文档](https://qlib.readthedocs.io/en/stable/advanced/alpha.html)
- [Qlib workflow 文档](https://qlib.readthedocs.io/en/stable/component/workflow.html)
- [Qlib data 文档](https://qlib.readthedocs.io/en/latest/component/data.html)
- [Qlib RL 文档](https://qlib.readthedocs.io/en/stable/component/rl/overall.html)
- [WorldQuant BRAIN 算子文档](https://platform.worldquantbrain.com/learn/data-and-operators/detailed-operator-descriptions)
- [Microsoft Qlib benchmarks](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)

---

## § 9 总结

**调研产出**:
- **Qlib + RD-Agent-Quant** 是 AIAC `agents/core/` 的同源项目,2025-2026 持续演进
- **400+ 条高质量 KB seed** 可直接从 Kakushadze 101 + Open Source Asset Pricing + Alpha158 一次性导入(P3-Q1 + Q2 + Q3)
- **Qlib → BRAIN 25 条算子映射表** 是核心迁移工具,可机器翻译 Alpha158 绝大多数
- **2024-2026 LLM-alpha 学术前沿** 已识别 8+ 个相关项目,Hubble 与 AIAC 架构最近邻
- **Replication Crisis 反例知识** 是 AIAC P2-D negative KB 最大未开采源

**10 项 P3-Q 路线图**(Q1-Q10)按 ROI 排序,分 3 个 Phase 推进:

| Phase | 项 | 工程量 | 价值 |
|---|---|---|---|
| **Phase 1** | Q1-Q3 | ~5-6 人日 | KB seed 80× 扩张(5→400+) |
| Phase 2 | Q4-Q6 | ~3-4 人日 | pillar 完备 + 跨 region |
| Phase 3 | Q7-Q10 | ~12-14 人日 | 架构借鉴 RD-Agent-Quant |

**与 AlphaGBM/skills 调研对照**:本次调研产出的 P3-Q 路线图与 AlphaGBM 路线图**完全 orthogonal**(AlphaGBM 是工程模式 + nudge,Qlib 是 seed 内容 + 学术理论),可并行推进。

---

*本文档由跨调研工作流自动生成 — Explore agent + Plan agent 三段串行,与既有调研流程同款。预期作为 P3 路线图设计的素材,实施时另开 PR。*
