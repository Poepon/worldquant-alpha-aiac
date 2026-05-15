"""P2-C RegimeInferenceService (2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skills `vix-status` +
`duan-analysis`.

Reads daily ``docs/alpha_health_check/<sh-date>.json`` reports, derives a
7-day proxy regime signal from the GREEN+YELLOW pass-rate (per region),
EWMA-smooths it (α=0.3), and writes:

  * Redis ``aiac:current_regime:{region}`` (string, 24h TTL) — fast read
    path for ``mining_agent.run_mining_iteration`` (S2 — only flag-on
    nudges call this).
  * Redis ``aiac:regime_snapshot:{region}`` (JSON, 24h TTL) — full
    inference snapshot for diagnostics.
  * Archive ``docs/regime_state/<sh-date>.json`` (combined region map) —
    written by the daily Celery task; not by this service directly.

NOTE — this service is the **read+write** path. The Celery task is in
``backend/tasks/regime_infer.py`` and orchestrates region iteration +
archive emission.

The `infer_current_regime` return dict deliberately OMITS
``sharpe_avg_7d`` (MF1) — alpha_health JSON ``records`` carry no top-
level ``current_sharpe`` field; the sharpe value lives nested under
``signals.drift.current_sharpe`` which we don't aggregate here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from backend.regime_classifier import (
    REGIME_PRESETS,
    apply_ewma_smoothing,
    classify_pass_rate_to_regime,
)
from backend.services.base import BaseService


# repo root (services/ is two levels deep under backend/, and backend/ is
# one level under repo root). NOTE on S3/S7: ``parents[2]`` is verified
# correct — backend/services/this_file.py → parents[0]=services,
# parents[1]=backend, parents[2]=<repo_root>.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_DIR = _REPO_ROOT / "docs" / "alpha_health_check"
_OUTPUT_DIR = _REPO_ROOT / "docs" / "regime_state"

SH_TZ = timezone(timedelta(hours=8))


class RegimeInferenceService(BaseService):
    """Read alpha_health_check JSONs → derive regime → cache to Redis."""

    # ------------------------------------------------------------------
    # infer_current_regime — pure (no DB / Redis writes)
    # ------------------------------------------------------------------
    async def infer_current_regime(
        self,
        *,
        region: str = "USA",
        window_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read the last N daily alpha_health_check JSONs for ``region`` and
        derive a smoothed regime.

        Args:
            region: BRAIN region (USA/CHN/EUR/ASI/GLB). Falls back to
                ``totals.by_band`` when the per-region path is missing.
            window_days: lookback. Defaults to
                ``settings.REGIME_INFERENCE_WINDOW_DAYS`` (7).

        Returns:
            ``{
                "regime":               <smoothed label>,
                "raw_regime_today":     <today's bucket, ignoring EWMA>,
                "pass_rate":            <today's pass_rate, or None>,
                "pass_rate_7d_mean":    <window mean, or None>,
                "confidence":           <#days observed / window_days>,
                "history":              <oldest-first list of daily labels>,
                "smoothed_at":          <UTC ISO timestamp>,
                "cold_start":           <True if <2 days observed>,
                "region":               <echo>,
            }``

            **Deliberately no `sharpe_avg_7d` key** (MF1): alpha_health
            JSON ``records`` carry no top-level ``current_sharpe`` field.
        """
        from backend.config import settings  # lazy — avoids cycle at import

        wd = int(window_days or getattr(
            settings, "REGIME_INFERENCE_WINDOW_DAYS", 7,
        ))
        ewma_alpha = float(getattr(settings, "REGIME_EWMA_ALPHA", 0.3))

        now_utc = datetime.now(timezone.utc)
        sh_today = now_utc.astimezone(SH_TZ).date()

        history: List[str] = []
        pass_rates: List[float] = []
        days_observed = 0
        latest_pass_rate: Optional[float] = None

        # Walk the window oldest-first so EWMA recency weighting is right.
        for back in range(wd - 1, -1, -1):
            day = sh_today - timedelta(days=back)
            path = _HEALTH_DIR / f"{day.isoformat()}.json"
            if not path.exists():
                continue
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
            except Exception as ex:
                logger.warning(
                    f"[regime_infer] skip unreadable health JSON "
                    f"{path.name}: {ex}"
                )
                continue

            by_band, checked = self._extract_band_counts(blob, region)
            if checked is None or checked <= 0:
                continue
            green = int(by_band.get("GREEN", 0) or 0)
            yellow = int(by_band.get("YELLOW", 0) or 0)
            pass_rate = (green + yellow) / float(checked)
            history.append(classify_pass_rate_to_regime(pass_rate))
            pass_rates.append(pass_rate)
            latest_pass_rate = pass_rate
            days_observed += 1

        cold_start = days_observed < 2
        smoothed = apply_ewma_smoothing(history, alpha=ewma_alpha)
        raw_today = (
            history[-1] if history else "normal"
        )
        mean_pr = (
            sum(pass_rates) / float(len(pass_rates)) if pass_rates else None
        )
        confidence = days_observed / float(wd) if wd > 0 else 0.0

        return {
            "regime": smoothed,
            "raw_regime_today": raw_today,
            "pass_rate": latest_pass_rate,
            "pass_rate_7d_mean": mean_pr,
            "confidence": round(confidence, 3),
            "history": list(history),
            "smoothed_at": now_utc.isoformat(),
            "cold_start": cold_start,
            "region": region,
        }

    # ------------------------------------------------------------------
    # get_cached_regime — Redis read (no DB)
    # ------------------------------------------------------------------
    async def get_cached_regime(
        self,
        region: str = "USA",
    ) -> Optional[str]:
        """Read ``aiac:current_regime:{region}`` from Redis.

        Returns the cached regime string (one of REGIME_PRESETS keys) on a
        hit, None on miss / Redis unavailable / bad value. The mining_agent
        injection path calls this on every iteration: a Redis blip MUST
        degrade silently to ``None`` so mining keeps running.
        """
        try:
            from backend.tasks.redis_pool import get_redis_client  # lazy
            cli = get_redis_client()
        except Exception as ex:
            logger.warning(f"[regime_infer] redis unavailable: {ex}")
            return None
        try:
            raw = cli.get(f"aiac:current_regime:{region}")
        except Exception as ex:
            logger.warning(
                f"[regime_infer] redis GET failed for region={region}: {ex}"
            )
            return None
        if raw is None:
            return None
        s = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        s = s.strip().strip('"').strip("'")
        if s in REGIME_PRESETS:
            return s
        return None

    # ------------------------------------------------------------------
    # write_regime_state — Redis SETEX + docs/regime_state archive
    # ------------------------------------------------------------------
    async def write_regime_state(
        self,
        *,
        region: str,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist a regime snapshot to Redis (+ return diagnostics).

        Args:
            region: BRAIN region label.
            snapshot: the dict returned by ``infer_current_regime``.

        Returns a small status dict ``{redis_ok, regime, ttl_sec}``.
        Archive emission to ``docs/regime_state/<sh-date>.json`` is the
        Celery task's responsibility (it merges regions across one call).
        """
        from backend.config import settings  # lazy
        ttl = int(getattr(settings, "REGIME_CACHE_TTL_SECONDS", 86400))
        regime = str(snapshot.get("regime") or "normal")
        out = {"redis_ok": False, "regime": regime, "ttl_sec": ttl}
        try:
            from backend.tasks.redis_pool import get_redis_client  # lazy
            cli = get_redis_client()
        except Exception as ex:
            logger.warning(f"[regime_infer] write — redis unavailable: {ex}")
            return out
        try:
            cli.setex(f"aiac:current_regime:{region}", ttl, regime)
            cli.setex(
                f"aiac:regime_snapshot:{region}",
                ttl,
                json.dumps(snapshot, default=str),
            )
            out["redis_ok"] = True
        except Exception as ex:
            logger.warning(
                f"[regime_infer] redis SETEX failed for region={region}: {ex}"
            )
        return out

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_band_counts(
        blob: Dict[str, Any],
        region: str,
    ) -> tuple:
        """Pull ``(by_band_dict, checked)`` for ``region`` from a health JSON.

        Order of resolution:
            1. ``regions.<R>.by_band`` + ``regions.<R>.checked``
            2. fallback to ``totals.by_band`` + ``totals.checked``
            3. None on missing
        """
        try:
            regions = blob.get("regions") or {}
            r_blob = regions.get(region)
            if isinstance(r_blob, dict):
                bb = r_blob.get("by_band") or {}
                checked = r_blob.get("checked")
                if isinstance(bb, dict) and checked is not None:
                    return bb, int(checked)
        except Exception:
            pass
        # fallback to totals
        try:
            totals = blob.get("totals") or {}
            bb = totals.get("by_band") or {}
            checked = totals.get("checked")
            if isinstance(bb, dict) and checked is not None:
                return bb, int(checked)
        except Exception:
            pass
        return {}, None
