# AIAC 开发计划 — 绿地精简版 (2026-06-07)

> **目标(用户明确):产出可提交 alpha + consultant 收益。**
> 视角:绿地(从零开始、知道现在所知的一切、剥离沉没成本)。
> 取代 `dev_plan_post_regime_2026-06-07.md`(那是"如何运维现有机器"的视角;本稿是"为目标该建什么"的视角)。证据链见该文档 + 对抗审查 `w4lpfssg7` + memory `reference_brain_os_hidden_is_only`。

---

## 0. 一句话
**瓶颈是"盒子"(USER tier / USA-only / 当前 regime),不是机器。** 现有系统相对其唯一目标(可提交 alpha)已严重过度工程(历史 13 提交 / ~14000 挖掘 = **0.09%** 提交率)。盒子换不了(Consultant 是考核**奖励**,不是开关);供给受 regime 限,短期变不出。**唯一 100% 在工程控制内的杠杆 = 在给定供给里"选哪些提交"** —— 而考核考的就是提交的 OS 表现。绿地结论:**只做两件事——①止损(停掉盒内一切 fiddling)②建「抗过拟合稳健∩正交」提交选择器(已 PoC 验证);其余全部冻结。**

---

## 1. 确知的硬真相(决定一切,不可绕)
1. **盒子是瓶颈,不是工程**:USER/USA 只有 19 数据集、挖穷 18、universe 轮转 0-yield;当前 regime 老结构衰减/反转(mLxlen69 提交 IS 2.01 → re-sim IS −0.74)。fresh 686 alpha 全 0 can_submit,signed sharpe p95=0.55 << 1.25 门。**这不是工程能修的。**
2. **OS 架构性隐藏**:BRAIN 模拟只返 IS;OS(Semi-OS/Real-OS)提交后后台盲测,永不可见(13/13 提交 os_metrics PENDING 实证)。→ **「从结果自进化」的反馈环地基不存在**(CoSTEER 学不到 outcome)。
3. **selection-limited,不是 discovery/throughput-limited**:能挖多少从不是问题;能产出多少"过门 + 正交 + 扛得住盲测 OS"的 alpha 才是,这个数在当前盒子 ≈ 0。
4. **OS 盲测 ⇒ 唯一可控的提交质量杠杆 = 提交前的抗过拟合稳健性**(DSR/PBO/CPCV/settings 稳健),不是堆 IS sharpe。挑 IS-sharpe 高的提交 ≠ 收益高(可能 OS 崩 → DECOMMISSIONED)。

---

## 2. 目标函数 ✅ 已明(用户提供 WQ 计酬模型 2026-06-07)
见 memory `reference_wq_consultant_compensation_model`:
- **Base(日 1-120$)**= 提交**数量** + 质量(Fitness/Sharpe) + 自我成长 + **Value Factors(过拟合控制)**
- **Quarterly(季 100-25k$)**= **Weight(平台组合权重)** + **OS 表现** + 历史
- 隐性好 alpha:Sub+Super 均 Sharpe>0.7 / Turnover<30% / Margin>4bps / 不加噪

**这推翻了之前的两种猜测,也推翻了现状机器的核心假设**:目标 = **质量(robustness/ValueFactors)× margin × fitness + 数量 + 正交度(=平台 Weight)+ OS 生存**;**不是 "marginal ΔSharpe vs 我自己的池"**(comp 公式无此项)→ 现 `auto_submit_selector` 的 G7/G8 + `marginal_recon` kill-switch 整套机器**优化了不被计酬的指标**。robustness(#39)= Value-Factor/OS 代理 = comp 核心,被直接背书。

→ 驱动 **`docs/unified_submit_selector_design_2026-06-07.md`**(统一选择器,把目标从 marginal-vs-我池重新对中到 comp;对抗审查 `wd7190y28`)。这不再是"P1 细化",是 **P1 选择器的目标重定向**。

---

## 3. 绿地架构 = 当前的 ~5%
**挣到位置的组件**(直接服务"可提交 alpha"):
①干净字段选择(#25c 已做)②IS 门 + 正交检查(已有)③**抗过拟合稳健∩正交提交选择器(唯一值得新建的件,本轮已 PoC)**④regime 死活闸(由③的在线 re-sim 顺带,不单建)。
> (capability/换盒子不在组件里——它是考核奖励,不是可控件。)

**绿地不会建的**(现已重投但不服务目标):CoSTEER 自进化环、dataset bandit/reward 导流(#31)、四池吞吐解耦/orchestrator、认知层 reconcile、orthogonality A/B(#32a)、33 项 ops 控制台扩展。→ 全部进 §6 冻结清单。

---

## 4. 计划:唯一完全在控制内的杠杆 = 提交选择

**框架(钉死)**:盒子换不了——**Consultant 是考核结果、是好提交的下游奖励,不是可拉的杆**(用户确认)。生成供给受 regime 限制,短期变不出。**唯一 100% 在工程控制内的,是「在给定供给里,选哪些提交」**——而考核考的就是提交 alpha 的 OS 表现。所以计划只两件事:**别浪费(止损)+ 把选择做到最好(稳健∩正交选择器)**。

### P0 — 止损(即刻,~0 工时)
- **池拧到 ~0**:产 0 可提交,全速 = 白烧 coding-plan 包月额度(实测 $0 计费但额度有 cap,maas 06-04 烧爆是前车)。直接调低 daily goal 到最小 / 暂停。
- **冻结**:#31 / #32a / 认知层 flag / bandit 调参 / ops 运维(见 §6)。
- regime 是否转 → 由 P1 选择器的在线 re-sim 阶段顺带探测,不单建告警 beat(gold-plating)。

### P1(计划主体)— 抗过拟合「稳健 ∩ 正交」提交选择器
**目标**:OS 盲测下最大化每次提交的 OS 存活概率 → 直接驱动考核 + 收益。**唯一价值不依赖 regime、完全可控的工程资产。**

**✅ 核心已 PoC 验证(本轮 backlog 实跑,纯存量数据、无凭据、无 skill)**:
- 离线两信号都能算:① **稳健性** = PnL 子周期 Sharpe 一致性(6 段是否全正,vs 靠单段暴涨)② **正交性** = 候选 PnL vs 13 已提交池的最大相关(= <0.7 提交门信号)+ 完全相同表达式去重(red-line)。
- 实证产出:67 → 去重 54 → 排出真候选 **QPE25Yer**(analyst4,6/6 子周期全正 / 最差 0.98 / margin 92bps / 对池 corr 0.66)、rKAr8Xeo(news12,最正交 0.38)。
- 实证拆陷阱:IS-sharpe 最高的 Wj9eQGeG(2.18)/GrkOaZj0(2.03)对已提交池 **corr 0.97–1.00 = 近重复** → 按 sharpe 提交即踩红线/冗余。**证明选择口径必须是 稳健∩正交,不是 sharpe**(也从个体层印证 #26)。

**✅ 离线阶段已落成正式组件(2026-06-07 build)**:
1. **新模块 `backend/robustness_selector.py`**(纯函数,12 单测):per-alpha 子周期 Sharpe 一致性 → `robustness_score [0,1]` + `ROBUST/MODERATE/FRAGILE` 裁决 + max drawdown。`assess_from_pnl_rows` 复用 `marginal_drain` 的行签名(端点喂同一批 PnL,零额外 DB)。标定入 `config.ROBUSTNESS_*`。
2. **接进 `GET /ops/submit-backlog/drain-order`**:既有端点已做"正交∩组合价值∩去重"排序;本次加 robustness 维度——`DrainOrderItem` 加 5 稳健字段 + `min_robustness` query 门(默认 0=仅标注不破坏;>0 剔 FRAGILE/无PnL 进 `fragile` 桶)。
3. **live 验证 + 顺带修了一个真缺陷**:端点原先"对已提交池相关性"只用存量 `metrics._self_corr` 种子,而 56/67 是 NULL → **池近重复(如 Wj9eQGeG corr=1.0)被当正交误推荐(红线)**。修=从 PnL 直算 candidate↔已提交池 max|corr| 填 NULL 种子。修后默认 selected 40→34(挡住 6 个池重复),gate=0.6 下选出 6 个全 ROBUST+正交+additive。
4. **在线阶段(待建,需 sim 配额)**:对离线 top-K **re-sim 当前数据**做 regime 衰减检查(冻结 PnL 稳健 ≠ 当前有效,mLxlen69 证)→ 只放行当前仍稳健者。这是设计里的 stage 2,需 BRAIN 凭据/配额,不在本次离线 build 内。
- **作用域**:① 67 backlog(立即可用)② 未来候选(常驻)。**增强**:DSR/PBO/CPCV 是后续,子周期一致性是 MVP。
- **测试**:42 passed(新 12 + marginal_drain + ops 集成回归);live PG 端到端冒烟过。未 commit。

### P1-细化 — 目标函数权重(原 §2,次要,不阻塞)
计酬 per-alpha OS vs portfolio 只影响"是否放行高相关候选"的阈值,**选择器在两种口径下都对**(稳健∩正交的 alpha 两种都好)。先建选择器;计酬口径确认后只调阈值。

---

## 5. 诚实边界
- **不保证短期更多提交**:供给受 regime 限,选择器只能从现有供给挑最好,变不出新货。
- **Consultant 不在计划里**:它是考核产出的奖励,不是动作项。**计划的产物(高 OS-存活的提交)本身就是考核材料 → 攒资格的唯一正路。**
- 选择器在线 re-sim 阶段要 sim 配额;离线阶段不要(已验证)。

---

## 6. 冻结清单(明确不做,别再运维/调/监控)
| 冻结项 | 为何冻结 |
|---|---|
| #31 reward 重设计 | 盒子内重排座椅,0-yield 无区分;且 reward 学不出新边际 |
| #32a orthogonality A/B | 8% 落库不可测 + steer 向死 pillar;盒内 discovery 效率,非供给 |
| 认知层 reconcile / Track A/C 激活 | OS 隐藏 + yield=0 → 无 outcome 可学,数据饿死 |
| dataset bandit 调参 | 19 个全 0,无差异可学 |
| 四池吞吐 / orchestrator 优化 | throughput 过剩(500+/day 产 0) |
| ops console 扩展 | 监控 0 产出 |
| live OS 对账(#34) | 架构死(OS 永不可得,见 memory) |

---

## 7. 即时待办
1. **(即刻)P0 止损**:池调最小 + 冻结清单生效。
2. **(主体)P1 稳健∩正交选择器**:离线阶段(已 PoC,升级为服务)→ 在线 re-sim 阶段(regime 衰减检查)→ 提交优先队列;先作用于 67 backlog,再常驻。
3. **(次要,不阻塞)**钉死计酬口径(per-alpha vs portfolio)→ 只调选择器"是否放行高相关候选"阈值。
4. ~~换盒子 / Consultant 路径~~:**移出计划**(是考核奖励非动作;计划产物=好提交=攒资格正路)。

---

## 附:与既有文档的关系
本稿是**目标导向重写**,不推翻 `dev_plan_post_regime` / `w4lpfssg7` 的事实结论(yield=0 真天花板 / breadth USER-mode 穷尽 / OS 隐藏),只是据"产出 + 收益"目标重排优先级、把盒内机器降为冻结。`pool_native_reward_redesign`(#31)、`orthogonality_steered_exploration_plan`(#32a)设计稿保留备查,但**不实施**。
