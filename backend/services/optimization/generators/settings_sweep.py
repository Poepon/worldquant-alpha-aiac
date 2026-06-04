"""SettingsSweepGenerator — Stage A's only generator.

Produces up to ``max_variants`` variants per candidate alpha (default 10, wired
from ``settings.MAX_OPTIMIZATION_VARIANTS`` by the factory; capped at the 10-cell
grid below — to exceed 10 you must add rows to ``_GRID``). The grid is traversed
in order and truncated to ``max_variants`` BEFORE the dedup pass, so a smaller cap
keeps the most important cells first (anchor → neut swap → decay sweep → …) and
spends fewer BRAIN sim slots. Variants sweep three axes:

  - decay ∈ {0, 4, 16, 64}
  - window ∈ {20, 40, 60, 120}      (rewrites the first ts_*(_, N) numeric
                                     window; falls back to baseline window
                                     when the expression has none)
  - neutralization ∈ {INDUSTRY, SECTOR}     (SUBINDUSTRY dropped — 15621
                                     showed ≈0.01 sharpe delta vs INDUSTRY
                                     so not worth the BRAIN sim slot)

The grid is hand-picked (not a full 32-cell cartesian product) to give
each axis at least 4 distinct draws within 10 variants. Order is
deterministic so a given alpha always generates the same 10 variants —
Stage A's dedup queries by ``(parent_alpha_id, expression_hash, settings)``
depend on this stability.

15621 → 15720 (the empirical anchor for the whole optimization closure)
landed via the first row of this grid (``decay=4|neut=INDUSTRY``, baseline
window). Listing it first keeps that path "obvious" in audit trails.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §6.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from backend.services.optimization.protocols import Variant


# The 10-variant grid. Tuple = (neut, decay, window_override).
# ``window_override = None`` → keep the baseline expression's window
# (or skip the variant entirely if the dedup pass would collapse it).
_GRID: Tuple[Tuple[str, int, Optional[int]], ...] = (
    ("INDUSTRY", 4, None),       # 1. 15621-winner-shape (anchor)
    ("SECTOR",   4, None),       # 2. neut swap
    ("INDUSTRY", 0, None),       # 3. decay sweep — fast
    ("INDUSTRY", 16, None),      # 4. decay sweep — slow
    ("INDUSTRY", 64, None),      # 5. decay sweep — very slow
    ("INDUSTRY", 4, 20),         # 6. window sweep — short
    ("INDUSTRY", 4, 40),         # 7. window sweep — mid-short
    ("INDUSTRY", 4, 120),        # 8. window sweep — long
    ("SECTOR",   0, None),       # 9. SECTOR × fast decay combo
    ("SECTOR",   16, None),      # 10. SECTOR × slow decay combo
)

# Matches the numeric second arg of a ts_* call. The negative lookbehind
# rules out matching the "60" inside any non-ts_ function. Uses ``[^()]*``
# instead of ``[^,]*`` so nested calls like ``divide(x, y)`` don't break
# the match (the comma inside divide is inside the inner parens, but our
# capture starts AFTER the outermost paren-comma of the ts_* call so it's
# only the last numeric arg of the ts_* call itself that matches).
_TS_WINDOW_RE = re.compile(r",\s*(\d+)\s*\)")


def _extract_first_window(expression: str) -> Optional[int]:
    """Return the first ``, N)`` numeric arg in the expression, or None.

    Used for two purposes:
      - Skip window-axis variants whose target window equals the baseline
        (dedup collapses them anyway).
      - Tag the baseline window in audit trails.
    """
    m = _TS_WINDOW_RE.search(expression)
    return int(m.group(1)) if m else None


def _substitute_first_window(expression: str, new_window: int) -> str:
    """Replace the first ``, N)`` window arg with ``, new_window)``.

    Idempotent on expressions without a window (returns unchanged), so
    Stage A's "no ts_* window" alphas just get the baseline expression
    back — dedup later collapses them.
    """
    return _TS_WINDOW_RE.sub(
        lambda m: f", {new_window})", expression, count=1
    )


class SettingsSweepGenerator:
    """Layer-2 generator. Stateless — single instance is fine across cycles.

    ``max_variants`` caps how many of the ``_GRID`` cells are tried (default 10 =
    the whole grid, so the default is byte-identical to the pre-config behaviour).
    Injected from ``settings.MAX_OPTIMIZATION_VARIANTS`` by ``build_optimization_
    service``. Clamped to [1, len(_GRID)] — a value above the grid length just
    uses the full grid (the grid is the hard ceiling).
    """

    name = "settings_sweep"

    def __init__(self, max_variants: int = 10) -> None:
        self._max_variants = max(1, min(int(max_variants), len(_GRID)))

    async def generate(self, alpha) -> List[Variant]:
        """Produce up to 10 variants from ``alpha`` (a backend.models.Alpha row).

        ``settings`` on each variant is BRAIN-ready (region/universe/delay/
        decay/neutralization/truncation/test_period). ``tag`` is the
        human-readable axis label. ``generation`` is always 0 (settings
        sweep doesn't have GA depth).

        Dedup pass at the end removes variants whose ``(expression, settings)``
        collide — e.g. window=baseline_window is a no-op against the
        unmodified expression so it collapses into another variant that
        also kept the baseline expression.
        """
        baseline_expr = alpha.expression
        baseline_window = _extract_first_window(baseline_expr)
        # NOTE: do NOT collapse via `or` — alpha.delay=0 is valid (delay-0
        # native mining) and `0 or 1 == 1` would silently misroute the variant
        # to the delay-1 BRAIN band. Use explicit None check / fallback.
        _trunc = getattr(alpha, "truncation", None)
        baseline_truncation = float(0.08 if _trunc is None else _trunc)
        _region = getattr(alpha, "region", None)
        baseline_region = str("USA" if _region is None else _region)
        _universe = getattr(alpha, "universe", None)
        baseline_universe = str("TOP3000" if _universe is None else _universe)
        _delay = getattr(alpha, "delay", None)
        baseline_delay = int(1 if _delay is None else _delay)

        out: List[Variant] = []
        for neut, decay, window_override in _GRID[:self._max_variants]:
            expr = baseline_expr
            tag_parts = [f"decay={decay}", f"neut={neut}"]
            if window_override is not None and baseline_window is not None and (
                window_override != baseline_window
            ):
                expr = _substitute_first_window(baseline_expr, window_override)
                tag_parts.append(f"window={window_override}")
            elif window_override is not None and baseline_window is None:
                # Expression has no ts_* window → window-axis variant is
                # indistinguishable from a pure decay/neut one. Tag the
                # intent anyway so audit can see we tried.
                tag_parts.append(f"window={window_override}(no-op)")
            settings = {
                "region": baseline_region,
                "universe": baseline_universe,
                "delay": baseline_delay,
                "decay": int(decay),
                "neutralization": neut,
                "truncation": baseline_truncation,
                "test_period": "P2Y0M",
            }
            out.append(Variant(
                expression=expr,
                settings=settings,
                tag="|".join(tag_parts),
                generator_name=self.name,
            ))

        # Dedup: (expression, sorted settings tuple) — two variants that
        # collapsed to the same (expr, settings) are the same BRAIN sim.
        seen = set()
        deduped: List[Variant] = []
        for v in out:
            key = (v.expression, tuple(sorted(v.settings.items())))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(v)
        return deduped
