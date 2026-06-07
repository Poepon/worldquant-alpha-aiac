# 下一步开发计划 — post-#25c-soak + REGIME 漂移 (2026-06-07)

> 起源:用户「规划下一步开发计划」/「调整开发计划,落实到文档」。
> 方法:草稿 → workflow `w4lpfssg7`(6-agent 对抗审查:5 个载重决策逐个证伪 + 1 完整性批判,全部核真实代码/live PG)→ 并入裁决 → 本稿。
> 状态:**[SUPERSEDED 2026-06-07] 被 `dev_plan_greenfield_2026-06-07.md` 取代。** 本稿是"如何运维现有机器(分支 B)"视角;用户改用绿地目标导向视角(目标=产出可提交 alpha + consultant 收益)→ 盒内机器降为冻结,优先级重排为「钉死目标函数 + 抗过拟合稳健选择器 + 换盒子」。本稿的**事实结论仍有效**(yield=0 真天花板 / breadth USER-mode 穷尽 / OS 架构隐藏),保留备查。原:用户已选分支 B(Consultant 不可用)。

---

## 0. TL;DR(经对抗审查修正,与草稿有实质出入)

1. **yield=0 是真 REGIME 供给天花板 —— 不是选择墙、不是度量 artifact、不是阈值问题**(D2 CRITICAL 实证)。fresh 686 alpha(36h):signed sharpe p50=**−0.04** / p90=0.40 / p95=**0.55**,远够不到 1.25 提交门。过 sharpe 门的 7/686 里仅 1 个过全部 BRAIN is.checks,还是 model16 CONCENTRATED_WEIGHT 假金。**删掉 self_corr 门:0→0**(self_corr 根本不是瓶颈,54/686 本就过);**调阈值救不了**(分布整体衰减一整档,不是「门偏高一点」)。
2. **#25c 字段卫生必要但远不充分**:mean sharpe −0.34→**−0.07**、univ1 退化垃圾灭掉(8/8 sharpe=0 自然沉底),但 **yield 仍 0/686**。
3. **唯一能把 |sharpe| 顶部抬过 1.25 门的杠杆 = 真·新正交供给**。而 USER-mode 已基本穷尽:USA 同步 **19** 数据集、挖了 **18**(仅剩 pv96 = 价量同族、不正交),universe 轮转(TOPSP500/TOP200/TOP1000/TOP500)实证**全 0-yield**。真正的 breadth 主轴 = **Consultant 模式 5-region**(各 region 有独立数据集目录),当前 `ENABLE_BRAIN_CONSULTANT_MODE` flag = **OFF**。
4. **因此 P0 卡在一个我无法从代码推断的用户事实:Consultant 升级是否可用。** 整个计划据此二分支(§3)。
5. 其余被审决策全部翻案/降级:
   - **#31 reward 重设计 → DEFER**(D3 **REFUTED**):0-yield 下 reward 只是「在一堆 mean-negative 死数据集间重排座椅」,SHARPE_SCALE=0.73 把 56 个候选饱和到 r=1.0 无区分力。设计方向(discounted-|sharpe| 是非平稳工具)正确但**当前实施是过早**,gate 在「新供给产出信号」之后。
   - **#32a orthogonality-steered exploration(Phase A)→ DEFER**(D4 **FLAWED**):orthogonality_score 仅 **8% 落库**(92% UNKNOWN,PnL 未就绪)→ A/B 检出力 <5% 不可测;且 pillar nudge 把生成导向**已死的 regime pillar**(value/quality 的 fundamental6 fresh mean 也 −0.10)。
   - **认知层(Track A/C/D)→ 保 P1 但 flag-OFF 观察态**(D5 **FLAWED**,精修):Track D bandit censoring **已 SHIPPED**(live);SUCCESS_PATTERN **1601 行**(源侧非饿死,Track A 在写);只是别在 yield=0 时翻 `ENABLE_POOL_COGNITIVE_RECONCILE` ON。
6. **横切隐患:offline 度量基底与 live regime 脱钩 + BRAIN realized OS 实证缺席**(完整性 critic + 2026-06-07 用户更正,已实证)。`alpha_pnl` 冻结 **2019-01-02 .. 2023-12-29**(5.08M 行);**关键更正:realized OS 不是"~2026-07 会来",而是实证缺席** —— **13/13 已提交 alpha 的 `os_metrics` 全 `PENDING`**(SELF_CORRELATION/SHARPE/IS_SHARPE…),最老提交于 1.5mo+ 前仍无动静。所以「等 BRAIN 填 OS 然后建 live 对账」是错路(`project_auto_submit_shipped` 的「~2026-07 复查」注脚被证伪)。⚠️ **平台架构事实(2026-06-07 用户确认,见 memory `reference_brain_os_hidden_is_only`)**:BRAIN **模拟阶段只返回 IS**(样本内),OS(Semi-OS/Real-OS)**架构性隐藏、仅提交后后台盲测**(Semi-OS=IQC Stage 2 占 50%,Real-OS=顾问薪酬依据;IS 好 OS 崩→DECOMMISSIONED)。"Show Test" 是 IS 切片的伪 OS,反复看会过拟合。**∴ realized OS 永远不可能作为 steering/对账输入 —— #34「live OS 对账」不是延迟,是架构上不可能。** ⚠️ **re-sim 口径更正**:mLxlen69 2.01→−0.74 是 **re-sim 当前 IS 窗口**(结果随时间变化是实证事实),**也是 IS 非 OS**;它仍是有效的 **regime 衰减探针**(老结构在更晚 IS 可见数据上变差),但别当「OS 真值」。本地 steering 度量(orthogonality/marginal/self_corr)跑在 2.5yr 陈旧 PnL 上 → 只能用 re-sim 的当前 IS 作探针(§3-B-2),无 OS 升级路径。

> 一句话:**问题不全是工程能修的 —— 是市场 regime 变了 + 本账号能力(USER mode)的搜索空间已耗尽。** #25c 止住了自伤;真产能恢复需要**新正交供给(Consultant 跨区域 / 新数据源)**,这是 P0 的真前提,其余 reward/steering/认知层都是「有了新信号之后才有用」的下游工具。

---

## 1. 对抗审查裁决(workflow `w4lpfssg7`,2026-06-07)

| # | 被审决策(草稿) | 裁决 | severity | 核心实证 / 修正 |
|---|---|---|---|---|
| D1 | breadth 是 P0 主导杠杆 | **FLAWED** | HIGH | USA catalog 19 个、挖 18,仅剩 pv96(价量同族**不正交**)→ USER-mode 新正交库存≈0。breadth **方向对**(D2/完整性都确认),但 USER-mode **无库存可兑现** → P0 真前提是 Consultant。 |
| D2 | yield=0 是真 regime 不是 artifact | **核心成立**(次假设 FLAWED) | CRITICAL | ✅ 是真 regime 供给天花板。但我草稿里的次要假设全错:self_corr 非瓶颈(54/686 过)、can_submit 字段不参与评估(用 brain_can_submit 权威)、调门无效(p95=0.55)。 |
| D3 | #31 reward 现在实施(gate MET) | **REFUTED** | CRITICAL | 0-yield 下 reward = 死 regime 里重排座椅;SHARPE_SCALE=0.73 饱和 56 个;过门的全是 CW 假金;univ1 已被 #25c+\|sharpe\| 解决。**DEFER 到新供给产信号后**。 |
| D4 | #32a orthogonality Phase A 现在跑 shadow | **FLAWED** | HIGH | orthogonality_score 仅 8% 落库(92% UNKNOWN)→ A/B 不可测(<5% power);pillar nudge 导向死 regime pillar。**DEFER**。 |
| D5 | 认知层降 P2 | **FLAWED** | HIGH | Track D 已 SHIPPED(非 P2 blocker);SUCCESS_PATTERN 1601 行(源非饿死)。应**保 P1 但 flag-OFF 观察态**,别据「yield=0」一刀切判全层无用(KB 源/reward 信号/生成质量是三件事)。 |
| 完整性 | 计划遗漏的杠杆 | 见 §2.3 | — | (1) **Consultant 是 P0 硬依赖非平行项**(最大);(2) **offline 度量基底陈旧**(横切风险);(3) clean-tail settings-sweep 是 P0 配套 conditional lever(别完全弃)。确认 **L2 组合层不是杠杆**(BRAIN 只提交单 alpha、各自独立过 1.25 门)。 |

---

## 2. 修正后的战略图(data-grounded,live PG 2026-06-07)

### 2.1 yield=0 是真天花板(三关递减,fresh 686)
```
686 fresh alpha (36h)
 → sharpe ≥ 1.25:        7   (1.0%)
 → + fitness ≥ 1.0:      2   (0.3%)
 → + turnover ∈[.01,.7]: 1   (0.1%)   ← 唯一,还是 model16 CW 假金 (fit 9.21)
 → + self_corr < 0.7:    0   (0.0%)   ← self_corr 删了也是 0,非瓶颈
signed sharpe: p50=-0.04  p90=0.40  p95=0.55  (max 19.89 = news12 CW 假金 outlier)
|sharpe|:      p50=0.26   p90=0.73  p95=0.98
```
**结论**:不是选择墙(供给本就 ~0)、不是度量 artifact、不是阈值问题 —— 是生成分布整体衰减一档,顶部够不到门。**这是供给天花板,breadth 方向正确。**

### 2.2 #25c 前后(必要不充分)
| 指标 | 修前 | #25c 后(683 fresh) |
|---|---|---|
| mean sharpe | −0.34 | **−0.07** |
| univ1 | 52% 退化垃圾 | 8 个全 sharpe=0(中和) |
| yield | ~0 | **0 / 683** |

### 2.3 breadth 库存盘点(P0 可执行性的命门)
- USER-mode 同步:**19 USA 数据集**(无其它 region),挖了 18,仅剩 **pv96**(价量同族,与 pv1/pv13 不正交)。
- universe 轮转:TOPSP500(361)/TOP200(214)/TOP1000(181)/TOP500(153)过去 14d 仍在大量挖,**全 0 can_submit** → 「TOPSP500=USER 可用真 breadth」(2026-05-25 memory)**已被当前 regime 数据推翻**。
- `sync_datasets` 只走 `effective_region_universes`(USER=仅 {USA:TOP3000})→ 跨区域供给被 `ENABLE_BRAIN_CONSULTANT_MODE`(OFF)gate。
- **净:USER-mode/USA breadth 已基本穷尽。真新正交供给在 Consultant 5-region 后面。**

### 2.4 积压(非杠杆,#26 已闭)
- can_submit 列权威:**67 未提交积压**(创建 2026-05-07 .. 06-04,fresh 686 产 0 新)。
- #26 实证:64 冗余(marginal ΔSharpe 负 vs 当前 13-池)+ 3 additive 但是**同一 alpha**(红线封号)→ 跳过提交。**积压不是杠杆。**

---

## 3. 优先级决策树(中心 = Consultant 可用性)

> P0 的可执行性完全押在「是否有可挖的新正交供给」。这取决于 Consultant 升级是否可用,而那是**用户侧事实**(收到 BRAIN 升级邮件 → ops 翻 flag),代码推不出。**故计划二分支。**

### 分支 A — Consultant 可用 → **P0 = 跨区域 breadth(真生产路)**
不同 region = 不同市场/驱动 = 真正独立的 regime 与正交供给(不撞 same-region self-corr 门)。
1. **P0-A1**:`ops/brain/activate-consultant` 翻 `ENABLE_BRAIN_CONSULTANT_MODE`(用户收到升级后)→ `effective_region_universes` 解锁 5 区(USA/CHN/HKG/JPN/EUR)。
2. **P0-A2**:跨区域 sync + cell_stats 接入轮转(`sync_datasets(regions=[...])` 已支持 per-region universe;`dataset_cell_stats` 已 per-(delay,universe) 规范化)。校验各区 universe(HKG=TOP500/JPN=TOP1600 等)。
3. **P0-A3**:跑 1-2 区 soak,看新 region 是否产 yield(新市场 regime 大概率未与老结构同步衰减)。
4. **解锁后**:#31 reward(有真信号可 steer)、#32a orthogonality(有跨区正交池)重新变得有用 → 从 DEFER 升回 P1。

### 分支 B — Consultant 不可用 → **无高 EV 生产杠杆,转「止血 + re-sim 监测,熬过 regime 低谷」**(✅ 已选定 2026-06-07)
诚实结论:USER/USA/当前 regime 下没有能把 yield 抬起来的工程杠杆(搜索空间耗尽 + regime 死)。**核心是:别烧资源追死 regime;用最便宜的方式监测 regime 是否转 + 顺带复核已有货是否复活;等市场转或 Consultant 到位。主动权在市场,不在工程。**

> **⚠️ 原「前置投资 = live OS 对账」(#34)已死 —— 不是延迟,是架构上不可能**:BRAIN 模拟只返回 IS,OS 架构性隐藏(仅提交后盲测,见 §0.6 + memory `reference_brain_os_hidden_is_only`);13/13 已提交 alpha `os_metrics` 全 `PENDING` 实证印证。**唯一可得的「当前表现」= 重新 simulate 拿当前 IS 窗口(非 OS,但可作 regime 探针,今天就能用)**。所以 B 没有「建了待命」的前置工程,只有下面两件立即可做的便宜事。

**B-1 止血:停/降 fresh 挖掘**。当前 686 alpha/36h 全 0-yield = 纯烧 LLM token + sim 槽,且 fresh-mining 不是好的 regime 探针(贵且间接)。**降到近零或暂停**,预算转给 B-2 的定向 re-sim。⚠️ 操作变更,需用户定档(见 §6)。

**B-2 re-sim 监测器(分支 B 的主探针,替代死掉的 #34)**:周期性(如每周)**重新 simulate**:
- **13 个已提交 alpha** → 当前 IS 窗口 sharpe 是否从衰减中恢复 = regime 是否转(直接信号,13 sims 极廉价;口径 IS 非 OS,但够当探针)。
- **67 积压的轮转抽样** → 是否有 alpha 在当前 IS 下重新过门 = 我们其实有却被 #26 判冗余的真货(survivor check)。
- 顺带 **re-validate n=2 regime 结论**(完整性 critic 提的小样本风险):多 re-sim 几个老赢家,确认是真 regime 死还是采样噪声。
- **告警**:滚动 mean(re-sim sharpe) 转正 / 出现 ≥1 个 current-data 过门候选 → 触发重评 #31/#32a + 重启全速挖掘。复用 `scripts/_soak_check_2026_06_07.py` 骨架 + `BrainAdapter` re-sim,挂 beat(worker 已有 BRAIN 凭据)。

**B-3(可选,小上界,需凭据)**:核 BRAIN 是否对 USER tier 暴露**未同步的 USA 数据集**(本计划未 live 核实=本地无 BRAIN 凭据);有则低成本 ingest,但 regime 下预期增量小。

**路径依赖警示**:Consultant 升级需「提交好 alpha 攒资格」,而提交需生产、生产被 regime 堵 → 鸡生蛋。当前 13 提交。**B 的本质是「熬过 regime 低谷」**:把成本压到最低,靠便宜的 re-sim 探针守着,市场一转就重新进攻;OS 数据这条路堵死,别等。

---

## 4. 逐项处置(#22–#32 重排)

| 任务 | 原状态 | 新处置 | 理由 |
|---|---|---|---|
| **#31** reward 重设计 | P1 in_progress | **DEFER**(设计稿保留,gate 在新供给产信号) | D3 REFUTED:0-yield 下无区分力 |
| **#32** breadth | P1 pending | **拆**:#32-core = 跨区域(分支 A 的 P0)/ #32a orthogonality Phase A = **DEFER**(D4) | D1:USER-mode 无库存,真 breadth=Consultant |
| **#22** Track A 验证 | P1 pending | 降 **P2**,保留(SUCCESS_PATTERN 1601 行已在写,验证即可) | D5:源非饿死 |
| **#23** Track D 验证 | P1 pending | **关闭**(已 SHIPPED live) | D5:censoring 已 live |
| **#24** Track C 翻 flag ON | P1 pending | **保持 flag OFF 观察态**,gate 在 yield 回升 | D5/D3 |
| **#27** Track B R1a | P2/HOLD | 维持 HOLD | regime 下更不相关 |
| **#28** Phase 2 深度 | P2/数据gated | 维持 defer | data-starved |
| **#26** 积压 | completed | 闭(非杠杆) | 64 冗余+3 红线 |
| **新 #33** live OS 对账基础设施 | — | 分支 B 的 P0 / 分支 A 的 P1(中期) | 度量层脱钩,最高 EV 工程项 |
| **新 #34** clean-tail settings-sweep | — | conditional lever,gate 在 P0 产出 ≥1 个 sharpe 1.0-1.2 正交近门候选 | 15621→2.18 实证 nuance,别完全弃 |

---

## 5. 风险 / 守卫

1. **REGIME 诊断 n=2**(mLxlen69 反转 + Xgkr0O6l 衰减)—— 强暗示但小样本。**但 fresh 686 的 p95=0.55 << 1.25 是大样本独立佐证**,供给天花板结论稳健。措辞保持「regime + 搜索空间耗尽」,不据 n=2 下过强单点结论。
2. **offline 度量基底陈旧**(§0.6):任何用本地 alpha_pnl 的 steering(reward/orthogonality/marginal)在升级到 live OS 对账前,结论都带 2.5yr 窗口噪声。**翻任何 bandit/steering flag 前先核 `refresh_os_correlation_cache` 缓存戳**。
3. **别追「恢复 ONESHOT +1.5」**:那个基线跑在更友好的 regime,老结构已死,不是目标。目标 = 当前/新 regime 下的可提交正交 alpha。
4. **别在 0-yield 时翻 reward/steering/reconcile flag**:会在死信号上自强化(LB4 重演)。全部 gate 在「新供给产 yield ≥1%」。
5. **Consultant 路径依赖**(§3-B4):升级需资格、资格需提交、提交需生产、生产被堵 —— 别把 Consultant 当「随时可翻的开关」规划,先确认可用性。
6. **flag hook footgun**:非-`ENABLE_` 配置直读 `_flag_override_cache`(见 `reference_feature_flag_hook_enable_prefix_only`);idle-in-txn:profile/corr 读用短事务别跨 await。

---

## 6. 决策记录 + 待定操作决策

### 已决(2026-06-07)
**Consultant 升级当前不可用 → 执行分支 B。** 接受「USER/USA/当前 regime 下无生产杠杆」的诚实结论,转「止血 + 监测 + 前置投资」。

### 分支 B 下待用户拍板的 2 个操作决策
1. **池速率档**(P0-B-now-1 止血):全速挖(686/36h,纯烧)/ 低速 sensor(只探 regime)/ 完全暂停(只靠手动 re-sim 监测)?—— 影响 LLM/sim 预算与 regime 探测灵敏度的权衡。
2. **live OS 基础设施时机**(P1-B-fwd / #34):现在就建好待命(~2026-07 BRAIN 填数即激活)/ 推迟到 ~2026-07 临近再建(避免建了空放)?

### 重评触发(回到分支 A / 重启生产)
regime-turn 监测器告警(fresh 7d 滚动 mean sharpe > 0 或 yield > 0.5%)→ 重评 #31 reward + #32a orthogonality + 全速挖掘;或 Consultant 升级到位 → 切分支 A。

---

## 附:关联
`pool_native_reward_redesign_2026-06-07.md`(#31,DEFER 但设计有效)、`orthogonality_steered_exploration_plan_2026-06-05.md`(#32a Phase A,DEFER)、`kb_feedback_redesign_2026-06-06.md`(Phase 1 认知层)、`competitive_analysis_v3_2026-05-26.md`(breadth=数据源轴 / same-region 门)、memory `project_submit_yield_collapse_field_hygiene_2026_06_07` / `project_depth_levers_refuted_breadth_is_answer_2026_05_25` / `reference_brain_before_after_score_removed`(Consultant 能力分类)。
