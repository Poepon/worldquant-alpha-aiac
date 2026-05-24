"""Unit tests for Settings.iqc_audit_scope() — the IQC marginal-audit scope
resolver introduced in the 2026-05-24 IQC2026S1→team migration.

Pure logic (no PG / no BRAIN); constructs throwaway Settings instances with
explicit overrides so the precedence rules are locked in.
"""
from __future__ import annotations

from backend.config import Settings


class TestIqcAuditScope:
    def test_competition_wins_when_set(self):
        s = Settings(IQC_AUTO_AUDIT_COMPETITION="IQC2026S2", IQC_AUTO_AUDIT_TEAM="deLkl06")
        assert s.iqc_audit_scope() == ("IQC2026S2", None)

    def test_team_used_when_competition_empty(self):
        s = Settings(IQC_AUTO_AUDIT_COMPETITION="", IQC_AUTO_AUDIT_TEAM="deLkl06")
        assert s.iqc_audit_scope() == (None, "deLkl06")

    def test_both_empty_disables_audit(self):
        s = Settings(IQC_AUTO_AUDIT_COMPETITION="", IQC_AUTO_AUDIT_TEAM="")
        assert s.iqc_audit_scope() == (None, None)

    def test_whitespace_is_stripped(self):
        s = Settings(IQC_AUTO_AUDIT_COMPETITION="   ", IQC_AUTO_AUDIT_TEAM="  deLkl06  ")
        assert s.iqc_audit_scope() == (None, "deLkl06")

    def test_default_scope_is_team_delkl06(self, monkeypatch):
        """The shipped default after IQC2026S1 was deleted: team scope, no competition.

        Isolate from ambient state — a bare ``Settings()`` reads process env AND
        the .env file (pydantic-settings: init > env > .env > default). Clear the
        process env vars and disable .env loading (``_env_file=None``) so the
        assertion tests the shipped field defaults, not the host/CI environment.
        """
        monkeypatch.delenv("IQC_AUTO_AUDIT_COMPETITION", raising=False)
        monkeypatch.delenv("IQC_AUTO_AUDIT_TEAM", raising=False)
        s = Settings(_env_file=None)
        assert s.iqc_audit_scope() == (None, "deLkl06")
