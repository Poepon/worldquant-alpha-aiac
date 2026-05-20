"""Tier D — robustness hardening verification tests (2026-05-20).

Pins the deferred SHOULD/NICE fixes batched in Tier D:
  D1 grammar parser thread-safe tri-state cache + retry-on-transient
  D3 recursion-safe _extract_op_names + length guard
  D7 CJK-aware estimate_tokens
  D9 FACTOR_LENS_OLS_LOOKBACK_DAYS wired into decompose_alpha
  D10 family scoring single-basis (no composite/sharpe scale mixing)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# D1: parser tri-state cache + thread-safe + reset helper
# ---------------------------------------------------------------------------

def test_d1_reset_parser_cache_forces_rebuild():
    from backend.services import grammar_validator as gv
    p1 = gv._lazy_parser()
    assert p1 is not None
    gv._reset_parser_cache()
    p2 = gv._lazy_parser()
    assert p2 is not None  # rebuilds cleanly


def test_d1_lazy_parser_caches_same_instance():
    from backend.services import grammar_validator as gv
    gv._reset_parser_cache()
    a = gv._lazy_parser()
    b = gv._lazy_parser()
    assert a is b  # cached, not rebuilt


# ---------------------------------------------------------------------------
# D3: length guard + recursion-safe walk
# ---------------------------------------------------------------------------

def test_d3_length_guard_skips_pathological_input():
    from backend.services import grammar_validator as gv
    huge = "rank(" * 600 + "close" + ")" * 600  # >2000 chars, deeply nested
    res = gv.validate(huge)
    # Degrade-open (don't burn Earley O(n³))
    assert res.ok is True
    assert res.error_msg == "too_long_skipped"


def test_d3_extract_op_names_deep_tree_no_recursion_error():
    """Deeply nested (but < length guard) expression must not RecursionError."""
    from backend.services import grammar_validator as gv
    # ~200 deep — would have approached the recursion limit in the old
    # recursive walk for larger N; stack-based handles it.
    expr = "rank(" * 100 + "close" + ")" * 100
    assert len(expr) < gv._MAX_EXPR_LEN
    res = gv.validate(expr)
    # parses fine; rank is a known op → no unknown_ops, no crash
    assert res.ok is True


# ---------------------------------------------------------------------------
# D7: CJK-aware token estimate
# ---------------------------------------------------------------------------

def test_d7_cjk_estimate_higher_than_latin():
    from backend.services.cognitive_layer_service import estimate_tokens
    latin = "the quick brown fox jumps over the lazy dog" * 4  # ~176 chars
    cjk = "动量信号在最近六十天内持续存在并且高于趋势基线" * 4   # ~92 chars CJK
    lat_est = estimate_tokens(latin)
    cjk_est = estimate_tokens(cjk)
    # CJK at ~1.5 tokens/char vs latin ~0.25 → CJK estimate higher despite
    # fewer chars
    assert cjk_est > lat_est


def test_d7_pure_latin_unchanged_ballpark():
    from backend.services.cognitive_layer_service import estimate_tokens
    # 40 latin chars → ~10 tokens (0.25/char)
    assert estimate_tokens("a" * 40) == 10


def test_d7_empty_returns_zero():
    from backend.services.cognitive_layer_service import estimate_tokens
    assert estimate_tokens("") == 0


# ---------------------------------------------------------------------------
# D9: FACTOR_LENS_OLS_LOOKBACK_DAYS wired
# ---------------------------------------------------------------------------

def test_d9_decompose_alpha_accepts_lookback_days(monkeypatch, tmp_path):
    from backend.services import factor_lens_service as fls
    n = 600
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    rng = np.random.default_rng(5)
    fdf = pd.DataFrame(
        {f: rng.normal(0, 0.01, n) for f in ("size", "value", "momentum", "quality", "low_vol")},
        index=dates,
    )
    fdf.to_parquet(tmp_path / "usa.parquet")
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)

    alpha = fdf["value"] + rng.normal(0.001, 0.005, n)
    alpha.index = dates
    # lookback_days=120 → only last 120 rows used → ols_n_days ≤ 120
    res = fls.decompose_alpha(alpha_returns=alpha, region="USA", lookback_days=120)
    assert res.mode_used == "ols_daily"
    assert res.ols_n_days <= 120


def test_d9_decompose_alpha_none_lookback_uses_full(monkeypatch, tmp_path):
    from backend.services import factor_lens_service as fls
    n = 300
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(6)
    fdf = pd.DataFrame(
        {f: rng.normal(0, 0.01, n) for f in ("size", "value", "momentum", "quality", "low_vol")},
        index=dates,
    )
    fdf.to_parquet(tmp_path / "usa.parquet")
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)
    alpha = fdf["momentum"] + rng.normal(0.0, 0.005, n)
    alpha.index = dates
    res = fls.decompose_alpha(alpha_returns=alpha, region="USA", lookback_days=None)
    # full series used
    assert res.ols_n_days == n


# ---------------------------------------------------------------------------
# D10: family scoring single-basis (no composite/sharpe mixing)
# ---------------------------------------------------------------------------

@dataclass
class _A:
    expression: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    quality_status: Optional[str] = "PASS"
    alpha_id: Optional[str] = None


def test_d10_family_uses_composite_only_when_all_have_it():
    from backend.family_classifier import _family_uses_composite
    all_comp = [
        _A("x", metrics={"composite_score": 1.0}),
        _A("y", metrics={"composite_score": 2.0}),
    ]
    assert _family_uses_composite(all_comp) is True
    mixed = [
        _A("x", metrics={"composite_score": 1.0}),
        _A("y", metrics={"sharpe": 2.0}),  # no composite
    ]
    assert _family_uses_composite(mixed) is False


def test_d10_family_cap_ranks_consistently_when_mixed():
    """When members have mixed composite/sharpe availability, the family
    ranks by sharpe for ALL (no scale mixing). 3 same-family alphas,
    top_k=2 → drop the lowest-sharpe one."""
    from backend.family_classifier import apply_family_cap
    # Same op skeleton (ts_rank) + same pillar → one family of 3.
    # a has composite=0.5 + sharpe=3.5 (high sharpe), b/c lack composite.
    # If old code mixed: a ranked by composite 0.5 (lowest) → wrongly dropped.
    # New code: all rank by sharpe → a (3.5) kept, lowest sharpe dropped.
    a = _A("ts_rank(close, 60)", metrics={"composite_score": 0.5, "sharpe": 3.5, "pillar": "momentum"})
    b = _A("ts_rank(volume, 60)", metrics={"sharpe": 2.0, "pillar": "momentum"})
    c = _A("ts_rank(returns, 60)", metrics={"sharpe": 1.0, "pillar": "momentum"})
    drop_idx = apply_family_cap([a, b, c], top_k=2)
    # c (sharpe 1.0, lowest) dropped — NOT a (which old mixing would drop)
    assert drop_idx == [2]
