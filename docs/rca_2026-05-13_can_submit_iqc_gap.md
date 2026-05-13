# RCA — can_submit gate 与 IQC value-add 完全脱钩

**日期**: 2026-05-13
**触发**: V-22.12.1 IQC audit beat sweep 上线后,Trigger monitor 显示 41 audited / 0 positive Δscore
**严重度**: 🔴 高 — mining pipeline 全力优化的"通过率"目标与最终 portfolio 价值无关

---

## 现象

41 个 can_submit=true unsubmitted alpha,V-22.12.1 sweep 后全部审计完毕:

| 维度 | n | mean Δscore | mean Δsharpe | merged_sharpe | standalone_sharpe |
|---|---|---|---|---|---|
| T1 | 7 | -1183.0 | -0.246 | 3.034 | 1.493 |
| T2 | 34 | -1257.2 | -0.351 | 2.929 | 1.412 |
| **合计** | **41** | **-1245** | **-0.34** | **~2.95** | **~1.43** |

- **Δscore>0**: 0 / 41
- **Δsharpe>0**: 0 / 41
- **Δfitness>0**: 1 / 41

跨 dataset 覆盖广(model51 / option9 / pv1 / pv13 / sentiment1 / socialmedia* / fundamental6),不是单点问题。

## 根因 — 三层错位

### 1. can_submit 的语义不是"value-add",是"形式合规"

`backend/can_submit.py:compute_can_submit` 计算的是 BRAIN 8-check 通过(SYNTAX / CONCENTRATED_WEIGHT / LOW_SUB_UNIVERSE_SHARPE / SELF_CORR / PROD_CORR / IS_SHARPE / IS_FITNESS / IS_TURNOVER)。

这是 **submission eligibility** check —— 检查 alpha 没有触发明显 risk flag。它**不预测** alpha 加入 portfolio 后的边际贡献。

### 2. Mining gate 阈值与 merged portfolio 状态脱节

当前 SHARPE_MIN=1.25,FITNESS_MIN=1.0,TURNOVER_MAX=0.5 是**绝对阈值**。但 IQC merged portfolio 已达 **sharpe ~2.95**,新增 standalone sharpe 1.43 的 alpha:

- 摊薄 merged sharpe(-0.34 平均)
- 同时增加 turnover(+0.03 平均)
- BRAIN IQC score = f(merged_sharpe, fitness, returns, turnover),双重惩罚

要正向贡献,新 alpha 的 standalone sharpe 需要 **接近或超过 merged sharpe(~3.0)**,且 correlation 与现有 portfolio 低。当前 mining 完全没有这两个信号。

### 3. pk=7810 (+341) 是个反例,标杆错位

用户上次手工提交的 pk=7810 标杆:
- `multiply(-1, ts_decay_linear(divide(subtract(close, open), open), 20))`
- standalone sharpe=1.55,turnover=0.217,**T1 单 dataset(model51)**
- merged 之后 +341 Δscore

它能赢不是因为 sharpe 高(实际只 1.55,跟当前 41 个 fail 的差不多),而是因为它进入 portfolio 时 portfolio 尚未饱和。**现在 portfolio 已经包含它了,再来一个相似 sharpe 的 alpha 就是冗余**。

## 数据证据

- mining pipeline 14 天产 ~660 alpha,其中 50 个 can_submit=true
- 50 中 9 个已提交(含 pk=7810),41 未提交可审计的 alpha **100% Δscore<0**
- top-5 best Δscore: -707, -1033, -1042, -1057, -1061 — 即"最不糟"的也扣 707 分
- 没有任何一个 alpha 的 standalone sharpe ≥ merged sharpe(全部 < 2.0 vs merged ~2.95)

## 影响

### 短期
- Mining 持续产 alpha,但**没有**新 alpha 能改善 IQC portfolio
- 用户手工 review can_submit 队列时 100% 都是负向贡献,等于在错误指标上挑选
- BRAIN simulate 配额每天烧掉,产出零 portfolio value

### 长期对 Plan v5+
- **Phase 3 hypothesis-centric 主循环翻转优先级降级** — 产更多 hypothesis-driven alpha 不解决"个体 sharpe<3" 的根本约束
- 真正阻塞 IQC 提交价值的 bottleneck 在两个地方:
  1. mining gate 没有 "marginal portfolio contribution" 信号
  2. SHARPE_MIN 阈值跟 portfolio 状态完全无关
- 当前 Trigger 1 +4.7pp (Phase 2 vs legacy PASS uplift) 测的是 "PASS rate",而 PASS rate 已经被证明跟 IQC value 脱钩,所以 Trigger 1 即使过线也不证明 Phase 3 ROI

## 修复方向

### Option A — IQC delta_score 作为 submit-queue 二级 filter(最小)

不动 mining gate,只在 frontend / can_submit queue 视图层加 filter:
```
display only: can_submit=true AND (metrics._iqc_marginal.delta_score IS NULL OR > 0)
```

用户看到的待提交队列自动剔除 100% 负向 alpha。

**工时**: 0.5 day(frontend 加 filter toggle + default ON)
**ROI**: 立竿见影 — 用户不再 review 0% 有用的队列
**局限**: mining 仍然产负向 alpha,只是不展示

### Option B — IQC delta_score 进 alpha quality_status gating

mining evaluation 写完 _iqc_marginal 后,若 delta_score < 0 → demote `quality_status` PASS→PASS_PROVISIONAL。
KB 不学这些 pattern,T2/T3 seed pool 也不取。

**工时**: 1 day(evaluation.py + backfill 41 alpha)
**ROI**: 中长期 — KB pitfall 学到"sharpe~1.5 的 T1/T2 加 portfolio 扣分"模式
**风险**: portfolio 一旦变化(其他人提交 / 删除已提交),历史 pitfall 失效

### Option C — Mining 主循环加 portfolio-aware target(根本但激进)

修改 SHARPE_MIN / FITNESS_MIN 阈值为相对值:`SHARPE_MIN = max(1.25, current_merged_sharpe - 0.5)`。
LLM prompt 加 "target portfolio sharpe is X, your alpha must be Y" 提示。
RAG 检索 SUCCESS_PATTERN 只取 +Δscore 的(需 KB schema 加 marginal field)。

**工时**: 3-5 day
**ROI**: 长期 — pipeline 自动追踪 portfolio 状态,产 alpha 直接对齐 IQC 提交标准
**风险**: portfolio 饱和情况下 mining 几乎产不出任何 PASS,需配套调度策略(切换 region/dataset 加冷启动)

## 推荐路径

```
Phase 1 立即(1 day内):
  - Option A: frontend submit queue 加 Δscore > 0 filter
  - 用户手动 review 时已经看到剩 0 个,直接放弃当前队列

Phase 2 短期(1 week内):
  - Option B: evaluation + KB pipeline 启用 IQC-aware demote
  - V-22.12.1 sweep 已就位,supplied data 已有 metrics._iqc_marginal
  - 加 1 个 unit test 验证 demote 逻辑

Phase 3 中长期(2-4 week,需用户决策):
  - Option C: portfolio-aware mining 重构
  - 与 Plan v5+ Phase 3 (hypothesis-centric) 形成 trade-off:
    * 选 Plan v5+ Phase 3 = 产更多 hypothesis 但仍受 portfolio 饱和限制
    * 选 Option C = portfolio-aware 但仍可叠加 hypothesis-driven 生成
  - **建议先 Option C 再 Phase 3**,因为 portfolio 阈值是底层 gate,Phase 3 只改生成路径
```

## Plan v5+ Trigger Monitor 解读修正

Trigger 4 (IQC marginal positive rate) 从 plan v5+ 当前的"observational, non-gating"应升级为 **hard gating prerequisite for Phase 3**:

```
Phase 3 启动前置条件:
  - 现有 Trigger 1+2+3 全过 → necessary not sufficient
  - **Trigger 4 IQC positive rate ≥ 20%** → sufficient signal
  - 如果 Trigger 4 仍为 0% → Phase 3 不会改善状况,先做 Option C
```

更新 `scripts/phase3_trigger_monitor.py`:Trigger 4 从 `gating=False` 改为 `gating=True`,加阈值 20%。

---

## 数据集证据 — pk=7810 vs 当前 41 个

| 维度 | pk=7810 winner | 41 个 fails |
|---|---|---|
| factor_tier | 1 | 1(7)+ 2(34) |
| standalone sharpe | 1.55 | 1.28-1.55(mean 1.43) |
| turnover | 0.217 | 0.10-0.40 |
| dataset | model51 单一 | 跨 10+ datasets |
| 提交时点 | portfolio sharpe ~2.0 | portfolio sharpe ~2.95(已含 7810)|
| Δscore | +341 | -707 ~ -1922 |

结论:**winner 与 failure 在 individual quality 上几乎没区别**,差在提交时点 portfolio 状态。这是 portfolio-state-aware mining 的核心 case。
