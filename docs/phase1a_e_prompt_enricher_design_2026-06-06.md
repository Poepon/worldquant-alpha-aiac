# Phase 1a-E 设计 checkpoint — node_hypothesis 8 nudge 块 → PromptContextEnricher

> 状态:**设计稿,待用户批准后实现**(task #10)。地基:grounding wf_041f3cee + 设计 wf_c39afd8f(3 路对真实代码取证)。
> 目标:把 `generation.py:node_hypothesis`(~485-1108)里 8 个 inline nudge 块抽成 `PromptContextEnricher` 策略 + orchestrator,**单 HG session 复用**(现状每块各开 session),node_hypothesis 收敛到 ~30 行。**行为字节级保持**(11 个测试不改即过)。

## 1. 现状(8 块实测)

| 块 | 行 | flag(全 `ENABLE_` 前缀,settings.X 安全)| 自开 session? | PromptContext 输出 | 额外副作用 |
|---|---|---|---|---|---|
| P2-B pillar | 485-591 | `ENABLE_PILLAR_AWARE_SELECTION` | resolve_db(config) | `pillar_hint` | redis 60s |
| P2-D neg-kb | 603-675 | `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` | resolve_db(config) | `failure_pitfalls`(与 state.pitfalls 折叠)| redis 300s + 后置 stamp `_negative_knowledge_pitfalls_seen` |
| P2-A macro | 687-771 | `ENABLE_MACRO_NARRATIVE_GUIDANCE` | resolve_db(config) | `macro_narratives` | redis(可配 TTL)+ 后置 stamp `_macro_narratives_seen` |
| P2-C style | 783-808 | `ENABLE_STYLE_PRESET_GUIDANCE` | **否**(config 读)| `style_preset` | 后置 stamp `_regime_style_seen` |
| G8 forest | 821-896 | `ENABLE_HYPOTHESIS_FOREST_REUSE` | AsyncSessionLocal | `cross_task_hypotheses` | state `g8_forest_referenced_ids` |
| R8-v3 cognitive | 906-979 | `ENABLE_COGNITIVE_LAYER_PROMPT` | 条件(bandit 模式)| `cognitive_layer_block`+`cognitive_layer_id` | state `cognitive_layer_id_used` |
| G10 distilled | 985-1020 | `ENABLE_G10_LOGIC_INJECT` | AsyncSessionLocal | `distilled_logic_block` | state `g10_injected_entries_n` |
| ORTH | 1027-1050 | `ENABLE_ORTHOGONAL_PROMPT_STEERING` | AsyncSessionLocal | `submitted_pool_profile`(via `_orth_steer_block or None`)| — |

- **唯一跨块依赖**:`pillar_hint`(P2-B 产)被 **G8(:855)/R8-v3(:963)/G10(:1000)** 用作 fetch 过滤。P2-D/P2-A/P2-C/ORTH 互相独立。
- **所有块**:`getattr(_gen_settings, FLAG, False)` 门控 + try/except 非致命 → flag-OFF/fetch-fail 均退化到 legacy 默认(`None`/`[]`/`""`)。
- **ORM→dict**:G8/R8-v3/G10/P2-B 在赋值前已把 ORM 行转 dict(无 DetachedInstance 风险)——抽取后必须保持。
- **后置 stamp**(P2-D/P2-A/P2-C):`_*_seen` 写在 **LLM 返回后** 的 hypothesis 对象上,非 PromptContext。
- **入口 reset**:`cognitive_layer_id_used`(:454)、`g8_forest_referenced_ids`(:828)、`g10_injected_entries_n`(:987)无条件重置——防跨轮残留,必须保留。

## 2. 设计

### 2.1 `HypothesisEnrichment`(累加器 / "local vars 袋")
一个 dataclass,字段默认值 = legacy(flag-OFF)值。承载三类输出:PromptContext 绑定字段 + state 变更 + 后置 stamp 数据。
```python
@dataclass
class HypothesisEnrichment:
    # → PromptContext
    pillar_hint: Optional[str] = None
    neg_kb_pitfalls: List[Dict] = field(default_factory=list)
    macro_narratives: List[Dict] = field(default_factory=list)
    style_preset: Optional[Dict] = None
    cross_task_hyps: List[Dict] = field(default_factory=list)
    cognitive_layer_block: str = ""
    cognitive_layer_id: str = ""
    distilled_logic_block: str = ""
    orth_steer_block: str = ""
    # → state 变更
    g8_referenced_ids: List[int] = field(default_factory=list)
    cognitive_layer_id_used: str = ""
    g10_injected_entries_n: int = 0
    # → LLM 后 hypothesis stamp
    neg_kb_keys_seen: List[str] = field(default_factory=list)
    macro_keys_seen: List[str] = field(default_factory=list)
    p2c_regime: Optional[str] = None
```

### 2.2 `PromptContextEnricher` 协议 + 8 个具体类
```python
class PromptContextEnricher(Protocol):
    key: str            # 'P2-B' ...
    enable_flag: str    # 'ENABLE_PILLAR_AWARE_SELECTION'
    async def enrich(self, state, config, db, acc: HypothesisEnrichment) -> None: ...
```
- 每个 enricher = 把对应 inline 块**逐字搬入** `enrich()`,读 `acc.pillar_hint`(G8/R8-v3/G10),写自己的 `acc.*` 字段。
- enricher **不 import generation.py**(避循环);import 各 service(lazy in `enrich`)+ `nodes/base.py` 的 `resolve_db`。
- 自身 try/except 保持(非致命),redis/logging 逐字保留。

### 2.3 Orchestrator(单 session + 源序 + 错误隔离)
```python
class HypothesisEnricherOrchestrator:
    def __init__(self, enrichers):  # 源序: P2-B,P2-D,P2-A,P2-C,G8,R8-v3,G10,ORTH
        self.enrichers = enrichers
    async def run(self, state, config) -> HypothesisEnrichment:
        acc = HypothesisEnrichment()
        async with resolve_db(config) as db:          # ★ 单 session(测试注入则复用,同今)
            for e in self.enrichers:
                if not getattr(_gen_settings, e.enable_flag, False):
                    continue                            # flag-OFF → 默认 → 字节级 legacy
                try:
                    await e.enrich(state, config, db, acc)
                except Exception as ex:
                    logger.warning(f"[{e.key}] enrich failed (non-fatal): {ex}")
                    await db.rollback()                 # 清 aborted-txn,后续 enricher 不被污染
        return acc
```
- **源序**保留(P2-B 先,满足 pillar_hint 依赖;其余按现行顺序,redis/log 顺序不变)。
- **单 session**:orchestrator `resolve_db(config)` 开一次传所有 enricher(取代现状 prod 下 7 次开)。read-only + 单协程串行 → F1 安全;失败 enricher 后 `rollback()` 防共享事务污染。

### 2.4 node_hypothesis 收敛(~30 行)
```python
enrichment = await HypothesisEnricherOrchestrator(_ENRICHERS).run(state, config)
state.g8_forest_referenced_ids = enrichment.g8_referenced_ids
state.cognitive_layer_id_used = enrichment.cognitive_layer_id_used
state.g10_injected_entries_n = enrichment.g10_injected_entries_n
prompt_context = PromptContext(
    ...,  # 静态字段同今
    failure_pitfalls=((enrichment.neg_kb_pitfalls + (state.pitfalls or []))[:5]
                      if enrichment.neg_kb_pitfalls else state.pitfalls[:5]),
    pillar_hint=enrichment.pillar_hint,
    submitted_pool_profile=(enrichment.orth_steer_block or None),
    macro_narratives=enrichment.macro_narratives,
    style_preset=enrichment.style_preset,
    cross_task_hypotheses=enrichment.cross_task_hyps,
    cognitive_layer_block=enrichment.cognitive_layer_block,
    cognitive_layer_id=enrichment.cognitive_layer_id,
    distilled_logic_block=enrichment.distilled_logic_block,
)
prompt = build_hypothesis_prompt(prompt_context, ...)   # ★ 同点调用 → spy 测兼容
# ... LLM call ...
# 后置 stamp 用 enrichment.neg_kb_keys_seen / macro_keys_seen / p2c_regime(逻辑同今)
```
- flag 门控移入 enricher(返回默认即 legacy),故 node 直接用 `enrichment.X`(无需再 `if _enabled` 三元)。

## 3. 行为保持契约(必满足 → 11 测不改即过)

- **8 条 flag-OFF 不变量**:flag OFF → 对应字段 = `None`/`[]`/`""`(enricher 默认),build_hypothesis_prompt 渲染空串 = 字节级 legacy。
- **fetch-fail 非致命**:每 enricher try/except 退化到默认(P2-D 回 `state.pitfalls[:5]` 等)。
- **state reset 保留**:三个入口 reset 不动(放 node_hypothesis 函数入口,先于 orchestrator)。
- **后置 stamp 仅 flag-ON+有数据时出现**:`_*_seen` 由 enrichment 字段驱动,逻辑同今。
- **build_hypothesis_prompt 同点调用**:spy 测(patch.object inspect ctx)看到同样的 PromptContext。
- **service 注入兼容**:enricher 收 db / llm_service(来自 config),与测试 mock 注入点一致。
- **flag-check 必执行**(test_node_hypothesis_orthogonal_node 验 node 双态都到 LLM)→ orchestrator 的 `getattr` 门控不 NameError。
- **redis key/TTL/log 逐字保留**(测试不查 log 内容,但保真)。
- **无行号依赖**:测试全字段级/spy/DB 断言 → 重构行号位移不破测。

## 4. 文件布局 + 序

- 新 `backend/agents/graph/nodes/prompt_enrichers.py`:`HypothesisEnrichment` + `PromptContextEnricher` 协议 + 8 enricher 类 + `HypothesisEnricherOrchestrator` + `_ENRICHERS` 列表。import services(lazy)+ base.resolve_db;**不 import generation.py**。
- 改 `generation.py:node_hypothesis`:删 8 块(~560 行)→ orchestrator 调用 + PromptContext 装配 + 保留入口 reset / 后置 stamp / build_prompt 调用。
- 实现序(逐 enricher 搬+测,降风险):P2-C(最简,无 session)→ P2-B(pillar,被依赖)→ P2-D/P2-A → G8/R8-v3/G10 → ORTH → orchestrator 接线 → node 收敛 → 全测。
- 验证:11 个 test_node_hypothesis_* / forest / orthogonal 全过 + `--regression` 0 漂移 + 全 unit 0 新失败 + 对抗审查。

## 5. 风险 + 缓解

| 风险 | 缓解 |
|---|---|
| 单 session 共享事务被失败 enricher 污染 | 失败后 `db.rollback()`(read-only,rollback 无损);见 §2.3 |
| pillar_hint 顺序依赖破坏 | orchestrator 固定源序(P2-B 先);加注释 + 顺序断言 |
| ORM 跨 session 关闭 DetachedInstance | enricher 返回 dict 非 ORM(现状已如此,保持) |
| resolve_db(config) 开一次 vs 每块:注入 session 退出被关 | 实现时核 resolve_db 语义(注入 session 不应在 `__aexit__` 关);测试用注入 session 必绿 |
| 后置 stamp 漏迁 | enrichment 显式带 `*_keys_seen` / `p2c_regime`;node 后置逻辑逐字 |
| **〔实现纠正 2026-06-06,读全源后〕单 session 非全 7→1** | **session 来源分三类**:(a)P2-B/P2-D/P2-A 用 `resolve_db(config)` → 直接用 orchestrator 共享 db ✓;(b)**G8/R8-v3-bandit/G10 用 `AsyncSessionLocal()`** 开各自新 session → 改用共享 db 时**必须逐块核对该块的测试 mock 是否仍拦截**(测试在 **service 层** mock fetch_*,故大概率 OK;若有测试 patch `backend.database.AsyncSessionLocal` 则该块保留自开或适配);(c)**ORTH 特殊**:`compute_submitted_pool_profile(AsyncSessionLocal, region)` 传的是 **session 工厂**非 session,helper 内部自管 → **ORTH 保留工厂调用不改**(字节级安全)。⇒ 实际 7→**2**(1 共享 + ORTH 自管),仍达标。R8-v3 bandit session 仅 bandit 模式开(条件)。 |
| G8/G10/R8-v3 切共享 db 破坏 patch AsyncSessionLocal 的测试 | 逐块核对:test_g8_hypothesis_forest mock `fetch_cross_task_promoted`(service 层)→ 安全;wiring 时跑该块测试确认,破则该块回退自开 session |

## 6. 待你拍(开工前)

1. **单 session 合并(§2.3)做不做?** 推荐 **做**(plan 目标「不每块开 session」;read-only 串行 + rollback 守卫安全)。保守 fallback:先纯结构抽取、每 enricher 仍各开 session(零行为变),session 合并留后续。
2. **文件位置**:`agents/graph/nodes/prompt_enrichers.py`(贴近 generation.py)vs `agents/services/`。推荐前者(它 import nodes/base)。
3. **一个 PR 还是逐 enricher 多 commit**:推荐**一个 PR**(8 块互相替换,避免新旧并存的 redis 撞键);内部按 §4 序逐个搬+本地测。
