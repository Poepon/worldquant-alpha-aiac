# 池原生反馈环 Reward 重设计 (Pool Phase 2 · #31)

> 2026-06-07 · 起因:submit-yield 自 05-20 塌方调查(#25/#25b/#25c)揭出**反馈环 reward 在当前 regime 结构性失效**。用户要求"弃 FLAT/ONESHOT 假设,池原生重设计 reward"。
> 状态:**设计稿,未实施**。实施 gated on #25c 字段卫生上线后的 soak(确认信号是否复活 + 实测分布)。

## 0. TL;DR
当前 dataset bandit 的 reward = **binary can_submit**,在 submit-yield≈0 的 regime 下**死了**:无正例 → 无法区分数据集 → exploit 项归零 → exploration floor 接管 → 反向导流到未试过的垃圾(univ1)。且**朴素稠密 sharpe reward 会误排**:univ1 退化式 sh≈0 排在 pv1 真实 sh=−0.43 之上(退化-平 > 真-负)。

**核心重设计**:reward 改挂 **信号幅度 |sharpe|(可符号翻转)+ 退化门(零方差=0)**,而非 submit-yield(死)或带符号 sharpe(误排)。
- 活在 yield≈0:真数据集 |sharpe| 非 0,退化 univ1 |sharpe|≈0 → 天然区分。
- degeneracy-aware:零方差/常数/退化式 → reward=0(真底),低于"真-负但可翻转"的 alpha。
- can_submit 降为**奖励 bonus / tiebreak**,不再是主信号(这就是"弃 FLAT/ONESHOT 假设")。

## 1. 为什么旧 reward 不适用(本次调查实证)
| 失效点 | 证据 |
|---|---|
| binary can_submit 饿死 | pv1 跑 995 sim,can_submit 成功 alpha≈0;全数据集 bandit α≈0 → 无区分力 |
| exploration floor 反向导流 | univ1(3 pulls)mining_weight=0.063 顶端,fundamental6(187 pulls)=0.0046 底;52% 产能挖 univ1 垃圾 |
| 朴素 sharpe 误排 | univ1 退化 sh≈0 > pv1 真 sh=−0.43;若按带符号 sharpe reward,会更偏向退化垃圾 |
| flag 与读分离 | `ENABLE_DATASET_VALUE_BANDIT=False` 但 mining_weight 仍被无条件读去选 cell → 陈旧权重持续危害 |

**前提验证**:sign-flip retry(`2d9d5f9`)确实把 sh=−2.0 翻成 +2.0(实测 ±2.0 孪生),∴**|sharpe| 是合法的"可用信号强度"steering 目标**——一个数据集产强信号(无论符号)就有价值,产退化-平才无价值。

## 2. 设计原则(池原生)
1. **稠密 + 跨 regime 存活**:reward 不能挂会归零的信号(can_submit/submit-yield)。挂 |IS sharpe|(连续、真数据集恒非 0)。
2. **degeneracy-aware**:零方差/常数/退化结构 → reward=0 + 去优先(比真-负更差,因为是浪费产能)。排序铁律:**真-正 > |真-负|(可翻转)> 退化-零**。
3. **多层学习**:dataset 层(现有 bandit)+ field 层(学哪些字段产强信号)。field 层是新的。
4. **复用现有数学**:`discounted_thompson_update` 已接受 fractional `s_d` ∈[0,1] → 喂 graded reward 零数学改动。
5. **不重蹈 LB4**:can_submit 当稀疏 bonus(有正例时加成),绝不当主信号。

## 3. 核心机制:degeneracy-aware 信号幅度 reward

### 3.1 per-alpha graded reward r ∈ [0,1] —— **v2(经对抗审查 §7 修订)**
```
# 不可救门(fool's-gold guard,§7 critic#1 HIGH):用 BRAIN 失败"类型"而非 binary can_submit
if _brain_failed_checks 含 CONCENTRATED_WEIGHT 或 LOW_SUB_UNIVERSE_SHARPE:
    r = 0.0          # 稀疏字段集中=高 |sharpe| 假金(model16 陷阱),结构不可救
else:
    r = clip(|is_sharpe| / SHARPE_SCALE, 0, 1)    # 信号幅度(sign-flip 可救)
    if can_submit is True:  r = min(1.0, r + CAN_SUBMIT_BONUS)   # 仅当 soak 证 can_submit 回来才用
```
- **⚠️ 删除原 zero-variance degeneracy 门**(§7 critic#2 REFUTED):实测 univ1 alpha **非零方差**(100M–3B var,只是低 sharpe);零方差指纹 0/47 命中真退化、却假阳压制真实低-sharpe alpha。**univ1 由 |sharpe| 自然沉底**(低幅度→低 r),不需专门门。
- **fool's-gold 门是真正需要的门**(§7 critic#1):实测 153 个 |sharpe|≥1.5 且 fitness/turnover 都好的 alpha 因 **CONCENTRATED_WEIGHT** 不可提交(alpha 7797 |sh|=14.14 全好却 CW FAIL)→ 裸 |sharpe| reward 会把它们当真信号、导流回稀疏字段垃圾。门用 `_brain_failed_checks`(刷新后的真值,非 stale `metrics['checks']`)。
- CW/sub-universe = **不可救**(结构性,r=0);LOW_FITNESS/HIGH_TURNOVER/LOW_SHARPE = **可救**(sign-flip/参数),留在 |sharpe| 信号空间。
- 未刷新 BRAIN 状态的 alpha:censored(不计),不盲奖(承 Track D censored-not-negative)。

### 3.2 喂进 dataset bandit
- `s_d` = Σ(per-alpha graded r),`t_d` = #(real sim)(不变)。`discounted_thompson_update(α,β,s_d,t_d,γ)` 原样用(已支持 fractional)。
- 效果:pv1(|sharpe|~0.4-2.4)s_d 高 → α 升 → mining_weight 升;univ1(退化 r=0)s_d=0 → 沉底。**反向导流被纠正。**
- **打开 `ENABLE_DATASET_VALUE_BANDIT`**(Track D censoring 已修)+ 修"flag gate 写不 gate 读"的危害:flag OFF 时 mining_weight 应回退中性(1.0)而非用陈旧值(本次 A 止血已手动重置一次,需代码化)。

### 3.3 field 层 reward(新,Phase 2-of-this)
- 现 bandit 只到 dataset 粒度。但同一数据集内字段质量差异大(fundamental6 的 fscore_* vs 其它)。
- 设计:per-field 累积"该字段参与的 alpha 的 graded r 均值"(字段从表达式抽取,复用 `extract_field_set`)。低分字段降权/排除(喂 `_get_dataset_fields` 排序或一个 field-level 软禁用)。
- **gated**:先 dataset 层验证有效,再上 field 层(避免一次引入两个未验证的学习信号)。

## 4. 开放问题(soak 数据决定)
1. **信号是否复活**:#25c 上线后若 mean sharpe 回正/can_submit 回来 → can_submit bonus 有用;若仍负 → |sharpe| 是唯一 workhorse。soak 实测再定 SHARPE_SCALE / bonus。
2. **degenerate 方差探针的数据源**:IS PnL 方差要不要落库(alpha_pnl 已有)?还是用 is_sharpe≈0∧is_turnover≈0 的廉价指纹代理?
3. **SHARPE_SCALE 标定**:从 soak 的 |sharpe| 分布 p50/p90 标定(类比 marginal_scales)。
4. **field 层 attribution**:表达式→字段是多对一,信号归因到单字段有噪声(同 R2 的 count-thresholded 纪律)。
5. **是否保留 can_submit 作 PROMOTE 闸**(认知层 1c)同时 reward 用 |sharpe|(steering):两个信号各司其职(PROMOTE=经济门用 can_submit;steering=产能用 |sharpe|)。倾向:是。

## 5. 与现有架构的衔接
- 复用:`discounted_thompson_update`/`thompson_sample_weight`/`weighted_choice`(数学不变)、`bandit_state`(Beta 后验)、`dataset_cell_stats.mining_weight`(选择权重)、Track D `_submit_yield_label`(degenerate 组件)、`extract_field_set`(field 归因)。
- 改:`dataset_weight_refresh._classify` 从 binary→graded degeneracy-aware r;打开 flag;flag-OFF 回退中性权重(代码化 A 止血)。
- 新:degenerate 探针 + field-level reward 累积表(Phase 2-of-this)。
- **不碰**:认知层 1c 的 PROMOTE-on-can_submit(经济门,正确);#25c 字段卫生(上游,正交)。

## 6. 实施顺序(soak-gated,经 §7 修订)
**Phase 0(硬前置,进行中)**:#25c soak 48–72h(workflow `bz6mu31as`)。测 per-dataset mean(is_sharpe)/can_submit 率/mining_weight + |sharpe| 分布。**决策门**:
- 若 yield 回到 **≥1%** → **#25c 字段卫生已够,reward 重设计 PREMATURE,封存本稿**(只留 A 止血的中性权重代码化 + 打开 bandit censoring)。
- 若 yield 仍 **<0.5%** → 进 Phase 1(仅 dataset 层)。
**Phase 1(仅在 soak 失败时)**:§3.1 v2 graded reward(含 CW 不可救门、**无**零方差门)+ 从 soak 标定 SHARPE_SCALE(=p90(|sh| | can_submit)) / CAN_SUBMIT_BONUS(can_submit 回升 >5% 才 >0)+ 打开 flag + flag-OFF 中性回退代码化 + 单测。**14d gate**:mining_weight 与 dataset 退化率负相关、univ/CW-type 沉底、throughput 不降、CW-假金率不升。
**Phase 2b(仅在 Phase 1 验证后)**:field 层,带强制护栏(min_uses≥5 / 多对一规范化 `r/字段数` / uses<10 压地板)。
**监控**:per-dataset (|sharpe|, can_submit, mining_weight, CW-fail 率) 四联。

---
## 7. 对抗审查裁决(workflow `w8fg607qx`,4-critic,2026-06-07)
**总裁决:核心 |sharpe| 思路有价值但"as-written 不安全",改 + soak-gate 后存活。** 4 个 HIGH/CRITICAL 全部成立、已并入上文:

| # | 决策 | 裁决 | 修订 |
|---|---|---|---|
| 1 | reward=\|sharpe\| | **FLAWED(HIGH)** | 实测 153 个 \|sh\|≥1.5+fitness/turnover 都好却因 **CONCENTRATED_WEIGHT** 不可提交(假金,model16 陷阱)。裸 \|sharpe\| 会导流回稀疏字段垃圾 → **加 CW/sub-universe 不可救门 r=0**(用 `_brain_failed_checks` 真值);LOW_FITNESS 等可救留信号空间。核心存活。 |
| 2 | zero-variance degeneracy 门 | **REFUTED(HIGH)** | univ1 非零方差(100M–3B var,只是低 sharpe);指纹 0/47 命中、假阳压真实低-sharpe → **删除整门**,univ1 由 \|sharpe\| 自然沉底。 |
| 3 | field 层 reward | **FLAWED(HIGH)** | 多对一归因噪声 + 当前 ~0 正例无信号可学 → **推迟 Phase 2b**,加 min_uses≥5 + `r/字段数` 规范化 + uses<10 压地板。 |
| 4 | 前置=#25c soak | **未满足(CRITICAL)** | 无 post-#25c 数据 → 无法判 reward 重设计是否需要(可能 #25c 单独就够)。**整稿 gated on soak**:yield 回 ≥1% 则封存,仍 <0.5% 才实施。 |

**教训(承 [[feedback_adversarial_review_before_plan]]、[[feedback_verify_root_cause_before_fix]])**:① 别用"质量"(\|sharpe\|)代理"价值"——CW 假金高 sharpe 低价值,必须用 BRAIN 失败类型而非裸幅度;② 低 sharpe ≠ 退化(univ1 有方差),零方差门是错靶;③ 大重设计实施前先 soak 数据隔离根因,别在 #25c 可能已解决的问题上 gold-plate。

---
## 8. REGIME 漂移实证 → 强化本设计(2026-06-07 只读诊断)
**决定性诊断(只读 BRAIN 重算老赢家于当前数据):**
- 真·提交赢家 `mLxlen69`(`ts_decay_linear(-ts_rank(returns,5),10)`,曾 sh=2.01 已提交)→ 重算 **sharpe −0.74**(符号反转!短期反转信号经典 regime 翻向)。
- 老 alpha `Xgkr0O6l`(close-low-high range)sh 2.43→0.87(衰减,且本就 turnover 0.804 不可提交)。
- 2/2 老赢家垮(一衰减一反转)+ fresh fundamental6 也弱 → **submit-yield 塌方是两层:05-20 FLAT 字段卫生(#25c 已修)+ 数周 REGIME 漂移(老边际衰减/反转)**。

**对 reward 设计的影响(强化、非推翻 §3 v2):**
1. **#31 不再是"可能过早"——regime 漂移使它更必要**:binary can_submit 在新 regime 下"双重死"(yield≈0 + 老结构失效);必须有能**自动淘汰失效数据集/结构、导向当前在产**的 reward。
2. **discounted Beta-Bernoulli(γ 遗忘,`discounted_thompson_update` 已实现)+ 当前 \|sharpe\| 正是非平稳工具**:γ 让重挖的失效臂快速遗忘、当前 \|sharpe\| 反映当下而非历史 edge。**这恰是为 regime 漂移设计的**——本设计方向被诊断证实。
3. **但 reward 只在"已存在的"里选 —— 变不出新边际**。regime 漂移下,**breadth/exploration(新正交数据源 + 新结构)是必备配套**:reward 导流到当下有效的,exploration 扩大"有效集"。**#31 ≠ 全部;需配 breadth。**
4. **yield 决策门重定**:老风格生成在新 regime 大概率持续 <0.5% → 不应"yield 没回就封存 #31",而是 **#25c(已)+ #31(steer 当前)+ breadth(找新)三管齐下**。soak 验的是"#25c 后 \|sharpe\| 分布"(标定 SHARPE_SCALE),非"yield 是否自愈"(regime 下不会)。
5. **不追"恢复 ONESHOT +1.5"**:ONESHOT 跑在更友好 regime,老结构已死,那个基线不是目标。目标=当前 regime 下的可提交正交 alpha。

**净结论:诊断把"是不是工程能修"钉死——不全是,是市场变了。#25c 止自伤;真产能恢复靠 reward(导流当下)+ breadth(找新边际),且 reward 的 discounted-|sharpe| 方向正确。**
