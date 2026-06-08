"""Backlog candidate current-data re-sim verdict (v2, 2026-06-08).

Pure, dependency-free verdict logic for the on-demand "current-data re-sim /
decay check" feature (`docs/submit_backlog_resim_current_design_2026-06-08.md`).
The celery task (`tasks/resim_backlog_tasks.py`) re-sims a backlog candidate on
CURRENT BRAIN data (口径 = current IS, NOT OS — BRAIN hides realized OS) and
feeds the result here to classify decay.

Design (post adversarial-review `wqxbthho4`):
  - **No decay perturbation.** v1's decay+1/+2 cache-buster measured a PERTURBED
    alpha, not the original on current data (review H1/H7). Empirically ~17/23
    alphas re-sim fresh with their stored settings anyway; the ~26% that return
    EXACTLY the stored metrics (BRAIN dedup) are honestly marked
    ``unmeasurable_cached`` rather than force-perturbed.
  - **Relative-to-baseline verdict** (review H2): an absolute 1.25 gate can't tell
    "1.0→0.8 (mild)" from "2.5→0.8 (regime collapse)". We classify on
    resim/baseline.
  - **Margin economic gate** (review H3): a sharpe that "holds" while margin drops
    below the 5bps cost floor is economically dead → ``margin_killed`` (priority
    over the sharpe band).
  - **NO-GO consistency** (review H4): a held alpha that is still gated
    (can_submit False) is ``hold_gated`` — "held ≠ should-submit; still must clear
    self_corr<0.7 + marginal". The UI surfaces self_corr/marginal alongside.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# verdict vocabulary (ordered by "should I act" severity for the UI legend)
VERDICT_STABLE = "stable"                    # resim ≥ stable_ratio × baseline
VERDICT_HOLD_GATED = "hold_gated"            # stable BUT can_submit False (still gated)
VERDICT_SOFT_DECAY = "soft_decay"            # soft_ratio ≤ resim/baseline < stable_ratio
VERDICT_HARD_DECAY = "hard_decay"            # resim/baseline < soft_ratio
VERDICT_MARGIN_KILLED = "margin_killed"      # resim margin < floor (economic death)
VERDICT_UNMEASURABLE = "unmeasurable_cached"  # BRAIN returned stored metrics (dedup) — current data not measured
VERDICT_ERROR = "error"                      # sim failed (timeout/auth/etc.)


def is_stale_resim(
    baseline_sharpe: Optional[float],
    resim_sharpe: Optional[float],
    stale_eps: float = 1e-3,
) -> bool:
    """True iff the re-sim came back EXACTLY at baseline (|Δ|≤eps) → BRAIN dedup
    returned the stored alpha's original metrics instead of a fresh current-window
    sim. Mirrors ``regime_monitor._is_stale`` (kept as a shared, importable helper
    so the two surfaces can't drift)."""
    if resim_sharpe is None or baseline_sharpe is None:
        return False
    return abs(resim_sharpe - baseline_sharpe) <= stale_eps


def build_resim_verdict(
    *,
    baseline_sharpe: Optional[float],
    resim_sharpe: Optional[float],
    resim_margin_bps: Optional[float] = None,
    can_submit: bool = True,
    error: Optional[str] = None,
    stale_eps: float = 1e-3,
    margin_floor_bps: float = 5.0,
    stable_ratio: float = 0.9,
    soft_ratio: float = 0.6,
    baseline_floor: float = 0.1,
) -> Dict[str, Any]:
    """Classify a single candidate's current-data re-sim vs its frozen-IS baseline.

    Returns ``{verdict, resim_sharpe, baseline_sharpe, resim_pct, resim_margin_bps,
    reason, basis}`` where ``resim_pct`` = resim/baseline (None when baseline≈0).
    ``basis`` is always ``"IS"`` (BRAIN hides OS).
    """
    out: Dict[str, Any] = {
        "baseline_sharpe": baseline_sharpe,
        "resim_sharpe": resim_sharpe,
        "resim_margin_bps": resim_margin_bps,
        "resim_pct": None,
        "basis": "IS",
    }

    # 1. hard error (sim failed) — caller couldn't get any value.
    if error or resim_sharpe is None:
        out["verdict"] = VERDICT_ERROR
        out["reason"] = f"re-sim 失败:{error}" if error else "re-sim 无返回值"
        return out

    # 2. dedup / cache hit — BRAIN returned the stored metrics, NOT current data.
    if is_stale_resim(baseline_sharpe, resim_sharpe, stale_eps):
        out["verdict"] = VERDICT_UNMEASURABLE
        out["reason"] = "BRAIN 返存储值(dedup),当前数据无法测——非衰减结论"
        return out

    # 3. economic margin gate — a held sharpe with sub-floor margin is dead.
    if resim_margin_bps is not None and resim_margin_bps < margin_floor_bps:
        out["verdict"] = VERDICT_MARGIN_KILLED
        out["reason"] = (
            f"当前 margin {resim_margin_bps:.1f}bps < {margin_floor_bps:.0f}bps 经济门"
            "(扣成本不盈利,即便 sharpe 未崩)"
        )
        return out

    # 4. relative-to-baseline decay band.
    if baseline_sharpe is None or abs(baseline_sharpe) < baseline_floor:
        # baseline ~0 → ratio undefined; fall back to absolute recovery sense.
        held = resim_sharpe >= 1.25
        out["verdict"] = VERDICT_STABLE if held else VERDICT_HARD_DECAY
        out["reason"] = (
            f"baseline≈0 无法算相对衰减,按绝对当前 sharpe {resim_sharpe:.2f} "
            f"{'≥1.25 视为有效' if held else '<1.25 视为失效'}"
        )
    else:
        pct = resim_sharpe / baseline_sharpe
        out["resim_pct"] = pct
        if pct >= stable_ratio:
            out["verdict"] = VERDICT_STABLE
            out["reason"] = f"当前 {resim_sharpe:.2f} = baseline 的 {pct * 100:.0f}%(持平)"
        elif pct >= soft_ratio:
            out["verdict"] = VERDICT_SOFT_DECAY
            out["reason"] = f"当前 {resim_sharpe:.2f} = baseline 的 {pct * 100:.0f}%(软衰减)"
        else:
            out["verdict"] = VERDICT_HARD_DECAY
            out["reason"] = f"当前 {resim_sharpe:.2f} = baseline 的 {pct * 100:.0f}%(硬衰减)"

    # 5. NO-GO consistency: a "stable" alpha that is still gated is held-but-gated.
    #    Held ≠ should-submit — it must still clear self_corr<0.7 + marginal dilution.
    if out["verdict"] == VERDICT_STABLE and not can_submit:
        out["verdict"] = VERDICT_HOLD_GATED
        out["reason"] += ";但仍被门挡(can_submit=False)→ 非可提交信号"

    return out
