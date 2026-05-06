"""Plan v5+ #3 — Pre-simulate filter runtime.

At node_simulate entry, predict P(quality_status='PASS') for each candidate
and skip those below threshold. Saves BRAIN concurrent-slot time on alphas
that the classifier identifies as very likely to fail quality eval.

Defaults conservatively (threshold=0.05) so PASS recall stays ≥99%. The
training script (scripts/train_pre_simulate_classifier.py) reports the
threshold-vs-recall table so you can tune for your risk tolerance.

Failure modes:
  - Model file missing → filter disabled (returns "keep all"). Worker
    log emits a one-time warning per process.
  - Inference exception → keep that alpha (don't skip on doubt). Logged.

Toggle:
  - ENABLE_PRE_SIMULATE_FILTER env var / setting controls activation.
    Default False — explicit opt-in via .env so production rollout is
    deliberate.
  - PRE_SIMULATE_FILTER_THRESHOLD overrides recommended threshold from
    metadata.json.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from backend.agents.services.pre_simulate_features import (
    extract_features, feature_keys,
)

logger = logging.getLogger("agents.pre_simulate_filter")

_MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
_MODEL_PATH = _MODELS_DIR / "pre_simulate_classifier.pkl"
_META_PATH = _MODELS_DIR / "pre_simulate_metadata.json"

_model = None
_metadata = None
_load_lock = threading.Lock()
_load_attempted = False
_warned_missing = False


def _try_load() -> bool:
    """Attempt to load the model + metadata. Returns True if available.
    Thread-safe: first caller wins, subsequent callers reuse."""
    global _model, _metadata, _load_attempted, _warned_missing

    if _load_attempted:
        return _model is not None

    with _load_lock:
        if _load_attempted:
            return _model is not None
        _load_attempted = True

        if not _MODEL_PATH.exists() or not _META_PATH.exists():
            if not _warned_missing:
                logger.info(
                    f"[pre_simulate_filter] model not found at {_MODEL_PATH}; "
                    f"filter disabled. Run scripts/train_pre_simulate_classifier.py to enable."
                )
                _warned_missing = True
            return False

        try:
            import joblib
            import json
            _model = joblib.load(_MODEL_PATH)
            _metadata = json.loads(_META_PATH.read_text(encoding="utf-8"))
            logger.info(
                f"[pre_simulate_filter] loaded model | trained={_metadata.get('trained_at')} "
                f"AUC={_metadata.get('auc_cv'):.3f} threshold={_metadata.get('recommended_threshold')}"
            )
            return True
        except Exception as e:
            logger.warning(f"[pre_simulate_filter] load failed: {e}; filter disabled")
            return False


def get_default_threshold() -> float:
    """Returns metadata's recommended_threshold (set at training time) or
    0.05 if metadata missing."""
    if not _try_load():
        return 0.05
    return float(_metadata.get("recommended_threshold", 0.05))


def predict_pass_probability(expressions: List[str]) -> List[float]:
    """Returns P(quality_status=PASS) per expression.

    When model unavailable returns all 1.0 (no filter — keep everything).
    Inference errors per-alpha return 1.0 for that alpha.
    """
    if not _try_load():
        return [1.0] * len(expressions)

    keys = feature_keys()
    X = np.zeros((len(expressions), len(keys)), dtype=float)
    for i, expr in enumerate(expressions):
        try:
            feats = extract_features(expr)
            for j, k in enumerate(keys):
                X[i, j] = float(feats.get(k, 0))
        except Exception as e:
            logger.debug(f"[pre_simulate_filter] feature extract failed for alpha {i}: {e}")
            # All zeros for this row → model will likely output ~0.5
            pass

    try:
        proba = _model.predict_proba(X)[:, 1]
    except Exception as e:
        logger.warning(f"[pre_simulate_filter] inference failed: {e}; keeping all")
        return [1.0] * len(expressions)

    return proba.tolist()


def filter_candidates(
    expressions: List[str],
    threshold: Optional[float] = None,
) -> Tuple[List[int], List[int], List[float]]:
    """Returns (keep_indices, skip_indices, all_probabilities).

    keep_indices: positions where P(PASS) >= threshold (send to BRAIN sim)
    skip_indices: positions where P(PASS) < threshold (skip simulate)
    all_probabilities: P(PASS) for every input position

    When the model is unavailable, all are kept (skip_indices empty).
    """
    if threshold is None:
        threshold = get_default_threshold()

    proba = predict_pass_probability(expressions)
    keep, skip = [], []
    for i, p in enumerate(proba):
        if p >= threshold:
            keep.append(i)
        else:
            skip.append(i)
    return keep, skip, proba
