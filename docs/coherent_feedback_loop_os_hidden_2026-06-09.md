# OS 隐藏下的单一连贯反馈环 — 设计定稿(架构决策版)2026-06-09

> 来源:workflow `wjyvgmfem`(ultracode)—— 5 第一性原理镜头各出一版 → 2 评委×6 准则评分 → top-2 对抗证伪 → 综合。winner = "UNIFIED COHERENT FEEDBACK LOOP"(graft QD 档案/marginal-recon 升级/诚实代理标注),**强制纳入 top-2 致命修法**(正交从 reward 被错误移到下游 gate → 还原进 reward + 可信度地平线)。
> 适用:WorldQuant BRAIN,USER/USA/当前 regime。承 `orthogonal_breadth_field_loop_design_2026-06-08.md`(现状 4 环 + PR-A/B 落地态)。

---

## ① 核心原则:OS 不可观测如何决定一切

三条硬事实(第一性约束,所有部件从它派生):
1. **OS 永不可作输入** —— BRAIN simulate 只返 IS;realized OS 提交后盲测、恒 null、`/check` 不触发(13/13 os_metrics PENDING 实证)。**任何声称"优化 OS"的 reward 都是幻觉,只能优化 IS 代理并诚实标注。**
2. **唯一 OS 微弱信号** = 提交后 before-and-after 组合边际(BRAIN 权威,但也是回测-merge 估计非 live realized,~13 对稀疏)+ 当前数据 re-sim(regime 衰减探针)。
3. **代理→OS 传递关系本身不可验证** —— "IS 鲁棒 + 字段广度 + regime 探针 ≈ OS 健壮"是 empirical hypothesis,非定理。**整环须 fail-closed + 优雅降级 + 把"赌注可能败"写进设计而非藏起来。**

三戒律(贯穿):**A 密集优先**(reward 用 p90-Sharpe×can_submit_rate 非稀疏 binary)/ **B 诚实标注**(所有信号标 "IS-proxy, OS-blind")/ **C 内生 kill-switch**(脱钩检测是一等部件,非人工巡检)。

---

## ② 最终【单一连贯环】设计

### 2.0 拓扑(一条路径,四个信号,三道闸)

```
   Field Ledger (datafield_cell_stats +6列: times_mined/signal_p90/orthogonality/
                 last_mined/band_pass_count/distinct_α)  ← 环的唯一长期记忆
        │ 回填(daily beat) + 实时(E阶段)
   Scheduler: field_score = novelty × signal_quality(内含可信度加权 orthogonality)
              → Thompson 比例采样(非 argmax) → target_field          ← 单一 reward
        │ hyp_intent.target_field
   HG: FieldScreener 强制围绕 target_field 生成 + code_gen 软约束 self_corr<0.5
       + G3 AST 原创性 nudge
        ↓
   S: BRAIN simulate(当前数据)
        ↓
   E: 闸1 robustness(子周期一致) → 闸2 self_corr<0.7(硬) → 闸3 margin≥5bps
      → 回填 Ledger → PROVISIONAL/PASS → 提交队列(人审)
        │
   内生 regime + kill-switch(beat,非热路径):
     • regime_monitor(6h): re-sim 提交池+10%backlog → TURNING/DOWN → 调 exploration_budget
     • marginal_recon(daily): 离线ΔSharpe符号 vs BRAIN边际 → 脱钩检测 → 调 orthogonality权重
```

### 2.1 状态(唯一长期记忆 = Field Ledger)
`datafield_cell_stats`(已 live)+ 6 列(PR-A `c011a8c` live)。概念上是 **MAP-Elites 档案**(行为维度=dataset×字段语义簇×正交 zone,每 cell 留最优),**但落地不引入 K-means 簇层**(避免 QD 版被证伪的"簇 K 失配"),簇退化为"字段本身"作 cell 坐标。`target_field` 沿 HG→S→E 传播(`HypothesisIntent.target_field` 迁移 `r3c8a5d1f9b4` live)。regime 状态 = Redis `regime:verdict`。kill-switch 状态 = feature_flag(可热翻)。

### 2.2 reward:可观测代理 + 防自欺(★ 已纳入 top-2 致命修法 ★)

```
field_score(f) = novelty(f) × signal_quality(f)
  novelty(f)        = 1/√(times_mined[f]+1)                 # UCB,未碰字段高分,自止损
  signal_quality(f) = p90_sharpe(f) × can_submit_rate(f)    # 密集(戒律A),防愚人金
                      × orthogonality_credible(f)            # ★ 致命修法:还原进 reward ★
```

**★ 致命修法(对抗证伪的核心)**:winner/QD 版实现中**把 orthogonality 从 reward 移到下游 self_corr 硬门**(正是 PR-B 按 §0.2 做的),导致 `novelty×signal_quality` 数学上**无法保证去拥挤**——只解决"覆盖率"(访问未碰字段),不解决"产出 alpha 是否正交"。反例:未碰字段 F 与 pv1 用不同数据源定义同一潜因子(流动性/波动率),F 的 alpha IS 高(→被挖)但 self_corr≈0.92(→事后批量拒)→ novelty 在 times_mined<K 前持续选 F → **预算失血,环"感觉在探索正交"实则只发现已知潜因子空间的相关结构**;kill-switch 也救不了(小池上符号信号太稀疏难触发)。

**修法三件(全纳入)**:
1. **orthogonality 还原进 field-selection reward**(不只下游 gate):`orthogonality(f)` = 该字段已评估 alpha 对当前提交池的边际 ΔSharpe 均值(`marginal_analysis`)**或** `1−mean_self_corr(field_alphas, pool)`;在 `field_ledger_refresh` beat 批量预计算(复用 `correlation_service.get_with_fallback()`)落 `orthogonality` 列。**字段选择时正交就是活反馈而非事后否决** → 高 p90 但结构相关的未碰字段拿低 field_score 被降权,闭环关上。
2. **可信度地平线**(防小样本正交噪声误杀新字段):`distinct_alphas<K_orth(3~5)` → 用乐观先验 1.0(新字段先探不预罚);`≥K_orth` → 用实测值。+ `min_overlap` 守卫(字段 alpha 与池 overlap<0.5 才算有效 marginal)。提交池仅 ~13、pv1 占 34% → 单异常提交会绑架计算,必须此守卫。
3. **orthogonality 与 novelty 解耦各自衰减**:novelty 只罚过度挖掘;orthogonality 在 beat 持续重算(池结构变/regime 翻转 → 某未碰字段变相关 → 立即降权,不等 times_mined 长大)。

**其余防自欺**:守卫栈(只对过 IS band∩robustness∩self_corr<0.7 的强候选投 reward,弱候选不污染 ledger)/ 乐观先验 + 快速自止损(连 K=3~5 失败 → novelty×1/(1+failures),1 周证伪噪声字段,每字段损失上限 ~15 sims)/ reward 三维上线前离线验互信息(共线则自适应降权,沿用 marginal_analysis 共线坑教训)。

### 2.3 单一 explore-exploit(替代 4 套独立环)
**字段粒度 Thompson 比例采样**(GFlowNet 风格,非 argmax):novelty 项=explore(未碰高分)、signal×orthogonality=exploit(已知好字段),**同一数学,无需两套**;字段被挖→times_mined↑→novelty↓→下轮概率↓→**自动漂向新字段(负反馈,0 配置)**,与现状"can_submit 正反馈→越拥挤越挖"方向相反。**方差护栏**:min-max 归一 + baseline `α·score+(1−α)·0.5` + temperature(约束期收敛 top-K)+ 硬约束(每轮≥3 字段、≥1 未碰)。

### 2.4 内生 regime(不外挂)
regime 是 re-sim 探针感知的状态,**自动回调 exploration_budget + orthogonality 权重**,非手翻旋钮。`regime_monitor`(6h):`TURNING`(回复≥50% 无实质衰减)→ 解锁探索;`DOWN`(回复<50% 或 mean_delta<−0.25)→ `exploration_budget→0`,只跑 re-sim refresher(监测态省 sim);`INSUFFICIENT` → 维持。**stale-cache 守卫**(|Δ|<1e-3 标 stale 剔除,防 BRAIN 缓存冒充恢复)。非平稳定量:signal_p90 每日 fresh 重算 / per-cell 衰减率软降 reward / last_mined 季节性 re-try。**关键**:regime DOWN 时全池 reward 同比降 → 环**自动退缩,无需独立 kill-switch**(信号在,只是绝对值烂)。

### 2.5 内建抗拥挤/正交(三层嵌套)
① 字段层 novelty(reward 内,未碰 89%/~7181 字段优先,目标 pv1 集中度 34%→<20%)② alpha 层 orthogonality(**reward 内,致命修法已还原**;用真组合边际非 1−max_corr 粗口径,因 AlphaGen 证"低相关≠高边际")③ 表达式层 G3 原创性(prompt 软正则,AST<0.8 vs zoo,`ce12028` 已接)。**硬门**:self_corr≥0.7 → 降 backlog 不丢弃(用 `GET /alphas/{id}/check` 非异步永挂的 `/correlations/SELF`)。**kill-switch 触发时 gate field-selection**(致命修法第 3 件):停 `ENABLE_FIELD_SCREENING` 退 robustness-only + self_corr<0.5 烤进 code_gen prompt + 违约拒 alpha + 字段本轮临时排除 → 堵死"反复锤打结构上无法产正交 alpha 的字段"失血路径。

### 2.6 proxy 谦卑/kill-switch(四层 + 人类终审)
**marginal-recon 升为一等公民 kill-switch**(非藏在 composite 里靠 stale 本地池):
- **KS1 Marginal Reconciliation(daily)**:离线 ΔSharpe 符号 vs BRAIN before-after,sign-agreement≤60% over≥15 对 → FALSIFIED:orthogonality 权重→0 + **停 `ENABLE_FIELD_SCREENING`** + 退 robustness-only + 人审 + 冻 24h。
- **KS2 Regime Divergence**:verdict 7 天翻转>1 次 → 节流 ×0.5 + 告警。
- **KS3 Field Self-Collapse**:单字段 marginal 连 3 负 → dormant 30 天;>30% 活跃字段 dormant → COHORT_FAILURE 人工门。
- **KS4 Regime Critical Down**:re-sim batch≥60% 衰减 → flag OFF + 退保守 + 通知,72h 后自动解冻。
**优雅降级**:任一触发 → 退纯 breadth(novelty only)+ 告警,挖掘继续(降级非硬失败)。**人类终审**:fail-closed G0-G10 绝不 auto-submit;PROVISIONAL→人审。**诚实代理面板** `/ops/field-screening-monitor`:per-cell score/tried/failed/orthogonality/衰减率 + 红绿灯 + 覆盖率%,让人肉眼确认多样性真在做、kill-switch 何时为何改行为。

---

## ③ 取代现状 4 环(部件映射)

| 现状独立环 | 病灶 | → 统一环部件 | 替代逻辑 |
|---|---|---|---|
| dataset bandit(can_submit→mining_weight)| 粒度粗(18 集)+ 稀疏 reward → 正反馈拥挤 | §2.3 Scheduler 比例采样(读 field_score)| 字段粒度 supersede 数据集;密集 reward;比例采样替贪心 |
| KB-RAG exploit(回喂 pv1 pattern)| 强化拥挤 + 与导流冲突 | §2.5 表达式层 + context bias | RAG 降为 context-only;"PREFER target_field over patterns";Phase 2 检索层 re-weight |
| field bandit(曾设想独立)| 与上两环对冲 | §2.2 reward 的 novelty 项 | 收编为统一 reward 一因子 |
| regime monitor(外挂只告警)| 不回调挖掘 | §2.4 内生 regime + §2.6 KS | verdict 直接调 exploration_budget,从告警变质检层+节流闸 |

**统一不变量**:一个 reward(field_score)、一个选择器(Thompson)、一个 regime 闸、一个对账锚(marginal_recon)。无冲突 bandit、无 RAG 与导流互掐。

---

## ④ 迁移路径(分阶段,尊重 flag-OFF 现状)

- **Phase 0 基建(✅ 已完成)**:6 列 / target_field / field_ledger beat / regime_monitor / resim / robustness / marginal_recon 均 live。
- **Phase 1 攻击代码(~3 周 2 PR,含致命修法)**:
  - **PR-C**:`field_selector.signal_quality` 乘入 `orthogonality_credible`(可信度地平线+min_overlap);`field_ledger_refresh` 重算 orthogonality 列;`marginal_recon` 触发改停 `ENABLE_FIELD_SCREENING`;reward 三维互信息验证。
  - **PR-D**:`field_screener.py` 完整重写;code_gen 烤入 self_corr<0.5 软约束(违约拒+临时排除)+ 自止损;Scheduler 比例采样护栏。
- **Phase 2 上线 gate(强化 canary)**:原 canary(N=25,p=0.82)只证"未碰 IS≈已用",**不够**——必须扩证 **"未碰字段产出 alpha self_corr<0.5 vs pv1 池比率≥50%"**;若未碰 self_corr≈已用 → 核心前提败(只换拥挤位置)→ NO-GO。canary 须 regime≠DOWN 跑。达标→ 90 天 flag OFF + 5% 白名单 canary → 达成率>60%∧衰减<30% → roll。
- **Phase 3 深度修(defer)**:RAG 检索层 per-field re-weighting(prompt 级被溺没才升级);orthogonality 算力优化。
- 工程量:~500 行 + 0 新迁移 + 无新基建/LLM/schema。

---

## ⑤ 诚实残留限(物理边界,即便完美也打不破)

1. **OS 隐藏:代理→OS 传递不可验证(根本不确定,无解)**。最坏情景:regime 翻转,未碰字段 rolling-IS 好看但提交后 OS 负 → recon 检测前已生成数百个。缓解非消除:自止损快/regime DOWN hibernate/recon 退 robustness-only/canary 认识论戳记。**诚实回报承诺:上线后真·提交失败率>50% → 报"分布鲁棒假设破产" → NO-GO,不护盘。**
2. **USER/USA 盒(机器换不了)**:只优化"盒内挑哪些挖/提交",不假装扩盒。field_score 按 BRAIN role config-frozen(USER 硬化,pool 启动快照);Consultant 解锁须停 pool→刷 config→重启。
3. **regime 天花板(不可控)**:彻底陷阱 → 所有 reward→0 → 环退监测态。无生产杠杆能在不利 regime 凭空造可提交 alpha。
4. **代理自欺剩余风险(已缓解未根除)**:未碰 89% 或真噪声(canary N=25 仍小,N=100 可能翻案→备 NO-GO)/ marginal 用冻结池非当前 / RAG 冲突非结构解 / 池小正交噪声 / Thompson 方差 / Ledger 双写竞态。
5. **启动依赖(当前死资产)**:三 gate(pool ON∧regime≠DOWN∧supply 企稳)全不满 → 不启动。fail-closed 设计的代价,非缺陷。

---

**一句话**:把现状 4 套互相对冲的环收敛为 **一条路径、一个 reward(novelty × signal × 可信度加权 orthogonality)、一个比例采样选择器、一个内生 regime 闸、一个 marginal 对账锚**,直击"字段覆盖 10.x%/pv1 34% 拥挤"实测根因;**关键修复是把正交还原进 reward(而非藏下游硬门)**,否则只解决覆盖不解决拥挤。但诚实承认:OS 永不可观测、盒不可换、regime 不可控下,**它优化 IS 代理而非 OS**——赌注可能不传递,故全环 fail-closed、优雅降级、kill-switch 内生、canary 戳记认识论、赌注败时承诺 NO-GO 而非护盘。
