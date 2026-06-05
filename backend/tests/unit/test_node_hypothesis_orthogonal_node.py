"""Regression guard — node_hypothesis must DRIVE under the orthogonality flag
ON and OFF (not just build_hypothesis_prompt).

ba65ac7 shipped a `NameError: name 'settings' is not defined` at the orthogonality
flag-check line (generation.py ~1028) — the getattr runs REGARDLESS of the flag
value, so it broke EVERY FLAT mining round (0 alphas). The prompt-builder-only unit
tests missed it because they never invoked the NODE. This test invokes
node_hypothesis so the flag-check line (and the flag-ON compute path) are exercised.
The LLM call happens AFTER the flag check + build_hypothesis_prompt, so a crash there
would PREVENT the call → empty history → these assertions fail.
"""
import pytest


def _state():
    from backend.agents.graph.state import MiningState
    return MiningState(
        task_id=1, region="USA", dataset_id="pv1",
        fields=[{"id": "close", "type": "MATRIX", "description": "close price"}],
        operators=[{"name": "rank", "definition": "rank(x)"}],
    )


async def _drive():
    from backend.tests.fixtures.mock_llm import MockLLMService
    from backend.agents.graph.nodes.generation import node_hypothesis
    mock = MockLLMService()
    mock.set_json_response({"hypotheses": [{"hypothesis": "h", "rationale": "r"}]})
    try:
        await node_hypothesis(_state(), llm_service=mock, config=None)
    except Exception:
        # Downstream persistence may need more state; the LLM call is recorded
        # BEFORE that and AFTER the orthogonality flag-check, which is the guard.
        pass
    return [c.get("node_key") for c in mock.get_call_history()]


@pytest.mark.asyncio
async def test_node_hypothesis_drives_flag_off(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(
        settings, "ENABLE_ORTHOGONAL_PROMPT_STEERING", False, raising=False)
    assert "hypothesis" in await _drive()  # reached LLM call → no NameError


@pytest.mark.asyncio
async def test_node_hypothesis_drives_flag_on(monkeypatch):
    # flag ON → compute_submitted_pool_profile runs (defensive — empty on any
    # test-env DB hiccup). Must still reach the LLM call without crashing.
    from backend.config import settings
    monkeypatch.setattr(
        settings, "ENABLE_ORTHOGONAL_PROMPT_STEERING", True, raising=False)
    assert "hypothesis" in await _drive()
