# Cascade stuck in T2 phase — known bug

> ✅ **RCA + FIX DONE 2026-05-11** — commit b6a6c97 (pg advisory-lock guards run_mining_task)。原因不是 round_plan 或 T1 phase 逻辑,是**3 workers 并发跑同一 task**。Redis 队列堆了 40 个 pending `run_mining_task.delay(384)`(每次手动 resume + 每 5min watchdog beat revive 都加一个)。Workers restart 后多 worker 同时拿活,每个独立读 task.cascade_phase 各自跑独立 cascade 循环。修法:pg_try_advisory_lock(task_id) 入口守卫。

> 原始发现:Layer 1 长期验证(60min)发现 cascade_round_idx 自 13→14 transition 后停滞 1h46m+,期间 86 个 T2 STRATEGY_SELECT 调用都在并发 T2 phase 内,T1 phase 完全未触发。

## 现象

```
task 384 自 02:27 UTC RUNNING:
  cascade_phase = T2 (常驻)
  cascade_round_idx = 14 (1h46m 不变)
  
trace_steps 类型分布:
  STRATEGY_SELECT (T2):    86
  TIER_WRAP:               86
  TIER_SEED_LOAD:          18
  RAG_QUERY (T1):           0
  HYPOTHESIS (T1):          0
  CODE_GEN (T1):            0
```

期望: 每个 cascade round = T1 phase + T2 phase + T3 skip。CASCADE_T2_ROUNDS 默认 ~10,意味着 86 T2 calls 应该对应 8-9 个完整 round,round_idx 应 13→22。实测 round_idx 仅 13→14。

## 根因假说

| 假说 | 验证方法 |
|---|---|
| **A. CASCADE_T2_ROUNDS 配置 >> 10** | grep `CASCADE_T2_ROUNDS` 在 config.py / 实际值 |
| **B. T2 phase 内部 retry 循环** | 读 `_run_cascade_phase(tier=2)` + 看是否有 retry 路径 |
| **C. round_plan 数量计算错** | `rounds_per_ds = max(1, max_rounds // len(datasets))` — 多 dataset 时可能 expand 太多 |
| **D. while True 外层 loop 重复调 T2 而不进 T3** | 读 `_run_continuous_cascade` 的 phase 跳转逻辑 |
| **E. async exception 吞 + 重试** | grep retry / try-except 包 _run_cascade_phase 调用 |

最高怀疑: **C** — 若 T2 phase 有 30 datasets,`max_rounds // 30 = 0` 但 max(1, 0) = 1 per ds → 30 round_plan items。86 calls / 30 ≈ 3 个 T2 phase 调用 stuck。

## 影响

| 路径 | 受影响? |
|---|---|
| Layer 1 ε-greedy in T2 wrapper | ✅ 22.1% fire rate 正常工作 |
| Layer 1 dedup blacklist | ✅ max_bl 21,正常累积 |
| db_dup rate | ✅ 76.3%(降 14pp vs 90% baseline)|
| **Layer 1 ε-greedy in T1 phase** | ❌ **未能验证**,T1 phase 不跑 |
| **Diversity-aware RAG retrieval(T1 effect)** | ❌ **未能验证** |
| Family monoculture 完整破除 | 🟡 部分(7 alpha 跨 3 family,但来自 T2 wrap 不同 seeds)|
| V-19 cascade re-dispatch | ✅(watchdog 不需要救)|
| V-22 BRAIN feedback chain | ✅(brain_check_at 持续填)|

## 修复优先级

🟠 **中** — 不阻塞当前 cascade 出 alpha,但堵住 Layer 1 完整验证 + 让 T1 phase 永远不跑 = T1 PASS pool 静态化 = 长期 T2 wrapper 重复同样 seed 池。

## 修复路径(预估 0.5-1 day)

1. **诊断**: 加 `logger.info` 打到 `_run_cascade_phase` 入口 + 出口,跑 30 min 看 phase 真实运行次数
2. **打开 config**: 检查 `CASCADE_T2_ROUNDS`、`CASCADE_T1_ROUNDS`、`CASCADE_T3_ROUNDS`、`CASCADE_ENABLE_T3` 实际值
3. **修 round_plan 计算**: 若假说 C 命中,加 `max(1, min(max_rounds, max_rounds // max(1, len(datasets))))` 但更可能要把 `len(datasets)` 上限到 `max_rounds` 总数
4. **加 cascade phase 自检 metric**: trace_step 加 `phase_entry_count` 字段方便后续监控
