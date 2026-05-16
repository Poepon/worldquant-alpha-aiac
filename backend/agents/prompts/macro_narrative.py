"""P2-A LLM batch prompt — macro narrative generation (2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `macro-view`.

Used by ``backend/tasks/macro_narrative_extract.py`` to ask the LLM (in
JSON mode) to produce ``MacroNarrative`` rows for DataFields that have no
narrative yet. The system prompt encodes the data contract; the user
prompt builder packs one batch of fields per call.
"""
from __future__ import annotations

import json
from typing import Dict, List


MACRO_NARRATIVE_BATCH_SYSTEM = """You are a research economist generating concise
economic-narrative metadata for stock-mining data fields.

For EACH input field you must return ONE object describing the
economic mechanism that links the field to expected cross-sectional
return signals, and the transmission channel through which that signal
propagates.

**Strict output contract**:

Return ONLY a JSON object of the form:
```json
{
  "items": [
    {
      "field_id": "<exact field_id from input>",
      "mechanism": "<≤500 chars — what economic process this field captures>",
      "transmission_channel": "<≤500 chars — how the field becomes a cross-sectional signal>",
      "expected_signal_hint": "<one of: momentum | mean_reversion | value | quality | volatility | sentiment>",
      "confidence": <float 0.0-1.0>
    }
  ]
}
```

Rules:
1. ``field_id`` must EXACTLY match a field_id from the input batch
   (case-insensitive exact, no fuzzy matching).
2. ``mechanism`` describes the ECONOMIC story (why this field carries
   information about future returns), NOT a statistical artifact.
3. ``transmission_channel`` describes the causal path from observation
   to price impact (e.g. "EPS revision → 慢扩散 → 30-90d 价格调整").
4. ``expected_signal_hint`` MUST be one of the 6 enumerated values.
5. ``confidence`` reflects how well-established the linkage is in the
   academic / practitioner literature: 0.9 = textbook, 0.5 = plausible
   but speculative, 0.3 = weak / exploratory.
6. Skip fields you cannot reason about — return only the items you are
   confident about; an empty ``items`` list is acceptable.
7. NO additional commentary, NO markdown — only the JSON object.
"""


def build_macro_narrative_batch_user_prompt(
    fields: List[Dict],
    *,
    region: str = "USA",
) -> str:
    """Build the user prompt for one LLM batch.

    Each field dict carries:
      ``field_id``, ``field_name``, ``description``, ``category_name``,
      ``dataset_id`` (BRAIN string), ``dataset_category_inferred``.

    When ``description`` is empty we fall back to ``field_name`` +
    ``category_name`` so the LLM still has some context (the LLM should
    return a lower confidence in that case).
    """
    if not fields:
        return "No fields to annotate."

    lines = [
        f"## Region: {region}",
        "",
        f"## Fields to annotate ({len(fields)} total)",
        "",
    ]
    for f in fields:
        fid = f.get("field_id", "")
        fname = f.get("field_name", "") or fid
        desc = (f.get("description") or "").strip()
        if not desc:
            # Fallback contract: field_name + category_name. LLM is
            # expected to return confidence ~0.5 when description is sparse.
            cat_name = f.get("category_name", "") or ""
            desc = f"(no description; category={cat_name})"
        ds_brain = f.get("dataset_id", "") or ""
        cat = f.get("dataset_category_inferred", "") or ""
        lines.append(
            f"- `{fid}` (dataset=`{ds_brain}`, category=`{cat}`): "
            f"{desc[:300]}"
        )
    lines.append("")
    lines.append("Return JSON only. Skip fields you cannot confidently characterize.")
    return "\n".join(lines)


def parse_macro_narrative_batch_response(
    raw_text: str,
) -> List[Dict]:
    """Parse the LLM's batch response. Returns a list of item dicts; on
    JSON failure returns an empty list (caller logs + increments errors)."""
    try:
        data = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items") or []
    if not isinstance(items, list):
        return []
    out: List[Dict] = []
    for it in items:
        if isinstance(it, dict) and it.get("field_id"):
            out.append(it)
    return out


__all__ = [
    "MACRO_NARRATIVE_BATCH_SYSTEM",
    "build_macro_narrative_batch_user_prompt",
    "parse_macro_narrative_batch_response",
]
