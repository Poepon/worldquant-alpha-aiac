"""
Mock LLM Service - Test implementation of LLMProtocol

Provides configurable mock responses for testing without
making real LLM API calls.
"""

from typing import Dict, List, Any, Optional, Type, Tuple
from dataclasses import dataclass, field
from pydantic import BaseModel
import json


@dataclass
class MockLLMConfig:
    """Configuration for mock LLM responses."""
    default_response: str = '{"result": "mock response"}'
    default_tokens: int = 100
    default_latency_ms: int = 50
    success_rate: float = 1.0  # Probability of success


class MockLLMResponse:
    """Mock LLM response matching the LLMResponse interface."""
    
    def __init__(
        self,
        content: str = "",
        parsed: Optional[Dict] = None,
        model: str = "mock-model",
        tokens_used: int = 100,
        latency_ms: int = 50,
        success: bool = True,
        error: Optional[str] = None,
    ):
        self.content = content
        self.parsed = parsed
        self.model = model
        self.tokens_used = tokens_used
        self.latency_ms = latency_ms
        self.success = success
        self.error = error


class MockLLMService:
    """
    Mock implementation of LLMProtocol for testing.
    
    Provides configurable responses for LLM calls without
    making real API requests.
    
    Usage:
        mock = MockLLMService()
        mock.set_response('{"alphas": ["rank(close)"]}')
        response = await mock.call("system", "user")
    """
    
    def __init__(self, config: MockLLMConfig = None):
        self.config = config or MockLLMConfig()
        self._response_queue: List[MockLLMResponse] = []
        self._call_history: List[Dict] = []
        self._model = "mock-model"
    
    def reset(self):
        """Reset mock state."""
        self._response_queue = []
        self._call_history = []
    
    # =========================================================================
    # Mock Configuration Methods
    # =========================================================================
    
    def set_response(
        self,
        content: str = None,
        parsed: Dict = None,
        success: bool = True,
        error: str = None,
    ):
        """
        Queue a specific response.
        
        Args:
            content: Raw content to return
            parsed: Parsed JSON to return
            success: Whether call succeeds
            error: Error message if not successful
        """
        if content is None and parsed is not None:
            content = json.dumps(parsed)
        elif content is None:
            content = self.config.default_response
        
        if parsed is None and content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None
        
        response = MockLLMResponse(
            content=content,
            parsed=parsed,
            model=self._model,
            tokens_used=self.config.default_tokens,
            latency_ms=self.config.default_latency_ms,
            success=success,
            error=error,
        )
        self._response_queue.append(response)
    
    def set_json_response(self, data: Dict):
        """Convenience method to set a JSON response."""
        self.set_response(parsed=data)
    
    def set_alpha_generation_response(
        self,
        alphas: List[str] = None,
        hypotheses: List[str] = None,
    ):
        """
        Set a response for alpha generation.
        
        Args:
            alphas: List of alpha expressions
            hypotheses: List of corresponding hypotheses
        """
        if alphas is None:
            alphas = ["rank(close)", "ts_mean(volume, 5)"]
        
        if hypotheses is None:
            hypotheses = ["Price momentum hypothesis", "Volume trend hypothesis"]
        
        response_data = {
            "alphas": [
                {
                    "expression": expr,
                    "hypothesis": hyp,
                    "confidence": 0.8,
                }
                for expr, hyp in zip(alphas, hypotheses)
            ]
        }
        self.set_response(parsed=response_data)
    
    def set_failure_response(self, error: str = "Mock LLM error"):
        """Set a failure response."""
        self.set_response(success=False, error=error)
    
    def get_call_history(self) -> List[Dict]:
        """Get history of LLM calls made."""
        return self._call_history
    
    # =========================================================================
    # LLMProtocol Implementation
    # =========================================================================
    
    @property
    def model(self) -> str:
        """Get the model identifier."""
        return self._model
    
    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        json_mode: bool = True,
        max_tokens: int = 4096,
        *,
        node_key: Optional[str] = None,
        thinking_effort: Optional[str] = None,
    ) -> MockLLMResponse:
        """
        Mock LLM call.

        Returns queued response if available, otherwise returns
        default response based on config. The `node_key` and
        `thinking_effort` keyword args (added when LLMProtocol gained
        per-node effort support) are recorded in call history so tests
        can assert `mock.call_history[-1]["node_key"] == "hypothesis"`.
        """
        self._call_history.append({
            "method": "call",
            "system_prompt": system_prompt[:100] + "..." if len(system_prompt) > 100 else system_prompt,
            "user_prompt": user_prompt[:100] + "..." if len(user_prompt) > 100 else user_prompt,
            "temperature": temperature,
            "json_mode": json_mode,
            "node_key": node_key,
            "thinking_effort": thinking_effort,
        })
        
        # Use queued response if available
        if self._response_queue:
            return self._response_queue.pop(0)
        
        # Generate default response
        import random
        if random.random() < self.config.success_rate:
            content = self.config.default_response
            try:
                parsed = json.loads(content) if json_mode else None
            except json.JSONDecodeError:
                parsed = None
            
            return MockLLMResponse(
                content=content,
                parsed=parsed,
                model=self._model,
                tokens_used=self.config.default_tokens,
                latency_ms=self.config.default_latency_ms,
                success=True,
            )
        else:
            return MockLLMResponse(
                content="",
                success=False,
                error="Mock random failure",
            )
    
    async def call_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[BaseModel],
        temperature: float = 0.7,
    ) -> Tuple[Optional[BaseModel], MockLLMResponse]:
        """
        Mock LLM call with schema validation.
        
        Returns queued response validated against schema.
        """
        self._call_history.append({
            "method": "call_with_schema",
            "schema": schema.__name__,
        })
        
        response = await self.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            json_mode=True,
        )
        
        if not response.success or not response.parsed:
            return None, response
        
        try:
            validated = schema.model_validate(response.parsed)
            return validated, response
        except Exception:
            return None, response
    
    def invalidate_credentials_cache(self) -> None:
        """Mock credential invalidation."""
        self._call_history.append({"method": "invalidate_credentials_cache"})
