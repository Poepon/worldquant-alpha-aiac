"""Resident pool pipeline (four-pool decoupling, Phase 1b).

DB-persistent claim/lease queues + HG/S/E worker loops + scheduler/lease-recycle
beats + Popen-respawn supervisor. INERT until ``ENABLE_POOL_PIPELINE`` is flipped
ON (Phase 1c-flip); runs in parallel with the legacy FLAT pipeline until then.

See docs/phase1b_pool_implementation_design_2026-06-06.md.
"""
