"""Unit tests for backend/is_diagnostic_card.py (Phase C, 2026-06-08).

Pure aggregation of already-live submit-selection signals → 5-dim IS card.
"""
from backend.is_diagnostic_card import build_diagnostic_card


def _card(**kw):
    base = dict(
        sharpe=1.5, turnover=0.2, margin_bps=8.0, corr_to_pool=0.3,
        robustness_verdict="ROBUST", robustness_score=0.8,
        marginal_verdict="SUBMIT", sub_universe_sharpe=0.9,
    )
    base.update(kw)
    return build_diagnostic_card(**base)


def test_all_green_recommends_submit():
    c = _card()
    assert c["overall"] == "SUBMIT"
    assert c["basis"] == "IS"
    assert c["dims"]["overfit"]["level"] == "ok"
    assert c["dims"]["crowding"]["level"] == "ok"
    assert c["dims"]["liquidity"]["level"] == "ok"
    assert c["dims"]["sub_universe"]["level"] == "ok"


def test_margin_below_floor_skips_even_if_clean():
    c = _card(margin_bps=3.0)          # < 5bps floor
    assert c["overall"] == "SKIP"
    assert "margin" in c["reason"]


def test_negative_margin_hard_skip():
    c = _card(margin_bps=-2.0)
    assert c["overall"] == "SKIP"


def test_fools_gold_high_sharpe_flags_overfit_risk_and_holds():
    c = _card(sharpe=3.5)             # >= 3.0 fool's-gold
    assert c["dims"]["overfit"]["level"] == "risk"
    assert c["dims"]["overfit"]["fools_gold"] is True
    assert c["overall"] == "HOLD"


def test_fragile_robustness_flags_overfit_risk():
    c = _card(robustness_verdict="FRAGILE", robustness_score=0.2)
    assert c["dims"]["overfit"]["level"] == "risk"
    assert c["overall"] == "HOLD"


def test_crowding_redline_holds():
    c = _card(corr_to_pool=0.75)      # >= 0.7 near-duplicate red-line
    assert c["dims"]["crowding"]["level"] == "risk"
    assert c["overall"] == "HOLD"


def test_low_sub_universe_sharpe_risk():
    c = _card(sub_universe_sharpe=0.3)   # < 0.49 (0.7*0.7) → risk
    assert c["dims"]["sub_universe"]["level"] == "risk"
    assert c["overall"] == "HOLD"


def test_marginal_skip_propagates():
    c = _card(marginal_verdict="SKIP")
    assert c["overall"] == "SKIP"


def test_marginal_neutral_clean_is_review():
    # No hard risks, margin ok, but marginal scorecard isn't a SUBMIT → human review.
    c = _card(marginal_verdict="NEUTRAL")
    assert c["overall"] == "REVIEW"


def test_unknown_dims_when_no_pnl():
    c = _card(robustness_verdict=None, robustness_score=None,
              corr_to_pool=None, turnover=None, sub_universe_sharpe=None)
    assert c["dims"]["overfit"]["level"] == "unknown"
    assert c["dims"]["crowding"]["level"] == "unknown"
    assert c["dims"]["liquidity"]["level"] == "unknown"
    assert c["dims"]["sub_universe"]["level"] == "unknown"
    # margin ok + marginal SUBMIT but overfit/crowding not "ok" → REVIEW (not SUBMIT)
    assert c["overall"] == "REVIEW"


def test_turnover_bands():
    assert _card(turnover=0.2)["dims"]["liquidity"]["level"] == "ok"
    assert _card(turnover=0.4)["dims"]["liquidity"]["level"] == "warn"
    assert _card(turnover=0.6)["dims"]["liquidity"]["level"] == "risk"
