"""Plan v5+ #3 — Pre-simulate skeleton classifier feature extractor.

Pure-Python feature engineering for alpha expressions. No sklearn dep so
the runtime path can use it without loading the full ML stack until the
classifier is actually invoked.

Features extracted from an expression:
- skeleton_hash: stable hash of normalized skeleton (categorical proxy)
- nesting_depth: max paren nesting level
- num_operators: total operator calls
- num_fields: distinct field references
- has_xs_op / has_ts_op / has_group_op / has_arith_op / has_trade_when:
  binary indicators per operator category
- num_negation: count of multiply(-1, ...) wrappers
- max_window: largest numeric literal (proxy for time horizon)
- min_window: smallest numeric literal
- mean_window: mean of numeric literals
- has_ts_rank / has_zscore / has_rank / has_group_neutralize / etc:
  binary indicators for top-k high-signal operators

The skeleton_hash is computed via existing
backend.alpha_semantic_validator.expression_to_skeleton (already used by
KB dedup, so this keeps feature space aligned with KB patterns).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List


# Operator category buckets — derived from BRAIN's operator taxonomy.
# Names lowercased for matching against tokenized expression.
_OP_CATEGORIES: Dict[str, List[str]] = {
    "ts": [
        "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta",
        "ts_delay", "ts_sum", "ts_corr", "ts_decay_linear", "ts_arg_max",
        "ts_arg_min", "ts_av_diff", "ts_count_nans", "ts_product",
        "ts_scale", "ts_step", "ts_regression", "ts_covariance",
        "ts_backfill", "ts_max", "ts_min", "ts_quantile",
    ],
    "xs": [
        "rank", "zscore", "normalize", "quantile", "winsorize", "scale",
    ],
    "group": [
        "group_neutralize", "group_rank", "group_zscore", "group_mean",
        "group_scale",
    ],
    "arith": [
        "add", "subtract", "multiply", "divide", "signed_power", "abs",
        "min", "max", "sign",
    ],
    "event": [
        "trade_when", "if_else", "less", "greater", "equal",
    ],
}

# Flatten for fast lookup
_ALL_OPS: List[str] = sorted(
    {op for cat in _OP_CATEGORIES.values() for op in cat},
    key=lambda x: -len(x),  # match longer names first
)

# High-signal individual operators tracked as binary features
_HIGH_SIGNAL_OPS: List[str] = [
    "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta",
    "ts_decay_linear", "ts_arg_max", "ts_arg_min",
    "rank", "zscore", "scale",
    "group_neutralize", "group_rank", "group_zscore", "group_mean",
    "trade_when",
]


_NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")
_FIELD_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b", flags=re.IGNORECASE)


def _max_paren_depth(s: str) -> int:
    depth = 0
    max_d = 0
    for ch in s:
        if ch == "(":
            depth += 1
            if depth > max_d:
                max_d = depth
        elif ch == ")":
            if depth > 0:
                depth -= 1
    return max_d


def _count_negations(s: str) -> int:
    """Count multiply(-1, ...) patterns (sign-flip wrappers)."""
    # Liberal regex — matches "multiply(-1," or "multiply( -1,"
    return len(re.findall(r"multiply\s*\(\s*-\s*1\s*,", s, flags=re.IGNORECASE))


def _extract_operators(s: str) -> List[str]:
    """Tokenize expression to find operator names. Liberal: an identifier
    followed by '(' is treated as an operator call."""
    out = []
    s_lower = s.lower()
    # Match identifier followed by optional whitespace then "("
    for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\s*\(", s_lower):
        token = m.group(1)
        if token in {op.lower() for op in _ALL_OPS}:
            out.append(token)
    return out


def _extract_field_refs(s: str) -> List[str]:
    """Identify field-like tokens (not operators, not numbers, not keywords)."""
    operators_set = {op.lower() for op in _ALL_OPS}
    keywords = {"true", "false", "null", "none", "and", "or", "not"}

    fields = []
    seen = set()
    s_lower = s.lower()
    # First pass: token positions of operators (followed by '(')
    op_positions = set()
    for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\s*\(", s_lower):
        op_positions.add((m.start(1), m.end(1)))

    # Second pass: tokens NOT followed by '('
    for m in _FIELD_RE.finditer(s_lower):
        token = m.group(1)
        pos = (m.start(), m.end())
        if pos in op_positions:
            continue
        if token in operators_set or token in keywords:
            continue
        if token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        fields.append(token)
    return fields


def _extract_numeric_literals(s: str) -> List[float]:
    return [float(m.group(1)) for m in _NUM_RE.finditer(s)]


def _skeleton_hash(skeleton: str) -> int:
    """Stable 32-bit hash of skeleton string. Stays consistent across runs
    so saved models can map back to skeleton categories during inference."""
    h = hashlib.md5(skeleton.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "little", signed=False)


def extract_features(expression: str) -> Dict[str, Any]:
    """Return a feature dict ready for sklearn DictVectorizer or pandas.

    Output keys:
      - skeleton_hash (int)
      - nesting_depth (int)
      - num_operators (int)
      - num_fields (int)
      - num_negation (int)
      - max_window / min_window / mean_window (float, 0.0 if no numerics)
      - has_<category> (binary 0/1) for each in _OP_CATEGORIES
      - has_<op> (binary 0/1) for each in _HIGH_SIGNAL_OPS
    """
    if not expression:
        # Empty / null expression → empty features (caller may skip)
        return {
            "skeleton_hash": 0,
            "nesting_depth": 0,
            "num_operators": 0,
            "num_fields": 0,
            "num_negation": 0,
            "max_window": 0.0,
            "min_window": 0.0,
            "mean_window": 0.0,
            **{f"has_cat_{c}": 0 for c in _OP_CATEGORIES},
            **{f"has_op_{op}": 0 for op in _HIGH_SIGNAL_OPS},
        }

    # Lazy import to avoid loading at module level (kept light)
    from backend.knowledge_extraction import expression_to_skeleton

    try:
        skel = expression_to_skeleton(expression)
    except Exception:
        skel = expression  # fall back to raw if skeletonizer breaks

    ops = _extract_operators(expression)
    fields = _extract_field_refs(expression)
    nums = _extract_numeric_literals(expression)

    feats: Dict[str, Any] = {
        "skeleton_hash": _skeleton_hash(skel),
        "nesting_depth": _max_paren_depth(expression),
        "num_operators": len(ops),
        "num_fields": len(fields),
        "num_negation": _count_negations(expression),
        "max_window": max(nums) if nums else 0.0,
        "min_window": min(nums) if nums else 0.0,
        "mean_window": (sum(nums) / len(nums)) if nums else 0.0,
    }

    # Category indicators
    op_set = set(ops)
    for cat, names in _OP_CATEGORIES.items():
        feats[f"has_cat_{cat}"] = int(any(n.lower() in op_set for n in names))

    # High-signal individual indicators
    for hsop in _HIGH_SIGNAL_OPS:
        feats[f"has_op_{hsop}"] = int(hsop.lower() in op_set)

    return feats


def feature_keys() -> List[str]:
    """Stable ordered list of feature keys the model expects.
    Used by training (matrix construction) and inference (DictVectorizer
    or manual lookup).
    """
    base = [
        "skeleton_hash", "nesting_depth", "num_operators", "num_fields",
        "num_negation", "max_window", "min_window", "mean_window",
    ]
    cats = [f"has_cat_{c}" for c in sorted(_OP_CATEGORIES)]
    ops = [f"has_op_{op}" for op in _HIGH_SIGNAL_OPS]
    return base + cats + ops
