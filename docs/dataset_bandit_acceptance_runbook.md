# 数据集导流 bandit Tier A — 生产验收 / 运维 Runbook

> 配套：`docs/dataset_steering_bandit_plan_2026-05-22.md`（plan v3）。
> 首次 go-live：2026-05-22（flag ON + seed 15 个数据集）。

## 组件

| 件 | 位置 |
|---|---|
| 后验列 | `bandit_state.alpha_param/beta_param/pulls_at_last_refresh`（Alembic `r9a1c5e3b7f2`）|
| 纯数学 | `backend/selection_strategy.py`：`discounted_thompson_update` / `thompson_sample_weight` / `weighted_choice` |
| 日频 beat | `backend/tasks/dataset_weight_refresh.py:run_dataset_weight_refresh`（celery beat `dataset-weight-refresh` 05:15 SH）|
| FLAT 加权采样 | `backend/tasks/mining_tasks.py:_run_flat_iteration` |
| flag | `ENABLE_DATASET_VALUE_BANDIT`（config + SUPPORTED_FLAGS）|
| dry-run 预览 | `scripts/dataset_bandit_dryrun.py`（只读）|

## reward 语义

```
S_d = #(can_submit AND metrics._iqc_marginal.delta_score > 0)   # book-marginal 正
T_d = #(真 BRAIN sim, 排除 metrics._pre_brain_skip)             # PRESIM_SKIP 不算
g   = γ^T_d   ;   α' = g·α + S_d   ;   β' = g·β + (T_d − S_d)    # pull-indexed 折扣
mining_weight = θ~Beta(α,β) + floor_c·exp(−pulls/τ)
```
窗口由 SystemConfig watermark `dataset_bandit_watermark` 划 `(lower, run_started]`（幂等、非重叠）。
首次无 bandit_state 行 → 从**全历史** seed（α=1+S_hist, β=1+(T_hist−S_hist)）+ watermark=now（不抹 seed）。
未解析 dataset_id（NULL）行**排除**出所有臂。

## 上线步骤

1. **重启** 加载新代码（`run.bat`）。确认 celery beat + 两个 solo worker（`-Q mining` / `-Q celery`）。
2. **备份** mining_weight（回滚必需）：
   ```sql
   SELECT region, dataset_id, universe, mining_weight FROM datasets ORDER BY 1,2;
   ```
   首次 go-live 的备份见 `docs/dataset_bandit_mining_weight_backup_2026-05-22.sql`。
3. **dry-run 预览**（零写入）：`python scripts/dataset_bandit_dryrun.py --seed 0`，确认 pv1 weight 最低、β>0、无饿死。
4. **翻 flag ON**：`PATCH /api/v1/ops/flags/ENABLE_DATASET_VALUE_BANDIT {"value": true}`（X-Ops-Token）。worker 每进程 refresher 60s 内传播（重启已首刷）。
5. **首次 seed**（不等 05:15）：`celery -A backend.celery_app call backend.tasks.run_dataset_weight_refresh`。
6. **校验**：`bandit_state` 行数 = 有历史的数据集数；`datasets.mining_weight` pv1 最低；`system_configs` 有 watermark。

## 观察（v1 成功指标，≥1 周）

- pv1 真 sim 占比↓（结构性挤出）
- 欠挖正交源 sim 占比↑
- 新可提交 alpha 的 self-corr 分布不退（多样性）
- **不**看 submittable↑ —— 那是 phase-2 残差 reward 目标

每日 05:15 beat 自动滚动折扣更新。随时 `python scripts/dataset_bandit_dryrun.py` 看当前后验。

## ⚠️ 已知行为（非 bug，Thompson 探索偏置）

`_get_datasets_to_mine` 的 `ORDER BY mining_weight DESC LIMIT 10` 是硬截断：
- 欠挖源（Beta(1,1)+floor≈0.5）权重 > proven 低产源（Beta(10,870)≈0.03）→ **proven 产出源（fundamental6/analyst4）可能被挤出 top-10**，这一周近乎不挖。
- 零历史数据集未进 bandit → 仍 `mining_weight=1.0` → 霸占池顶。

如太激进的缓解：(B) bandit-gated 放宽候选池 LIMIT（让权重而非硬截断导流）；(C) 给零历史源也 seed Beta(1,1)。

## 回滚（缺一不可）

```
1. flag OFF:  PATCH /ops/flags/ENABLE_DATASET_VALUE_BANDIT {"value": false}
              （停 beat 写 + FLAT 回等概率 round-robin）
2. 恢复权重:  跑 docs/dataset_bandit_mining_weight_backup_2026-05-22.sql
              （候选池 ORDER BY mining_weight 无条件读它，仅 flag OFF 不够）
```
