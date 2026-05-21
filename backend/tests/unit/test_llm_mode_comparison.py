"""Phase 4 Sprint 1 A1.4 — llm_mode_comparison service unit tests.

Coverage:
  - query_mode_pool against real in-memory aiosqlite with seeded
    author + assistant alphas across regions and templates
  - bootstrap_diff_ci correctness:
    - identical rates → CI contains 0
    - large effect → CI strictly positive/negative
    - empty pools → insufficient_samples=True
    - reproducibility via seed
  - evaluate_go_gate decision matrix:
    - INSUFFICIENT when no assistant data
    - NO-GO on big negative effect
    - GO on clear positive effect (synthetic high-signal)
    - PARTIAL when CI straddles 0
    - ERROR when comparison errored upstream
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# bootstrap_diff_ci — pure function math correctness
# ---------------------------------------------------------------------------


def test_bootstrap_identical_rates_ci_contains_zero():
    """Same PASS rate on both pools → CI centered at 0."""
    from backend.services.llm_mode_comparison import bootstrap_diff_ci
    out = bootstrap_diff_ci(
        author_total=500, author_pass=50,    # 10% rate
        assistant_total=500, assistant_pass=50,  # 10% rate
        iterations=1000,
        ci_level=0.80,
        seed=42,
    )
    assert out["author_rate"] == pytest.approx(0.10)
    assert out["assistant_rate"] == pytest.approx(0.10)
    assert out["effect_pct_pts"] == pytest.approx(0.0)
    assert out["ci_lower"] < 0
    assert out["ci_upper"] > 0
    assert out["insufficient_samples"] is False


def test_bootstrap_large_positive_effect_ci_above_zero():
    """Assistant 20% vs author 5% → CI strictly positive."""
    from backend.services.llm_mode_comparison import bootstrap_diff_ci
    out = bootstrap_diff_ci(
        author_total=500, author_pass=25,    # 5%
        assistant_total=500, assistant_pass=100,  # 20%
        iterations=1000,
        ci_level=0.80,
        seed=42,
    )
    assert out["effect_pct_pts"] == pytest.approx(0.15)
    assert out["ci_lower"] > 0, f"CI lower should be > 0, got {out['ci_lower']}"


def test_bootstrap_large_negative_effect_ci_below_zero():
    """Assistant 1% vs author 10% → CI strictly negative."""
    from backend.services.llm_mode_comparison import bootstrap_diff_ci
    out = bootstrap_diff_ci(
        author_total=500, author_pass=50,    # 10%
        assistant_total=500, assistant_pass=5,   # 1%
        iterations=1000,
        ci_level=0.80,
        seed=42,
    )
    assert out["effect_pct_pts"] == pytest.approx(-0.09)
    assert out["ci_upper"] < 0


def test_bootstrap_empty_pool_returns_insufficient():
    """Either pool empty → insufficient_samples=True."""
    from backend.services.llm_mode_comparison import bootstrap_diff_ci
    for ap, bp in [(0, 100), (100, 0), (0, 0)]:
        out = bootstrap_diff_ci(
            author_total=ap, author_pass=0,
            assistant_total=bp, assistant_pass=0,
        )
        assert out["insufficient_samples"] is True


def test_bootstrap_reproducible_with_seed():
    """Same seed → same CI."""
    from backend.services.llm_mode_comparison import bootstrap_diff_ci
    a = bootstrap_diff_ci(100, 10, 100, 15, iterations=500, seed=7)
    b = bootstrap_diff_ci(100, 10, 100, 15, iterations=500, seed=7)
    assert a["ci_lower"] == b["ci_lower"]
    assert a["ci_upper"] == b["ci_upper"]


# ---------------------------------------------------------------------------
# evaluate_go_gate decision matrix
# ---------------------------------------------------------------------------


def _make_comp(*, a_total, a_pass, b_total, b_pass):
    """Build a minimal comparison dict the evaluator expects."""
    return {
        "by_mode": {
            "author": {"total": a_total, "pass": a_pass, "rate": a_pass / max(1, a_total)},
            "assistant": {"total": b_total, "pass": b_pass, "rate": b_pass / max(1, b_total)},
        },
    }


def test_go_gate_insufficient_when_assistant_pool_empty():
    from backend.services.llm_mode_comparison import evaluate_go_gate
    decision = evaluate_go_gate(_make_comp(a_total=100, a_pass=5, b_total=0, b_pass=0))
    assert decision["decision"] == "INSUFFICIENT"


def test_go_gate_no_go_on_large_negative_effect():
    from backend.services.llm_mode_comparison import evaluate_go_gate
    decision = evaluate_go_gate(
        _make_comp(a_total=500, a_pass=100, b_total=500, b_pass=10),  # 20% → 2% (-18pp)
        seed=42,
    )
    assert decision["decision"] == "NO-GO"
    assert "underperforms" in decision["rationale"]


def test_go_gate_go_on_clear_positive_effect():
    from backend.services.llm_mode_comparison import evaluate_go_gate
    decision = evaluate_go_gate(
        _make_comp(a_total=500, a_pass=25, b_total=500, b_pass=100),  # 5% → 20%
        seed=42,
    )
    assert decision["decision"] == "GO"
    assert "statistically significant" in decision["rationale"]


def test_go_gate_partial_on_small_inconclusive_effect():
    """Tiny effect with wide CI straddling 0 → PARTIAL."""
    from backend.services.llm_mode_comparison import evaluate_go_gate
    # 5% vs 6%: effect=+1pp, but with 100/100 samples CI ~±5pp → straddles
    decision = evaluate_go_gate(
        _make_comp(a_total=100, a_pass=5, b_total=100, b_pass=6),
        seed=42,
    )
    assert decision["decision"] == "PARTIAL"


def test_go_gate_error_when_comparison_failed():
    from backend.services.llm_mode_comparison import evaluate_go_gate
    decision = evaluate_go_gate({"error": "db blew up"})
    assert decision["decision"] == "ERROR"
    assert "db blew up" in decision["rationale"]


# ---------------------------------------------------------------------------
# query_mode_pool — real in-memory aiosqlite integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_mode_pool_groups_by_mode_and_template(db_session):
    """Seed mixed alphas, verify by_mode + by_template + by_region_mode."""
    from backend.models import Alpha
    from backend.services.llm_mode_comparison import query_mode_pool

    now = datetime.utcnow()
    # Seed 6 alphas:
    # - 2 author USA PASS (1.5 / 2.0 sharpe)
    # - 1 author USA FAIL
    # - 2 assistant USA PASS via momentum.basic_ts_zscore (1.8 / 1.9)
    # - 1 assistant CHN PASS via value.book_to_market_rank with fallthrough
    db_session.add_all([
        Alpha(expression="rank(x)", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.5,
              metrics={}),  # author (no llm_mode_used → defaults author)
        Alpha(expression="rank(y)", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=2.0,
              metrics={"llm_mode_used": "author"}),
        Alpha(expression="rank(z)", region="USA", universe="TOP3000",
              quality_status="FAIL", is_sharpe=0.5,
              metrics={"llm_mode_used": "author"}),
        Alpha(expression="ts_zscore(a,60)", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.8,
              metrics={
                  "llm_mode_used": "assistant",
                  "assistant_template_id": "momentum.basic_ts_zscore",
                  "assistant_template_fallthrough": False,
              }),
        Alpha(expression="ts_zscore(b,60)", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.9,
              metrics={
                  "llm_mode_used": "assistant",
                  "assistant_template_id": "momentum.basic_ts_zscore",
                  "assistant_template_fallthrough": False,
              }),
        Alpha(expression="rank(book_to_market)", region="CHN",
              universe="TOP2000A", quality_status="PASS", is_sharpe=1.2,
              metrics={
                  "llm_mode_used": "assistant",
                  "assistant_template_fallthrough": True,  # template didn't match
              }),
    ])
    await db_session.commit()

    result = await query_mode_pool(db_session, days=30)
    assert result.get("error") is None
    assert result["total_alphas"] == 6
    assert result["window_days"] == 30

    # by_mode
    assert result["by_mode"]["author"]["total"] == 3
    assert result["by_mode"]["author"]["pass"] == 2
    assert result["by_mode"]["author"]["rate"] == pytest.approx(2 / 3)
    assert result["by_mode"]["assistant"]["total"] == 3
    assert result["by_mode"]["assistant"]["pass"] == 3
    assert result["by_mode"]["assistant"]["rate"] == pytest.approx(1.0)

    # by_region_mode
    assert result["by_region_mode"]["USA"]["author"]["total"] == 3
    assert result["by_region_mode"]["USA"]["assistant"]["total"] == 2
    assert result["by_region_mode"]["CHN"]["assistant"]["total"] == 1

    # by_template — only non-fallthrough assistant rows count
    assert "momentum.basic_ts_zscore" in result["by_template"]
    mb = result["by_template"]["momentum.basic_ts_zscore"]
    assert mb["total"] == 2 and mb["pass"] == 2
    # Fallthrough assistant alpha (CHN) should NOT be in by_template
    assert len(result["by_template"]) == 1

    # Fallthrough counter
    assert result["assistant_fallthrough_count"] == 1


@pytest.mark.asyncio
async def test_query_mode_pool_region_filter(db_session):
    """region=USA filter excludes CHN alphas."""
    from backend.models import Alpha
    from backend.services.llm_mode_comparison import query_mode_pool

    db_session.add_all([
        Alpha(expression="x", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.5, metrics={}),
        Alpha(expression="y", region="CHN", universe="TOP2000A",
              quality_status="PASS", is_sharpe=1.2, metrics={}),
    ])
    await db_session.commit()

    result = await query_mode_pool(db_session, days=30, region="USA")
    assert result["total_alphas"] == 1
    assert result["region_filter"] == "USA"


@pytest.mark.asyncio
async def test_query_mode_pool_excludes_old_alphas(db_session):
    """Alphas outside the window must be excluded."""
    from backend.models import Alpha
    from backend.services.llm_mode_comparison import query_mode_pool

    db_session.add_all([
        Alpha(expression="recent", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.5, metrics={},
              created_at=datetime.utcnow()),
        Alpha(expression="ancient", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.5, metrics={},
              created_at=datetime.utcnow() - timedelta(days=90)),
    ])
    await db_session.commit()
    result = await query_mode_pool(db_session, days=30)
    assert result["total_alphas"] == 1


@pytest.mark.asyncio
async def test_query_mode_pool_empty_db_returns_zero(db_session):
    from backend.services.llm_mode_comparison import query_mode_pool
    result = await query_mode_pool(db_session, days=30)
    assert result["total_alphas"] == 0
    assert result["by_mode"]["author"]["total"] == 0
    assert result["by_mode"]["assistant"]["total"] == 0
    assert result["by_template"] == {}
    assert result["assistant_fallthrough_count"] == 0


@pytest.mark.asyncio
async def test_query_mode_pool_soft_fails_on_broken_db():
    from backend.services.llm_mode_comparison import query_mode_pool
    class Broken:
        async def execute(self, *a, **kw):
            raise ConnectionError("db down")
    result = await query_mode_pool(Broken(), days=30)
    assert "error" in result
