"""Window-parameter perturbation — pure leaf utility (extracted from genetic_optimizer.py).

Deterministic window-grid enumeration over an alpha expression's
``(ts_*|group_*)`` call sites. Extracted out of ``genetic_optimizer.py`` (Phase 1a
of the four-pool decoupling) so the live ``RobustnessGate`` dependency
(multi_fidelity_eval.py → enumerate_window_perturbations) survives the deletion
of the dead ``GeneticOptimizer`` class in Phase 1c.

This module is a PURE LEAF: stdlib only (re + typing), zero project imports — it
must never import genetic_optimizer / multi_fidelity_eval (would re-introduce the
cycle this extraction breaks). ``genetic_optimizer.py`` re-imports these names for
backward compat until the GA class is deleted.
"""
import re
from typing import Any, Dict, List, Set, Tuple


# Window values for mutation / perturbation.
WINDOW_VALUES = [5, 10, 20, 22, 40, 44, 60, 66, 120, 126, 252]


_WINDOW_NAME_RE = re.compile(r'(ts_\w+|group_\w+)\s*\(')
_INT_LITERAL_RE = re.compile(r'^\d+$')


def _find_window_sites(expression: str) -> List[Dict[str, Any]]:
    """Walk balanced parens to find every ``(ts_*|group_*)(...)`` call site
    whose LAST positional arg is an integer literal (= the window).

    Handles binary (``ts_rank(close, 20)``), ternary
    (``ts_co_skewness(close, returns, 20)``), and arbitrarily-nested
    inner args (``ts_corr(rank(close), rank(returns), 20)``) — all of which
    a flat ``[^,]+`` regex cannot match.

    Returns a list of dicts: ``{func_name, window_value, window_start,
    window_end, call_start}`` — ``window_start/end`` are absolute positions
    suitable for slice substitution.
    """
    sites: List[Dict[str, Any]] = []
    for nm in _WINDOW_NAME_RE.finditer(expression):
        open_paren = nm.end() - 1  # absolute index of '('
        # Walk to matching close paren.
        depth = 0
        close = -1
        for i in range(open_paren, len(expression)):
            c = expression[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close < 0:
            continue  # unbalanced — skip silently
        body = expression[open_paren + 1:close]
        # Split body at depth-0 commas to enumerate positional args.
        arg_spans: List[Tuple[int, int]] = []
        depth = 0
        last = 0
        for j, c in enumerate(body):
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            elif c == ',' and depth == 0:
                arg_spans.append((last, j))
                last = j + 1
        arg_spans.append((last, len(body)))
        # Find the LAST positional arg that is purely an int literal — that's
        # the window for every BRAIN ts_*/group_* op we care about.
        # (Trailing kwargs like ``constant=False`` are non-digit and skipped.)
        for (a_start, a_end) in reversed(arg_spans):
            text = body[a_start:a_end]
            stripped = text.strip()
            if not _INT_LITERAL_RE.match(stripped):
                continue
            # Absolute positions of the literal in `expression`.
            offset = body[a_start:a_end].index(stripped)
            abs_start = open_paren + 1 + a_start + offset
            abs_end = abs_start + len(stripped)
            sites.append({
                "func_name": nm.group(1),
                "window_value": int(stripped),
                "window_start": abs_start,
                "window_end": abs_end,
                "call_start": nm.start(),
            })
            break  # at most one window per call site
    return sites


def enumerate_window_perturbations(
    expression: str,
    n: int = 4,
    *,
    selection_strategy: str = "first",
) -> List[Tuple[str, str]]:
    """Generate up to N deterministic unique window-parameter variants.

    Args:
        expression: alpha expression (may be empty/None — returns []).
        n: number of variants requested.
        selection_strategy: 'first' | 'largest' | 'all_in_order'.
            'first' (default, recommended for determinism): perturb the first
                ``(ts_*|group_*)`` call site whose last positional arg is an
                int literal; returns up to ``n`` nearest WINDOW_VALUES.
            'largest': perturb the site whose original window value is the
                largest; ties broken by ``call_start`` (earliest wins).
            'all_in_order': perturb up to ``n`` distinct sites in source order
                (one nearest variant per site).

    Returns:
        List of ``(new_expression, description)`` tuples — empty when no
        matching window site is found (caller treats as ``skip_reason='no_window'``).
        Order is by ``abs(w - original_window)`` ascending for first/largest,
        or by site position for all_in_order.  Dedup ensures no duplicate
        ``new_expression`` rows.

    Edge cases (P3 fix: now handles ternary + nested):
      - Empty / None expression                                → []
      - No ``(ts_\\w+|group_\\w+)`` site                        → []
      - Binary ``ts_rank(close, 20)``                          → ✓
      - Ternary ``ts_co_skewness(close, returns, 20)``         → ✓ (P3 fix)
      - Nested ``ts_corr(rank(close), rank(returns), 20)``     → ✓ (P3 fix)
      - n > available nearest values                           → truncated
      - Duplicate windows in expression                        → dedup'd
      - Original window not in WINDOW_VALUES (e.g. 7)          → still works
    """
    if not expression:
        return []

    sites = _find_window_sites(expression)
    if not sites:
        return []

    n = max(1, int(n))

    def _nearest_values(original: int, count: int) -> List[int]:
        """Return up to `count` WINDOW_VALUES nearest to `original`, excluding it.

        Tiebreaker: smaller value first (stable, deterministic)."""
        ordered = sorted(
            (w for w in WINDOW_VALUES if w != original),
            key=lambda w: (abs(w - original), w),
        )
        return ordered[:count]

    def _substitute(site: Dict[str, Any], new_window: int) -> str:
        return (
            expression[:site["window_start"]]
            + str(new_window)
            + expression[site["window_end"]:]
        )

    out: List[Tuple[str, str]] = []
    seen_exprs: Set[str] = set()

    if selection_strategy == "all_in_order":
        for site in sites[:n]:
            nearest = _nearest_values(site["window_value"], 1)
            if not nearest:
                continue
            new_window = nearest[0]
            new_expr = _substitute(site, new_window)
            if new_expr == expression or new_expr in seen_exprs:
                continue
            seen_exprs.add(new_expr)
            out.append((
                new_expr,
                f"window_perturbation: {site['func_name']} "
                f"{site['window_value']} -> {new_window}",
            ))
        return out

    if selection_strategy == "largest":
        # Largest window value; ties broken by earliest call position.
        chosen = max(sites, key=lambda s: (s["window_value"], -s["call_start"]))
    else:
        # 'first' (default) and any unknown strategy falls back to first.
        chosen = sites[0]

    orig = chosen["window_value"]
    func_name = chosen["func_name"]
    for new_window in _nearest_values(orig, n):
        new_expr = _substitute(chosen, new_window)
        if new_expr == expression or new_expr in seen_exprs:
            continue
        seen_exprs.add(new_expr)
        out.append((
            new_expr,
            f"window_perturbation: {func_name} {orig} -> {new_window}",
        ))
    return out
