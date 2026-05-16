"""
Static Alpha Checks - expression-only suspicion checks, no metrics required.

These are the three V-16 risk audits that depend purely on the expression text
(no backtest metrics): look-ahead bias, divide-by-zero, and overfit-window.
Extracted from node_evaluate so they can run pre-simulate inside node_validate —
a bad expression should never burn a BRAIN simulation slot, and look-ahead bias
must be caught regardless of the post-simulate sharpe>3 suspicion gate.

The metric-dependent V-16 checks (cost_vacuum, outlier metrics, survivorship)
stay in node_evaluate — they need simulation results.

Pure functions only — no DB, no I/O. Logic is carried over verbatim from the
former evaluation.py implementation (V-26.69 / V-26.70 fixes included).
"""

from __future__ import annotations

import re
from typing import List, Optional

# Fields that can be 0 (returns on no-trade days, volume on halts, etc.)
DIVIDE_RISKY_DENOMS: set = {
    "returns", "volume", "amount",
    # Fundamental fields can be 0 / negative for distressed firms
    "net_income", "fnd6_newa2v1300_ni",
    "ebit", "fnd6_newa2v1300_oiadp",
    "total_equity", "fnd6_newa1v1300_ceq",
    # Synthetic-zero risks
    "high", "low",  # rare but high==low on illiquid
}

# Fields that arrive at announcement boundary; need ts_delay wrapping
LOOKAHEAD_FIELDS: tuple = (
    "actual_eps_value", "actual_sales_value",
    "actual_cashflow_per_share_value",
    "actual_dividend_value",
    # Earnings-event fields
    "fam_earn_date", "fam_earn_announce",
)

# Standard rolling-window sizes — anything outside is suspicious of
# parameter mining
STANDARD_WINDOWS: set = {1, 2, 3, 5, 10, 15, 20, 30, 60, 90, 120, 240, 480, 1200}

TS_WINDOW_RE = re.compile(r"\bts_\w+\s*\([^,()]+,\s*(\d+)\b")


def _extract_divide_denominators(expression: str) -> List[str]:
    """V-26.69 (2026-05-13): paren-balanced extraction of the 2nd
    argument to every `divide(...)` call in `expression`.

    Pre-fix the V-16 divide check used a flat regex
    `divide\\(\\s*[^,()]+,\\s*([a-zA-Z_]\\w*)\\s*\\)` which only matched a
    bare identifier denominator — anything nested (`divide(x, ts_mean(returns, 5))`
    or arithmetic compounds) was invisible. Now we walk paren depth so
    the denominator's full sub-expression is captured and any risky
    field token inside is detected.
    """
    out: List[str] = []
    n = len(expression)
    i = 0
    while i < n:
        idx = expression.find("divide(", i)
        if idx == -1:
            break
        # Position cursor inside divide('s arg list; depth=0 means at the
        # comma/close-paren that matches this divide call.
        depth = 0
        j = idx + len("divide(")
        comma_idx = -1
        while j < n:
            c = expression[j]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                comma_idx = j
                break
            j += 1
        if comma_idx == -1:
            i = idx + 1
            continue
        # Walk to matching close-paren of this divide(...)
        depth2 = 0
        k = comma_idx + 1
        while k < n:
            c = expression[k]
            if c == "(":
                depth2 += 1
            elif c == ")":
                if depth2 == 0:
                    break
                depth2 -= 1
            k += 1
        denom_expr = expression[comma_idx + 1:k].strip()
        if denom_expr:
            out.append(denom_expr)
        i = idx + 1
    return out


def check_divide_by_zero(expression: str) -> Optional[str]:
    """Risk 1: divide() with denominator that may be 0 on some dates.

    V-26.69: extracts the full denominator sub-expression and checks
    whether any risky field name appears as a token inside it. Catches
    `divide(x, ts_mean(returns, 5))` and arithmetic-compound denominators
    that the original shallow regex missed.
    """
    if not expression:
        return None
    denoms = _extract_divide_denominators(expression)
    if not denoms:
        return None
    for denom_expr in denoms:
        low = denom_expr.lower()
        for risky in DIVIDE_RISKY_DENOMS:
            if re.search(rf"\b{re.escape(risky)}\b", low):
                return f"divide(_, …{risky}…) — denominator can be 0"
    return None


def check_lookahead_bias(expression: str) -> Optional[str]:
    """Risk 2: announcement-type fields must be ts_delay-wrapped.

    V-26.70 (2026-05-13): pre-fix used `find` + `rfind` index comparison
    which counted ts_delay anywhere before the field — including a
    sibling `ts_delay(other_field, 1)` that's not actually wrapping the
    announcement field. Now we require both the field-presence check
    AND the ts_delay-wrap check to use whole-token (\\b...\\b) regex so:

      - `actual_eps_value_quarterly` doesn't trigger the
        `actual_eps_value` rule (substring false positive)
      - sibling ts_delay calls don't satisfy the direct-wrap requirement
        (the original false negative)
    """
    if not expression:
        return None
    el = expression.lower()
    for field in LOOKAHEAD_FIELDS:
        field_re = re.escape(field)
        # V-26.70: `field` is treated as a token-prefix — the announcement
        # field family includes suffixed variants like
        # `actual_eps_value_quarterly`. Word boundary on the LEFT only;
        # right side allows any word-char continuation. This is the same
        # token a direct ts_delay wrap must reference.
        token_re = rf"\b{field_re}\w*"
        if not re.search(token_re, el):
            continue
        # ts_delay must directly wrap a token in the same family.
        if re.search(rf"ts_delay\s*\(\s*{token_re}", el):
            continue
        return (
            f"announcement field '{field}' used without ts_delay direct-wrap "
            f"(sibling ts_delay does not mitigate lookahead)"
        )
    return None


def check_overfit_window(expression: str) -> Optional[str]:
    """Risk 5: ts_op uses non-standard window size suggesting parameter mining."""
    if not expression:
        return None
    weird = []
    for m in TS_WINDOW_RE.finditer(expression):
        n = int(m.group(1))
        if n > 1 and n not in STANDARD_WINDOWS:
            weird.append(n)
    if weird:
        return f"ts_op uses non-standard windows {weird} (standard: 5/10/20/60/120/240)"
    return None


def run_static_suspicion_checks(expression: str) -> List[dict]:
    """Run the three expression-only V-16 risk audits.

    Unlike node_evaluate's _run_suspicion_checks, there is NO sharpe gate —
    these checks are purely structural and run pre-simulate inside
    node_validate. Returns list[dict] with the same shape the post-simulate
    suspicion checks use:

      {"check": str, "severity": "hard" | "soft", "evidence": str}

    Severity semantics (consumed by node_validate):
      hard — look-ahead bias → invalidate the expression → SELF_CORRECT
      soft — divide-by-zero / overfit-window → annotate as a warning only
    """
    flags: List[dict] = []
    if not expression:
        return flags

    flag = check_lookahead_bias(expression)
    if flag:
        flags.append({"check": "lookahead_bias", "severity": "hard", "evidence": flag})

    flag = check_divide_by_zero(expression)
    if flag:
        flags.append({"check": "divide_by_zero", "severity": "soft", "evidence": flag})

    flag = check_overfit_window(expression)
    if flag:
        flags.append({"check": "overfit_window", "severity": "soft", "evidence": flag})

    return flags
