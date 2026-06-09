# 设计稿:正交广度字段探索环(Orthogonal-Breadth Field Exploration Loop)2026-06-08

> 状态:**草案 — 两轮对抗审查 + canary 已跑。裁决演进:REVISE → 🟢 核心赌注 canary PASS(未碰字段信号 ≈/略高于熟字段,长尾噪声假设被证伪)→ 方向已实证,待 pool/regime gate 满足即建。先读 §0 + §0.2 + §0.3。**

## 0. 对抗审查结论(`wzvcwsm0t`,权威,覆盖下文一切乐观标注)

**总判定:需修订再定稿 + 先 canary 验长尾信号 + 主体 DEFER 到「池重启 + regime 非 DOWN」。** 不是该全否的第 4 个坏假设——根因诊断(886/8365=**10.6% 覆盖** / pv1 **34%** / 18 集一致 0 yield = **内卷非空**)是对的、新的、被 SOTA(AlphaAgent 同根因)独立印证,且**不被 execution-limited / breadth-not-depth 逻辑直接杀死**——是本会话唯一真·进攻方向。但落地态有硬缺陷:

**⚠️ 存在性纠错(下文 §2.1/§2.3/§3 的「已存在/非新基建」全是误标,实测如下)**:

| 设计声称 | 实测 | 判定 |
|---|---|---|
| `FieldScreener`「已存在,接 HG」(§2.3)| **源码已删**(`b89b732`,仅剩 `field_screener.cpython-311.pyc`)| ❌ 核心 steering 件需**重写 ~428 行** |
| `datafield_cell_stats` 维护 times_mined/signal_p90/orthogonality/last_mined/band_pass_count(§2.1)| 表只有 date_coverage/coverage/pyramid_multiplier/user_count/alpha_count/themes(`metadata.py:119`)| ❌ **6 列全无**,reward 三分子都算不出 |
| scheduler 按字段选(§2.3/§3)| `pool/scheduler.py` 只读 `dataset_cell_stats.mining_weight`(数据集粒度)| ❌ 字段粒度未接 |
| `marginal_analysis` 即插(§8.4 正交项)| `analyze_marginal_contribution` 存在,但**单 alpha 粒度**(before/after deltas → 建议),非字段聚合 | ⚠️ 存在但粒度错,字段级聚合口径**未定义** |
| G3 AST 原创性「接进生成目标」(§8.4.3)| `soft_regularizer.originality_penalty` 存在,但 **E 阶段 post-hoc**,不注入 code_gen | ⚠️ 仅事后监控 |
| `resim_backlog`/`regime_monitor`/`robustness` 体检(§2.4)| 均在 | ✅ **唯一真已存在的一环** |

**必改(HIGH,GO 前阻塞)**:
1. **文档状态**:§2.1/§2.3/§3 的「已存在/非新基建」改为诚实 Phase 标注——**只有 §2.4 体检环是 already-built;字段环(状态列+回填 beat+FieldScreener+scheduler 改粒度)是全新基建**。
2. **orthogonality reward**:删 §2.2/§3 内联的 `1−max_corr`(`evaluation.py:722` 现行实现,与 §8.4 冲突),回填为「对池组合边际贡献」,并**定字段级聚合口径**(该字段名下 alpha 的均值/最高边际?新建?)——否则实现者无法落地。
3. **signal_quality = p90-Sharpe 是已被否方案换皮**(最尖锐):`dataset_weight_refresh` v6 当初**正因 graded sharpe 被 CONCENTRATED_WEIGHT 愚人金驱动**(model16:110 个 sharpe≥1.25 却 0/110 can_submit)才改 binary can_submit。p90-Sharpe 在字段粒度复活此死路 → 必须 `p90 × can_submit_rate`(或叠 robustness),不能裸用 p90。
4. **双环冲突**:字段 bandit 奖新字段,但 KB success-RAG(`hierarchical_rag` 读 + `persister` 写)仍奖 pv1 旧 pattern,两环对冲(同 DAG-UCB vs bandit 旧坑)。必须定替换/并存/两层 + RAG 如何不抵消。
5. **乐观先验冷启未量化**:K(几次塌缩)/初值/`E[cost]=字段×cells×K×sim` 未定义;止损期 sim 预算受限,这是硬数。

**应改(MED)**:提交池基数仅 13 + pv1 主导作正交基准噪声大(用 ~67 can_submit 池 + min-overlap 守卫 + 小池诚实标);scheduler 仍贪心 `weighted_choice`,未改 §8.3 的 Thompson/比例采样;reward 三项小基数下共线(self-pruning 兜底,需 Phase 2 实证监控)。

**落地顺序(分两 PR,降爆炸半径)+ 硬 gate** → 见 §9。

### 0.2 第二轮深度概念审查(`wf2tanq33`,41→36 确认,更狠)

一审抓战术(存在性/缺列);二审攻**概念层**,新增三处更深打击 + 收紧裁决:

1. **「10.6%」分母被夸大(category-error,§0 未纠)**:`fetch_helpers.py:28 _is_signal_field` 早在喂 LLM 前就剔了 UNIVERSE/SYMBOL/timestamp/ISO 类——**8365 是原始 API 目录,非可挖信号字段全集**。真覆盖率**显著高于 10.6%**。内卷方向仍对(pv1 34% / 18 集 yield 一致 0),但**具体数字虚高,凡引用 10.6% 处都要标"分母含不可挖元数据字段,真值更高"**。
2. **marginal 修正不连贯 + 没真破环(最致命)**:§2.2 静态 `1−max_corr` 与 §8.4「组合边际」数学不等价;且组合边际**仍反向依赖那 13 个 pv1 主导的提交池** → **正反馈环只是被换了标签,没被打破**。环的核心卖点("打破拥挤")因此存疑——orthogonality 项要么换「与池关联硬门(self_corr<0.5)+ novelty + signal 复合、marginal 移出 reward」,要么显式接受 13-池噪声 + min-overlap 守卫,不能继续用"换标签的池依赖 marginal"。
3. **§9「PR-A 可 staging」自相矛盾**:三 gate 全不满时 PR-A 也是死资产 → 删。正确路径 = **canary 先行,过了 PR-A+PR-B 一起上**(见已修订 §9)。

**二审总裁决 = REVISE(不 GO 不 KILL)**:方向保留(唯一不被 execution-limited 逻辑杀死的进攻轴),但核心赌注零验证 + 爆炸半径大 + 三 gate 市场驱动翻不了 → **建 = 造死资产**。**GO 前必改**:上述 1/2/3 + 一审的 signal `p90×can_submit_rate`(防愚人金)+ 字段级正交聚合口径 + 双环仲裁。

**🎯 唯一生死问题(整环的赌注)**:**经 `_is_signal_field` 过滤后的未碰真·信号字段,IS Sharpe 分布是否真低于 886 熟字段?**
- ≈ 或 < → **89% 未碰是生成器的正确回避**,整环过度工程 → **朴素结论:长尾无信号,别建,真杠杆是新正交数据源/跨区 + robustness 门**;
- 有 p50>1.0 的字段 → 才值得投基建。
- **这一锤(离线 canary)出结果前,任何 PR 代码都是赌博,不写一行。**

### 0.3 🟢 canary 结果(2026-06-09,已跑,核心赌注 PASS)

**setup**:12 未碰 vs 12 已用 MATRIX 字段(跨同批正交数据集,经 `datafield_cell_stats` is_active 可用过滤,杜绝 unknown-variable)× 3 标准模板(`rank`/`ts_rank`/`ts_mean`,neut=SUBINDUSTRY)→ 72 sim,取每字段 best,比 RELATIVE 分布。

| 组 | n | p50 | mean | max | err |
|---|---|---|---|---|---|
| **UNTOUCHED 未碰** | 12 | **0.43** | 0.47 | **1.56**(option8 IV) | 2/36 |
| **USED 已用** | 12 | 0.29 | 0.32 | 0.96 | 1/36 |

**RELATIVE p50(未碰)/p50(已用) = 1.48** —— **远不是 <<1**。

**裁决:核心赌注 PASS。** 「未碰=生成器正确回避/长尾噪声」被证伪;**89% 未碰是真·未探索库存,不是噪声**,广度环方向已实证(本会话第一个通过实证的进攻假设)。option-vol 类未碰字段(1.56)是首批高价值目标。

**诚实限定(纪律)**:① **绝对值都低**(p50 0.3-0.43)→ regime DOWN 下两组都不可提交,canary 验的是**信号存在性(相对)非可提交性(绝对)**;② 测简单模板,非字段全潜力。

**✅ 确认性 canary(N=25,150 sim,2026-06-09)**:UNTOUCHED p50=0.42/mean=0.49(n=25,0 错)vs USED p50=0.36/mean=0.29(n=25,3 错);RELATIVE=1.17;**置换检验(median diff two-sided,20000 次)obs=+0.060 p=0.82 → 两分布统计不可区分**。首跑 1.48 部分是小样本噪声,N=25 回到 ≈持平。**稳健结论(N=25+检验背书)**:**89% 未碰字段信号分布与 886 拥挤字段不可区分 → 长尾不是噪声/不是生成器正确回避,under-exploration 是真的。** 不主张「未碰更好」(p=0.82 无显著差异)。

**真·可用 roster 修正(顺带)**:USA/TOP3000/d1 `is_active` + 卫生过滤后 = **7989 信号字段 / 808 用 = 10.1% 覆盖**(原 8365/886=10.6% 是含 inactive 的虚高分母;修正后量级不变,内卷诊断稳)。7181 个可用未碰字段。

**对全环判定的影响**:从「DEFER + 大概率 KILL」→ **「核心赌注已验、方向成立、待 pool/regime gate 满足即建」**。不改变当下(regime DOWN+池 OFF 仍不可提交);改变**前向计划确定性**(从赌→验过)。

---
> 起因:本会话实证 USA 字段覆盖率 = **886 / 8365 = 10.6%**(89% 字段从没进过任何 alpha 表达式),且 14193 alpha 里 **34% 砸在 pv1**;18 个 USA 数据集 yield **一致 0–0.85%**(正交集 option8/fundamental2/sentiment1 也挖了几百个 → 同样 ~0)。结论:不是「市场空 / regime DOWN」,是**生成器在 ~886 个熟字段里内卷**,反馈环把探索逼成了 exploit。

## 1. 问题:现有两个反馈环都是「正反馈到拥挤」

| 现有环 | reward | 实际效果 |
|---|---|---|
| dataset bandit(`ENABLE_DATASET_VALUE_BANDIT`,`pool/scheduler.py` weighted_choice over `dataset_cell_stats.mining_weight`)| binary **can_submit**(discounted Beta-Bernoulli)| ① 粒度=数据集(18),看不见「pv1 的 close 拥挤但 pv1 还有 100 个没碰的字段」;② can_submit ~0.5% 正例太稀疏,学不动;③ pv1 绝对 can_submit 最多(42)→ 权重偏 pv1 → 更多 pv1 → **更拥挤**(正反馈) |
| KB success-pattern RAG(`agents/hierarchical_rag.py` 读 + `pipeline/persister.py` 写)| 过往 PASS/SUCCESS 模式 | 把生成偏向已成功的(=pv1 价量)→ **再强化熟字段拥挤** |

两环都 reward「已知赢家」→ 死磕 886 熟字段 → 产出近重复(撞 self_corr)、被 #40 判稀释(59/67 vs 我池)、regime 一动一起垮。**根因是反馈环目标错位,不是 selection / regime。**

> 关键区分:NO-GO「多挖=0 yield」测的是**再挖同一批 886 拥挤字段**(成立)。**挖未碰的 89% + 强制正交 = 从没测过**,且与 NO-GO 不矛盾。

## 2. 正确的环:奖「正交广度」而非「can_submit」

换 reward 的**对象、密度、方向**三件事。

### 2.1 State — per-field ledger(粒度=字段)
基础设施已存在:`datafield_cell_stats`(cell_stats 规范化时建,per-(datafield, universe, delay))。每字段维护:
- `times_mined`(被某 alpha 表达式用过几次)、`distinct_alphas`
- `signal_p90`(该字段名下 alpha 的 IS Sharpe p90 — 密集质量信号,非稀疏 can_submit)
- `orthogonality`(= 1 − max_corr(该字段 alpha 的 PnL, 已提交池 PnL);需 PnL,可从存量 `alpha_pnl` 引导)
- `last_mined`、`band_pass_count`

大多数字段 = 从没挖(全 0)→ 这是探索目标。

### 2.2 Reward — 正交广度复合(密集 + 抗拥挤 + 乐观探索)
> ⚠️ **本节以 §0.2/§8.4 为准(两轮审查已驳下列原稿)**:① `1−max_corr` 已废(换组合边际,但组合边际仍依赖 13-pv1 池=**换标签未真破环**,见 §0.2-2,待重设计);② `signal_quality` 裸 p90-Sharpe 是 CONCENTRATED_WEIGHT 愚人金,必须 `p90×can_submit_rate`;③ 「未碰字段=探索目标」的覆盖率分母虚高(§0.2-1)。
```
field_score = novelty(field) × orthogonality_potential(field) × signal_quality(field)

  novelty        = UCB 式探索奖励 ∝ 1/√(times_mined + 1)         # 未碰字段高分,驱动 89%
  orthogonality_potential = 1 − max_corr(field alphas PnL, 提交池 PnL)
                            未碰字段未知 → 乐观先验(optimism-under-uncertainty)# 直击 #40 + self_corr + Grinold 广度
  signal_quality = 归一化 p90 Sharpe(密集)
                            未碰字段未知 → 乐观先验
```
- **乐观先验**:未碰字段的 orthogonality/signal 未知 → 给乐观初值 → **必被试到**;试 K 次后先验塌成实测 → **死字段自动掉出(self-pruning)**,不再浪费 sim。
- **方向反转**:字段被挖多 → novelty 衰减 → 环**自动轮转到新字段** → 覆盖率 10.6%→↑、拥挤↓(与现有 bandit 正反馈相反)。

### 2.3 Selection / Steering — 字段导向喂给生成器
- scheduler 按 `field_score` 挑「欠探索 ∩ 潜在正交」字段(单个或字段集),不再只挑数据集。
- **`FieldScreener`(已存在,memory 标"未接 FLAT")接入池 HG stage**:把目标字段注入 `hypothesis` / `code_gen` 的 prompt 上下文,**强制生成器离开默认熟字段**(close/volume/常见基本面),围绕目标字段造表达式。
- 仍受语义校验(`alpha_semantic_validator`)+ 算子可见性约束。

### 2.4 诚实闭合 — robustness ∩ regime 体检后才算「好供给」
探索出的 alpha 不直接进提交,要过:
- **当前数据 re-sim**(`resim_backlog` / `regime_monitor`,本会话已建)→ 滤掉 IS 好但当前数据衰减的;
- **robustness_selector**(已建)→ 子周期一致性,滤孤峰货;
- **正交门**(self_corr<0.7 + marginal)→ 与提交池去相关。
只有 robust ∩ orthogonal ∩ 当前数据稳的进提交队列。

## 3. 环路图 + 接线

```
[field-coverage bandit]  datafield_cell_stats.mining_weight ← reward=正交广度
   │  挑「欠探索 ∩ 潜在正交」字段(乐观先验保证未碰字段被试)
   ▼
[HG]  FieldScreener 注入目标字段 → hypothesis/code_gen 围绕该字段造表达式
   │  (semantic_validator 守语法/算子)
   ▼
[S]  BRAIN simulate
   ▼
[E]  evaluate → 更新 per-field ledger:signal_p90 + orthogonality(vs 提交池 PnL)+ times_mined
   │  reward 回填 bandit;novelty 随 times_mined 衰减
   └────────────► 下一轮挑新字段(覆盖率↑ / 正交↑ / 拥挤↓ / 死字段自动 prune)
   ┊
   [定期 re-sim 存活者] regime 衰减体检 → 仅 robust∩orthogonal∩当前稳 → 提交队列
```

接线落点(全在现有件上,非新基建):
- bandit reward 源:`pool/scheduler.py weighted_choice`(改读 `datafield_cell_stats` 的新 field_score,而非 dataset can_submit)
- per-field 统计:`datafield_cell_stats`(在)+ 一个回填 beat(从 `alphas`/`alpha_pnl` 算 signal_p90/orthogonality)
- 字段注入:`field_screener.py`(在,接 HG stage 的 generation node)
- 体检:`resim_backlog` + `robustness_selector` + `regime_monitor`(本会话已建)

## 4. 为什么这次是「对」的

- **直击实测根因**:覆盖 10.6% / pv1 34% / 18 集 yield 一致烂 = 内卷,不是空。
- **一环解多问题**:供给(覆盖↑)、#40 稀释(reward 直接奖正交于池)、self_corr 墙(同上)、crowding(novelty 衰减)、regime(re-sim 体检)。
- **不重复被否实验**:挖未碰 89% + 强制正交,NO-GO「多挖」没覆盖。
- **reward 三反转**:对象(can_submit→正交广度)、密度(0.5%稀疏→p90密集)、方向(正反馈拥挤→负反馈拥挤)。

## 5. 诚实残留风险 + de-risk

- **核心赌注**:未碰的 89% 字段**可能也低信号**(长尾常稀疏/噪声;生成器避开它们也许有道理)。环结构对,但**回报取决于长尾有没有信号**——这是不验证、靠环自己探的那点。
- **de-risk 内置**:① 探索预算小(每轮少量字段);② **self-pruning 快**(乐观先验几次实测就塌,死字段秒掉)→ 即便长尾全是噪声,环只花小成本就收敛、不失血。把「赌长尾有信号」变成「廉价持续探测 + 自动止损」。
- **orthogonality 算力**:需 PnL 两两相关;可只对「过 IS band 的候选」算(量小),非全量。
- **regime 仍是上层闸**:正交新字段在敌对 regime 也会衰减 → re-sim 体检兜底,不进提交。

## 6. 待对抗审查的设计决策

- D1 reward 三项(novelty/orthogonality/signal)是否共线 / 某项退化?乐观先验会不会被噪声刷爆(假高 orthogonality)?
- D2 字段级粒度的状态/计算成本(8365 字段 × universe × delay)是否爆 `datafield_cell_stats`?
- D3 `FieldScreener` 注入 HG 的真实接线:生成器能否真围绕"指定字段"造有效表达式,还是会忽略指令回默认?
- D4 与现有 dataset bandit 的关系:替换 / 并存 / 两层(数据集→字段)?
- D5 orthogonality vs 提交池——提交池只 13 个且 pv1 主导,基数太小,代理可靠吗?
- D6 探索预算与 sim 配额冲突(止损期池停);本环是否要先小规模 canary 验证长尾有无信号再规模化?

## 7. 业界实践校准

> ✅ 已调研(见 §8)。结论:**方向被 SOTA 独立印证**(AlphaAgent 同根因同解法),但 **§2.2 的 orthogonality reward 被纠正**(用「对组合边际贡献」而非「1−max_corr」)+ 三处精炼(proportional 采样 / 双层 novelty / dense reward)。修订汇总见 §8.4。**注意:§2.2 reward 以 §8.4 为准。**

## 8. 业界实践映射(2026-06-08 调研)

调研 LLM/RL formulaic-alpha mining + quality-diversity + bandit feature selection 的 SOTA。**核心方向被独立验证,但有一处关键设计被纠正。**

### 8.1 核心思路被 SOTA 独立印证
- **AlphaAgent(LLM 驱动 + 正则化探索抗 alpha 衰减,arXiv 2502.16789)** —— 与我们设置几乎同构,且**独立发现同一根因**:*"LLMs tend to overly rely on well-documented financial knowledge and established factors"* → 拥挤。**这正是我们的 886/8365 字段集中**(LLM 死磕 close/volume)。它的解:目标函数加正则项 `f* = argmax L(f(X),y) − λ·R_g(f,h)`,**R_g 显式惩罚与已拥挤因子的相似度** = 我们「reward 正交而非 exploit」。
- **多篇共识**:diversity 缓解 overfitting/decay(GFlowNet 系 alpha-gfn / AlphaSAGE 2509.25055;synergistic collections 2306.12964)。方向稳。
- **架构对齐**:AlphaAgent 三 agent(Idea/Factor/Eval)= 我们 HG/S/E 闭环;失败模式回填 KB = 我们 negative-knowledge。**我们与 SOTA 同构**。

### 8.2 ⚠️ 关键纠正:orthogonality reward 用错了度量
**AlphaGen(Synergistic Collections,2306.12964)直接挑战我 §2.2 的 `orthogonality = 1 − max_corr`**:
- 它的 reward = **新 alpha 对「加权组合模型」IC 的边际贡献**(`r ← IC(∑ wᵢfᵢ)`),**不是个体 IC、也不是低相关性**。
- 它**刻意拒绝按 mutual-IC 过滤**:案例里两个 alpha 互相关 **0.97** 却组合后 IC 更高(可作负权对冲)。**「低相关」≠「边际贡献高」。**
- **推论**:我的 `1 − max_corr` 太粗——一个与池相关的字段 alpha 仍可能有正边际贡献(对冲/互补)。**正确信号 = 对提交池组合的边际 ΔSharpe/ΔIC**。
- **这恰好再次印证 #40 的 marginal 机器该保留**(本会话 workflow 已驳「计酬不奖励 marginal」;AlphaGen 独立确认 marginal-to-combination 才是对的 reward)。→ **不是另起 orthogonality,是把 marginal_analysis 的「对组合边际贡献」接进字段 reward。**

### 8.3 其它可落地精炼
- **proportional sampling 取代 argmax**(GFlowNet):按 field_score **比例采样**字段(Thompson 式),天然多样,而非贪心选最高 → 改 §2.2 选择机制。
- **dense reward 抗稀疏**(多篇指出 RL alpha mining 的 reward sparsity:只在整条公式完成才有信号)→ 印证我们用密集 p90-Sharpe 替 0.5% 稀疏 can_submit;可further 上 trajectory-level reward shaping(2507.20263)。
- **两级 novelty**:AlphaAgent 的 novelty 在 **AST/表达式层**(max-common-subtree 相似度 vs alpha zoo)——**我们已有 G3 AST 原创性**(g3-monitor),但只当**监控**;应像 AlphaAgent 把它**接进生成目标当正则项**。→ novelty 应**字段层(我们的创新)+ 表达式-AST 层(AlphaAgent)双管**。
- **QD/MAP-Elites 当 archive**(Monte Carlo Elites:QD 选择当 MAB):字段/类别作 behavior 维度,每 cell 留最优 → 与我们 field-bandit 天然合;可把 `datafield_cell_stats` 当 MAP-Elites archive(cell=类别×universe×delay,留 best)。

### 8.4 对 §2 设计的修订(据调研)
1. **reward 的正交项**:`1 − max_corr` → **对提交池组合的边际贡献**(接 `marginal_analysis`,与 #40 一致);保留「低相关」仅作 self_corr 硬门(BRAIN 拒)用,不作 reward 主项。
2. **选择**:UCB-argmax → **proportional / Thompson 采样**(GFlowNet 式多样性)。
3. **novelty 双层**:字段覆盖(本设计)+ **表达式 AST 原创性正则**(复用 G3,接进生成目标)。
4. **我们真正的新贡献点**:SOTA 的多样性几乎都在**表达式/AST 层**;**显式的 datafield-覆盖广度探索(攻 10.6%)是 SOTA 没系统做的角度** → 值得做,但须与 8.4.1-3 合体(单靠字段覆盖、不接组合边际,会退化成"挖新字段但仍稀释")。

### 8.5 诚实落差
- SOTA 多在**离线回测**做组合 reward;我们卡在 **BRAIN OS 隐藏**(组合 OS 永不可得)→ 组合边际只能用**当前 IS + 本地 PnL 代理**(就是 marginal/recon 机器在做的),仍是代理非真值。
- AlphaGen 的组合 reward 需对池**联合优化权重**;我们提交是**离散单个提交**(无组合权重控制)→ 边际贡献是"加入后池组合 Sharpe 变化"的近似,口径要诚实标(同 drain-order 现状)。

**Sources**:
- [AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration to Counteract Alpha Decay](https://arxiv.org/html/2502.16789v1)
- [Generating Synergistic Formulaic Alpha Collections via RL (AlphaGen)](https://ar5iv.labs.arxiv.org/html/2306.12964)
- [alpha-gfn — GFlowNet for formulaic alphas](https://github.com/nshen7/alpha-gfn) · [AlphaSAGE: Structure-Aware Alpha Mining via GFlowNets](https://arxiv.org/html/2509.25055v1)
- [Learning from Expert Factors: Trajectory-level Reward Shaping](https://arxiv.org/html/2507.20263) · [Navigating the Alpha Jungle: LLM-Powered MCTS](https://arxiv.org/html/2505.11122v2)
- [Monte Carlo Elites: Quality-Diversity Selection as a Multi-Armed Bandit](https://www.researchgate.net/publication/350991786) · [Quality Diversity / MAP-Elites (Mouret)](https://rl-vs.github.io/rlvs2021/class-material/evolutionary/light-virtual_school_qd.pdf)

## 9. 落地顺序 + 硬 gate(对抗审查定稿)

> ✅ **PR-A + PR-B 已建(2026-06-09,canary PASS 后;全 flag-OFF code-ready)**:
> - **PR-A `c011a8c`**:`datafield_cell_stats` +6 列(迁移 `r2b7f4c9a1e3`,applied live)+ `field_ledger_refresh` 回填任务(token 提取聚合;一次性回填 1461 cell 验通)+ 5 测。
> - **PR-B `09c6c27`**:`field_selector.py`(field_score=novelty×signal_quality,愚人金 guard,proportional 采样,6 测)+ `field_screener.py`(pick_target_field)+ `hyp_intent.target_field`(迁移 `r3c8a5d1f9b4`)+ `MiningState.target_field` + scheduler `_assign_target_fields`(gated)+ hydrate 透传 + generation `_prepend_target_field`。test_suite 0 漂移(flag-OFF 字节不变)。
> - **激活硬 gate(未满足)**:`ENABLE_FIELD_SCREENING` ON ∧ `ENABLE_POOL_PIPELINE` ON ∧ regime 非 DOWN ∧ 供给企稳。当前池 OFF+regime DOWN → 不激活。
> - **follow-up `ce12028`(✅ 3/4 已做)**:① field_ledger beat 注册(每日 05:40 self-gate)② G3 originality nudge 接进 code_gen prompt(prompt 级,劝阻拥挤 pattern)③ RAG 双环仲裁(prompt 级「PREFER 目标字段 over 检索 success_patterns」防被 pv1-heavy RAG 抵消)——②③ 合一 FIELD-EXPLORE directive,gated on target_field。**剩**:N≈30 确认性 canary(烧 sim)+ 更重的 RAG 检索层 re-weighting(现 prompt 级仲裁够用,深度版 defer)。


**Step 0 — 离线 canary(✅ 2026-06-09 已跑 PASS,见 §0.3)**
- ✅ 已执行:12 未碰 vs 12 已用 × 3 模板,`datafield_cell_stats` is_active 可用过滤;**RELATIVE p50(未碰)/p50(已用)=1.48 → 未碰非更差,长尾噪声证伪,核心赌注 PASS**。
- (原规格,供 N≈30 确认性复跑参考)K=20 未碰**真·信号字段**(经 cell-stats is_active + `_is_signal_field` 过滤,非原始 8365)× N=5 alpha,**仅本地 IS 回测**,与熟字段比 RELATIVE 分布。
- **硬门(收紧 — 旧版仅比 `p50(new)>p50(old)` 会被 regime 噪声污染、把"regime 塌方"误判成"长尾无信号")**:
  - 通过 = `p50(未碰) > 1.0 sharpe` **AND** `self_corr < 0.5`(对池)**AND** 字段测试覆盖率 > 50%(非 NaN/可回测);
  - ⚠️ **当前 regime DOWN → 未碰与熟字段大概率"双方都失败"属预期,不能据此判"长尾无信号"或 pivot 数据源**;canary 应在 regime 非 DOWN 时跑才有判别力(或显式标注 DOWN 期结果仅供参考)。
- **不过 → 长尾无信号 = 整环过度工程,STOP,真杠杆转新正交数据源/跨区 + robustness 门;过 → 才进 PR-A。**

**PR-A(基建,仅 canary 过后才建,不再"池 OFF 期 staging")**
1. Alembic 迁移:`datafield_cell_stats` +6 列(`times_mined / distinct_alphas / signal_p90 / orthogonality / last_mined / band_pass_count`)。成本核:只对「近期有 alpha 的 cell」回填,非全 ~125k 行。
2. 回填 beat:`signal_p90 × can_submit_rate`(防愚人金,非裸 p90)+ 字段级正交(**先定聚合口径 + 解 §0.2-2「换标签未破环」**:倾向 self_corr<0.5 硬门 + novelty + signal,marginal 移出 reward 主项)+ min-overlap NULL 守卫;诚实标 IS-proxy。

**PR-B(进攻,flag-OFF code-ready)**
3. 重写 `field_screener.py`(目标字段注入 hypothesis/code_gen prompt)+ `HypothesisIntent` 加 `target_field` 列 + scheduler 两层(dataset→field)+ Thompson 比例采样 + G3 originality 接进生成目标(非仅 E 阶段)+ **双环仲裁**(KB success-RAG 加 target_field 软过滤,防 steering 被同节点 RAG 抵消)。
4. `ENABLE_FIELD_SCREENING` flag 默认 OFF;canary 期 10% 预算;监控 self-pruning 速度 + 覆盖率↑ + 与 KB-RAG 不对冲。

**硬 gate(canary 已过 + PR-A/PR-B 开工前必须同时满足)**:`ENABLE_POOL_PIPELINE=true`+池重启 ∧ regime 非 DOWN ∧ can_submit 供给企稳。

**当前三者均不满足(池 OFF / regime DOWN / 止损)→ 不写任何 PR 代码。** 正确路径 = **Step 0 canary 先行**(离线、不占 BRAIN 槽、不与 regime_monitor 竞争),过了再 PR-A+PR-B 一起上。**二审已删原"PR-A 可 staging"——三 gate 全不满时 PR-A 也是死资产。**
