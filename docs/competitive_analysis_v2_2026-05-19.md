# 竞品分析 v2 — 顶级量化交易系统 alpha 挖矿

> **文档日期**:2026-05-19
> **承前**:[`competitive_analysis_ai_alpha_mining_2026-05-17.md`](competitive_analysis_ai_alpha_mining_2026-05-17.md)(v1,13 学界系统 + 工业 1 段简引)
> **本文增量**:
> 1. 工业顶级 8 家深挖(Renaissance / Citadel / D.E. Shaw / Two Sigma / Millennium / Jane Street / Point72 / AQR + Bridgewater bonus)
> 2. AIAC 在 2026-05-19 现状下的重定位(cascade 已退役 / 33 production flag ON / 22+ 学界机制已对齐)
> 3. 2026-05-17 之后新论文 / 之前遗漏的 9 篇增量
> **决策影响**:为 Phase 4(G9 / G10 / R11-R14 / flat-F4)路线提供工业 + 学界双源依据

---

## 1. 关键发现

### 1.1 与 v1 对比的范式变化

| | 2026-05-17(v1) | 2026-05-19(v2) |
|---|---|---|
| AIAC 架构 | **cascade T1/T2/T3 孤例** | **flat-only**(cascade 已 2026-05-18 phase15-D 退役) |
| 学界对照 | 13 系统;AIAC 0 个 SOTA 机制 | 22 系统;AIAC 已对齐 14 项 SOTA(RD-Agent forest/DAG/CoSTEER、AlphaAgent AST/judge、Hubble dual-RAG/family-cap、QuantaAlpha crossover、Alpha-GPT hierarchical RAG、Co-STEER retry/mutate) |
| 工业派对照 | 1 段(Citadel CTO 立场)+ 2 句 | 8 家深挖 + 5 共性 + 5 gap |
| AIAC 排位 | **outlier** | **学界覆盖 top tier;工业风控/平台维度仍落后** |

### 1.2 三条 take-away

1. **AIAC 已脱离 "孤例" 状态** — 22 学界机制中 14 已对齐(64%),平均吸收延迟 ≤30 天;落后 5 项集中在 *2026-05-17 之后新论文*(AlphaCrafter / CogAlpha / AlphaLogics / AlphaCFG / FactorMiner)。
2. **工业派共识与 AIAC v1 设计有张力** — 8 家工业实践收敛到 "**LLM 是 research assistant,不是 alpha-expression-author**"(Citadel / Two Sigma / Bridgewater AIA 显式)。AIAC 当前 `node_code_gen` 让 LLM 直出 expression,与共识相反。
3. **AIAC 真正的 gap 在 *评估层 + 风控层 + 平台维度*,不在生成层** — 工业派的护城河是 factor-lens / capacity-cap / pod-level stop-loss / multi-strategy 多 region 并行 / 研究平台 — AIAC 这 5 项要么缺失要么薄弱。

---

## 2. 调研方法

| 信息源 | 数量 | 来源 |
|---|---|---|
| 学界论文(2026-05-17 后) | 6 + 3 遗漏 | arxiv / Springer / ICLR FinAI Workshop |
| 工业一手 | 8 家公司 + 1 bonus | careers / investor / SEC filing / 公司 engineering blog |
| 工业二手 | ~50 篇 | Bloomberg / FT / hedgeco / Quartr / Resonanz / efinancialcareers |
| AIAC 自审 | 6 文档 | flag_lifecycle / master_implementation_plan / phase15 / memory index / config.py 28 ENABLE_* flag |

每条 fact 标 `[一手:URL]` / `[二手:URL]` / `[三手 hearsay]` / `[公开不可得]`。

---

## 3. 总览矩阵(学术 + 工业,~25 系统)

### 3.1 学术 / 开源 系统(承自 v1 §3,新增 §7 列出的 9 个)

| 系统 | 年份 | 生成机制 | 调度 | 反馈 | 抗 decay | AIAC 对齐 |
|---|---|---|---|---|---|---|
| **AIAC**(2026-05-19) | 2026 | LLM + typed hypothesis,flat-only | DAG (R6) + bandit (G1) | self-correct (R7) + LLM judge (R5) + dual-channel RAG (R4') | KB negative + pillar (P2-B/D) + family-cap (R10) + AST gate (G3 shadow) | — |
| **RD-Agent-Quant**(NeurIPS 2025)⭐ | 2025 | LLM hypothesis forest → Co-STEER DAG | flat + multi-armed bandit 方向 | bandit reward | 因子精简 | ✅ G8 forest / R6 DAG / G1 bandit / R1b CoSTEER |
| **Alpha-GPT v1/v2**(EMNLP 2025) | 2025 | LLM seed + GP 邻域 | 3-阶段 flat / 4 层 Hier RAG | natural language analyst | 多轮 human-in-loop | ✅ R8 hierarchical RAG |
| **AlphaAgent**(KDD 2025) | 2025 | LLM flat | flat + 三正则化 | 单流回测 | AST subtree isomorphism | ✅ R5 judge / G3 AST gate / R10 family-cap |
| **Hubble v2**(2026-04, 2604.09601) | 2026 | LLM + DSL flat | family-cap top-k=2 | dual-channel RAG | negative "avoid like" 模板 | ✅ R4' dual-channel / R10 family-cap |
| **QuantaAlpha**(2026-02, 2602.07085) | 2026 | LLM + trajectory mutate/crossover | flat 进化 + 拓扑 | trajectory replay | semantic consistency + crowding | ✅ G5 crossover |
| **Chain-of-Alpha**(撤稿,2508.06312) | 2025 | dual-chain | 两链迭代 | backtest + prior | — | ✗(已撤稿) |
| **AlphaEvolve / AlphaGen / AlphaSAGE** | — | GP / DRL / GFlowNet | flat | — | 内置 | ✗ |
| **Navigate Alpha Jungle**(2505.11122) | 2025 | LLM + MCTS | tree search | UCB rollout | tree pruning | ⚠️ R6 是 MCTS-lite |
| **AlphaCrafter**(NEW, 2605.05580, 2026-05-07) | 2026 | full-stack Miner/Screener/Trader 多 agent | regime-conditioned ensemble | persistent shared memory | — | ❌ AIAC 缺 portfolio+execution |
| **CogAlpha**(NEW, 2511.18850, 2025-11) | 2026 | 7 层 cognitive agent + "Thinking Evolution" | per-layer specialization | multi-agent QA | 5-mode paraphrase | ❌ AIAC flat 单 prompt |
| **AlphaLogics**(NEW, 2603.20247, 2026-03) | 2026 | 5 agent reverse-mine Alpha101/191/158/360 | logic library refine | logic-as-asset | logic 库每周演化 | ❌ AIAC KB 单向 ingest |
| **FactorMiner**(NEW, 2602.14670, 2026-02) | 2026 | Ralph Loop(R/G/E/Distill)+ Modular Skills | global library 互补 | Experience Memory + **forbidden region** | hard family ban N rounds | ❌ AIAC R10 是 top-k 软限,无硬封禁 |
| **AlphaCFG**(NEW, 2601.22119, 2026-01) | 2026 | CFG-guided MCTS | grammar-aware policy net | syntax sensitive | 事前 grammar 约束 | ❌ AIAC validator 是事后 |
| **TLRS**(NEW, 2507.20263) | 2025 | PPO + RPN token-level | trajectory reward shaping | dense expert subseq match | reward centering | ❌ AIAC 无 RL |
| **AlphaR1**(2512.23515) | 2025 | LLM reasoning + RL screening | RL post-hoc filter | reward model | — | ⚠️ R5 是简化版 |

### 3.2 工业派 8 家(本文 §5 详)

| 公司 | AUM | Alpha 类型 | 生成机制 | LLM 立场(2026) | 对 AIAC 关键启示 |
|---|---|---|---|---|---|
| **Renaissance Technologies** | ~$130B | mid-freq 1-2 日 | 纯系统化 ML(无 narrative) | 公开不可得(暗箱) | capacity-cap($10B Medallion)/ signal-leak jitter |
| **Citadel**(对冲) | ~$71B | multi-strategy pod(5 业务线) | pod autonomy + 中央 risk | "LLM 是 research assistant,不产 alpha"(Griffin 2025-10) | 中央 risk overlay / LLM-as-assistant |
| **Citadel Securities** | n/a(market maker) | HFT 毫秒 | 系统化 ML 端到端 | "AI as productivity tool"(Peng Zhao) | — |
| **D.E. Shaw** | ~$60B | 多策略 stat arb + macro | 系统化 + 独立 ML group(2018) | 模糊 | Python-first 轻量化 |
| **Two Sigma** | ~$60B | mid-freq systematic equity | 250 PhD + 100k sim/day | "5+ 年 GenAI 内部" + "AI-first 2026 mandate" | **factor lens 18 个**(R13)/ Beakerx 平台 |
| **Millennium** | ~$78B | ~320 multi-strategy pods | 100% pod autonomy | pod 自定 | **5%/7.5% hard stop-loss**(R14)/ 320 pods 多策略 |
| **Jane Street** | $40B+ 自营 | HFT 市做 + ETF arb + 期权 | OCaml + 5 年 DL signal | "DL is the future" / 不强调 LLM | 结构性 alpha / 自研 DSL stack |
| **Point72 / Cubist** | ~$50B(45% Cubist) | fundamental + Cubist mid-freq quant + Turion AI 主题 | Cohen + Cubist 双轨 | Turion 是 *投 AI 公司*,不是用 AI 找 alpha | Quant Academy rotational |
| **AQR** | ~$130B | systematic factor + ML overlay | factor zoo + Turing Equities/Macro ML | "AI believer";1/5 主信号已 ML | **Kelly/Xiu autoencoder + LLM expected-return paper** 直接 KB seed |
| Bridgewater(bonus) | ~$92B | systematic macro + AIA Labs | rules + AIA(OpenAI/Anthropic/Perplexity) | AIA Macro 2025 +11.9% / $5B AUM | LLM-as-assistant + rules-engine 共生 |

---

## 4. AIAC 在 2026-05-19 现状下的重定位

### 4.1 与 v1 的状态差(2 天 41 ship + 6 phase 全 close)

- **退役**:cascade T1/T2/T3 phase 切换(phase15-D 2026-05-18)+ 11 个 ENABLE flag 退役(2026-05-19 lifecycle batch)
- **新增**:flat-F1/F2/F3 + R1b.1-1.5 CoSTEER + R5 LLM judge + R6 DAG + R7 self-correct semi-accept + R8 hierarchical RAG + R9 sim cache + R10 family-cap + Q10 pyqlib + G1 direction bandit + G2 cost telemetry + G3 AST originality + G4 dual-channel RAG 补强 + G5 trajectory crossover + G8 hypothesis forest + A+ CircuitBreaker
- **production flag ON**:33 个(v1 时 9 个)
- **节点 / 模块**:`agents/core/` 3223 行 RD-Agent 兼容代码 已激活(R1b.4 typed pipeline)

### 4.2 已对齐的 14 学界机制清单

| AIAC flag / 机制 | 对应学界源 | Ship 日期 |
|---|---|---|
| `ENABLE_R1B_RETRY_LOOP` | Co-STEER(RD-Agent) | 2026-05-18 |
| `ENABLE_R1B_HYPOTHESIS_MUTATE` | Co-STEER + CoSTEER 闭环 | 2026-05-18 |
| `ENABLE_R1B_FAILURE_TREE` | RD-Agent ExperimentTrace DAG | 2026-05-18 |
| `ENABLE_R1B_TYPED_PIPELINE` | RD-Agent AlphaExperiment | 2026-05-18 |
| `ENABLE_LLM_JUDGE` (R5) | AlphaAgent Eq. 7 双向 LLM judge | 2026-05-18 |
| `ENABLE_DAG_TRACE` (R6) | RD-Agent ExperimentTrace + MCTS-lite | 2026-05-18 |
| `ENABLE_FAMILY_CAP` (R10) | Hubble v2 Table 1 | 2026-05-18 |
| `ENABLE_HIERARCHICAL_RAG` (R8) | Alpha-GPT v2 4-layer Hier RAG | 2026-05-18 |
| `ENABLE_SIMULATION_CACHE` (R9) | 普遍工程实践 | 2026-05-18 |
| `ENABLE_DIRECTION_BANDIT` (G1/R2-Q7) | RD-Agent direction bandit | 2026-05-17/19 |
| `ENABLE_DUAL_CHANNEL_RAG` (R4') | Hubble v2 negative-channel | 2026-05-17 |
| `ENABLE_AST_ORIGINALITY_GATE` (G3) | AlphaAgent KDD 2025 Eq. 5 | 2026-05-19 |
| `ENABLE_HYPOTHESIS_FOREST_REUSE` (G8) | RD-Agent NeurIPS 2025 cross-task forest | 2026-05-19 |
| `ENABLE_G5_CROSSOVER` (G5) | QuantaAlpha 2602.07085 | 2026-05-19 |

### 4.3 AIAC 排位结论

- **学界覆盖**:14/22 = 64%,**在 2026-05 学界处于 top tier**。剩 8 项主要落在 *2026-05-17 之后的新论文*(覆盖延迟 < 30 天,可接受)和 *non-LLM 流派*(GFlowNet / DRL / 纯 GP — 与 AIAC 路线主动不重合)。
- **工业派落后**:**评估层(factor lens)/ 风控层(stop-loss)/ 平台维度(multi-pod)** 三个领域,工业派 8 家普遍领先于 AIAC。
- **设计哲学张力**:AIAC v1 让 LLM 直出 expression,工业 8 家共识是 LLM 做 research assistant。需要 R12 dual-mode 拉回共识区。

---

## 5. 工业派 8 家深挖

### 5.1 Renaissance Technologies

**Alpha 类型**:mid-freq 1-2 日 holding(Medallion),日内 150k-300k trades [二手:wikipedia.org/wiki/Renaissance_Technologies / quantvps.com/blog/jim-simons-trading-strategy]。Medallion 净年化 ~39%(1988-2021)。

**生成机制**:纯数据驱动系统化挖矿。Simons:"We don't start with models. We start with data." Peter Brown(CEO):"We don't know any economics" [二手:trendspider / wikipedia]。聘人偏物理 / 数学 / 语言学(Mercer/Brown 来自 IBM speech recognition team)。petabyte 级 data warehouse。

**LLM 立场**:公开不可得 — 8 家最少公开者。

**团队结构**:~300 人 East Setauket campus,Linux/JVM/PostgreSQL/Kotlin back office stack [二手:news.ycombinator.com #18359263]。C++ 在核心组。

**Decay 应对**:Medallion 严格 capacity-cap($10B 上限,LP 全员工)[二手:quartr]。执行时刻意 randomize 进场时间防 leak signal。

**对 AIAC 启示**:
- 学 **capacity-cap 思维** → 加 **R11 `alpha_capacity_estimator`**(turnover × universe × ADV 算单 alpha $ capacity,进 score)
- 学 **signal-leak 防御** → submit 时机做 jitter,防同一 fingerprint 反复打同一时窗
- *不学* 纯黑盒 — AIAC 的 hypothesis-driven explainable pipeline 不应回退

### 5.2 Citadel

**Alpha 类型**:5 业务线 multi-strategy pod — Equities / Tactical / Fixed Income & Macro / GQS(2012,纯量化)/ Credit & Convertibles [一手:citadel.com/what-we-do/global-quantitative-strategies]。GQS 跨 30+ 国 15,000+ securities 算法化。Wellington flagship 自 1990 净年化 19.2%。

**LLM 立场**:Griffin 反复 — 2026 Davos "GenAI garbage 不能产 alpha" [二手:bloomberg/2025-10-15],2026-05 改口"profoundly more powerful" [二手:fortune/2026-05-18]。**关键 distinguish**:Citadel 用 LLM 是 *research assistant*(equity chatbot 扫 filings/transcripts/proprietary strategies)[二手:ai-street.co],**不直接产 alpha 表达式**。

**风险管理**:著名中央 risk overlay — pod 在中央 risk framework 内运作,而非完全独立。2025 quant unwind 期间表现优于 multi-strat peers 暗示 crowding-defense 强 [二手:hedgeweek]。

**对 AIAC 启示**:
- 学 **LLM-as-research-assistant 定位** → **R12 dual-mode**:`LLM_MODE=assistant` 时 LLM 只提 hypothesis,expression 由 GA/template 生成;`LLM_MODE=author` 时 LLM 直接出 expression。默认应是 assistant
- 学 **中央 risk overlay** → 加 ops-level "total exposure budget" 防 cross-task 同方向堆积

### 5.3 D.E. Shaw

**Alpha 类型**:多策略 + discretionary;系统化 stat arb 历史曾占美股 volume 几个百分点 [二手:rupakghose.substack]。

**生成机制**:2018 招 Pedro Domingos(UW CS Prof)成立独立 ML group,JD "develop new tradeable signals" [二手:prnewswire / syncedreview]。Domingos 2019 离职,ML 工作内部化 [二手:wikipedia/Pedro_Domingos]。

**团队结构**:Python(pandas/numpy)pervasive,C++ for HFT-critical。赞助 IPython/Jupyter/NumPy [二手:medium.com/@tzjy]。

**对 AIAC 启示**:
- 学 **Python-first researcher-owned pipeline** — researcher 写自己的 pipeline 而非堆重 framework
- *不学* **单点 ML czar** — Domingos 1 年走人案例表明 hire-a-star 战略对量化研究不可靠;AIAC 的多 agent 设计更合适

### 5.4 Two Sigma

**Alpha 类型**:mid-freq systematic equity + macro factor + alternative data signals。

**生成机制**:~1,700 employees / 250+ PhDs / 1,000+ data scientists+engineers;300 PB 数据 / 10,000+ sources [二手:twosigma.com]。**Factor Lens 18 个 factor**(最新 2020 加)— Two Sigma 明确区分:Style factor 便宜高容量,"alpha" 是高成本低容量信号 [一手:venn.twosigma.com/resources/factor-lens-update]。每日跑 **100,000+ simulations**。

**LLM 立场**:公开最积极 — head of AI Mike Shuster "5+ 年用 GenAI"(Columbia 2024-11)[二手:ai-street.co]。2025-02 发表 "abstracting LLM interactions" 框架 paper。**2026 起 "AI-first internal mandate"** [二手:hedgeco.net/04/2026]。**Distinguish**:Two Sigma 把 LLM 用于 *alternative data 解析 + research workflow*,不是 *expression generation*。

**Decay 应对**:"researcher 花精力理解为何 signal 会 decay 是否只是 repackaged momentum" [二手:datainterview.com]。

**对 AIAC 启示**:
- 学 **factor-lens 显式分离** → **R13 `factor_decomposition_neutralizer`**:simulate 后强制对 5-6 style factor(size/value/momentum/quality/low-vol)做 OLS,剩余 residual sharpe ≥ τ 才算 PASS。AIAC 当前 evaluation 只看 sharpe/fitness/turnover/self-corr,**没有显式 style-factor neutralization**
- 学 **LLM-for-data-pipeline 而非 LLM-for-expression** — 把 LLM 重心从 expression-author 转移到 feature-extraction-author(P2-A macro-narrative extract 已是雏形)
- 学 **100k simulations/day 规模** → 本地 cache(R9 已有)+ offline backtest(Q10 pyqlib 部分覆盖)放大 sim 量级

### 5.5 Millennium Management

**Alpha 类型**:~320 pods 完全 multi-strategy [二手:getsmartresume]。pod 内 alpha 类型自由。

**生成机制**:100% pod-level autonomy。Millennium 不做 central alpha discovery,只做 central risk + capital。

**风险管理**:**最严格** — 5% 损失 pod allocation 减半,7.5% 损失 PM 解雇("5% 规则")[二手:getsmartresume / techinterview.org]。Englander 强调 market-neutral 以减少方向暴露。

**对 AIAC 启示**:
- 学 **5%/7.5% hard stop-loss** → **R14 `task_stop_loss`**:基于近 N round PASS-rate EMA,低于阈值自动 pause(类比 BRAIN_AUTH_CIRCUIT,但 reward-driven 而非 error-driven)。AIAC 当前 task 可无限烧 round 直到 budget 耗尽
- 学 **pod 多元化 = task 多元化** → **flat-F4**:强制 cross-region 平衡(USA/CHN/JPN/EUR/HKG 至少各占 15%),对接已有 G8 hypothesis forest
- *不学* 完全 autonomy 无 cross-pod learning — AIAC G8 hypothesis forest 走得对,task 之间应该有 KB-mediated 学习

### 5.6 Jane Street

**Alpha 类型**:HFT 毫秒-秒级市做 + ETF 套利 + 期权做市。资本 $40B+ 纯自营 [二手:hedgeco/05/2026]。

**LLM 立场**:"Deep learning is the future" [一手:janestreet.com/join-jane-street/machine-learning],但强调 neural net 而非 LLM 路线。

**团队结构**:**500+ OCaml 程序员,30M+ 行 OCaml**。一切研究 / 系统 / 交易 / 会计都 OCaml,赞助 OCaml Labs Cambridge [二手:ocaml.org/success-stories]。

**风险**:SEBI 2025-07 105 页 interim order 公开了印度 Bank Nifty expiry-day "intraday index manipulation" 策略细节,Jane Street 抗辩这是标准套利。SEBI 没收 ₹4,843 crore [二手:blogs.law.ox.ac.uk / globaltrading.net]。

**Decay 应对**:"alpha is increasingly structural" — 靠基础设施 + spread market making 而非短期 signal [二手:hedgeco]。

**对 AIAC 启示**:
- 学 **结构性 vs 临时性 alpha 分类** — 把 alpha 标记 `structural`(operator-pattern 永久型)/ `temporal`(短期 anomaly),前者权重更高,对接 R8 KB lifecycle metadata
- 学 **自研 DSL 控制研究→生产链路** — Jane Street OCaml 一种语言贯穿;AIAC 的 alpha expression DSL 是类似定位,继续投资 DSL 是对的
- *不学* 毫秒延迟 — BRAIN 日级模拟,AIAC 永远不是 HFT

### 5.7 Point72 / Cubist

**Alpha 类型**:双轨 — Cohen 系 fundamental long/short + Cubist Systematic mid-freq quant + Turion AI 主题 fund(2025,$2.8B)[二手:thestreet.com]。Cubist 占 ~45% capital。

**生成机制**:Cubist 系统化全链(methodology / data / testing / backtest / monitoring)[一手:builtinnyc.com/job/cubist-quantitative-researcher]。**Quant Academy 5 年 rotational program** 培养 PhD [一手:point72.com/blog/cubist-systematic-launches-quant-academy-program]。

**LLM 立场**:Turion 是 *theme 投资*(投 Nvidia/TSMC/AMZN 等)**不是用 LLM 产 alpha**。Cubist Systematic 招聘 JD 显示在加 LLM/AI researcher。

**对 AIAC 启示**:
- 学 **Quant Academy rotational** → 强制 mining session 经历 hypothesis→generation→validation→eval→retrospective 全 5 阶段,不跳过(当前 retrospective 偶尔被绕过)
- *不学* theme AI fund 蹭热点 — Turion 是 *投资 AI 公司股票*,与 *用 AI 找 alpha* 不同维度

### 5.8 AQR Capital Management

**Alpha 类型**:classic systematic factor(value/momentum/quality/carry)中-慢频 + ML overlay。

**生成机制**:2023 launch **Turing Equities + Turing Macro ML 策略**;2025 外部资金;**ML 现在驱动 ~1/5 主策略信号** [二手:bloomberg/2025-04-23 / fa-mag]。

**LLM 立场**:Asness 7 年前 skeptical,2025-04 转 "AI believer";但坚持 "ML 不是 silver bullet,需配人类专业 judgment" [二手:aitechtrend]。AQR 的 LLM 路线偏 academic — Bryan Kelly 在 Yale/AQR 双聘,SSRN 高产。

**公开 paper**(最丰富 — 直接可 ingest):
- Giglio/Kelly/Xiu "Factor Models, ML, and Asset Pricing"(Annual Review 2022)[一手:papers.ssrn.com/4267961]
- Kelly/Xiu "Financial Machine Learning"(2023)[一手:papers.ssrn.com/4501707]
- Kelly et al. "Large (and Deep) Factor Models" [一手:papers.ssrn.com/4679269]
- Chen/Kelly/Xiu **"Expected Returns and Large Language Models"** [一手:papers.ssrn.com/4416687]
- "Autoencoder Asset Pricing Models" [一手:aqr.com/Insights/Research/Working-Paper/Autoencoder-Asset-Pricing-Models]

**对 AIAC 启示**:
- 学 **直接 KB seed AQR Kelly/Xiu paper** — Kelly "Expected Returns and LLMs" 是 AIAC 的学术对照,直接对齐 R7 LLM judge 的 expected-return-extraction prompt;Autoencoder paper 启发 R8 hierarchical RAG embedding 升级路径
- 学 **academic-industry hybrid** — AQR-Yale 直通保证 IP 涌入。AIAC 已对接 openassetpricing.com,继续 + 定期扫 arxiv q-fin
- 学 **坦诚 1/5 信号已 ML drive** — 不要 all-in,留 4/5 给经典 factor。AIAC 大多数 G* flag default OFF 路线契合

### 5.9 Bridgewater Pure Alpha + AIA Labs(bonus)

**Alpha 类型**:systematic macro(月-季)+ AIA Labs AI 子策略(2024 launch)。

**AIA Labs**:AIA Macro Fund **2025 +11.9%**($5B AUM 2026)[二手:bridgewater.com/aia-labs / hedgeco/03/2026]。用 OpenAI/Anthropic/Perplexity 多 LLM。

**对 AIAC 启示**:Bridgewater 把 LLM 用作 *rules-engine 的 assistant*(几百条 codified decision rules 不变,LLM 帮 refine + 提案新 rule),不是替代 rules-engine。AIAC R12 dual-mode 设计可参考这种共生。

---

## 6. 工业派共性 5 条 → reference architecture

| # | 共性 | 8 家中的实现 |
|---|---|---|
| 1 | **LLM = research assistant,不是 expression-author**(2026 共识) | Citadel chatbot / Two Sigma alt-data / Bridgewater AIA — 全把 LLM 用于 alt-data 处理 + 假设生成 + 文档检索。Griffin 2025-10 "GenAI fails to produce alpha" 是最赤裸版本 |
| 2 | **factor-lens / risk-factor decomposition 是默认基础设施** | Two Sigma 18 factor / AQR factor zoo / Bridgewater rules-based regime / Citadel 中央 risk overlay。**与 RenTec 纯黑盒反例形成对比** |
| 3 | **严格 stop-loss / capacity-cap** | Millennium 5%/7.5% / RenTec Medallion $10B cap / Citadel pod risk overlay / Bridgewater rules |
| 4 | **多策略多频段并行**而非单押一频 | Citadel/Millennium/Point72/D.E. Shaw 全是 multi-strategy;甚至 RenTec 分 Medallion(short-mid)+ RIEF/RIDA(long) |
| 5 | **核心 IP 是 research platform 而非单 alpha** | Two Sigma Beakerx/Flint/Venn / Jane Street OCaml stack / RenTec 内部 trading sys / AQR-Yale paper 流。**alpha 会 decay,平台不会** |

---

## 7. 学界增量 — 2026-05-17 之后 + 之前遗漏

### 7.1 新论文(2026-05-17 之后)

#### 7.1.1 AlphaCrafter — full-stack Miner/Screener/Trader(arxiv:2605.05580, 2026-05-07)
**核心**:把 alpha 挖矿、regime 识别、portfolio execution 合到一个 daily-rotating multi-agent pipeline,Miner/Screener/Trader 三 agent 共享 persistent memory。
**vs AIAC**:AIAC 已有 flat-only + R6 DAG + R9 cache,但 **整个栈停在 BRAIN 提交,没有 portfolio construction / regime-conditioned ensemble / execution agent**。Screener("根据当前 regime 动态组装 factor ensemble")在 AIAC 是完整缺失 → **G9 候选**。

#### 7.1.2 CogAlpha — 7 层 cognitive hierarchy(arxiv:2511.18850, 2025-11)
**核心**:Market Structure → Extreme Risk → Price-Volume → Price-Vol → Multi-Scale Complexity → Stability-Gating → Geometric/Fusion 七层 agent + Thinking Evolution(5 mode 文本变异:Light/Moderate/Creative/Divergent/Concrete)。CSI300 IC=0.0591 / IR=1.8999。
**vs AIAC**:AIAC G5 trajectory crossover LIVE,但 mutation 是 *单层 LLM 直出*;CogAlpha 把 evolution 拉到 **七层不同抽象层级的 agent prompt** → AIAC R8 L1 pillar 可演化为 cognitive layer 调度;5-mode paraphrase 可直接 port 到 G5 crossover prompt 工厂。

#### 7.1.3 AlphaLogics — logic-as-asset reverse mining(arxiv:2603.20247, 2026-03-10)
**核心**:5 agent(FormulaStructure / FinancialSemanticsMapping / MarketLogicAbstraction / FactorExpressionGenerator / MarketLogicRefinementDirection)从 Alpha101/191/158/360 库 **逆向抽取 market logic** 作为可优化对象。
**vs AIAC**:AIAC KB 3357 entries 是单向 ingest(外部→KB),**没有 "reverse mining 自家 PASS alpha → 抽 logic → 反哺 prompt" 闭环** → **G10 候选**:每周从 PASS alpha 表跑 LogicAbstractionAgent。

#### 7.1.4 FactorMiner — Ralph Loop + Skills + ℳ Memory(arxiv:2602.14670, 2026-02-16)
**核心**:Retrieve→Generate→Evaluate→Distill + Modular Skill(60+ operator deterministic offload)+ Experience Memory ℳ(success / **forbidden region** / strategic insight)+ correlation-aware factor replacement(保 global library 视角)。
**vs AIAC**:AIAC R10 family-cap 是 top-k=2 软限,**FactorMiner 是 family-level dynamic hard ban N 轮** → **R10-v2 演化方向**:对高互相关 family 做 dynamic ban。Skills(60+ operator deterministic)AIAC 通过 BRAIN operators 表 66 行已有。

#### 7.1.5 AlphaCFG — grammar-aware MCTS(arxiv:2601.22119, 2026-01-29)
**核心**:context-free grammar 定义搜索空间(syntactically valid + size-controlled),grammar-aware MCTS + syntax-sensitive policy net。
**vs AIAC**:AIAC `alpha_semantic_validator` 是 **事后** 过滤,AlphaCFG 是 **事前** 约束。G3 AST originality 升 hard gate 时应合并 CFG-aware generation,节约 LLM 重 retry cost。

#### 7.1.6 TLRS — trajectory-level reward shaping(arxiv:2507.20263)
**核心**:PPO + RPN token-level,用 expert formula(Alpha101 等)**exact subsequence match ratio** 做密集 reward,复杂度 O(d)→O(1)。
**vs AIAC**:AIAC 完全无 RL — G1 是 contextual bandit,不是 token-level PPO。BRAIN sim 限额下 PPO 不划算,但 **"expert formula 子串 match 当 dense reward"** 思想可 port 成 *离线 R8 ranking 信号* — Alpha101/191 subseq overlap 高的 entry 加权。

### 7.2 遗漏(2026-05-17 前应纳但 v1 missed)

#### 7.2.1 TradingAgents v0.2.4(arxiv:2412.20138)
LangGraph-based multi-agent 模拟 trading firm(Bull/Bear / Risk / Trader / Fundamental/Sentiment/Technical analyst)。decision-centric 非 factor-centric — **不直接竞品**。Pydantic + checkpoint resume + persistent decision-memory 模式 AIAC 在 R1b CoSTEER 已有等价。

#### 7.2.2 FactorMoE(Springer 2026)
MoE + Multi-Head Attention 做 alpha 因子 *动态组合*,gating network 条件于 market regime + recent factor performance。AIAC 上层组合完全 absent → G9 portfolio extension 可参考 gating-on-regime。

#### 7.2.3 BlindTrade(arxiv:2603.17692, ICLR 2026 FinAI Workshop)
4 LLM agent + **GNN graph from reasoning embeddings** + PPO-DSR + anonymize ticker 防 memorization bias。AIAC 不暴露 ticker(走 BRAIN expression DSL),但 GNN-on-embedding 模式可替代 G8 hypothesis forest 当前的 pillar JSONB @> flat retrieval。

### 7.3 既有系统版本更新(2026-05-17 → 2026-05-19)

| 系统 | 旧版 | 现状 | 主要变化 | AIAC 影响 |
|---|---|---|---|---|
| RD-Agent | v0.8.0(2025-11) | 仍 v0.8.0 未发 v1.0 | LiteLLM 默认 backend + ICML 2026 FT-Agent | 无紧迫,继续按 NeurIPS 2025 paper 对齐(G8 已 ship) |
| qlib | v0.9.7 | unchanged | 整合 RD-Agent 入口 | 无新机制,Q10 路径稳定 |
| AlphaAgent | v2 KDD 2025 | unchanged | CSI500 IR=1.5 / SP500 IR=1.05 实证 | 已纳 G3 |
| Hubble | v2(2026-04) | unchanged,**无 v3** | dual-channel RAG + family-cap | 已纳 P2-D / R10 |
| QuantaAlpha | v1(2026-02) | 持续被引 | 无新版 | 已纳 G5 |
| TradingAgents | v0.2.0 | v0.2.4(2026-04-25) | typed + checkpoint + memory + 10 LLM provider | 非竞品 |
| Chain-of-Alpha | v2 撤稿 | 未见 corrected v3 | — | N/A |

---

## 8. 横切发现:AIAC 当前 5 个明确 gap

源自工业 8 家共性 §6 + 学界 7.1/7.2 增量。

### 8.1 评估层 gap — factor-lens 缺失(Two Sigma + AQR + Bridgewater 共识)
**现状**:`evaluation.py` 只看 sharpe/fitness/turnover/self-corr。
**Gap**:**没有显式 style-factor neutralization** — 一个 momentum 信号 sharpe 1.8 可能 80% 来自 size/value 暴露,真 alpha residual 接近 0。
**建议**:**R13 `factor_decomposition_neutralizer`** — 对 5-6 style factor(size/value/momentum/quality/low-vol)做 OLS,剩余 residual sharpe ≥ τ 才算 PASS。

### 8.2 风控层 gap — capacity 维度缺失(RenTec + Bridgewater 共识)
**现状**:composite score 不考虑单 alpha $ capacity。
**Gap**:高 sharpe 低 capacity 的 alpha 与高 sharpe 高 capacity 同等对待。Medallion 因 $10B cap 才能维持 39% net,大资金需要 capacity-weighted 排序。
**建议**:**R11 `alpha_capacity_estimator`** — turnover × universe ADV × universe size 算单 alpha capacity,进 score 公式 + alpha rank。

### 8.3 风控层 gap — task-level stop-loss 缺失(Millennium 共识)
**现状**:task 可无限烧 round 直到 budget 耗尽,无 reward-driven 自动暂停。
**Gap**:Millennium 5%/7.5% hard stop 是工业派最严格风控,AIAC 完全无。
**建议**:**R14 `task_stop_loss`** — 基于近 N round PASS-rate EMA,低于阈值自动 pause task(类比 BRAIN_AUTH_CIRCUIT,reward-driven 而非 error-driven)。

### 8.4 平台维度 gap — multi-strategy 并行不足(Citadel/Millennium/Point72 共识)
**现状**:AIAC 当前 task 并行度低且大多压在 USA TOP3000。
**Gap**:Millennium 320 pods 同时跑;Citadel 5 业务线并行。AIAC region/universe 严重偏 USA。
**建议**:**flat-F4 cross-region 平衡** — POST `/mining-session/start` 时强制 USA/CHN/JPN/EUR/HKG 至少各占 15%,对接 G8 hypothesis forest 做 cross-region knowledge sharing。

### 8.5 设计哲学 gap — LLM-as-author vs assistant(8 家共识)
**现状**:`node_code_gen` 让 LLM 直出 expression。
**Gap**:工业 8 家共识是 LLM 做 *research assistant*(提 hypothesis / 解析 alt-data / 文档检索),expression 由 GA/template/CFG 生成。AIAC 与共识反向。
**建议**:**R12 dual-mode** — `LLM_MODE=assistant`(默认,LLM 提 hypothesis + 解释,expression 由 GA + template 生成)/ `LLM_MODE=author`(当前行为,作为次选)。

### 8.6 学界 5 gap(承自 §7.1)

| # | Gap | 学界源 | AIAC 候选 phase |
|---|---|---|---|
| 6 | Full-stack 延伸到 portfolio + execution | AlphaCrafter(5-07)/ FactorMoE | **G9** |
| 7 | Logic-as-asset 反向蒸馏 | AlphaLogics(3-10) | **G10** |
| 8 | Grammar-aware generation | AlphaCFG(1-29) | G3-v2(hard-gate 升级时合并) |
| 9 | Hard forbidden region in memory | FactorMiner(2-16) | **R10-v2** |
| 10 | 7 层 cognitive prompt 分层 | CogAlpha(11) | R8 L1 演化方向 |

---

## 9. 对 AIAC 设计建议(优先级排序)

### 9.1 P0(2-4 周,直接闭风险口)

- **R14 `task_stop_loss`**(Millennium 启示)— 1.5 人日,reward EMA 触发 pause,光是把 Millennium 5%/7.5% 思想 port 过来就值
- **R12 dual-mode `LLM_MODE=assistant`**(8 家共识)— 3 人日,把工业派最强的 stance 对齐;默认翻 assistant 避免 v1 设计与工业脱节

### 9.2 P1(4-8 周,补评估层)

- **R13 `factor_decomposition_neutralizer`**(Two Sigma + AQR 启示)— 4 人日,5-6 style factor OLS;先 shadow 模式 stamp residual_sharpe,7d obs 后促 hard gate
- **R11 `alpha_capacity_estimator`**(RenTec 启示)— 2 人日,turnover × ADV × universe size 进 composite_score 权重
- **flat-F4 cross-region 平衡**(Millennium / Citadel 启示)— 2 人日,POST 强制 region quota

### 9.3 P2(8-12 周,补学界 SOTA 5 gap)

- **R10-v2 hard forbidden region**(FactorMiner 启示)— 2 人日,family-level dynamic ban N 轮
- **G3-v2 grammar-aware generation**(AlphaCFG 启示)— 3 人日,与 G3 hard-gate 升级合并
- **G9 portfolio extension**(AlphaCrafter / FactorMoE 启示)— 6-8 人日,Screener Agent 做 regime-conditioned ensemble
- **G10 logic-as-asset 反向蒸馏**(AlphaLogics 启示)— 4 人日,每周从 PASS alpha 表抽 logic 反哺 hypothesis prompt
- **R8-v3 cognitive layer 调度**(CogAlpha 启示)— 4 人日,L1 pillar 演化为 7 层 cognitive layer system prompt

### 9.4 P3(quick wins,zero risk)

- **AQR Kelly/Xiu paper KB seed**:5 篇 SSRN paper(已列 §5.8)直接 batch ingest 进 R8 KB(0.5 人日)
- **RenTec signal-leak jitter**:submit 时机做 randomize(0.5 人日)
- **Point72 Quant Academy rotational**:强制 mining session 经历全 5 阶段不跳过(0.5 人日,改 mining_agent round 流程)

### 9.5 *不学* 的明确事项

- **RenTec 纯黑盒** — AIAC explainable pipeline 不应回退
- **D.E. Shaw 单点 ML czar** — Domingos 1 年走人案例验证不可靠
- **Point72 Turion theme fund** — 投 AI 公司 ≠ 用 AI 找 alpha,不混淆
- **Jane Street 毫秒延迟** — BRAIN 是日级模拟,AIAC 永远不是 HFT

---

## 10. 信息不可得

- Renaissance Medallion 内部 signal 配方(8 家最暗箱者)
- Citadel GQS 具体 alpha 类型分布(5 业务线 internals 全 NDA)
- D.E. Shaw Domingos era ML group output(2018-2019)结果
- Millennium pod 内 alpha 实战分布
- Jane Street SEBI 案件后内部策略变化
- AlphaCrafter / CogAlpha / AlphaLogics / FactorMiner / AlphaCFG / TLRS 在公开 benchmark(CSI500/SP500)的 third-party 复现
- RD-Agent v1.0 release date(v0.8.0 仍是 latest,2025-11-03 后无新 minor)
- Hubble v3 是否存在(v2 后无新版)

---

## 11. Sources

### 11.1 学术论文(新增 + 承自 v1)

- AlphaCrafter(2605.05580)— https://arxiv.org/html/2605.05580
- CogAlpha(2511.18850)— https://arxiv.org/abs/2511.18850
- AlphaLogics(2603.20247)— https://arxiv.org/html/2603.20247v1
- FactorMiner(2602.14670)— https://arxiv.org/html/2602.14670v1
- AlphaCFG(2601.22119)— https://arxiv.org/abs/2601.22119
- TLRS(2507.20263)— https://arxiv.org/html/2507.20263
- TradingAgents(2412.20138)— https://arxiv.org/abs/2412.20138
- FactorMoE — https://link.springer.com/article/10.1007/s40747-026-02307-2
- BlindTrade(2603.17692)— https://arxiv.org/abs/2603.17692
- Hubble v2(2604.09601)— https://arxiv.org/abs/2604.09601
- QuantaAlpha(2602.07085)— https://arxiv.org/abs/2602.07085
- AlphaAgent(2502.16789)— https://arxiv.org/abs/2502.16789
- RD-Agent-Quant(2505.15155)— https://arxiv.org/abs/2505.15155
- Navigate Alpha Jungle(2505.11122)— https://arxiv.org/html/2505.11122v2
- Alpha-GPT v2(EMNLP 2025 Demos)— https://aclanthology.org/2025.emnlp-demos.14/
- Giglio/Kelly/Xiu(2022)— https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4267961
- Kelly/Xiu Financial ML(2023)— https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4501707
- Kelly Large and Deep Factor Models — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4679269
- Chen/Kelly/Xiu **Expected Returns and LLMs** — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416687
- AQR Autoencoder Asset Pricing — https://www.aqr.com/Insights/Research/Working-Paper/Autoencoder-Asset-Pricing-Models

### 11.2 工业一手(careers / engineering blog / official)

- Citadel GQS — https://www.citadel.com/what-we-do/global-quantitative-strategies/
- Two Sigma IM — https://www.twosigma.com/businesses/investment-management/
- Two Sigma Factor Lens — https://www.venn.twosigma.com/resources/factor-lens-update
- Jane Street ML — https://www.janestreet.com/join-jane-street/machine-learning/
- Jane Street Tech — https://www.janestreet.com/technology/
- Point72 Cubist — https://point72.com/cubist/
- Point72 Quant Academy — https://point72.com/blog/cubist-systematic-launches-quant-academy-program/
- Cubist QR JD — https://www.builtinnyc.com/job/cubist-quantitative-researcher/3424232
- AQR ML page — https://www.aqr.com/Learning-Center/Machine-Learning
- D.E. Shaw careers — https://www.deshaw.com/careers/quantitative-analyst-intern-new-york-summer-2026-5519
- Citadel Securities Peng Zhao — https://www.citadelsecurities.com/who-we-are/leadership/peng-zhao/
- Bridgewater AIA Labs — https://www.bridgewater.com/aia-labs

### 11.3 工业二手(Bloomberg / FT / hedgeco / Quartr 等)

- Ken Griffin "GenAI fails to produce alpha"(Bloomberg, 2025-10-15)— https://www.bloomberg.com/news/articles/2025-10-15/ken-griffin-says-genai-fails-to-help-hedge-funds-produce-alpha
- Griffin 改口(Fortune, 2026-05-18)— https://fortune.com/2026/05/18/billionaire-ken-griffin-ai-garbage-depressed-dramatic-impact-society/
- Citadel AI Assistant(ai-street.co)— https://www.ai-street.co/p/citadel-reveals-ai-assistant
- Two Sigma AI-first mandate — https://hedgeco.net/news/04/2026/two-sigmas-ai-first-internal-mandate-the-race-for-operational-alpha-in-the-age-of-frontier-models.html
- Two Sigma research paper survey — https://medium.com/@tzjy/what-two-sigma-research-is-working-on-these-days-12cc4b8a5b0b
- RenTec Quartr deep-dive — https://quartr.com/insights/edge/renaissance-technologies-and-the-medallion-fund
- RenTec quantvps strategy summary — https://www.quantvps.com/blog/jim-simons-trading-strategy
- Millennium pods structure — https://www.getsmartresume.com/article/millennium-early-career-programs
- Millennium 5% rule — https://www.techinterview.org/companies/millennium-management-interview-guide/
- Jane Street $40B dominance — https://hedgeco.net/news/05/2026/jane-streets-40-billion-dominance-the-quiet-trading-giant-reshaping-wall-street.html
- Jane Street SEBI(Oxford Business Law)— https://blogs.law.ox.ac.uk/oblb/blog-post/2025/07/jane-street-and-expiry-day-trap-unpacking-sebis-crackdown-algorithmic
- Point72 Turion AI fund — https://hedgeweek.com/point72-am-to-launch-1bn-ai-focused-hedge-fund/
- Cubist Systematic AI standout — https://hedgeco.net/news/05/2026/point72s-ai-standout-steve-cohens-turion-fund-is-a-defining-trade-of-the-ai-infrastructure-boom.html
- AQR bets on ML(Bloomberg)— https://www.bloomberg.com/news/articles/2025-04-23/aqr-bets-on-machine-learning-as-cliff-asness-becomes-ai-believer
- AQR Asness AI believer(aitechtrend)— https://aitechtrend.com/aqr-bets-on-machine-learning-as-cliff-asness-becomes-ai-believer/
- Bridgewater AIA $5B AUM — https://hedgeco.net/news/03/2026/bridgewater-dalios-principles-to-algorithmic-intelligence-the-road-to-5billion.html
- D.E. Shaw Domingos hire — https://www.prnewswire.com/news-releases/d-e-shaw-group-forms-new-machine-learning-research-group-300698027.html
- D.E. Shaw Quant King — https://rupakghose.substack.com/p/de-shaw-the-quant-king
- Citadel outperforms in July 2025 unwind — https://www.hedgeweek.com/citadel-outperforms-big-multi-strat-peers-in-choppy-july/
- Quant crowding & unwind(Resonanz)— https://resonanzcapital.com/insights/crowding-deleveraging-a-manual-for-the-next-quant-unwind
- Hedge funds GenAI usage(Resonanz)— https://resonanzcapital.com/insights/how-hedge-funds-are-really-using-generative-ai-and-why-it-matters-for-manager-selection

### 11.4 AIAC 内部对照源(2026-05-19)

- `backend/config.py` 28 ENABLE_* flag + 33 production flag ON
- `backend/services/feature_flag_service.py` `SUPPORTED_FLAGS` whitelist
- `docs/flag_lifecycle.md` — Tier 1/2/3 promotion 规则 + 2026-05-19 11 flag 退役 batch
- `docs/master_implementation_plan_2026-05-17.md` — Phase 0-3 progress
- `docs/phase15_task_schema_refactor_plan.md` — cascade 退役 + flat 完整设计
- `backend/agents/core/ARCHITECTURE.md` — RD-Agent 兼容代码 3223 行(R1b.4 activated)
- `backend/agents/graph/nodes/evaluation.py` `_eval_thresholds()` — flat EVAL band 单一来源
- `MEMORY.md` 索引 — 60+ ship/project/feedback memory entries

---

*v2 独立可引;v1 仍保留作为历史 snapshot。本文是 2026-05-19 单 session capstone day 的总结性 reference,后续 Phase 4(G9/G10/R11-R14/flat-F4)路线决策应 cross-reference 本文 §8 + §9。下一次更新触发条件:Phase 4 任一项 ship 完成,或 2026-06 出现新 SOTA 论文。*
