# 设计稿:正交广度字段探索环(Orthogonal-Breadth Field Exploration Loop)2026-06-08

> 状态:**草案 — 对抗审查 `wzvcwsm0t`(47→34 确认)已过。判定 = 方向成立 / 文档误标已纠 / 主体 DEFER。先读 §0。**

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

**PR-A(基建,池 OFF 期可 staging,不阻塞止损主线)**
1. Alembic 迁移:`datafield_cell_stats` +6 列(`times_mined / distinct_alphas / signal_p90 / orthogonality / last_mined / band_pass_count`)。成本核:只对「近期有 alpha 的 cell」回填,非全 ~125k 行。
2. 回填 beat:从 `alphas`/`alpha_pnl` 算 `signal_p90 × can_submit_rate`(防愚人金)+ 用 `analyze_marginal_contribution` 算字段级边际(**先定聚合口径**)+ min-overlap NULL 守卫;诚实标 IS-proxy(OS 隐藏)。

**PR-B(进攻,flag-OFF code-ready,等条件)**
3. **canary 先行(硬前置)**:K=20 未碰字段 × N=5 alpha,**仅本地 IS 回测**,比 p50(未碰) vs p50(886 熟字段)。**不过 → 长尾无信号,STOP,转新数据源/跨区,别投基建。**
4. canary 过 → 重写 `field_screener.py`(目标字段注入 hypothesis/code_gen prompt)+ `HypothesisIntent` 加 `target_field` 列 + scheduler 两层(dataset→field,避双环冲突)+ Thompson 比例采样 + G3 originality 接进生成目标(非仅 E 阶段)。
5. `ENABLE_FIELD_SCREENING` flag 默认 OFF;canary 期 10% 预算;监控 self-pruning 速度 + 覆盖率↑ + 与 KB-RAG 不对冲。

**硬 gate(任何 PR-B 开工前必须同时满足)**:
- `ENABLE_POOL_PIPELINE=true` + 池重启(字段探索需 mining ON + sim 预算);
- regime 非 DOWN(`regime_monitor` 不报持续 DOWN);
- can_submit 供给企稳。

**当前三者均不满足(池 OFF / regime DOWN / 止损)→ PR-B 此刻不能开工。** PR-A 可作前向资产 staging,但若不确定要推进,**最省的是先只跑离线 canary(步骤 3,零基建)** 验长尾信号——这是把「赌长尾有信号」变成廉价一锤的决定性测试(与本会话用户「不验证、先设计」的取舍相反,但审查认为投基建前这一锤值得)。
