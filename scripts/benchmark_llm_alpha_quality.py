"""Benchmark LLM models for alpha-generation quality (proxy metrics, no BRAIN).

For each model on the DashScope OpenAI-compat endpoint, generate ~N alpha
expressions from the SAME prompt, then score them with the system's own gates:
  - call success rate + JSON parse rate (format compliance)
  - semantic validity (real BRAIN operators / syntax — catches hallucinated ops)
  - pre-simulate ML P(PASS) (the same classifier node_simulate uses)
  - diversity (distinct skeletons / total)
  - avg latency, total tokens

Proxy-only — no BRAIN simulation. Run:
    venv/Scripts/python.exe scripts/benchmark_llm_alpha_quality.py
Writes docs/llm_alpha_quality_benchmark_<date>.json + prints a ranked table.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import date
from pathlib import Path

MODELS = [
    "qwen3.6-plus", "qwen3.5-plus", "qwen3-max-2026-01-23",
    "qwen3-coder-next", "qwen3-coder-plus",
    "glm-5", "glm-4.7", "kimi-k2.5", "MiniMax-M2.5",
]
TARGET_PER_MODEL = 50
ALPHAS_PER_CALL = 10
MAX_CALLS_PER_MODEL = 9  # safety cap
TEMPERATURE = 0.8

SYSTEM = (
    "You are a quantitative alpha researcher for WorldQuant BRAIN. "
    "You output ONLY a valid JSON object, no commentary."
)
USER_TMPL = (
    "Generate {n} DIVERSE alpha expressions in WorldQuant BRAIN FASTEXPR syntax for "
    "region=USA, universe=TOP3000. Use real price-volume / fundamental fields "
    "(close, open, high, low, volume, vwap, returns, cap, sharesout, assets, "
    "revenue, debt) and real BRAIN operators (ts_rank, ts_mean, ts_std_dev, "
    "ts_delta, ts_zscore, ts_decay_linear, ts_corr, rank, zscore, scale, "
    "group_neutralize, group_rank, group_zscore, trade_when, winsorize). "
    "Each must be a non-trivial cross-sectional or time-series signal, not a bare field. "
    'Return a JSON object exactly like {{"alphas": ["expr1", "expr2", ...]}} '
    "with {n} distinct expressions."
)


async def _gen_for_model(svc, model: str, known_ops):
    from backend.alpha_semantic_validator import validate_alpha_semantically
    from backend.agents.services.pre_simulate_filter import predict_pass_probability
    from backend.knowledge_extraction import expression_to_skeleton

    exprs: list[str] = []
    latencies: list[float] = []
    tokens = 0
    calls = 0
    call_fail = 0
    json_fail = 0
    last_err = None

    while len(exprs) < TARGET_PER_MODEL and calls < MAX_CALLS_PER_MODEL:
        calls += 1
        svc.model = model
        # set the active-model field that matches the loaded provider so the
        # bookkeeping stays consistent (LLMService.call reads self.model, but
        # downstream code may also read the provider-specific field).
        if svc.provider == "anthropic":
            svc.anthropic_model = model
        else:
            svc.openai_model = model
        t = time.time()
        try:
            r = await svc.call(
                SYSTEM, USER_TMPL.format(n=ALPHAS_PER_CALL),
                temperature=TEMPERATURE, json_mode=True, max_tokens=2500,
            )
        except Exception as e:  # noqa: BLE001
            call_fail += 1
            last_err = f"{type(e).__name__}: {e}"[:120]
            continue
        latencies.append(time.time() - t)
        tokens += getattr(r, "tokens_used", 0) or 0
        if not getattr(r, "success", False):
            call_fail += 1
            last_err = (getattr(r, "error", "") or "")[:120]
            continue
        try:
            # Anthropic models often wrap JSON in markdown code fences
            # (```json ... ```) despite the "ONLY JSON" instruction. Strip
            # the fence so json.loads succeeds. OpenAI-compat models that
            # honour response_format are unaffected (content has no fence).
            content = (r.content or "").strip()
            if content.startswith("```"):
                first_nl = content.find("\n")
                content = content[first_nl + 1:] if first_nl >= 0 else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
            data = json.loads(content)
            got = data.get("alphas") or data.get("expressions") or []
            got = [e.strip() for e in got if isinstance(e, str) and e.strip()]
            if not got:
                json_fail += 1
            exprs.extend(got)
        except Exception:  # noqa: BLE001
            json_fail += 1

    exprs = exprs[:TARGET_PER_MODEL]
    n = len(exprs)
    if n == 0:
        return {
            "model": model, "n_alphas": 0, "calls": calls, "call_fail": call_fail,
            "json_fail": json_fail, "last_err": last_err,
            "valid_rate": 0.0, "mean_p_pass": 0.0, "diversity": 0.0,
            "avg_latency_s": round(statistics.mean(latencies), 1) if latencies else None,
            "tokens": tokens, "quality_score": 0.0,
        }

    valid = 0
    for e in exprs:
        try:
            res = validate_alpha_semantically(e, fields=[], operators=list(known_ops), strict=False)
            if res.get("valid"):
                valid += 1
        except Exception:  # noqa: BLE001
            pass
    valid_rate = valid / n

    try:
        probas = predict_pass_probability(exprs)
        mean_p_pass = statistics.mean(probas) if probas else 0.0
    except Exception:  # noqa: BLE001
        mean_p_pass = 0.0

    skels = set()
    for e in exprs:
        try:
            skels.add(expression_to_skeleton(e, max_depth=3))
        except Exception:  # noqa: BLE001
            skels.add(e)
    diversity = len(skels) / n

    # composite: expected useful-and-distinct yield weighted by pass-likelihood
    quality_score = valid_rate * mean_p_pass * (0.5 + 0.5 * diversity)

    return {
        "model": model, "n_alphas": n, "calls": calls, "call_fail": call_fail,
        "json_fail": json_fail, "last_err": last_err,
        "valid_rate": round(valid_rate, 3),
        "mean_p_pass": round(mean_p_pass, 3),
        "diversity": round(diversity, 3),
        "avg_latency_s": round(statistics.mean(latencies), 1) if latencies else None,
        "tokens": tokens,
        "quality_score": round(quality_score, 4),
    }


def _agg(vals):
    """(mean, std, min, max) over a list; std=0 when <2 samples."""
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, None, None
    m = statistics.mean(vals)
    s = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return round(m, 4), round(s, 4), round(min(vals), 4), round(max(vals), 4)


async def main(runs: int = 3, models: list | None = None) -> int:
    from backend.agents.services.llm_service import LLMService, LLM_API_CIRCUIT
    from backend.alpha_semantic_validator import load_operators_from_db
    # Start every bench run with a clean circuit so a prior failed run's
    # 5-min cooldown doesn't fast-fail the first call here.
    try:
        LLM_API_CIRCUIT.clear(reason="bench_start")
        print("[bench] LLM_API_CIRCUIT cleared", flush=True)
    except Exception as _e:
        print(f"[bench] LLM_API_CIRCUIT clear failed: {_e}", flush=True)

    models = models or MODELS
    known_ops = await load_operators_from_db()
    print(f"loaded {len(known_ops)} known BRAIN operators | runs={runs} | models={models}\n")

    svc_openai = LLMService(provider="openai")
    await svc_openai._ensure_credentials_loaded()
    # Anthropic svc is lazy-built only when a claude-* model is in `models`,
    # so old runs (qwen / glm / kimi only) don't pay the auth round-trip.
    svc_anthropic = None

    def _pick_svc(m: str):
        nonlocal svc_anthropic
        if m.startswith("claude-"):
            if svc_anthropic is None:
                svc_anthropic = LLMService(provider="anthropic")
            return svc_anthropic
        return svc_openai

    # per-model list of per-run result dicts
    per_model: dict[str, list] = {m: [] for m in models}
    for run_i in range(runs):
        print(f"\n===== RUN {run_i + 1}/{runs} @ {time.strftime('%H:%M:%S')} =====", flush=True)
        for m in models:
            print(f"[{time.strftime('%H:%M:%S')}] run{run_i + 1} {m} ...", flush=True)
            svc = _pick_svc(m)
            try:
                res = await _gen_for_model(svc, m, known_ops)
            except Exception as e:  # noqa: BLE001
                res = {"model": m, "n_alphas": 0, "error": f"{type(e).__name__}: {e}"[:150],
                       "valid_rate": 0.0, "mean_p_pass": 0.0, "diversity": 0.0, "quality_score": 0.0}
            per_model[m].append(res)
            print(f"   -> n={res.get('n_alphas')} valid={res.get('valid_rate')} "
                  f"P(PASS)={res.get('mean_p_pass')} div={res.get('diversity')} "
                  f"lat={res.get('avg_latency_s')} score={res.get('quality_score')} "
                  f"err={res.get('last_err')}", flush=True)

    # aggregate
    agg = []
    for m, runs_list in per_model.items():
        q = [r.get("quality_score", 0) for r in runs_list]
        qm, qs, qmin, qmax = _agg(q)
        agg.append({
            "model": m,
            "runs": len(runs_list),
            "quality_mean": qm, "quality_std": qs, "quality_min": qmin, "quality_max": qmax,
            "valid_rate_mean": _agg([r.get("valid_rate", 0) for r in runs_list])[0],
            "p_pass_mean": _agg([r.get("mean_p_pass", 0) for r in runs_list])[0],
            "p_pass_std": _agg([r.get("mean_p_pass", 0) for r in runs_list])[1],
            "diversity_mean": _agg([r.get("diversity", 0) for r in runs_list])[0],
            "n_alphas_mean": _agg([r.get("n_alphas", 0) for r in runs_list])[0],
            "avg_latency_s_mean": _agg([r.get("avg_latency_s") for r in runs_list])[0],
        })
    agg.sort(key=lambda r: r.get("quality_mean") or 0, reverse=True)

    _tag = f"x{runs}" + ("" if len(models) == len(MODELS) else f"_{len(models)}m")
    out = Path(__file__).resolve().parent.parent / "docs" / f"llm_alpha_quality_benchmark_{date.today()}_{_tag}.json"
    out.write_text(json.dumps({"aggregate": agg, "raw_per_model": per_model}, indent=2, default=str), encoding="utf-8")

    print(f"\n=== AGGREGATE RANKING over {runs} runs (by mean quality_score) ===")
    print(f"{'model':<24}{'runs':>5}{'valid':>7}{'P(PASS)±std':>14}{'divers':>8}{'lat_s':>7}{'score(mean±std)':>18}")
    for r in agg:
        pp = f"{r['p_pass_mean']}±{r['p_pass_std']}"
        sc = f"{r['quality_mean']}±{r['quality_std']}"
        print(f"{r['model']:<24}{r['runs']:>5}{str(r['valid_rate_mean']):>7}{pp:>14}"
              f"{str(r['diversity_mean']):>8}{str(r['avg_latency_s_mean']):>7}{sc:>18}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=3, help="number of full passes over the models")
    p.add_argument("--models", type=str, default=None,
                   help="comma-separated subset of models (default: all)")
    args = p.parse_args()
    _models = [m.strip() for m in args.models.split(",")] if args.models else None
    sys.exit(asyncio.run(main(runs=args.runs, models=_models)))
