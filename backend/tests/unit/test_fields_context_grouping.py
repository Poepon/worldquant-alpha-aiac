"""build_fields_context must (a) surface GROUP fields with explicit
grouping-only guidance and (b) never crowd MATRIX value fields out on a
GROUP-heavy dataset (pv13: 135 GROUP / 30 MATRIX) — otherwise the LLM uses
GROUP fields as ts_*/arithmetic value inputs (2026-05-23 fix).
"""
from backend.agents.prompts.base import build_fields_context


def test_group_fields_get_grouping_only_guidance():
    fields = [
        {"id": "pv13_2l_scibr", "type": "GROUP"},
        {"id": "sector", "type": "GROUP"},
        {"id": "close", "type": "MATRIX"},
    ]
    out = build_fields_context(fields)
    assert "GROUP fields" in out
    assert "ONLY as the grouping argument" in out
    assert "NEVER as a value input" in out
    # a GROUP field must not be advertised under the MATRIX value bucket
    matrix_line = next(l for l in out.splitlines() if "MATRIX fields" in l)
    assert "pv13_2l_scibr" not in matrix_line
    assert "close" in matrix_line


def test_matrix_not_crowded_out_by_group_heavy_list():
    # 40 GROUP fields FIRST, then the value fields — bucketing the FULL list
    # (not a head slice) must still surface the MATRIX/VECTOR value fields.
    fields = [{"id": f"g{i}", "type": "GROUP"} for i in range(40)]
    fields += [{"id": "close", "type": "MATRIX"}, {"id": "nws_v", "type": "VECTOR"}]
    out = build_fields_context(fields)
    assert "close" in out          # MATRIX value field surfaced
    assert "nws_v" in out          # VECTOR value field surfaced
    assert "GROUP fields" in out
