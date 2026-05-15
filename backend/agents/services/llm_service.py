"""
LLM Service - Unified LLM calling interface with logging and retries

Implements LLMProtocol for dependency injection and testability.
High cohesion: All LLM-related logic in one place.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional, Any, Type, Tuple
from pydantic import BaseModel
import openai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger

from backend.config import settings
from backend.protocols.llm_protocol import LLMProtocol, LLMResponse as LLMResponseProtocol

# W5: Anthropic SDK is optional — only loaded when LLM_PROVIDER=anthropic.
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None
    _ANTHROPIC_AVAILABLE = False


# Anthropic reasoning models that reject `temperature` at the API layer.
# Same pattern as OpenAI's o-series — reasoning is opaque, so the param is
# deprecated and a 400 is returned if passed. Match by prefix so dated /
# context-variant ids (e.g. "claude-opus-4-7[1m]", "claude-opus-4-7-20260301")
# are covered.
_ANTHROPIC_NO_TEMPERATURE_PREFIXES: Tuple[str, ...] = ("claude-opus-4-7",)

# Anthropic reasoning models that support extended thinking. Same prefix
# pattern as the no-temperature list; kept separate because the two
# capabilities are technically independent (e.g. a future opus could still
# accept temperature without thinking, or vice versa).
_ANTHROPIC_THINKING_PREFIXES: Tuple[str, ...] = ("claude-opus-4-7",)

# Reasoning-effort tier → thinking budget_tokens. 1024 is Anthropic's hard
# minimum (per /thinking docs). Tier names match Anthropic's model capability
# metadata (low/medium/high/max) plus our intermediate "xhigh" between high
# and max (mirrors OpenAI o-series x-high reasoning_effort).
_ANTHROPIC_THINKING_BUDGETS: Dict[str, int] = {
    "low":    1024,
    "medium": 4096,
    "high":   16384,
    "xhigh":  32000,
    "max":    64000,   # Anthropic official top tier — budget < max_tokens still applies
}

# Tier name aliases — "auto" is the user-friendly name for Anthropic's
# `thinking.type=adaptive` (model self-allocates budget).
_ANTHROPIC_EFFORT_ALIASES: Dict[str, str] = {
    "auto": "adaptive",
}


def _anthropic_supports_temperature(model: str) -> bool:
    return not any(model.startswith(p) for p in _ANTHROPIC_NO_TEMPERATURE_PREFIXES)


def _anthropic_supports_thinking(model: str) -> bool:
    return any(model.startswith(p) for p in _ANTHROPIC_THINKING_PREFIXES)


class LLMResponse(BaseModel):
    """Standard LLM response wrapper."""
    content: str
    parsed: Optional[Dict] = None
    model: str
    tokens_used: int = 0
    latency_ms: int = 0
    success: bool = True
    error: Optional[str] = None
    
    def to_protocol_response(self) -> LLMResponseProtocol:
        """Convert to protocol response type."""
        return LLMResponseProtocol(
            content=self.content,
            parsed=self.parsed,
            model=self.model,
            tokens_used=self.tokens_used,
            latency_ms=self.latency_ms,
            success=self.success,
            error=self.error,
        )


class LLMService:
    """
    Unified LLM Service implementing LLMProtocol.
    
    Features:
    - Automatic retries with exponential backoff
    - JSON cleaning (markdown removal)
    - Token tracking
    - Structured logging
    - Credential caching with invalidation support
    
    This class implements the LLMProtocol interface, allowing for
    easy mocking in tests and dependency injection.
    """
    
    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        provider: str = None,
    ):
        # W5: provider switch (openai-compat vs anthropic). Provider is
        # selected from settings.LLM_PROVIDER ("openai" | "anthropic"); the
        # corresponding api_key/model override applies. The opposite
        # provider's credentials are still loaded as fallback.
        self.provider = (provider or getattr(settings, 'LLM_PROVIDER', 'openai')).lower()

        # OpenAI-compatible (Qwen/DeepSeek/etc.) — always set up so caller can
        # fall back per-call by passing provider="openai".
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = base_url or settings.OPENAI_BASE_URL
        self.openai_model = model if (model and self.provider == 'openai') else getattr(
            settings, 'OPENAI_MODEL', 'deepseek-chat'
        )

        # Anthropic (W5)
        self.anthropic_api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
        self.anthropic_model = model if (model and self.provider == 'anthropic') else getattr(
            settings, 'ANTHROPIC_MODEL', 'claude-haiku-4-5'
        )
        # Optional endpoint override (proxy / mirror). The constructor's
        # `base_url` doubles as the anthropic override when provider=anthropic;
        # otherwise we fall back to settings.ANTHROPIC_BASE_URL ("" → SDK default).
        self.anthropic_base_url = (
            base_url if (base_url and self.provider == 'anthropic')
            else (getattr(settings, 'ANTHROPIC_BASE_URL', '') or '')
        )
        # Extended-thinking reasoning effort (opus-4-7 family). Settings-driven
        # so call() signature stays Protocol-stable. Normalize to lowercase;
        # unknown values fall back to "xhigh" at use-time.
        self.anthropic_thinking_effort = (
            getattr(settings, 'ANTHROPIC_THINKING_EFFORT', 'xhigh') or 'xhigh'
        ).strip().lower()

        # Active model for self.model (back-compat with downstream readers)
        self.model = (
            self.anthropic_model if self.provider == 'anthropic' else self.openai_model
        )

        self._credentials_lock = asyncio.Lock()
        self._credentials_loaded = False

        self.client = openai.AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        # Lazy-init anthropic client only when provider=anthropic and SDK
        # is available; raises clear error if not installed.
        self.anthropic_client = None
        if self.provider == 'anthropic':
            if not _ANTHROPIC_AVAILABLE:
                raise RuntimeError(
                    "LLM_PROVIDER=anthropic but `anthropic` SDK is not installed. "
                    "Run: pip install anthropic>=0.40"
                )
            if not self.anthropic_api_key:
                raise RuntimeError(
                    "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty"
                )
            # Only pass base_url when explicitly overridden — keeps the SDK
            # default (https://api.anthropic.com) when ANTHROPIC_BASE_URL="".
            anthropic_kwargs: Dict[str, Any] = {"api_key": self.anthropic_api_key}
            if self.anthropic_base_url:
                anthropic_kwargs["base_url"] = self.anthropic_base_url
            self.anthropic_client = anthropic.AsyncAnthropic(**anthropic_kwargs)

        # Pre-resolve thinking-tier display name for logging only.
        _thinking_tag = (
            self.anthropic_thinking_effort
            if (self.provider == 'anthropic' and _anthropic_supports_thinking(self.model))
            else 'n/a'
        )
        logger.info(
            f"[LLMService] Initialized | provider={self.provider} model={self.model} "
            f"openai_base_url={self.base_url} "
            f"anthropic_base_url={self.anthropic_base_url or '<sdk default>'} "
            f"thinking={_thinking_tag}"
        )

    async def _ensure_credentials_loaded(self):
        if self._credentials_loaded:
            return

        async with self._credentials_lock:
            if self._credentials_loaded:
                return

            try:
                from backend.database import AsyncSessionLocal
                from backend.services.credentials_service import CredentialsService, CredentialKey

                async with AsyncSessionLocal() as db:
                    service = CredentialsService(db)
                    db_api_key = await service.get_credential(CredentialKey.OPENAI_API_KEY, fallback_env="OPENAI_API_KEY")
                    db_base_url = await service.get_credential(CredentialKey.OPENAI_BASE_URL, fallback_env="OPENAI_BASE_URL")
                    db_model = await service.get_credential(CredentialKey.OPENAI_MODEL, fallback_env="OPENAI_MODEL")

                if db_api_key:
                    self.api_key = db_api_key
                if db_base_url:
                    self.base_url = db_base_url
                if db_model:
                    self.model = db_model

                self.client = openai.AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception as e:
                logger.warning(f"[LLMService] Failed to load DB credentials, using settings/env | error={e}")
            finally:
                self._credentials_loaded = True

    def invalidate_credentials_cache(self):
        self._credentials_loaded = False
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((openai.APIConnectionError, openai.RateLimitError))
    )
    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        json_mode: bool = True,
        max_tokens: int = 4096
    ) -> LLMResponse:
        """
        Make an LLM call with automatic retries and logging.
        
        Args:
            system_prompt: System message
            user_prompt: User message
            temperature: Sampling temperature
            json_mode: Whether to request JSON output
            max_tokens: Maximum response tokens
            
        Returns:
            LLMResponse with content and metadata
        """
        start_time = time.time()
        call_id = f"{int(start_time * 1000) % 100000}"
        
        logger.debug(f"[LLMService] Call started | id={call_id} json_mode={json_mode}")
        
        try:
            await self._ensure_credentials_loaded()

            # W5: Anthropic provider uses messages.create + system prompt with
            # cache_control. JSON mode is enforced by prompt instructions
            # (Anthropic doesn't have a response_format flag; the existing
            # prompts already say "Output Schema: JSON ...").
            if self.provider == 'anthropic':
                # Reasoning models (opus-4-7 family) reject `temperature`;
                # only send it when the model still accepts it.
                anth_kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": [{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                if _anthropic_supports_temperature(self.model):
                    anth_kwargs["temperature"] = temperature

                # Extended thinking — opus-4-7 family only; caller's max_tokens
                # is preserved as the *output* budget (thinking adds on top).
                thinking_enabled = False
                # Resolve aliases (e.g. "auto" → "adaptive") so downstream
                # branches only need to know about canonical tier names.
                effort = _ANTHROPIC_EFFORT_ALIASES.get(
                    self.anthropic_thinking_effort, self.anthropic_thinking_effort
                )
                if (
                    _anthropic_supports_thinking(self.model)
                    and effort
                    and effort != 'disabled'
                ):
                    thinking_enabled = True
                    if effort == 'adaptive':
                        anth_kwargs['thinking'] = {
                            "type": "adaptive",
                            "display": "omitted",
                        }
                    else:
                        budget = _ANTHROPIC_THINKING_BUDGETS.get(
                            effort, _ANTHROPIC_THINKING_BUDGETS['xhigh']
                        )
                        # Spec: 1024 <= budget_tokens < max_tokens. Bump
                        # max_tokens so the original `max_tokens` arg is
                        # honored as output budget on top of thinking.
                        anth_kwargs['max_tokens'] = budget + max(max_tokens, 1024)
                        anth_kwargs['thinking'] = {
                            "type": "enabled",
                            "budget_tokens": budget,
                            "display": "omitted",
                        }

                # The SDK forces streaming when total expected output exceeds
                # its 10-minute non-streaming budget — that's always the case
                # with thinking enabled at medium+ effort. Use the stream
                # context manager and aggregate to the same final Message
                # shape so the downstream code path stays identical.
                if thinking_enabled:
                    async with self.anthropic_client.messages.stream(**anth_kwargs) as stream:
                        resp = await stream.get_final_message()
                else:
                    resp = await self.anthropic_client.messages.create(**anth_kwargs)
                # Extract text from the first content block (TextBlock)
                content = ""
                for block in resp.content:
                    if getattr(block, "type", "") == "text":
                        content = block.text
                        break
                if not content:
                    raise ValueError("Empty content in Anthropic response")
                # Token accounting (input + output, log cache hit ratio)
                u = resp.usage
                tokens_used = (u.input_tokens or 0) + (u.output_tokens or 0)
                cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
                latency_ms = int((time.time() - start_time) * 1000)
                if cache_read or cache_create:
                    logger.debug(
                        f"[LLMService] Anthropic cache | id={call_id} "
                        f"input={u.input_tokens} cache_read={cache_read} "
                        f"cache_create={cache_create}"
                    )
            else:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"} if json_mode else None
                )

                # Defensive: handle empty/malformed responses
                choices = getattr(response, "choices", None)
                if not response or not choices:
                    status = getattr(response, "status", None)
                    msg = getattr(response, "msg", None)
                    extra = f" | status={status} msg={msg}" if status or msg else ""
                    raise ValueError(f"Empty response from LLM API{extra}")

                if len(choices) == 0:
                    raise ValueError("Empty choices from LLM API")

                message = response.choices[0].message
                if not message:
                    raise ValueError("No message in LLM response")

                content = message.content or ""
                if json_mode and not content.strip():
                    finish_reason = getattr(response.choices[0], "finish_reason", None)
                    reasoning_content = getattr(message, "reasoning_content", None)
                    extra = f"finish_reason={finish_reason}" if finish_reason else ""
                    if reasoning_content:
                        extra = (extra + " | reasoning_content_present=True").strip()
                    raise ValueError(f"Empty content in LLM response ({extra})")
                tokens_used = response.usage.total_tokens if response.usage else 0
                latency_ms = int((time.time() - start_time) * 1000)
            
            # Parse JSON if requested
            parsed = None
            if json_mode:
                try:
                    cleaned = self._clean_json(content)
                    parsed = json.loads(cleaned)
                except json.JSONDecodeError as e:
                    logger.warning(f"[LLMService] JSON parse failed | id={call_id} error={e}")
            
            logger.info(
                f"[LLMService] Call success | id={call_id} "
                f"tokens={tokens_used} latency={latency_ms}ms"
            )
            
            return LLMResponse(
                content=content,
                parsed=parsed,
                model=self.model,
                tokens_used=tokens_used,
                latency_ms=latency_ms,
                success=True
            )
            
        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[LLMService] Call failed | id={call_id} error={e}")
            
            return LLMResponse(
                content="",
                model=self.model,
                latency_ms=latency_ms,
                success=False,
                error=str(e)
            )
    
    async def call_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[BaseModel],
        temperature: float = 0.7
    ) -> tuple[Optional[BaseModel], LLMResponse]:
        """
        Call LLM and validate response against a Pydantic schema.
        
        Args:
            system_prompt: System message
            user_prompt: User message
            schema: Pydantic model class to validate against
            temperature: Sampling temperature
            
        Returns:
            Tuple of (parsed model or None, raw response)
        """
        response = await self.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            json_mode=True
        )
        
        if not response.success or not response.parsed:
            return None, response
        
        try:
            validated = schema.model_validate(response.parsed)
            return validated, response
        except Exception as e:
            logger.warning(f"[LLMService] Schema validation failed | error={e}")
            return None, response
    
    def _clean_json(self, content: str) -> str:
        """Remove markdown blocks + trim text trailing the JSON object.

        OpenAI/Qwen with response_format=json_object guarantee pure JSON;
        Anthropic Claude doesn't have such a flag, so it occasionally emits
        natural-language commentary after the JSON object. We extract the
        first complete JSON object/array by brace-matching with string-aware
        escape handling.
        """
        content = content.strip()

        # Strip markdown fences
        if content.startswith('```json'):
            content = content[7:]
        elif content.startswith('```'):
            content = content[3:]
        if content.endswith('```'):
            content = content[:-3]
        content = content.strip()

        if not content or content[0] not in ('{', '['):
            return content

        opener = content[0]
        closer = '}' if opener == '{' else ']'
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(content):
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return content[: i + 1]
        # Unbalanced — return as-is so json.loads raises a clear error
        return content


# Singleton instance for reuse
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Get or create singleton LLM service."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
