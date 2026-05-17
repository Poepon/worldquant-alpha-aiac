"""Phase 1 Q6 (2026-05-17) Alpha191 CHN seed extractor tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# normalize_formula — Matlab notation, field lowercase, operator uppercase
# ---------------------------------------------------------------------------

class TestNormalizeFormula:
    def test_lowercases_uppercase_fields(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("MEAN(CLOSE, 20)")
        assert "close" in out
        assert "CLOSE" not in out

    def test_uppercases_lowercase_operators(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("delta(close, 1)")
        # delta → DELTA so QLIB_TO_BRAIN_OPERATORS lookup hits the Delta key
        # (CamelCase entry — _OPERATOR_NORMALIZE maps delta → Delta)
        assert "Delta" in out or "DELTA" in out

    def test_matlab_dotmul_normalized(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("(close-open).*volume")
        assert ".*" not in out
        assert "*volume" in out

    def test_matlab_dotdiv_normalized(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("(high-low)./close")
        assert "./" not in out
        assert "/close" in out

    def test_power_operator_to_function(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("returns^2")
        # `x^N` should become `power(x, N)` (later operator-normalize may
        # capitalize to Power — both are valid translator keys)
        assert "(returns, 2)" in out
        assert ("power" in out) or ("Power" in out)

    def test_strips_leading_formula_prefix(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("公式: RANK(close)")
        assert "公式" not in out
        # rank is downcased by operator normalizer, then translator uppercases again
        assert "RANK" in out or "rank" in out

    def test_balanced_outer_parens_stripped(self):
        from scripts.extract_alpha191 import normalize_formula
        out = normalize_formula("(RANK(close))")
        # Outer ( ) stripped exactly once when balanced — inner RANK(close)
        # keeps its parens
        assert out == "RANK(close)"


# ---------------------------------------------------------------------------
# parse_alpha191_source — docstring "公式:" extraction
# ---------------------------------------------------------------------------

class TestParseSource:
    def test_yields_alpha_id_and_formula(self):
        from scripts.extract_alpha191 import parse_alpha191_source
        fake_src = '''
def alpha_001(code, end_date=None, fq="pre"):
    """
    公式:
        (-1 * CORR(RANK(DELTA(LOG(VOLUME),1)),RANK(((CLOSE-OPEN)/OPEN)),6)
    Inputs:
        code: 股票池
    """
    pass

def alpha_002(code, end_date=None, fq="pre"):
    """
    公式:
        SUM(CLOSE, 10)
    Outputs:
        因子的值
    """
    pass
'''
        out = parse_alpha191_source(fake_src)
        assert len(out) == 2
        assert out[0]["alpha_id"] == 1
        assert "CORR" in out[0]["formula_raw"]
        assert out[1]["alpha_id"] == 2
        assert "SUM" in out[1]["formula_raw"]


# ---------------------------------------------------------------------------
# try_translate — gate ternary / unbalanced source / unknown op
# ---------------------------------------------------------------------------

class TestTryTranslate:
    def test_ternary_skipped(self):
        from scripts.extract_alpha191 import try_translate
        expr = "CLOSE > DELAY(CLOSE,1) ? 1 : -1"
        brain, reason = try_translate(99, expr)
        assert brain is None
        assert "untranslatable_pattern" in reason

    def test_logical_or_skipped(self):
        from scripts.extract_alpha191 import try_translate
        expr = "CLOSE > 0 || HIGH > LOW"
        brain, reason = try_translate(99, expr)
        assert brain is None
        assert "untranslatable_pattern" in reason

    def test_unbalanced_source_skipped(self):
        from scripts.extract_alpha191 import try_translate
        expr = "(-1 * CORR(RANK(VOLUME), 6)"  # missing one close paren
        brain, reason = try_translate(99, expr)
        assert brain is None
        assert "unbalanced" in reason

    def test_unknown_operator_skipped(self):
        from scripts.extract_alpha191 import try_translate
        expr = "SEQUENCE(VOLUME, 5)"
        brain, reason = try_translate(99, expr)
        assert brain is None
        assert "translator_skip" in reason or "translator_error" in reason

    def test_simple_alpha191_translates(self):
        from scripts.extract_alpha191 import try_translate
        expr = "(-1 * CORR(RANK(DELTA(LOG(VOLUME),1)),RANK(((CLOSE-OPEN)/OPEN)),6))"
        brain, reason = try_translate(1, expr)
        assert brain is not None
        assert reason == "ok"
        # Should have ts_corr (Qlib CORR → ts_corr), rank, ts_delta, log
        assert "ts_corr" in brain
        assert "rank" in brain
        assert "ts_delta" in brain
        assert "log" in brain


# ---------------------------------------------------------------------------
# JSON contents — verify production data quality
# ---------------------------------------------------------------------------

class TestAlpha191JsonContents:
    @pytest.fixture
    def data(self):
        path = Path("backend/data/alpha191_jq.json")
        if not path.exists():
            pytest.skip("alpha191_jq.json not yet generated; run extract_alpha191.py")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_at_least_30_rows(self, data):
        # Plan §5.2 lower bound — partial-OK threshold
        assert len(data) >= 30, f"only {len(data)} rows; plan §5.2 expects ≥30"

    def test_all_rows_have_required_fields(self, data):
        required = {"source", "pattern", "alpha191_id", "region", "horizon",
                    "qlib_origin", "category", "confidence"}
        for r in data[:5]:  # spot-check first 5 (catch schema drift fast)
            missing = required - set(r.keys())
            assert not missing, f"row missing fields: {missing}"

    def test_all_rows_region_chn(self, data):
        regions = {r["region"] for r in data}
        assert regions == {"CHN"}

    def test_all_rows_horizon_short(self, data):
        horizons = {r["horizon"] for r in data}
        assert horizons == {"short"}

    def test_no_unbalanced_parens_in_output(self, data):
        for r in data:
            opens = r["pattern"].count("(")
            closes = r["pattern"].count(")")
            assert opens == closes, (
                f"alpha_{r['alpha191_id']:03d} pattern has unbalanced parens "
                f"({opens} open / {closes} close): {r['pattern'][:80]}"
            )

    def test_no_matlab_dot_notation_leaked(self, data):
        for r in data:
            assert "./" not in r["pattern"], f"alpha_{r['alpha191_id']:03d}: Matlab ./"
            assert ".*" not in r["pattern"], f"alpha_{r['alpha191_id']:03d}: Matlab .*"

    def test_alpha191_id_unique(self, data):
        ids = [r["alpha191_id"] for r in data]
        assert len(ids) == len(set(ids)), "duplicate alpha191_id"


# ---------------------------------------------------------------------------
# import_alpha191_knowledge — function presence + signature
# ---------------------------------------------------------------------------

class TestImportFunction:
    def test_function_exists_and_async(self):
        from backend.external_knowledge import import_alpha191_knowledge
        import inspect
        assert inspect.iscoroutinefunction(import_alpha191_knowledge)

    def test_q6_import_batch_constant(self):
        from backend.external_knowledge import PHASE1_Q6_IMPORT_BATCH
        assert PHASE1_Q6_IMPORT_BATCH == "phase1_q6_alpha191_chn_2026_05"
