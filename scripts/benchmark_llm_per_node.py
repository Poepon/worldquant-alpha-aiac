"""Per-node LLM benchmark — pick the best model for EACH pipeline node_key.

Unlike benchmark_llm_alpha_quality.py (one end-to-end alpha-gen scenario), this
drives **each real node_key's own production prompt builder** with a
representative fixture, parses its real output schema, and scores it with a
node-specific offline metric. Output → a per-node ranking + a ready-to-paste
`LLM_FUNCTION_MODEL_MAP` recommendation for the routing plan
(docs/per_function_llm_routing_plan_2026-05-29.md).

Coverage (core objective-scorable subset, per plan §3):
  expression-producing  → code_gen, self_correct, r1b_retry, llm_mutate_alpha,
                          llm_crossover_alpha   (semantic-validity + p_pass + diversity)
  semi-structured       → hypothesis, r1b_mutate (JSON schema + pillar + diversity)
  judge / consistency   → r5_alignment_c1, r5_alignment_c2, attribution
                          (verdict stability over K runs + correctness + schema)

Proxy-only, no BRAIN. Run:
    venv/Scripts/python.exe scripts/benchmark_llm_per_node.py
    venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --nodes code_gen,self_correct --runs 2
Writes docs/llm_per_node_benchmark_<date>_x<runs>.json + prints ranked tables.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Allow `python scripts/benchmark_llm_per_node.py` from any cwd — put repo root
# (parent of scripts/) on the path so `import backend...` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Candidate models — the exact 9 that produced the 2026-05-29 FINAL decision
# (docs/llm_per_node_benchmark_2026-05-29_FINAL.json + config.py defaults).
# Keep this list == the benchmarked population so a bare re-run reproduces the
# committed per-node picks; override a subset via --models. (All 9 verified
# available on Aliyun DashScope, zero fail → plan 放行条件 #1.)
# ---------------------------------------------------------------------------
MODELS = [
    "qwen3.7-max", "qwen3.6-plus", "qwen3.6-flash",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "kimi-k2.6", "kimi-k2.5", "glm-5.1", "glm-5",
]
RUNS_DEFAULT = 3
TEMPERATURE = 0.8
JUDGE_TEMPERATURE = 0.2   # judge/consistency nodes run cooler (match production)
CONSISTENCY_REPEATS = 4   # how many times to re-ask the same judge fixture

# ---------------------------------------------------------------------------
# Representative fixtures (static → fair + reproducible across models)
# ---------------------------------------------------------------------------
FIELDS_FIXTURE: List[Dict] = [
    {"id": "close", "type": "MATRIX"}, {"id": "open", "type": "MATRIX"},
    {"id": "high", "type": "MATRIX"}, {"id": "low", "type": "MATRIX"},
    {"id": "volume", "type": "MATRIX"}, {"id": "vwap", "type": "MATRIX"},
    {"id": "returns", "type": "MATRIX"}, {"id": "cap", "type": "MATRIX"},
    {"id": "sharesout", "type": "MATRIX"}, {"id": "assets", "type": "MATRIX"},
    {"id": "revenue", "type": "MATRIX"}, {"id": "debt", "type": "MATRIX"},
    {"id": "sector", "type": "GROUP"}, {"id": "industry", "type": "GROUP"},
    {"id": "subindustry", "type": "GROUP"},
]
FIELD_NAMES: List[str] = [f["id"] for f in FIELDS_FIXTURE]

HYP_FIXTURE: Dict[str, Any] = {
    "statement": "Stocks with strong recent price momentum relative to their sector "
                 "continue to outperform over the next month",
    "rationale": "Underreaction to firm-specific news creates persistent momentum; "
                 "sector-relative framing strips out beta",
    "expected_signal": "momentum",
    "pillar": "momentum",
    "key_fields": ["close", "returns", "volume"],
}

# Seed / parents for mutate & crossover — valid momentum/value expressions.
SEED_EXPR = "group_neutralize(ts_rank(ts_delta(close, 5), 20), sector)"
PARENT_A = "group_rank(ts_mean(returns, 20), industry)"          # momentum-ish
PARENT_B = "rank(divide(revenue, cap))"                          # value-ish

# Failing expressions + their real error category — used by self_correct / r1b_retry.
# Each targets a real FASTEXPR failure mode (hallucinated op, arity, GROUP-as-value, …).
BAD_EXPRS: List[Tuple[str, str, str]] = [
    ("neg_ts_rank(close, 20)", "hallucinated_operator",
     "Operator 'neg_ts_rank' does not exist; sign-flip with multiply(-1, ...)"),
    ("ts_regression(close, volume)", "arity",
     "ts_regression needs 3 inputs (y, x, d); got 2"),
    ("vec_ts_rank(returns, 10)", "hallucinated_operator",
     "There is no vec_ts_* operator; reduce VECTOR first then ts_*"),
    ("rank(industry)", "group_as_value",
     "GROUP field 'industry' cannot be a value input to rank()"),
    ("ts_mean(close)", "arity",
     "ts_mean needs 2 inputs (x, d); got 1"),
]

# Judge/consistency fixtures: (label, payload, expected_aligned)
R5_C1_FIXTURES = [
    ("aligned",
     {"hyp": "Earnings surprise drives short-term price momentum",
      "desc": "Signal captures post-earnings-announcement drift driven by earnings surprise momentum"},
     True),
    ("misaligned",
     {"hyp": "Analyst-sentiment revisions predict reversal",
      "desc": "Signal based on trading volume and realized volatility"},
     False),
]
R5_C2_FIXTURES = [
    ("aligned",
     {"desc": "Rank stocks cross-sectionally by 20-day price momentum",
      "expr": "rank(ts_delta(close, 20))", "ops": ["rank", "ts_delta"]},
     True),
    ("misaligned",
     {"desc": "Mean-revert on abnormal trading volume",
      "expr": "ts_rank(close, 60)", "ops": ["ts_rank"]},
     False),
]
# (label, kwargs for graph.attribution._build_user_prompt, expected_attribution)
ATTRIBUTION_FIXTURES = [
    ("implementation",
     dict(hypothesis_statement="Sector-relative price momentum predicts next-month returns",
          alpha_count=5, pass_count=0, syntax_fail=3, simulate_fail=2, quality_fail=0,
          samples=["'neg_ts_rank(close,20)' -> SYNTAX_FAIL (no operator neg_ts_rank)",
                   "'vec_ts_rank(returns,10)' -> SYNTAX_FAIL (no vec_ts_*)",
                   "'ts_regression(close,volume)' -> SIMULATE_FAIL (arity 2 vs 3)"]),
     "implementation"),
    ("hypothesis",
     dict(hypothesis_statement="A lottery-style random field predicts cross-sectional returns",
          alpha_count=5, pass_count=0, syntax_fail=0, simulate_fail=0, quality_fail=5,
          samples=["'rank(ts_mean(returns,20))' -> QUALITY_FAIL (sharpe=0.10)",
                   "'group_rank(ts_delta(close,5),sector)' -> QUALITY_FAIL (sharpe=-0.20)"]),
     "hypothesis"),
]

PILLARS = {"momentum", "value", "quality", "volatility", "sentiment", "other"}


# ---------------------------------------------------------------------------
# JSON parsing (markdown-fence tolerant — mirrors benchmark_llm_alpha_quality)
# ---------------------------------------------------------------------------
def _parse_json(content: str) -> Optional[Any]:
    c = (content or "").strip()
    if c.startswith("```"):
        nl = c.find("\n")
        c = c[nl + 1:] if nl >= 0 else c[3:]
        if c.endswith("```"):
            c = c[:-3]
        c = c.strip()
    try:
        return json.loads(c)
    except Exception:  # noqa: BLE001
        return None


def _extract_exprs(data: Any, keys=("alphas", "expressions", "variants", "offspring")) -> List[str]:
    """Pull a list of expression strings out of varied node output schemas."""
    if data is None:
        return []
    seq = None
    if isinstance(data, list):
        seq = data
    elif isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                seq = data[k]
                break
    if seq is None:
        # Single-object outputs: r1b_retry returns top-level {"fixed_expression"};
        # self_correct nests it under {"fix": {"fixed_expression": ...}}.
        if isinstance(data, dict):
            top = data.get("fixed_expression") or data.get("expression")
            if not top and isinstance(data.get("fix"), dict):
                top = data["fix"].get("fixed_expression") or data["fix"].get("expression")
            if isinstance(top, str) and top.strip():
                return [top.strip()]
        return []
    out = []
    for item in seq:
        if isinstance(item, str):
            out.append(item.strip())
        elif isinstance(item, dict):
            e = item.get("expression") or item.get("fixed_expression")
            if isinstance(e, str) and e.strip():
                out.append(e.strip())
    return [e for e in out if e]


# ---------------------------------------------------------------------------
# Shared offline scorers
# ---------------------------------------------------------------------------
def _score_expressions(exprs: List[str], known_ops) -> Dict[str, float]:
    """valid_rate × p_pass × (0.5+0.5·diversity) — same composite as the
    end-to-end bench, applied to whatever a node produced."""
    from backend.alpha_semantic_validator import validate_alpha_semantically
    from backend.agents.services.pre_simulate_filter import predict_pass_probability
    from backend.knowledge_extraction import expression_to_skeleton

    n = len(exprs)
    if n == 0:
        return {"n": 0, "valid_rate": 0.0, "p_pass": 0.0, "diversity": 0.0, "score": 0.0}

    valid = 0
    for e in exprs:
        try:
            if validate_alpha_semantically(e, fields=[], operators=list(known_ops), strict=False).get("valid"):
                valid += 1
        except Exception:  # noqa: BLE001
            pass
    valid_rate = valid / n
    try:
        probas = predict_pass_probability(exprs)
        p_pass = statistics.mean(probas) if probas else 0.0
    except Exception:  # noqa: BLE001
        p_pass = 0.0
    skels = set()
    for e in exprs:
        try:
            skels.add(expression_to_skeleton(e, max_depth=3))
        except Exception:  # noqa: BLE001
            skels.add(e)
    diversity = len(skels) / n
    score = valid_rate * p_pass * (0.5 + 0.5 * diversity)
    return {"n": n, "valid_rate": round(valid_rate, 3), "p_pass": round(p_pass, 3),
            "diversity": round(diversity, 3), "score": round(score, 4)}


# ===========================================================================
# Node bench definitions — each returns a list of (system, user) prompts to
# run, plus a scorer over the parsed outputs.
# ===========================================================================
class NodeBench:
    def __init__(self, node_key: str, kind: str,
                 build_calls: Callable[[], List[Tuple[str, str]]],
                 score: Callable[[List[Any]], Dict[str, float]],
                 temperature: float = TEMPERATURE, repeats: int = 1,
                 max_tokens: int = 4096):
        self.node_key = node_key
        self.kind = kind                 # 'expr' | 'struct' | 'consistency'
        self.build_calls = build_calls   # () -> [(system,user), ...] (one per fixture)
        self.score = score               # (list of parsed outputs) -> metrics dict
        self.temperature = temperature
        self.repeats = repeats           # extra repeats per call (consistency nodes)
        self.max_tokens = max_tokens     # per-node output budget (code_gen needs ~6k)


def _build_registry(known_ops) -> Dict[str, NodeBench]:
    from backend.agents.prompts.base import PromptContext
    from backend.agents.prompts.generation import ALPHA_GENERATION_SYSTEM, build_alpha_generation_prompt
    from backend.agents.prompts.hypothesis import HYPOTHESIS_SYSTEM, build_hypothesis_prompt
    from backend.agents.prompts.validation import SELF_CORRECT_SYSTEM, build_self_correct_prompt
    from backend.agents.prompts.r1b_retry import build_r1b_retry_prompt
    from backend.agents.prompts.r1b_mutate import build_r1b_mutate_prompt
    from backend.agents.prompts.r5_alignment import (
        R5_C1_SYSTEM, R5_C2_SYSTEM, build_r5_c1_prompt, build_r5_c2_prompt)
    from backend.agents.llm_mutate_alpha import MUTATE_SYSTEM, build_mutate_prompt
    from backend.agents.llm_crossover_alpha import CROSSOVER_SYSTEM, build_crossover_prompt

    operators = _OPERATORS_CACHE  # populated in main()

    def ctx(num_alphas=10):
        return PromptContext(dataset_id="pv1", dataset_description="price-volume",
                             region="USA", universe="TOP3000",
                             fields=FIELDS_FIXTURE, operators=operators,
                             num_alphas=num_alphas)

    reg: Dict[str, NodeBench] = {}

    # --- expression-producing -------------------------------------------------
    reg["code_gen"] = NodeBench(
        "code_gen", "expr",
        lambda: [(ALPHA_GENERATION_SYSTEM,
                  build_alpha_generation_prompt(ctx(5), target_hypothesis=HYP_FIXTURE))],
        lambda outs: _score_expressions(_extract_exprs(outs[0]), known_ops),
        max_tokens=6000,   # 5 alphas × full schema (hypothesis/velocity/turnover/expr) is long
    )

    def _selfcorrect_calls():
        calls = []
        for bad, etype, emsg in BAD_EXPRS:
            calls.append((SELF_CORRECT_SYSTEM, build_self_correct_prompt(
                expression=bad, error_message=emsg, error_type=etype,
                available_fields=FIELD_NAMES, operators=operators)))
        return calls

    def _fix_score(outs):
        fixed = [_extract_exprs(o)[0] for o in outs if _extract_exprs(o)]
        m = _score_expressions(fixed, known_ops)
        m["fix_rate"] = round(len(fixed) / max(1, len(BAD_EXPRS)), 3)  # did it return a fix at all
        return m

    reg["self_correct"] = NodeBench("self_correct", "expr", _selfcorrect_calls, _fix_score,
                                    max_tokens=1200)

    def _r1b_retry_calls():
        calls = []
        for bad, etype, emsg in BAD_EXPRS:
            calls.append(build_r1b_retry_prompt(
                original_expression=bad, original_hypothesis=HYP_FIXTURE["statement"],
                failure_metrics={"sharpe": 0.3, "fitness": 0.2, "turnover": 0.4},
                r1a_evidence=[emsg], r5_c2_reason=emsg,
                allowed_fields=FIELD_NAMES, operators=operators))
        return calls

    reg["r1b_retry"] = NodeBench("r1b_retry", "expr", _r1b_retry_calls, _fix_score,
                                 max_tokens=1200)

    reg["llm_mutate_alpha"] = NodeBench(
        "llm_mutate_alpha", "expr",
        lambda: [(MUTATE_SYSTEM, build_mutate_prompt(
            seed_expression=SEED_EXPR, region="USA",
            failure_context="", decay_context="", top_k=5))],
        lambda outs: _score_expressions(
            [e.replace("<SEED>", SEED_EXPR) for e in _extract_exprs(outs[0])], known_ops),
        max_tokens=2048,
    )

    reg["llm_crossover_alpha"] = NodeBench(
        "llm_crossover_alpha", "expr",
        lambda: [(CROSSOVER_SYSTEM, build_crossover_prompt(
            PARENT_A, PARENT_B, parent_a_pillar="momentum", parent_b_pillar="value",
            parent_a_metrics={"sharpe": 1.4}, parent_b_metrics={"sharpe": 1.3}, top_k=3))],
        lambda outs: _score_expressions(
            [e.replace("<A>", PARENT_A).replace("<B>", PARENT_B)
             for e in _extract_exprs(outs[0])], known_ops),
        max_tokens=2048,
    )

    # --- semi-structured ------------------------------------------------------
    def _hyp_score(outs):
        data = outs[0] or {}
        hyps = data.get("hypotheses") if isinstance(data, dict) else None
        hyps = hyps if isinstance(hyps, list) else []
        n = len(hyps)
        if n == 0:
            return {"n": 0, "schema_ok": 0.0, "pillar_div": 0.0, "concise": 0.0, "score": 0.0}
        ok = sum(1 for h in hyps if isinstance(h, dict)
                 and h.get("statement") and h.get("pillar") and h.get("expected_signal"))
        pillars = [h.get("pillar") for h in hyps if isinstance(h, dict) and h.get("pillar") in PILLARS]
        pillar_div = len(set(pillars)) / n if n else 0.0
        concise = sum(1 for h in hyps if isinstance(h, dict)
                      and len(str(h.get("statement", ""))) <= 180) / n
        schema_ok = ok / n
        score = schema_ok * (0.5 + 0.5 * pillar_div) * (0.5 + 0.5 * concise)
        return {"n": n, "schema_ok": round(schema_ok, 3), "pillar_div": round(pillar_div, 3),
                "concise": round(concise, 3), "score": round(score, 4)}

    reg["hypothesis"] = NodeBench(
        "hypothesis", "struct",
        lambda: [(HYPOTHESIS_SYSTEM, build_hypothesis_prompt(ctx(5)))],
        _hyp_score,
        max_tokens=5000,   # 5 hypotheses × full schema + analysis block
    )

    def _r1b_mutate_score(outs):
        data = outs[0] or {}
        nh = data.get("new_hypothesis") if isinstance(data, dict) else None
        if not isinstance(nh, dict):
            return {"n": 0, "pillar_keep": 0.0, "schema_ok": 0.0, "novel": 0.0, "score": 0.0}
        pillar_keep = 1.0 if nh.get("pillar") == "momentum" else 0.0   # fixture pillar
        schema_ok = 1.0 if (nh.get("statement") and nh.get("expected_signal")) else 0.0
        novel = 1.0 if str(nh.get("statement", "")).strip().lower() != HYP_FIXTURE["statement"].lower() else 0.0
        score = pillar_keep * schema_ok * (0.5 + 0.5 * novel)
        return {"n": 1, "pillar_keep": pillar_keep, "schema_ok": schema_ok,
                "novel": novel, "score": round(score, 4)}

    reg["r1b_mutate"] = NodeBench(
        "r1b_mutate", "struct",
        lambda: [build_r1b_mutate_prompt(
            original_hypothesis=HYP_FIXTURE["statement"],
            original_alpha_outcomes=[{"expression": PARENT_A, "sharpe": 0.4, "fitness": 0.3}],
            r5_c1_reason="hypothesis too broad", pillar="momentum", region="USA")],
        _r1b_mutate_score,
        max_tokens=1536,
    )

    # --- judge / consistency --------------------------------------------------
    def _verdict_score(fixtures_expected: List[bool]):
        """Build a scorer over a flat list of parsed verdicts, grouped by fixture.
        Each fixture is asked CONSISTENCY_REPEATS times consecutively."""
        def _scorer(outs):
            k = CONSISTENCY_REPEATS
            groups = [outs[i * k:(i + 1) * k] for i in range(len(fixtures_expected))]
            correct, stable, schema = [], [], []
            for verdicts, expected in zip(groups, fixtures_expected):
                aligned = []
                for v in verdicts:
                    if isinstance(v, dict) and isinstance(v.get("aligned"), bool):
                        aligned.append(v["aligned"])
                        reason = str(v.get("reason", ""))
                        schema.append(1.0 if (isinstance(v.get("confidence"), (int, float))
                                              and 0 < len(reason) <= 220) else 0.0)
                    else:
                        schema.append(0.0)
                if aligned:
                    mode, cnt = Counter(aligned).most_common(1)[0]
                    stable.append(cnt / len(aligned))
                    correct.append(1.0 if mode == expected else 0.0)
                else:
                    stable.append(0.0); correct.append(0.0)
            c = statistics.mean(correct) if correct else 0.0
            s = statistics.mean(stable) if stable else 0.0
            sc = statistics.mean(schema) if schema else 0.0
            return {"n": len(outs), "correct": round(c, 3), "stability": round(s, 3),
                    "schema_ok": round(sc, 3), "score": round(0.5 * c + 0.3 * s + 0.2 * sc, 4)}
        return _scorer

    reg["r5_alignment_c1"] = NodeBench(
        "r5_alignment_c1", "consistency",
        lambda: [(R5_C1_SYSTEM, build_r5_c1_prompt(fx["hyp"], fx["desc"]))
                 for _, fx, _ in R5_C1_FIXTURES],
        _verdict_score([exp for _, _, exp in R5_C1_FIXTURES]),
        temperature=JUDGE_TEMPERATURE, repeats=CONSISTENCY_REPEATS, max_tokens=600,
    )
    reg["r5_alignment_c2"] = NodeBench(
        "r5_alignment_c2", "consistency",
        lambda: [(R5_C2_SYSTEM, build_r5_c2_prompt(fx["desc"], fx["expr"], fx["ops"]))
                 for _, fx, _ in R5_C2_FIXTURES],
        _verdict_score([exp for _, _, exp in R5_C2_FIXTURES]),
        temperature=JUDGE_TEMPERATURE, repeats=CONSISTENCY_REPEATS, max_tokens=600,
    )

    # attribution: real node_key="attribution" path is graph/attribution.py
    # (short {attribution, confidence, reasoning} verdict). Guarded so a
    # signature drift skips this node rather than killing the whole run.
    try:
        from backend.agents.graph.attribution import _SYSTEM as ATTR_SYSTEM, _build_user_prompt as _attr_user

        def _attr_calls():
            return [(ATTR_SYSTEM, _attr_user(**fx)) for _, fx, _ in ATTRIBUTION_FIXTURES]

        def _attr_score(outs):
            k = CONSISTENCY_REPEATS
            expected = [exp for _, _, exp in ATTRIBUTION_FIXTURES]
            groups = [outs[i * k:(i + 1) * k] for i in range(len(ATTRIBUTION_FIXTURES))]
            valid_vals = {"hypothesis", "implementation", "both", "unknown", "neither"}
            correct, stable, schema = [], [], []
            for verdicts, exp in zip(groups, expected):
                labels = []
                for v in verdicts:
                    lab = v.get("attribution") if isinstance(v, dict) else None
                    if isinstance(lab, str) and lab.lower() in valid_vals:
                        labels.append(lab.lower()); schema.append(1.0)
                    else:
                        schema.append(0.0)
                if labels:
                    mode, cnt = Counter(labels).most_common(1)[0]
                    stable.append(cnt / len(labels))
                    correct.append(1.0 if mode == exp else 0.0)
                else:
                    stable.append(0.0); correct.append(0.0)
            c = statistics.mean(correct) if correct else 0.0
            s = statistics.mean(stable) if stable else 0.0
            sc = statistics.mean(schema) if schema else 0.0
            return {"n": len(outs), "correct": round(c, 3), "stability": round(s, 3),
                    "schema_ok": round(sc, 3), "score": round(0.5 * c + 0.3 * s + 0.2 * sc, 4)}

        reg["attribution"] = NodeBench("attribution", "consistency", _attr_calls,
                                       _attr_score, temperature=JUDGE_TEMPERATURE,
                                       repeats=CONSISTENCY_REPEATS, max_tokens=800)
    except Exception as e:  # noqa: BLE001
        print(f"[bench] attribution node skipped (builder import failed: {e})", flush=True)

    return reg


_OPERATORS_CACHE: List[Dict] = []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def _run_node_for_model(svc, model: str, bench: NodeBench) -> Dict[str, Any]:
    svc.model = model
    if getattr(svc, "provider", "openai") == "anthropic":
        svc.anthropic_model = model
    else:
        svc.openai_model = model

    try:
        base_calls = bench.build_calls()
    except Exception as e:  # noqa: BLE001
        return {"model": model, "error": f"build_failed: {type(e).__name__}: {e}"[:160], "score": 0.0}

    # expand each call by `repeats` (consistency nodes re-ask the same fixture)
    calls: List[Tuple[str, str]] = []
    for c in base_calls:
        calls.extend([c] * bench.repeats)

    parsed: List[Any] = []
    latencies, tokens, call_fail = [], 0, 0
    last_err = None
    for system, user in calls:
        t = time.time()
        try:
            # Pass model/provider EXPLICITLY so call()'s per-node router is
            # bypassed (resolve_model_for only kicks in when both are None). Else
            # if ENABLE_PER_FUNCTION_LLM_ROUTING is ON in this process, every
            # candidate would be silently benchmarked as the map's fixed model.
            eff_provider = getattr(svc, "provider", "openai")
            r = await svc.call(system, user, temperature=bench.temperature,
                               json_mode=True, max_tokens=bench.max_tokens,
                               node_key=bench.node_key, model=model, provider=eff_provider)
        except Exception as e:  # noqa: BLE001
            call_fail += 1; last_err = f"{type(e).__name__}: {e}"[:120]; parsed.append(None); continue
        latencies.append(time.time() - t)
        tokens += getattr(r, "tokens_used", 0) or 0
        if not getattr(r, "success", False):
            call_fail += 1; last_err = (getattr(r, "error", "") or "")[:120]; parsed.append(None); continue
        parsed.append(_parse_json(r.content or ""))

    try:
        metrics = bench.score(parsed)
    except Exception as e:  # noqa: BLE001
        metrics = {"score": 0.0, "score_error": f"{type(e).__name__}: {e}"[:120]}

    metrics.update({
        "model": model, "calls": len(calls), "call_fail": call_fail, "last_err": last_err,
        "avg_latency_s": round(statistics.mean(latencies), 1) if latencies else None,
        "tokens": tokens,
    })
    return metrics


def _agg(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    m = statistics.mean(vals)
    s = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return round(m, 4), round(s, 4)


async def main(runs: int = RUNS_DEFAULT, models: Optional[List[str]] = None,
               nodes: Optional[List[str]] = None) -> int:
    global _OPERATORS_CACHE
    from backend.agents.services.llm_service import LLMService, LLM_API_CIRCUIT
    from backend.alpha_semantic_validator import load_operators_from_db
    from backend.database import AsyncSessionLocal
    from backend.tasks.mining_tasks import _get_operators

    try:
        LLM_API_CIRCUIT.clear(reason="per_node_bench_start")
        print("[bench] LLM_API_CIRCUIT cleared", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[bench] circuit clear failed: {e}", flush=True)

    models = models or MODELS
    known_ops = await load_operators_from_db()
    async with AsyncSessionLocal() as db:
        _OPERATORS_CACHE = await _get_operators(db)
    print(f"loaded {len(known_ops)} known ops, {len(_OPERATORS_CACHE)} operator defs | "
          f"runs={runs} | models={models}", flush=True)

    registry = _build_registry(known_ops)
    node_keys = nodes or list(registry.keys())
    node_keys = [n for n in node_keys if n in registry]
    print(f"benchmarking nodes: {node_keys}\n", flush=True)

    svc = LLMService(provider="openai")
    await svc._ensure_credentials_loaded()
    svc_anthropic = None

    def _pick(m):
        nonlocal svc_anthropic
        if m.startswith("claude-"):
            if svc_anthropic is None:
                svc_anthropic = LLMService(provider="anthropic")
            return svc_anthropic
        return svc

    # results[node][model] = list of per-run metric dicts
    results: Dict[str, Dict[str, List[Dict]]] = {nk: {m: [] for m in models} for nk in node_keys}

    for run_i in range(runs):
        print(f"===== RUN {run_i + 1}/{runs} @ {time.strftime('%H:%M:%S')} =====", flush=True)
        for nk in node_keys:
            bench = registry[nk]
            for m in models:
                res = await _run_node_for_model(_pick(m), m, bench)
                results[nk][m].append(res)
                print(f"  [{nk:<20}] {m:<22} score={res.get('score')} "
                      f"lat={res.get('avg_latency_s')} tok={res.get('tokens')} "
                      f"fail={res.get('call_fail')} err={res.get('last_err')}", flush=True)

    # aggregate + rank per node
    report: Dict[str, Any] = {"runs": runs, "models": models, "nodes": {}}
    recommendation: Dict[str, str] = {}
    for nk in node_keys:
        rows = []
        for m in models:
            runs_list = results[nk][m]
            score_mean, score_std = _agg([r.get("score", 0) for r in runs_list])
            lat_mean, _ = _agg([r.get("avg_latency_s") for r in runs_list])
            tok_mean, _ = _agg([r.get("tokens") for r in runs_list])
            rows.append({"model": m, "score_mean": score_mean, "score_std": score_std,
                         "avg_latency_s": lat_mean, "tokens": tok_mean,
                         "sample": runs_list[-1] if runs_list else {}})
        rows.sort(key=lambda r: (r["score_mean"] or 0), reverse=True)
        report["nodes"][nk] = {"kind": registry[nk].kind, "ranking": rows}
        if rows and (rows[0]["score_mean"] or 0) > 0:
            recommendation[nk] = rows[0]["model"]

    report["recommendation_LLM_FUNCTION_MODEL_MAP"] = {
        nk: {"model": m, "provider": "openai"} for nk, m in recommendation.items()
    }

    tag = f"x{runs}" + ("" if len(models) == len(MODELS) else f"_{len(models)}m")
    out = Path(__file__).resolve().parent.parent / "docs" / f"llm_per_node_benchmark_{date.today()}_{tag}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # print ranked tables
    for nk in node_keys:
        print(f"\n=== {nk} ({registry[nk].kind}) — ranked by score (mean±std over {runs} runs) ===")
        print(f"{'model':<24}{'score':>16}{'lat_s':>8}{'tokens':>9}")
        for r in report["nodes"][nk]["ranking"]:
            sc = f"{r['score_mean']}±{r['score_std']}"
            print(f"{r['model']:<24}{sc:>16}{str(r['avg_latency_s']):>8}{str(r['tokens']):>9}")
    print("\n=== RECOMMENDED LLM_FUNCTION_MODEL_MAP (paste into routing plan) ===")
    print(json.dumps(report["recommendation_LLM_FUNCTION_MODEL_MAP"], indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=RUNS_DEFAULT)
    p.add_argument("--models", type=str, default=None, help="comma-separated subset")
    p.add_argument("--nodes", type=str, default=None,
                   help="comma-separated node_key subset (default: all registered)")
    args = p.parse_args()
    _models = [m.strip() for m in args.models.split(",")] if args.models else None
    _nodes = [n.strip() for n in args.nodes.split(",")] if args.nodes else None
    sys.exit(asyncio.run(main(runs=args.runs, models=_models, nodes=_nodes)))
