"""can_submit decision: based on BRAIN /alphas/{id} `is.checks` array.

Rule (per user spec):
  - any check with result=FAIL → can_submit=False
  - no FAIL → can_submit=True
  - PENDING items (e.g. SELF_CORRELATION still computing) are NOT blockers,
    but reported separately in pending_checks so the UI can warn the user
    that the verdict may flip.

V-26.81 (2026-05-13): the return type is `Optional[bool]` and None means
"BRAIN gave us no signal" (response missing, no checks). Callers MUST
distinguish None from False — `if not can_submit:` will treat both as
unsubmittable which can silently demote alphas after a transient BRAIN
hiccup. Use `can_submit is True` / `is False` / `is None` explicitly.

V-26.82 (2026-05-13): historically the function recognised only FAIL +
PENDING and silently ignored anything else (default = pass through). If
BRAIN ever adds a new result type (e.g. WARNING, ERROR) the alpha would
be labelled `can_submit=True` until someone notices. A logger.warning
surfaces unknown result types so the issue is observable; the verdict
keeps the conservative fall-back of treating unknowns as non-FAIL
because BRAIN's contract today is "non-FAIL ⇒ submittable".

V-26.77 follow-up #3 (2026-05-14): callers can now pass the locally
measured self-correlation as `local_self_corr` (+ source). When the
source is trusted (`local` / `brain`) and the value is at/above
`local_self_corr_threshold` (default 0.7) we synthesise a
`LOCAL_SELF_CORRELATION` failed check and force `can_submit=False`. This
closes a gap where BRAIN's SELF_CORRELATION sometimes reports PENDING
(non-blocking) even though we already measured the alpha is heavily
correlated with the existing OS pool — submitting that alpha wastes a
slot. Untrusted sources (`unknown` / None) leave the verdict to BRAIN.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger


# Known BRAIN check result types as of 2026-05-13. Anything outside this
# set triggers a logger.warning so future BRAIN API additions are noticed
# rather than being silently absorbed.
_KNOWN_RESULT_TYPES: Set[str] = {"PASS", "FAIL", "PENDING", "WARNING", "ERROR"}
_FAIL_RESULT_TYPES: Set[str] = {"FAIL", "ERROR"}  # ERROR is treated as fail
_PENDING_RESULT_TYPES: Set[str] = {"PENDING"}
_UNKNOWN_TYPES_SEEN: Set[str] = set()  # process-level dedup for log spam


def _extract_is_checks(brain_alpha: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull is.checks array out of a BRAIN GET /alphas/{id} response."""
    is_block = brain_alpha.get("is") if isinstance(brain_alpha, dict) else None
    if not isinstance(is_block, dict):
        return []
    checks = is_block.get("checks")
    return checks if isinstance(checks, list) else []


LOCAL_SELF_CORR_DEMOTE_THRESHOLD = 0.7
_TRUSTED_LOCAL_SELF_CORR_SOURCES: Set[str] = {"local", "brain"}


def _local_self_corr_demote_entry(
    local_self_corr: Optional[float],
    local_self_corr_source: Optional[str],
    local_self_corr_threshold: float,
) -> Optional[Dict[str, Any]]:
    """Synthetic LOCAL_SELF_CORRELATION FAIL check, or None.

    Returns a FAIL entry only when the local measurement is present, from a
    trusted source, and at/above the threshold. None otherwise (absent /
    untrusted / below threshold).
    """
    if (
        local_self_corr is not None
        and local_self_corr_source in _TRUSTED_LOCAL_SELF_CORR_SOURCES
        and local_self_corr >= local_self_corr_threshold
    ):
        return {
            "name": "LOCAL_SELF_CORRELATION",
            "result": "FAIL",
            "value": float(local_self_corr),
            "limit": float(local_self_corr_threshold),
            "source": local_self_corr_source,
        }
    return None


def compute_can_submit(
    brain_alpha: Optional[Dict[str, Any]],
    local_self_corr: Optional[float] = None,
    local_self_corr_source: Optional[str] = None,
    local_self_corr_threshold: float = LOCAL_SELF_CORR_DEMOTE_THRESHOLD,
) -> Tuple[Optional[bool], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Decide whether an alpha satisfies all BRAIN submission gates.

    Args:
        brain_alpha: The full JSON returned by BRAIN GET /alphas/{id}, or None
            if the call failed (treated as "unknown" — return None).
        local_self_corr: optional locally-measured self correlation (max corr
            against the OS alpha pool). Only acted on when source is trusted.
        local_self_corr_source: where the value came from. Only `"local"` or
            `"brain"` (the CorrelationService three-tier resolver) are trusted
            for the demote — `"unknown"` / cache-miss leaves it to BRAIN.
        local_self_corr_threshold: corr >= this forces demote. Default 0.7
            matches BRAIN's SELF_CORRELATION gate.

    Returns:
        (can_submit, failed_checks, pending_checks) where
          - can_submit = True/False/None (V-26.81: None ≠ False; see module
            docstring). True iff no FAIL/ERROR results in the checks array
            AND the trusted local self-corr (if any) is below the threshold.
          - failed_checks = list of {name, value, limit, ...} for FAIL/ERROR
            items; includes a synthetic LOCAL_SELF_CORRELATION entry when the
            local demote fires.
          - pending_checks = list of {name, ...} for PENDING items
    """
    local_fail = _local_self_corr_demote_entry(
        local_self_corr, local_self_corr_source, local_self_corr_threshold
    )

    # V-27.141: a trusted local self_corr >= threshold is an independent
    # "should reject" signal. Don't let it be buried when BRAIN is
    # unreachable (brain_alpha is None) or returned no checks array — those
    # only mean "no BRAIN signal", not "the alpha is fine".
    if brain_alpha is None:
        return (False, [local_fail], []) if local_fail else (None, [], [])

    checks = _extract_is_checks(brain_alpha)
    if not checks:
        return (False, [local_fail], []) if local_fail else (None, [], [])

    failed: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        result = c.get("result")
        if result in _FAIL_RESULT_TYPES:
            failed.append(_compact_check(c))
        elif result in _PENDING_RESULT_TYPES:
            pending.append(_compact_check(c))
        elif result not in _KNOWN_RESULT_TYPES and result is not None:
            # V-26.82: surface unknown BRAIN result type once per process
            # so a contract change doesn't silently flow through as PASS.
            if result not in _UNKNOWN_TYPES_SEEN:
                _UNKNOWN_TYPES_SEEN.add(result)
                logger.warning(
                    f"[can_submit] V-26.82 unknown BRAIN check result type "
                    f"{result!r} on check name={c.get('name')!r}; treating "
                    f"as non-FAIL but please verify BRAIN API contract"
                )

    if local_fail is not None:
        failed.append(local_fail)

    return (len(failed) == 0), failed, pending


def _compact_check(c: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a single check to the fields a UI tooltip / KB log actually needs."""
    out = {"name": c.get("name"), "result": c.get("result")}
    for k in ("value", "limit", "date"):
        if k in c:
            out[k] = c[k]
    return out
