"""
Alpha Semantic Validator - Enhanced validation with MATRIX/VECTOR type constraints.

This module provides semantic validation beyond syntax checking:
1. Field existence validation
2. Operator existence validation  
3. MATRIX/VECTOR type constraint enforcement
4. Expression deduplication
5. Diversity scoring

P0-1: Core type/signature validation
"""

import re
import hashlib
import asyncio
from typing import Dict, List, Any, Optional, Set, Tuple, Literal
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

# P1-E: structured-finding severity and category taxonomies.
FindingSeverity = Literal["hard", "soft", "info"]
FindingCategory = Literal["syntax", "semantics", "risk", "duplicate", "other"]


class RuleId:
    """Canonical rule identifiers for structured findings (P1-E).

    Used as `Finding.rule_id` values. Catalog is the single source of truth —
    new rules MUST add a constant here and a row in the unit-test rule-catalog
    coverage table.
    """

    # syntax / semantics
    EMPTY_EXPRESSION = "empty_expression"
    UNKNOWN_OPERATOR = "unknown_operator"           # severity=soft (Q1: unchanged)
    FIELD_NOT_FOUND = "field_not_found"             # severity=hard if strict_field_check, else soft
    TYPE_MISMATCH_VECTOR_TS = "type_mismatch_vector_ts"
    LOW_COVERAGE_FIELD = "low_coverage_field"
    OTHER = "other"

    # risk (P1-E new — static max-loss inference)
    RISK_DIVIDE_BY_VOLATILE_DENOM = "risk_divide_by_volatile_denom"
    RISK_HIGH_EXPONENT_SIGNED_POWER = "risk_high_exponent_signed_power"
    RISK_SHORT_DECAY_WINDOW = "risk_short_decay_window"
    RISK_EXTREME_WINSORIZATION = "risk_extreme_winsorization"

    # static_alpha_checks adapter (node_validate dict→Finding bridge — M-5)
    STATIC_LOOKAHEAD_BIAS = "static_lookahead_bias"
    STATIC_DIVIDE_BY_ZERO = "static_divide_by_zero"
    STATIC_OVERFIT_WINDOW = "static_overfit_window"


@dataclass
class Finding:
    """Single structured validation issue. P1-E.

    Replaces the legacy `errors: List[str]` / `warnings: List[str]` shape.
    Severity drives the SELF_CORRECT prompt rendering layer (hard → MUST fix,
    soft → may-fix-if-relevant, info → context-only risk hint).
    """

    rule_id: str
    severity: FindingSeverity
    message: str
    category: FindingCategory = "semantics"
    location: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "category": self.category,
            "location": self.location,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Optional["Finding"]:
        """S-8: KeyError-safe; skip entries that lack rule_id (legacy KB rows)."""
        if not isinstance(d, dict):
            return None
        if not d.get("rule_id"):
            return None
        return cls(
            rule_id=d["rule_id"],
            severity=d.get("severity", "info"),
            message=d.get("message", ""),
            category=d.get("category", "semantics"),
            location=d.get("location"),
            metadata=d.get("metadata") or {},
        )


class FieldType(Enum):
    """BRAIN platform field types"""
    MATRIX = "MATRIX"  # Time-series data, supports ts_* operators
    VECTOR = "VECTOR"  # Cross-sectional/static data, supports vec_* operators
    GROUP = "GROUP"    # Grouping fields (sector, industry, etc.)
    UNKNOWN = "UNKNOWN"


@dataclass
class FieldInfo:
    """Field metadata for validation"""
    field_id: str
    field_type: FieldType = FieldType.UNKNOWN
    coverage: float = 1.0
    alpha_count: int = 0
    pyramid_multiplier: float = 1.0
    description: str = ""
    
    @classmethod
    def from_dict(cls, d: Dict) -> "FieldInfo":
        field_type_str = d.get("type", "MATRIX")
        try:
            field_type = FieldType(field_type_str.upper()) if field_type_str else FieldType.UNKNOWN
        except ValueError:
            field_type = FieldType.UNKNOWN
            
        return cls(
            field_id=d.get("id") or d.get("name", ""),
            field_type=field_type,
            coverage=d.get("coverage", 1.0) or 1.0,
            alpha_count=d.get("alpha_count", 0) or 0,
            pyramid_multiplier=d.get("pyramid_multiplier", 1.0) or 1.0,
            description=d.get("description", "")
        )


# =============================================================================
# Operator Registry - Dynamic loading from database
# =============================================================================

class OperatorRegistry:
    """
    Global registry for operators loaded from database.
    
    Provides:
    - Async loading from database
    - In-memory caching
    - Category-based operator sets
    
    Note: No hardcoded fallback - operators must be synced from BRAIN platform.
    """
    
    _instance: Optional["OperatorRegistry"] = None
    
    def __init__(self):
        self._operators: Set[str] = set()
        self._operators_by_category: Dict[str, Set[str]] = {}
        self._loaded = False
        self._warned = False  # Only warn once
        self._load_lock = asyncio.Lock()
    
    @classmethod
    def get_instance(cls) -> "OperatorRegistry":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @property
    def operators(self) -> Set[str]:
        """Get all known operators."""
        if not self._operators and not self._warned:
            logger.warning("[OperatorRegistry] No operators loaded. Run 'POST /api/v1/operators/sync' first.")
            self._warned = True
        return self._operators
    
    @property
    def ts_operators(self) -> Set[str]:
        """Get time-series operators."""
        return self._operators_by_category.get("Time Series", set())
    
    @property
    def vec_operators(self) -> Set[str]:
        """Get vector operators."""
        return self._operators_by_category.get("Vector", set())
    
    @property
    def group_operators(self) -> Set[str]:
        """Get group operators."""
        return self._operators_by_category.get("Group", set())
    
    async def load_from_db(self, db=None) -> bool:
        """
        Load operators from database.
        
        Args:
            db: AsyncSession instance (optional, will create if not provided)
            
        Returns:
            True if loaded successfully
        """
        async with self._load_lock:
            if self._loaded and self._operators:
                return True
            
            try:
                if db is None:
                    from backend.database import AsyncSessionLocal
                    async with AsyncSessionLocal() as session:
                        return await self._load_operators(session)
                else:
                    return await self._load_operators(db)
            except Exception as e:
                logger.error(f"[OperatorRegistry] Failed to load from DB: {e}. Sync operators first.")
                return False
    
    async def _load_operators(self, db) -> bool:
        """Internal load implementation."""
        from sqlalchemy import select, func
        from backend.models import Operator
        
        # First check total count
        count_result = await db.execute(select(func.count()).select_from(Operator))
        total_count = count_result.scalar()
        logger.debug(f"[OperatorRegistry] Total operators in DB: {total_count}")
        
        # Load all operators (don't filter by is_active, some may be NULL)
        result = await db.execute(
            select(Operator.name, Operator.category)
        )
        rows = result.all()
        
        if not rows:
            logger.warning("[OperatorRegistry] No operators in database. Run 'POST /api/v1/operators/sync' first.")
            return False
        
        self._operators = set()
        self._operators_by_category = {}
        
        for name, category in rows:
            if not name:
                continue
            name_lower = name.lower()
            self._operators.add(name_lower)
            
            if category:
                if category not in self._operators_by_category:
                    self._operators_by_category[category] = set()
                self._operators_by_category[category].add(name_lower)
        
        self._loaded = True
        self._warned = False  # Reset warning flag after successful load
        logger.info(f"[OperatorRegistry] Loaded {len(self._operators)} operators from database")
        return True
    
    def reload(self):
        """Force reload on next access."""
        self._loaded = False
        self._warned = False
        self._operators = set()
        self._operators_by_category = {}


# Global registry instance
_operator_registry = OperatorRegistry.get_instance()


async def load_operators_from_db(db=None) -> Set[str]:
    """
    Load operators from database.
    
    Convenience function for async contexts.
    """
    await _operator_registry.load_from_db(db)
    return _operator_registry.operators


def get_known_operators() -> Set[str]:
    """
    Get known operators (sync).
    
    Returns cached operators or fallback if not loaded.
    """
    return _operator_registry.operators


# Built-in group fields (these are not operators, kept hardcoded)
BUILTIN_GROUPS = {"sector", "subindustry", "industry", "exchange", "country", "market"}


@dataclass
class SemanticValidationResult:
    """Result of semantic validation (P1-E: structured Finding format).

    Internal storage is now `findings: List[Finding]`. Backward-compat
    `errors` / `warnings` / `error_messages` are exposed as derived
    properties so legacy callers (factor_tier_classifier logger,
    test_optimization_modules truthy assertions, KB regex categorizer)
    keep working without edits.
    """

    valid: bool = True
    findings: List[Finding] = field(default_factory=list)

    # Extracted info
    used_fields: Set[str] = field(default_factory=set)
    used_operators: Set[str] = field(default_factory=set)
    field_types_used: Set[str] = field(default_factory=set)

    # Metrics
    complexity_score: float = 0.0
    diversity_score: float = 0.0

    # P1-E: aggregated static risk bound (max_loss_hint + rationale + confidence).
    # Populated by `_aggregate_risk_bounds` at the end of validate().
    risk_bounds: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Primary emit helper — internal callers use this.
    # ------------------------------------------------------------------
    def _emit_finding(
        self,
        *,
        rule_id: str,
        severity: FindingSeverity,
        message: str,
        category: FindingCategory = "semantics",
        location: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        f = Finding(
            rule_id=rule_id,
            severity=severity,
            message=message,
            category=category,
            location=location,
            metadata=metadata or {},
        )
        self.findings.append(f)
        if severity == "hard":
            self.valid = False

    # ------------------------------------------------------------------
    # Backward-compat properties — return List[Finding] / List[str] views.
    # Truthy / iterable / `in` lookups on legacy assertions still work
    # because empty list is falsy and Finding.__repr__ contains the
    # original message.
    # ------------------------------------------------------------------
    @property
    def errors(self) -> List[Finding]:
        """Hard-severity findings (legacy `result.errors` callers)."""
        return [f for f in self.findings if f.severity == "hard"]

    @property
    def warnings(self) -> List[Finding]:
        """Soft+info-severity findings (legacy `result.warnings` callers)."""
        return [f for f in self.findings if f.severity in ("soft", "info")]

    @property
    def error_messages(self) -> List[str]:
        """S-7: emergency string-list escape for legacy logger / tests."""
        return [f.message for f in self.errors]

    @property
    def warning_messages(self) -> List[str]:
        """Symmetric helper to error_messages — string view of warnings."""
        return [f.message for f in self.warnings]

    # ------------------------------------------------------------------
    # Deprecated mutators — kept as one-shot shim emitting rule_id="other".
    # New code must call `_emit_finding` directly.
    # ------------------------------------------------------------------
    def add_error(self, msg: str) -> None:  # pragma: no cover - shim
        self._emit_finding(
            rule_id=RuleId.OTHER, severity="hard", message=msg, category="other",
        )

    def add_warning(self, msg: str) -> None:  # pragma: no cover - shim
        self._emit_finding(
            rule_id=RuleId.OTHER, severity="soft", message=msg, category="other",
        )


# =============================================================================
# P1-E: Static risk-bound inference (info-only Findings emitted at validate end)
# =============================================================================

# Re-use the divide-by-zero denom set from static_alpha_checks (single source
# of truth — S-2: don't verbatim copy). Augment with extra volatile fields that
# matter for max-loss pre-annotation but aren't a true divide-by-zero risk.
from backend.static_alpha_checks import DIVIDE_RISKY_DENOMS as _STATIC_RISKY_DENOMS

ADDITIONAL_VOLATILE_DENOMS: Set[str] = {
    "eps",
    "book_value_per_share",
    "cap",
    "sharesout",
    "adv5", "adv20", "adv60", "adv120",
}
ALL_VOLATILE_DENOMS: Set[str] = _STATIC_RISKY_DENOMS | ADDITIONAL_VOLATILE_DENOMS

# S-1: momentum operators whose decay-window pairing under-reacts to noise.
# Confirmed real (validator.py:65-107): ts_delta / ts_arg_max / ts_arg_min /
# ts_returns / ts_max_diff / ts_min_diff. Plan agent flagged the last three
# as "made up" — they're not, but we keep the catalog defensive (only ops
# that are present in validator.supported_functions).
_MOMENTUM_OPS: Set[str] = {
    "ts_delta", "ts_arg_max", "ts_arg_min",
    "ts_returns", "ts_max_diff", "ts_min_diff",
}

_MAX_LOSS_RANK = {"low": 1, "medium": 2, "high": 3}
_MAX_LOSS_RANK_INV = {v: k for k, v in _MAX_LOSS_RANK.items()}

_RISK_RULE_IDS: Tuple[str, ...] = (
    RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM,
    RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER,
    RuleId.RISK_SHORT_DECAY_WINDOW,
    RuleId.RISK_EXTREME_WINSORIZATION,
)


def _walk_call_args(expression: str, func_name: str) -> List[List[str]]:
    """Paren-balanced extraction of every `func_name(...)` call's argument list.

    M-6: Mandatory replacement for regex `[^,]+,\\s*(\\d+)` patterns which
    parse the inner comma of nested expressions like
    `signed_power(divide(x, y), 2)` and pick `y` instead of `2`.

    Returns a list of arg-lists — one entry per `func_name(...)` call site.
    Each arg is the raw substring between the (paren-depth-balanced) top-level
    commas, with leading/trailing whitespace stripped.

    Example:
        _walk_call_args("signed_power(divide(x, y), 2)", "signed_power")
        => [["divide(x, y)", "2"]]
        _walk_call_args("divide(close, ts_mean(returns, 5))", "divide")
        => [["close", "ts_mean(returns, 5)"]]
    """
    if not expression or not func_name:
        return []
    out: List[List[str]] = []
    n = len(expression)
    needle = func_name + "("
    needle_len = len(needle)
    i = 0
    while i < n:
        # Find next candidate `func_name(`; demand word-boundary on the left
        # so `ts_delta(` doesn't match inside `xts_delta(`.
        idx = expression.find(needle, i)
        if idx == -1:
            break
        # Left boundary check — char before must NOT be word-char.
        if idx > 0 and (expression[idx - 1].isalnum() or expression[idx - 1] == "_"):
            i = idx + 1
            continue
        # Walk depth-balanced from inside the parens, splitting at top-level commas.
        depth = 0
        j = idx + needle_len
        args: List[str] = []
        cur_start = j
        while j < n:
            c = expression[j]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    args.append(expression[cur_start:j].strip())
                    break
                depth -= 1
            elif c == "," and depth == 0:
                args.append(expression[cur_start:j].strip())
                cur_start = j + 1
            j += 1
        # Filter empties (e.g. trailing comma)
        args = [a for a in args if a != ""]
        if args:
            out.append(args)
        i = idx + 1
    return out


def _infer_risk_findings(expression: str) -> List[Finding]:
    """Pure-string analysis emitting info-severity risk Findings.

    M-6: every argument extraction uses `_walk_call_args` (paren-balanced)
    so nested expressions parse correctly. None of these findings invalidate
    the expression — they are context for downstream SELF_CORRECT and for
    `_aggregate_risk_bounds` max-loss-hint summarization.
    """
    findings: List[Finding] = []
    if not expression:
        return findings

    # R1: divide-by-volatile-denom — second arg contains a token from
    # ALL_VOLATILE_DENOMS (case-insensitive word-boundary).
    for divide_args in _walk_call_args(expression, "divide"):
        if len(divide_args) < 2:
            continue
        denom_low = divide_args[1].lower()
        # sorted() — set iteration order is non-deterministic; without
        # it `metadata["denom_field"]` differs across runs whenever the
        # denom contains multiple risky tokens (e.g. divide(x, eps*adv5)).
        for risky in sorted(ALL_VOLATILE_DENOMS):
            if re.search(rf"\b{re.escape(risky)}\b", denom_low):
                findings.append(Finding(
                    rule_id=RuleId.RISK_DIVIDE_BY_VOLATILE_DENOM,
                    severity="info",
                    message=(
                        f"divide-by-{risky} can amplify extreme tails on "
                        f"illiquid days"
                    ),
                    category="risk",
                    location=f"divide(_, …{risky}…)",
                    metadata={"max_loss_hint": "high", "denom_field": risky},
                ))
                break  # one finding per divide call site

    # R2: signed_power(_, exp) with |exp| > 1.5 — super-linear outlier
    # amplification.
    for sp_args in _walk_call_args(expression, "signed_power"):
        if len(sp_args) < 2:
            continue
        try:
            exp_val = float(sp_args[1].strip())
        except (ValueError, TypeError):
            continue
        if abs(exp_val) > 1.5:
            findings.append(Finding(
                rule_id=RuleId.RISK_HIGH_EXPONENT_SIGNED_POWER,
                severity="info",
                message=(
                    f"signed_power(_, {exp_val}) inflates outliers super-linearly"
                ),
                category="risk",
                location=f"signed_power(_, {exp_val})",
                metadata={"max_loss_hint": "high", "exponent": exp_val},
            ))

    # R3: ts_decay_linear(inner, d<4) with momentum-style inner — over-reacts
    # to single-bar noise. Requires BOTH d<4 AND momentum op in inner.
    for dl_args in _walk_call_args(expression, "ts_decay_linear"):
        if len(dl_args) < 2:
            continue
        try:
            d_val = int(float(dl_args[1].strip()))
        except (ValueError, TypeError):
            continue
        if d_val >= 4:
            continue
        inner_low = dl_args[0].lower()
        if not any(
            re.search(rf"\b{re.escape(op)}\b", inner_low) for op in _MOMENTUM_OPS
        ):
            continue
        findings.append(Finding(
            rule_id=RuleId.RISK_SHORT_DECAY_WINDOW,
            severity="info",
            message=(
                f"ts_decay_linear(_, {d_val}) + momentum inner over-reacts "
                f"to single-bar noise"
            ),
            category="risk",
            location=f"ts_decay_linear(…, {d_val})",
            metadata={"max_loss_hint": "medium", "decay_window": d_val},
        ))

    # R4: winsorize(_, std=S) with S outside [1, 6] — too tight clips signal,
    # too loose fails to trim outliers. Accept both positional `S` and `std=S`.
    for ws_args in _walk_call_args(expression, "winsorize"):
        if len(ws_args) < 2:
            continue
        std_str = ws_args[1].strip()
        if "=" in std_str:
            std_str = std_str.split("=", 1)[1].strip()
        try:
            std_val = float(std_str)
        except (ValueError, TypeError):
            continue
        if 1.0 <= std_val <= 6.0:
            continue
        too_tight = std_val < 1.0
        findings.append(Finding(
            rule_id=RuleId.RISK_EXTREME_WINSORIZATION,
            severity="info",
            message=(
                f"winsorize std={std_val} "
                f"{'clips legitimate signal' if too_tight else 'fails to trim outliers'}"
                f"; typical bound std in [1, 6]"
            ),
            category="risk",
            location=f"winsorize(_, {std_str})",
            metadata={
                "max_loss_hint": "medium",
                "winsorize_std": std_val,
                "too_tight": too_tight,
            },
        ))

    return findings


def _aggregate_risk_bounds(findings: List[Finding]) -> Dict[str, Any]:
    """Roll risk-category findings into a single max_loss_hint summary.

    Output schema (consumed by SELF_CORRECT prompt + alpha.metrics persistence):
        {
            "max_loss_hint": "low" | "medium" | "high",
            "rationale": List[rule_id, sorted],
            "confidence": float (0..1) — fired_rules / total_risk_rules,
            "severity_distribution": {"hard": int, "soft": int, "info": int},
        }
    """
    risk = [f for f in findings if f.category == "risk"]
    if not risk:
        return {}
    max_rank = max(
        _MAX_LOSS_RANK.get(f.metadata.get("max_loss_hint", "low"), 1) for f in risk
    )
    total_rules = len(_RISK_RULE_IDS)  # N-2: don't hardcode 4
    fired_rule_ids = {f.rule_id for f in risk}
    return {
        "max_loss_hint": _MAX_LOSS_RANK_INV[max_rank],
        "rationale": sorted(fired_rule_ids),
        "confidence": round(len(fired_rule_ids) / total_rules, 3),
        "severity_distribution": {
            sev: sum(1 for f in risk if f.severity == sev)
            for sev in ("hard", "soft", "info")
        },
    }


class AlphaSemanticValidator:
    """
    Enhanced semantic validator for alpha expressions.
    
    Validates:
    - Field existence in dataset
    - Operator existence in platform
    - Type constraints (MATRIX vs VECTOR)
    - Coverage warnings
    """
    
    def __init__(
        self,
        fields: Optional[List[Dict]] = None,
        operators: Optional[List[str]] = None,
        strict_field_check: bool = True,
        strict_type_check: bool = True
    ):
        """
        Initialize validator with dataset context.
        
        Args:
            fields: List of field dicts with id, type, coverage, etc.
            operators: List of allowed operator names
            strict_field_check: If True, unknown fields are errors; if False, warnings
            strict_type_check: If True, type mismatches are errors; if False, warnings
        """
        self.strict_field_check = strict_field_check
        self.strict_type_check = strict_type_check
        
        # Build field lookup
        self.field_map: Dict[str, FieldInfo] = {}
        if fields:
            for f in fields:
                info = FieldInfo.from_dict(f)
                if info.field_id:
                    self.field_map[info.field_id.lower()] = info
        
        # Build operator set
        self.allowed_operators: Set[str] = set()
        if operators:
            self.allowed_operators = {op.lower() for op in operators}
        else:
            # Default: allow all operators from registry (loaded from DB or fallback)
            self.allowed_operators = get_known_operators()
            
        # Regex patterns for parsing
        self._field_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b')
        self._func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
        
    def validate(self, expression: str) -> SemanticValidationResult:
        """
        Perform semantic validation on an expression.
        
        Args:
            expression: Alpha expression string
            
        Returns:
            SemanticValidationResult with errors, warnings, and extracted info
        """
        result = SemanticValidationResult()

        if not expression or not expression.strip():
            # P1-E S-3 row 1 (L300): empty_expression → hard / category=syntax
            result._emit_finding(
                rule_id=RuleId.EMPTY_EXPRESSION,
                severity="hard",
                message="Empty expression",
                category="syntax",
            )
            return result

        expression = expression.strip()

        # 1. Extract operators used
        operators_used = self._extract_operators(expression)
        result.used_operators = operators_used

        # 2. Extract fields used (identifiers not matching operators)
        fields_used = self._extract_fields(expression, operators_used)
        result.used_fields = fields_used

        # 3. Validate operators exist (M-1: unknown_operator stays soft)
        for op in operators_used:
            op_lower = op.lower()
            if self.allowed_operators and op_lower not in self.allowed_operators:
                # Check against all known operators from registry
                all_known = get_known_operators()
                if op_lower not in all_known:
                    # P1-E S-3 row 2 (L320): unknown_operator → soft (Q1 unchanged)
                    result._emit_finding(
                        rule_id=RuleId.UNKNOWN_OPERATOR,
                        severity="soft",
                        message=f"Unknown operator: {op}",
                        category="semantics",
                        location=op,
                    )

        # 4. Validate fields exist and collect type info
        matrix_fields = set()
        vector_fields = set()
        unknown_fields = set()

        for field_id in fields_used:
            field_lower = field_id.lower()

            # Skip built-in groups
            if field_lower in BUILTIN_GROUPS:
                continue

            # Skip numeric literals and keywords
            if field_lower in {"true", "false", "nan", "inf"}:
                continue

            if field_lower in self.field_map:
                info = self.field_map[field_lower]
                result.field_types_used.add(info.field_type.value)

                if info.field_type == FieldType.MATRIX:
                    matrix_fields.add(field_id)
                elif info.field_type == FieldType.VECTOR:
                    vector_fields.add(field_id)

                # P1-E S-3 row 3 (L349): low_coverage_field → soft / metadata.coverage
                if info.coverage < 0.5:
                    result._emit_finding(
                        rule_id=RuleId.LOW_COVERAGE_FIELD,
                        severity="soft",
                        message=f"Low coverage field: {field_id} ({info.coverage:.1%})",
                        category="semantics",
                        location=field_id,
                        metadata={"coverage": info.coverage},
                    )
            else:
                unknown_fields.add(field_id)

        # Handle unknown fields — M-2: severity is strict-mode-dependent.
        # P1-E S-3 rows 4-5 (L355-359).
        for field_id in unknown_fields:
            severity: FindingSeverity = "hard" if self.strict_field_check else "soft"
            result._emit_finding(
                rule_id=RuleId.FIELD_NOT_FOUND,
                severity=severity,
                message=f"Field not found in dataset: {field_id}",
                category="semantics",
                location=field_id,
            )

        # 5. Type constraint validation — M-2 style dynamic severity.
        # P1-E S-3 rows 6-7 (L367 / L369).
        type_errors = self._validate_type_constraints(
            expression, operators_used, matrix_fields, vector_fields
        )
        for err in type_errors:
            severity = "hard" if self.strict_type_check else "soft"
            result._emit_finding(
                rule_id=RuleId.TYPE_MISMATCH_VECTOR_TS,
                severity=severity,
                message=err,
                category="semantics",
            )

        # 6. Calculate complexity score
        result.complexity_score = len(operators_used) + len(fields_used) * 0.5

        # 7. P1-E: static risk-bound inference (info-only, never invalidates).
        for rf in _infer_risk_findings(expression):
            result.findings.append(rf)
        result.risk_bounds = _aggregate_risk_bounds(result.findings)

        return result
    
    def _extract_operators(self, expression: str) -> Set[str]:
        """Extract function/operator names from expression"""
        operators = set()
        for match in self._func_pattern.finditer(expression):
            operators.add(match.group(1))
        return operators
    
    def _extract_fields(self, expression: str, operators: Set[str]) -> Set[str]:
        """Extract field identifiers (non-operator identifiers)"""
        fields = set()
        op_lower = {op.lower() for op in operators}
        
        # Keywords and built-ins to skip
        skip = {
            "true", "false", "nan", "inf",
            "sector", "subindustry", "industry", "exchange", "country", "market",
            "std", "k", "mode", "lag", "rettype", "filter", "scale", "rate",
            "constant", "percentage", "driver", "sigma", "lower", "upper",
            "target", "dest", "event", "sensitivity", "force", "h", "t", "period",
            "stddev", "factor", "usetd", "limit", "gaussian", "uniform", "cauchy",
            "buckets", "range", "nth", "precise", "longscale", "shortscale"
        }
        
        for match in self._field_pattern.finditer(expression):
            ident = match.group(1)
            ident_lower = ident.lower()
            
            # Skip if it's an operator
            if ident_lower in op_lower:
                continue
                
            # Skip keywords/params
            if ident_lower in skip:
                continue
                
            # Skip pure numbers (shouldn't match pattern but just in case)
            if ident.isdigit():
                continue
                
            fields.add(ident)
            
        return fields
    
    def _validate_type_constraints(
        self,
        expression: str,
        operators: Set[str],
        matrix_fields: Set[str],
        vector_fields: Set[str]
    ) -> List[str]:
        """
        Validate that field types match operator requirements.
        
        Key rules:
        - ts_* operators work best with MATRIX fields (time-series)
        - vec_* operators require VECTOR fields
        - Using VECTOR fields with ts_* may cause issues
        """
        errors = []
        
        expr_lower = expression.lower()
        
        for op in operators:
            op_lower = op.lower()
            
            # Check ts_* operators with VECTOR fields (use naming convention)
            if op_lower.startswith("ts_"):
                # Look for vec_ prefix fields being passed to ts_ functions
                # This is a heuristic - we look for vector field names near ts_ calls
                for vf in vector_fields:
                    # Simple heuristic: if vector field appears right after ts_xxx(
                    pattern = rf'{op_lower}\s*\(\s*{re.escape(vf.lower())}'
                    if re.search(pattern, expr_lower):
                        errors.append(
                            f"Type mismatch: VECTOR field '{vf}' used as first arg of time-series operator '{op}'. "
                            f"Consider using vec_* wrapper or MATRIX equivalent."
                        )
                        
            # Check vec_* operators - they expect aggregation over vector dimensions
            # (vec_* operators on MATRIX fields is actually fine - aggregates across vector dim)
                
        return errors


def compute_expression_hash(expression: str) -> str:
    """
    Compute a normalized hash for expression deduplication.
    
    Normalizes:
    - Whitespace
    - Case (for operators)
    - Numeric precision
    """
    # Normalize whitespace
    normalized = " ".join(expression.split())
    
    # Normalize operator case using registry
    for op in get_known_operators():
        pattern = re.compile(re.escape(op), re.IGNORECASE)
        normalized = pattern.sub(op.lower(), normalized)
        
    # Hash
    return hashlib.md5(normalized.encode()).hexdigest()


def compute_structural_similarity(expr1: str, expr2: str) -> float:
    """
    Compute structural similarity between two expressions.
    
    Based on:
    - Operator n-gram overlap
    - Field Jaccard similarity
    
    Returns: Similarity score 0.0 to 1.0
    """
    validator = AlphaSemanticValidator()
    
    # Extract operators
    ops1 = validator._extract_operators(expr1)
    ops2 = validator._extract_operators(expr2)
    
    # Extract fields
    fields1 = validator._extract_fields(expr1, ops1)
    fields2 = validator._extract_fields(expr2, ops2)
    
    # Operator overlap (Jaccard)
    if ops1 or ops2:
        op_jaccard = len(ops1 & ops2) / len(ops1 | ops2) if (ops1 | ops2) else 0
    else:
        op_jaccard = 1.0
        
    # Field overlap (Jaccard)
    if fields1 or fields2:
        field_jaccard = len(fields1 & fields2) / len(fields1 | fields2) if (fields1 | fields2) else 0
    else:
        field_jaccard = 1.0
        
    # Weighted combination
    return 0.4 * op_jaccard + 0.6 * field_jaccard


class ExpressionDeduplicator:
    """
    Track seen expressions and detect duplicates.
    
    P0-2: Deduplication gate before simulation
    """
    
    def __init__(self, similarity_threshold: float = 0.85):
        self.seen_hashes: Set[str] = set()
        self.seen_expressions: List[str] = []
        self.similarity_threshold = similarity_threshold
        
    def is_duplicate(self, expression: str) -> Tuple[bool, Optional[str]]:
        """
        Check if expression is a duplicate.
        
        Returns:
            (is_duplicate, reason)
        """
        expr_hash = compute_expression_hash(expression)
        
        # Exact hash match
        if expr_hash in self.seen_hashes:
            return True, "Exact duplicate (hash match)"
            
        # Structural similarity check (expensive, limit to recent)
        recent = self.seen_expressions[-100:]  # Only check last 100
        for seen in recent:
            sim = compute_structural_similarity(expression, seen)
            if sim >= self.similarity_threshold:
                return True, f"Structurally similar ({sim:.1%}) to: {seen[:50]}..."
                
        return False, None
        
    def add(self, expression: str):
        """Add expression to seen set"""
        expr_hash = compute_expression_hash(expression)
        self.seen_hashes.add(expr_hash)
        self.seen_expressions.append(expression)
        
    def clear(self):
        """Clear all seen expressions"""
        self.seen_hashes.clear()
        self.seen_expressions.clear()


# =============================================================================
# Integration helper for node_validate
# =============================================================================

def validate_alpha_semantically(
    expression: str,
    fields: List[Dict],
    operators: Optional[List[str]] = None,
    strict: bool = False
) -> Dict[str, Any]:
    """
    Convenience function for semantic validation.
    
    Args:
        expression: Alpha expression
        fields: List of field dicts from state
        operators: Optional list of allowed operators
        strict: If True, use strict checking
        
    Returns:
        Dict with 'valid', 'errors', 'warnings', 'used_fields', 'used_operators'
    """
    validator = AlphaSemanticValidator(
        fields=fields,
        operators=operators,
        strict_field_check=strict,
        strict_type_check=strict
    )
    
    result = validator.validate(expression)

    return {
        "valid": result.valid,
        # P1-E: return string-list views for legacy `errors/warnings` callers
        # (test_suite, factor_tier logger) — full Finding dicts available via
        # `findings` key for new structured consumers.
        "errors": result.error_messages,
        "warnings": result.warning_messages,
        "findings": [f.to_dict() for f in result.findings],
        "risk_bounds": result.risk_bounds,
        "used_fields": list(result.used_fields),
        "used_operators": list(result.used_operators),
        "field_types": list(result.field_types_used),
        "complexity_score": result.complexity_score,
    }
