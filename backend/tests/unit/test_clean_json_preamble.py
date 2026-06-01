"""_clean_json must strip a non-JSON preamble before the object (2026-06-02 fix):
kimi-k2.6 sometimes emits a leading '>\n' even in response_format=json_object,
which used to make EVERY such response a parse_error → broke all kimi mining."""
import json

import pytest

from backend.agents.services.llm_service import LLMService


@pytest.fixture
def svc():
    return LLMService.__new__(LLMService)  # _clean_json needs no init state


@pytest.mark.parametrize("raw,expect", [
    ('>\n{\n  "a": 1, "b": [2,3]\n}\n', {"a": 1, "b": [2, 3]}),      # kimi '>' prefix
    ('```json\n{"x": true}\n```', {"x": True}),                       # fence
    ('Here you go:\n{"y": 2}', {"y": 2}),                             # prose prefix
    ('{"z": 3}\nThat is all.', {"z": 3}),                             # trailing prose
    ('{"p": 1}', {"p": 1}),                                           # pure
    ('>\n[1, 2, 3]', [1, 2, 3]),                                      # '>' before array
])
def test_clean_json_strips_preamble(svc, raw, expect):
    assert json.loads(svc._clean_json(raw)) == expect


def test_clean_json_no_json_returns_asis(svc):
    # No opener anywhere → return as-is (json.loads will raise a clear error).
    out = svc._clean_json("just some prose, no json here")
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
