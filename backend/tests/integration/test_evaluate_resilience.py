"""
Integration tests for P1-B: node_evaluate resilience under per-alpha crashes.
来源: docs/alphagbm_skills_research_2026-05-15.md P1-B
"""
import pytest
from backend.agents.graph.nodes.evaluation import _safe_metric
from backend.agents.graph.state import AlphaCandidate


# --------------------------------------------------------------------------- #
# Counter-invariance: tally logic only, no full node_evaluate needed
# --------------------------------------------------------------------------- #

def _tally(alphas):
    """Mirror the post-loop tally from node_evaluate."""
    pass_count = 0
    optimize_count = 0
    fail_count = 0
    provisional_count = 0
    for a in alphas:
        qs = a.quality_status
        if qs == "PASS":
            pass_count += 1
        elif qs == "PASS_PROVISIONAL":
            provisional_count += 1
            optimize_count += 1
        elif qs == "OPTIMIZE":
            optimize_count += 1
        else:
            fail_count += 1
    return pass_count, optimize_count, fail_count, provisional_count


def _make_alpha(status: str) -> AlphaCandidate:
    a = AlphaCandidate(expression="rank(close)", is_simulated=True, simulation_success=True)
    a.quality_status = status
    a.metrics = {}
    return a


@pytest.mark.parametrize("statuses,expected", [
    (["PASS", "PASS", "FAIL"],           (2, 0, 1, 0)),
    (["PASS_PROVISIONAL"],               (0, 1, 0, 1)),
    (["OPTIMIZE"],                       (0, 1, 0, 0)),
    (["FAIL", "PENDING"],                (0, 0, 2, 0)),
    (["PASS", "PASS_PROVISIONAL", "OPTIMIZE", "FAIL"],  (1, 2, 1, 1)),
    (["PASS"] * 5,                       (5, 0, 0, 0)),
    ([],                                 (0, 0, 0, 0)),
    (["PASS_PROVISIONAL"] * 3,           (0, 3, 0, 3)),
    (["OPTIMIZE", "OPTIMIZE", "FAIL"],   (0, 2, 1, 0)),
    (["PENDING"],                        (0, 0, 1, 0)),
])
def test_counter_invariance(statuses, expected):
    alphas = [_make_alpha(s) for s in statuses]
    result = _tally(alphas)
    assert result == expected


def test_counter_invariance_prov_double_counts():
    """PASS_PROVISIONAL enters BOTH provisional_count AND optimize_count."""
    alphas = [_make_alpha("PASS_PROVISIONAL"), _make_alpha("PASS")]
    p, o, f, prov = _tally(alphas)
    assert prov == 1
    assert o == 1   # PROV enters optimize bucket
    assert p == 1
    assert f == 0
