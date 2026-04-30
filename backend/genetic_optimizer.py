"""
Genetic Programming Optimizer - Systematic Mutation Search for Alpha Improvement

Features:
1. Population-based optimization with selection, mutation, crossover
2. Multi-objective fitness (Sharpe, Fitness, Turnover, Novelty)
3. Adaptive mutation rates based on improvement trajectory
4. Diversity maintenance through niching
5. Efficient batch simulation

This module performs systematic exploration of the alpha expression space
to find high-quality variants of promising alphas.
"""

import re
import random
import hashlib
from typing import List, Dict, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
from loguru import logger


# =============================================================================
# Configuration
# =============================================================================

# Operator substitution groups (semantically similar)
OPERATOR_GROUPS = {
    "rank_normalize": ["rank", "ts_rank", "ts_zscore", "zscore", "quantile"],
    "aggregation": ["ts_mean", "ts_median", "ts_sum", "ts_decay_linear"],
    "volatility": ["ts_std_dev", "ts_kurtosis", "ts_skewness"],
    "change": ["ts_delta", "ts_returns", "ts_av_diff", "ts_max_diff"],
    "extrema": ["ts_max", "ts_min", "ts_argmax", "ts_argmin"],
    "correlation": ["ts_corr", "ts_cov", "ts_covariance"],
    "group_ops": ["group_rank", "group_zscore", "group_mean", "group_neutralize"],
    "math": ["log", "sqrt", "abs", "sign", "sigmoid", "tanh"],
    "vector": ["vec_sum", "vec_avg", "vec_max", "vec_min", "vec_count"],
}

# Window values for mutation
WINDOW_VALUES = [5, 10, 20, 22, 40, 44, 60, 66, 120, 126, 252]

# Decay values
DECAY_VALUES = [0, 2, 4, 6, 8, 12, 16]

# Common wrapper patterns
WRAPPER_PATTERNS = [
    ("rank", "rank({})"),
    ("ts_rank", "ts_rank({}, 20)"),
    ("ts_zscore", "ts_zscore({}, 60)"),
    ("ts_decay_linear", "ts_decay_linear({}, 10)"),
    ("group_neutralize", "group_neutralize({}, sector)"),
    ("abs", "abs({})"),
    ("sign", "sign({})"),
]


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class Individual:
    """Represents an alpha expression individual in the population."""
    expression: str
    generation: int = 0
    parent_expression: str = ""
    
    # Fitness metrics
    sharpe: float = 0.0
    fitness: float = 0.0
    turnover: float = 0.0
    os_sharpe: float = 0.0
    
    # Derived scores
    overall_fitness: float = 0.0
    novelty_score: float = 0.0
    
    # Metadata
    mutation_type: str = ""
    mutation_description: str = ""
    simulated: bool = False
    passed: bool = False

    # W2: Island provenance — track which sub-population the individual
    # currently lives in so update_individual can route metrics back.
    island_id: int = 0
    
    @property
    def fingerprint(self) -> str:
        """Unique fingerprint for deduplication."""
        return hashlib.md5(self.expression.encode()).hexdigest()[:12]
    
    def calculate_fitness(self, weights: Dict[str, float] = None):
        """Calculate overall fitness from component metrics."""
        w = weights or {
            "sharpe": 0.50,
            "fitness": 0.20,
            "turnover": 0.15,  # Negative weight (lower is better)
            "os_sharpe": 0.15,
        }
        
        # Normalize components
        sharpe_score = min(1.0, self.sharpe / 2.0) if self.sharpe > 0 else 0
        fitness_score = min(1.0, self.fitness / 1.5) if self.fitness > 0 else 0
        turnover_score = max(0, 1.0 - self.turnover) if self.turnover < 1.0 else 0
        os_score = min(1.0, self.os_sharpe / 1.5) if self.os_sharpe > 0 else 0
        
        self.overall_fitness = (
            w["sharpe"] * sharpe_score +
            w["fitness"] * fitness_score +
            w["turnover"] * turnover_score +
            w["os_sharpe"] * os_score
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "expression": self.expression,
            "generation": self.generation,
            "sharpe": round(self.sharpe, 4),
            "fitness": round(self.fitness, 4),
            "turnover": round(self.turnover, 4),
            "os_sharpe": round(self.os_sharpe, 4),
            "overall_fitness": round(self.overall_fitness, 4),
            "mutation_type": self.mutation_type,
            "mutation_description": self.mutation_description,
            "passed": self.passed,
        }


@dataclass
class Population:
    """Collection of individuals with diversity management."""
    individuals: List[Individual] = field(default_factory=list)
    generation: int = 0
    
    # Tracking
    fingerprints: Set[str] = field(default_factory=set)
    best_fitness_history: List[float] = field(default_factory=list)
    
    def add(self, individual: Individual) -> bool:
        """Add individual if not duplicate. Returns True if added."""
        if individual.fingerprint in self.fingerprints:
            return False
        
        self.individuals.append(individual)
        self.fingerprints.add(individual.fingerprint)
        return True
    
    def get_best(self, n: int = 1) -> List[Individual]:
        """Get top N individuals by overall fitness."""
        sorted_pop = sorted(
            self.individuals,
            key=lambda x: x.overall_fitness,
            reverse=True
        )
        return sorted_pop[:n]
    
    def get_passed(self) -> List[Individual]:
        """Get individuals that passed quality threshold."""
        return [i for i in self.individuals if i.passed]
    
    def stats(self) -> Dict[str, Any]:
        """Get population statistics."""
        if not self.individuals:
            return {"size": 0}
        
        fitness_values = [i.overall_fitness for i in self.individuals if i.simulated]
        
        return {
            "size": len(self.individuals),
            "simulated": sum(1 for i in self.individuals if i.simulated),
            "passed": len(self.get_passed()),
            "avg_fitness": sum(fitness_values) / len(fitness_values) if fitness_values else 0,
            "max_fitness": max(fitness_values) if fitness_values else 0,
            "generation": self.generation,
        }


@dataclass
class OptimizationConfig:
    """Configuration for genetic optimization."""
    population_size: int = 50
    generations: int = 5
    mutation_rate: float = 0.3
    crossover_rate: float = 0.2
    elite_ratio: float = 0.1
    tournament_size: int = 3

    # Thresholds for passing
    sharpe_threshold: float = 1.25
    fitness_threshold: float = 1.0
    turnover_threshold: float = 0.7

    # Simulation budget
    max_simulations: int = 100

    # W2: Island-model parameters (per plan R3 — kept budget-neutral)
    # Total budget ≈ num_islands * island_size * generations.
    # Default 4×12×5 = 240, comparable to legacy single-pool 50×5 = 250.
    num_islands: int = 4
    migration_interval: int = 5  # generations between elite migrations
    migration_ratio: float = 0.10  # fraction of island swapped on each migration


# =============================================================================
# Mutation Operators
# =============================================================================

def mutate_operator_substitution(expression: str) -> Tuple[str, str]:
    """
    Substitute an operator with a semantically similar one.
    
    Returns:
        (mutated_expression, description)
    """
    # Find all function calls
    func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    matches = list(func_pattern.finditer(expression))
    
    if not matches:
        return expression, "no_change"
    
    # Pick random function to mutate
    match = random.choice(matches)
    func_name = match.group(1).lower()
    
    # Find operator group
    for group_name, operators in OPERATOR_GROUPS.items():
        if func_name in operators:
            # Pick different operator from same group
            alternatives = [op for op in operators if op != func_name]
            if alternatives:
                new_op = random.choice(alternatives)
                mutated = expression[:match.start(1)] + new_op + expression[match.end(1):]
                return mutated, f"operator_sub: {func_name} -> {new_op}"
    
    return expression, "no_substitution_found"


def mutate_window_parameter(expression: str) -> Tuple[str, str]:
    """
    Mutate window parameter values.
    
    Returns:
        (mutated_expression, description)
    """
    # Pattern: function(field, NUMBER)
    window_pattern = re.compile(r'(ts_\w+|group_\w+)\s*\(\s*([^,]+)\s*,\s*(\d+)')
    matches = list(window_pattern.finditer(expression))
    
    if not matches:
        return expression, "no_window_params"
    
    # Pick random window to mutate
    match = random.choice(matches)
    func_name = match.group(1)
    original_window = int(match.group(3))
    
    # Pick new window value
    new_window = random.choice([w for w in WINDOW_VALUES if w != original_window])
    
    mutated = expression[:match.start(3)] + str(new_window) + expression[match.end(3):]
    return mutated, f"window: {func_name} {original_window} -> {new_window}"


def mutate_add_wrapper(expression: str) -> Tuple[str, str]:
    """
    Add a wrapper function around the expression.
    
    Returns:
        (mutated_expression, description)
    """
    wrapper_name, pattern = random.choice(WRAPPER_PATTERNS)
    
    # Don't double-wrap with same function
    if expression.startswith(f"{wrapper_name}("):
        return expression, "already_wrapped"
    
    mutated = pattern.format(expression)
    return mutated, f"add_wrapper: {wrapper_name}"


def mutate_remove_wrapper(expression: str) -> Tuple[str, str]:
    """
    Remove outermost wrapper function.
    
    Returns:
        (mutated_expression, description)
    """
    # Check for function wrapper pattern
    wrapper_pattern = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(.+)\s*\)$', expression.strip())
    
    if wrapper_pattern:
        wrapper = wrapper_pattern.group(1)
        inner = wrapper_pattern.group(2)
        
        # Don't remove if inner has unbalanced parens
        if inner.count('(') == inner.count(')'):
            return inner, f"remove_wrapper: {wrapper}"
    
    return expression, "no_wrapper_to_remove"


def mutate_sign_flip(expression: str) -> Tuple[str, str]:
    """
    Flip the sign of the expression.
    
    Returns:
        (mutated_expression, description)
    """
    if expression.startswith("-1 * ") or expression.startswith("-1*"):
        # Remove negative
        mutated = expression.replace("-1 * ", "", 1).replace("-1*", "", 1)
        return mutated, "remove_negative"
    elif expression.startswith("-(") and expression.endswith(")"):
        # Remove negation wrapper
        return expression[2:-1], "remove_negation"
    else:
        # Add negative
        return f"-1 * ({expression})", "add_negative"


def mutate_structure_modification(expression: str) -> Tuple[str, str]:
    """
    Modify expression structure (e.g., add neutralization).
    
    Returns:
        (mutated_expression, description)
    """
    modifications = [
        (f"group_neutralize({expression}, sector)", "add sector neutralization"),
        (f"group_neutralize({expression}, industry)", "add industry neutralization"),
        (f"ts_decay_linear({expression}, 5)", "add short decay"),
        (f"ts_decay_linear({expression}, 10)", "add medium decay"),
        (f"pasteurize({expression})", "add pasteurize"),
    ]
    
    # Filter out already present modifications
    filtered = []
    for mod_expr, desc in modifications:
        key = desc.split()[1] if len(desc.split()) > 1 else desc
        if key not in expression.lower():
            filtered.append((mod_expr, desc))
    
    if filtered:
        mutated, desc = random.choice(filtered)
        return mutated, f"structure: {desc}"
    
    return expression, "no_structure_change"


# =============================================================================
# Crossover Operators
# =============================================================================

def crossover_swap_inner(expr1: str, expr2: str) -> Tuple[str, str]:
    """
    Swap inner expressions between two alphas.
    
    Returns:
        (child1, child2)
    """
    # Extract outer function and inner expression
    pattern = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(.+)\s*\)$', expr1.strip())
    if not pattern:
        return expr1, expr2
    
    outer1, inner1 = pattern.groups()
    
    pattern2 = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(.+)\s*\)$', expr2.strip())
    if not pattern2:
        return expr1, expr2
    
    outer2, inner2 = pattern2.groups()
    
    # Swap inners
    child1 = f"{outer1}({inner2})"
    child2 = f"{outer2}({inner1})"
    
    return child1, child2


def crossover_combine(expr1: str, expr2: str) -> str:
    """
    Combine two expressions with an arithmetic operator.
    
    Returns:
        Combined expression
    """
    operators = [
        ("add", f"add({expr1}, {expr2})"),
        ("multiply", f"multiply({expr1}, {expr2})"),
        ("average", f"divide(add({expr1}, {expr2}), 2)"),
    ]
    
    _, combined = random.choice(operators)
    return combined


# =============================================================================
# Genetic Optimizer
# =============================================================================

class GeneticOptimizer:
    """
    Genetic programming optimizer for alpha expressions.
    
    Usage:
        optimizer = GeneticOptimizer(config)
        
        # Initialize with seed expression
        optimizer.initialize(seed_expression, seed_metrics)
        
        # Evolve population
        for gen in range(config.generations):
            # Get individuals to simulate
            candidates = optimizer.get_simulation_candidates(batch_size=10)
            
            # Simulate and update
            for ind, result in zip(candidates, simulation_results):
                optimizer.update_individual(ind, result)
            
            # Evolve to next generation
            optimizer.evolve()
        
        # Get best results
        best = optimizer.get_best_individuals(n=5)
    """
    
    def __init__(self, config: OptimizationConfig = None):
        self.config = config or OptimizationConfig()
        self.all_fingerprints: Set[str] = set()  # Global dedup across islands
        self.simulations_used = 0

        # W2: Island model — 4 isolated sub-populations exchanging elite
        # individuals every `migration_interval` generations. Per plan R3
        # we keep the total budget close to the legacy single-pool size to
        # avoid quietly inflating BRAIN simulation usage.
        self._island_size = max(8, self.config.population_size // self.config.num_islands)
        self.islands: List[Population] = [Population() for _ in range(self.config.num_islands)]

        # Per-island adaptive mutation rates (initially uniform; diversity
        # emerges via adapt_mutation_rates over generations rather than
        # hand-coded biases — see plan R5 修订项 1).
        default_rates = {
            "operator_sub": 0.25,
            "window": 0.25,
            "add_wrapper": 0.15,
            "remove_wrapper": 0.10,
            "sign_flip": 0.10,
            "structure": 0.15,
        }
        self.mutation_rates_per_island: List[Dict[str, float]] = [
            dict(default_rates) for _ in range(self.config.num_islands)
        ]

        # Backwards-compat: callers that touch self.population get island 0
        # as a stand-in (sufficient for read-only stats/best lookups; for
        # mutating ops use the new island-aware methods).
        self.population = self.islands[0]
        self.mutation_rates = self.mutation_rates_per_island[0]

        # Tracking
        self.generation_stats: List[Dict] = []
    
    def initialize(
        self,
        seed_expression: str,
        seed_metrics: Dict[str, float] = None
    ):
        """
        Initialize island populations with the seed expression and per-island
        mutations. Each island starts with the same seed but evolves
        independently; periodic migration cross-pollinates elite individuals.
        """
        self.islands = [Population() for _ in range(self.config.num_islands)]
        self.population = self.islands[0]
        self.all_fingerprints.clear()

        for island_id, island in enumerate(self.islands):
            seed = Individual(
                expression=seed_expression,
                generation=0,
                mutation_type="seed",
                mutation_description="original",
                island_id=island_id,
            )
            if seed_metrics:
                seed.sharpe = seed_metrics.get("sharpe", 0)
                seed.fitness = seed_metrics.get("fitness", 0)
                seed.turnover = seed_metrics.get("turnover", 0)
                seed.os_sharpe = seed_metrics.get("os_sharpe", 0)
                seed.calculate_fitness()
                seed.simulated = True
            island.add(seed)
            self.all_fingerprints.add(seed.fingerprint)
            self._generate_initial_mutations_for_island(seed_expression, island_id)

        total = sum(len(i.individuals) for i in self.islands)
        logger.info(
            f"[GeneticOpt] Initialized {self.config.num_islands} islands "
            f"(total population={total}, per-island={self._island_size}) "
            f"seed_fitness={self.islands[0].individuals[0].overall_fitness:.3f}"
        )
    
    def _generate_initial_mutations(self, seed: str, count: int = None):
        """Backwards-compat shim: populate island 0 only."""
        self._generate_initial_mutations_for_island(seed, island_id=0, count=count)

    def _generate_initial_mutations_for_island(
        self, seed: str, island_id: int, count: int = None
    ):
        """Generate initial mutations for a specific island."""
        target_count = count or self._island_size
        island = self.islands[island_id]

        mutation_funcs = [
            mutate_operator_substitution,
            mutate_window_parameter,
            mutate_add_wrapper,
            mutate_remove_wrapper,
            mutate_sign_flip,
            mutate_structure_modification,
        ]

        attempts = 0
        max_attempts = target_count * 3

        while len(island.individuals) < target_count and attempts < max_attempts:
            mutation_func = random.choice(mutation_funcs)
            mutated, description = mutation_func(seed)

            if mutated != seed and "no_" not in description:
                ind = Individual(
                    expression=mutated,
                    generation=0,
                    parent_expression=seed,
                    mutation_type=mutation_func.__name__.replace("mutate_", ""),
                    mutation_description=description,
                    island_id=island_id,
                )

                if ind.fingerprint not in self.all_fingerprints:
                    island.add(ind)
                    self.all_fingerprints.add(ind.fingerprint)
            
            attempts += 1
    
    def get_simulation_candidates(self, batch_size: int = 10) -> List[Individual]:
        """
        Get unsimulated individuals for batch simulation across all islands.

        Returns individuals prioritized by expected quality, drawn evenly
        from each island so no single island starves the BRAIN budget.
        """
        # Collect unsimulated per island and round-robin to balance
        per_island_pools: List[List[Individual]] = [
            [i for i in island.individuals if not i.simulated]
            for island in self.islands
        ]

        priority_order = ["window", "operator_sub", "sign_flip", "add_wrapper", "structure"]

        def priority_key(ind: Individual) -> int:
            try:
                return priority_order.index(ind.mutation_type)
            except ValueError:
                return len(priority_order)

        for pool in per_island_pools:
            pool.sort(key=priority_key)

        out: List[Individual] = []
        # Round-robin pick from each island until batch full
        cursor = 0
        while len(out) < batch_size and any(per_island_pools):
            island_idx = cursor % len(per_island_pools)
            if per_island_pools[island_idx]:
                out.append(per_island_pools[island_idx].pop(0))
            cursor += 1
            # break once we've cycled through all islands without finding any
            if cursor > batch_size * len(self.islands) * 2:
                break

        return out[:batch_size]
    
    def update_individual(
        self,
        individual: Individual,
        sim_result: Dict[str, Any]
    ):
        """
        Update individual with simulation results.
        
        Args:
            individual: Individual to update
            sim_result: Simulation result dict
        """
        # Extract metrics
        is_stats = sim_result.get("is", sim_result.get("train", {})) or {}
        os_stats = sim_result.get("os", sim_result.get("test", {})) or {}
        
        individual.sharpe = float(is_stats.get("sharpe", is_stats.get("Sharpe", 0)) or 0)
        individual.fitness = float(is_stats.get("fitness", is_stats.get("Fitness", 0)) or 0)
        individual.turnover = float(is_stats.get("turnover", is_stats.get("Turnover", 0)) or 0)
        individual.os_sharpe = float(os_stats.get("sharpe", os_stats.get("Sharpe", 0)) or 0)
        
        individual.calculate_fitness()
        individual.simulated = True
        
        # Check if passed thresholds
        individual.passed = (
            individual.sharpe >= self.config.sharpe_threshold and
            individual.fitness >= self.config.fitness_threshold and
            individual.turnover <= self.config.turnover_threshold
        )
        
        self.simulations_used += 1
    
    def evolve(self):
        """
        Evolve every island to its next generation independently, then
        trigger periodic migration across islands when configured.
        """
        # Per-island evolution
        per_island_stats = []
        for island_id, island in enumerate(self.islands):
            self._evolve_island(island_id, island)
            per_island_stats.append(island.stats())

        # Migration: every `migration_interval` generations, exchange elite
        # individuals around a ring topology (top-K from island i replace
        # bottom-K of island (i+1) % N).
        gen = self.islands[0].generation
        if gen and gen % self.config.migration_interval == 0:
            self._migrate(gen)

        # Aggregate stats so external code that reads generation_stats keeps
        # working (uses union across islands).
        aggregate = {
            "generation": gen,
            "size": sum(s.get("size", 0) for s in per_island_stats),
            "simulated": sum(s.get("simulated", 0) for s in per_island_stats),
            "passed": sum(s.get("passed", 0) for s in per_island_stats),
            "max_fitness": max((s.get("max_fitness", 0) for s in per_island_stats), default=0),
            "avg_fitness": sum(
                s.get("avg_fitness", 0) for s in per_island_stats
            ) / max(1, len(per_island_stats)),
            "per_island": per_island_stats,
        }
        self.generation_stats.append(aggregate)

        logger.info(
            f"[GeneticOpt] Generation {gen} | islands={len(self.islands)} "
            f"total_pop={aggregate['size']} max_fitness={aggregate['max_fitness']:.3f}"
        )

    def _evolve_island(self, island_id: int, island: Population):
        """Run one generation of evolution within a single island."""
        island.generation += 1
        gen = island.generation

        simulated = [i for i in island.individuals if i.simulated]
        if not simulated:
            logger.debug(f"[GeneticOpt] island={island_id} no simulated individuals; skip")
            return

        simulated.sort(key=lambda x: x.overall_fitness, reverse=True)

        new_individuals: List[Individual] = []

        # Elite preservation (per-island)
        elite_count = max(1, int(len(simulated) * self.config.elite_ratio))
        new_individuals.extend(simulated[:elite_count])

        target = self._island_size
        rates = self.mutation_rates_per_island[island_id]

        attempts = 0
        max_attempts = target * 5
        while len(new_individuals) < target and attempts < max_attempts:
            attempts += 1
            parent = self._tournament_select(simulated)

            if random.random() < self.config.mutation_rate:
                offspring = self._mutate_with_rates(parent.expression, gen, rates, island_id)
                if offspring and offspring.fingerprint not in self.all_fingerprints:
                    new_individuals.append(offspring)
                    self.all_fingerprints.add(offspring.fingerprint)

            if random.random() < self.config.crossover_rate and len(simulated) > 1:
                parent2 = self._tournament_select(simulated)
                offspring = self._crossover(parent, parent2, gen)
                if offspring and offspring.fingerprint not in self.all_fingerprints:
                    offspring.island_id = island_id
                    new_individuals.append(offspring)
                    self.all_fingerprints.add(offspring.fingerprint)

        island.individuals = new_individuals
        island.fingerprints = {i.fingerprint for i in new_individuals}

    def _migrate(self, gen: int):
        """Ring-topology migration: top-K from island i → island (i+1)%N."""
        n = len(self.islands)
        if n < 2:
            return
        k = max(1, int(self._island_size * self.config.migration_ratio))
        migrants_to_send: List[List[Individual]] = []
        for island in self.islands:
            simulated = sorted(
                (i for i in island.individuals if i.simulated),
                key=lambda x: x.overall_fitness,
                reverse=True,
            )
            migrants_to_send.append(simulated[:k])

        for src in range(n):
            dst = (src + 1) % n
            island_dst = self.islands[dst]
            # Drop the bottom-K from destination (un-passed worst first)
            island_dst.individuals.sort(key=lambda x: x.overall_fitness)
            island_dst.individuals = island_dst.individuals[k:]
            island_dst.fingerprints = {i.fingerprint for i in island_dst.individuals}
            for migrant in migrants_to_send[src]:
                # Clone with new island id (avoid double-counting in source)
                clone = Individual(**{
                    **migrant.__dict__,
                    "island_id": dst,
                })
                if clone.fingerprint not in island_dst.fingerprints:
                    island_dst.add(clone)
        logger.info(
            f"[GeneticOpt] Migration at gen {gen} | k={k} per island, "
            f"ring topology over {n} islands"
        )

    def _mutate_with_rates(
        self,
        expression: str,
        generation: int,
        rates: Dict[str, float],
        island_id: int,
    ) -> Optional[Individual]:
        """Same as _mutate but uses per-island rates and stamps island_id."""
        offspring = self._mutate_using(expression, generation, rates)
        if offspring is not None:
            offspring.island_id = island_id
        return offspring

    def _mutate_using(
        self,
        expression: str,
        generation: int,
        rates: Dict[str, float],
    ) -> Optional[Individual]:
        mutation_funcs = [
            (mutate_operator_substitution, rates["operator_sub"]),
            (mutate_window_parameter, rates["window"]),
            (mutate_add_wrapper, rates["add_wrapper"]),
            (mutate_remove_wrapper, rates["remove_wrapper"]),
            (mutate_sign_flip, rates["sign_flip"]),
            (mutate_structure_modification, rates["structure"]),
        ]
        total_weight = sum(w for _, w in mutation_funcs)
        if total_weight <= 0:
            return None
        r = random.random() * total_weight
        cumulative = 0.0
        selected_func = mutation_funcs[0][0]
        for func, weight in mutation_funcs:
            cumulative += weight
            if r <= cumulative:
                selected_func = func
                break
        mutated, description = selected_func(expression)
        if mutated == expression or "no_" in description:
            return None
        return Individual(
            expression=mutated,
            generation=generation,
            parent_expression=expression,
            mutation_type=selected_func.__name__.replace("mutate_", ""),
            mutation_description=description,
        )
    
    def _tournament_select(self, candidates: List[Individual]) -> Individual:
        """Select individual through tournament selection."""
        tournament = random.sample(
            candidates,
            min(self.config.tournament_size, len(candidates))
        )
        return max(tournament, key=lambda x: x.overall_fitness)
    
    def _mutate(self, expression: str, generation: int) -> Optional[Individual]:
        """Apply random mutation to expression."""
        mutation_funcs = [
            (mutate_operator_substitution, self.mutation_rates["operator_sub"]),
            (mutate_window_parameter, self.mutation_rates["window"]),
            (mutate_add_wrapper, self.mutation_rates["add_wrapper"]),
            (mutate_remove_wrapper, self.mutation_rates["remove_wrapper"]),
            (mutate_sign_flip, self.mutation_rates["sign_flip"]),
            (mutate_structure_modification, self.mutation_rates["structure"]),
        ]
        
        # Weighted random selection
        total_weight = sum(w for _, w in mutation_funcs)
        r = random.random() * total_weight
        
        cumulative = 0
        selected_func = mutation_funcs[0][0]
        for func, weight in mutation_funcs:
            cumulative += weight
            if r <= cumulative:
                selected_func = func
                break
        
        mutated, description = selected_func(expression)
        
        if mutated == expression or "no_" in description:
            return None
        
        return Individual(
            expression=mutated,
            generation=generation,
            parent_expression=expression,
            mutation_type=selected_func.__name__.replace("mutate_", ""),
            mutation_description=description,
        )
    
    def _crossover(
        self,
        parent1: Individual,
        parent2: Individual,
        generation: int
    ) -> Optional[Individual]:
        """Create offspring through crossover."""
        child1, child2 = crossover_swap_inner(
            parent1.expression,
            parent2.expression
        )
        
        # Pick the child that's more different from parents
        if child1 != parent1.expression and child1 != parent2.expression:
            return Individual(
                expression=child1,
                generation=generation,
                parent_expression=parent1.expression,
                mutation_type="crossover",
                mutation_description=f"swap_inner with {parent2.fingerprint[:6]}",
            )
        
        return None
    
    def get_best_individuals(self, n: int = 5) -> List[Individual]:
        """Get top N individuals across ALL islands."""
        all_individuals: List[Individual] = []
        for island in self.islands:
            all_individuals.extend(island.individuals)
        all_individuals.sort(key=lambda x: x.overall_fitness, reverse=True)
        return all_individuals[:n]

    def get_passed_individuals(self) -> List[Individual]:
        """Get all individuals across islands that passed quality thresholds."""
        out: List[Individual] = []
        for island in self.islands:
            out.extend(island.get_passed())
        return out

    def get_optimization_report(self) -> Dict[str, Any]:
        """Generate optimization report aggregating all islands."""
        per_island = [
            {**island.stats(), "island_id": idx, "mutation_rates": self.mutation_rates_per_island[idx]}
            for idx, island in enumerate(self.islands)
        ]
        return {
            "generations": self.islands[0].generation if self.islands else 0,
            "simulations_used": self.simulations_used,
            "num_islands": len(self.islands),
            "per_island_stats": per_island,
            "generation_history": self.generation_stats,
            "best_individuals": [i.to_dict() for i in self.get_best_individuals(5)],
            "passed_count": len(self.get_passed_individuals()),
        }

    def adapt_mutation_rates(self):
        """
        Adapt per-island mutation rates based on each island's success history.

        Per plan R5 修订项 1: islands evolve their own mutation biases rather
        than being assigned hand-crafted ones at initialization. This is the
        OpenEvolve-style "specialization through evolution" mechanism.
        """
        if len(self.generation_stats) < 2:
            return

        for island_id, island in enumerate(self.islands):
            mutation_success: Dict[str, int] = defaultdict(int)
            mutation_total: Dict[str, int] = defaultdict(int)
            for ind in island.individuals:
                if ind.simulated:
                    mutation_total[ind.mutation_type] += 1
                    if ind.passed or ind.overall_fitness > 0.5:
                        mutation_success[ind.mutation_type] += 1

            rates = self.mutation_rates_per_island[island_id]
            for mut_type in rates:
                total = mutation_total.get(mut_type, 0)
                success = mutation_success.get(mut_type, 0)
                if total > 3:
                    success_rate = success / total
                    current = rates[mut_type]
                    rates[mut_type] = 0.7 * current + 0.3 * max(0.05, success_rate)


# =============================================================================
# Helper Functions
# =============================================================================

async def run_genetic_optimization(
    seed_expression: str,
    seed_metrics: Dict[str, float],
    simulate_func,  # async function(expression) -> Dict
    config: OptimizationConfig = None,
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 0,
    neutralization: str = "INDUSTRY",
) -> Dict[str, Any]:
    """
    Run complete genetic optimization on a seed expression.
    
    Args:
        seed_expression: Starting alpha expression
        seed_metrics: Metrics from seed simulation
        simulate_func: Async function to simulate an expression
        config: Optimization configuration
        region, universe, delay, decay, neutralization: Simulation settings
    
    Returns:
        Optimization result dictionary
    """
    config = config or OptimizationConfig()
    optimizer = GeneticOptimizer(config)
    
    # Initialize
    optimizer.initialize(seed_expression, seed_metrics)
    
    # Evolution loop
    for gen in range(config.generations):
        # Get candidates to simulate
        candidates = optimizer.get_simulation_candidates(batch_size=10)
        
        if not candidates:
            logger.info(f"[GeneticOpt] No more candidates at generation {gen}")
            break
        
        # Check budget
        if optimizer.simulations_used >= config.max_simulations:
            logger.info(f"[GeneticOpt] Simulation budget exhausted at {optimizer.simulations_used}")
            break
        
        # Simulate candidates
        for ind in candidates:
            try:
                result = await simulate_func(
                    expression=ind.expression,
                    region=region,
                    universe=universe,
                    delay=delay,
                    decay=decay,
                    neutralization=neutralization,
                )
                
                if result.get("success"):
                    optimizer.update_individual(ind, result)
                else:
                    ind.simulated = True  # Mark as tried
                
            except Exception as e:
                logger.warning(f"[GeneticOpt] Simulation failed: {e}")
                ind.simulated = True
        
        # Evolve
        optimizer.evolve()
        
        # Adapt mutation rates
        optimizer.adapt_mutation_rates()
    
    # Generate report
    report = optimizer.get_optimization_report()
    
    # Add best variants for downstream use
    best = optimizer.get_best_individuals(10)
    report["best_expressions"] = [i.expression for i in best]
    
    passed = optimizer.get_passed_individuals()
    report["passed_expressions"] = [i.expression for i in passed]
    
    logger.info(
        f"[GeneticOpt] Complete | generations={report['generations']} "
        f"simulations={report['simulations_used']} passed={report['passed_count']}"
    )
    
    return report
