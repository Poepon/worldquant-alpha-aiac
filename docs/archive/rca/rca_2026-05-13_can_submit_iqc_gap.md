# RCA — IQC marginal audit data 解读 (2026-05-13 修订版)

**日期**: 2026-05-13(初稿),2026-05-13 修订(用户校准认知错误)
**触发**: V-22.12.1 IQC audit beat sweep 上线后,Trigger monitor 显示 41 audited / 0 positive Δscore
**严重度**: 🟡 中 — 数据已就位,但**原初 RCA 把现象当 bug 解读错了**

---

## ⚠ 关键认知校准(2026-05-13 用户指出)

初稿 RCA 把两个完全独立的概念混为一谈,是错的:

| 概念 | 实质 | 关系 |
|---|---|---|
| **WQB `can_submit`** | 平台 8 个硬性 check(syntax / correlation / sub_universe / ...) | **形式合规**,跟 IQC 无关 |
| **IQC marginal Δscore** | 提交后 merged portfolio 累加效应**快照** | **动态**,随其他 alpha 提交/删除变化 |
| **Mining quality gate** | SHARPE_MIN / FITNESS_MIN / TURNOVER_MAX 等阈值 | **独立** mining 决策,不应绑 IQC 状态 |

**`can_submit` 与 IQC value-add 脱钩不是 bug,是 by design**。`can_submit` 只承诺"WQB 允许提交",从未承诺"对 IQC 加分"。这两件事在系统里**就该**独立。

**IQC marginal Δscore 是动态快照,不是稳定 quality 标签**。提交一个 alpha 之后,所有未提交 alpha 的 marginal Δscore 都会变化。删除一个已提交 alpha 也会变。今天 -1200 的 alpha 可能明天就 +500。

## 现象(数据未变,解读修正)

41 个 can_submit=true unsubmitted alpha,V-22.12.1 sweep 后全部审计完毕:

| 维度 | n | mean Δscore | mean Δsharpe | merged_sharpe(当前 portfolio)| standalone_sharpe |
|---|---|---|---|---|---|
| T1 | 7 | -1183.0 | -0.246 | 3.034 | 1.493 |
| T2 | 34 | -1257.2 | -0.351 | 2.929 | 1.412 |
| **合计** | **41** | **-1245** | **-0.34** | **~2.95** | **~1.43** |

**正确解读**:
1. 当前 portfolio 已包含 pk=7810 等贡献者 → merged sharpe ~2.95
2. 在**此时此刻**这个 portfolio 状态下,这 41 个 standalone sharpe~1.43 的 alpha 提交进去会摊薄(标的 sharpe 低于 merged sharpe)
3. **如果删除已提交的 pk=7810**(portfolio 回到更稀疏状态),这 41 个 alpha 的 Δscore 大概率会**全部重算**,部分可能转正
4. **如果再提交 1 个更高 sharpe 的 alpha**,merged 提升,这 41 个 Δscore 会更负

**错误解读(初稿)**: "can_submit gate 找出的 alpha 全部对 IQC 无价值 → gate 失效"。错。Gate 本来就不管这事。

## 真正的洞察

不是"gate 失效",而是**系统缺少一个组件**:**IQC marginal 是动态信号,需要 stale 跟踪 + 提交时重新 audit**。

当前问题:
- audit 时刻拿到的 Δscore 是 portfolio 那一刻的快照
- 没有任何机制提醒用户"这个 Δscore 是 X 小时前 audited 的,期间有 Y 个 alpha 已提交,数据可能过时"
- 用户看到队列里 41 个 Δscore 都是负的,可能误以为这 41 个 alpha "本质上没价值"。其实它们的 Δscore 跟 portfolio 状态强耦合

## 正确处理方向(替代初稿 V-23.A/B/C/D)

### V-23.A (修正): submit queue 按 Δscore 排序 + stale 标记

**不是 filter** — IQC marginal 是动态的,不能用来"过滤掉"alpha。

- 按 `metrics._iqc_marginal.delta_score DESC` 排序展示
- 显示 audited_at 时间戳
- 若 audited_at 超过 N 小时,或自该 audit 后有新 alpha 提交 → 标 "stale"
- 用户看到的是"当前 portfolio 状态下,这些 alpha 谁更有可能加分"的 ranked list,而非"过滤后的可提交 candidates"

**工时**: 0.5 day

### V-23.E (新增): IQC marginal re-audit on submission

提交 alpha 后,portfolio 状态变化,所有未提交 can_submit alpha 的 Δscore stale:

- 加 `alpha.metrics._iqc_marginal.stale: bool` 字段
- 提交触发(date_submitted flip None→timestamp): 批量 SET 所有未提交 can_submit alpha 的 `stale=true`
- `iqc_audit_backfill_sweep` 优先扫 `stale=true` 的 alpha(WHERE 加 ORDER BY stale=true DESC)
- frontend 显示 stale 标记 + 用户主动 trigger re-audit 按钮

**工时**: 1 day

### 撤回的初稿建议

| 初稿 task | 状态 | 撤回理由 |
|---|---|---|
| V-23.B IQC-aware demote in evaluation | 🗑 删除 | 把动态 Δscore 当稳定 quality 标签,demote 不可逆,portfolio 状态变了无法回滚 |
| V-23.C portfolio-aware mining gate | 🗑 删除 | mining gate 跟 portfolio 状态绑定会让 mining 追逐移动靶,且 mixed-region/dataset 时 portfolio 维度未定义 |
| V-23.D Trigger 4 hard gating | 🗑 删除 | IQC marginal 是动态信号,不适合做严格 gating;trigger monitor 保留 observational 角色,加 stale-awareness |

## Plan v5+ 影响修正

**初稿错误结论**: "Phase 3 hypothesis-centric 优先级降级,portfolio-aware mining 是真 bottleneck"。

**正确结论**: Phase 3 优先级不受这次发现影响。Phase 3 关于"用 hypothesis 而非 dataset 锚 mining 路径",跟 IQC marginal 解读没有直接因果。

Phase 3 ROI 判断仍基于:
- Trigger 1 PASS uplift(+4.7pp,临阈值)
- Trigger 2 retirement rate(43.4% ✅)
- Trigger 3 cross-dataset rate(51.1% ✅)
- Trigger 4 **仍保持 observational** — IQC 价值是提交决策辅助信号,不是 Phase 3 启动 gate

## 数据示例(教学价值)

pk=7810 winner (+341 Δscore) 与当前 41 个 fails 对比:

| 维度 | pk=7810 winner(已提交)| 41 个未提交 fails |
|---|---|---|
| factor_tier | 1 | 1(7) + 2(34) |
| standalone sharpe | 1.55 | 1.28-1.55(mean 1.43) |
| turnover | 0.217 | 0.10-0.40 |
| dataset | model51 | 跨 10+ datasets |
| **提交时点 portfolio 状态** | **稀疏(~2.0 merged sharpe)** | **饱和(~2.95 merged sharpe,已含 7810)** |
| Δscore | +341 | -707 ~ -1922 |

结论:**这两组 alpha 个体质量几乎相同**。差别在提交时点。如果**先**提交 41 中任意一个,后**再**提交 7810,标签会反过来 — 41 中那个变 winner,7810 反而摊薄(因为后者 sharpe 1.55 < 已提交那个的 merged sharpe)。

这正是 V-23.E re-audit on submission 要解决的:**提交决策的影响是双向的**,Δscore 必须实时反映当前 portfolio 状态。

---

## V-23 修正后的最终 task 清单

| Task | 内容 | 工时 | 依赖 |
|---|---|---|---|
| V-23.A | submit queue 按 Δscore 排序 + stale 标记(不 filter) | 0.5 day | — |
| V-23.E | IQC marginal re-audit on submission(提交时 stale + sweep 优先) | 1 day | — |
| ~~V-23.B~~ | ~~IQC-aware demote~~ — **撤回**:动态信号不能用作稳定 quality 标签 | — | — |
| ~~V-23.C~~ | ~~portfolio-aware mining gate~~ — **撤回**:mining 不应追逐 portfolio 移动靶 | — | — |
| ~~V-23.D~~ | ~~Trigger 4 hard gating~~ — **撤回**:动态信号不适合 hard gate | — | — |

总工时 1.5 day,且**不影响 Plan v5+ Phase 3 / V-23.C portfolio-aware mining 等中长期路径**。
