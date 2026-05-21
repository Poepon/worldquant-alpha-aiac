"""Phase 4 Sprint 1 A1.1 — LLM mode service + state machine.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.1

Pattern: 8 industrial-派 共识 — "LLM 是 research assistant 不是
expression-author" (Citadel / Two Sigma / Bridgewater AIA Labs). AIAC v1
ships LLM-as-author; this service is the foundation for a per-task
opt-in 'assistant' mode that lets the LLM emit hypothesis text + lets a
GA + template library compose the actual BRAIN DSL expression.

A1 split into 4 sub-PRs:
  - A1.1 (THIS file)  — pure-function service: resolve_mode +
                        drain_pending_residue + mode_change_requires_drain
  - A1.2              — feature_flag_service.set sentinel guard (flips 6
                        LIVE flag OFF) + audit + restore_sentinel
  - A1.3              — code_gen branching + assistant template library
  - A1.4              — /ops/llm-mode/comparison endpoint + bootstrap CI
                        GO gate

This file does NOT (yet) flip global sentinel flags or branch code_gen.
It exposes the primitives those PRs will call.

State machine
-------------
Three task states a caller must distinguish to drain residue correctly::

    NEW_POST       — Task being created via POST. ``resolve_mode`` reads
                     payload-provided task.config["llm_mode"] (or default
                     "author"). No drain — fresh task has no residue.

    RUNNING_GRANDFATHER — Mode flipped mid-task (operator changed
                     ENABLE_LLM_ASSISTANT_MODE globally OR edited
                     task.config["llm_mode"]). Current in-flight round
                     completes under PRIOR mode (don't re-key the LLM
                     mid-prompt); ``drain_pending_residue`` at round-end
                     hook ensures the NEXT round starts clean.

    PAUSED_RESUME  — Task previously paused (R14 stop_loss, operator,
                     etc.) and now resuming. Apply current resolved mode
                     at resume time + drain residue (the residue keys
                     could be stale from before the pause).

Caller is responsible for distinguishing these states; this service
exposes the building blocks.

Soft-fail philosophy
--------------------
``drain_pending_residue`` swallows DB / ORM errors and returns a partial
forensic dict. Mode resolution NEVER raises — flag OFF / missing
task.config / corrupted JSON all collapse to default "author".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("services.llm_mode")


# Public mode literals — kept as strings (not Enum) so they round-trip
# through JSONB task.config without enum serialization plumbing.
MODE_AUTHOR = "author"
MODE_ASSISTANT = "assistant"
VALID_MODES = (MODE_AUTHOR, MODE_ASSISTANT)


def resolve_mode(task, *, settings=None) -> str:
    """Return the LLM mode that should be active for ``task`` right now.

    Priority chain (highest first):
      1. ``ENABLE_LLM_ASSISTANT_MODE=False`` (global kill switch)
         → MODE_AUTHOR regardless of task.config.  This is the kill-switch
         operator can flip during incidents — instantly reverts every
         in-flight task to author mode.
      2. ``task.config["llm_mode"]`` if present + valid → that mode.
         Lets operator gate per-task (set on POST, edit anytime).
      3. Default → MODE_AUTHOR.

    NEVER raises. Unknown / malformed values silently fall back to
    MODE_AUTHOR with a debug log.

    Args:
      task: ORM row (MiningTask) or any object with a ``.config`` dict
            attribute. Pass any duck-type with .config to test.
      settings: Settings instance. None → lazy import.
    """
    if settings is None:
        from backend.config import settings as _stg
        settings = _stg

    # Layer 1: global kill switch
    if not bool(getattr(settings, "ENABLE_LLM_ASSISTANT_MODE", False)):
        return MODE_AUTHOR

    # Layer 2: task.config opt-in
    try:
        cfg = getattr(task, "config", None) or {}
        if isinstance(cfg, dict):
            requested = cfg.get("llm_mode")
            if isinstance(requested, str) and requested in VALID_MODES:
                return requested
            if requested is not None:
                logger.debug(
                    "[llm_mode] task=%s invalid llm_mode=%r in task.config; "
                    "falling back to author",
                    getattr(task, "id", "?"), requested,
                )
    except Exception as ex:  # noqa: BLE001
        logger.debug("[llm_mode] resolve_mode task=%s read failed: %s",
                     getattr(task, "id", "?"), ex)

    # Layer 3: default
    return MODE_AUTHOR


def mode_change_requires_drain(prev_mode: Optional[str], new_mode: str) -> bool:
    """Decide whether a transition from ``prev_mode`` to ``new_mode``
    needs the residue keys drained.

    Drain is needed when:
      - prev_mode is set and != new_mode (mode flipped)

    Not needed when:
      - prev_mode is None (fresh task, no residue could exist)
      - prev_mode == new_mode (no flip)
    """
    if prev_mode is None:
        return False
    if not isinstance(prev_mode, str) or not isinstance(new_mode, str):
        return False
    return prev_mode != new_mode


def drain_pending_residue(
    task,
    *,
    settings=None,
    keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Pop the configured cross-mode residue keys off ``task.config``.

    Returns a forensic dict ``{key: prior_value}`` of what was actually
    drained (for audit / log). NEVER raises — soft-fail returns
    ``{"_error": "..."}`` on any exception.

    Implementation note: this mutates ``task.config`` in-place + sets
    ``task.config = new_dict`` so SQLAlchemy's JSONB change detection
    works WITHOUT needing the caller to call flag_modified (still safe
    to call flag_modified afterward — idempotent).

    Args:
      task: ORM row or duck-type with mutable ``.config`` dict.
      settings: Settings instance. None → lazy import.
      keys: Override the LLM_ASSISTANT_RESIDUE_KEYS list (testing).
    """
    if settings is None:
        from backend.config import settings as _stg
        settings = _stg

    drain_keys = (
        list(keys)
        if keys is not None
        else list(getattr(settings, "LLM_ASSISTANT_RESIDUE_KEYS", []) or [])
    )

    out: Dict[str, Any] = {}
    try:
        cfg = getattr(task, "config", None) or {}
        if not isinstance(cfg, dict):
            return {"_error": "task.config is not a dict"}
        new_cfg = dict(cfg)
        for k in drain_keys:
            if k in new_cfg:
                out[k] = new_cfg.pop(k)
        # Always reassign, even if nothing was popped, so the caller's
        # subsequent flag_modified picks up no-op consistently.
        task.config = new_cfg
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "config")
        except Exception:  # noqa: BLE001
            pass
        if out:
            logger.info(
                "[llm_mode] drain_pending_residue task=%s drained=%s",
                getattr(task, "id", "?"), sorted(out.keys()),
            )
        return out
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[llm_mode] drain_pending_residue task=%s failed: %s",
            getattr(task, "id", "?"), ex,
        )
        return {"_error": str(ex)[:200]}


def resolve_mode_and_drain_if_needed(
    task,
    *,
    prev_mode: Optional[str],
    settings=None,
) -> Tuple[str, Dict[str, Any]]:
    """Convenience composer that drives the state machine for a *single*
    decision point (e.g. round-end hook deciding whether to drain before
    the next round).

    Returns ``(new_mode, drained_residue)``. Drains only when
    ``mode_change_requires_drain(prev_mode, new_mode)`` returns True.

    Caller uses this in three states:
      - NEW_POST          → pass prev_mode=None (no drain)
      - RUNNING_GRAND     → pass prev_mode=state.llm_mode_used (current
                            in-flight round's mode); drain happens between
                            rounds when caller calls again with prev=now
      - PAUSED_RESUME     → pass prev_mode=stored mode from task.config
                            ["llm_mode_used_at_pause"] (if recorded);
                            drain on any mode change since pause
    """
    new_mode = resolve_mode(task, settings=settings)
    drained: Dict[str, Any] = {}
    if mode_change_requires_drain(prev_mode, new_mode):
        drained = drain_pending_residue(task, settings=settings)
    return new_mode, drained


__all__ = [
    "MODE_AUTHOR",
    "MODE_ASSISTANT",
    "VALID_MODES",
    "resolve_mode",
    "mode_change_requires_drain",
    "drain_pending_residue",
    "resolve_mode_and_drain_if_needed",
]
