"""OpsReportReader — async double-source reader for /ops/* dashboards.

Source: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.5.

Each /ops/* page has two ways to fetch its data:

1. **Service path** (T+0, freshest) — call e.g. ``AlphaHealthService.run_full_check``
   and get a live computed report. Slow on big libraries (~seconds).
2. **Archive path** — read ``docs/{kind}/<sh-date>.json``, dropped by the
   daily Celery beat. Fast (~ms) but only as fresh as the last beat run.

This reader is the unified front door. ``get_or_compute(kind, date,
fresh_service=...)`` decides which source to use:

* Today + a fresh_service is provided → service path (with archive
  fallback on exception).
* Otherwise → archive path. If the file for the requested date is
  missing, walk back up to ``ARCHIVE_FALLBACK_DAYS`` days and tag the
  payload as stale.
* Still nothing → return ``({}, "missing")`` so the router emits a 200
  with an empty body + a banner asking the operator to click *Rerun*.

The returned ``source_tag`` is rendered as a colored badge in the UI so
operators can tell whether they're looking at live numbers or yesterday's
snapshot.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import anyio

logger = logging.getLogger("services.ops_report_reader")


# repo root: backend/services/ops_report_reader.py → parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_ROOT = _REPO_ROOT / "docs"

SH_TZ = timezone(timedelta(hours=8))


# -- knobs (env-overridable for tests) --------------------------------------

# How many days back to scan when the requested date file is missing.
ARCHIVE_FALLBACK_DAYS: int = int(os.getenv("OPS_REPORT_ARCHIVE_FALLBACK_DAYS", "7"))

# Hard cap to keep a single rogue file from blowing the response. Anything
# bigger is truncated and tagged with ``_truncated_bytes``.
MAX_FILE_BYTES: int = int(os.getenv("OPS_REPORT_MAX_FILE_BYTES", str(5 * 1024 * 1024)))

# Per-(path, mtime) read cache TTL — 5 min keeps repeated dashboard
# refreshes off disk without making truly fresh data invisible.
READ_CACHE_TTL_SEC: int = int(os.getenv("OPS_REPORT_READ_CACHE_TTL_SEC", "300"))


# Tag values returned alongside the payload — match exactly what
# /ops/* router responses promise to the frontend.
SOURCE_SERVICE = "service"
SOURCE_DOCS_TODAY = "docs_today"
SOURCE_DOCS_ARCHIVED = "docs_archived"
SOURCE_MISSING = "missing"


# ---------------------------------------------------------------------------
# Module-level mtime cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    mtime: float
    cached_at: float
    payload: Dict[str, Any]


# Keyed by absolute path string. Module-level so all reader instances share.
# We keep this small (≤ a few hundred entries) by clearing entries that
# have aged past TTL on the next miss.
_read_cache: Dict[str, _CacheEntry] = {}


def _read_json_cached(path: Path) -> Optional[Dict[str, Any]]:
    """Sync helper — read + parse JSON with mtime + TTL cache.

    Returned dict may include synthetic keys like ``_truncated_bytes`` if
    the file exceeded MAX_FILE_BYTES. Returns None on any IO / JSON error
    (caller logs once and treats as miss).
    """
    try:
        st = path.stat()
    except OSError:
        return None

    key = str(path)
    now = time.time()
    cached = _read_cache.get(key)
    if (
        cached is not None
        and cached.mtime == st.st_mtime
        and (now - cached.cached_at) < READ_CACHE_TTL_SEC
    ):
        return cached.payload

    try:
        with path.open("rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as ex:
        logger.warning("[ops_report] read failed %s: %s", path, ex)
        return None

    truncated = len(raw) > MAX_FILE_BYTES
    if truncated:
        raw = raw[:MAX_FILE_BYTES]

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        logger.warning("[ops_report] decode failed %s: %s", path, ex)
        return None

    if not isinstance(payload, dict):
        # Top-level lists are valid JSON but our reports are always
        # objects — wrap to keep the return contract consistent.
        payload = {"_payload": payload}

    if truncated:
        payload["_truncated_bytes"] = len(raw)

    _read_cache[key] = _CacheEntry(mtime=st.st_mtime, cached_at=now, payload=payload)
    return payload


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

class OpsReportReader:
    """Async wrapper around the docs/<kind>/<date>.json hierarchy.

    Stateless aside from the module-level ``_read_cache``. Constructed
    fresh per request by the ops router; safe to instantiate liberally.
    """

    def __init__(self, docs_root: Optional[Path] = None) -> None:
        # docs_root override is for tests — production always uses the
        # repo-rooted ``docs/`` directory.
        self._docs_root = docs_root or _DOCS_ROOT

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _kind_dir(self, kind: str) -> Path:
        return self._docs_root / kind

    def _date_path(self, kind: str, d: date) -> Path:
        return self._kind_dir(kind) / f"{d.isoformat()}.json"

    @staticmethod
    def today_sh() -> date:
        """Today in Asia/Shanghai — matches the daily beat timezone."""
        return datetime.now(SH_TZ).date()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_compute(
        self,
        kind: str,
        date_: Optional[date] = None,
        *,
        fresh_service: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Fetch the report payload + source tag.

        Args:
            kind: The docs subdirectory name (e.g. "pillar_balance",
                "alpha_health_check"). No path traversal validation
                because callers pass hard-coded literals; we trust them.
            date_: The report date. Defaults to today (Asia/Shanghai).
            fresh_service: Optional async callable that recomputes the
                report live. If today is requested and this is given, we
                try service first. The callable must return a dict.

        Returns:
            ``(payload, source_tag)`` where source_tag is one of the
            ``SOURCE_*`` module-level constants.
        """
        target = date_ or self.today_sh()
        is_today = target == self.today_sh()

        # 1. Service path for T+0 + given fresh_service
        if is_today and fresh_service is not None:
            try:
                live = await fresh_service()
                if isinstance(live, dict):
                    return live, SOURCE_SERVICE
                logger.warning(
                    "[ops_report] fresh_service returned non-dict for kind=%s",
                    kind,
                )
            except Exception as ex:
                # Service blew up — log + fall through to archive path
                logger.warning(
                    "[ops_report] fresh_service failed for kind=%s: %s — "
                    "falling back to docs",
                    kind, ex,
                )

        # 2. Try the requested-date file
        primary = self._date_path(kind, target)
        payload = await self._read_async(primary)
        if payload is not None:
            tag = SOURCE_DOCS_TODAY if is_today else SOURCE_DOCS_ARCHIVED
            return payload, tag

        # 3. Walk back ARCHIVE_FALLBACK_DAYS, tagging staleness
        for back in range(1, ARCHIVE_FALLBACK_DAYS + 1):
            alt = self._date_path(kind, target - timedelta(days=back))
            stale = await self._read_async(alt)
            if stale is not None:
                stale = dict(stale)  # don't mutate the cached entry
                stale["_stale_days"] = back
                stale["_stale_source_path"] = alt.name
                return stale, SOURCE_DOCS_ARCHIVED

        # 4. Nothing
        return {}, SOURCE_MISSING

    async def list_recent(
        self,
        kind: str,
        days: int = 30,
        *,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """Return up to ``days`` payloads for a kind, newest-first.

        Each entry is the original JSON payload extended with ``_date``
        (ISO string). Missing days are skipped, NOT zero-filled — the
        frontend handles gaps.
        """
        end = end_date or self.today_sh()
        days = max(1, min(days, 365))

        async def _read_one(d: date) -> Optional[Dict[str, Any]]:
            payload = await self._read_async(self._date_path(kind, d))
            if payload is None:
                return None
            out = dict(payload)
            out["_date"] = d.isoformat()
            return out

        # Fan out concurrently — each is a single threadpool read; cheap.
        results: List[Optional[Dict[str, Any]]] = []
        async with anyio.create_task_group() as tg:
            slots: List[List[Optional[Dict[str, Any]]]] = [[None] for _ in range(days)]

            async def _fill(idx: int, d: date) -> None:
                slots[idx][0] = await _read_one(d)

            for i in range(days):
                tg.start_soon(_fill, i, end - timedelta(days=i))
        results = [s[0] for s in slots]

        return [r for r in results if r is not None]

    async def list_kinds(self) -> List[str]:
        """Enumerate all docs subdirectories — for /ops/overview health grid."""
        try:
            return await anyio.to_thread.run_sync(self._list_kinds_sync)
        except Exception as ex:
            logger.warning("[ops_report] list_kinds failed: %s", ex)
            return []

    def _list_kinds_sync(self) -> List[str]:
        if not self._docs_root.exists():
            return []
        return sorted(
            p.name for p in self._docs_root.iterdir() if p.is_dir()
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_async(self, path: Path) -> Optional[Dict[str, Any]]:
        """Off-load the sync read to the thread pool to avoid blocking
        the event loop."""
        return await anyio.to_thread.run_sync(_read_json_cached, path)


# ---------------------------------------------------------------------------
# Test helper — clear cache between tests so mtime-stamped reads are honest
# ---------------------------------------------------------------------------

def _reset_read_cache_for_tests() -> None:
    _read_cache.clear()
