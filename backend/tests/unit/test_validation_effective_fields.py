"""node_validate must validate against the EFFECTIVE field scope (2026-05-23).

For a cross-dataset hypothesis, node_hypothesis loads the pool-union fields into
state.current_hypothesis_fields and node_code_gen generates against them. The
validator previously used only the anchor state.fields, so a legitimate pool
field (e.g. socialmedia8's snt_social_value used on a non-social anchor) got a
false "Field not found in dataset" → 4/4 rejected → wasteful self-correct churn.
"""
import pytest

from backend.agents.graph.nodes.validation import node_validate
from backend.agents.graph.state import AlphaCandidate, MiningState

_ANCHOR = [{"id": "close", "type": "MATRIX"}]
_POOL_FIELD = {"id": "snt_social_value", "type": "MATRIX"}
_OPS = [{"name": "ts_zscore"}]
_EXPR = "ts_zscore(snt_social_value, 20)"  # uses a POOL field, not the anchor


def _state(*, union):
    return MiningState(
        task_id=1,
        pending_alphas=[AlphaCandidate(expression=_EXPR)],
        fields=list(_ANCHOR),
        current_hypothesis_fields=union,
        operators=_OPS,
        region="USA",
        universe="TOP3000",
    )


@pytest.mark.asyncio
async def test_pool_field_valid_when_in_union():
    # current_hypothesis_fields (union) includes the pool field → recognized.
    out = await node_validate(_state(union=[_POOL_FIELD] + _ANCHOR))
    alpha = out["pending_alphas"][0]
    assert alpha.is_valid is True, (
        f"pool field should validate via the union; got error={alpha.validation_error}"
    )


@pytest.mark.asyncio
async def test_pool_field_rejected_without_union_regression_proof():
    # No union → falls back to anchor (no snt_social_value) → false "not found".
    # This is the OLD behavior; asserting it documents what the fix repairs.
    out = await node_validate(_state(union=[]))
    alpha = out["pending_alphas"][0]
    assert alpha.is_valid is False
    assert "not found" in (alpha.validation_error or "").lower()
