"""Feedback data structures — RE-EXPORT SHIM (Phase 1a, four-pool decoupling).

The real definitions of ``AttributionType`` + ``HypothesisFeedback`` were moved
to the pure leaf module ``backend/agents/attribution_types.py`` so the dead
RD-Agent ``core/`` cluster can be deleted in Phase 1c without stranding them.

This shim re-exports them so every existing ``from backend.agents.core.feedback
import ...`` (and the ``backend.agents.core`` package re-export) keeps working
byte-identically. The shim itself is deleted with the rest of ``core/`` in
Phase 1c; by then all surviving importers point at ``attribution_types`` directly.
"""

from backend.agents.attribution_types import AttributionType, HypothesisFeedback

__all__ = ["AttributionType", "HypothesisFeedback"]
