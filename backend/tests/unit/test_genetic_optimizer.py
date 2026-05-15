"""
Unit tests for backend/genetic_optimizer.py

Covers data structures (Individual / Population), the pure-function mutation
and crossover operators, and the island-model GeneticOptimizer lifecycle
(initialize → simulate → evolve → migrate → report).

Run with: pytest backend/tests/unit/test_genetic_optimizer.py -v
"""

import random

import pytest

from backend.genetic_optimizer import (
    Individual,
    Population,
    OptimizationConfig,
    GeneticOptimizer,
    compute_overall_fitness,
    _metrics_from_result,
    mutate_operator_substitution,
    mutate_window_parameter,
    mutate_add_wrapper,
    mutate_remove_wrapper,
    mutate_sign_flip,
    mutate_structure_modification,
    crossover_swap_inner,
    crossover_combine,
    run_genetic_optimization,
    OPERATOR_GROUPS,
    WINDOW_VALUES,
)


@pytest.fixture(autouse=True)
def _seed_rng():
    """Make the randomized operators deterministic per test."""
    random.seed(20260515)
    yield


# =============================================================================
# Individual
# =============================================================================

class TestIndividual:

    def test_fingerprint_stable_and_distinct(self):
        a = Individual(expression="ts_rank(close, 20)")
        b = Individual(expression="ts_rank(close, 20)")
        c = Individual(expression="ts_rank(close, 21)")

        assert a.fingerprint == b.fingerprint
        assert a.fingerprint != c.fingerprint
        assert len(a.fingerprint) == 12

    def test_calculate_fitness_default_weights(self):
        ind = Individual(expression="x", sharpe=2.0, fitness=1.5,
                         turnover=0.3, os_sharpe=1.5)
        ind.calculate_fitness()
        # all four components saturate at 1.0 except turnover (0.7)
        # 0.50*1 + 0.20*1 + 0.15*0.7 + 0.15*1 = 0.955
        assert ind.overall_fitness == pytest.approx(0.955)

    def test_calculate_fitness_zero_metrics(self):
        ind = Individual(expression="x")
        ind.calculate_fitness()
        # turnover 0.0 < 1.0 -> turnover_score = 1.0, weight 0.15
        assert ind.overall_fitness == pytest.approx(0.15)

    def test_calculate_fitness_high_turnover_penalised(self):
        ind = Individual(expression="x", sharpe=2.0, turnover=1.5)
        ind.calculate_fitness()
        # turnover >= 1.0 -> turnover_score = 0
        assert ind.overall_fitness == pytest.approx(0.50)

    def test_to_dict_rounds_and_includes_keys(self):
        ind = Individual(expression="x", sharpe=1.23456, mutation_type="window")
        d = ind.to_dict()
        assert d["expression"] == "x"
        assert d["sharpe"] == 1.2346
        assert d["mutation_type"] == "window"
        assert set(d) >= {"expression", "generation", "sharpe", "fitness",
                          "turnover", "os_sharpe", "overall_fitness",
                          "mutation_type", "mutation_description", "passed",
                          "grid_confirmed", "fidelity_count", "fitness_dispersion"}
        assert d["grid_confirmed"] is False
        assert d["fidelity_count"] == 1
        assert d["fitness_dispersion"] == 0.0


# =============================================================================
# Population
# =============================================================================

class TestPopulation:

    def test_add_dedup(self):
        pop = Population()
        assert pop.add(Individual(expression="a")) is True
        assert pop.add(Individual(expression="a")) is False  # duplicate
        assert pop.add(Individual(expression="b")) is True
        assert len(pop.individuals) == 2

    def test_get_best_orders_by_overall_fitness(self):
        pop = Population()
        for expr, of in [("a", 0.1), ("b", 0.9), ("c", 0.5)]:
            ind = Individual(expression=expr)
            ind.overall_fitness = of
            pop.add(ind)
        best = pop.get_best(2)
        assert [i.expression for i in best] == ["b", "c"]

    def test_get_passed(self):
        pop = Population()
        p = Individual(expression="p", passed=True)
        f = Individual(expression="f", passed=False)
        pop.add(p)
        pop.add(f)
        assert pop.get_passed() == [p]

    def test_stats_empty(self):
        assert Population().stats() == {"size": 0}

    def test_stats_populated(self):
        pop = Population(generation=3)
        i1 = Individual(expression="a", passed=True, simulated=True)
        i1.overall_fitness = 0.8
        i2 = Individual(expression="b", simulated=True)
        i2.overall_fitness = 0.4
        i3 = Individual(expression="c", simulated=False)  # not simulated
        for i in (i1, i2, i3):
            pop.add(i)
        stats = pop.stats()
        assert stats["size"] == 3
        assert stats["simulated"] == 2
        assert stats["passed"] == 1
        assert stats["avg_fitness"] == pytest.approx(0.6)
        assert stats["max_fitness"] == pytest.approx(0.8)
        assert stats["generation"] == 3


# =============================================================================
# Mutation operators (pure functions)
# =============================================================================

class TestMutationOperators:

    def test_operator_substitution_swaps_within_group(self):
        # force a deterministic pick
        random.seed(1)
        mutated, desc = mutate_operator_substitution("ts_mean(close, 20)")
        assert mutated != "ts_mean(close, 20)"
        assert desc.startswith("operator_sub:")
        # new operator must be from the same semantic group
        new_op = mutated.split("(")[0]
        assert new_op in OPERATOR_GROUPS["aggregation"]

    def test_operator_substitution_no_func(self):
        mutated, desc = mutate_operator_substitution("close")
        assert mutated == "close"
        assert desc == "no_change"

    def test_window_parameter_changes_number(self):
        random.seed(2)
        mutated, desc = mutate_window_parameter("ts_rank(close, 20)")
        assert desc.startswith("window:")
        # extract the new window
        new_window = int(mutated.split(",")[1].strip().rstrip(")"))
        assert new_window in WINDOW_VALUES
        assert new_window != 20

    def test_window_parameter_no_params(self):
        mutated, desc = mutate_window_parameter("rank(close)")
        assert mutated == "rank(close)"
        assert desc == "no_window_params"

    def test_add_wrapper(self):
        random.seed(3)
        mutated, desc = mutate_add_wrapper("close")
        assert desc.startswith("add_wrapper:")
        assert mutated != "close"
        assert "close" in mutated

    def test_add_wrapper_avoids_double_wrap(self):
        # rank(...) already starts with rank( -> must not double wrap with rank
        for seed in range(20):
            random.seed(seed)
            mutated, desc = mutate_add_wrapper("rank(close)")
            if desc == "already_wrapped":
                assert mutated == "rank(close)"
            else:
                assert not mutated.startswith("rank(rank(")

    def test_remove_wrapper(self):
        mutated, desc = mutate_remove_wrapper("rank(ts_delta(close, 5))")
        assert mutated == "ts_delta(close, 5)"
        assert desc == "remove_wrapper: rank"

    def test_remove_wrapper_none(self):
        mutated, desc = mutate_remove_wrapper("close")
        assert mutated == "close"
        assert desc == "no_wrapper_to_remove"

    def test_sign_flip_add_and_remove(self):
        mutated, desc = mutate_sign_flip("close")
        assert mutated == "-1 * (close)"
        assert desc == "add_negative"

        back, desc2 = mutate_sign_flip(mutated)
        assert back == "(close)"
        assert desc2 == "remove_negative"

    def test_structure_modification(self):
        random.seed(4)
        mutated, desc = mutate_structure_modification("close")
        assert desc.startswith("structure:")
        assert "close" in mutated and mutated != "close"


# =============================================================================
# Crossover operators
# =============================================================================

class TestCrossover:

    def test_swap_inner(self):
        c1, c2 = crossover_swap_inner("rank(close)", "ts_mean(volume, 5)")
        assert c1 == "rank(volume, 5)"
        assert c2 == "ts_mean(close)"

    def test_swap_inner_non_wrapped_returns_unchanged(self):
        c1, c2 = crossover_swap_inner("close", "ts_mean(volume, 5)")
        assert (c1, c2) == ("close", "ts_mean(volume, 5)")

    def test_combine_produces_valid_op(self):
        random.seed(5)
        combined = crossover_combine("a", "b")
        assert any(combined.startswith(p) for p in ("add(", "multiply(", "divide("))
        assert "a" in combined and "b" in combined


# =============================================================================
# GeneticOptimizer — island model lifecycle
# =============================================================================

SEED_EXPR = "ts_rank(ts_delta(close, 5), 20)"
SEED_METRICS = {"sharpe": 1.8, "fitness": 1.2, "turnover": 0.4, "os_sharpe": 1.1}


def _good_sim_result():
    return {
        "success": True,
        "is": {"sharpe": 2.0, "fitness": 1.5, "turnover": 0.3},
        "os": {"sharpe": 1.5},
    }


class TestGeneticOptimizerInit:

    def test_islands_created(self):
        cfg = OptimizationConfig(num_islands=4, population_size=48)
        opt = GeneticOptimizer(cfg)
        assert len(opt.islands) == 4
        assert opt._island_size == 12
        # backwards-compat aliases point at island 0
        assert opt.population is opt.islands[0]
        assert opt.mutation_rates is opt.mutation_rates_per_island[0]

    def test_island_size_floor(self):
        # population_size // num_islands could be tiny -> floored at 8
        opt = GeneticOptimizer(OptimizationConfig(num_islands=10, population_size=10))
        assert opt._island_size == 8

    def test_initialize_seeds_every_island(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=3, population_size=30))
        opt.initialize(SEED_EXPR, SEED_METRICS)

        for island_id, island in enumerate(opt.islands):
            assert len(island.individuals) > 1  # seed + mutations
            seed = island.individuals[0]
            assert seed.expression == SEED_EXPR
            assert seed.mutation_type == "seed"
            assert seed.simulated is True
            assert seed.island_id == island_id
            assert seed.overall_fitness > 0

    def test_initialize_global_dedup(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=3, population_size=30))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        # the seed is intentionally replicated into every island, but the
        # generated mutations are globally deduped against all_fingerprints
        mutation_fps = [
            i.fingerprint
            for isl in opt.islands
            for i in isl.individuals
            if i.mutation_type != "seed"
        ]
        assert len(mutation_fps) == len(set(mutation_fps))
        # each island internally never holds a duplicate
        for isl in opt.islands:
            fps = [i.fingerprint for i in isl.individuals]
            assert len(fps) == len(set(fps))


class TestGeneticOptimizerSimulation:

    def test_get_simulation_candidates_round_robin(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=4, population_size=48))
        opt.initialize(SEED_EXPR, SEED_METRICS)

        candidates = opt.get_simulation_candidates(batch_size=8)
        assert 0 < len(candidates) <= 8
        # none of the returned candidates is already simulated
        assert all(not c.simulated for c in candidates)

    def test_update_individual_sets_metrics_and_pass(self):
        opt = GeneticOptimizer(OptimizationConfig())
        opt.initialize(SEED_EXPR, SEED_METRICS)
        ind = opt.get_simulation_candidates(batch_size=1)[0]

        opt.update_individual(ind, _good_sim_result())

        assert ind.simulated is True
        assert ind.sharpe == 2.0
        assert ind.fitness == 1.5
        assert ind.turnover == 0.3
        assert ind.os_sharpe == 1.5
        assert ind.passed is True
        assert ind.overall_fitness > 0
        assert opt.simulations_used == 1

    def test_update_individual_failing_thresholds(self):
        opt = GeneticOptimizer(OptimizationConfig())
        opt.initialize(SEED_EXPR, SEED_METRICS)
        ind = opt.get_simulation_candidates(batch_size=1)[0]

        opt.update_individual(ind, {
            "is": {"sharpe": 0.5, "fitness": 0.2, "turnover": 0.9},
            "os": {"sharpe": 0.1},
        })
        assert ind.simulated is True
        assert ind.passed is False

    def test_update_individual_sets_fidelity_fields(self):
        """update_individual stamps search-fidelity bookkeeping on the individual."""
        opt = GeneticOptimizer(OptimizationConfig())
        opt.initialize(SEED_EXPR, SEED_METRICS)
        ind = opt.get_simulation_candidates(batch_size=1)[0]

        opt.update_individual(ind, _good_sim_result())

        assert ind.fidelity_count == 1
        assert ind.grid_confirmed is False
        assert ind.fitness_dispersion == 0.0

    def test_update_individual_train_test_keys(self):
        """Accepts the train/test alias instead of is/os."""
        opt = GeneticOptimizer(OptimizationConfig())
        opt.initialize(SEED_EXPR, SEED_METRICS)
        ind = opt.get_simulation_candidates(batch_size=1)[0]

        opt.update_individual(ind, {
            "train": {"Sharpe": 1.9, "Fitness": 1.1, "Turnover": 0.5},
            "test": {"Sharpe": 1.0},
        })
        assert ind.sharpe == 1.9
        assert ind.fitness == 1.1
        assert ind.passed is True  # 1.9>=1.25, 1.1>=1.0, 0.5<=0.7


class TestGeneticOptimizerEvolve:

    def _simulate_all(self, opt, result_fn=_good_sim_result):
        """Simulate every unsimulated individual across all islands."""
        for island in opt.islands:
            for ind in island.individuals:
                if not ind.simulated:
                    opt.update_individual(ind, result_fn())

    def test_evolve_advances_generation(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=2, population_size=20))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        self._simulate_all(opt)

        opt.evolve()

        assert opt.islands[0].generation == 1
        assert len(opt.generation_stats) == 1
        agg = opt.generation_stats[0]
        assert agg["generation"] == 1
        assert "per_island" in agg
        assert len(agg["per_island"]) == 2

    def test_evolve_preserves_elite(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=2, population_size=20))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        self._simulate_all(opt)

        best_before = opt.get_best_individuals(1)[0]
        opt.evolve()
        # the elite expression survives into the next generation
        survivors = {i.expression for isl in opt.islands for i in isl.individuals}
        assert best_before.expression in survivors

    def test_evolve_no_simulated_is_safe(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=2, population_size=20))
        opt.initialize(SEED_EXPR, seed_metrics=None)  # seeds not simulated
        # should not raise even though nothing is simulated
        opt.evolve()
        assert opt.islands[0].generation == 1

    def test_migration_runs_on_interval(self):
        cfg = OptimizationConfig(num_islands=3, population_size=30,
                                 migration_interval=1)
        opt = GeneticOptimizer(cfg)
        opt.initialize(SEED_EXPR, SEED_METRICS)
        self._simulate_all(opt)
        # migration_interval=1 -> migration triggers after the first evolve
        opt.evolve()
        # islands still well-formed after migration
        assert all(len(isl.individuals) > 0 for isl in opt.islands)
        for isl in opt.islands:
            fps = [i.fingerprint for i in isl.individuals]
            assert len(fps) == len(set(fps))  # no intra-island dups

    def test_adapt_mutation_rates_requires_history(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=2, population_size=20))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        before = [dict(r) for r in opt.mutation_rates_per_island]
        opt.adapt_mutation_rates()  # < 2 generation_stats -> no-op
        assert [dict(r) for r in opt.mutation_rates_per_island] == before


class TestGeneticOptimizerReport:

    def test_get_best_and_passed_across_islands(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=3, population_size=30))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        for island in opt.islands:
            for ind in island.individuals:
                if not ind.simulated:
                    opt.update_individual(ind, _good_sim_result())

        best = opt.get_best_individuals(5)
        assert len(best) == 5
        # sorted descending by overall_fitness
        assert all(best[i].overall_fitness >= best[i + 1].overall_fitness
                   for i in range(len(best) - 1))

        passed = opt.get_passed_individuals()
        assert len(passed) > 0
        assert all(p.passed for p in passed)

    def test_get_optimization_report_shape(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=2, population_size=20))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        report = opt.get_optimization_report()
        assert report["num_islands"] == 2
        assert len(report["per_island_stats"]) == 2
        assert "best_individuals" in report
        assert "passed_count" in report
        assert report["simulations_used"] == 0


# =============================================================================
# run_genetic_optimization — end-to-end async helper
# =============================================================================

class TestRunGeneticOptimization:

    @pytest.mark.asyncio
    async def test_end_to_end_with_mock_simulate(self):
        call_count = {"n": 0}

        async def fake_simulate(expression, **kwargs):
            call_count["n"] += 1
            # deterministic "good" result so some individuals pass
            return {
                "success": True,
                "is": {"sharpe": 1.6, "fitness": 1.1, "turnover": 0.35},
                "os": {"sharpe": 1.0},
            }

        cfg = OptimizationConfig(num_islands=2, population_size=20,
                                 generations=2, max_simulations=30)
        report = await run_genetic_optimization(
            seed_expression=SEED_EXPR,
            seed_metrics=SEED_METRICS,
            simulate_func=fake_simulate,
            config=cfg,
        )

        assert call_count["n"] > 0
        assert report["generations"] >= 1
        assert "best_expressions" in report
        assert "passed_expressions" in report
        assert report["simulations_used"] == call_count["n"]

    @pytest.mark.asyncio
    async def test_simulate_failure_marks_tried(self):
        async def failing_simulate(expression, **kwargs):
            raise RuntimeError("BRAIN down")

        cfg = OptimizationConfig(num_islands=2, population_size=20,
                                 generations=1, max_simulations=30)
        report = await run_genetic_optimization(
            seed_expression=SEED_EXPR,
            seed_metrics=SEED_METRICS,
            simulate_func=failing_simulate,
            config=cfg,
        )
        # no successful simulations recorded, but the run completes cleanly
        assert report["simulations_used"] == 0
        assert report["passed_count"] == 0

    @pytest.mark.asyncio
    async def test_unsuccessful_result_not_counted(self):
        async def unsuccessful_simulate(expression, **kwargs):
            return {"success": False, "error": "compile error"}

        cfg = OptimizationConfig(num_islands=2, population_size=20,
                                 generations=1, max_simulations=30)
        report = await run_genetic_optimization(
            seed_expression=SEED_EXPR,
            seed_metrics=SEED_METRICS,
            simulate_func=unsuccessful_simulate,
            config=cfg,
        )
        assert report["simulations_used"] == 0


# =============================================================================
# Helper pure-functions
# =============================================================================

class TestHelperFunctions:

    def test_metrics_from_result_is_os_keys(self):
        r = {"is": {"sharpe": 1.5, "fitness": 1.1, "turnover": 0.4},
             "os": {"sharpe": 0.9}}
        m = _metrics_from_result(r)
        assert m == {"sharpe": 1.5, "fitness": 1.1, "turnover": 0.4, "os_sharpe": 0.9}

    def test_metrics_from_result_train_test_aliases(self):
        r = {"train": {"Sharpe": 2.0, "Fitness": 1.3, "Turnover": 0.5},
             "test":  {"Sharpe": 1.2}}
        m = _metrics_from_result(r)
        assert m["sharpe"] == 2.0 and m["os_sharpe"] == 1.2

    def test_metrics_from_result_missing_fields_zero(self):
        m = _metrics_from_result({})
        assert all(v == 0.0 for v in m.values())

    def test_compute_overall_fitness_default_weights(self):
        # sharpe=2 → 1.0, fitness=1.5 → 1.0, turnover=0.3 → 0.7, os_sharpe=1.5 → 1.0
        # 0.50*1 + 0.20*1 + 0.15*0.7 + 0.15*1 = 0.955
        ov = compute_overall_fitness(2.0, 1.5, 0.3, 1.5)
        assert ov == pytest.approx(0.955)

    def test_compute_overall_fitness_os_consistency_penalty(self):
        # same inputs but apply_os_consistency=True, os/sharpe = 0.5 → factor 0.75
        ov_no = compute_overall_fitness(2.0, 1.5, 0.3, 1.0, apply_os_consistency=False)
        ov_yes = compute_overall_fitness(2.0, 1.5, 0.3, 1.0, apply_os_consistency=True)
        assert ov_yes < ov_no  # penalty reduces score
        # consistency factor = 0.5 + 0.5*(1.0/2.0) = 0.75
        assert ov_yes == pytest.approx(ov_no * 0.75)


# =============================================================================
# Tiered-fidelity: get_promotion_candidates
# =============================================================================

class TestGetPromotionCandidates:

    def _make_opt_with_simulated(self, n_islands=2, good_result=True):
        """Initialise optimizer and single-sim all individuals."""
        opt = GeneticOptimizer(OptimizationConfig(num_islands=n_islands,
                                                  population_size=n_islands * 10))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        result = _good_sim_result() if good_result else {
            "is": {"sharpe": 0.3, "fitness": 0.1, "turnover": 0.9}, "os": {"sharpe": 0.0}
        }
        for island in opt.islands:
            for ind in island.individuals:
                if not ind.simulated:
                    opt.update_individual(ind, result)
        return opt

    def test_returns_top_n_per_island(self):
        opt = self._make_opt_with_simulated(n_islands=3)
        cfg = opt.config
        candidates = opt.get_promotion_candidates()
        # total ≤ num_islands × promotion_pool_size
        assert len(candidates) <= 3 * cfg.promotion_pool_size
        # all must be simulated and not yet confirmed
        assert all(c.simulated for c in candidates)
        assert all(not c.grid_confirmed for c in candidates)

    def test_excludes_already_confirmed(self):
        opt = self._make_opt_with_simulated(n_islands=2)
        # manually mark first island's top individual as confirmed
        top_before = opt.get_promotion_candidates()
        if top_before:
            top_before[0].grid_confirmed = True

        after = opt.get_promotion_candidates()
        confirmed_fps = {top_before[0].fingerprint} if top_before else set()
        assert all(c.fingerprint not in confirmed_fps for c in after)

    def test_excludes_unsimulated(self):
        opt = GeneticOptimizer(OptimizationConfig(num_islands=2, population_size=20))
        opt.initialize(SEED_EXPR, SEED_METRICS)
        # nothing simulated except the seed
        candidates = opt.get_promotion_candidates()
        assert all(c.simulated for c in candidates)


# =============================================================================
# Tiered-fidelity: confirm_individual_grid
# =============================================================================

class TestConfirmIndividualGrid:

    def _make_result(self, sharpe, fitness=1.0, turnover=0.4, os_sharpe=0.8):
        return {
            "is": {"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
            "os": {"sharpe": os_sharpe},
        }

    def test_picks_median_overall_config(self):
        """Median-overall (lower-middle) config's real metrics are used — not a blend."""
        opt = GeneticOptimizer(OptimizationConfig())
        ind = Individual(expression="x", simulated=True, sharpe=2.0, fitness=1.5,
                         turnover=0.3, os_sharpe=1.5, overall_fitness=0.9)

        # Three configs: overalls will differ; median should be the middle config
        r_low  = self._make_result(sharpe=0.5)   # worst
        r_mid  = self._make_result(sharpe=1.3)   # median
        r_high = self._make_result(sharpe=2.5)   # best

        opt.confirm_individual_grid(ind, [r_low, r_mid, r_high])

        # After confirmation, ind metrics == median config's real values
        assert ind.sharpe == pytest.approx(1.3)
        assert ind.fitness == pytest.approx(1.0)
        assert ind.turnover == pytest.approx(0.4)
        assert ind.grid_confirmed is True
        assert ind.fidelity_count == 3

    def test_passed_based_on_real_median_config(self):
        """A lucky high-sharpe base run is rejected if median config fails thresholds."""
        opt = GeneticOptimizer(OptimizationConfig(
            sharpe_threshold=1.25, fitness_threshold=1.0
        ))
        ind = Individual(expression="x", simulated=True, sharpe=2.0, fitness=1.5,
                         turnover=0.3, os_sharpe=1.5, overall_fitness=0.95)
        ind.passed = True  # 'passed' from single-run luck

        # Two sub-universe results: both sub-threshold → median is sub-threshold
        r1 = self._make_result(sharpe=0.8, fitness=0.7)
        r2 = self._make_result(sharpe=0.6, fitness=0.5)

        opt.confirm_individual_grid(ind, [r1, r2])

        assert ind.passed is False          # overfit caught
        assert ind.grid_confirmed is True

    def test_fitness_dispersion_uniform_is_zero(self):
        """If all grid configs give identical overall_fitness, dispersion ≈ 0."""
        opt = GeneticOptimizer(OptimizationConfig())
        ind = Individual(expression="x", simulated=True)

        identical = self._make_result(sharpe=1.5, fitness=1.1, turnover=0.4,
                                      os_sharpe=0.9)
        opt.confirm_individual_grid(ind, [identical, identical, identical])

        assert ind.fitness_dispersion == pytest.approx(0.0, abs=1e-9)

    def test_fitness_dispersion_nonzero_when_configs_differ(self):
        opt = GeneticOptimizer(OptimizationConfig())
        ind = Individual(expression="x", simulated=True)

        opt.confirm_individual_grid(ind, [
            self._make_result(sharpe=2.5),   # high overall
            self._make_result(sharpe=0.3),   # low overall
        ])

        assert ind.fitness_dispersion > 0

    def test_simulations_used_incremented_by_grid_length(self):
        opt = GeneticOptimizer(OptimizationConfig())
        opt.initialize(SEED_EXPR, SEED_METRICS)
        before = opt.simulations_used
        ind = Individual(expression="x", simulated=True)

        opt.confirm_individual_grid(ind, [
            self._make_result(1.3),
            self._make_result(1.1),
        ])

        assert opt.simulations_used == before + 2

    def test_single_result_no_dispersion(self):
        """K=1 grid (edge case): confirmed, fidelity_count=1, dispersion=0."""
        opt = GeneticOptimizer(OptimizationConfig())
        ind = Individual(expression="x", simulated=True)

        opt.confirm_individual_grid(ind, [self._make_result(sharpe=1.6)])

        assert ind.grid_confirmed is True
        assert ind.fidelity_count == 1
        assert ind.fitness_dispersion == 0.0


# =============================================================================
# run_genetic_optimization — tiered-fidelity end-to-end
# =============================================================================

class TestRunGeneticOptimizationTieredFidelity:

    @pytest.mark.asyncio
    async def test_simulations_used_matches_call_count(self):
        """simulations_used accounts for both single-run and grid calls."""
        call_count = {"n": 0}

        async def fake_simulate(expression, **kwargs):
            call_count["n"] += 1
            return {
                "success": True,
                "is": {"sharpe": 1.6, "fitness": 1.1, "turnover": 0.35},
                "os": {"sharpe": 1.0},
            }

        cfg = OptimizationConfig(num_islands=2, population_size=20,
                                 generations=2, max_simulations=120,
                                 fidelity_grid=[{"universe": "TOP1000"},
                                                {"universe": "TOP500"}])
        report = await run_genetic_optimization(
            seed_expression=SEED_EXPR,
            seed_metrics=SEED_METRICS,
            simulate_func=fake_simulate,
            config=cfg,
        )

        assert report["simulations_used"] == call_count["n"]
        assert report["simulations_used"] > 0
        assert "best_expressions" in report

    @pytest.mark.asyncio
    async def test_grid_confirmed_individuals_exist(self):
        """After a run with fidelity_grid, some individuals carry grid_confirmed=True."""
        async def fake_simulate(expression, **kwargs):
            return {
                "success": True,
                "is": {"sharpe": 1.5, "fitness": 1.0, "turnover": 0.4},
                "os": {"sharpe": 0.9},
            }

        cfg = OptimizationConfig(num_islands=2, population_size=20,
                                 generations=2, max_simulations=120,
                                 fidelity_grid=[{"universe": "TOP1000"},
                                                {"universe": "TOP500"}])
        report = await run_genetic_optimization(
            seed_expression=SEED_EXPR,
            seed_metrics=SEED_METRICS,
            simulate_func=fake_simulate,
            config=cfg,
        )

        # best_individuals in report should include at least one grid_confirmed entry
        confirmed = [b for b in report["best_individuals"] if b.get("grid_confirmed")]
        assert len(confirmed) > 0

    @pytest.mark.asyncio
    async def test_lucky_individual_excluded_after_grid_confirmation(self):
        """
        Anti-overfit behaviour: an individual that scores high on the base universe
        but collapses on sub-universes should not appear in passed_expressions.
        """
        lucky_expr = "lucky_alpha(close, 20)"  # unique so we can track it

        async def fake_simulate(expression, **kwargs):
            universe = kwargs.get("universe", "TOP3000")
            if expression == lucky_expr and universe != "TOP3000":
                # Terrible on sub-universes
                return {
                    "success": True,
                    "is": {"sharpe": 0.2, "fitness": 0.1, "turnover": 0.9},
                    "os": {"sharpe": 0.0},
                }
            # All other expressions / base universe: good
            return {
                "success": True,
                "is": {"sharpe": 1.6, "fitness": 1.1, "turnover": 0.35},
                "os": {"sharpe": 1.0},
            }

        cfg = OptimizationConfig(
            num_islands=1, population_size=10, generations=2,
            max_simulations=100,
            sharpe_threshold=1.25, fitness_threshold=1.0,
            fidelity_grid=[{"universe": "TOP1000"}, {"universe": "TOP500"}],
        )
        # Inject lucky_expr as seed so it starts in the population
        report = await run_genetic_optimization(
            seed_expression=lucky_expr,
            seed_metrics={"sharpe": 2.5, "fitness": 1.8, "turnover": 0.2,
                          "os_sharpe": 2.0},
            simulate_func=fake_simulate,
            config=cfg,
        )

        assert lucky_expr not in report.get("passed_expressions", [])

    @pytest.mark.asyncio
    async def test_empty_fidelity_grid_disables_promotion(self):
        """fidelity_grid=[] → legacy single-fidelity: no grid sims, no grid_confirmed."""
        call_count = {"n": 0}

        async def fake_simulate(expression, **kwargs):
            call_count["n"] += 1
            return {"success": True,
                    "is": {"sharpe": 1.6, "fitness": 1.1, "turnover": 0.35},
                    "os": {"sharpe": 1.0}}

        cfg = OptimizationConfig(num_islands=2, population_size=20,
                                 generations=2, max_simulations=60,
                                 fidelity_grid=[])
        report = await run_genetic_optimization(
            seed_expression=SEED_EXPR,
            seed_metrics=SEED_METRICS,
            simulate_func=fake_simulate,
            config=cfg,
        )

        # simulations_used == call_count (no grid overhead)
        assert report["simulations_used"] == call_count["n"]
        # no individual should be grid_confirmed when grid is disabled
        confirmed = [b for b in report["best_individuals"] if b.get("grid_confirmed")]
        assert len(confirmed) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
