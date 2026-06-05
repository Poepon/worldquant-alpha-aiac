"""Per-provider model catalog — single source of truth for which models each
Alibaba-Cloud plan serves. Recurrence guard for the qwen3.6-flash-on-coding-plan
incident (task 3981): a node routed to a model NOT in its provider's catalog 401s.
"""
from backend.config import (
    _ACTIVE_LLM_PROVIDER,
    _LLM_AVAILABLE_MODELS_CACHE,
    _LLM_FUNCTION_MODEL_MAP_CACHE,
    _PROVIDER_MODEL_CATALOG,
)


def test_catalog_has_both_plans():
    assert "aliyun_coding_plan" in _PROVIDER_MODEL_CATALOG  # Coding Plan
    assert "aliyun_maas" in _PROVIDER_MODEL_CATALOG          # Token Plan
    cp = _PROVIDER_MODEL_CATALOG["aliyun_coding_plan"]
    assert cp and "kimi-k2.5" in cp and "qwen3.6-plus" in cp
    # known-unsupported-on-coding-plan must NOT be in the Coding Plan list
    for bad in ("qwen3.6-flash", "kimi-k2.6", "deepseek-v4-pro"):
        assert bad not in cp, f"{bad} is not a Coding Plan model"


def test_startup_routing_map_conforms_to_provider_catalog():
    # Every node's model must be in its provider_ref's catalog. This is exactly
    # the check that would have caught self_correct/r1b_retry/r5_alignment_c1
    # routing to qwen3.6-flash on aliyun_coding_plan.
    for node, entry in _LLM_FUNCTION_MODEL_MAP_CACHE.items():
        model = entry.get("model")
        pref = entry.get("provider_ref")
        if pref and pref in _PROVIDER_MODEL_CATALOG:
            assert model in _PROVIDER_MODEL_CATALOG[pref], (
                f"node {node!r} routes to {model!r} which is NOT in the "
                f"{pref} catalog — will 401/fail at call time"
            )


def test_dropdown_defaults_to_active_provider_catalog():
    assert _LLM_AVAILABLE_MODELS_CACHE == _PROVIDER_MODEL_CATALOG[_ACTIVE_LLM_PROVIDER]
