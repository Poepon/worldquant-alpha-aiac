"""
Knowledge Extraction Module - Enhanced pattern extraction for feedback learning.

P1-3: Success/Pitfall knowledge representation upgrade
- AST skeleton extraction
- Field type combination tracking
- Decay mechanism for patterns
- Evidence chain linking

P2-1: AST mutation and crossover support
"""

import re
import hashlib
import math
from typing import Dict, List, Any, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from loguru import logger


# =============================================================================
# AST Skeleton Extraction
# =============================================================================

@dataclass
class OperatorNode:
    """Simplified AST node for pattern extraction"""
    name: str
    children: List["OperatorNode"] = field(default_factory=list)
    depth: int = 0
    
    def to_skeleton(self, max_depth: int = 3) -> str:
        """Convert to skeleton string (limited depth)"""
        if self.depth >= max_depth:
            return f"{self.name}(...)"
        if not self.children:
            return self.name
        child_skels = [c.to_skeleton(max_depth) for c in self.children[:3]]  # Limit children
        return f"{self.name}({', '.join(child_skels)})"


def extract_operator_tree(expression: str, max_depth: int = 4) -> Optional[OperatorNode]:
    """
    Extract simplified operator tree from expression.
    
    This creates a skeleton representation that captures the structure
    without specific field names or numeric parameters.
    """
    expression = expression.strip()
    if not expression:
        return None
        
    # Simple recursive descent parser for function calls
    def parse_at(pos: int, depth: int) -> Tuple[Optional[OperatorNode], int]:
        if depth > max_depth or pos >= len(expression):
            return None, pos
            
        # Skip whitespace
        while pos < len(expression) and expression[pos].isspace():
            pos += 1
            
        if pos >= len(expression):
            return None, pos
            
        # Check for function call: name(...)
        match = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', expression[pos:])
        if match:
            func_name = match.group(1).lower()
            pos += match.end()
            
            children = []
            # Parse arguments
            paren_depth = 1
            arg_start = pos
            
            while pos < len(expression) and paren_depth > 0:
                char = expression[pos]
                if char == '(':
                    paren_depth += 1
                elif char == ')':
                    paren_depth -= 1
                    if paren_depth == 0:
                        # Parse last argument
                        arg_text = expression[arg_start:pos].strip()
                        if arg_text:
                            child, _ = parse_at(arg_start, depth + 1)
                            if child:
                                children.append(child)
                elif char == ',' and paren_depth == 1:
                    # Parse argument
                    arg_text = expression[arg_start:pos].strip()
                    if arg_text:
                        child, _ = parse_at(arg_start, depth + 1)
                        if child:
                            children.append(child)
                    arg_start = pos + 1
                pos += 1
                
            return OperatorNode(name=func_name, children=children, depth=depth), pos
        else:
            # Not a function call - could be field, number, or expression
            # Find the extent of this token
            token_match = re.match(r'[a-zA-Z_][a-zA-Z0-9_]*', expression[pos:])
            if token_match:
                token = token_match.group(0)
                # Check if it's a known operator used without parens (shouldn't happen in valid expr)
                return OperatorNode(name="FIELD", depth=depth), pos + len(token)
            elif expression[pos].isdigit() or expression[pos] == '-':
                # Number
                num_match = re.match(r'-?\d+\.?\d*', expression[pos:])
                if num_match:
                    return OperatorNode(name="NUM", depth=depth), pos + len(num_match.group(0))
            return None, pos + 1
            
    root, _ = parse_at(0, 0)
    return root


def enumerate_subtrees(node: Optional["OperatorNode"], max_depth: int = 3) -> set:
    """Phase 1 R3/Q8 (2026-05-17): depth-first enumerate every subtree skeleton.

    Returns a set of string-hashed subtree skeletons (via ``OperatorNode.to_skeleton``).
    Empty set for None node or depth-overflow. Used by ``ast_distance`` to
    compute Jaccard-style subtree overlap between two expression trees.

    Brute-force O(n) per call (recursion over n nodes). Plan §2.2 verified
    AIAC alpha AST n ≤ 8 truncated at max_depth=3 — perf irrelevant.
    """
    if node is None or getattr(node, "depth", 0) > max_depth:
        return set()
    # to_skeleton uses ABSOLUTE depth check (self.depth >= max_depth), so a
    # subtree rooted at depth=2 with max_depth=3 would truncate all its
    # descendants to "name(...)". Pass max_depth + node.depth so the cap is
    # relative to the subtree root (preserves to_skeleton's absolute-depth
    # semantics for other callers).
    rel_max = max_depth + getattr(node, "depth", 0)
    out = {node.to_skeleton(max_depth=rel_max)}
    for child in getattr(node, "children", []):
        out |= enumerate_subtrees(child, max_depth)
    return out


def ast_distance(t1: Optional["OperatorNode"], t2: Optional["OperatorNode"],
                 max_depth: int = 3) -> float:
    """Phase 1 R3/Q8: 1 − Jaccard subtree overlap between two trees.

    Returns:
      0.0 — identical subtree population (most similar)
      1.0 — disjoint subtree populations (most different)

    Brute-force O(n²) — fine for AIAC AST n < 20 at max_depth ≤ 3 per
    plan §2.2 verification. Shamir-Tsur 1999 deferred to Phase 2+ (the
    plan-cited O(n²·⁵/log n) bound was unverifiable; brute-force matches
    actual perf needs).

    Special cases:
      - Both trees None / empty subtree sets → 0.0 (identical-by-vacuity)
      - One empty + one populated → 1.0 (max distance)
    """
    s1 = enumerate_subtrees(t1, max_depth)
    s2 = enumerate_subtrees(t2, max_depth)
    if not s1 and not s2:
        return 0.0
    if not s1 or not s2:
        return 1.0
    union = s1 | s2
    return 1.0 - (len(s1 & s2) / len(union))


def ast_distance_from_expressions(expr1: str, expr2: str, max_depth: int = 3) -> float:
    """Convenience: parse both expressions + compute ast_distance.

    Caller-friendly wrapper for diversity_tracker hot path; isolates the
    parse-failure handling so the bandit hot path never raises.
    """
    try:
        t1 = extract_operator_tree(expr1, max_depth) if expr1 else None
        t2 = extract_operator_tree(expr2, max_depth) if expr2 else None
        return ast_distance(t1, t2, max_depth)
    except Exception:
        # Parse failures shouldn't crash the diversity-gate hot path —
        # return max distance (1.0) so the unparseable input is treated as
        # maximally novel rather than rejecting / pinning to 0.
        return 1.0


def expression_to_skeleton(expression: str, max_depth: int = 3) -> str:
    """
    Convert expression to skeleton string.
    
    Example:
        "ts_rank(ts_delta(close, 5), 20)" -> "ts_rank(ts_delta(...))"
    """
    tree = extract_operator_tree(expression, max_depth)
    if tree:
        return tree.to_skeleton(max_depth)
    return "UNKNOWN"


def extract_operator_chain(expression: str) -> List[str]:
    """
    Extract ordered list of operators from expression.
    
    This captures the "operator recipe" without structure details.
    """
    func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    operators = []
    
    for match in func_pattern.finditer(expression):
        op = match.group(1).lower()
        operators.append(op)
        
    return operators


# =============================================================================
# P1-3: Enhanced Pattern Representation
# =============================================================================

@dataclass
class AlphaPattern:
    """
    Enhanced pattern representation for knowledge base.
    
    Captures:
    - Operator skeleton (structural pattern)
    - Field type combination
    - Applicable conditions (region, dataset, etc.)
    - Evidence chain (linked alphas)
    - Decay info
    """
    pattern_id: str
    pattern_type: str  # "SUCCESS" or "PITFALL"
    
    # Structural info
    skeleton: str
    operator_chain: List[str]
    field_types: Set[str]  # e.g., {"MATRIX", "VECTOR"}
    
    # Applicability
    region: str = "USA"
    universe: str = "TOP3000"
    dataset_id: Optional[str] = None
    
    # Evidence
    alpha_ids: List[str] = field(default_factory=list)
    example_expression: str = ""
    
    # Metrics (for success patterns)
    avg_sharpe: float = 0.0
    avg_fitness: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    
    # Decay info
    created_at: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)
    use_count: int = 0
    
    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.5
    
    @property
    def confidence(self) -> float:
        """Confidence based on evidence count"""
        return min(len(self.alpha_ids) / 10.0, 1.0)
    
    def decay_weight(self, half_life_days: int = 30) -> float:
        """
        Calculate decay weight based on time since last use.
        
        Older patterns get lower weight to prevent stale patterns
        from dominating.
        """
        days_since = (datetime.now() - self.last_used).days
        return math.pow(0.5, days_since / half_life_days)
    
    def effective_score(self, half_life_days: int = 30) -> float:
        """Combined score considering success rate, confidence, and decay"""
        base = self.success_rate * self.confidence
        decay = self.decay_weight(half_life_days)
        return base * decay
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage"""
        return {
            "pattern_id": self.pattern_id,
            "pattern_type": self.pattern_type,
            "skeleton": self.skeleton,
            "operator_chain": self.operator_chain,
            "field_types": list(self.field_types),
            "region": self.region,
            "universe": self.universe,
            "dataset_id": self.dataset_id,
            "alpha_ids": self.alpha_ids,
            "example_expression": self.example_expression,
            "avg_sharpe": self.avg_sharpe,
            "avg_fitness": self.avg_fitness,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "use_count": self.use_count,
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat(),
        }
    
    @classmethod
    def from_expression(
        cls,
        expression: str,
        pattern_type: str,
        alpha_id: str,
        metrics: Dict[str, Any],
        field_types: Set[str],
        region: str = "USA",
        universe: str = "TOP3000",
        dataset_id: Optional[str] = None
    ) -> "AlphaPattern":
        """Create pattern from successful/failed alpha"""
        skeleton = expression_to_skeleton(expression)
        op_chain = extract_operator_chain(expression)
        
        # Generate stable pattern ID from skeleton + context
        id_base = f"{skeleton}:{region}:{universe}:{dataset_id or 'any'}"
        pattern_id = hashlib.md5(id_base.encode()).hexdigest()[:12]
        
        sharpe = metrics.get("sharpe", 0) or 0
        fitness = metrics.get("fitness", 0) or 0
        
        return cls(
            pattern_id=pattern_id,
            pattern_type=pattern_type,
            skeleton=skeleton,
            operator_chain=op_chain,
            field_types=field_types,
            region=region,
            universe=universe,
            dataset_id=dataset_id,
            alpha_ids=[alpha_id] if alpha_id else [],
            example_expression=expression[:500],
            avg_sharpe=sharpe,
            avg_fitness=fitness,
            success_count=1 if pattern_type == "SUCCESS" else 0,
            fail_count=1 if pattern_type == "PITFALL" else 0,
        )


class PatternRegistry:
    """
    In-memory pattern registry with decay management.
    
    Can be persisted to/from database.
    """
    
    def __init__(self, max_patterns: int = 1000, decay_half_life_days: int = 30):
        self.patterns: Dict[str, AlphaPattern] = {}
        self.max_patterns = max_patterns
        self.decay_half_life = decay_half_life_days
        
    def add_or_update(self, pattern: AlphaPattern):
        """Add new pattern or update existing"""
        if pattern.pattern_id in self.patterns:
            existing = self.patterns[pattern.pattern_id]
            # Merge evidence
            existing.alpha_ids = list(set(existing.alpha_ids + pattern.alpha_ids))[-20:]  # Keep last 20
            existing.success_count += pattern.success_count
            existing.fail_count += pattern.fail_count
            # Update averages
            total = existing.success_count + existing.fail_count
            if total > 0:
                existing.avg_sharpe = (existing.avg_sharpe * (total - 1) + pattern.avg_sharpe) / total
                existing.avg_fitness = (existing.avg_fitness * (total - 1) + pattern.avg_fitness) / total
            existing.last_used = datetime.now()
            existing.use_count += 1
        else:
            self.patterns[pattern.pattern_id] = pattern
            
        # Prune if too many
        self._prune()
        
    def _prune(self):
        """Remove lowest-scoring patterns if over limit"""
        if len(self.patterns) <= self.max_patterns:
            return
            
        # Sort by effective score
        sorted_patterns = sorted(
            self.patterns.values(),
            key=lambda p: p.effective_score(self.decay_half_life),
            reverse=True
        )
        
        # Keep top N
        keep_ids = {p.pattern_id for p in sorted_patterns[:self.max_patterns]}
        self.patterns = {k: v for k, v in self.patterns.items() if k in keep_ids}
        
    def get_top_patterns(
        self,
        pattern_type: str,
        n: int = 10,
        region: Optional[str] = None,
        dataset_id: Optional[str] = None
    ) -> List[AlphaPattern]:
        """Get top patterns by effective score with optional filtering"""
        candidates = [
            p for p in self.patterns.values()
            if p.pattern_type == pattern_type
        ]
        
        if region:
            candidates = [p for p in candidates if p.region == region]
        if dataset_id:
            candidates = [p for p in candidates if p.dataset_id == dataset_id or p.dataset_id is None]
            
        # Sort by effective score
        candidates.sort(key=lambda p: p.effective_score(self.decay_half_life), reverse=True)
        
        return candidates[:n]
    
    def record_use(self, pattern_id: str):
        """Record that a pattern was used (for decay tracking)"""
        if pattern_id in self.patterns:
            self.patterns[pattern_id].last_used = datetime.now()
            self.patterns[pattern_id].use_count += 1


# =============================================================================
# P2-1: AST Mutation Operators
# =============================================================================

# Common operator substitution groups
OPERATOR_SUBSTITUTIONS = {
    # Time-series ranking/normalization
    "ts_rank": ["ts_zscore", "rank", "ts_quantile"],
    "ts_zscore": ["ts_rank", "rank", "normalize"],
    "rank": ["ts_rank", "ts_zscore", "quantile"],
    
    # Time-series statistics
    "ts_mean": ["ts_median", "ts_sum", "ts_decay_linear"],
    "ts_std_dev": ["ts_kurtosis", "ts_skewness", "ts_ir"],
    "ts_delta": ["ts_returns", "ts_av_diff", "ts_max_diff"],
    
    # Cross-sectional
    "group_rank": ["group_zscore", "group_normalize"],
    "group_mean": ["group_median", "group_sum"],
    "group_neutralize": ["group_zscore", "regression_neut"],
    
    # Arithmetic
    "log": ["sqrt", "sigmoid", "tanh", "s_log_1p"],
    "abs": ["sign", "sigmoid"],
}

# Common window parameter variations
WINDOW_VARIATIONS = [5, 10, 20, 40, 60, 120, 252]


def mutate_operator(expression: str, mutation_rate: float = 0.3) -> List[str]:
    """
    Generate operator mutations of an expression.
    
    Substitutes operators with semantically similar alternatives.
    """
    import random
    
    mutations = []
    func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    
    for match in func_pattern.finditer(expression):
        op = match.group(1).lower()
        
        if op in OPERATOR_SUBSTITUTIONS and random.random() < mutation_rate:
            for alt in OPERATOR_SUBSTITUTIONS[op]:
                mutated = expression[:match.start(1)] + alt + expression[match.end(1):]
                mutations.append(mutated)
                
    return mutations


def mutate_windows(expression: str) -> List[str]:
    """
    Generate window parameter mutations.
    
    Replaces numeric window parameters with alternatives.
    """
    mutations = []
    
    # Find patterns like ts_xxx(field, N) or ts_xxx(field, N, ...)
    window_pattern = re.compile(r'(ts_\w+\s*\([^,]+,\s*)(\d+)([,\)])')
    
    for match in window_pattern.finditer(expression):
        original_window = int(match.group(2))
        
        for new_window in WINDOW_VARIATIONS:
            if new_window != original_window:
                mutated = (
                    expression[:match.start(2)] + 
                    str(new_window) + 
                    expression[match.end(2):]
                )
                mutations.append(mutated)
                
    return mutations


def crossover_expressions(expr1: str, expr2: str) -> List[str]:
    """
    Generate crossover variants by combining subexpressions.
    
    Simple strategy: swap outer operators or combine inner expressions.
    """
    crossovers = []
    
    # Extract outer operator from each
    outer1_match = re.match(r'(\w+)\s*\((.+)\)$', expr1.strip())
    outer2_match = re.match(r'(\w+)\s*\((.+)\)$', expr2.strip())
    
    if outer1_match and outer2_match:
        op1, inner1 = outer1_match.groups()
        op2, inner2 = outer2_match.groups()
        
        # Swap outer operators
        crossovers.append(f"{op2}({inner1})")
        crossovers.append(f"{op1}({inner2})")
        
        # Combine (wrap one in other)
        crossovers.append(f"{op1}({expr2})")
        crossovers.append(f"{op2}({expr1})")
        
    return crossovers


def generate_variants(
    expression: str,
    max_variants: int = 10,
    include_mutations: bool = True,
    include_windows: bool = True,
    include_crossover: bool = False,
    crossover_partner: Optional[str] = None
) -> List[str]:
    """
    Generate expression variants for exploration.
    
    Returns list of variant expressions (deduplicated).
    """
    variants = set()
    
    if include_mutations:
        variants.update(mutate_operator(expression))
        
    if include_windows:
        variants.update(mutate_windows(expression))
        
    if include_crossover and crossover_partner:
        variants.update(crossover_expressions(expression, crossover_partner))
        
    # Remove original and empty
    variants.discard(expression)
    variants.discard("")
    
    # Return limited list
    return list(variants)[:max_variants]
