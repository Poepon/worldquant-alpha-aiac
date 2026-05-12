# V-22 系列总报告

**生成时间:** 2026-05-13
**作用范围:** alpha mining 工程链路(BRAIN 提交 gate / OS 相关性 / composite 路径 / LLM prompt / dataset 选择 / final-pool stratification / fields propagation)
**结论一句话:** **8 个版本闭环了 IQC 边际贡献检测 → 多字段 composite 端到端 mining → 已成功提交 1 个 +341 Δscore 的 V-22.6.3 decay composite alpha (pk=7810)。fund composite SAVE 路径被 BRAIN User-tier 数据可发现性限制,等 Consultant 升级后再实测。**

---

## 系列定位

### 问题起点

2026-05-10 IQC 提交期 spike 审计:13/13 net-positive Δscore 的候选 alpha 全部被 BRAIN /correlations/SELF 拦截(corr 0.85-0.99 vs OS portfolio),无法提交。深挖发现:

1. **OS portfolio 严重饱和于 returns-reversal** 单字段反向变体(`ts_decay_linear(returns, 5)` 系列)
2. **现有 mining 池 75% can_submit 都是 returns-reversal**(48 候选中 36 个)
3. **submitted portfolio 实际只 11% 是 returns-reversal**,主力是 T2 group-wrapped + analyst + intraday composite(共 78%)
4. **真正能加分的"新主题"alpha 在 mining 池里几乎不存在**

V-22 系列的核心使命:**让 mining 产出能加分的多样化 alpha,而非堆栈同质化候选**。

### 工程主轴

```
V-22.5: 拦住已饱和方向          (T2 self-corr gate 启用)
   ↓
V-22.6.0: 引入多字段 composite   (突破单字段瓶颈)
   ↓
V-22.6.1: bare 形(去 preprocess)(BRAIN 8-op 限制)
   ↓
V-22.6.2: 2-input ts ops 过滤    (避免 SELF_CORRECT 死循环)
   ↓
V-22.6.3: ts_decay_linear 包装   (turnover 0.82 → 0.22)
   ↓
V-22.6.4: prompt 强制 PV 锚字段  (LLM ingredient 配对)
   ↓
V-22.6.5: composite final-pool 配额 33%(stratified bucket 平衡)
   ↓
V-22.6.6: PV 字段防 strategy 误杀(strategy evolution edge case)
   ↓
V-22.7: BRAIN auth body-marker 检测(401 silent fail 修复)
```

---

## 各版本详记

### V-22.5 — T2 OS self-corr gate(2026-05-11 已 deploy)

**变更:**
- `backend/agents/graph/tier_thresholds.py` 启用 `ENABLE_T2_SELF_CORR_CHECK = True`
- `backend/agents/graph/nodes/evaluation.py` 在 T2 PASS gate 前增加 OS portfolio self-corr 检查
- `scripts/v22_5_backfill_t2_self_corr.py` 回溯标记历史 can_submit=True 的 T2 alpha

**意义:** Mining 时就拦掉与 OS 高自相关的 T2 候选,避免它们 PASS 后污染 KB + 进 submission 队列尾端再被 BRAIN 拒。

**实测:**
- 回溯 75 个历史 T2 PASS alpha,**37 个 (49.3%) downgrade 为 PASS_PROVISIONAL**
- 后续 mining 的 T2 候选直接被 self-corr 拦截而非 PASS → submit → reject 的浪费链路

---

### V-22.6.0 — Composite-field 多字段融合 mining

**变更:**
- `backend/agents/seed_pool/composite_fields.yaml` 15 个 composite 定义(VALUE×4 / QUALITY×4 / INTRADAY×3 / GAP×2 / LIQUIDITY×2)
- `backend/agents/seed_pool/composite_fields.py` loader + `generate_composite_t1_candidates`
- `backend/factor_generation.py` `expand_t1_strategy` 加 V-22.6 分支
- `backend/factor_tier_classifier.py` 加 `_is_t2_composite` + `_peel_composite_preprocess`
- `backend/config.py` 加 `COMPOSITE_T1_ENABLED` 等 4 个 tunable

**核心理念:** BRAIN `ts_op(field, w)` 只接受单字段输入,要表达 PE = close/eps、accrual = cfo/ni 等 multi-field 信号必须**先做算术合成再 ts_op 包装**。Composite 模板预先定义 → 字段可用性 gate → 自动枚举 `ts_op × window` 变体。

**实测:**
- 195 单测全绿(`test_factor_tier_classifier.py` 增 19 个 V-22.6 case)
- USA TOP3000 fundamental6 dataset 全 25 个 ingredient 字段确认存在
- Classifier 透明 peel 机制:`ts_op(winsorize(ts_backfill(quasi_t1, W), std=S), w)` → T2
- Bare form `ts_op(quasi_t1, w)` 也 → T2(后由 V-22.6.1 转为默认)

---

### V-22.6.1 — Bare 形 + 默认关闭 preprocess(2026-05-12 1:00)

**问题:** Spike round 1 task 528 发现 BRAIN 8-operator complexity limit,**完整 wrap 12 ops over limit**(`ts_op + winsorize + ts_backfill + divide + subtract + ts_delay + ts_delay = 7 ops`,加 outer 和 args 总 9-13 ops)。SELF_CORRECT 试图修复但产 `let(...)` 幻觉,死循环 3 次后放弃。

**变更:**
- `COMPOSITE_T1_APPLY_PREPROCESS: bool = False` 默认关闭 preprocess wrap
- `generate_composite_t1_candidates` 加 `apply_preprocess` 参数
- 简化形式:`ts_op(<composite>, w)` 替代 `ts_op(winsorize(ts_backfill(<composite>, W), std=S), w)`

**实测:**
- 2-leg composite (pe_synth, divide(close, eps)) = **2 ops** ✓
- 3-leg composite (intraday_range, divide(subtract(high,low), close)) = **3-4 ops** ✓
- 5-leg 极端(overnight_gap with ts_delay) = **5 ops** ✓(全部 ≤ 8 ops)
- 196 单测全绿(+ 5 个 `TestCompositeFieldsLoader`)

**Commit:** a695013 `fix(t1): V-22.6.1 hotfix — drop preprocess wrap by default`

---

### V-22.6.2 — 过滤 2-input ts ops

**问题:** Spike task 530 round 1 发现 LLM 选 `ts_regression` 进 `preferred_ts_ops`,loader naive 发 `ts_regression(<composite>, 20)` 但 BRAIN 实际签名是 `ts_regression(y, x, d)` 三参数。SELF_CORRECT 复制 composite 当 y → 9 ops 超 8-op 限 → 幻觉 `let(...)` → 死循环。

**变更:**
- `TWO_INPUT_TS_OPS = frozenset({"ts_corr", "ts_regression", "ts_covariance"})`
- `generate_composite_t1_candidates` 在 cartesian 前先 filter ts_ops

**实测:** 198 单测全绿(+ 2 个 filter 测试)

**Commit:** 6ab7950 `fix(t1): V-22.6.2 — filter two-input ts ops from composite enumeration`

---

### V-22.6.3 — ts_decay_linear turnover dampener

**问题:** V-22.6.1 PV composite spike 出 4 个 PROV alpha sharpe 1.81-2.35 但 **turnover 全 0.81-0.82**,BRAIN can_submit gate 一律 < 0.7,卡 submit。

**变更:**
- `COMPOSITE_T1_AUTO_DECAY_WRAPPER: bool = True` + `COMPOSITE_T1_AUTO_DECAY_VALUE: int = 4`
- 每个 composite 额外 emit 一个 `ts_decay_linear(<composite>, 4)` 变体
- 每个 composite decay variant 独占 stratified bucket `composite_decay4_<name>`(避免被 collision 摊薄)

**实测:**
- 单测 200 全绿(+ 2 个 V-22.6.3 测试)
- 实际 mining:
  - pk=7806 sh=1.66 to=**0.50** PASS can_submit=True — 第一个 V-22.6.3 可提交 alpha!
  - **pk=7810 sh=1.55 to=0.22 PASS can_submit=True — 已成功提交 IQC2026S1**
  - pk=7822 sh=1.67 to=0.50 PASS can_submit=True
  - pk=7825 sh=1.11 to=0.33 PROV(后续 V-22.6.6 验证 task)

**IQC marginal audit (pk=7810 submission 前):** 
- 39 candidates audit,**只 pk=7810 (Δscore=+341) 是 net-positive**
- 其余 38 个全负(-394 ~ -827),证实 returns-reversal monoculture

**Commit:** 54dd2a0 `feat(t1): V-22.6.3 — ts_decay_linear turnover dampener for composites`

---

### V-22.6.4 — Prompt: 强制 PV anchor 配料

**问题:** Layer 1 theme audit 发现 5 轮 fundamental6 RAG → 0 个 fund composite 落库。诊断:LLM `promising_fields` 是 fund-only 列表,**没包含 PV 锚字段 close/cap/vwap**,V-22.6 composite branch 的 `required_fields ⊆ available_fields` 检查失败 → fund composite 全部 skip。

**变更:** `backend/agents/prompts/strategy_prompts.py` `T1_STRATEGY_SYSTEM` 加 HARD rule:

> 当 `dataset_id` 是 fundamental / analyst 且选了 eps/ebit/enterprise_value/cash_flow_from_operations 等字段,**必须 ALSO 包含 close, cap, vwap 在 promising_fields**。这些 PV anchors 不挤占 budget(目标 8-15 字段,12 fnd + 3 PV 没问题)且 unlock 4-8 value/quality composite 免费枚举。

**实测:**
- LLM rationale 显式响应:"price-volume anchors (close/cap/vwap) to enable composite value/quality ratios"
- Composite emission 从 **7/15 (PV-only) → 10/15** (+43%)
- 但 stratified_sample 摊薄,final pool fund composite ratio < 6%

**Commit:** 79b3af1 `feat(prompt): V-22.6.4 — composite ingredient bridge + Layer 1 theme audit tooling`

---

### V-22.6.5 — Composite final-pool 配额

**问题:** V-22.6.4 fix emission 但 5 round × 21 candidates × ~10% PASS rate × ~30% fund 占比 → 期望 0.6 fund composite/round 落库,实测 0/5 → stratified_sample 把 composite 摊到 42 bucket 之一,fund composite 在 final pool 只 ~6%。

**变更:** `backend/factor_generation.py` `expand_t1_strategy` 末尾 stratified_sample 拆两次:
```
composite_quota = ceil(target_n × 0.33)
non_comp_quota = target_n - composite_quota
composite_candidates → stratified_sample(by="op", n=composite_quota)
non_composite → stratified_sample(by="op", n=non_comp_quota)
```

**实测:**
- 单测 200 全绿(+ 2 V-22.6.5 case)
- Spot check: daily_goal=6 target_n=9 → composite slots=3(33%),其中 **2/3 是 fundamental composite**
- 实际 task 534:fund composite 在 first 5 出现 2/5 rounds(`divide(close, eps)`)

**Commit:** f64fdd4 `feat(t1): V-22.6.5 — reserved composite quota in final candidate pool`

---

### V-22.6.6 — PV 字段防 strategy 误杀

**问题:** Task 534 round 2 VALIDATE 突然报 5 个 `Field 'close' not found in dataset` 失败(round 1 0 个)。深挖发现:
- `_prepare_round_fields` 正确返回 896 字段(886 fundamental + 10 universal_pv)
- `MiningAgent._apply_field_filters` 把 `avoid_fields` 中的字段**完全丢弃**
- Round 1 0 PASS 后,LLM 策略进化把 `close/cap/vwap` 加进 `avoid_fields`(认为聚焦 fundamental)
- Round 2 起 state.fields 不再含 close/cap/vwap → composite candidates 全部 VALIDATE fail

**变更:** `backend/agents/mining_agent.py:_apply_field_filters`
```python
_PROTECTED_PV = {close, open, high, low, volume, vwap, returns, cap, ...}
avoid_set -= _PROTECTED_PV     # strategy 不能 avoid PV
screened_set -= _PROTECTED_PV   # 不能 screen 排除 PV
return pv_anchors + screened/preferred + capped_others  # PV 永远在前
```

**实测:**
- 单测 4 个新 `TestPVProtected`,核心场景:strategy.avoid_fields=PV → 仍输出 close/cap/vwap
- 实际 task 535 round 2:`field_not_found_fails=1`(round 1 残留),round 2 first VALIDATE 0 新增 — fix 生效
- 保存 pk=7825 `multiply(-1, ts_decay_linear(divide(subtract(high, open), close), 4))` PROV,**high/open/close 三个 PV 都存活 V-22.6.6 保护**

**Commit:** d490be3 `fix(mining): V-22.6.6 — protect universal PV anchors from strategy filtering`

---

### V-22.7 — BRAIN auth body-marker 检测(并行修复)

**问题:** Spike task 530 round 3-5 全部 SIMULATE 0 alpha,worker 日志无 `401` 报错。BRAIN 实际返回 **非-401 status + body `{"detail":"Incorrect authentication credentials."}`**,旧 `_request` 只看 status_code==401 触发 re-auth → 不触发 → session 静默过期 → 烧 30min 0 产出。

**变更:** `backend/adapters/brain_adapter.py`
```python
def _is_auth_error(response):
    if response.status_code == 401: return True
    if response.status_code >= 400 OR body ≤ 2KB:
        return "Incorrect authentication credentials" in response.text
    return False
```

**实测:** 10 单测全绿(401 / 403 / 400 / 200-with-marker → True;normal 200/500/403-multisim/429/empty/big-body → False)。

**Commit:** 6f70f25 `fix(brain): V-22.7 — detect auth-error body marker, not just 401 status`

---

## 累计提交 alpha

| pk | brain_id | sharpe | turnover | submitted | composite type |
|---|---|---|---|---|---|
| pk=7806 | 58LbpOwo | 1.66 | 0.50 | ✗ | V-22.6.3 intraday_return × decay(4) sign-flip |
| **pk=7810** | **xAe6bxrp** | **1.55** | **0.22** | **✓ Δscore +341** | **V-22.6.3 intraday_return × decay(20) sign-flip** |
| pk=7822 | pwnA3O1q | 1.67 | 0.50 | ✗ | V-22.6 vwap_to_open variant × decay(4) |
| pk=7825 | (待 refresh) | 1.11 | 0.33 | ✗ | V-22.6.6 high_above_open variant × decay(4) |

**关键洞察:**
- 4/4 都是 **PV composite × ts_decay_linear × sign-flip** 模式
- pk=7810 (decay=20) 是唯一 +Δscore,decay=4 变体全负(与 OS portfolio decay4-family 高同质)
- **0 fund composite 落库**(User-tier 字段可发现性限制)

---

## IQC Marginal Audit 演化

| 日期 | candidates audited | net-positive | 备注 |
|---|---|---|---|
| 2026-05-11 12:29 | 9 (V-22.5 backfill cohort) | - | V-22.5 deploy 前 baseline |
| 2026-05-11 12:31 | 75 (扩展 cohort) | 1 (pk=7810 Δscore=+341) | 发现 pk=7810 是唯一可提交 |
| 2026-05-12 07:52 | 15 (after V-22.6.3 deploy) | 1 (pk=7810 同上) | V-22.6.3 deploy 验证 |
| 2026-05-12 08:13 | 39 (post-pk=7810 submission) | 0 | 提交后 portfolio 饱和 |
| 2026-05-12 22:55 | 20 (V-22.6.6 deploy 后) | 0 | best Δscore -1033 |

---

## Layer 1 Theme Audit 结果(2026-05-12)

**Submitted (n=9) vs Can-submit (n=48) vs All-pass (n=702):**

| Theme | submitted | can_submit | all_pass | gap |
|---|---|---|---|---|
| returns_reversal | 11.1% | **77.1%** | 20.4% | **-66 pp** mining over-produces |
| t2_group_wrapped | **44.4%** | 8.3% | 8.3% | **+36 pp** submitted favors T2 |
| analyst | 22.2% | 4.2% | 1.6% | **+18 pp** analyst under-mined |
| intraday_return | 11.1% (pk=7810!) | 6.2% | 1.0% | V-22.6.3 win |
| **All V-22.6 fund composites** | **0** | **0** | **0** | 完全 missing |

**结论:**
- mining 池 75% can_submit 是 returns_reversal → 跟 OS 高度重合 → 所有 39 候选 negative Δscore
- submitted 实际偏好 t2_group_wrapped + analyst + intraday_return + fundamental_other
- V-22.6 PV composite 路径已成功撕开 intraday_return 主题缺口(pk=7810)
- fund composite / analyst 主题需 Consultant tier 解锁

---

## User-tier 限制假设(2026-05-13 用户提出,关键)

**当前 BRAIN tier: User**(Consultant 申请中)

**实测可证的 tier 影响:**

1. **multi-simulation 已确认 User-locked**(`_no_multisim` flag triggered,fallback 到 single-sim 3x 慢)
2. **fundamental6 部分字段缺失:** `book_value_per_share_2` / `cash_flow_from_operations` / `net_income_total_2` 在 datafields 表中没记录(可能 Consultant-only data subset)
3. **字段命名差异:** LLM 在 User-tier 看到的 fundamental6 字段元数据可能用 `fnd6_*` 描述名,而 V-22.6 composite_fields.yaml 用 BRAIN canonical 短名(`eps`, `ebit`, `enterprise_value`)→ LLM 不选 → composite 不 fire

**Consultant tier 升级后预测:**

| 路径 | User tier | Consultant tier 预期 |
|---|---|---|
| PV composite (intraday/gap/liquidity, 7/15) | ✅ 已 verified | 同样工作 + sim 速度 3x |
| VALUE composite (pe_synth/earnings_yield/...) | ❌ 字段名不匹配 | 应工作,eps/ebit/ev 应是 standard |
| QUALITY composite (accrual/cfo_yield/...) | ❌ 关键字段 User 看不到 | 应工作 |
| analyst dataset 深挖 | ⏳ 1.6% all_pass | 大幅提升(submitted 池 22%)|
| event-driven mining | ⏳ 偶发 | 可能解锁 trade_when 真实信号 |
| 多 sim 并发 | 1 slot | 3 slot |

---

## 工程债务 / 未决问题

### 1. Stratified_sample bucket 摊薄(V-22.6.5 部分缓解)
**现状:** target_n=9 时 V-22.6.5 给 composite 3 slots = 33%。可调高到 50% 但会挤占 raw T1。
**等待信号:** Consultant 升级后看 fund composite 真实 PASS rate 决定是否再加 quota。

### 2. composite_fields.yaml 字段名 vs LLM picked 名不匹配
**现状:** yaml 用 BRAIN 短名(`eps`),LLM 选 `fnd6_*` 描述名。User-tier 下完全 mismatch。
**两条修复方向:**
- 改 yaml `required_fields` 用 prefix 匹配(`fnd6_*_eps_*`)— 风险:hit 错字段
- 改 DISTILL_CONTEXT 让 LLM 看到 `eps` 等短名 — 需 Consultant 元数据
**当前决定:** 等 Consultant 升级后判断短名是否本来就可见,不在 User tier 硬修。

### 3. SELF_CORRECT 跨 dataset 字段幻觉
**现状:** Run 485 round 1 LLM 生成 `opt8_put_call_ratio_30d` (option) 在 fundamental6 task 中。SELF_CORRECT 修复但浪费 BRAIN sim slot。
**未深挖:** 可能是 LLM context window 内还残留之前 round 的 KB pattern。

### 4. 8001 phantom socket(已绕过)
**现状:** Windows hibernation 后 port 8001 phantom listener,新 uvicorn 无法 bind。当前 backend 在 8002。
**绕过:** 不重启系统的话长期用 8002,frontend 如需要可改 vite proxy。

---

## 累计 push 历史 (V-22 系列 commits)

```
07f8718  V-22.5  T2 self-corr backfill
e29bf16  V-22.5  evaluation gate enable
28f59ed  V-22.4  L1 dedup blacklist tunables
0aafe09  V-22.6  composite-field pipeline (initial)
a695013  V-22.6.1  drop preprocess wrap
6ab7950  V-22.6.2  filter two-input ts ops
54dd2a0  V-22.6.3  ts_decay_linear dampener
6f70f25  V-22.7   BRAIN auth body marker
63ba07f  chore    IQC audit tooling
79b3af1  V-22.6.4  ingredient bridge + Layer 1 audit
7f57284  fix      SPECIFIC dataset_strategy 500
f64fdd4  V-22.6.5  composite final-pool quota
d490be3  V-22.6.6  protect PV anchors
```

11 commits, ~+1600 LOC backend + ~+700 LOC scripts/docs。

---

## 推荐下次 session 起步

1. **Consultant tier 通过日起:**
   - 重跑 task 535 (SPECIFIC=fundamental6 daily_goal=6),观察 fund composite TIER_WRAP first 5
   - 跑 IQC marginal audit:fund composite saved alpha 的 Δscore 是否正贡献
   - 验证 `eps`/`ebit`/`enterprise_value` 是否成 promising_fields 可选

2. **若 V-22.6 fund composite 落库且 +Δscore:**
   - submit 该 alpha 进 IQC
   - 跑 V-22 系列再回归 audit

3. **若仍 0 fund composite:**
   - 改 composite_fields.yaml 用 fnd6_* prefix 匹配机制(V-22.6.7)
   - 或扩 DISTILL_CONTEXT 让 LLM 看到所有候选 ingredient 名

4. **Backlog 优化(独立于 tier):**
   - 修 `_apply_field_filters` 字段池 cap(30 太低,可调 50-80)
   - 8001 phantom socket(重启 OS 释放)
   - SELF_CORRECT 跨 dataset 幻觉防护

---

**审计完成。等 Consultant 解锁后续。**
