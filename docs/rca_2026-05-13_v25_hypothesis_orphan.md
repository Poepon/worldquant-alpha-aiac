# V-25 RCA — "89% hypothesis orphan rate" 真相

**日期**: 2026-05-13
**触发**: V-24.A audit script 报告 89% (595/671) hypothesis 无 alpha 链接
**严重度**: 🟡 中 — 初稿严重程度判断过高,真实 bug 是三个独立小问题不是一个大 bug

---

## ⚠ 校准:89% 不是单一 bug,是三层独立成因

| 层 | 状态 | 数量 | 真实原因 |
|---|---|---|---|
| 历史污染 | SUPERSEDED | 290 | V-19.7 zombie cleanup 手动批量 transition(05-06)|
| 度量误读 | ACTIVE (orphaned) | 275 | hypothesis 试过了但 FAIL,FAIL alpha 不写 hypothesis_id 链 |
| 真实 bug | PROPOSED (orphaned) | 30 | V-22.13 跨轮复用偶尔失效,创建后立刻被替换 |

---

## 层 1 — 290 SUPERSEDED 是 zombie cleanup 历史

证据:**所有 290 SUPERSEDED 的 abandon_reason 都是同一字符串**:
> `V-19.7 zombie cleanup — pre-fix non-primary siblings never received alphas; transitioned ACTIVE → SUPERSEDED`

意味着这些行是 V-19.7 修复部署(05-06)时,**一次性手动批量 transition** 的"僵尸 ACTIVE"(早期多 sibling 设计的遗留)。**G-refine 路径从未产生过任何真实 SUPERSEDED**:

```sql
SELECT COUNT(*) FROM hypotheses WHERE parent_hypothesis_id IS NOT NULL;
-- 结果: 0/673
```

0 hypothesis 有 parent 链 → `mark_superseded` 的 child.parent_hypothesis_id 验证从未通过 → G-refine 从未真正 fire 过完整路径。

可能 G-refine LLM call 总是 fall back 到 mark_abandoned,但 mark_abandoned 路径也是 0(ABANDONED=0)。最可能解释:**G-refine 整个分支根本没被触发过**,因为 should_abandon_hypothesis 在生产中从未返回 True。

为什么 should_abandon_hypothesis 不返回 True?需要 `state.hypothesis_round_history[hid]` 累积 3 round 且 attribution=hypothesis × 0 PASS。这个**内存累积**依赖 V-22.13 跨轮复用 — 见层 3。

---

## 层 2 — 275 ACTIVE orphaned 是度量错位

audit script 的 "orphan" 定义:`alphas` 表里没有 hypothesis_id 指向该 hypothesis。

但**只有 PASS alpha 写 alphas 表**:
- PASS alpha → alphas table with hypothesis_id ✅
- FAIL alpha → **alpha_failures table**,**没有 hypothesis_id 列**:
```
alpha_failures cols: id, task_id, trace_step_id, run_id, expression,
                      error_type, error_message, raw_response,
                      is_analyzed, created_at
```

所以一个 hypothesis 跑了 10 个 alpha 全 FAIL,在 alphas 表里看是 0 行 → audit 报"orphan"。但 hypothesis 实际上**被试过了**,只是 0 PASS。

数据印证:
- 76 PROMOTED → 100% linked(都有 PASS,所以都有 alphas 表 row)
- 275 ACTIVE → 100% "orphan" by audit(都跑过 alpha 但 0 PASS)
- 30 PROPOSED → 100% "orphan"(根本没跑成 alpha,见层 3)

**ACTIVE 不是 bug** — 是 hypothesis 尝试了但没找到信号。这是 mining 的正常 noise。

**真 bug:alpha_failures 缺 hypothesis_id 列**。FAIL alpha 失去 hypothesis 归属,影响:
- attribution 分析(哪个 hypothesis 的实现 fail 多)
- B6 should_abandon_hypothesis 的 attribution 统计来源(目前用内存 pending_alphas,跨任务/重启失效)
- failure_pattern 学习链路断裂

---

## 层 3 — 30 PROPOSED orphaned 是 V-22.13 reuse 偶尔失效

PROPOSED 状态意味着:hypothesis 创建后**没有任何 alpha 被 mark_active 触发**。

正常流程:
1. generation 创建 hypothesis A → state.current_hypothesis_id = A
2. code_gen + simulate 产生 N alpha 候选(无论 PASS/FAIL)
3. persistence._process_hypothesis_feedback: alpha_count = len(pending_alphas) > 0 → mark_active(A) → A 变 ACTIVE

如果 A 卡在 PROPOSED:
- 要么 code_gen/simulate 失败,pending_alphas 为空
- 要么 task 在 mark_active 前 crash / 被 supersede 替代

实测今天 1 个 variant=2 task(task 551):13 hypothesis,只有 2 个 PROMOTED(linked alpha)。意味着另外 11 个 hypothesis 经历"创建后被替换"。

数据线索:
- task 533/535/551: alpha 数 == unique_hids_alpha_linked(每 alpha 一个新 hid)
- task 536: alpha=3, unique_hids=1(✅ V-22.13 reuse 工作,3 alpha 共享 1 hid)

**V-22.13 跨轮复用偶尔工作**。差别可能在:
- LangGraph state propagation(scalar field 可能跨节点丢失)
- Task pause/resume 重新初始化 MiningState → current_hypothesis_id 丢失
- node_hypothesis 内 V-22.13 路径异常 fall through

每次 reuse 失效就产生 1 个孤儿 PROPOSED + 1 个新 hypothesis。

---

## 推荐处理

### V-25.A 立即(0.25 day):**audit script 区分 "tried + 0 PASS" vs "never tried"**

`scripts/abandon_path_audit.py` 的 "orphaned" 度量误导。改成:
- `linked_with_pass`: alpha 表有 PASS 行
- `tried_no_pass`: alpha_failures 表有 task_id 但 alpha 表无 PASS(用 task_id 反推 hypothesis,因为 hypothesis 表无 task_id)
- `never_tried`: hypothesis 创建后 0 alpha 尝试

更细粒度的 orphan rate 让真实问题可见。

### V-25.B 中期(1 day):**alpha_failures 加 hypothesis_id 列**

```sql
ALTER TABLE alpha_failures ADD COLUMN hypothesis_id INTEGER
    REFERENCES hypotheses(id) ON DELETE SET NULL;
CREATE INDEX ix_alpha_failures_hypothesis_id ON alpha_failures(hypothesis_id);
```

backfill 暂不可行(历史 FAIL 无法关联),但新增 FAIL 写入路径加 hypothesis_id。让 attribution 链路完整。

### V-25.C 中期(1-2 day):**V-22.13 reuse 路径加诊断日志 + LangGraph state propagation 修复**

generation.py:463 加 log:
- `state.current_hypothesis_id` 在 round 入口的值
- V-22.13 check pass/fail 原因(state None / hypothesis SUPERSEDED / history >= N)
- 如 fail,是否 fall through 到创建新 hypothesis

7 天后用日志数据决定:
- 如 state propagation 丢失 → fix LangGraph state schema(scalar 字段加 reducer)
- 如 hypothesis 状态切换太快 → 调 V-22.13 status filter 接受更多状态

### V-25.D 低优先级(可选):**G-refine 路径生产可用性验证**

G-refine LLM call 是否真的运行?当前 0 hypothesis 有 parent_hypothesis_id,说明 G-refine 完整链路从未在生产环境跑通。可能:
- LLM JSON response 解析失败
- refine prompt 设计不对,LLM 总返回 give_up
- find_chain_depth 限制提前 cut

加日志看 G-refine 调用 LLM 的次数 + 各种 return 路径分布。但优先级不高 — 因为 should_abandon_hypothesis 在层 3 fix 前根本不 fire。

---

## 修订 Trigger 2 解读

之前修 Trigger 2 公式为 `(ABANDONED + SUPERSEDED) / total = 43.4%`,认为 G-refine 把 abandon 100% 转 SUPERSEDED。

**现在校准**:43.4% retirement rate 是历史 zombie cleanup 制造的伪数据。**真实 Phase 2 lifecycle retirement = 0/total = 0%**。

Trigger 2 度量应进一步过滤掉 V-19.7 zombie:
```sql
SELECT status, COUNT(*) FROM hypotheses
WHERE COALESCE(abandon_reason, '') NOT LIKE 'V-19.7 zombie%'
  AND created_at > NOW() - INTERVAL '14 days'
GROUP BY status
-- 真实: ACTIVE 276, PROMOTED 77, PROPOSED 30, SUPERSEDED 0, ABANDONED 0
```

更新 `scripts/phase3_trigger_monitor.py` 的 hypothesis_abandon_stats 加这个 WHERE 子句。

---

## 工时汇总

| Task | 工时 | ROI |
|---|---|---|
| V-25.A audit refine | 0.25 d | 度量准确 |
| V-25.B alpha_failures.hypothesis_id | 1 d | attribution 链完整 |
| V-25.C V-22.13 reuse 诊断 + fix | 1-2 d | 真减少 PROPOSED 孤儿 |
| V-25.D G-refine 生产验证 | 0.5 d | 可选 |
| Trigger 2 公式再校准 | 0.25 d | 度量诚实 |
| **合计** | **3-4 day** | 中等 ROI |

不紧急 — Phase 2 hypothesis 路径的最终 ROI 取决于 Phase 3 invert,V-25 系列是基础设施完整性 fix,不在阻塞路径上。
