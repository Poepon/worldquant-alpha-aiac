"""
BaselineProvider - DB orchestration for the baseline grid screener.

Wraps ``AlphaRepository.get_cell_metric_samples`` with the fine→coarse fallback
policy and an in-memory per-instance cache, so one evaluate round queries each
distinct grid cell at most once.

Instantiate one BaselineProvider per ``node_evaluate`` call and discard it after
— the cache is intentionally request-scoped.
"""

import logging
from typing import Callable, Dict, Optional, Tuple

from backend.baseline_screener import BaselineStats, fit_baseline
from backend.config import settings

logger = logging.getLogger(__name__)


class BaselineProvider:
    """Resolves a fitted (mean, std) baseline for a grid cell, with fallback."""

    def __init__(
        self,
        min_samples: Optional[int] = None,
        metric_col: str = "is_sharpe",
        lookback_days: Optional[int] = None,
        sample_limit: Optional[int] = None,
        category_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self.min_samples = (
            min_samples if min_samples is not None
            else getattr(settings, "BASELINE_MIN_SAMPLES", 30)
        )
        self.metric_col = metric_col
        self.lookback_days = (
            lookback_days if lookback_days is not None
            else getattr(settings, "BASELINE_LOOKBACK_DAYS", 120)
        )
        self.sample_limit = (
            sample_limit if sample_limit is not None
            else getattr(settings, "BASELINE_SAMPLE_LIMIT", 2000)
        )
        # dataset_id -> category. Lets the coarse fallback resolve a dataset's
        # category without this class querying the datasets table itself —
        # callers pass a resolver built from metadata they already loaded.
        self._category_resolver = category_resolver or (lambda _ds: None)
        self._cache: Dict[Tuple[str, str, str], BaselineStats] = {}

    async def get_baseline(
        self, expected_signal: str, dataset_id: str, region: str
    ) -> BaselineStats:
        """
        Return the fitted baseline for a (expected_signal, dataset_id, region)
        cell. Tries the fine cell first, falls back to the coarse
        (category-level) cell, then to an ``insufficient`` no-op baseline.
        Result is cached per cell key for the lifetime of this instance.
        """
        key = (expected_signal or "unknown", dataset_id or "", region or "")
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        stats = await self._fit(expected_signal, dataset_id, region)
        self._cache[key] = stats
        return stats

    async def _fit(
        self, expected_signal: str, dataset_id: str, region: str
    ) -> BaselineStats:
        from backend.database import AsyncSessionLocal
        from backend.repositories.alpha_repository import AlphaRepository

        es = expected_signal or "unknown"
        fine_key = f"{es}|{dataset_id}|{region}"
        # Default no-op result if anything below fails or comes up short.
        result = BaselineStats(0.0, 0.0, 0, fine_key, "insufficient")

        try:
            async with AsyncSessionLocal() as db:
                repo = AlphaRepository(db)

                # 1) Fine cell: (expected_signal, dataset_id, region)
                fine_samples = await repo.get_cell_metric_samples(
                    expected_signal=es,
                    region=region,
                    metric_col=self.metric_col,
                    dataset_id=dataset_id,
                    lookback_days=self.lookback_days,
                    limit=self.sample_limit,
                )
                fine = fit_baseline(
                    fine_samples, self.min_samples, fine_key, "fine"
                )
                if fine.granularity != "insufficient":
                    return fine
                result = fine

                # 2) Coarse cell: (expected_signal, dataset_category, region)
                category = self._category_resolver(dataset_id)
                if category:
                    coarse_key = f"{es}|cat:{category}|{region}"
                    coarse_samples = await repo.get_cell_metric_samples(
                        expected_signal=es,
                        region=region,
                        metric_col=self.metric_col,
                        category=category,
                        lookback_days=self.lookback_days,
                        limit=self.sample_limit,
                    )
                    coarse = fit_baseline(
                        coarse_samples, self.min_samples, coarse_key, "coarse"
                    )
                    if coarse.granularity != "insufficient":
                        return coarse
                    result = coarse
        except Exception as e:
            # A baseline lookup failure must never break evaluation — degrade
            # this cell to a no-op insufficient baseline and move on.
            logger.warning(
                f"BaselineProvider: cell fit failed for {fine_key}: {e}"
            )

        return result
