"""Integration-test conftest (no-op placeholder).

The P3-Brain integration tests added in 2026-05-16 (test_self_corr_cache,
test_brain_role_router, etc.) all run with AsyncMock-based fixtures and
do NOT need a SQLite engine — the schema includes Postgres-specific
features (interval columns, JSONB, ARRAY) that don't compile cleanly
with simple type substitutions. Tests that need real SQL should use the
Postgres fixture in test_v27_1_cascade_lock_takeover.py.
"""
from __future__ import annotations
