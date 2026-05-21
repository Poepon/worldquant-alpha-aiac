"""
Dataset Selector Service - Enhanced with Pre-Mining Quality Evaluation

Features:
1. DatasetEvaluator: Pre-mining quality assessment
2. DatasetSelector: Bandit-based intelligent selection
3. Category-aware quality scoring
4. Historical success rate tracking

P1-fix-1: Enable adaptive dataset selection to escape ineffective datasets.
"""

import json
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_
from loguru import logger

from backend.selection_strategy import DatasetBandit, DatasetArm
from backend.models import DatasetMetadata, Alpha, DataField, KnowledgeEntry
from backend.config import settings


# =============================================================================
# Dataset Quality Evaluation
# =============================================================================

@dataclass
class DatasetQualityScore:
    """Comprehensive quality assessment for a dataset."""
    dataset_id: str
    region: str
    universe: str
    
    # Raw metrics
    coverage: float = 0.0          # Data coverage (0-1)
    field_count: int = 0           # Number of fields
    alpha_count: int = 0           # Historical alphas produced
    success_rate: float = 0.0      # Historical success rate
    avg_sharpe: float = 0.0        # Average Sharpe of successful alphas
    pyramid_multiplier: float = 1.0  # Platform pyramid multiplier
    
    # Derived scores (0-1)
    coverage_score: float = 0.0
    richness_score: float = 0.0    # Based on field count
    track_record_score: float = 0.0  # Based on historical success
    potential_score: float = 0.0    # Estimated potential
    
    # Final combined score
    overall_score: float = 0.0
    
    # Metadata
    category: str = "other"
    last_success_date: Optional[datetime] = None
    recommendation: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "region": self.region,
            "universe": self.universe,
            "coverage": self.coverage,
            "field_count": self.field_count,
            "alpha_count": self.alpha_count,
            "success_rate": self.success_rate,
            "avg_sharpe": self.avg_sharpe,
            "pyramid_multiplier": self.pyramid_multiplier,
            "coverage_score": round(self.coverage_score, 3),
            "richness_score": round(self.richness_score, 3),
            "track_record_score": round(self.track_record_score, 3),
            "potential_score": round(self.potential_score, 3),
            "overall_score": round(self.overall_score, 3),
            "category": self.category,
            "recommendation": self.recommendation,
        }


class DatasetEvaluator:
    """
    Pre-mining dataset quality evaluation.
    
    Evaluates datasets based on:
    1. Data coverage and field richness
    2. Historical alpha production success
    3. Category-based potential estimation
    4. Platform pyramid multipliers
    
    Usage:
        evaluator = DatasetEvaluator(db)
        scores = await evaluator.evaluate_datasets(region="USA", universe="TOP3000")
        top_datasets = evaluator.rank_datasets(scores, top_n=10)
    """
    
    # Category-based potential multipliers (from empirical observations)
    CATEGORY_POTENTIAL = {
        "pv": 1.0,           # Price-volume: standard, competitive
        "analyst": 1.3,      # Analyst: high alpha potential
        "fundamental": 1.2,  # Fundamental: solid, less competitive
        "news": 0.9,         # News/sentiment: noisy, harder
        "other": 0.8,        # Other: unknown, lower priority
    }
    
    # Minimum thresholds for consideration
    MIN_COVERAGE = 0.3       # 30% coverage minimum
    MIN_FIELDS = 5           # At least 5 fields
    
    def __init__(self, db: AsyncSession):
        self.db = db
        
    async def evaluate_datasets(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        dataset_ids: Optional[List[str]] = None,
        include_zero_alpha: bool = True
    ) -> List[DatasetQualityScore]:
        """
        Evaluate all available datasets for a region/universe.
        
        Args:
            region: Market region
            universe: Universe
            dataset_ids: Optional specific datasets to evaluate
            include_zero_alpha: Include datasets with no historical alphas
            
        Returns:
            List of DatasetQualityScore objects
        """
        logger.info(f"[DatasetEvaluator] Evaluating datasets | region={region} universe={universe}")
        
        # 1. Get dataset metadata
        query = select(DatasetMetadata).where(
            DatasetMetadata.region == region,
            DatasetMetadata.is_active == True
        )
        
        if dataset_ids:
            query = query.where(DatasetMetadata.dataset_id.in_(dataset_ids))
            
        result = await self.db.execute(query)
        datasets = result.scalars().all()
        
        if not datasets:
            logger.warning(f"[DatasetEvaluator] No datasets found for region={region}")
            return []
        
        # 2. Get historical alpha statistics
        alpha_stats = await self._get_alpha_statistics(region, universe)
        
        # 3. Evaluate each dataset
        scores = []
        for ds in datasets:
            score = await self._evaluate_single_dataset(
                dataset=ds,
                region=region,
                universe=universe,
                alpha_stats=alpha_stats
            )
            
            # Filter out low-quality datasets
            if score.coverage >= self.MIN_COVERAGE or include_zero_alpha:
                if score.field_count >= self.MIN_FIELDS or include_zero_alpha:
                    scores.append(score)
        
        logger.info(f"[DatasetEvaluator] Evaluated {len(scores)} datasets")
        return scores
    
    async def _get_alpha_statistics(
        self,
        region: str,
        universe: str
    ) -> Dict[str, Dict]:
        """Get historical alpha statistics by dataset."""
        # Query alphas in the last 90 days
        cutoff = datetime.now() - timedelta(days=90)
        
        query = select(
            Alpha.dataset_id,
            func.count(Alpha.id).label('total'),
            func.count(Alpha.id).filter(Alpha.quality_status == 'PASS').label('passed'),
            func.avg(Alpha.is_sharpe).label('avg_sharpe'),
            func.max(Alpha.created_at).label('last_alpha')
        ).where(
            Alpha.region == region,
            Alpha.created_at >= cutoff
        ).group_by(Alpha.dataset_id)
        
        result = await self.db.execute(query)
        rows = result.fetchall()
        
        stats = {}
        for row in rows:
            if row.dataset_id:
                stats[row.dataset_id] = {
                    'total': row.total or 0,
                    'passed': row.passed or 0,
                    'avg_sharpe': float(row.avg_sharpe or 0),
                    'last_alpha': row.last_alpha
                }
        
        return stats
    
    async def _evaluate_single_dataset(
        self,
        dataset: DatasetMetadata,
        region: str,
        universe: str,
        alpha_stats: Dict[str, Dict]
    ) -> DatasetQualityScore:
        """Evaluate a single dataset."""
        ds_id = dataset.dataset_id
        
        # Get historical stats
        hist = alpha_stats.get(ds_id, {'total': 0, 'passed': 0, 'avg_sharpe': 0, 'last_alpha': None})
        
        # Calculate success rate
        success_rate = hist['passed'] / hist['total'] if hist['total'] > 0 else 0.5  # Default 50%
        
        # Infer category
        category = self._infer_category(ds_id, dataset.category)
        
        # Create score object
        score = DatasetQualityScore(
            dataset_id=ds_id,
            region=region,
            universe=universe,
            coverage=dataset.coverage or dataset.date_coverage or 0.5,
            field_count=dataset.field_count or 0,
            alpha_count=hist['total'],
            success_rate=success_rate,
            avg_sharpe=hist['avg_sharpe'],
            pyramid_multiplier=dataset.pyramid_multiplier or dataset.mining_weight or 1.0,
            category=category,
            last_success_date=hist['last_alpha']
        )
        
        # Calculate component scores
        score.coverage_score = self._score_coverage(score.coverage)
        score.richness_score = self._score_richness(score.field_count)
        score.track_record_score = self._score_track_record(
            success_rate=score.success_rate,
            total_alphas=score.alpha_count,
            avg_sharpe=score.avg_sharpe
        )
        score.potential_score = self._score_potential(
            category=category,
            pyramid_multiplier=score.pyramid_multiplier,
            coverage=score.coverage
        )
        
        # Combined overall score (weighted average)
        weights = {
            'coverage': 0.15,
            'richness': 0.10,
            'track_record': 0.35,
            'potential': 0.40
        }
        
        score.overall_score = (
            weights['coverage'] * score.coverage_score +
            weights['richness'] * score.richness_score +
            weights['track_record'] * score.track_record_score +
            weights['potential'] * score.potential_score
        )
        
        # Generate recommendation
        score.recommendation = self._generate_recommendation(score)
        
        return score
    
    def _infer_category(self, dataset_id: str, db_category: Optional[str]) -> str:
        """Infer dataset category from ID or database category."""
        if db_category:
            cat_lower = db_category.lower()
            for cat in ['pv', 'analyst', 'fundamental', 'news']:
                if cat in cat_lower:
                    return cat
        
        # Infer from dataset_id
        ds_lower = dataset_id.lower()
        category_keywords = {
            'pv': ['pv', 'price', 'volume', 'trade'],
            'analyst': ['anl', 'analyst', 'estimate'],
            'fundamental': ['fnd', 'fundamental', 'fin'],
            'news': ['news', 'sentiment', 'oth', 'social']
        }
        
        for cat, keywords in category_keywords.items():
            for kw in keywords:
                if kw in ds_lower:
                    return cat
        
        return 'other'
    
    def _score_coverage(self, coverage: float) -> float:
        """Score data coverage (0-1)."""
        if coverage >= 0.9:
            return 1.0
        elif coverage >= 0.7:
            return 0.8 + (coverage - 0.7) * 1.0  # 0.8 to 1.0
        elif coverage >= 0.5:
            return 0.5 + (coverage - 0.5) * 1.5  # 0.5 to 0.8
        else:
            return max(0, coverage * 1.0)
    
    def _score_richness(self, field_count: int) -> float:
        """Score field richness (0-1)."""
        if field_count >= 100:
            return 1.0
        elif field_count >= 50:
            return 0.8 + (field_count - 50) * 0.004
        elif field_count >= 20:
            return 0.5 + (field_count - 20) * 0.01
        elif field_count >= 5:
            return 0.2 + (field_count - 5) * 0.02
        else:
            return max(0, field_count * 0.04)
    
    def _score_track_record(
        self,
        success_rate: float,
        total_alphas: int,
        avg_sharpe: float
    ) -> float:
        """Score historical track record."""
        if total_alphas == 0:
            return 0.5  # Unknown - neutral score
        
        # Base score from success rate
        rate_score = min(success_rate * 2, 1.0)  # Double the rate, cap at 1
        
        # Confidence adjustment based on sample size
        confidence = min(total_alphas / 20, 1.0)  # Full confidence at 20+ alphas
        
        # Sharpe bonus
        sharpe_bonus = min(avg_sharpe / 2.0, 0.3) if avg_sharpe > 0 else 0
        
        # Combined score
        return rate_score * 0.6 + confidence * 0.2 + (0.5 + sharpe_bonus) * 0.2
    
    def _score_potential(
        self,
        category: str,
        pyramid_multiplier: float,
        coverage: float
    ) -> float:
        """Score estimated potential based on category and platform metrics."""
        # Category multiplier
        cat_mult = self.CATEGORY_POTENTIAL.get(category, 0.8)
        
        # Pyramid multiplier bonus (higher pyramid = higher payout potential)
        pyramid_score = min(pyramid_multiplier / 2.0, 1.0)
        
        # Coverage factor
        coverage_factor = 0.5 + coverage * 0.5  # 50% base + coverage bonus
        
        return cat_mult * 0.5 + pyramid_score * 0.3 + coverage_factor * 0.2
    
    def _generate_recommendation(self, score: DatasetQualityScore) -> str:
        """Generate human-readable recommendation."""
        if score.overall_score >= 0.8:
            return "HIGHLY_RECOMMENDED"
        elif score.overall_score >= 0.6:
            return "RECOMMENDED"
        elif score.overall_score >= 0.4:
            return "WORTH_TRYING"
        elif score.overall_score >= 0.2:
            return "LOW_PRIORITY"
        else:
            return "NOT_RECOMMENDED"
    
    def rank_datasets(
        self,
        scores: List[DatasetQualityScore],
        top_n: int = 10,
        min_score: float = 0.2
    ) -> List[DatasetQualityScore]:
        """
        Rank datasets by overall score.
        
        Args:
            scores: List of quality scores
            top_n: Number of top datasets to return
            min_score: Minimum overall score threshold
            
        Returns:
            Top N datasets sorted by overall score
        """
        # Filter by minimum score
        filtered = [s for s in scores if s.overall_score >= min_score]
        
        # Sort by overall score
        filtered.sort(key=lambda x: x.overall_score, reverse=True)
        
        return filtered[:top_n]
    
    async def get_recommended_datasets(
        self,
        region: str,
        universe: str,
        top_n: int = 5
    ) -> List[str]:
        """
        Get top recommended dataset IDs for mining.
        
        Convenience method that returns just the dataset IDs.
        """
        scores = await self.evaluate_datasets(region, universe)
        ranked = self.rank_datasets(scores, top_n=top_n)
        return [s.dataset_id for s in ranked]


class DatasetSelector:
    """
    Intelligent dataset selection using Multi-Armed Bandit.
    
    Usage:
        selector = DatasetSelector(db)
        await selector.initialize(region="KOR", universe="TOP600")
        
        # Select dataset for mining
        dataset_id = await selector.select_dataset()
        
        # After mining iteration, update rewards
        await selector.update_reward(dataset_id, pass_count=2, total_count=4)
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.bandit: Optional[DatasetBandit] = None
        self.region: str = "USA"
        self.universe: str = "TOP3000"
        self._initialized = False
        
    async def initialize(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        dataset_ids: Optional[List[str]] = None
    ):
        """
        Initialize the Bandit with available datasets.
        
        Args:
            region: Market region
            universe: Universe of stocks
            dataset_ids: Optional list of specific dataset IDs to consider
        """
        self.region = region
        self.universe = universe
        
        # Get bandit parameters from settings
        exploration_weight = getattr(settings, 'BANDIT_EXPLORATION_WEIGHT', 2.0)
        pyramid_bonus = getattr(settings, 'BANDIT_PYRAMID_BONUS_WEIGHT', 0.3)
        saturation_penalty = getattr(settings, 'BANDIT_SATURATION_PENALTY_WEIGHT', 0.2)
        
        self.bandit = DatasetBandit(
            exploration_weight=exploration_weight,
            pyramid_bonus_weight=pyramid_bonus,
            saturation_penalty_weight=saturation_penalty
        )
        
        # Load datasets from database
        await self._load_datasets(dataset_ids)
        
        # Load persisted bandit state if exists
        await self._load_bandit_state()
        
        self._initialized = True
        logger.info(f"[DatasetSelector] Initialized | region={region} arms={len(self.bandit.arms)}")
        
    async def _load_datasets(self, dataset_ids: Optional[List[str]] = None):
        """Load datasets from DB and create Bandit arms"""
        query = select(DatasetMetadata).where(
            DatasetMetadata.region == self.region
        )
        
        if dataset_ids:
            query = query.where(DatasetMetadata.dataset_id.in_(dataset_ids))
            
        result = await self.db.execute(query)
        datasets = result.scalars().all()
        
        for ds in datasets:
            arm = DatasetArm(
                dataset_id=ds.dataset_id,
                region=ds.region,
                universe=self.universe,
                pyramid_multiplier=ds.mining_weight or 1.0,
                alpha_count=ds.alpha_count or 0,
                field_count=ds.field_count or 0
            )
            self.bandit.add_arm(arm)
            
        logger.debug(f"[DatasetSelector] Loaded {len(datasets)} datasets as Bandit arms")
        
    async def _load_bandit_state(self):
        """W3: Load persisted Bandit state from bandit_state table.

        Pre-populates each arm's pulls / total_reward from the row
        matching (region, dataset_id). Missing rows are left at zero
        (cold start)."""
        if not self.bandit or not self.bandit.arms:
            return
        try:
            from sqlalchemy import select as sa_select
            from backend.models import BanditState
            stmt = sa_select(BanditState).where(BanditState.region == self.region)
            result = await self.db.execute(stmt)
            rows = {r.dataset_id: r for r in result.scalars().all()}
            for arm in self.bandit.arms:
                row = rows.get(arm.dataset_id)
                if row is not None:
                    arm.pulls = row.pulls
                    arm.total_reward = row.total_reward
                    # Stash sim_count_today on arm so update_reward can use it
                    setattr(arm, "sim_count_today", row.sim_count_today)
            logger.debug(
                f"[DatasetSelector] Loaded bandit state | "
                f"region={self.region} arms_with_state={len(rows)}"
            )
        except Exception as e:
            logger.warning(f"[DatasetSelector] _load_bandit_state failed: {e}")

    async def _save_bandit_state(self):
        """W3: Persist arm state via INSERT ... ON CONFLICT atomic upsert."""
        if not self.bandit or not self.bandit.arms:
            return
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.sql import func as sa_func
            from backend.models import BanditState
            for arm in self.bandit.arms:
                values = {
                    "region": self.region,
                    "dataset_id": arm.dataset_id,
                    "pulls": int(getattr(arm, "pulls", 0) or 0),
                    "total_reward": float(getattr(arm, "total_reward", 0.0) or 0.0),
                    "sim_count_today": int(getattr(arm, "sim_count_today", 0) or 0),
                }
                stmt = pg_insert(BanditState).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["region", "dataset_id"],
                    set_={
                        "pulls": stmt.excluded.pulls,
                        "total_reward": stmt.excluded.total_reward,
                        "sim_count_today": stmt.excluded.sim_count_today,
                    },
                )
                await self.db.execute(stmt)
            await self.db.commit()
        except Exception as e:
            logger.warning(f"[DatasetSelector] _save_bandit_state failed: {e}")
        
    async def select_dataset(self, n: int = 1) -> List[str]:
        """
        Select dataset(s) using UCB algorithm.
        
        Args:
            n: Number of datasets to select
            
        Returns:
            List of selected dataset IDs
        """
        if not self._initialized:
            raise RuntimeError("DatasetSelector not initialized. Call initialize() first.")
            
        if not self.bandit.arms:
            logger.warning("[DatasetSelector] No datasets available for selection")
            return []
            
        selected_arms = self.bandit.select(n=n)
        dataset_ids = [arm.dataset_id for arm in selected_arms]
        
        logger.info(f"[DatasetSelector] Selected datasets: {dataset_ids}")
        return dataset_ids
        
    async def update_reward(
        self,
        dataset_id: str,
        pass_count: int,
        total_count: int,
        avg_sharpe: float = 0.0
    ):
        """
        Update Bandit reward after mining iteration.
        
        Args:
            dataset_id: Dataset that was mined
            pass_count: Number of PASS alphas
            total_count: Total alphas evaluated
            avg_sharpe: Average Sharpe ratio of passes
        """
        if not self._initialized:
            return
            
        # Raw reward: pass rate with Sharpe bonus
        pass_rate = pass_count / total_count if total_count > 0 else 0
        sharpe_bonus = min(avg_sharpe / 3.0, 0.5) if avg_sharpe > 0 else 0  # Cap at 0.5
        reward_raw = pass_rate + sharpe_bonus * 0.3

        # W3: cost-aware FLAML-CFO-style adjustment.
        # cost_factor downweights datasets we've already simulated heavily
        # today, encouraging exploration of cheaper alternatives. Floor 0.3
        # prevents starvation of high-pyramid datasets in the morning when
        # everyone's sim_count is zero. Per plan R3 #6 simplification we
        # use sim_count_today (reset by celery beat) rather than a sliding
        # 24h window — simpler and BanditState row already carries it.
        import math as _math
        arm = next((a for a in (self.bandit.arms if self.bandit else []) if a.dataset_id == dataset_id), None)
        sim_count_today = int(getattr(arm, "sim_count_today", 0) or 0) if arm else 0
        # Increment for THIS round's simulations
        sim_count_today += int(total_count or 0)
        if arm is not None:
            setattr(arm, "sim_count_today", sim_count_today)
        cost_factor = max(0.3, 1.0 / (1.0 + _math.log(1 + sim_count_today)))
        reward = reward_raw * cost_factor

        success = pass_count > 0

        self.bandit.update(
            dataset_id=dataset_id,
            reward=reward,
            success=success,
            region=self.region,
            universe=self.universe
        )

        logger.info(
            f"[DatasetSelector] Updated reward | dataset={dataset_id} "
            f"pass={pass_count}/{total_count} raw={reward_raw:.3f} "
            f"cost_factor={cost_factor:.3f} reward={reward:.3f} "
            f"sim_count_today={sim_count_today}"
        )

        # Persist state (incl. sim_count_today on the arm)
        await self._save_bandit_state()
        
    def get_stats(self) -> Dict:
        """Get Bandit statistics for observability"""
        if not self.bandit:
            return {}
        return self.bandit.get_stats()


