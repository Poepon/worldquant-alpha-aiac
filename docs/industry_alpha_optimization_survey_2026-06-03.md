# 业界是怎么做 alpha 优化的 — 工业级"优化"工具箱调研

> **文档定位**:承接 `competitive_analysis_v2_2026-05-19.md`(8 工业派 + 学界 25 系统 + 5 gap)与 `competitive_analysis_v3_2026-05-26.md`(BRAIN self-corr 提交模型 + selection-vs-discovery + 广度轴)。前两篇回答"谁在做、瓶颈在哪";**本篇回答前两篇刻意没碰的问题:"优化"这个词在工业界到底指什么操作、用什么数学、在哪一层做**。触发于一次实战:平台建了"优化闭环"(对近门 alpha 做 settings-sweep),933 BRAIN sims → 2 winner → 0 提交(0.2%)。本文用 4 个面的一手文献 + 对抗审查,给"settings-sweep 是不是工业说的 alpha 优化"一个有据可查的回答。

---

## 1. 一句话总览

**工业界的"alpha 优化"几乎从不指"调一条表达式的参数把单个指标顶过门",而指三件完全不同层级的事:(a) 把大量弱而独立的信号组合成一个预测/组合(combination),(b) 在多重检验下对"选出来的赢家"做统计去偏(deflation/CV),(c) 在整本组合上解一个"收益−风险−成本"的凸优化(portfolio construction)。**平台现在做的 single-alpha settings-sweep 在这三层里都不是"优化"——它在 combination 层是制造高相关冗余(不增 breadth)、在统计层是增加 trial 数(放大 overfitting haircut)、在 portfolio 层根本没有那一层。**四个面的文献 + 对抗审查一致 CONFIRM 了既有方法论评审"execution-limited、settings-sweep 是弱深度杠杆"的结论;唯一被 nuance 的是:settings-sweep 不是"零价值",当 sweep 的设置(decay/neutralization)有真实经济含义、且呈"平台(plateau)而非尖峰(peak)"时,它是合法的 de-risk/robustness 诊断——只是绝不能当 discovery 引擎。**

---

## 2. 业界的"优化"分层

核心洞察:专业人士在**五个不同的层**上"优化",而平台目前只在最弱的第 1 层、且用最弱的手法(单表达式扫参)。每往下一层,杠杆越大、平台覆盖越少。

| 层 | 这一层"优化"指什么 | 代表技术 | 谁在用(年份) | 成熟度 | 平台现状 |
|---|---|---|---|---|---|
| **L1 信号生成 / 单 alpha 精炼** | 在**表达式/结构空间**搜索一个**多样的集合**;单 alpha 调参只是末端 de-risk | GP/符号回归(AutoAlpha)、RL(AlphaGen)、生成式(AlphaForge)、MCTS+LLM(AlphaJungle)、LLM-evolve(AlphaAgent/AlphaEvolve);**plateau 选参** | WorldQuant 101-Alphas(Kakushadze 2016);Yu et al. AlphaGen(KDD 2023);Shi et al. AlphaForge(AAAI 2025);DeepMind AlphaEvolve(2025) | L1 教科书,自动化搜索 emerging→frontier | ✅ LLM 生成单表达式;❌ 无"为组合而搜索";**settings-sweep = L1 末端手法当成了主引擎** |
| **L2 组合 / 集成(combination)** | 把许多弱而**弱相关**的 alpha 融成一个预测 | IC/风险加权多因子模型;ML stacking(GBT/NN);DoubleEnsemble;**等权(combination puzzle)**;HRP/聚类组合;AlphaForge 动态时变加权 | Qian-Hua-Sorensen(2007);Gu-Kelly-Xiu(RFS 2020);Qlib(MSR);López de Prado HRP(2016) | textbook→widely-adopted | ❌ **完全缺失**——平台逐条 gate+提交单表达式,无组合层 |
| **L3 抗过拟合的选择(overfitting-controlled selection)** | 在海量候选里**统计正确地**选出真信号,不被运气骗 | Deflated Sharpe + SR0;PBO via CSCV;Sharpe haircut(Bonferroni/Holm/BHY);CPCV(purge+embargo);plateau;Lucky-Factors max-stat;**ONC 聚类算有效 trial 数** | Bailey-López de Prado(2014/15);Harvey-Liu-Zhu(RFS 2016, t>3.0);Chordia-Goyal-Saretto(RFS 2020, t>3.4-3.8);Man Group(2021) | textbook→widely-adopted | ⚠️ 部分(self-corr 门、marginal 打分卡雏形);❌ 无 DSR/PBO/plateau/trial-count haircut |
| **L4 组合构建 / 成本-换手-容量(portfolio & cost)** | 在**整本书**上解凸优化:max(收益−风险−交易成本−持有成本) | Markowitz;Ledoit-Wolf 协方差收缩;Black-Litterman;risk-parity/HRP;Boyd 多期凸优化(cvxportfolio);换手罚项/no-trade band;transfer coefficient | Markowitz(1952);Ledoit-Wolf(2004);Boyd et al.(2017);AQR Frazzini-Israel-Moskowitz($1.7T 实盘) | textbook→widely-adopted | ❌ **完全缺失**——换手/成本被错误地在单 alpha 上"修",没有 netting、没有组合层成本优化 |
| **L5 执行(execution)** | 给定交易清单,最优拆单(冲击 vs 时机风险) | Almgren-Chriss implementation shortfall;VWAP/IS 算法 | Almgren-Chriss(2000);卖方执行台 | textbook | N/A(BRAIN 日级模拟,平台不触及执行) |

**读这张表的方式**:平台的"优化闭环"活在 L1 的最末端(单表达式扫参),而工业界的真实杠杆在 **L2(组合)+ L3(抗过拟合选择)+ L4(组合成本)**。0.2% 转化率不是 tuning bug,是**在错误的层做优化**的预期结果。

---

## 3. 四个面的要点

### 面 1 — 组合 / 集成(combination):工业"优化"的本体

- **决定性技术 = Grinold-Kahn 主动管理基本定律 `IR ≈ IC·√BR`**(Grinold-Kahn《Active Portfolio Management》1999),广义形式 `IR ≈ TC·IC·√BR` 加 transfer coefficient(Clarke-de Silva-Thorley 2002)。结论:per-bet 技巧(IC)与独立下注数(breadth)是**替代品**。经典算例(Buffett vs Simons):IC 只有 1/1000 的信号,用 ~10⁶× 的 breadth(~120 vs ~120,000,000 下注/年)就能匹配同一 IR——**对抗审查确认这组数字算术精确无误**。
- **WorldQuant Alpha Factory 范式**(Tulchinsky《Finding Alphas》2019;Kakushadze 101 Formulaic Alphas 2016, arXiv:1601.00991):量产百万级弱 alpha → 独立的组合/选择层融成策略。单 alpha 质量是次要的,fleet 规模与多样性是主要的。
- **现代化身 = ML 集成**:Gu-Kelly-Xiu(RFS 2020)证明 ML 集成(树/NN)把回归策略的经济收益**翻倍**;DoubleEnsemble(MSR 2020, Qlib)专为低信噪比金融面板抗过拟合;**AlphaForge(AAAI 2025)是最贴近本平台的对照**——挖公式 alpha 后**动态组合**,增益**全部**来自组合层(ICIR 0.144→0.368 约 2.5×;IC 4.40% vs RL 2.09%)。
- **"combination puzzle"重要警示**(Bates-Granger 1969;Timmermann 2006;Wang-Hyndman 50 年综述 2022):**等权常常 OOS 打败"估计最优权重"**,因为最优权重的估计误差吞掉理论增益。→ 组合层也应从简单/等权起步,价值来自多样性而非巧妙拟权。

**对抗审查 NUANCE(关键修正)**:"breadth 无界"是**实质性夸大**。真实 alpha 相关,**有效 breadth 上界 = 1/ρ**(`BR = N/(1+ρ(N−1)) → 1/ρ`,Kakushadze;arXiv:physics/0601166)。ReSolve 对 30 只道指股做 PCA(Kaiser-Guttman)只得 ~1 个统计独立下注。→ 正确表述是"**breadth 大但被 1/ρ 封顶,这恰恰是为什么 *低相关/多样性* 而非 *原始数量* 才是主杠杆**"。这直接给平台 self-corr<0.7 门一个数学解释。

### 面 2 — 抗过拟合的评估与选择:为什么 settings-sweep 在统计上必败

- **决定性事实**(Bailey-Borwein-López de Prado-Zhu《Pseudo-Mathematics and Financial Charlatanism》AMS Notices 2014):**调一条表达式的参数把指标顶过门,正是回测过拟合的教科书定义**。每个 settings variant 都是一次 **trial**,观察到的 Sharpe 必须按 trial 数(及相关结构)**deflate**。
- **三件机械**:
  1. **Deflated Sharpe Ratio + SR0**(Bailey-López de Prado 2014):`SR0` = N 个无技巧策略中"最幸运者"期望的 Sharpe,**随 trial 数 N 上升**。若赢家 Sharpe 没显著超过 SR0,大概率是噪声。
  2. **PBO via CSCV**(2015):衡量 IS 赢家在 OOS 跌破中位数的频率;PBO≈0.5 = 选择过程零 OOS 技巧。**933 sims / 2 winner 的网格,PBO 几乎必然很高**。
  3. **Sharpe haircut**(Harvey-Liu 2015;Harvey-Liu-Zhu RFS 2016 把因子发现门从 t>2.0 抬到 **t>3.0**;Chordia-Goyal-Saretto RFS 2020 从 210 万策略推出 t>3.4-3.8,估 ~45% 朴素显著异象是假的)。Man Group(2021)实务版:用**非线性** haircut,明确否定朴素 50% 砍半。
- **正确的"优化"姿势**:(a) 预登记小的、假设驱动的参数集;(b) 选**稳定平台(plateau)而非脆弱尖峰(peak)**;(c) 用 **CPCV(purge+embargo)**而非单条回测验证(Elsevier KBS 2024 实证 CPCV 比单路 walk-forward 给更低 PBO、更高 DSR);(d) 按**有效 trial 数**(ONC 聚类——相关 variant 算少数独立 trial)deflate;(e) 建 breadth 而非深挖一条。
- **L3 的"选择"工具直击平台真瓶颈**:**Lucky Factors**(Harvey-Liu JFE 2021)正交化 + max-stat 选**边际/增量**贡献者;**meta-labeling**(López de Prado AFML 2018)二级模型给 primary 信号定 size(含到 0),**不重拟 primary**——正是"给 67 个待提交 alpha 按 OOS 存活概率/边际价值排序自动决策"的严格版本。

**对抗审查 NUANCE**:"settings-sweep 就是 trials"作为**保守默认正确**,但作为**绝对论断略过头**——BRAIN 的 decay(降换手→机械抬 Fitness)、neutralization(剥离未补偿风险)有**真实经济含义**,可产生 OOS 稳定的真改进;且 sweep 揭示宽 plateau 时是合法 robustness 诊断。→ 修正:settings-sweep 是**统计上弱、易过拟合**的杠杆(相对 breadth+selection),但当设置经济驱动、呈 plateau 时**并非严格零价值**。

### 面 3 — 组合构建 & 成本/换手/容量:换手该在哪一层修

- **决定性框架**(Boyd-Busseti-Diamond-Kahn et al.《Multi-Period Trading via Convex Optimization》2017,cvxportfolio):每期解一个凸程序 `max(期望收益 − 风险 − 交易成本 − 持有成本)`,**alpha 预测是外生输入**。Boyd 等**逐字声明不处理预测的产生**——干净地把 forecast 层与 trade-optimization 层分开。**对抗审查 CONFIRMED**(逐字核对 abstract)。
- **AQR**(Frazzini-Israel-Moskowitz, $1.7T 实盘)**逐字**:"use portfolio optimization techniques to design strategies that decrease realized trading costs",且执行算法"does not make any explicit portfolio decisions"——**两层架构确证**。
- **换手/成本的归宿 = L4 组合层**:换手罚项、L1/二次成本项、no-trade band,尤其**跨多信号 netting**(一个高换手 alpha 在整本书里的交易可能与别的 alpha 对冲掉→边际成本远低于其单独换手)。**单 alpha 的孤立换手 ≠ 它的边际组合成本**。
- **稳定化工具**(因裸 Markowitz 是"估计误差放大器"):Ledoit-Wolf 协方差收缩(2004,sklearn 默认);Black-Litterman(1992,把 alpha 当 view 注入而不爆角点解);risk-parity/HRP(López de Prado 2016,免求逆、抗病态);Michaud 重采样(2005)。
- **transfer coefficient 重定义瓶颈**:TC = 信号隐含权重与实际约束权重的相关;换手/中性/long-only 约束压低 TC、压低实现 IR。**"把信号弄进书里"(低 TC)正是 execution-limited 的数学刻画**——精确对上既有评审的 execution-limited 结论。

**对抗审查 NUANCE**:"NOT by sweeping a single signal's parameters"措辞**略绝对**——单信号的**成本感知设计**是合法实务(Gârleanu-Pedersen JoF 2013 的"部分向 aim 组合调仓";Qian 的 no-trade band/按信号 autocorrelation 调平滑),但这是**参数/结构设计**而非朴素回测扫参,且被组合层 subsume。被否的只是"把单信号回测扫参当成 optimization 的所在地"。

### 面 4 — 自动化/程序化 alpha 搜索:严肃文献里 settings-sweep 几乎不存在

- **决定性范式转移 = AlphaGen**(Yu-Xue et al., KDD 2023, arXiv:2306.12964):把 RL reward 设为**组合模型的总 IC**,于是生成器为"alpha 之间协同好"而非"单 alpha 强"获奖——显式否定旧的"逐条挖+去重"流水线。CSI300 test IC ~0.0725 vs GP-filter ~0.0183(~4×)。**反直觉发现:互相关 0.9746 的两个 alpha 仍可同时提升组合模型**→朴素去重可能适得其反。
- **谱系**:AutoAlpha(2020,层次进化 + PCA-on-root + 低相关约束);AlphaForge(动态时变组合);**AlphaJungle**(MCTS+LLM, 2505.11122,**Frequent Subtree Avoidance** 显式反结构同质化——与平台 LLM 生成架构直接相关);**AlphaAgent**(2502.16789,三正则:AST 原创性/假设对齐/复杂度罚——抗 crowding+过拟合,CSI500 11.0% 超额 vs 4.96% 基线,IC 4 年稳定);**AlphaEvolve**(DeepMind 2025,维护候选 pool 的 LLM-进化通用范式)。
- **共识**:自动化"优化" = 在**表达式空间**搜一个**多样集合、为组合 IC 联合优化**;冗余/过拟合在两层控制:in-search(组合-IC 目标 + 相关/AST/频繁子树多样性罚 + 复杂度罚)+ 统计(DSR/PBO/purged CV)。**单 alpha 扫参作为 discovery 机制在严肃文献中基本缺席**,且每多一个 variant 都恶化 deflated-Sharpe haircut。

**对抗审查 NUANCE**:AlphaGen 的 reward 严格说是**增量 pool IC**(非简单 total),且参数调优也是"优化"的一种合法义项——所以"自动化优化 = 表达式空间搜多样集合"成立但不应被读成"参数调优不算优化"。

---

## 4. 对照本平台:CONFIRM 还是 CHALLENGE 既有方法论评审

既有评审结论:**execution-limited(67 干净可提交 alpha 积压、仅 12 曾提交);single-alpha settings-sweep 是弱深度杠杆**。本次 4 面调研 + 4 份对抗审查的裁决:

### 4.1 平台**已经在做**的工业实践
- **镜像 BRAIN self-corr<0.7 门**(L3 的"独立下注计数"约束)——v3 已确证,本文给它**数学根据**:它就是 HRP/基本定律里"effective breadth ≤ 1/ρ"的约束实例。
- **marginal 打分卡 + IQC marginal audit 雏形**(`marginal_analysis.py` / `iqc_marginal_audit.py`)——这是 L3 "边际贡献选择"的早期形态,方向对(对齐 Lucky-Factors / meta-labeling)。
- **fitness/sharpe/turnover gate**——L1 末端的单 alpha 质量门,标准但弱。

### 4.2 平台**缺失**的工业实践(按层)
- **L2 组合层:完全缺失**。无 IC/风险加权、无 stacking、无 HRP、无动态组合。**这是与 AlphaForge 对比最刺眼的缺口**——AlphaForge 的全部增益来自这一层。
- **L3 抗过拟合统计:缺 DSR / PBO / plateau / trial-count haircut**。933-sim 网格从未被 deflate。
- **L4 组合成本层:完全缺失**。换手被错误地在单 alpha 上"修"(扫 decay),而工业在组合层用 netting + 罚项处理。
- **L1 自动化搜索:有 LLM 单表达式生成,无"为组合 IC 而搜"**(AlphaGen 范式)、无 Frequent-Subtree-Avoidance 式结构多样性。

### 4.3 调研是 CONFIRM 还是 CHALLENGE?

**四面全部 CONFIRM 既有评审的核心,且加固了数学根据:**
- **面 1/4(combination + portfolio)**:工业"优化"在 L2/L4,平台两层皆无 → settings-sweep 是"在错误的层优化",0.2% 是预期结果。**CONFIRM execution-limited**:transfer-coefficient 把"信号进不了书"刻画为低 TC,精确对上"67 积压"。
- **面 2/3(统计选择)**:settings-sweep 在统计上是增加 trial、放大 haircut;真杠杆是 breadth + 边际选择。**CONFIRM "弱深度杠杆"**。

**对抗审查在三处 NUANCE / 部分 REFUTE,必须诚实标注**:
1. **面 1 REFUTE "breadth 无界"** → breadth 被 1/ρ 封顶。**这对平台是利好论据不是利空**:它解释了为什么 self-corr 门正确、为什么换 universe(高 ρ)≈0 新增 breadth(与 v3 §5 一致),也警告"多挖相关 alpha"同样无效。
2. **面 2/3 NUANCE "sweep 就是 trials / 绝不在单信号修成本"** → decay/neutralization 有真经济含义,plateau-sweep 是合法 robustness 诊断;单信号成本设计(no-trade band、向 aim 调仓)是合法实务。→ **评审"settings-sweep 弱"作为 *discovery claim* 正确;但它不是零价值的 *de-risk* 工具**。失败模式是"把它当主 edge 源、且不 deflate 结果",不是"sweep 这个动作本身错"。
3. **面 4 NUANCE**:AlphaGen 的反直觉发现(互相关 0.97 的 alpha 仍可联合增值)**挑战平台 per-alpha self-corr 一票否决**——self-corr 门可能在丢弃"单看冗余、组合看有边际价值"的 alpha。这不否定门的提交合规作用,但指向:**真正的优化是 set-level 边际 IC,不是 per-alpha 否决**。

---

## 5. 给本平台的建议(按杠杆排序)

约束锚定:**execution-limited;self-corr<0.7 提交墙;BRAIN 单 alpha 提交模型(无真实组合层可提交)**。因此 L2/L4 不能直接落到 BRAIN 提交,但能驱动**选择/排序**——这才是当前杠杆所在。

| 优先级 | 建议 | 对应层/技术 | 成本 | 为什么是最高杠杆 |
|---|---|---|---|---|
| **P0** | **抽干积压的"set-level 边际选择"管线**:对 67 积压按 (self-corr<0.7 vs 已提交) + **边际 IC / Lucky-Factors max-stat / meta-labeling 存活概率**排序,自动出"最正交者先提交"短名单 | L3 边际选择 | 低(已有 `marginal_analysis.py`/`iqc_marginal_audit.py`,只差接成管线) | 直击 execution-limited;零新挖;v3 已指同向,本文加 Lucky-Factors/meta-labeling 严格化 |
| **P0** | **给 settings-sweep 加 plateau 门 + trial-count deflation**:sweep 赢家必须在邻近 decay/window 上 Sharpe 稳定(plateau 而非 peak),并对现有 933-sim 网格算 **PBO/DSR**,用 ONC 算有效 trial 数 deflate | L3 抗过拟合 | 低(纯后处理,~1-2 行 robustness filter + 一个离线脚本) | 几行代码即可杀掉 2 个 lucky winner 那类噪声;把"弱杠杆"至少变成"诚实的弱杠杆"。直接回应 owner 的开放问题 |
| **P1** | **新正交数据源 onboarding**(真 breadth 轴):把账号可用未挖的 BRAIN 数据集/类别接进轮转(现仅挖 15/18,8 大类别未铺满) | L1 breadth | 中 | 唯一能真正增 effective breadth(降 ρ)的杠杆;1/ρ 上界数学证明"多挖相关 alpha"无效,必须降 ρ |
| **P1** | **离线组合层(shadow,不提交)**:对 PASS/积压 alpha 建一个 **HRP/聚类 或 等权/IC-加权 组合**,算每个 alpha 的**边际组合 IC**,作为提交排序信号——把 AlphaForge 的"动态组合增益"以离线评分形式引入 | L2 组合 | 中(纯离线 Python,不碰 BRAIN 提交语义) | AlphaForge 增益全在此层;combination puzzle 说从等权起步即可,不必先做复杂优化 |
| **P2** | **AlphaGen 式"为组合而搜"试点**:把生成/选择 reward 从"单 alpha 过门"转向"对当前已提交 pool 的边际 IC 贡献";探索把 per-alpha self-corr 一票否决放宽为 set-level 边际判定 | L1+L3 联合 | 高(改 reward/选择逻辑 + 验证) | 挑战 self-corr 一票否决的 NUANCE;长期把平台从 discovery-engine 转成 combination-engine |
| **P2** | **Frequent-Subtree-Avoidance / AST 多样性罚**注入 LLM 生成 prompt(AlphaJungle/AlphaAgent) | L1 多样性 | 中 | 直接对上平台 LLM 生成架构;在生成端就降 ρ,比事后 self-corr 拒更省 sim |

**明确不做**:① 把 settings-sweep 当 discovery 主引擎(统计必败);② 多挖相关 alpha(1/ρ 封顶,无新 breadth);③ universe 轮转(同-region 高 ρ → 撞门 + ~0 breadth,v3 已否);④ 直接上 L4/L5 portfolio-execution(BRAIN 单 alpha 提交模型下无落地面,且 v2 已列为远期 G9)。

**一句话给 owner**:你问"single-alpha 调参是不是 professional 说的优化"——**不是**。专业的"优化"是 L2 组合 + L3 抗过拟合选择 + L4 组合成本三层;你平台缺这三层,而 settings-sweep 活在最弱的 L1 末端。最高杠杆不是把 sweep 调好,而是**把 67 积压用 set-level 边际选择抽干(P0,零新挖)+ 给 sweep 加 plateau/deflation 止血(P0)+ 上新正交数据源真正增 breadth(P1)**。

---

## 6. Sources(去重、分组、含 URL 与年份)

### 学术 — 组合 & 基本定律
- Grinold & Kahn, *Active Portfolio Management* (1999) — IR ≈ IC·√BR — https://www.amazon.com/Active-Portfolio-Management-Quantitative-Controlling/dp/0070248826
- Clarke, de Silva & Thorley, *Portfolio Constraints and the Fundamental Law* (FAJ 2002) — IR ≈ TC·IC·√BR — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=290322
- Kakushadze, *How many independent bets are there?* — effective breadth = 1/ρ — https://arxiv.org/abs/physics/0601166
- Qian, Hua & Sorensen, *Quantitative Equity Portfolio Management* (2007) — 多因子 IC/风险加权组合
- Bates & Granger (1969); Wang & Hyndman, *Forecast combinations: a 50-year review* (2022) — combination puzzle — https://arxiv.org/pdf/2205.04216
- Gu, Kelly & Xiu, *Empirical Asset Pricing via ML* (RFS 2020) — https://academic.oup.com/rfs/article/33/5/2223/5758276

### 学术 — 抗过拟合 & 选择
- Bailey, Borwein, López de Prado, Zhu, *Pseudo-Mathematics and Financial Charlatanism* (AMS Notices 2014) — https://www.ams.org/notices/201405/rnoti-p458.pdf
- Bailey & López de Prado, *The Deflated Sharpe Ratio* (JPM 2014) — DSR + SR0 — https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- Bailey et al., *The Probability of Backtest Overfitting* (JCF 2015) — PBO/CSCV — https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf
- Harvey & Liu, *Backtesting* (Haircut Sharpe, JPM 2015) — https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF
- Harvey, Liu & Zhu, *…and the Cross-Section of Expected Returns* (RFS 2016, t>3.0) — https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF
- Chordia, Goyal & Saretto, *p-Hacking: Evidence from Two Million Trading Strategies* (RFS 2020, t>3.4-3.8) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3017677
- Harvey & Liu, *Lucky Factors* (JFE 2021) — 正交化 max-stat 边际选择 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2528780
- López de Prado, *Advances in Financial Machine Learning* (2018) — CPCV/purge-embargo/meta-labeling/ONC
- Arian, Norouzi & Seco, *Backtest Overfitting in the ML Era* (KBS/Elsevier 2024) — CPCV vs walk-forward — https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110

### 学术 — 组合构建 & 成本
- Boyd et al., *Multi-Period Trading via Convex Optimization* (2017, cvxportfolio) — https://arxiv.org/abs/1705.00109
- Frazzini, Israel & Moskowitz (AQR), *Trading Costs* (2018) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3229719
- Ledoit & Wolf, *Honey, I Shrunk the Sample Covariance Matrix* (2004) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=433840
- López de Prado, *Building Diversified Portfolios that Outperform OOS* (HRP, 2016) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2708678
- Black & Litterman / He & Litterman (1992); Michaud & Michaud, *Estimation Error and Portfolio Optimization* (2005) — https://newfrontieradvisors.com/media/rxbld4hq/estimation-error-and-portfolio-optimization-12-05.pdf
- Gârleanu & Pedersen, *Dynamic Trading with Predictable Returns and Transaction Costs* (JoF 2013) — https://www.nber.org/papers/w15205
- Almgren & Chriss, *Optimal Execution of Portfolio Transactions* (2000) — https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf

### 学术 — 自动化 alpha 搜索
- Yu, Xue et al., *AlphaGen: Generating Synergistic Formulaic Alpha Collections via RL* (KDD 2023) — https://arxiv.org/abs/2306.12964
- Zhang et al., *AutoAlpha* (2020) — https://arxiv.org/pdf/2002.08245
- Shi et al., *AlphaForge* (AAAI 2025) — https://arxiv.org/html/2406.18394v1
- *Navigating the Alpha Jungle* (LLM-MCTS, 2025) — Frequent Subtree Avoidance — https://arxiv.org/html/2505.11122v1
- *AlphaAgent* (2025) — AST/假设/复杂度三正则 — https://arxiv.org/html/2502.16789v2
- Zhang, Li et al., *DoubleEnsemble* (MSR 2020, Qlib) — https://www.semanticscholar.org/paper/96d8383288eba50d69f516522154cf52625c7a4f
- Google DeepMind, *AlphaEvolve* (2025) — https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/

### 工业一手 / 实务
- WorldQuant / Tulchinsky (ed.), *Finding Alphas* (Wiley 2019) — https://onlinelibrary.wiley.com/doi/abs/10.1002/9781119571278.ch12
- Kakushadze, *101 Formulaic Alphas* (2016) — https://arxiv.org/abs/1601.00991;*Combining Alpha Streams with Costs* (2014, arXiv:1405.4716)
- Man Group, *Backtesting* (practitioner 非线性 haircut, 2021) — https://www.man.com/insights/backtesting
- ReSolve Asset Management, *The Fundamental Flaw of Grinold's Fundamental Law* — 30 道指股 ≈ 1 独立下注 — investresolve.com

### 工具 / 实现
- Qlib (Microsoft, model zoo + DoubleEnsemble);cvxportfolio (Boyd);skfolio / PyPortfolioOpt (HRP, Ledoit-Wolf, CPCV);R `pbo` 包(CSCV);mlfinpy / Hudson&Thames(DSR, meta-labeling)

---

*本文聚焦"优化工具箱",与 v2(竞品景观)、v3(BRAIN 提交模型 + selection-vs-discovery)互补、不重复。核心结论:工业"alpha 优化"在 L2/L3/L4,平台缺这三层、且 settings-sweep 活在最弱的 L1 末端——4 面调研全 CONFIRM 既有 execution-limited 评审,三处 NUANCE(breadth 有界 / sweep 非零价值 / self-corr 一票否决可商榷)已诚实标注。*