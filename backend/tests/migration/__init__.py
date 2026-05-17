"""Phase 1.5+ Alembic migration tests.

Tests marked @pytest.mark.requires_postgres are skipped unless PG_TEST_DSN
env var is set (see conftest.py pytest_collection_modifyitems hook).
"""
