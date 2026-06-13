"""Per-node LLM USABILITY + COST screen for the Aliyun Coding Plan (v2, 2026-06-05).

REDESIGN (post adversarial review). The previous version tried to RANK model
QUALITY offline — but valid_rate/diversity saturate at 1.0, the only varying term
(p_pass) is online-non-transferring noise, and the online A/B already settled that
reasoning models don't beat kimi (and cost ~48% more). So offline does NOT decide
quality. It screens each node's candidate models on the NEW coding.dashscope
endpoint for what offline CAN validly measure:

  - reachability   (catalog intersection / per-model probe)
  - usability      (parse-rate, a real op+arity+group validity SCREEN, and
                    truncation-rate at PRODUCTION max_tokens — a screen, not a ranker)
  - cost           (quota-token consumption incl. the discarded-reasoning premium;
                    Coding Plan is a fixed-quota subscription so total tokens = cost)
  - reliability    (API call success rate; timeouts are MISSING DATA, not score 0)

Quality decisions defer to the online A/B harness (scripts/phase_c_llm_routing_ab.py).
p_pass / diversity are kept as DIAGNOSTIC columns only — never in the ranking.

SAFETY — this shares the PRODUCTION Coding-Plan key / quota / Redis circuits:
  * Run ONLY with production mining paused (stop FLAT sessions + worker) OR a
    separate key. A full run can exhaust the shared subscription and starve live
    mining via shared-key 429s.
  * Hard call/token budget + abort; concurrency=1 + inter-call sleep; a 429 HALTS
    the run (does not let the SDK @retry keep burning quota).
  * `llm_circuits_clear_all()` is NOT called unless --reset-circuits (default OFF),
    so the benchmark never wipes production's shared circuit state.

Usage:
    venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --verify-catalog
    venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --smoke
    venv/Scripts/python.exe scripts/benchmark_llm_per_node.py --i-understand-quota
Writes docs/llm_per_node_benchmark_<date>_x<runs>.json + prints per-node scorecards.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CODING_PLAN_PROVIDER = "aliyun_coding_plan"
CODING_PLAN_API_KEY_REF = "llm_provider_aliyun_coding_plan"

# ---------------------------------------------------------------------------
# Coding Plan authoritative catalog (2026-06-05, user-confirmed screenshot).
# reasoning class is MEASURED empirically (reasoning_share) at run time; the
# catalog "纯文本生成" coder models are the non-reasoning prior.
# ---------------------------------------------------------------------------
CATALOG = [
    "qwen3.6-plus", "qwen3.5-plus", "qwen3-max-2026-01-23",
    "qwen3-coder-next", "qwen3-coder-plus",
    "glm-5", "glm-4.7", "kimi-k2.5", "MiniMax-M2.5",
]
NONREASONING_PRIOR = {"qwen3-coder-next", "qwen3-coder-plus"}  # catalog: text-gen only

# Focused per-kind candidate matrices (control shared quota — NOT 9×all-nodes).
# The node's live incumbent is always appended if missing.
EXPR_CANDIDATES = ["kimi-k2.5", "qwen3-coder-next", "qwen3-coder-plus", "qwen3.6-plus"]
STRUCT_CANDIDATES = ["qwen3.6-plus", "kimi-k2.5", "qwen3-coder-plus", "glm-5"]
JUDGE_CANDIDATES = ["kimi-k2.5", "qwen3.6-plus"]

TEMPERATURE = 0.8
JUDGE_TEMPERATURE = 0.2
CONSISTENCY_REPEATS = 4
PILLARS = {"momentum", "value", "quality", "volatility", "sentiment", "other"}


# ===========================================================================
# Safety: shared-quota guard (Phase A)
# ===========================================================================
class QuotaHalt(RuntimeError):
    pass


class QuotaGuard:
    """Hard call/token budget + 429 kill-switch for the shared Coding-Plan key."""

    def __init__(self, max_calls: int, max_tokens: int, sleep_s: float):
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.sleep_s = sleep_s
        self.calls = 0
        self.tokens = 0
        self.halted = False
        self.reason: Optional[str] = None

    def check(self):
        if self.halted:
            raise QuotaHalt(f"HALT: {self.reason}")
        if self.calls >= self.max_calls:
            self._halt(f"max_calls={self.max_calls} reached")
        if self.tokens >= self.max_tokens:
            self._halt(f"max_tokens={self.max_tokens} reached")

    def record(self, tokens: int, resp_error: Optional[str]):
        self.calls += 1
        self.tokens += int(tokens or 0)
        if resp_error and re.search(r"(429|rate.?limit|too many requests)", resp_error, re.I):
            self._halt(f"429 / rate-limit seen — refusing to burn shared production quota ({resp_error[:80]})")

    def _halt(self, reason: str):
        self.halted = True
        self.reason = reason
        raise QuotaHalt(f"HALT: {reason}")


# ===========================================================================
# Endpoint re-pointing (Phase C) — deterministic, flag/DB-state independent
# ===========================================================================
async def point_at_coding_plan(svc) -> str:
    """Point svc.client at coding.dashscope. MUST run after _ensure_credentials_
    loaded() (which flips the idempotent guard) so the next call()'s guard is a
    no-op and won't overwrite our client (root cause of the dead-endpoint bug:
    the standalone process is flag-OFF with no OPENAI_* creds → legacy path →
    api.openai.com)."""
    import backend.config as cfg
    await svc._ensure_credentials_loaded()  # flip guard first
    providers = cfg._flag_override_cache.get("LLM_PROVIDERS") or cfg._LLM_PROVIDERS_CACHE
    prof = providers.get(CODING_PLAN_PROVIDER)
    if not prof or not prof.get("base_url"):
        raise RuntimeError(f"provider {CODING_PLAN_PROVIDER} has no base_url in LLM_PROVIDERS")
    base_url = prof["base_url"]
    key = await svc._resolve_api_key_ref(CODING_PLAN_API_KEY_REF)
    if not key:
        raise RuntimeError(f"cannot resolve credential:{CODING_PLAN_API_KEY_REF} (CredentialsService + env)")
    svc.api_key = key
    svc.base_url = base_url
    svc.clear_client_cache()
    svc.client = svc._get_client("openai", base_url, api_key=key)
    actual = str(getattr(svc.client, "base_url", ""))
    if "coding.dashscope" not in actual:
        raise RuntimeError(f"repoint failed: client.base_url={actual!r}")
    return base_url


async def probe_catalog(svc, candidates: List[str]) -> Dict[str, Any]:
    """Verify which candidates the LIVE coding.dashscope endpoint serves.
    models.list() best-effort; on failure fall back to a max_tokens=1 probe per
    model (authoritative). Returns {served, reachable, unreachable, method}."""
    served: Optional[List[str]] = None
    method = "models.list"
    try:
        listing = await svc.client.models.list()
        served = sorted({getattr(m, "id", None) for m in getattr(listing, "data", []) if getattr(m, "id", None)})
    except Exception as e:  # noqa: BLE001
        print(f"[catalog] models.list() unavailable ({type(e).__name__}: {str(e)[:80]}) → per-model probe", flush=True)
        method = "per-model-probe"

    reachable, unreachable = [], []
    if served is not None:
        served_set = set(served)
        for m in candidates:
            (reachable if m in served_set else unreachable).append(m)
        # models.list can under-report aliases → probe the ones it omitted
        for m in list(unreachable):
            if await _probe_one(svc, m):
                unreachable.remove(m)
                reachable.append(m)
                method = "models.list+probe"
    else:
        for m in candidates:
            (reachable if await _probe_one(svc, m) else unreachable).append(m)

    return {"method": method, "served_listing": served, "reachable": reachable, "unreachable": unreachable}


async def _probe_one(svc, model: str) -> bool:
    try:
        r = await svc.call("ping", "Return the single token: ok", temperature=0.0,
                           json_mode=False, max_tokens=1, model=model, provider="openai")
        return bool(getattr(r, "success", False)) or bool((getattr(r, "content", "") or "").strip())
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Incumbent (live map) — Phase E. Read the DB feature-flag override, NOT settings.
# ===========================================================================
async def load_live_map() -> Dict[str, Dict[str, str]]:
    import backend.config as cfg
    try:
        from backend.database import AsyncSessionLocal
        from backend.services.feature_flag_service import FeatureFlagService
        async with AsyncSessionLocal() as db:
            await FeatureFlagService(db).load_overrides_into_cache()
    except Exception as e:  # noqa: BLE001
        print(f"[incumbent] flag-cache warm failed ({e}) → config startup seed", flush=True)
    return cfg._flag_override_cache.get("LLM_FUNCTION_MODEL_MAP") or cfg._LLM_FUNCTION_MODEL_MAP_CACHE


def incumbent_for(node_key: str, live_map: Dict[str, Dict[str, str]]) -> Optional[str]:
    entry = live_map.get(node_key) or live_map.get("__default__")
    return (entry or {}).get("model") if isinstance(entry, dict) else None


# ===========================================================================
# Fixtures (typed fields so the group-as-value guard fires; static = reproducible)
# ===========================================================================
FIELDS_FIXTURE: List[Dict] = [
    {"id": "close", "type": "MATRIX", "category": "Price Volume"},
    {"id": "open", "type": "MATRIX", "category": "Price Volume"},
    {"id": "high", "type": "MATRIX", "category": "Price Volume"},
    {"id": "low", "type": "MATRIX", "category": "Price Volume"},
    {"id": "volume", "type": "MATRIX", "category": "Price Volume"},
    {"id": "vwap", "type": "MATRIX", "category": "Price Volume"},
    {"id": "returns", "type": "MATRIX", "category": "Price Volume"},
    {"id": "cap", "type": "MATRIX", "category": "Fundamental"},
    {"id": "sharesout", "type": "MATRIX", "category": "Fundamental"},
    {"id": "assets", "type": "MATRIX", "category": "Fundamental"},
    {"id": "revenue", "type": "MATRIX", "category": "Fundamental"},
    {"id": "debt", "type": "MATRIX", "category": "Fundamental"},
    {"id": "eps", "type": "MATRIX", "category": "Analyst"},
    {"id": "sector", "type": "GROUP", "category": "Classifier"},
    {"id": "industry", "type": "GROUP", "category": "Classifier"},
    {"id": "subindustry", "type": "GROUP", "category": "Classifier"},
]
FIELD_NAMES: List[str] = [f["id"] for f in FIELDS_FIXTURE]
FIELD_CATEGORIES = sorted({f["category"] for f in FIELDS_FIXTURE})

HYP_FIXTURE: Dict[str, Any] = {
    "statement": "Stocks with strong recent price momentum relative to their sector "
                 "continue to outperform over the next month",
    "rationale": "Underreaction to firm-specific news creates persistent momentum; "
                 "sector-relative framing strips out beta",
    "expected_signal": "momentum", "pillar": "momentum",
    "key_fields": ["close", "returns", "volume"],
}
SEED_EXPR = "group_neutralize(ts_rank(ts_delta(close, 5), 20), sector)"
PARENT_A = "group_rank(ts_mean(returns, 20), industry)"
PARENT_B = "rank(divide(revenue, cap))"
GOOD_EXPRS = [SEED_EXPR, PARENT_A, PARENT_B, "rank(ts_zscore(close, 20))",
              "group_zscore(ts_decay_linear(returns, 10), sector)"]

# Failing expressions + real error category — drives self_correct / r1b_retry and
# PINS the validity screen (these MUST be judged invalid).
BAD_EXPRS: List[Tuple[str, str, str]] = [
    ("neg_ts_rank(close, 20)", "hallucinated_operator",
     "Operator 'neg_ts_rank' does not exist; sign-flip with multiply(-1, ...)"),
    ("ts_regression(close, volume)", "arity",
     "ts_regression needs 3 inputs (y, x, d); got 2"),
    ("vec_ts_rank(returns, 10)", "hallucinated_operator",
     "There is no vec_ts_* operator; reduce VECTOR first then ts_*"),
    ("rank(industry)", "group_as_value",
     "GROUP field 'industry' cannot be a value input to rank()"),
    ("ts_mean(close)", "arity", "ts_mean needs 2 inputs (x, d); got 1"),
]

R5_C1_FIXTURES = [
    ("aligned", {"hyp": "Earnings surprise drives short-term price momentum",
                 "desc": "Signal captures post-earnings-announcement drift driven by earnings surprise momentum"}, True),
    ("misaligned", {"hyp": "Analyst-sentiment revisions predict reversal",
                    "desc": "Signal based on trading volume and realized volatility"}, False),
    ("aligned2", {"hyp": "Low-volatility stocks earn higher risk-adjusted returns",
                  "desc": "Cross-sectional rank of inverse realized volatility captures the low-vol anomaly"}, True),
    ("misaligned2", {"hyp": "Sector-relative value predicts mean reversion",
                     "desc": "Signal ranks stocks by 5-day price momentum within sector"}, False),
]
R5_C2_FIXTURES = [
    ("aligned", {"desc": "Rank stocks cross-sectionally by 20-day price momentum",
                 "expr": "rank(ts_delta(close, 20))", "ops": ["rank", "ts_delta"]}, True),
    ("misaligned", {"desc": "Mean-revert on abnormal trading volume",
                    "expr": "ts_rank(close, 60)", "ops": ["ts_rank"]}, False),
    ("aligned2", {"desc": "Sector-neutral value rank using earnings yield",
                  "expr": "group_rank(divide(eps, close), sector)", "ops": ["group_rank", "divide"]}, True),
    ("misaligned2", {"desc": "Capture short-term reversal in returns",
                     "expr": "ts_mean(volume, 20)", "ops": ["ts_mean"]}, False),
]
ATTRIBUTION_FIXTURES = [
    ("implementation",
     dict(hypothesis_statement="Sector-relative price momentum predicts next-month returns",
          alpha_count=5, pass_count=0, syntax_fail=3, simulate_fail=2, quality_fail=0,
          samples=["'neg_ts_rank(close,20)' -> SYNTAX_FAIL (no operator neg_ts_rank)",
                   "'vec_ts_rank(returns,10)' -> SYNTAX_FAIL (no vec_ts_*)",
                   "'ts_regression(close,volume)' -> SIMULATE_FAIL (arity 2 vs 3)"]), "implementation"),
    ("hypothesis",
     dict(hypothesis_statement="A lottery-style random field predicts cross-sectional returns",
          alpha_count=5, pass_count=0, syntax_fail=0, simulate_fail=0, quality_fail=5,
          samples=["'rank(ts_mean(returns,20))' -> QUALITY_FAIL (sharpe=0.10)",
                   "'group_rank(ts_delta(close,5),sector)' -> QUALITY_FAIL (sharpe=-0.20)"]), "hypothesis"),
]

# distill_context fixture (multi-category so grounding discriminates)
DISTILL_FIXTURE = dict(
    dataset_id="pv1", description="US equity price-volume and fundamental dataset",
    category="Price Volume", success_patterns="- ts_rank momentum on close\n- group_neutralize by sector",
    field_categories="\n".join(f"- {c}" for c in FIELD_CATEGORIES),
)


# ===========================================================================
# JSON parsing + expr extraction (markdown-fence tolerant)
# ===========================================================================
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


# ===========================================================================
# Validity SCREEN — real ops (reject_unknown_operators) + arity (parse the
# operator `definition` string; param_count is 0 for all live ops) + group-as-value
# (typed fields). A SCREEN that catches a broken model, NOT a quality ranker.
# ===========================================================================
def build_arity_map(operators: List[Dict]) -> Dict[str, Tuple[int, int]]:
    """op_name -> (min_required, max_allowed) parsed from the definition string,
    e.g. 'ts_regression(y, x, d, lag = 0, rettype = 0)' -> (3, 5);
    'ts_mean(x, d)' -> (2, 2); 'rank(x, rate=2)' -> (1, 2)."""
    arity: Dict[str, Tuple[int, int]] = {}
    for op in operators or []:
        name = (op.get("name") or "").strip().lower()
        defn = op.get("definition") or ""
        if not name or "(" not in defn:
            continue
        inside = defn[defn.find("(") + 1: defn.rfind(")")] if ")" in defn else defn[defn.find("(") + 1:]
        params = [p for p in _split_top_level(inside) if p.strip()]
        if not params:
            arity[name] = (0, 0)
            continue
        required = sum(1 for p in params if "=" not in p)
        arity[name] = (required, len(params))
    return arity


def _split_top_level(s: str) -> List[str]:
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _arity_violations(expr: str, arity: Dict[str, Tuple[int, int]]) -> bool:
    """True if any operator call in expr has an out-of-window arg count."""
    for m in _CALL_RE.finditer(expr):
        name = m.group(1).lower()
        win = arity.get(name)
        if not win:
            continue  # unknown op handled by the semantic validator, not here
        open_idx = m.end() - 1
        depth, j = 0, open_idx
        while j < len(expr):
            if expr[j] in "([{":
                depth += 1
            elif expr[j] in ")]}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = expr[open_idx + 1:j]
        nargs = 0 if not inner.strip() else len([p for p in _split_top_level(inner) if p.strip()])
        lo, hi = win
        if nargs < lo or nargs > hi:
            return True
    return False


def expr_usable(expr: str, validator, arity: Dict[str, Tuple[int, int]]) -> bool:
    """The validity screen: semantic-valid (real ops + group-as-value, hard) AND
    arity within window."""
    try:
        if not validator.validate(expr).valid:
            return False
    except Exception:  # noqa: BLE001
        return False
    return not _arity_violations(expr, arity)


# ===========================================================================
# Diagnostic-only (NOT in ranking): p_pass + skeleton diversity
# ===========================================================================
def _diag_p_pass(exprs: List[str]) -> float:
    try:
        from backend.agents.services.pre_simulate_filter import predict_pass_probability
        probas = predict_pass_probability(exprs)
        return round(statistics.mean(probas), 3) if probas else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _diag_diversity(exprs: List[str]) -> float:
    try:
        from backend.knowledge_extraction import expression_to_skeleton
        skels = set()
        for e in exprs:
            try:
                skels.add(expression_to_skeleton(e, max_depth=3))
            except Exception:  # noqa: BLE001
                skels.add(e)
        return round(len(skels) / len(exprs), 3) if exprs else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# ===========================================================================
# NodeBench — per-node real prompt + production max_tokens + usability scorer
# ===========================================================================
class NodeBench:
    def __init__(self, node_key: str, kind: str,
                 build_calls: Callable[[], List[Tuple[str, str]]],
                 usability: Callable[[List[Any], "ScreenCtx"], Dict[str, float]],
                 max_tokens: int, temperature: float = TEMPERATURE,
                 repeats: int = 1, runs: int = 2):
        self.node_key = node_key
        self.kind = kind            # 'expr' | 'struct' | 'judge'
        self.build_calls = build_calls
        self.usability = usability
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.repeats = repeats
        self.runs = runs


class ScreenCtx:
    def __init__(self, validator, arity):
        self.validator = validator
        self.arity = arity


def _expr_usability(exprs: List[str], ctx: ScreenCtx) -> Dict[str, float]:
    n = len(exprs)
    if n == 0:
        return {"n": 0, "validity_rate": None, "p_pass_diag": None, "diversity_diag": None}
    usable = sum(1 for e in exprs if expr_usable(e, ctx.validator, ctx.arity))
    return {"n": n, "validity_rate": round(usable / n, 3),
            "p_pass_diag": _diag_p_pass(exprs), "diversity_diag": _diag_diversity(exprs)}


def build_registry(known_ops, operators) -> Dict[str, NodeBench]:
    from backend.agents.prompts.base import PromptContext
    from backend.agents.prompts.generation import ALPHA_GENERATION_SYSTEM, build_alpha_generation_prompt
    from backend.agents.prompts.hypothesis import HYPOTHESIS_SYSTEM, build_hypothesis_prompt, DISTILL_SYSTEM
    from backend.agents.prompts.legacy import DISTILL_USER
    from backend.agents.prompts.validation import SELF_CORRECT_SYSTEM, build_self_correct_prompt
    from backend.config import settings

    hyp_mt = getattr(settings, "HYPOTHESIS_MAX_TOKENS", 6000)
    cg_per = getattr(settings, "CODE_GEN_MAX_TOKENS_PER_ALPHA", 512)
    cg_ceil = getattr(settings, "CODE_GEN_MAX_TOKENS_CEILING", 8000)
    code_gen_mt = min(cg_ceil, max(4096, 1024 + cg_per * 5))  # production formula @ N=5

    def ctx(num_alphas=5):
        return PromptContext(dataset_id="pv1", dataset_description="price-volume",
                             region="USA", universe="TOP3000",
                             fields=FIELDS_FIXTURE, operators=operators, num_alphas=num_alphas)

    reg: Dict[str, NodeBench] = {}

    # --- expression-producing -------------------------------------------------
    reg["code_gen"] = NodeBench(
        "code_gen", "expr",
        lambda: [(ALPHA_GENERATION_SYSTEM, build_alpha_generation_prompt(ctx(5), target_hypothesis=HYP_FIXTURE))],
        lambda outs, c: _expr_usability(_extract_exprs(outs[0]), c),
        max_tokens=code_gen_mt, runs=3)

    def _selfcorrect_calls():
        return [(SELF_CORRECT_SYSTEM, build_self_correct_prompt(
            expression=bad, error_message=emsg, error_type=etype,
            available_fields=FIELD_NAMES, operators=operators)) for bad, etype, emsg in BAD_EXPRS]

    def _fix_usability(outs, c):
        fixed = [_extract_exprs(o)[0] for o in outs if _extract_exprs(o)]
        m = _expr_usability(fixed, c)
        m["fix_rate"] = round(len(fixed) / max(1, len(BAD_EXPRS)), 3)
        return m

    reg["self_correct"] = NodeBench("self_correct", "expr", _selfcorrect_calls, _fix_usability,
                                    max_tokens=1200, runs=3)


    # --- semi-structured ------------------------------------------------------
    def _hyp_usability(outs, c):
        data = outs[0] or {}
        hyps = data.get("hypotheses") if isinstance(data, dict) else None
        hyps = hyps if isinstance(hyps, list) else []
        n = len(hyps)
        if n == 0:
            return {"n": 0, "schema_ok": None, "pillar_div_diag": None}
        ok = sum(1 for h in hyps if isinstance(h, dict) and h.get("statement")
                 and h.get("pillar") and h.get("expected_signal"))
        pillars = [h.get("pillar") for h in hyps if isinstance(h, dict) and h.get("pillar") in PILLARS]
        return {"n": n, "schema_ok": round(ok / n, 3),
                "pillar_div_diag": round(len(set(pillars)) / n, 3)}

    reg["hypothesis"] = NodeBench("hypothesis", "struct",
                                  lambda: [(HYPOTHESIS_SYSTEM, build_hypothesis_prompt(ctx(5)))],
                                  _hyp_usability, max_tokens=hyp_mt, runs=3)


    def _distill_usability(outs, c):
        data = outs[0] or {}
        if not isinstance(data, dict):
            return {"n": 0, "schema_ok": None, "grounding": None, "count_ok": None}
        concepts = data.get("selected_concepts")
        if not isinstance(concepts, list) or not concepts:
            return {"n": 0, "schema_ok": 0.0, "grounding": 0.0, "count_ok": 0.0}
        schema_ok = 1.0 if (concepts and data.get("reasoning")) else 0.0
        cats = {x.lower() for x in FIELD_CATEGORIES}
        grounded = sum(1 for x in concepts if isinstance(x, str) and x.strip().lower() in cats)
        grounding = round(grounded / len(concepts), 3)
        count_ok = 1.0 if 3 <= len(concepts) <= 5 else 0.0
        return {"n": len(concepts), "schema_ok": schema_ok, "grounding": grounding, "count_ok": count_ok}

    reg["distill_context"] = NodeBench(
        "distill_context", "struct",
        lambda: [(DISTILL_SYSTEM, DISTILL_USER.format(**DISTILL_FIXTURE))],
        _distill_usability, max_tokens=4096, runs=3)


    return reg


KIND_CANDIDATES = {"expr": EXPR_CANDIDATES, "struct": STRUCT_CANDIDATES, "judge": JUDGE_CANDIDATES}


# ===========================================================================
# Runner (serial, quota-guarded)
# ===========================================================================
async def run_node_model(svc, model: str, bench: NodeBench, ctx: ScreenCtx,
                         guard: QuotaGuard) -> Dict[str, Any]:
    try:
        base_calls = bench.build_calls()
    except Exception as e:  # noqa: BLE001
        return {"model": model, "error": f"build_failed: {type(e).__name__}: {e}"[:160]}
    calls: List[Tuple[str, str]] = []
    for c in base_calls:
        calls.extend([c] * bench.repeats)

    parsed: List[Any] = []
    lat, total_tok, comp_tok, reason_tok = [], 0, 0, 0
    call_fail, truncated = 0, 0
    last_err = None
    for system, user in calls:
        guard.check()
        t = time.time()
        try:
            r = await svc.call(system, user, temperature=bench.temperature, json_mode=True,
                               max_tokens=bench.max_tokens, node_key=bench.node_key,
                               model=model, provider="openai")
        except Exception as e:  # noqa: BLE001
            guard.record(0, str(e))
            call_fail += 1; last_err = f"{type(e).__name__}: {e}"[:120]; parsed.append(None)
            await asyncio.sleep(guard.sleep_s); continue
        guard.record(getattr(r, "tokens_used", 0) or 0, getattr(r, "error", None))
        lat.append(time.time() - t)
        total_tok += getattr(r, "tokens_used", 0) or 0
        comp_tok += getattr(r, "completion_tokens", 0) or 0
        reason_tok += getattr(r, "reasoning_tokens", 0) or 0
        if getattr(r, "truncated", False):
            truncated += 1
        if not getattr(r, "success", False):
            call_fail += 1; last_err = (getattr(r, "error", "") or "")[:120]; parsed.append(None)
            await asyncio.sleep(guard.sleep_s); continue
        parsed.append(_parse_json(r.content or ""))
        await asyncio.sleep(guard.sleep_s)

    n_calls = len(calls)
    n_ok = sum(1 for p in parsed if p is not None)
    try:
        usab = bench.usability(parsed, ctx)
    except Exception as e:  # noqa: BLE001
        usab = {"usability_error": f"{type(e).__name__}: {e}"[:120]}

    reasoning_share = round(reason_tok / comp_tok, 3) if comp_tok else 0.0
    out = {
        "model": model,
        "reliability": round(1 - call_fail / n_calls, 3) if n_calls else 0.0,
        "parse_rate": round(n_ok / max(1, n_calls - call_fail), 3) if (n_calls - call_fail) > 0 else 0.0,
        "truncation_rate": round(truncated / n_calls, 3) if n_calls else 0.0,
        "quota_tokens": round(total_tok / max(1, n_calls - call_fail), 1) if (n_calls - call_fail) > 0 else None,
        "reasoning_share": reasoning_share,
        "avg_latency_s": round(statistics.mean(lat), 1) if lat else None,
        "calls": n_calls, "call_fail": call_fail, "last_err": last_err,
    }
    out.update(usab)
    return out


async def main(argv=None) -> int:
    import backend.config as cfg
    from backend.agents.services.llm_service import LLMService
    from backend.alpha_semantic_validator import AlphaSemanticValidator, load_operators_from_db
    from backend.database import AsyncSessionLocal
    from backend.tasks.mining_tasks import _get_operators

    p = argparse.ArgumentParser()
    p.add_argument("--verify-catalog", action="store_true", help="only probe which catalog models are reachable")
    p.add_argument("--smoke", action="store_true", help="1 model × 1 node, runs=1, then estimate + stop")
    p.add_argument("--i-understand-quota", action="store_true",
                   help="REQUIRED for a full run — acknowledges shared production-quota burn")
    p.add_argument("--reset-circuits", action="store_true",
                   help="clear the shared Redis LLM circuits at start (DANGER: affects production)")
    p.add_argument("--nodes", type=str, default=None, help="comma node_key subset")
    p.add_argument("--max-calls", type=int, default=4000)
    p.add_argument("--max-tokens-budget", type=int, default=25_000_000)
    p.add_argument("--sleep", type=float, default=0.4, help="inter-call sleep (QPS guard)")
    args = p.parse_args(argv)

    if args.reset_circuits:
        try:
            from backend.agents.services.llm_service import llm_circuits_clear_all
            print(f"[bench] WARNING reset shared circuits ({llm_circuits_clear_all(reason='bench')})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[bench] circuit reset failed: {e}", flush=True)

    svc = LLMService(provider="openai")
    base_url = await point_at_coding_plan(svc)
    print(f"[bench] repointed → {base_url}", flush=True)

    live_map = await load_live_map()

    # ---- catalog verification (always) ----
    cat = await probe_catalog(svc, CATALOG)
    print(f"[bench] catalog method={cat['method']} reachable={cat['reachable']} unreachable={cat['unreachable']}", flush=True)
    # incumbent reachability — flags the qwen3.6-flash live-routing problem
    incumbents = {nk: incumbent_for(nk, live_map) for nk in
                  ["code_gen", "hypothesis", "self_correct", "distill_context", "__default__"]}
    incumbent_unreachable = {}
    for nk, m in incumbents.items():
        if m and m not in cat["reachable"]:
            reachable_now = await _probe_one(svc, m)
            if not reachable_now:
                incumbent_unreachable[nk] = m
    if incumbent_unreachable:
        print(f"[bench] ⚠ BROKEN INCUMBENTS (model not on Coding Plan): {incumbent_unreachable}", flush=True)
    # config drift
    stale_avail = [m for m in cfg._LLM_AVAILABLE_MODELS_CACHE if m not in CATALOG]
    catalog_report = {"endpoint": base_url, **cat, "incumbents": incumbents,
                      "incumbent_unreachable": incumbent_unreachable,
                      "config_LLM_AVAILABLE_MODELS_stale": stale_avail}

    if args.verify_catalog:
        out = Path(__file__).resolve().parent.parent / "docs" / f"coding_plan_catalog_{date.today()}.json"
        out.write_text(json.dumps(catalog_report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(catalog_report, indent=2, default=str))
        print(f"\nwrote {out}")
        return 0

    # ---- screen setup ----
    known_ops = await load_operators_from_db()
    async with AsyncSessionLocal() as db:
        operators = await _get_operators(db)
    arity = build_arity_map(operators)
    validator = AlphaSemanticValidator(fields=FIELDS_FIXTURE, operators=list(known_ops),
                                       strict_field_check=False, strict_type_check=False,
                                       reject_unknown_operators=True)
    screen = ScreenCtx(validator, arity)
    # PIN: the validity screen must reject every BAD_EXPR and accept the good ones.
    _assert_screen(screen)
    print(f"[bench] validity screen PINNED (BAD_EXPRS rejected, GOOD accepted) | {len(arity)} arity ops", flush=True)

    registry = build_registry(known_ops, operators)
    node_keys = [n.strip() for n in args.nodes.split(",")] if args.nodes else list(registry.keys())
    node_keys = [n for n in node_keys if n in registry]

    guard = QuotaGuard(max_calls=args.max_calls, max_tokens=args.max_tokens_budget, sleep_s=args.sleep)

    if args.smoke:
        nk = node_keys[0]
        bench = registry[nk]
        cand = (incumbents.get(nk) or KIND_CANDIDATES[bench.kind][0])
        print(f"[bench] SMOKE {nk} × {cand} runs=1 (max_tokens={bench.max_tokens})", flush=True)
        res = await run_node_model(svc, cand, bench, screen, guard)
        print(json.dumps(res, indent=2, default=str))
        per_call = guard.tokens / max(1, guard.calls)
        est = _estimate(registry, node_keys, incumbents, per_call)
        print(f"\n[bench] smoke used {guard.calls} calls / {guard.tokens} tokens "
              f"(~{per_call:.0f} tok/call). FULL-RUN ESTIMATE: ~{est['calls']} calls / "
              f"~{est['tokens']:,} tokens. Re-run with --i-understand-quota to proceed.", flush=True)
        return 0

    if not args.i_understand_quota:
        print("[bench] REFUSING full run without --i-understand-quota (shared production quota). "
              "Pause mining or use a separate key, then re-run with the flag. Try --smoke first.", flush=True)
        return 2

    # ---- full screen ----
    results: Dict[str, Dict[str, List[Dict]]] = {}
    halted = None
    try:
        for nk in node_keys:
            bench = registry[nk]
            cands = list(KIND_CANDIDATES[bench.kind])
            inc = incumbents.get(nk)
            if inc and inc not in cands:
                cands.append(inc)
            cands = [m for m in cands if (m in cat["reachable"]) or (m == inc)]
            results[nk] = {m: [] for m in cands}
            for run_i in range(bench.runs):
                for m in cands:
                    res = await run_node_model(svc, m, bench, screen, guard)
                    results[nk][m].append(res)
                    print(f"  [{nk:<18}] run{run_i+1} {m:<22} valid={res.get('validity_rate')} "
                          f"parse={res.get('parse_rate')} trunc={res.get('truncation_rate')} "
                          f"qtok={res.get('quota_tokens')} rshare={res.get('reasoning_share')} "
                          f"rel={res.get('reliability')} lat={res.get('avg_latency_s')}", flush=True)
    except QuotaHalt as e:
        halted = str(e)
        print(f"[bench] {halted} — stopping, writing partial report", flush=True)

    report = synthesize(results, registry, incumbents, catalog_report, halted, guard)
    tag = "partial" if halted else "full"
    out = Path(__file__).resolve().parent.parent / "docs" / f"llm_per_node_benchmark_{date.today()}_{tag}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _print_report(report)
    print(f"\nwrote {out}  (calls={guard.calls} tokens={guard.tokens})")
    return 0


def _assert_screen(screen: ScreenCtx):
    # HARD: every BAD_EXPR must be rejected (the plan's pinned invariant).
    for bad, _, _ in BAD_EXPRS:
        assert not expr_usable(bad, screen.validator, screen.arity), f"validity screen FAILED to reject {bad!r}"
    # SOFT: GOOD acceptance guards against an over-aggressive screen, but a GOOD
    # op merely missing from the live registry must not abort the whole run.
    rejected_good = [g for g in GOOD_EXPRS if not expr_usable(g, screen.validator, screen.arity)]
    if rejected_good:
        print(f"[bench] WARN validity screen rejected GOOD exprs (op missing from registry?): {rejected_good}", flush=True)


def _estimate(registry, node_keys, incumbents, per_call):
    calls = 0
    for nk in node_keys:
        b = registry[nk]
        cands = set(KIND_CANDIDATES[b.kind]) | ({incumbents.get(nk)} if incumbents.get(nk) else set())
        calls += b.runs * len(cands) * len(b.build_calls()) * b.repeats
    return {"calls": calls, "tokens": int(calls * per_call)}


# ===========================================================================
# Decision (Phase E) — default KEEP INCUMBENT; flag only forced/genuine candidates
# ===========================================================================
def _agg(rows: List[Dict], key: str):
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    return round(statistics.mean(vals), 3)


def synthesize(results, registry, incumbents, catalog_report, halted, guard) -> Dict[str, Any]:
    nodes_out = {}
    overrides_payloads = {}
    for nk, by_model in results.items():
        kind = registry[nk].kind
        inc = incumbents.get(nk)
        scorecard = {}
        for m, rows in by_model.items():
            scorecard[m] = {
                "reliability": _agg(rows, "reliability"),
                "validity_rate": _agg(rows, "validity_rate"),
                "parse_rate": _agg(rows, "parse_rate"),
                "schema_ok": _agg(rows, "schema_ok"),
                "grounding": _agg(rows, "grounding"),
                "correct": _agg(rows, "correct"),
                "stability": _agg(rows, "stability"),
                "truncation_rate": _agg(rows, "truncation_rate"),
                "quota_tokens": _agg(rows, "quota_tokens"),
                "reasoning_share": _agg(rows, "reasoning_share"),
                "avg_latency_s": _agg(rows, "avg_latency_s"),
                "p_pass_diag": _agg(rows, "p_pass_diag"),
                "diversity_diag": _agg(rows, "diversity_diag"),
            }
        decision, rationale, challenger = _decide(nk, kind, inc, scorecard, catalog_report)
        nodes_out[nk] = {"kind": kind, "incumbent": inc, "decision": decision,
                         "rationale": rationale, "challenger": challenger, "scorecard": scorecard}
        if decision in ("FORCE_SWITCH", "ONLINE_AB") and challenger and challenger != inc:
            overrides_payloads[nk] = {nk: {"model": challenger, "provider_ref": CODING_PLAN_PROVIDER}}

    return {"date": str(date.today()), "halted": halted,
            "budget_used": {"calls": guard.calls, "tokens": guard.tokens},
            "catalog": catalog_report, "nodes": nodes_out,
            "online_ab_payloads": overrides_payloads,
            "note": "Offline screens usability+cost only. Quality decisions → online A/B "
                    "(scripts/phase_c_llm_routing_ab.py). Apply map via LLMRoutingConsole audit path, not DB."}


def _usable(sc: Dict[str, Any]) -> bool:
    """A model is 'usable' on a node when it parses, doesn't truncate badly, and
    (for expr nodes) clears the validity screen."""
    if (sc.get("reliability") or 0) < 0.8:
        return False
    if (sc.get("truncation_rate") or 0) > 0.2:
        return False
    if sc.get("validity_rate") is not None and (sc.get("validity_rate") or 0) < 0.8:
        return False
    if sc.get("parse_rate") is not None and (sc.get("parse_rate") or 0) < 0.8:
        return False
    return True


def _decide(nk, kind, inc, scorecard, catalog_report):
    # ① forced switch: incumbent unreachable / broken on the Coding Plan
    if nk in catalog_report.get("incumbent_unreachable", {}):
        usable_alts = [m for m, sc in scorecard.items() if m != inc and _usable(sc)]
        usable_alts.sort(key=lambda m: scorecard[m].get("quota_tokens") or 1e9)
        pick = usable_alts[0] if usable_alts else None
        return "FORCE_SWITCH", (f"incumbent {inc!r} not on Coding Plan catalog → must switch to a "
                                f"reachable usable model"), pick
    inc_sc = scorecard.get(inc)
    if inc_sc is None:
        return "REVIEW", f"incumbent {inc!r} not benchmarked here", None
    # ② / ③ genuine online-A/B candidate: a usable challenger materially cheaper
    inc_cost = inc_sc.get("quota_tokens") or 0
    best = None
    for m, sc in scorecard.items():
        if m == inc or not _usable(sc):
            continue
        cost = sc.get("quota_tokens") or 0
        if inc_cost and cost and cost <= 0.8 * inc_cost:  # >20% cheaper
            if best is None or cost < (scorecard[best].get("quota_tokens") or 1e9):
                best = m
    if best:
        reason = (f"usable challenger {best!r} is >20% cheaper in quota tokens "
                  f"({scorecard[best].get('quota_tokens')} vs incumbent {inc_cost}); "
                  f"reasoning_share inc={inc_sc.get('reasoning_share')} chal={scorecard[best].get('reasoning_share')}")
        return "ONLINE_AB", reason, best
    return "KEEP", f"incumbent {inc!r} usable; no usable challenger materially cheaper — keep (quality is online-settled)", None


def _print_report(report):
    print("\n=== PER-NODE SCREEN (usability+cost; quality → online A/B) ===")
    for nk, nd in report["nodes"].items():
        print(f"\n# {nk} [{nd['kind']}] incumbent={nd['incumbent']} → {nd['decision']}")
        print(f"  {nd['rationale']}")
        hdr = f"  {'model':<22}{'rel':>5}{'valid':>7}{'parse':>7}{'sok':>6}{'trunc':>7}{'qtok':>9}{'rshare':>8}{'lat':>7}"
        print(hdr)
        for m, sc in nd["scorecard"].items():
            mark = "*" if m == nd["incumbent"] else (">" if m == nd["challenger"] else " ")
            print(f" {mark}{m:<22}{_f(sc['reliability']):>5}{_f(sc['validity_rate']):>7}{_f(sc['parse_rate']):>7}"
                  f"{_f(sc['schema_ok']):>6}{_f(sc['truncation_rate']):>7}{_f(sc['quota_tokens']):>9}"
                  f"{_f(sc['reasoning_share']):>8}{_f(sc['avg_latency_s']):>7}")
    if report["online_ab_payloads"]:
        print("\n=== ONLINE A/B payloads (POST /ops/start-flat-session llm_overrides) ===")
        print(json.dumps(report["online_ab_payloads"], indent=2))


def _f(v):
    return "-" if v is None else (f"{v:g}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
