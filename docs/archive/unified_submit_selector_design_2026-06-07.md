# 统一提交选择器设计 (#39 统一 · incorporating WQ 计酬模型)

> 2026-06-07 · 起因:用户问"可以统一吗"(drain-order 端点 + auto_submit_selector 两条路漂移 + QPE25Yer 矛盾)+ 提供 WQ 计酬模型(resolve §2 objective)。
> 状态:**[大幅修订 2026-06-07] 对抗审查 `wd7190y28` 5 决策全 FLAWED(HIGH)→ 放弃"重新对中/拆 marginal/统一合并",改外科手术。** 见 §0.5。原 §1-3 的"recenter"主张**已被证伪**,保留备查。

## 0. 计酬模型(用户提供 2026-06-07,resolve §2 — 一切排序的目标函数)
| 收入 | 周期 | 决定因素 |
|---|---|---|
| **Base** | 日 1–120 USD | 提交**数量**(vs 全球顾问日提交排名)+ 质量(Fitness/Sharpe)+ 自我成长 + **Value Factors**(越接近 1 越好 = 纯净度/过拟合控制)|
| **Quarterly** | 季 100–25k USD | **Weight**(alpha 在平台组合累积权重,需时间)+ **OS 表现**(直接关联 Value Factors)+ 历史系数 |

隐性"好 alpha"标准(社区经验):**Sub+Super Universe 均 Sharpe>0.7 / Turnover<30% / Margin>4bps / 不为过相关检测加噪**。

## 0.5 对抗审查裁决(workflow `wd7190y28`,6-agent,2026-06-07)— 本设计大幅修订
**全 5 决策 FLAWED(HIGH)。净结论:放弃"重新对中/拆 marginal/统一合并",只做审查背书的外科手术。**

| 决策 | 裁决 | 关键证据 |
|---|---|---|
| D1 拆 marginal 机器 | **KEEP(证伪拆除)** | "Weight=平台组合"**不可证伪**(无 API 测平台 Weight);my-pool ΔSharpe 是**可观测代理**;recon kill-switch 实测 87.5% sign-agreement(supported)=载重验证 |
| D2 降 sharpe 1.5→1.25 | **FLAWED** | 设计承诺的 G3b/robustness gate **未实装**→裸降门=无 guard 宽松;且 **cosmetic**(候选先卡 SKIP)|
| D3 提 per_run_cap | **FLAWED** | 真瓶颈=**85% backlog SKIP**(composite<0);BRAIN 历史 ~1/day;pairwise>0.7 红线 greedy **不保证**(只保证 vs 已选集,非传递闭包;125/780 对>0.7)|
| D4 robustness=ValueFactor | **FLAWED** | ValueFactor **非 BRAIN 返回字段**(0/98 keys);robustness 是我们本地量;冻结-PnL 弱 OS 预测(mLxlen69 2.01→−0.74)→ 只作**软预筛**,gate 在在线 re-sim |
| D5 统一合并两路 | **FLAWED** | G3b corr-from-PnL 是**孤立 bug 先修**(已做);别合并;shadow 必须 |
| 完整性 | **2 HIGH** | ① 竞赛 **delta_score 是 Quarterly 代理**(54/67 backlog 竞赛稀释)— 砍 G7/G8 会让稀释货涌入提交拖累 Quarterly(大头);② BRAIN **sub-universe Sharpe 连续值**(0.74-1.57)已返回,选择器只用了二值 |

**我的关键误判(收回)**:把 marginal 机器当成"优化不计酬指标"是错的——my-pool ΔSharpe 是 Weight/Quarterly 的**可观测代理**(平台 Weight 测不了),竞赛 delta_score 更是 Quarterly 直接代理。**真瓶颈不是 sharpe 门,是 85% SKIP(组合/竞赛稀释)。**

## 0.6 修订后的外科手术清单(替代原 §2-3 的"recenter/unify")
- ✅ **已做**:G3b `corr-from-PnL` 移植进 `auto_submit_selector`(端点 #39 修同款)→ self_corr 可填 43/67(QPE25Yer 0.657);39 测试过;**仅影响 G3b/G9 种子,不碰 recon**。
- ✅ **已做**:**纳入 BRAIN sub-universe Sharpe 连续值**(`metrics.checks[LOW_SUB_UNIVERSE_SHARPE].value`)→ auto-submit 加 `G5b_sub_universe` 软门(≥`SUBUNIV_SHARPE_MIN`=0.7,comp 隐性标准,实测挡 1/67 val<0.7);drain-order 端点 `DrainOrderItem.sub_universe_sharpe` 暴露 67/67 供人工复核。config `SUBUNIV_SHARPE_MIN`。83 测试过(含 2 新 G5b 测例)+ live 验证。
- **待呈批(均保守 + shadow)**:
  2. robustness 当**软预筛 + 在线 re-sim gate**,**不**替 G7/G8。
  3. sharpe 1.5→1.25 / turnover→0.30:**低优先 cosmetic**(先解 85% SKIP),要 shadow 验证净解锁数(审查估净 <10 可能是 NO-OP)。
  4. **真问题 = 85% SKIP**:先跑 `marginal_recon` 对账验证 offline ΔSharpe 是否仍是有效代理(若 sign-agreement<60% 才考虑改 routing),**而非降门硬塞**。
- **KEEP**:marginal/composite/recon/G7/G8(Quarterly/Weight 代理)、per_run_cap=2、mode=shadow、ENABLE_AUTO_SUBMIT=False。
- **NO**:拆 marginal、合并两路、提 volume。

## 1. 三个推翻现状的推论 [⚠️ 已被 §0.5 大幅修订,保留备查]
1. **目标 ≠ "marginal ΔSharpe vs 我自己的提交池"**:comp 公式里没有这一项。auto-submit 的 G7(recommendation=SUBMIT)/G8(value_tier=additive)+ 整套 `marginal_recon` sign kill-switch 优化的是**不被计酬的指标**(组合稀释 vs 我池)。Quarterly 的 "Weight" 是 vs **平台**组合(全顾问)非我池,且更正交=更高 weight=正向,不是"稀释我池→拒"。
2. **auto-submit sharpe≥1.5 门过严**:comp 隐性门 Sharpe>0.7、BRAIN can_submit 门 1.25。QPE25Yer(sharpe 1.35 / margin 92bps / turnover 0.038 / fitness 1.6 / 6-6 子周期稳健)按 comp 是优质货,被 1.5 白挡 = 漏钱。
3. **robustness(#39 子周期一致性)≈ Value-Factors / OS-生存代理 = comp 核心**;`per_run_cap=1 + 只 additive` 太保守(Base 奖数量)。

## 2. 统一框架(一个组件,两路共用)
`assess_submit_candidates(rows, cand_pnl, pool_pnl, settings, objective) → ranked + signals + verdict`
两路共调:drain-order 端点(人工复核)直接用;auto_submit_selector 在其上只加运维门(G4 新鲜度 + G10 不可逆 submit_alpha)。消除漂移 + 把 #39 robustness/corr-from-PnL 一处定义到处用。

**硬门(BRAIN + comp 隐性标准,全 AND)**:
- can_submit IS TRUE(BRAIN form)
- self_corr < 0.7(BRAIN 红线;种子用 **corr-from-PnL**,非 stale 存量 NULL — 即 #39 端点修,移植进来)
- margin ≥ **4bps**(comp;现 5bps 可保守留 5)
- turnover ≤ **0.30**(comp;现 EVAL 0.40 过松)
- sharpe ≥ **BRAIN floor(1.25)**(comp 0.7 更松,取 BRAIN 硬下限;**废 auto-submit 的 1.5**)

**排序 = 质量分(comp-grounded)**:
- 主:**robustness(#39)× margin × fitness**(Value-Factors/OS + Base 质量)
- 正交度作**正向**(corr 越低 → 平台 Weight 越高 → Quarterly 越好),非"稀释→拒"
- **marginal-ΔSharpe-vs-我池 降为 informational**(展示不 gate;它不在 comp 里)

**objective 参数**(config `SUBMIT_OBJECTIVE`):
- `quality`(**新默认,comp-grounded**):上面的质量分排序。
- `portfolio`(备查):旧 marginal-ΔSharpe 排序(若日后证明 Weight 真按我池边际)。

## 3. 对 auto-submit 的具体改动(待审)
| 门 | 现状 | 改为 |
|---|---|---|
| G5_sharpe | ≥1.5 | ≥ BRAIN floor 1.25(废 1.5)|
| G5_turnover | ≤0.40 | ≤0.30(comp)|
| G6_margin | ≥5bps | ≥4bps(comp;或留 5 保守)|
| G3b_self_corr | 只读 stale 存量(NULL→fail) | corr-from-PnL 填(#39)|
| G7_recommendation / G8_value_tier | 硬门 | **降级/移除**(comp 不奖 marginal-vs-我池)→ 改 **robustness gate**(robustness_score ≥ 阈)|
| per_run_cap | 1 | 提高(Base 奖数量;但仍 robust-gate + 队列内互 corr<0.7 防红线)|

## 4. 风险 / 待审(为何先对抗审查)
- **R1**:计酬模型解读对吗?"Weight" 是否其实就是组合边际(则旧机器对,别拆)?
- **R2**:sharpe 门 1.5→1.25 安全吗?提交 1.25-1.5 货是否拖累 Base "质量分" / 触发 OS decommission → 净负?
- **R3**:提 per_run_cap(多提)是否被 Base"质量"反噬 / 一批里互相红线?
- **R4**:robustness 是冻结 2019-2023 IS 子周期 —— 作 Value-Factor/OS 代理在 regime 漂移下有效吗?(在线 re-sim 阶段才是决胜;离线只缩范围。)
- **R5**:拆 `marginal_recon` kill-switch 机器是否丢了别的价值?
- **不可逆**:auto-submit 改门 = 改真实提交行为,审查通过 + 默认仍 shadow/flag-OFF 起步。

## 5. 关联
[[reference_brain_os_hidden_is_only]](OS 隐藏 → robustness 是唯一可控代理)、`dev_plan_greenfield_2026-06-07.md`(#39 选择器)、`auto_submit_design_2026-06-04.md`(现 G0-G10 栈)、`marginal_drain.py`/`marginal_recon.py`/`auto_submit_selector.py`(改动面)、`robustness_selector.py`(#39 已建)。
