# 竞品分析 v3 — selection vs discovery:顶级玩家与 BRAIN 怎么把候选 alpha 变成提交

> **文档日期**:2026-05-26
> **承前**:[`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)(8 工业派深挖 + 学界 ~25 系统 + AIAC 5 gap)、[`competitive_analysis_ai_alpha_mining_2026-05-17.md`](competitive_analysis_ai_alpha_mining_2026-05-17.md)(v1)
> **本文增量(v2 缺的视角)**:
> 1. **WorldQuant BRAIN 自身的 alpha 估值/提交模型**——AIAC 最直接的竞品/平台(我们就是往 BRAIN 提交),v2 只写了对冲基金、没深挖 BRAIN 本身的提交激励机制
> 2. **selection vs discovery 瓶颈定位**——顶级玩家与学界的共识:约束在「从巨量候选里选出真信号」,不在「生成更多候选」
> 3. **广度的"轴"**——Grinold 定律下,广度=独立(不相关)下注数;换股票池 universe ≠ 独立,广度来自新正交数据源
> **触发原因**:长期自动化挖掘方向辩论 + 一次对抗审查揪出「11 已提交 vs 121 can_submit 积压未提交」(亲查 live DB),需用顶级实践给"该建什么"定锚
> **决策影响**:为「抽干 can_submit 积压 / onboard 新数据源 / 多 universe 轮转」三选给出工业+学界+平台三源依据

---

## 1. 三条 take-away

1. **顶级玩家是 selection-limited,不是 discovery-limited。** 生成候选已商品化(Two Sigma 100k sims/天);护城河在**选择/组合/相关性控制/风控**层(factor-lens 正交化、边际贡献、容量、组合)。学界共识同向:López de Prado「多重检验危机」、Harvey-Liu-Zhu「factor zoo」把显著性门从 t>2 抬到 **t>3**。
2. **BRAIN 自身的绑定门 = self-correlation < 0.7 vs 你自己已提交的 alpha。** 同 region、日收益 Pearson、4 年滚窗、取 max;≥0.7 仅当新 Sharpe ≥ 1.1× 那个相关 alpha 才放行。Pyramid 价值多样性加权(数据类别 × region × delay),目的就是逼用户分散以**降 self-correlation**。顶级 BRAIN 用户/Consultant 优化的是「**低相关 + 多样**」,不是多挖(实证:Glazar 提交 28/1103 ≈ **2.5%**,漏斗塌在选择;CrisperX 整个项目就是「50 个**互相**正交到能一起提交」)。
3. **广度的轴是「数据源/region/delay」,不是 stock universe。** Grinold:IR = IC × √breadth,breadth = **独立**下注数。同一信号换股票池 → 相关 PnL → ~0 新增 breadth,且在 BRAIN 上**同-region 撞 0.7 self-corr 门**直接被拒。Tulchinsky《Finding Alphas》:成功度量 = **不相关** alpha 的数量。

---

## 2. 调研方法

| 信息源 | 来源 |
|---|---|
| BRAIN 一手 | worldquant.com/brain、worldquantbrain.com/consultant、IQC guidelines、alpha-examples |
| BRAIN 二手(API 逆向 / 参赛者写作) | DeepWiki(xiegengcai / krocellx WorldQuant repo 逆向)、Glazar IQC project、CrisperX 仓库 |
| 学界(选择-偏差/多重检验) | López de Prado(SSRN 3177057 / 2460551)、Harvey-Liu-Zhu(NBER w20592)、Grinold(Fundamental Law)、Tulchinsky《Finding Alphas》(Wiley)|
| 工业派(承自 v2) | Two Sigma factor lens(一手 venn/twosigma)+ v2 的 8 家 |
| AIAC 自审(亲查) | live DB(11 submitted / 121 can_submit-unsubmitted / 67 AIAC-mined)、`config.py`(EVAL_SELF_CORR_MAX=0.7)、`correlation_service.py`(日收益 diff)|

每条 fact 标 `[一手]` / `[二手]` / `[亲查]`。

---

## 3. WorldQuant BRAIN 自身模型(v2 缺;最直接竞品)

AIAC 就是往 BRAIN 提交 alpha,所以 BRAIN 的**提交估值机制**才是"how to grow from 11 submitted"的直接答案。

### 3.1 单 alpha 评分 + 提交门
- **IS 门**:Sharpe / turnover / **fitness** = `sqrt(|Returns| / max(turnover, 0.125)) × Sharpe`(奖高 Sharpe+收益、低换手)。`[二手:Glazar IQC]`
- **fitness/sharpe/turnover 阈值** AIAC 侧对齐 `config.EVAL_*`(flat EVAL band)。`[亲查]`

### 3.2 绑定门 = self-correlation(确证带数字)
- **阈值 0.7**,vs **你自己已提交(production/OS)的 alpha**,**仅同 region**,基于 **PnL 转日收益的 Pearson**,**4 年滚窗**,报**所有自有 alpha 中的 max**。`[二手:DeepWiki xiegengcai 4.1 / krocellx 4.3]`
- **决策逻辑**:self-corr < 0.7 → 可提交;≥ 0.7 → 仅当新 Sharpe ≥ **1.1 ×** 那个相关 alpha 的 Sharpe(「10% 改进」例外)才可提交,否则拒。`[二手:同上]`
- **AIAC 已忠实镜像此门**:`EVAL_SELF_CORR_MAX = 0.7`、`MAX_CORRELATION = 0.7`(`config.py:844/194`),`correlation_service._series_to_returns = pnl - pnl.ffill().shift(1)`(日收益 diff,非累计——避免 ~0.98 假象),`calc_self_corr` 取 max。`[亲查]`

### 3.3 Pyramid 多样性加权
- "Pyramid multiplier" 是 BRAIN 内部术语,**无公开数字表**(信息不可得);**机制确证**:价值 = 多样性加权 + 相关性门控。BRAIN 明确引导用户跨**数据类别**(PriceVolume / Fundamental / Analyst / Sentiment / Options / Model / Insider / ShortInterest)+ **多 region/delay** 提交,目的就是**降 self-correlation**。`[一手:worldquant.com/brain、alpha-examples、consultant]`
- 经济机制 = 边际价值:挤进已覆盖类别/region 的 alpha 边际价值 ~0 且大概率撞相关门;新类别/region/delay 清门得满分。

### 3.4 顶级 BRAIN 用户/Consultant 怎么最大化提交数
- 优化「**低相关 + 多样数据/region/delay**」,**不是多挖**。`[一手:consultant「accumulate points by submitting Alphas that meet criteria」]`
- 实证:Glazar 测 1103 个、提交 **28(2.5%)**——漏斗塌在选择。`[二手:Glazar]` CrisperX 仓库存在的意义 = 「50 个**一起提交能过相关测试**的 alpha」(声称 400+)——硬工程问题是**互相正交**,不是生成候选。`[二手:CrisperX repo]`
- **BRAIN 结论**:绑定约束 = 选择(self-corr-vs-own-pool + 边际门),**不是生成**。

---

## 4. 选择 vs 发现(基金 + 学界)

- **López de Prado**:"Most discoveries in empirical finance are false, as a consequence of **selection bias under multiple testing**." Deflated Sharpe Ratio / CPCV 的存在就是为了从大候选池里**选**出真信号。`[一手:SSRN 3177057 / 2460551]`
- **Harvey-Liu-Zhu「…and the Cross-Section of Expected Returns」**:factor zoo;生成变便宜后,显著性门必须从 t>2.0 抬到 **t>3.0**。选择是纪律。`[一手:NBER w20592]`
- **Two Sigma Factor Lens**:"Not all residual return represents true 'alpha'; it can often just be uncompensated risk." 价值 = 剥离 style/macro 暴露后的**正交化(残差)边际贡献**;且 Two Sigma 跑 100k+ sims/天——**生成已工业化/商品化**,护城河在验证/选择/组合/风控。`[一手:twosigma factor lens / venn;承 v2 §5.4]`
- **共识:瓶颈在选择/组合/相关性控制,不在候选生成。**

---

## 5. 广度的"轴":数据源,不是 universe

- **Grinold 主动管理基本定律**:`IR = IC × √breadth`,breadth = **独立**下注数。同一信号在不同股票池上 = 同因子、相关 PnL = **非独立** → ~0 新增 breadth。`[一手:Grinold 1989]`
- 在 BRAIN 上更直接:同 region 的不同 universe(如 TOP1000 ⊂ TOP3000)PnL 高度相关 → **撞同-region 0.7 self-corr 门** → 提交不了。
- **Tulchinsky《Finding Alphas》**:成功度量 = **不相关** alpha 的数量;多样性「make-or-break」。`[一手:Wiley]`
- Two Sigma 从「a wide spectrum of traditional and non-traditional data」抽信号——是**新数据**,不是把旧信号重铺到新池子。`[一手;承 v2 §5.4]`
- **广度结论**:来自**新正交数据源 / region / delay / 频段**,**不是 stock universe 轮转**。

---

## 6. AIAC 现状映射 + 杠杆排序

### 6.1 现状(亲查 live DB,2026-05-26)
- **11 已提交 / 132 can_submit / 121 can_submit 但未提交(67 个 AIAC-mined)/ 提交 ~5/月**(4月6 + 5月5)。
- USA 目录 18 数据集、**实挖 15**;can_submit 集中在 pv1(62)/analyst4(7)/news12(1)等少数。
- → **教科书 selection 瓶颈**:can_submit 积压是提交速率的 ~24×。

### 6.2 多 universe 轮转为何是低杠杆(给数学理由)
- self-corr 门是**同-region**。USA 的 TOP1000/TOP2000 ⊂ TOP3000(重叠股票池)→ 同信号 PnL 高相关 → **撞 0.7 门**,提交不了;Grinold 下也 ~0 新增 breadth。
- RRd2kvJz 能成的**唯一原因**:TOPSP500(大盘)与 TOP3000(含小盘)**最大不相交** → 日收益 self-corr 0.31。中间 universe 重叠重 → 大概率废。
- 实证背书:本轮 transfer-harvest 70 赢家 → TOPSP500 仅 4 no_FAIL,**self-corr 后净新可提交 = 0**(2 个 analyst4 与 RRd2kvJz 冗余 0.96-1.00;2 个 pv1 边际负)。

### 6.3 杠杆排序
**高杠杆(贴合顶级实践)**:
1. **抽干 121 积压**:自动化按 self-corr<0.7(vs 已提交 11,同 region 日收益)+ 边际打分卡 + 多样性排序,**最正交的先提交**。现成工具 `backend/marginal_analysis.py` + `scripts/iqc_marginal_audit.py`,只差接成「积压排序→提交短名单」管线。立即见效、零新挖。
2. **新正交数据源 onboarding**:真广度轴——把账号可用但未挖的 BRAIN 数据集/类别(现只挖 15/18,且 8 大类别未铺满)接进挖掘轮转。

**低杠杆(避免)**:
- 多挖 / discovery(积压已 24× 提交速率)。
- **多 universe 轮转**(同-region 相关 → 撞门 + ~0 Grinold breadth;且运维上每次暂停留 BRAIN 僵尸 sim、≤3 槽不变式难保——见对抗审查)。

### 6.4 与既有结论一致
本文与 memory「广度 not 深度 = 新正交数据源进轮转,非 EVAL/prompt tweak」([[project_depth_levers_refuted_breadth_is_answer_2026_05_25]])、v2 §8「AIAC gap 在评估/选择/风控层非生成层」([[reference_competitive_analysis_v2_2026_05_19]])、以及 `marginal_analysis.py` 方向([[project_marginal_submit_recommendation_2026_05_24]])**三方一致**。本文新增的是:用 BRAIN 自身的同-region self-corr 门 + Grinold 给「universe 轮转为何弱」一个**数学**理由,并用 121-积压把瓶颈从"发现"重定位到"选择"。

---

## 7. 信息不可得
- BRAIN pyramid multiplier 的精确数字表(内部,只确证机制非数值)
- BRAIN prod-correlation(跨全平台用户)的精确算法(self-corr 是自有池;prod-corr 更严但算法未公开)
- 顶级对冲基金的候选→部署精确转化率(NDA)

## 8. Sources
**BRAIN**:[brain 平台 一手](https://www.worldquant.com/brain/) · [alpha-examples 一手](https://worldquantbrain.com/alpha-examples) · [consultant 一手](https://worldquantbrain.com/consultant) · [IQC guidelines 一手](https://www.worldquant.com/brain/iqc-guidelines/) · [self-corr DeepWiki/xiegengcai 二手](https://deepwiki.com/xiegengcai/world-quant-brain/4.1-self-correlation-analysis) · [DeepWiki/krocellx 二手](https://deepwiki.com/krocellx/WorldQuant-Alpha-Research/4.3-result-extraction-and-correlation-analysis) · [Glazar IQC 二手](https://jglazar.github.io/projects/wq_project/) · [CrisperX 二手](https://github.com/jingmouren/CrisperX-50_WorldQuant_Alpha_Examples_for_Alphathon)
**学界**:[López de Prado 多重检验 SSRN 3177057](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3177057) · [Deflated Sharpe SSRN 2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) · [Harvey-Liu-Zhu NBER w20592](https://www.nber.org/system/files/working_papers/w20592/w20592.pdf) · [Two Sigma Factor Lens](https://www.twosigma.com/wp-content/uploads/Introducing-the-Two-Sigma-Factor-Lens.10.18.pdf) · [Grinold Fundamental Law](https://analystprep.com/study-notes/cfa-level-2/) · [Tulchinsky《Finding Alphas》Wiley](https://onlinelibrary.wiley.com/doi/book/10.1002/9781119057871)
**AIAC 自审**:live DB(亲查 11/121/67)· `backend/config.py:844` EVAL_SELF_CORR_MAX=0.7 · `backend/services/correlation_service.py:129` 日收益 diff

---

*v3 独立可引;v1/v2 保留为历史。核心结论:AIAC 是 selection-limited(121 积压),顶级实践指向「抽干积压(marginal/self-corr 自动选择)+ 新正交数据源」,而非多挖或 universe 轮转。下一步 build 方向据此定。*
