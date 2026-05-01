"""can_submit decision: based on BRAIN /alphas/{id} `is.checks` array.

Rule (per user spec):
  - any check with result=FAIL → can_submit=False
  - no FAIL → can_submit=True
  - PENDING items (e.g. SELF_CORRELATION still computing) are NOT blockers,
    but reported separately in pending_checks so the UI can warn the user
    that the verdict may flip.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _extract_is_checks(brain_alpha: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull is.checks array out of a BRAIN GET /alphas/{id} response."""
    is_block = brain_alpha.get("is") if isinstance(brain_alpha, dict) else None
    if not isinstance(is_block, dict):
        return []
    checks = is_block.get("checks")
    return checks if isinstance(checks, list) else []


def compute_can_submit(
    brain_alpha: Optional[Dict[str, Any]],
) -> Tuple[Optional[bool], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Decide whether an alpha satisfies all BRAIN submission gates.

    Args:
        brain_alpha: The full JSON returned by BRAIN GET /alphas/{id}, or None
            if the call failed (treated as "unknown" — return None).

    Returns:
        (can_submit, failed_checks, pending_checks) where
          - can_submit = True/False/None (None = unable to decide, no overwrite)
          - failed_checks = list of {name, value, limit, ...} for FAIL items
          - pending_checks = list of {name, ...} for PENDING items
    """
    if brain_alpha is None:
        return None, [], []

    checks = _extract_is_checks(brain_alpha)
    if not checks:
        return None, [], []

    failed: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        result = c.get("result")
        if result == "FAIL":
            failed.append(_compact_check(c))
        elif result == "PENDING":
            pending.append(_compact_check(c))

    return (len(failed) == 0), failed, pending


def _compact_check(c: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a single check to the fields a UI tooltip / KB log actually needs."""
    out = {"name": c.get("name"), "result": c.get("result")}
    for k in ("value", "limit", "date"):
        if k in c:
            out[k] = c[k]
    return out
