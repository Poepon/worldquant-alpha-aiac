"""Alpha-metric decay snapshot helper.

TODO #1 (schema + collection only — analysis deferred until ≥3 months of
data accumulates):

A snapshot is one entry in `Alpha.decay_curve`, a JSONB list mutated only
by the daily Celery beat. Each call to `maybe_append_decay_snapshot` either
appends a new entry or no-ops (if the last entry is too recent or the
alpha has no metrics yet).

Cadence: weekly. Caller invokes daily, but dedup skips appends until 6+
days have passed since the last snapshot. Keeps storage at ~52
entries/year/alpha (~7.5KB JSON).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional


# Minimum gap before we record a new snapshot. The daily beat will call this
# every day; <6 days means "already snapped this week, skip".
MIN_SNAPSHOT_GAP_DAYS = 6


def _to_date(value) -> Optional[date]:
    """Coerce a date/datetime/ISO-string to a date, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _days_between(start, end: date) -> Optional[int]:
    s = _to_date(start)
    if s is None:
        return None
    return (end - s).days


def build_decay_snapshot(alpha, now: datetime) -> Optional[dict]:
    """Construct a snapshot dict from the alpha's current metrics.

    Returns None if the alpha has no usable metrics yet (is_sharpe missing
    means BRAIN hasn't filled in the data — no point in storing a zero row).

    `now` is passed in (rather than read from the clock) so tests can pin
    a deterministic snapshot_date.
    """
    if alpha.is_sharpe is None:
        return None

    today = now.date() if isinstance(now, datetime) else now

    # Prefer the formal submission date; fall back to created_at so brand-new
    # alphas that haven't been formally submitted still get a sensible
    # "days alive" anchor. If both are missing the field is None — analysis
    # code will filter those out later.
    anchor = alpha.date_submitted or alpha.created_at
    days_since_submit = _days_between(anchor, today)

    # Pull metrics from flattened columns first, fall back to the metrics
    # JSONB (some legacy alphas only have the unflattened blob).
    metrics_blob = alpha.metrics if isinstance(alpha.metrics, dict) else {}
    def _pick(flat, key):
        if flat is not None:
            return flat
        v = metrics_blob.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "snapshot_date": today.isoformat(),
        "days_since_submit": days_since_submit,
        "sharpe": _pick(alpha.is_sharpe, "sharpe"),
        "fitness": _pick(alpha.is_fitness, "fitness"),
        "turnover": _pick(alpha.is_turnover, "turnover"),
        "returns": _pick(alpha.is_returns, "returns"),
        "drawdown": _pick(alpha.is_drawdown, "drawdown"),
        "margin": _pick(alpha.is_margin, "margin"),
    }


def should_append_snapshot(decay_curve, now: datetime) -> bool:
    """Dedup gate: True iff the last snapshot is at least
    MIN_SNAPSHOT_GAP_DAYS old (or the curve is empty).
    """
    if not decay_curve:
        return True
    today = now.date() if isinstance(now, datetime) else now
    last = decay_curve[-1] if isinstance(decay_curve, list) else None
    if not isinstance(last, dict):
        return True
    last_date = _to_date(last.get("snapshot_date"))
    if last_date is None:
        return True
    return (today - last_date).days >= MIN_SNAPSHOT_GAP_DAYS


def maybe_append_decay_snapshot(alpha, now: Optional[datetime] = None) -> bool:
    """Append a snapshot to `alpha.decay_curve` if dedup allows.

    Mutates `alpha.decay_curve` in place — caller is responsible for the
    surrounding DB commit. Returns True if a snapshot was appended.

    Safe to call repeatedly: the dedup gate makes daily invocation produce
    weekly entries with no extra logic at the call site.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    curve = list(alpha.decay_curve) if alpha.decay_curve else []
    if not should_append_snapshot(curve, now):
        return False

    snap = build_decay_snapshot(alpha, now)
    if snap is None:
        return False

    curve.append(snap)
    # Reassign (not in-place append) so SQLAlchemy detects the JSONB change.
    alpha.decay_curve = curve
    return True
