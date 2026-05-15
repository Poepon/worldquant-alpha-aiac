# AlphaGBM/skills 调研结论 — 对 AIAC Alpha 挖掘的可迁移知识

> 调研日期:2026-05-15
> 调研对象:GitHub `AlphaGBM/skills`(26 个 Claude Code Skills,薄封装 AlphaGBM SaaS API)
> 目的:提炼一切可迁移到 AIAC "LLM 生成 alpha → 回测 → 自进化" 流程的设计模式与方法论

---

## 0. 仓库定性

`AlphaGBM/skills` 是一套**期权/股票分析的 Claude Code Skills 集合**,每个 skill 是单文件 `SKILL.md`(frontmatter + 方法论表 + API 端点 + 示例 + Related Skills),内部计算逻辑全在服务端不可见。它本身不是 alpha 挖掘工具,但**契约层设计模式**——评分、归一化、分级路由、信号验证、抗过拟合、多保真、知识库治理——可逐条映射到 AIAC。

仓库结构:`skills/`(26 个 skill 目录,每个仅含一个 `SKILL.md`)、`cli/`(pip 安装的 Python CLI,与 skill 共用同一 REST API)、`mock-data/`(离线 JSON fixture,既是 I/O 契约又是 per-tier 测试场景)、`assets/`。

---

## 1. 26 个 Skill 知识点速查

| Skill | 对 alpha 挖掘最有价值的知识点 |
|---|---|
| **bps-backtest** | signal-vs-control 双跑(去掉信号项再跑对照组,用 Δ 指标证明信号在做功);参数=default+range 即有界搜索空间;exit_reasons 枚举归因;硬风控参数与策略参数分离 |
| **fear-score** | 6 因子非均匀加权(25/20/15/15/15/10);`fallback` 标记 + `confidence`=真实数据占比,端点永不崩;"极端读数非线性放大"映射;阈值≥60 配 A/B 实证(10.8% vs 3.5%) |
| **iv-rank** | min-max 归一化 `(x−low)/(high−low)×100`;rank vs percentile 双指标;5 档 zone 硬映射表,每档绑定动作;VRP=预期−实际 |
| **vol-surface** | **拟合基线 + Nσ残差异常检测**(`deviation_sigma`)——最可直接落地的挖矿筛选算法;VRP 5 档命名分级;便宜端点 `snapshot` 单独成一等公民(免配额预检);双轴网格输出 |
| **take-profit** | **15 策略 × 120 建仓点取中位数**——抗过拟合;**过山车率**(达 +50% 后回撤>50% 的频率)作稳健性因子;`reverse_alpha`=主动跑赢被动;`no_hold` 黑名单硬否决;negative knowledge;强制带 sample_size+period |
| **pnl-simulator** | What-if 压力测试(Price±10%/IV±50%)→ 参数扰动鲁棒性检验;概率分布+期望值而非单点;P&L over time = alpha 衰减分析;结构化 legs[] schema 便于变异 |
| **hedge-advisor** | 优先级决策树,先命中先返回;复合布尔 AND 分类(回撤≥15% AND PnL≤5%);场景化差异预算(不同场景给不同 DTE/预算);中英双语 reason |
| **investment-thesis** | 散文(买入理由)+ 结构化触发器(退出条件)二分;自动监控 + 状态翻转 active→triggered + `trigger_detail`;`thesis_score`+`ai_feedback` = LLM 自评+自批 |
| **health-check** | 知识库定期体检:stale/drift/orphan + 0-100 分 + 带 reason 的推荐动作;漂移检测=入库时指标 vs 当前指标;5 档健康带 |
| **market-sentiment** | 多因子→百分位归一化→复合分→regime 分类+置信度;lookback=252;breadth(广度)≈ alpha 组合集中度检查 |
| **options-score** | 按策略类型各一套权重表(各因子权重不同,和=100%);4 档分数解释;同步/异步双通道 + 配额分层 |
| **options-strategy** | market view→模板库→打分排序→输出 rationale 全管线;Trend Alignment 打分(匹配=100/不匹配=30)= 假设-实现一致性;4 档 risk-return style;Max Loss 风险边界预标注 |
| **greeks** | 一阶/二阶敏感度分离 → 有指导的参数变异;场景热力图=参数网格扫描;按资本单位归一化;集中度识别(哪条腿主导风险) |
| **polymarket** | 两个独立信号源的价差=alpha 来源;`min_spread=0.10` 阈值门控;Historical Accuracy 反哺 bandit;confidence+magnitude 双维排序 |
| **vol-smile** | 差分双点构造因子 `IV(25Δput)−IV(25Δcall)` = 可复用 alpha 表达式母题;skew percentile vs 252 天;曲线 5 形态分类 |
| **earnings-crush** | 单指标→3 档策略硬映射(IV Rank >70/30-70/<30);implied vs actual 历史对照;`straddle_win_rate` 策略族元指标;ready-to-execute 输出 |
| **company-profile** | 百分位 vs 历史区间(PE 8 年第 85 百分位);规则化红旗 `{rule_id, severity, message}`;staleness>7d;幂等 Create + 软删除 archived;event_radar=数据驱动触发器 |
| **compare** | 逐维度选 winner + 加权 composite 两层评估;Five Pillars(Momentum/Value/Quality/Volatility/Sentiment)因子分类骨架;key differentiators 解释性输出 |
| **alert** | 阈值穿越 vs 极性翻转两种触发语义;one-time vs recurring 生命周期;触发即带 context+suggested action |
| **macro-view** | field→经济传导机制映射(VIX→风险情绪,US10Y→贴现率)→ RAG 引导生成有经济叙事的因子;staleness 数据预检 |
| **watchlist** | 阈值穿越告警(IV rank>80/<20);优先级排序通知队列(优于散落日志);时间临近事件标记(财报 7 天内) |
| **stock-analysis** | G=B+M 五法加权目标价;风险评分 0-10 带 flags;EV 期望值跨 1 周/1 月/3 月概率加权 |
| **vix-status** | regime 分类器门控策略激进度(VIX 5 档→仓位);百分位 + 各档时间占比分布;免配额缓存端点 |
| **duan-analysis** | 投资哲学编码进 skill 约束生成空间;面板可 null 优雅降级;默认值有业务语义;VIX≥35 regime switch |
| **theme-research** | theme=alpha-idea basket → 按假设族分组;关键词卫生提示;orphan 检测 |

---

## 2. 八大可迁移知识模块

### ① 评分体系:多因子加权 → 归一化 → 分级 → 置信度
不输出裸数字。`fear-score` 用非均匀权重(25/20/15/15/15/10),`options-score` 按策略类型各备一套权重表。所有连续量先用 `(x−low)/(high−low)` 或 percentile 相对 252 天历史归一化,再切成 4-5 档,每档绑定明确动作。带 `confidence`(= 真实数据占比)和极端读数的非线性放大。

### ② 信号有效性验证:对照、价差、残差
- **signal-vs-control 双跑**(bps-backtest):去掉信号项的对照组,`Δ(Sharpe_signal − Sharpe_control)` 才是真判据。
- **拟合基线 + Nσ残差**(vol-surface):对 (假设×数据集) 网格拟合期望表现,超出期望 cell >2σ 的才是真发现而非噪声。
- **价差即 alpha**:polymarket(预测市场 vs 期权)、vol-smile(25Δ put vs call)、take-profit(主动 vs 被动)—— 两个独立信号源的 gap。

### ③ 抗过拟合:网格、中位数、压力测试
- 变体族群 × N 样本点取中位数(take-profit:15 策略×120 建仓点)。
- What-if 参数扰动(pnl-simulator:Price±10%/IV±50%)—— 只有扰动下仍稳健的进知识库。
- 过山车率 —— 累计收益达 +X% 后回撤>50% 的频率,作为独立稳健性因子。
- 输出概率分布 + 期望值,强制带 `sample_size / period`。

### ④ 工作流路由:优先级决策树
`hedge-advisor` 的 4 场景按优先级求值、先命中先返回,每个场景是复合布尔 AND(印证 AIAC "提交三道门" 是对的),每档绑定差异化优化预算(接近门槛的多给模拟次数,全崩的不给)。

### ⑤ 多保真 / 配额纪律
便宜端点(`snapshot`/`vix-status`)被设计成独立一等公民而非一个 flag —— 廉价预检永远先跑。配合缓存(5min~30 天)、tier 配额、`--json` 双输出。

### ⑥ 知识库治理:体检、漂移、状态机
`health-check` 周期体检标记 stale/drift/orphan;`investment-thesis` 用"散文理由 + 结构化退出触发器"二分,自动监控 → 状态翻转 `active→triggered`;存 negative knowledge(take-profit 明确记录"移动止损持续跑输")。

### ⑦ 生成引导:机制映射、分类骨架、风格编码
`macro-view` 的 field→经济传导机制映射可做 RAG 上下文,引导 LLM 生成有经济叙事的因子;`compare` 的 Five Pillars 是保证 alpha 池均衡覆盖的分类骨架;`duan-analysis` 把投资哲学写死进 prompt 约束生成空间。

### ⑧ 工程化:SKILL.md 格式
单文件 = frontmatter(含 NL triggers)+ 方法论表 + API + 示例 + Related Skills。config 优先级 env > file > defaults,密钥脱敏。mock-data 既是 I/O 契约又是 per-tier 离线测试场景。

---

## 3. AIAC 落地优先级路线图

| 优先级 | 改动 | 来源 | 落地文件 |
|---|---|---|---|
| 🔴 P0 | 拟合基线 + Nσ残差 作为 alpha 筛选算法 | vol-surface | `multi_fidelity_eval.py`、新增 screener |
| 🔴 P0 | signal-vs-control 双跑归因,接 AttributionType | bps-backtest | `agents/core/` `Experiment2Feedback` |
| 🔴 P0 | 多保真严格化:语义校验→低保真→simulate,堵掉"先 simulate 后报错" | vol-surface/snapshot | `agents/graph/nodes/`、`multi_fidelity_eval.py` |
| 🔴 P0 | 变体族群网格 × 取中位数 抗过拟合 | take-profit | `genetic_optimizer.py` |
| 🔴 P0 | `SCORE_PASS_THRESHOLD` → 多档动作路由(提交/GA优化/丢弃) | iv-rank/hedge-advisor | `selection_strategy.py`、`alpha_scoring.py` |
| 🟡 P1 | 评分改 百分位归一化 + 非均匀权重 + confidence 维度 | fear-score/market-sentiment | `alpha_scoring.py`、`diversity_tracker.py` |
| 🟡 P1 | fallback 降级:指标缺失回退中性值+降 confidence,evaluate 节点永不崩 | fear-score | `graph/nodes/evaluation`、`metrics_tracker.py` |
| 🟡 P1 | 结构化淘汰触发器 + 定时 alpha 库体检(漂移检测) | thesis/health-check | `agents/core/Hypothesis`、`tasks/sync_tasks.py` |
| 🟡 P1 | What-if 参数扰动鲁棒性检验 | pnl-simulator | `multi_fidelity_eval.py` |
| 🟡 P1 | 语义校验输出 `{rule_id, severity, message}` 结构化红旗 + 风险边界预标注 | company-profile/options-strategy | `alpha_semantic_validator.py` |
| 🟢 P2 | field→经济机制映射 RAG 引导生成 | macro-view | `field_screener.py`、prompts |
| 🟢 P2 | Five Pillars 因子分类保证 alpha 池均衡覆盖 | compare | `diversity_tracker.py` |
| 🟢 P2 | regime-aware 阈值门控 + 风格 preset 编码 | vix-status/duan | `evolution_strategy.py`、`config.py` |
| 🟢 P2 | negative knowledge 沉淀 + 标准化复盘 schema | take-profit/health-check | `knowledge_extraction.py`、`scripts/v26_retrospective.py` |

---

## 4. 建议起点

从 P0 的 **"拟合基线 + Nσ残差" 挖矿算法** 入手 —— 它是本次调研唯一能直接变成新挖矿能力(而非仅优化现有流程)的知识点:对 (假设×数据集) 网格拟合期望表现,残差 `deviation_sigma > 2` 的候选才是真发现。其余 P0 项(signal-vs-control 双跑、多保真严格化、网格取中位数、多档路由)均为对现有流程的增强。

---

## 5. 实施进度(2026-05-16 P0 + P1 + P2-A + P2-B + P2-D 完成,剩 P2-C)

### P0 已完成 ✅

| 项 | commit | 说明 |
|---|---|---|
| 拟合基线 + Nσ残差挖矿筛选 | `07f8944` | vol-surface 模式落地 — `baseline_screener.py` + `BaselineProvider` + `residual_sigma` |
| 多保真严格化(语义→低保真→simulate) | `753589a` | `static_alpha_checks.py` 前置 3 项检查(look-ahead/divide/overfit-window),simulate 前拦截 |
| signal-vs-control 双跑归因 | `d36656e` | `evaluation.py` 内联块 + AttributionType 接入 |
| 变体族群网格 × 取中位数 抗过拟合 | `e57259b` | genetic_optimizer 晋级网格 fidelity 多次跑取中位数 |
| 多档动作路由(submit / GA / 丢) | `78938c1` | 集中化 `alpha_routing.py` + tier-aware score 阈值 |

### P1 已完成 ✅

| 项 | commit | 说明 |
|---|---|---|
| 评分百分位归一化 + 非均匀权重 + confidence | `c8df434` | `alpha_scoring.compute_graded_score` + 5 档 A-E grade;`diversity_tracker` 同口径 |
| fallback 降级 + 节点永不崩 | `fb67ff6` / `81c87ad` | `_safe_metric` NaN/inf/bool/str 防御 + per-alpha try/except + post-loop tally |
| 结构化淘汰触发器(part 1:Alpha 库体检) | `6a9dd47` / `3d6aaba` | 每日 08:00 SH beat,`docs/alpha_health_check/*.json` 5 档健康带;后续把 drift severity 改 worst-of(sharpe+fitness) |
| 结构化淘汰触发器(part 2:Hypothesis 触发器) | `9044483` | 每日 08:30 SH beat,5 类 trigger + active→triggered 软标记 + LLM thesis_score/ai_feedback;`hypothesis_status_transitions` 审计表 |
| What-if 参数扰动鲁棒性检验 | `d6f3abb` | `multi_fidelity_eval.RobustnessGate`:window 邻近 N=4 变体 + worst-of;Redis counter 双烧防御 + per-alpha hard timeout |
| 结构化语义校验红旗 + 风险边界预标注 | `2cd6c46` | `Finding{rule_id, severity, message, category}` 替代 `List[str]`;4 类静态 risk 推断(divide-by-volatile / signed_power / short-decay / extreme-winsorize);SELF_CORRECT prompt 按 severity 分段渲染 |

### P0 + P1 测试覆盖统计

- 总新增测试 **300+ 个**(unit + integration)
- `test_suite.py --unit` 7/7 零漂移
- `baseline.json` 在所有 flag OFF 路径上**逐位保持**(每个 P1 项都加 disabled passthrough 回归测试)

### P2 已完成 ✅

| 项 | commit | 说明 |
|---|---|---|
| Five Pillars 因子分类保证 alpha 池均衡覆盖 | `4ec6e8f` | `Hypothesis.pillar` 列(LLM emit 或 `pillar_classifier.infer_pillar` 静态推断兜底)+ `diversity_tracker` 5 维(老 4 维 byte-for-byte 不变)+ `node_hypothesis` opt-in `ENABLE_PILLAR_AWARE_SELECTION=False` nudge(Redis 60s cache + LEFT JOIN 覆盖 legacy NULL hypothesis_id)+ 09:00 SH `pillar_balance_check` 每日 task。49 new tests |
| negative knowledge 沉淀 + 标准化复盘 schema | `6cae5f5` | 6 类失败信号 → `FailureSignature`(sha1[:16] cluster)→ UPSERT `knowledge_entries.entry_type=FAILURE_PITFALL` 修复 `prompts/hypothesis.py:208` dead reference + opt-in `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` + 09:30 SH 每日 task `docs/negative_knowledge/<date>.json` + `v26_retrospective.py --full` ADDITIVE Pydantic superset(legacy CLI 完全不动)。0 Alembic / 0 新表 / 0 新 index。25 new tests |
| field→经济机制映射 RAG 引导生成 | `5a72da0` | `MacroNarrative` 数据契约(field/dataset/category 三 scope)+ 11 条种子(6 field + 5 category)inline 入 KB `entry_type=MACRO_NARRATIVE` + LLM 离线批生成填长尾(opt-in)+ `PromptContext.macro_narratives` 段(opt-in `ENABLE_MACRO_NARRATIVE_GUIDANCE`)+ `RAGService.get_macro_narratives` parallel pipeline(query 签名不动)+ 10:00 SH 每日 beat(序 08:00→08:30→09:00→09:30→**10:00**)+ Redis 10min cache + S5 token budget guard。两 flag 都默认 OFF(M9)。0 Alembic。24 new tests |

### P2 待办(优先级 🟢 nice-to-have)

| 项 | 来源 skill | 落地文件 |
|---|---|---|
| regime-aware 阈值门控 + 风格 preset 编码 | vix-status / duan | `evolution_strategy.py` / `config.py` |
