"""qwen/DashScope json_mode guard (2026-05-20).

DashScope's OpenAI-compatible endpoint 400s when response_format=json_object
is set but the messages don't contain the literal word "json". LLMService now
injects a JSON directive into the user prompt when json_mode is on and neither
prompt mentions json. These tests pin that the directive is added only when
needed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _fake_openai_response(content: str = '{"ok": true}'):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(total_tokens=5, prompt_tokens=3, completion_tokens=2),
    )


def _make_openai_svc(capture: dict):
    from backend.agents.services.llm_service import LLMService
    svc = LLMService()
    svc.provider = "openai"

    async def fake_create(**kwargs):
        capture.update(kwargs)
        return _fake_openai_response()

    svc.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    svc._ensure_credentials_loaded = AsyncMock()
    return svc


def _user_content(capture: dict) -> str:
    return [m for m in capture["messages"] if m["role"] == "user"][0]["content"]


@pytest.mark.asyncio
async def test_guard_injects_json_word_when_absent():
    cap: dict = {}
    svc = _make_openai_svc(cap)
    await svc.call("You are helpful.", "Reply with OK", json_mode=True)
    assert "json" in _user_content(cap).lower(), "guard should inject a JSON directive"
    # response_format must still be json_object
    assert cap["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_guard_no_double_inject_when_prompt_has_json():
    cap: dict = {}
    svc = _make_openai_svc(cap)
    await svc.call("Output a JSON object.", "Return {\"x\": 1}", json_mode=True)
    # Prompt already mentions json → user content unchanged (no appended directive)
    assert "respond with a single valid json object" not in _user_content(cap).lower()


@pytest.mark.asyncio
async def test_guard_skipped_when_json_mode_off():
    cap: dict = {}
    svc = _make_openai_svc(cap)
    await svc.call("You are helpful.", "Reply with OK", json_mode=False)
    assert "respond with a single valid json object" not in _user_content(cap).lower()
    assert cap["response_format"] is None
