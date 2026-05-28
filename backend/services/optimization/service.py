"""OptimizationService — the cycle orchestrator (Layer 3).

The single entry point Layer 4 triggers (beat in Stage A; pipeline-hook in
Stage C) call. Composes:

    open_cycle → generator.generate
              → simulator.run_batch
              → selector.pick
              → persister.save
              → submit_policy.decide
              → record_persist + record_submit
              → finish_cycle
              → feedback.on_winner (Stage A no-op)

The signature ``run_one_cycle(parent_alpha, trigger_source, budget)`` is
frozen for the whole A→C arc — Stage B/C swap implementations of the
injected protocols behind it without changing this call.

Exceptions mid-cycle stamp ``optimization_runs.error`` via
``finish_cycle(opt_run_id, error=…)`` and re-raise so the caller can
decide whether to abort the beat task or move to the next candidate.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §3 + §6.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.services.optimization.protocols import (
    KnowledgeFeedback,
    OptimizationRunRepository,
    Persister,
    Simulator,
    SubmitPolicy,
    VariantGenerator,
    WinnerSelector,
)


logger = logging.getLogger("optimization.service")


class NoOpKnowledgeFeedback:
    """Stage A + B feedback impl — does nothing. Stage C swaps in a real
    RAG-write impl. Lives here (not its own file) because it's two lines
    and tying it to the service module keeps the no-op visible to anyone
    reading the orchestrator."""

    async def on_winner(self, alpha: Any) -> None:
        return None


class OptimizationService:
    """The orchestrator. All collaborators injected — never self-constructed —
    so tests can swap in fakes (MockBrainAdapter, in-memory repository, etc.)
    per :file:`backend/CODE_STATUS.md` DI rule."""

    def __init__(
        self,
        *,
        generator: VariantGenerator,
        simulator: Simulator,
        winner_selector: WinnerSelector,
        persister: Persister,
        submit_policy: SubmitPolicy,
        repository: OptimizationRunRepository,
        feedback: Optional[KnowledgeFeedback] = None,
    ):
        self.generator = generator
        self.simulator = simulator
        self.winner_selector = winner_selector
        self.persister = persister
        self.submit_policy = submit_policy
        self.repository = repository
        self.feedback = feedback or NoOpKnowledgeFeedback()

    async def run_one_cycle(
        self,
        parent_alpha: Any,
        *,
        trigger_source: str,
        budget: int,
    ) -> Dict[str, Any]:
        """Run one optimization cycle against ``parent_alpha``.

        ``budget`` is the per-cycle BRAIN sim cap. Generator may emit more
        variants than budget; Simulator truncates to ``variants[:budget]``.

        Returns a summary dict suitable for logging / telemetry payload.
        Raises on any uncaught exception in the cycle pipeline — the
        ``optimization_runs.error`` field will already be stamped via
        finish_cycle in the except block before the raise.
        """
        # Parent alpha attribute pulls — fail fast if model shape changed
        parent_id = int(parent_alpha.id)
        delay = int(getattr(parent_alpha, "delay", None) or 1)

        opt_run_id = await self.repository.open_cycle(
            parent_alpha_id=parent_id,
            generator_name=self.generator.name,
            trigger_source=trigger_source,
            sim_budget_granted=int(budget),
        )
        # Commit the open_cycle row so it survives a later mid-cycle
        # rollback (review fix E). Without this, an exception during
        # generator/simulator/persister would rollback the entire session
        # and the optimization_runs row would vanish — telemetry would
        # show no record of the failure, defeating the GO/STOP gate audit.
        db = getattr(self.repository, "db", None)
        if db is not None:
            try:
                await db.commit()
            except Exception as commit_ex:  # noqa: BLE001
                logger.warning(
                    "[OptimizationService] open_cycle commit failed "
                    "(continuing without durability guarantee): %s", commit_ex,
                )

        try:
            variants = await self.generator.generate(parent_alpha)
            n_variants_total = len(variants)

            sim_results = await self.simulator.run_batch(variants, budget=int(budget))
            sim_spent = len(sim_results)

            winners = self.winner_selector.pick(sim_results, delay=delay)
            n_winners = len(winners)

            persisted_pks = await self.persister.save(
                winners=winners,
                parent_alpha_id=parent_id,
                opt_run_id=opt_run_id,
            )

            await self.repository.record_persist(
                opt_run_id=opt_run_id,
                n_variants=n_variants_total,
                n_winners=n_winners,
                sim_spent=sim_spent,
            )

            actions = await self.submit_policy.decide(persisted_pks)
            n_submitted = sum(1 for a in actions if a == "submit")
            await self.repository.record_submit(
                opt_run_id=opt_run_id, n_submitted=n_submitted
            )

            # Stage A: feedback is a no-op; Stage C will gate by submit
            # action. Call once per winner so the per-alpha hook semantics
            # are stable across stages.
            for w in winners:
                try:
                    await self.feedback.on_winner(w)
                except Exception as ex:  # noqa: BLE001 — feedback is best-effort
                    logger.warning(
                        "[OptimizationService] feedback.on_winner failed "
                        "(non-fatal): %s", ex,
                    )

            await self.repository.finish_cycle(opt_run_id=opt_run_id)
            return {
                "opt_run_id": opt_run_id,
                "parent_alpha_id": parent_id,
                "generator_name": self.generator.name,
                "n_variants": n_variants_total,
                "n_winners": n_winners,
                "n_submitted": n_submitted,
                "sim_budget_used": sim_spent,
                "sim_budget_granted": int(budget),
                "persisted_pks": [pk for pk in persisted_pks if pk is not None],
            }
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
            logger.exception(
                "[OptimizationService] cycle %s failed: %s", opt_run_id, err,
            )
            # Recover session from any mid-cycle flush poisoning before
            # stamping the error (review fix E). A failed db.flush() in
            # Persister/Simulator leaves the session in InvalidRequest state;
            # finish_cycle's own flush would then silently no-op, leaving
            # optimization_runs.error NULL despite the visible exception.
            if db is not None:
                try:
                    await db.rollback()
                except Exception as rb_ex:  # noqa: BLE001
                    logger.warning(
                        "[OptimizationService] poison recovery rollback "
                        "failed for cycle %s: %s", opt_run_id, rb_ex,
                    )
            try:
                await self.repository.finish_cycle(
                    opt_run_id=opt_run_id, error=err
                )
                # Make the error stamp durable so /ops/optimization/cycles
                # sees it on the next refresh — without this commit, the
                # outer session manager's rollback (on re-raise) would wipe
                # the stamp again.
                if db is not None:
                    try:
                        await db.commit()
                    except Exception as commit_ex:  # noqa: BLE001
                        logger.warning(
                            "[OptimizationService] error-stamp commit "
                            "failed for cycle %s: %s", opt_run_id, commit_ex,
                        )
            except Exception as fin_ex:  # noqa: BLE001
                logger.warning(
                    "[OptimizationService] finish_cycle error-stamp also "
                    "failed for %s: %s", opt_run_id, fin_ex,
                )
            raise
