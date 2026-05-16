"""P2-A macro narratives — seed bank + data contract (2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `macro-view`.

Pure-function module. Holds the dataclass + seed banks (field-scope and
category-scope) that the daily macro-narrative extract task UPSERTs into
``knowledge_entries`` (entry_type='MACRO_NARRATIVE').

No DB / network / settings imports — safe to unit-test under aiosqlite or
without any database at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from backend.models import compute_pattern_hash


@dataclass
class MacroNarrative:
    """In-memory representation of a (field|category|dataset)-scoped economic
    narrative. Mirrors the JSONB meta_data we persist into
    ``knowledge_entries`` (see ``narrative_to_kb_payload``).

    Field rules:
      - field_id XOR dataset_category XOR dataset_id determines scope
      - region '*' = universal (matches every region); else exact code
      - mechanism / transmission_channel: each ≤500 chars (S2)
      - expected_signal_hint ∈ momentum | mean_reversion | value | quality |
        volatility | sentiment (used by the LLM to align hypothesis pillar)
      - source ∈ seed | llm | brain | forum
    """
    field_id: Optional[str] = None
    dataset_id: Optional[str] = None          # BRAIN string id (e.g. "fundamental6")
    dataset_category: Optional[str] = None    # "pv"|"analyst"|"fundamental"|"news"|"macro"
    region: str = "*"                         # "*" universal or "USA"/"CHN"/...
    mechanism: str = ""                       # ≤500 chars
    transmission_channel: str = ""            # ≤500 chars
    expected_signal_hint: str = ""            # momentum|mean_reversion|value|quality|volatility|sentiment
    confidence: float = 0.7
    source: str = "seed"                      # seed | llm | brain | forum


# -----------------------------------------------------------------------------
# Field-scope seeds (6 entries — M11 vwap is consistent mean_reversion)
# -----------------------------------------------------------------------------
SEED_FIELD_NARRATIVES: List[MacroNarrative] = [
    MacroNarrative(
        field_id="close", dataset_category="pv", region="*",
        mechanism="收盘价是日终市场共识;时序承载动量(中期)与反转(短期)双向力学",
        transmission_channel="价格 → 投资者锚定 → 后续买卖压力 → 1-5d 反转 / 20-60d 动量",
        expected_signal_hint="momentum", confidence=0.9, source="seed",
    ),
    MacroNarrative(
        field_id="volume", dataset_category="pv", region="*",
        mechanism="成交量代表信息流强度,与价格背离时常预示反转",
        transmission_channel="信息冲击 → 异常成交量 → 短期价格压力 → 1-3d 流动性溢价",
        expected_signal_hint="mean_reversion", confidence=0.85, source="seed",
    ),
    MacroNarrative(
        field_id="returns", dataset_category="pv", region="*",
        mechanism="日度收益率短期反转 (1-5d) + 中期动量 (20-120d)",
        transmission_channel="过去回报 → 过度反应 (短) 或迟滞反应 (中) → 截面排序回报",
        expected_signal_hint="momentum", confidence=0.9, source="seed",
    ),
    MacroNarrative(
        field_id="eps", dataset_category="fundamental", region="*",
        mechanism="EPS 是估值锚;EPS surprise 触发慢扩散的盈利公告漂移 (PEAD)",
        transmission_channel="公告 → 分析师调升 → 散户跟买 → 60d 漂移",
        expected_signal_hint="value", confidence=0.85, source="seed",
    ),
    MacroNarrative(
        field_id="pe_ratio", dataset_category="fundamental", region="*",
        mechanism="估值倍数;高 PE 暗示成长预期或泡沫,低 PE 暗示价值或衰退担忧",
        transmission_channel="估值压缩/扩张 → 长期均值回归 (12-36m)",
        expected_signal_hint="value", confidence=0.85, source="seed",
    ),
    # M11 修正:一致 mean_reversion(去掉旧版"趋势加强"的双向矛盾)
    MacroNarrative(
        field_id="vwap", dataset_category="pv", region="*",
        mechanism="VWAP 是机构日内成本基准;偏离触发再平衡 / 反向算法订单",
        transmission_channel="价格偏离 VWAP → 算法 fade → 日内反转 (分钟-小时)",
        expected_signal_hint="mean_reversion", confidence=0.8, source="seed",
    ),
]


# -----------------------------------------------------------------------------
# Category-scope seeds (5 entries — covers all 5 mapped categories from
# rag_service.DATASET_CATEGORY_MAPPING + macro extra)
# -----------------------------------------------------------------------------
SEED_CATEGORY_NARRATIVES: List[MacroNarrative] = [
    MacroNarrative(
        dataset_category="pv", region="*",
        mechanism="价量数据最高频;短期 reversal + 中期 momentum 双段",
        transmission_channel="价格压力/惊喜 → 行为偏差 → 1-20d 信号",
        expected_signal_hint="momentum", confidence=0.9, source="seed",
    ),
    MacroNarrative(
        dataset_category="analyst", region="*",
        mechanism="分析师预期修正包含私有信息;revision drift 慢信号",
        transmission_channel="EPS revision → 慢扩散 → 30-90d 价格调整",
        expected_signal_hint="sentiment", confidence=0.85, source="seed",
    ),
    MacroNarrative(
        dataset_category="fundamental", region="*",
        mechanism="基本面低频但稳健;财务质量与估值是长期回报锚",
        transmission_channel="盈利、负债、现金流 → 长期价值锚 → 90-365d 均值回归",
        expected_signal_hint="value", confidence=0.9, source="seed",
    ),
    MacroNarrative(
        dataset_category="news", region="*",
        mechanism="新闻情绪信号短期主导价格;散户先反应后均值回归",
        transmission_channel="新闻冲击 → 情绪极端 → 1-5d 过冲 → 反转",
        expected_signal_hint="sentiment", confidence=0.75, source="seed",
    ),
    MacroNarrative(
        dataset_category="macro", region="*",
        mechanism="宏观因子(VIX/US10Y/iv)驱动横截面风险溢价",
        transmission_channel="VIX↑ → risk-off → 低波动股相对走强;US10Y↑ → 贴现率 → 久期长股压力",
        expected_signal_hint="volatility", confidence=0.8, source="seed",
    ),
]


def get_all_seeds() -> List[MacroNarrative]:
    """Return the concatenated seed bank (field-scope + category-scope).

    Ordering is field-first so the daily extract task UPSERTs the more
    specific scope before the broader category fallback — pattern_hash is
    independent per (scope, key, source) tuple, so order is functional only.
    """
    return list(SEED_FIELD_NARRATIVES) + list(SEED_CATEGORY_NARRATIVES)


def compute_narrative_hash(
    *,
    field_id: Optional[str],
    dataset_category: Optional[str],
    region: str,
    source: str,
    dataset_id: Optional[str] = None,
) -> str:
    """Stable pattern_hash for the MACRO_NARRATIVE KB row's UNIQUE index.

    Encoding is symmetric with ``narrative_to_kb_payload``'s pattern string.
    Region '*' (universal) maps to '*' in the hash; per-region narratives
    use the exact region code. Source is included so a 'seed' row vs an
    'llm'-generated row for the same field can coexist in the KB (the
    extract task curates seed-source rows only).
    """
    if field_id:
        pattern = f"MACRO_NARRATIVE::field::{field_id}::{source}"
    elif dataset_category:
        pattern = f"MACRO_NARRATIVE::category::{dataset_category}::{source}"
    else:
        pattern = f"MACRO_NARRATIVE::dataset::{dataset_id or ''}::{source}"
    return compute_pattern_hash(pattern, region or "*", dataset_id or None)


def narrative_to_kb_payload(n: MacroNarrative) -> Dict:
    """Render a ``MacroNarrative`` as a kwargs dict suitable for
    ``KnowledgeEntry(**payload)`` or pg_insert(KnowledgeEntry).values(...).

    Caps mirror S2 (≤500 chars) on description / mechanism /
    transmission_channel. ``entry_type`` is the string column value
    (``KnowledgeEntryType.MACRO_NARRATIVE.value``), kept literal here so
    this module stays import-free of the enum.
    """
    scope = "field" if n.field_id else ("category" if n.dataset_category else "dataset")
    pattern = (
        f"MACRO_NARRATIVE::{scope}::"
        f"{n.field_id or n.dataset_category or n.dataset_id}::{n.source}"
    )
    mechanism_capped = (n.mechanism or "")[:500]
    transmission_capped = (n.transmission_channel or "")[:500]
    return {
        "entry_type": "MACRO_NARRATIVE",
        "pattern": pattern[:500],
        "pattern_hash": compute_narrative_hash(
            field_id=n.field_id,
            dataset_category=n.dataset_category,
            region=n.region,
            source=n.source,
            dataset_id=n.dataset_id,
        ),
        "description": mechanism_capped,
        "meta_data": {
            "scope": scope,
            "field_id": n.field_id,
            "dataset_id": n.dataset_id,
            "dataset_category": n.dataset_category,
            "region": n.region,
            "mechanism": mechanism_capped,
            "transmission_channel": transmission_capped,
            "expected_signal_hint": n.expected_signal_hint,
            "confidence": float(n.confidence),
            "source": n.source,
        },
        "is_active": True,
        "created_by": "P2A_MACRO",
    }


__all__ = [
    "MacroNarrative",
    "SEED_FIELD_NARRATIVES",
    "SEED_CATEGORY_NARRATIVES",
    "get_all_seeds",
    "compute_narrative_hash",
    "narrative_to_kb_payload",
]
