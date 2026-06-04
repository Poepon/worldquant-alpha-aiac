# P0+P1+P2 路线图跨阶段回顾 (AlphaGBM/skills 落地)

> 调研日期:2026-05-07 ~ 2026-05-16(10 天)
> 调研对象:AlphaGBM/skills 路线图 15 项 + 1 项测试基础设施清理
> 目的:为后续 ops 部署、新 contributor 上手、未来调研做参考的实施复盘
> 配套调研文档:[`docs/alphagbm_skills_research_2026-05-15.md`](alphagbm_skills_research_2026-05-15.md)

---

## § 0 速览(Quick Scan)

**一句话定性**:AlphaGBM/skills 26 个 Claude Code Skills 调研产出落地为 **11 个可工程化的 opt-in 能力**,通过严格的对抗审查机制与 byte-for-byte 不变性保护,在 0 prod 路径改动 / 0 baseline 漂移的前提下完成 15 项路线图交付。

| 维度 | 数字 |
|---|---|
| 调研期 | 2026-05-07 → 2026-05-16,共 10 天 |
| 主功能 commit | **15 项**(P0×5 + P1×6 + P2×4)+ 4 项 fix/refactor follow-up |
| 实际 LOC | **+24115 / -1094**(净 +23021) |
| 改动文件 | 134 个文件 |
| 测试新增 | **480+**(P0 ~60-80 / P1 ~150-180 / P2 ~134) |
| 对抗审查累计 MUST FIX | **30 个**(P2-B 7 + P2-D 5 + P2-A 11 + P2-C 7) |
| 新 opt-in flag | **11 个**(全默认 OFF) |
| 新 daily Celery beat | **6 个**(08:00-10:30 Asia/Shanghai 单调) |
| 新 docs/<topic>/ JSON 目录 | **6 个**(每日 sh-date 报告) |
| Alembic head | `c9d4b1a82e57`(P2-B 后保持不变) |
| `test_suite --regression` baseline | **post-cleanup 7 metric 全零漂移**(`82aa317` 后;期间 3 metric 因 pre-existing 循环导入未跑) |

**关键交付物**:
- 15 个主功能 commit + 4 个 follow-up fix(对齐 § 1.5 总表 19)
- 一份调研文档 + 本回顾文档
- 6 个每日报告(在 flag flip 后启动)
- 一份分 4 周渐进 ops onboarding 顺序(§ 5)

---

## § 1 路线图 15 项全景

### § 1.1 P0(5 项,~2.7K LOC,2026-05-07)

来源 skill `vol-surface` / `bps-backtest` / `take-profit` / `iv-rank` / `hedge-advisor`。

| 优先级 | commit | 标题 | 文件 | +行 / -行 | 落地文件 |
|---|---|---|---|---|---|
| 🔴 P0 | [`07f8944`](#) | 拟合基线 + Nσ残差挖矿筛选(vol-surface) | 7 | +540 / -5 | `multi_fidelity_eval.py`、新 screener |
| 🔴 P0 | [`753589a`](#) | 多保真严格化 — 静态检查前移到 simulate 之前 | 5 | +469 / -183 | `static_alpha_checks.py` 前置 lookahead/divide/overfit |
| 🔴 P0 | [`78938c1`](#) | 集中化多档路由 + tier-aware score 阈值(iv-rank/hedge-advisor) | 5 | +494 / -139 | `alpha_routing.py` Band A/B/C/D + `tier_thresholds.py` |
| 🔴 P0 | [`e57259b`](#) | 分层保真抗过拟合 — genetic_optimizer 晋级网格确认(take-profit) | 2 | +602 / -64 | `genetic_optimizer.py` 多次跑取中位数 |
| 🔴 P0 | [`d36656e`](#) | signal-vs-control 双跑归因(bps-backtest) | 5 | +622 / -0 | `evaluation.py` 内联块 + AttributionType |

**P0 小计**:5 commit,~2.7K LOC,**奠定信号验证 + 抗过拟合 + 多档路由基础**。

### § 1.2 P1(6 主功能 + 2 fix/refactor,~10.6K LOC,2026-05-08)

来源 skill `fear-score` / `market-sentiment` / `pnl-simulator` / `health-check` / `investment-thesis` / `company-profile` / `options-strategy`。

| 优先级 | commit | 标题 | 文件 | +行 / -行 | 落地文件 |
|---|---|---|---|---|---|
| 🟡 P1 | [`c8df434`](#) | P1-A 百分位归一化 + 非均匀权重 + confidence(fear-score/market-sentiment) | 6 | +856 / -33 | `alpha_scoring.compute_graded_score` + `diversity_tracker` |
| 🟡 P1 | [`fb67ff6`](#) | P1-B fallback 降级 + per-alpha try/except + post-loop tally(fear-score) | 3 | +791 / -511 | `graph/nodes/evaluation` + `metrics_tracker.py` |
| 🟡 P1 fix | [`81c87ad`](#) | P1-B 联动 fallback→confidence + PENDING 单桶 + lazy import 清理 | 2 | +81 / -49 | follow-up |
| 🟡 P1 | [`6a9dd47`](#) | P1-C(part 1) 定时 Alpha 库体检(stale/drift/orphan,5 档健康带)(health-check) | 7 | +1928 / -0 | `alpha_health_service.py` + 08:00 SH beat |
| 🟡 P1 refactor | [`3d6aaba`](#) | P1-C(part 1) drift severity → worst-of(sharpe+fitness) | 2 | +155 / -39 | follow-up |
| 🟡 P1 | [`9044483`](#) | P1-C(part 2) Hypothesis 结构化触发器 + active→triggered + LLM 评分(investment-thesis) | 14 | +3487 / -7 | `hypothesis_health_service.py` 5 trigger T1-T5 + audit 表 + 08:30 SH beat |
| 🟡 P1 | [`d6f3abb`](#) | P1-D What-if 参数扰动鲁棒性检验 RobustnessGate(pnl-simulator) | 8 | +1738 / -2 | `multi_fidelity_eval.RobustnessGate` window 邻近 N=4 |
| 🟡 P1 | [`2cd6c46`](#) | P1-E 结构化语义校验红旗 + 风险边界预标注(company-profile/options-strategy) | 6 | +1544 / -109 | `alpha_semantic_validator.Finding{rule_id,severity,message,category}` + 4 类静态 risk 推断 |

**P1 小计**:6 主 commit + 2 fix/refactor,~10.6K LOC,**评分体系 + 健康检查 + 鲁棒性 + 语义校验 4 维全面成熟**。

### § 1.3 P2(4 项,~10.7K LOC,2026-05-15 → 2026-05-16)

来源 skill `compare` / `take-profit`+`health-check` / `macro-view` / `vix-status`+`duan-analysis`。

| 优先级 | commit | 标题 | 文件 | +行 / -行 | 落地文件 |
|---|---|---|---|---|---|
| 🟢 P2-B | [`4ec6e8f`](#) | Five Pillars 因子分类保证 alpha 池均衡覆盖(compare) | 16 | +2030 / -26 | `Hypothesis.pillar` 列 + `pillar_classifier.infer_pillar` + `diversity_tracker` 5 维 + 09:00 SH `pillar_balance_check` |
| 🟢 P2-D | [`6cae5f5`](#) | negative knowledge 沉淀 + 标准化复盘 superset(take-profit/health-check) | 13 | +3503 / -1 | `FailureSignature` 6 类信号 → `knowledge_entries.entry_type=FAILURE_PITFALL` + 修复 `prompts/hypothesis.py:208` dead reference + `v26_retrospective.py --full` + 09:30 SH beat |
| 🟢 P2-D fix | [`e8f1845`](#) | min_failure_count_to_promote 默认 2 + curator deactivate 守卫 | 2 | +119 / -3 | follow-up |
| 🟢 P2-A | [`5a72da0`](#) | field→经济机制 RAG 引导生成 macro-view(macro-view) | 19 | +2799 / -4 | `MacroNarrative` 三 scope + 11 条种子 + LLM 离线批生成 + `RAGService.get_macro_narratives` parallel pipeline + 10:00 SH beat |
| 🟢 P2-C | [`d99e4c1`](#) | regime-aware 阈值门控 + 风格 preset 编码(vix-status/duan) | 18 | +2329 / -3 | 5 档 `RegimePreset` 倍率叠加 + `RegimeInferenceService` 7 天 EWMA + `PromptContext.style_preset` + 10:30 SH beat |

**P2 小计**:4 主 commit + 1 fix,~10.8K LOC,**生成引导 + 因子分类 + 复盘 + regime 感知 4 个高阶能力**。

### § 1.4 测试基础设施清理(2026-05-16)

| commit | 标题 | 文件 | +行 / -行 |
|---|---|---|---|
| 🔧 [`82aa317`](#) | 测试基础设施清理 — pytest 循环导入 + test fixtures 同步 | 4 | +28 / -4 |

修 3 个 pre-existing 问题:`evaluation.py:44` top-level `backend.tasks` import(唯一残留循环根源)、`test_upsert_new_pitfall` fixture 同步、`test_robustness_evaluation` 8 个 fail。意外恢复 3 个 `test_suite --unit` metric(`category_inference` / `failure_classification` / `pattern_count`)— 此前因循环导入跑不全。

### § 1.5 累计统计

| 阶段 | commit | LOC + | LOC - | 净 LOC | 主要价值 |
|---|---|---|---|---|---|
| P0 | 5 | 2727 | 391 | +2336 | 信号验证 + 多档路由基石 |
| P1 主 | 6 | 10344 | 662 | +9682 | 评分 + 健康 + 鲁棒 + 语义 4 维 |
| P1 fix | 2 | 236 | 88 | +148 | P1-B/P1-C 微调 |
| P2 主 | 4 | 10661 | 34 | +10627 | 生成引导 + 因子 + 复盘 + regime |
| P2 fix | 1 | 119 | 3 | +116 | P2-D 守卫修补 |
| Tech debt | 1 | 28 | 4 | +24 | 测试基础设施 |
| **总计** | **19** | **24115** | **1182** | **+22933** | **134 文件** |

---

## § 2 七大共享工程模式

跨 P0/P1/P2 反复验证的可移植设计模式。按出现次数排序,每个带反面教训。

### § 2.1 Opt-in flag default OFF(出现 11 次,P2 系列每个项目都有)

**规则**:新加 nudge / threshold adjust / data-collect 都用 `ENABLE_*` 设置,**默认 False**,1-2 天观察期后 ops 才 flip。

**正例**:
- `ENABLE_PILLAR_AWARE_SELECTION` (P2-B `4ec6e8f`):pillar nudge 默认 OFF;`Hypothesis.pillar` stamp 总写(数据采集 vs 注入分离,模式 § 2.3)
- `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` (P2-D `6cae5f5`):flag=False 时 `failure_pitfalls=state.pitfalls[:5]` 与现状字节完全一致(byte-for-byte 模式 § 2.2)
- `ENABLE_MACRO_NARRATIVE_GUIDANCE` + `ENABLE_MACRO_NARRATIVE_EXTRACT`(P2-A `5a72da0`):**两 flag 分离**控制 prompt 注入 vs LLM 批生成
- `ENABLE_REGIME_AWARE_THRESHOLDS` + `ENABLE_STYLE_PRESET_GUIDANCE` + `ENABLE_REGIME_INFERENCE`(P2-C `d99e4c1`):**三 flag 分离**

**反面教训(P2-A M9)**:Plan agent 原版提议 `ENABLE_MACRO_NARRATIVE_EXTRACT=True` 默认开,被对抗审查 catch — "零 LLM 成本不等于零 ops 风险",首日部署会放 LLM 数千 call。强制改回 False。

**反面教训(P2-C MF-S1)**:Plan agent 提议 `ENABLE_REGIME_INFERENCE=True`(数据采集零成本),对抗审查 catch — 即便零成本也违反"与既有 P2 项目惯例对齐"。改回 False,加 onboarding 说"7 天数据攒够后 flip"。

### § 2.2 Byte-for-byte legacy preserved + field assertion test(P2-A/B/C/D 全有)

**规则**:flag OFF 时 prod 路径**逐字节**与改前相同;test 验证用 `PromptContext.<field> is None / []` + `"_stamp" not in primary_h` field assertion,**不**用 prompt 字符串相等。

**反面教训**(出现 3 次):
- **P2-D M5**:`test_flag_off_byte_for_byte_legacy` 原版用 `prompt_off == prompt_legacy` 字符串相等。`build_hypothesis_prompt` 模板含动态 `{trace_section}` / `{strategy_section}` / `{pillar_nudge_block}`,字符串相等必失败。改 field assertion。
- **P2-A M8**:Plan 原版重复同款错误。改 field assertion。注释 `test_node_hypothesis_negative_knowledge.py:185-187` 写"frozen prompt text is infeasible — field assertion is the only viable invariant test for byte-for-byte"。
- **P2-C MF4**:Plan agent 在已知前两次教训情况下又写了一次相同的 frozen prompt 相等比较,对抗审查再次 catch。

**byte-for-byte gate 模板**(prompt 模板插入):
```python
macro_block_with_leading_newline = (
    f"\n{macro_context_block}\n" if macro_context_block else ""
)
# 模板 f-string 用 {macro_block_with_leading_newline}
# 空 string 时模板渲染字节级等同改前
```

### § 2.3 数据采集 vs 注入分离(P2-B M8 / P2-C MF-S2 显式架构)

**规则**:数据级 stamp(`Hypothesis.pillar` / `Alpha.metrics["_regime_at_eval"]` 等)**总是写**,与 `ENABLE_*_NUDGE` / `ENABLE_*_THRESHOLDS` 这些 prompt/threshold 注入 flag **独立**控制。

**正例**:
- **P2-B**:`Hypothesis.pillar` 在 LLM emit + `infer_pillar` 静态兜底时**总 stamp**(数据采集);`pillar_hint` 注入 prompt 由 `ENABLE_PILLAR_AWARE_SELECTION` 控制(决策注入)
- **P2-C**:`Alpha.metrics["_regime_at_eval"]` 在 regime 已注入到 strategy 时**总 stamp** + `_regime_applied_thresholds` 仅当倍率应用时 stamp(数据采集);prompt 是否含 Investment Philosophy 段由 `ENABLE_STYLE_PRESET_GUIDANCE` 单独控制

**价值**:flag OFF 期仍有完整 audit 数据可分析,**不需要回填**就能事后回看"regime 切换是否影响 PASS 率"等问题。

**反面教训(P2-C S2)**:Plan 原版描述 stamp"independent of two enable flags",但代码实际是"仅当 regime 注入时 stamp"。对抗审查发现 plan 自相矛盾,统一为"前提是任一 effect flag 开启触发 mining_agent fetch regime,从而 strategy.regime 非 None,stamp 才触发"。

### § 2.4 Inline `_pg_reachable()` skipif(P2-A M3 教训)

**规则**:每个 PG-only integration test 文件**顶部 inline** 5 行 boilerplate,**不依赖** conftest 共享 fixture。

```python
import os, socket
def _pg_reachable() -> bool:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT", "5433"))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False
pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Test requires Postgres reachable for JSONB ops",
)
```

**反面教训(P2-A M3)**:Plan agent 原版假设有 `conftest.py` 共享 helper,对抗审查 catch — `backend/tests/integration/` **没** conftest,既有 P2-D / P2-B 测试都自己 inline。Plan 改"6 个新 integration test 文件每个 inline"。

**适用范围**:`fetch_*` / SQL `meta_data->>'<key>'` / `pg_insert` 等 PG-only 路径的 integration test。

### § 2.5 lazy import 防循环(`backend.agents ↔ backend.tasks`)

**规则**:`backend/agents/*` 内对 `backend.tasks.*` 的引用**必须**在函数体内 lazy import。

**历史**:`backend.tasks.__init__:27` import `mining_tasks`,而 `mining_tasks:16` 又 `from backend.agents import MiningAgent` — 这是项目级循环,2026-05 多次出现:

- **P2-B M9**:对抗审查 catch — `node_hypothesis` 内 `MacroNarrativeService` / `redis_pool.get_redis_client` 必须 lazy import
- **P2-D**:同款,`NegativeKnowledgeService` lazy
- **P2-A M10**:`RAGService.get_macro_narratives` 内 `MacroNarrativeService` lazy + `MacroNarrativeService._infer_category` 内 `infer_dataset_category` lazy(双层)
- **P2-C MF2**:`mining_agent.run_mining_iteration` 用 `self.db` 而非新建 `AsyncSessionLocal()`(避破 V-26.79 session 污染)

**循环 contained(`82aa317`)**:`evaluation.py:44` 唯一残留 top-level `from backend.tasks.session_watchdog import _quota_guard_async` 移回 L2064 函数体内 lazy import(此前由 `1dab299` review-fix 时移到 top,理由"无循环风险";`82aa317` 因 test collection 顺序敏感再移回——top↔lazy 反复一次,反映项目级反模式)+ `backend/tests/conftest.py` 加 `import backend.tasks # noqa` warmup 兜底。**结构性循环图保留**(`backend/tasks/__init__:27 → mining_tasks:16 → MiningAgent`),仅通过 lazy + warmup 错开 import 顺序——评估为 "contained" 而非 "eliminated"。意外恢复 6 个此前 collect fail 的 test 文件。

### § 2.6 0 Alembic / 0 新表 / 0 新 index(P2-A/B/C/D 全部)

**规则**:利用既有 JSONB 字段 + Redis 状态 + String 列(无 CheckConstraint),**避免 schema 改动**。

**正例**:
- **P2-B**:加 `Hypothesis.pillar` 列(**唯一** Alembic 改动,head 从 `b2c5d8e1a9f4` → `c9d4b1a82e57`)
- **P2-D**:复用 `KnowledgeEntry.entry_type` String(50) 加 `FAILURE_PITFALL`(无 CheckConstraint,**0 schema 改动**);失败信号全存 `Alpha.metrics` JSONB
- **P2-A**:复用 `KnowledgeEntryType` 加 `MACRO_NARRATIVE`(同上 0 schema 改动);叙事文本存 `KnowledgeEntry.meta_data` JSONB
- **P2-C**:**全部用 Redis + 文件 + 既有 JSONB**,0 schema 改动(`_regime_at_eval` stamp 存 `Alpha.metrics`,regime 状态存 Redis 24h TTL)

**Alembic head**:从 P2-B 起保持 `c9d4b1a82e57` 不变,**P2-A/C/D 都 0 migration**。

### § 2.7 倍率叠加而非替换(P2-C 设计核心)

**规则**:对既有阈值系统(`tier_thresholds`)做调整时,**乘倍率**而非替换数值。

**例**(P2-C):`apply_regime_multipliers(tier_thresholds, regime)` 把 sharpe_min / fitness_min / turnover_max / score_pass 各乘 RegimePreset 的倍率;normal regime multiplier=1.0 → 字面无变化。

**价值**:
- 保留既有 T1/T2/T3 阈值 6+ 周经验校准
- 不破坏既有 routing 测试断言
- regime=None 走 identity passthrough,等价于"P2-C 不存在"

**反面教训(P2-C MF6)**:Plan agent 原版 `apply_regime_multipliers` 把 `score_optimize` 也乘倍率。对抗审查 catch — "score_optimize 应保持稳定不随 regime 移动,维持 OPTIMIZE 下边界稳定;PASS 与 OPTIMIZE 间距随 regime 自然收缩/扩张"。改为仅乘 4 个核心阈值,`score_optimize` 故意不动。

---

## § 3 对抗审查 Lessons Learned

**累计 30 个 MUST FIX**(P2-B 7 + P2-D 5 + P2-A 11 + P2-C 7),按类型分类:

### § 3.1 API/schema 假设错(9 个)

Plan agent 凭训练数据假设字段,对抗审查需逐个查 file:line VERIFY。

| 项目 | MUST FIX | 错假设 | 实际现状 |
|---|---|---|---|
| P2-A M11 | `infer_dataset_category(dataset_id)` 接 string | Plan 想传 `DataField.dataset_id` (Integer FK) | 真实是 BRAIN string id,需要 JOIN `DatasetMetadata.dataset_id` |
| P2-C MF1 | `alpha_health JSON.records[].current_sharpe` 顶层 | 实际仅 `signals.drift.current_sharpe` (`alpha_health_service.py:188/231`) | 删除 `sharpe_avg_7d` 字段简化 |
| P2-A M2 | `AlphaFailure.alpha_id` 存在 | `backend/models/alpha.py:134-168` 无此列 | 改用 `MiningTask.region` outerjoin 拿 region |
| P2-D M2 | aiosqlite 支持 `meta_data->>'key'` | aiosqlite 不识别 PG JSONB 运算符 | 强制 integration test PG-only + `_pg_reachable` skipif |

**教训**:对抗审查必先 grep / read 每个被引用的字段定义,assertive 语气("verified at file:line, current code does X")替代"Plan assumes"。

### § 3.2 SQL/JSONB aiosqlite 不兼容(3 个)

- **P2-D M2**:`meta_data ? 'key'` / `->>` / `pg_insert.on_conflict_do_nothing` 全是 PG-only
- **P2-A M3**:同上,且必须**inline** `_pg_reachable()` 而非依赖共享 conftest
- 对应措施:所有 JSONB SQL 路径的 integration test 都加 `pytestmark.skipif(not _pg_reachable())`

### § 3.3 byte-for-byte 保护漏洞(6 个)

| 项目 | MUST FIX | 漏洞 |
|---|---|---|
| P2-D M5 | frozen prompt 相等比较 | 模板含动态段 → 改 field assertion |
| P2-A M8 | 同上重复 | 同改 |
| P2-C MF4 | 同上 3 次 | 同改 |
| P2-A M9 | `ENABLE_MACRO_NARRATIVE_EXTRACT=True` 默认 | 改 False(token 预算保护) |
| P2-C MF-S1 | `ENABLE_REGIME_INFERENCE=True` 默认 | 改 False(与项目惯例对齐) |
| P2-A M5 | 模板 splice 时缺 leading newline 条件 gate | `f"\n{block}\n" if block else ""` 字节级保护 |

### § 3.4 设计/语义错(7 个)

| 项目 | MUST FIX | 错 |
|---|---|---|
| P2-B M4 | weight 重归一化破 P1-A baseline | 保留老 4 维 default,pillar 单独叠加 |
| P2-A M11 | vwap 种子 mechanism mean-reversion + transmission "趋势加强" 自相矛盾 | 统一 mean_reversion 路径 |
| P2-C MF6 | `score_optimize` 也乘 regime 倍率 | 故意不乘,维持 OPTIMIZE 下边界 |
| P2-D M3 | `v26_retrospective.py --full` 破坏 legacy CLI | ADDITIVE 分支,legacy 4 路径字面不动 |
| P2-D M4 | Pydantic 与 `json.dumps` 路径混用 | Pydantic 只走 `--full` 内 |
| P2-D S1 | `success_count` 用作 pitfall "downweight" | 不更(epistemic 错;pitfall warning 本身是成功的) |

### § 3.5 数据库 / 测试隔离(5 个)

| 项目 | MUST FIX | 漏洞 |
|---|---|---|
| P2-C MF2 | `AsyncSessionLocal()` 新建独立 session | 改用 `self.db` 复用 mining session(V-26.79 防污染) |
| Tech debt (`82aa317`) | `test_robustness_evaluation` `_patch_block` 没 override `BRAIN_DAILY_SIMULATE_LIMIT` | 加 `99999` 绕开 quota guard |
| Tech debt | `_FakeAsyncRedis` mock 用旧 key `"aiac:robustness_today_used"` | 改 prefix match `"aiac:robustness_used:"`(P2 日期分桶后改名) |
| P2-D S6 | 同款 fixture 反序列化错 | `_make_sig` default 与 service min_count 配对 |
| P2-D `e8f1845` | curator deactivate 被自动覆盖 | UPDATE 分支 created_by guard `"P2D_NEGKB"` |

---

## § 4 当前部署状态(运维参考)

### § 4.1 11 个 P0-P2 新 opt-in flag(全默认 OFF)

| Flag | 来源 P | 控制 | 启动顺序建议 |
|---|---|---|---|
| `ENABLE_SIGNAL_CONTROL_DUAL_RUN` | P0 `d36656e` | 双跑归因(额外 BRAIN simulate) | Week 4(慎,烧配额) |
| `ENABLE_GRADED_SCORE` | P1-A `c8df434` | 百分位评分 + confidence(数据采集) | **Week 2** |
| `ENABLE_ROBUSTNESS_CHECK` | P1-D `d6f3abb` | What-if 参数扰动(烧 BRAIN 配额) | Week 4 |
| `ENABLE_PILLAR_AWARE_SELECTION` | P2-B `4ec6e8f` | pillar nudge prompt 注入 | **Week 3** |
| `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` | P2-D `6cae5f5` | failure_pitfalls prompt 注入 | **Week 3** |
| `ENABLE_MACRO_NARRATIVE_EXTRACT` | P2-A `5a72da0` | LLM 离线批生成 field 叙事 | **Week 1**(数据采集) |
| `ENABLE_MACRO_NARRATIVE_GUIDANCE` | P2-A `5a72da0` | macro_narratives prompt 注入 | **Week 3** |
| `ENABLE_REGIME_INFERENCE` | P2-C `d99e4c1` | 7 天 pass_rate 推 regime(数据采集) | **Week 1** |
| `ENABLE_REGIME_AWARE_THRESHOLDS` | P2-C `d99e4c1` | regime 倍率应用到 tier_thresholds | **Week 4** |
| `ENABLE_STYLE_PRESET_GUIDANCE` | P2-C `d99e4c1` | Investment Philosophy prompt 注入 | **Week 3** |
| `MULTI_FIDELITY_ENABLED` | P0 `753589a` | 多保真严格化(quick/medium/full) | Week 4 |

**注**:`backend/config.py` 还有 ~19 个**既有**基础 flag(`ENABLE_FACTOR_TIERING` / `ENABLE_T2_SELF_CORR_CHECK` / `ENABLE_PRE_SIMULATE_FILTER` 等)默认 ON,不在此表。

### § 4.2 6 个 daily Celery beat(Asia/Shanghai 单调链)

```
08:00 alpha-health-check         → docs/alpha_health_check/<sh-date>.json
08:30 hypothesis-health-check    → docs/hypothesis_health_check/<sh-date>.json
09:00 pillar-balance-check       → docs/pillar_balance/<sh-date>.json       [P2-B]
09:30 negative-knowledge-extract → docs/negative_knowledge/<sh-date>.json   [P2-D]
10:00 macro-narrative-extract    → docs/macro_narratives/<sh-date>.json     [P2-A]
10:30 regime-infer               → docs/regime_state/<sh-date>.json         [P2-C]
                                   + Redis aiac:current_regime:{region} 24h TTL
```

**注**:序时单调,后者依赖前者输出(regime_infer 在 10:30 跑,确保 alpha_health 数据已 settle)。

**早期 beat**(06:00-07:00):`sync-datasets` / `refresh-kb-referenced-alphas` / `refresh-os-correlation-cache` / `monitor-llm-op-hallucinations`(非 P0-P2 范畴)。

### § 4.3 6 个 docs/<topic>/<sh-date>.json 输出目录

| 目录 | 来源 P | 内容 |
|---|---|---|
| `docs/alpha_health_check/` | P1-C-1 | per-alpha health_score (0-100) + 5 档 band |
| `docs/hypothesis_health_check/` | P1-C-2 | T1-T5 trigger 状态 + LLM thesis_score |
| `docs/pillar_balance/` | P2-B | 5 pillar 分布 + deficit + skew |
| `docs/negative_knowledge/` | P2-D | top-20 failure patterns + by_category counts |
| `docs/macro_narratives/` | P2-A | 种子 + LLM batch counters + by_source |
| `docs/regime_state/` | P2-C | 全 region regime snapshot + EWMA history |

**注**:flag OFF 时 task 不跑(P2-A 例外:种子 idempotent 写但仍受 `ENABLE_MACRO_NARRATIVE_EXTRACT` 控制),目录有但无 JSON。

### § 4.4 Alembic / baseline / regression

- **Alembic head**:`c9d4b1a82e57`(P2-B 后保持不变;P2-A/C/D + Tech debt 都 0 migration)
- **baseline.json** 7 metric 零漂移:
  - `syntax_validation_accuracy` 0.833 → +0.000
  - `threshold_checks_passed` 1.000 → +0.000
  - `category_inference_accuracy` 1.000 → +0.000 ←(`82aa317` 后恢复)
  - `failure_classification_accuracy` 0.833 → +0.000 ←(恢复)
  - `mutation_validity_rate` 1.000 → +0.000
  - `diversity_logic_correct` 1.000 → +0.000
  - `pattern_count` 10.000 → +0.000 ←(恢复)
- **480+ 新测试** 全 pass(P2-A 24 / P2-B 49 / P2-C 36 / P2-D 25 + P0/P1 ~330)

---

## § 5 Ops Onboarding — 分 4 周渐进 flag flip 顺序

**核心原则**:数据采集 → 评分 → prompt 注入 → 阈值调整,**按副作用从轻到重**渐进开启。每周观察 baseline drift + nudge 响应率 + BRAIN 配额。

### Week 1:数据采集启动(零 prompt/threshold 副作用)

```env
ENABLE_REGIME_INFERENCE=True
ENABLE_MACRO_NARRATIVE_EXTRACT=True
```

**期望**:`docs/regime_state/` 每日产出(从冷启动 `"normal"` 渐到真实推断);`docs/macro_narratives/` 增量 LLM 批生成填长尾(种子 11 条立即写入 KB)。

**监控**:`docs/regime_state/<date>.json` 内 `confidence > 0.6` + `cold_start == False` 转持续;LLM token 用量 ≤ `MAX_TOKENS_PER_DAY` 50%。

**baseline drift**:零(纯数据采集,不影响主流程)。

### Week 2:评分增强(数据采集,不影响 routing)

```env
ENABLE_GRADED_SCORE=True
```

**期望**:`Alpha.metrics["graded_score"]` / `graded_score_grade` / `_score_confidence` 开始 stamp;routing 仍走旧 `SCORE_PASS_THRESHOLD=0.8`(P0-78938c1 多档路由)。

**监控**:`graded_score` 中位数与 P1-A baseline 一致(±5%)。

**baseline drift**:零(`ENABLE_GRADED_SCORE` 只影响数据采集层,not routing)。

### Week 3:prompt 注入(影响 LLM 生成 hypothesis 偏好)

```env
ENABLE_PILLAR_AWARE_SELECTION=True
ENABLE_NEGATIVE_KNOWLEDGE_NUDGE=True
ENABLE_MACRO_NARRATIVE_GUIDANCE=True
ENABLE_STYLE_PRESET_GUIDANCE=True
```

**期望**:hypothesis prompt 同时含 4 个 nudge 段(pillar / failure_pitfalls / macro / style)。`primary_h["_pillar_nudged"]` / `_negative_knowledge_pitfalls_seen` / `_macro_narratives_seen` / `_regime_style_seen` 4 个 stamp 出现率上升。

**监控**:
- **nudge 响应率**:每个 stamp 在 1000 个 hypothesis 中出现的频率 > 30%
- **prompt 长度**:不超过 5000 token(4 个 nudge 段合计 ≤ 1500 token)
- **PASS 率漂移**:Week 3 末与 Week 2 比较,PASS 率 ±10% 内
- **LLM JSON 解析失败率**:不上升

### Week 4:阈值调整(影响 PASS/FAIL 判定)

```env
ENABLE_REGIME_AWARE_THRESHOLDS=True
ENABLE_ROBUSTNESS_CHECK=True
ENABLE_SIGNAL_CONTROL_DUAL_RUN=True   # 慎(烧 BRAIN 配额 2x)
MULTI_FIDELITY_ENABLED=True            # 慎(同上)
```

**期望**:`Alpha.metrics["_regime_at_eval"]` + `_regime_applied_thresholds` + `_robustness_failed` + `_signal_control_attribution` 同时出现。PASS/PROVISIONAL/OPTIMIZE/FAIL 各档比例随 regime 显著变化。

**监控**:
- **BRAIN 配额**:每日 `today_total` 不超 `BRAIN_DAILY_SIMULATE_LIMIT * 0.85`(robustness hot-check 阈值)
- **PASS 率**:与 Week 3 比较 ±15%(crisis regime 应放宽,very_calm 应收紧)
- **`baseline.json`**:`test_suite --regression` drift 不超 5%;若超 → 1000+ alpha 数据后回看 + 刷 `--save-baseline`

### Onboarding 监控速查命令

```powershell
# 每日报告
Get-Content docs/regime_state/2026-MM-DD.json | ConvertFrom-Json | Select-Object -ExpandProperty regions
Get-Content docs/pillar_balance/2026-MM-DD.json | ConvertFrom-Json | Select-Object -ExpandProperty totals

# nudge 响应率(每周末跑)
psql -c "SELECT count(*) FILTER (WHERE thesis_score IS NOT NULL) FROM hypotheses WHERE created_at > now() - interval '7 days';"
psql -c "SELECT meta_data->>'_pillar_nudged' AS pillar, count(*) FROM hypotheses WHERE ... GROUP BY 1;"

# baseline drift
python backend/tests/test_suite.py --regression

# v26_retrospective superset(P2-D)
python -m scripts.v26_retrospective --window-hours 168 --full   # 周报
```

---

## § 6 未来工作(P3 待办,本期已识别)

按价值/复杂度排:

| 优先级 | 项 | 来源 | 价值 |
|---|---|---|---|
| 🟢 P3-A | **two-stage RAG**(hypothesis 后 LLM 选 key_fields 第二阶段 field-level 经济叙事) | P2-A 显式 defer | 高 — macro_narratives field-level scope 真正实现 |
| 🟢 P3-D | **监控 dashboard 前端**(6 个 JSON 报告可视化) | P0-P2 留白 | 高 — ops 体验从命令行升前端 |
| 🟢 P3-B | **LLM 评 pitfall confidence**(替代 P2-D 固定 dict) | P2-D 显式 defer | 中 — KB 质量持续校准 |
| 🟢 P3-C | **`nudge_ignored_count` 审计**(LLM 看 pitfall 但仍重犯) | P2-D 显式 defer | 中 — 量化 nudge 有效性 |
| 🟢 P3-E | **per-user / per-task preset override**(P2-C 全局 regime → 用户级) | P2-C 显式 defer | 中 — 多用户协作场景 |
| 🟡 P3-F | **外部 VIX/US10Y API 拉**(P2-C 排除,只用 pass_rate 代理) | P2-C 排除项 | 低 — 代理已工作,真 VIX 是 nice-to-have |
| 🟡 P3-G | **regime fail_count 衰减**(P2-D 14 天硬 cutoff → EWMA 衰减) | P2-D S2 defer | 低 — 长期 KB 质量优化 |
| 🟡 P3-H | **跨 region preset**(P2-C 全 region 共享 → 分 region preset) | P2-C 简化 | 低 — 多市场扩展需求 |

**不承诺时间表**,只列已识别项。

---

## § 7 附录 — 文档与 memory 索引

### § 7.1 项目内文档

- 调研原文:[`docs/alphagbm_skills_research_2026-05-15.md`](alphagbm_skills_research_2026-05-15.md)
- 本回顾:`docs/retrospective_p012_2026-05-16.md`(本文档)
- v26 retrospective superset:`scripts/v26_retrospective.py --full`(P2-D)
- daily 报告:`docs/{alpha_health_check, hypothesis_health_check, pillar_balance, negative_knowledge, macro_narratives, regime_state}/<sh-date>.json`
- 历史 V-26 backlog:`docs/v26_retrospective/`
- V-27 code review:`docs/code_review_v27_fixes_2026-05-14.md`

### § 7.2 内部 memory(`~/.claude/projects/<project>/memory/`)

- `reference_alphagbm_skills_research`:调研产出位置 + P0-P2 commit 索引
- `feedback_alpha_submission_criteria`:三道门(can_submit + self_corr<0.7 + IQC Δscore>0)
- `feedback_plan_workflow`:plan mode 标准流程(Explore → ROI → Plan agent → 对抗审查)

### § 7.3 关键源文件清单(按 P 项)

| P 项 | 主要源文件 |
|---|---|
| P0 baseline+Nσ | `backend/multi_fidelity_eval.py` |
| P0 多保真严格化 | `backend/static_alpha_checks.py` + `backend/agents/graph/nodes/validation.py` |
| P0 多档路由 | `backend/alpha_routing.py` + `backend/agents/graph/tier_thresholds.py` |
| P0 genetic 中位数 | `backend/genetic_optimizer.py` |
| P0 signal-control | `backend/agents/graph/nodes/evaluation.py`(内联块) |
| P1-A 评分 | `backend/alpha_scoring.py` + `backend/diversity_tracker.py` |
| P1-B fallback | `backend/agents/graph/nodes/evaluation.py:_safe_metric` |
| P1-C-1 Alpha 体检 | `backend/services/alpha_health_service.py` + `backend/tasks/alpha_health_check.py` |
| P1-C-2 Hypothesis 触发器 | `backend/services/hypothesis_health_service.py` + `backend/tasks/hypothesis_health_check.py` |
| P1-D RobustnessGate | `backend/multi_fidelity_eval.py:RobustnessGate` |
| P1-E Finding | `backend/alpha_semantic_validator.py:Finding/RuleId` |
| P2-B Five Pillars | `backend/pillar_classifier.py` + `backend/services/hypothesis_service.py`(pillar 字段)+ `backend/tasks/pillar_balance_check.py` |
| P2-D negative knowledge | `backend/negative_knowledge.py` + `backend/services/negative_knowledge_service.py` + `backend/tasks/negative_knowledge_extract.py` + `scripts/v26_retrospective.py --full` |
| P2-A macro narratives | `backend/macro_narratives.py` + `backend/services/macro_narrative_service.py` + `backend/tasks/macro_narrative_extract.py` + `backend/agents/prompts/macro_narrative.py` |
| P2-C regime-aware | `backend/regime_classifier.py` + `backend/services/regime_inference_service.py` + `backend/tasks/regime_infer.py` |

### § 7.4 关键 commit SHA 速查

| SHA | 标题 |
|---|---|
| `07f8944` | P0 baseline + Nσ残差 |
| `753589a` | P0 多保真严格化 |
| `78938c1` | P0 多档路由 |
| `e57259b` | P0 genetic_optimizer 中位数 |
| `d36656e` | P0 signal-vs-control 双跑 |
| `c8df434` | P1-A 百分位评分 |
| `fb67ff6` | P1-B fallback 降级 |
| `81c87ad` | P1-B fix(fallback→confidence) |
| `6a9dd47` | P1-C-1 Alpha 体检 |
| `3d6aaba` | P1-C-1 refactor(drift severity worst-of) |
| `9044483` | P1-C-2 Hypothesis 触发器 |
| `d6f3abb` | P1-D RobustnessGate |
| `2cd6c46` | P1-E 结构化语义校验 |
| `4ec6e8f` | P2-B Five Pillars |
| `6cae5f5` | P2-D negative knowledge + 复盘 superset |
| `e8f1845` | P2-D fix(min_count + curator 守卫) |
| `5a72da0` | P2-A macro-view |
| `d99e4c1` | P2-C regime-aware + 风格 preset |
| `82aa317` | 测试基础设施清理(循环导入 + fixtures) |

---

## § 8 总结

**项目交付**:AlphaGBM/skills 26 skill 调研价值悉数转化为 11 个 opt-in 能力,15 项路线图全部实施,**0 prod 改动 / post-cleanup 7 metric baseline 全零漂移 / 2 次 Alembic 改动(P1-C-2 加 audit 表 + 9 列;P2-B 加 1 列;P0/P1-A/B/D/E/P2-A/C/D 全部零 migration)**。

**方法论沉淀**:7 个跨 P2 共享工程模式(opt-in default OFF / byte-for-byte field assertion / 数据采集 vs 注入分离 / inline _pg_reachable / lazy import / 0 Alembic 设计 / 倍率叠加)+ 30 个对抗审查 MUST FIX 复盘,可作为未来 P3 / V-28+ 工作的范本。

**ops 路径**:分 4 周渐进 flag flip,从数据采集 → 评分 → prompt 注入 → 阈值调整,每周观察 baseline drift + nudge 响应率 + BRAIN 配额。

**待办**:P3-A/B/C/D/E/F/G/H 共 8 项已识别,按价值排序,不承诺时间表。

---

*本文档由跨 P0+P1+P2 retrospective 工作流自动生成 — Plan agent + 对抗审查 + 实施 agent 三段串行,与既有 P 项交付流程同款。*
