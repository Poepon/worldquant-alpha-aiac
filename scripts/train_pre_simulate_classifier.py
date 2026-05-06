"""Plan v5+ #3 — Train pre-simulate skeleton classifier.

Trains a sklearn LogisticRegression to predict P(quality_status='PASS')
from expression-only features. Used at runtime by node_simulate to filter
out alphas with very low P(PASS) BEFORE sending to BRAIN simulate
(saves ~30 min/round in the BRAIN concurrent-slot bottleneck).

Training data:
  - POSITIVE (label=1, ~451):
      alphas WHERE quality_status IN ('PASS', 'PASS_PROVISIONAL')
  - NEGATIVE (label=0, ~2286):
      alphas WHERE quality_status IN ('FAIL', 'REJECT')   [post-sim quality fail]
      alpha_failures WHERE error_type = 'QUALITY_CHECK_FAILED'
                    [pre-sim FAIL that reached quality eval]
  - EXCLUDED:
      PENDING, OPTIMIZE, SYNTAX_ERROR, SIMULATION_ERROR, OTHER
      (these aren't post-quality-eval outcomes)

Output:
  models/pre_simulate_classifier.pkl  — pickled sklearn Pipeline
  models/pre_simulate_metadata.json    — feature_keys + threshold table

Usage:
  python scripts/train_pre_simulate_classifier.py
  python scripts/train_pre_simulate_classifier.py --target-recall 0.95
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    classification_report, confusion_matrix, roc_auc_score,
    precision_recall_curve,
)
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.agents.services.pre_simulate_features import (
    extract_features, feature_keys,
)


_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
_OUT_DIR = Path(__file__).resolve().parents[1] / "models"


async def _load_training_data() -> Tuple[List[str], List[int]]:
    """Returns (expressions, labels) lists. label=1 for PASS, 0 for FAIL."""
    engine = create_async_engine(_PG_URL, echo=False)
    expressions: List[str] = []
    labels: List[int] = []

    async with engine.begin() as conn:
        # Positive: alphas PASS / PASS_PROVISIONAL
        r = await conn.execute(text("""
            SELECT expression FROM alphas
            WHERE quality_status IN ('PASS', 'PASS_PROVISIONAL')
              AND expression IS NOT NULL AND expression <> ''
        """))
        for row in r.fetchall():
            expressions.append(row.expression)
            labels.append(1)

        # Negative 1: alphas FAIL / REJECT (post-sim quality fail)
        r = await conn.execute(text("""
            SELECT expression FROM alphas
            WHERE quality_status IN ('FAIL', 'REJECT')
              AND expression IS NOT NULL AND expression <> ''
        """))
        for row in r.fetchall():
            expressions.append(row.expression)
            labels.append(0)

        # Negative 2: alpha_failures with QUALITY_CHECK_FAILED
        r = await conn.execute(text("""
            SELECT expression FROM alpha_failures
            WHERE error_type = 'QUALITY_CHECK_FAILED'
              AND expression IS NOT NULL AND expression <> ''
        """))
        for row in r.fetchall():
            expressions.append(row.expression)
            labels.append(0)

    await engine.dispose()
    return expressions, labels


def _build_feature_matrix(expressions: List[str]) -> np.ndarray:
    keys = feature_keys()
    X = np.zeros((len(expressions), len(keys)), dtype=float)
    for i, expr in enumerate(expressions):
        feats = extract_features(expr)
        for j, k in enumerate(keys):
            X[i, j] = float(feats.get(k, 0))
    return X


def _build_threshold_table(y_true, y_proba) -> List[dict]:
    """At various P thresholds, report precision/recall/filter-rate."""
    table = []
    for t in [0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50]:
        # Filter decision: P(PASS) < t → SKIP (predict label=0)
        # We measure: if we use this threshold, what fraction of TRUE PASS
        # we'd lose vs what fraction of TRUE FAIL we'd correctly skip
        skipped = y_proba < t  # mask of "would skip"
        kept = ~skipped

        n_pass = int((y_true == 1).sum())
        n_fail = int((y_true == 0).sum())

        # Among true PASS, how many we kept (recall on PASS)
        pass_kept = int(((y_true == 1) & kept).sum())
        pass_lost = int(((y_true == 1) & skipped).sum())

        # Among true FAIL, how many we correctly skipped
        fail_skipped = int(((y_true == 0) & skipped).sum())
        fail_kept = int(((y_true == 0) & kept).sum())

        table.append({
            "threshold": t,
            "pass_recall": pass_kept / n_pass if n_pass else 0,
            "pass_lost": pass_lost,
            "pass_lost_pct": pass_lost / n_pass if n_pass else 0,
            "fail_skipped": fail_skipped,
            "fail_skipped_pct": fail_skipped / n_fail if n_fail else 0,
            "total_skipped": int(skipped.sum()),
            "total_kept": int(kept.sum()),
        })
    return table


async def main(target_recall: float, dry_run: bool) -> int:
    print("=" * 70)
    print("Pre-simulate skeleton classifier — training")
    print("=" * 70)

    expressions, labels = await _load_training_data()
    y = np.array(labels)
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    print(f"  Loaded {len(expressions)} expressions: PASS={n_pos} FAIL={n_neg}")
    if n_pos < 50 or n_neg < 50:
        print("  ERROR: insufficient training data (need ≥50 each class)")
        return 1

    print(f"  Class ratio (FAIL/PASS): {n_neg / n_pos:.2f}x")

    print("\n[1/4] Building feature matrix...")
    X = _build_feature_matrix(expressions)
    print(f"  X shape: {X.shape}, feature_keys: {len(feature_keys())}")

    print("\n[2/4] Training LogisticRegression with class_weight=balanced...")
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(
            class_weight="balanced",
            max_iter=1000,
            random_state=42,
            solver="liblinear",  # works well for small datasets
        )),
    ])

    print("\n[3/4] 5-fold CV evaluation...")
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    y_proba_cv = cross_val_predict(
        pipeline, X, y, cv=cv, method="predict_proba", n_jobs=1,
    )[:, 1]

    auc = roc_auc_score(y, y_proba_cv)
    print(f"  AUC-ROC: {auc:.3f}")
    print()
    print("  Threshold | pass_recall | pass_lost | fail_skipped | total_kept")
    print("  ----------|-------------|-----------|--------------|----------")
    table = _build_threshold_table(y, y_proba_cv)
    for r in table:
        print(f"  {r['threshold']:9.2f} | {r['pass_recall']*100:10.1f}% | "
              f"{r['pass_lost']:3d} ({r['pass_lost_pct']*100:5.1f}%) | "
              f"{r['fail_skipped']:5d} ({r['fail_skipped_pct']*100:5.1f}%) | "
              f"{r['total_kept']}")

    # Recommend threshold meeting target_recall on PASS
    recommended = None
    for r in table:
        if r["pass_recall"] >= target_recall:
            recommended = r
    if recommended is None:
        recommended = table[0]
    print(f"\n  Recommended threshold (target_recall={target_recall}): "
          f"{recommended['threshold']} "
          f"(pass_recall={recommended['pass_recall']*100:.1f}%, "
          f"fail_skipped={recommended['fail_skipped_pct']*100:.1f}%)")

    if dry_run:
        print("\n[DRY-RUN] no model saved")
        return 0

    print("\n[4/4] Training final model on full data + saving...")
    pipeline.fit(X, y)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    model_path = _OUT_DIR / "pre_simulate_classifier.pkl"
    joblib.dump(pipeline, model_path)

    metadata = {
        "trained_at": datetime.utcnow().isoformat() + "Z",
        "n_train": len(expressions),
        "n_pos": n_pos,
        "n_neg": n_neg,
        "auc_cv": auc,
        "feature_keys": feature_keys(),
        "recommended_threshold": recommended["threshold"],
        "threshold_table": table,
        "model_class": "sklearn.linear_model.LogisticRegression",
        "preprocessing": "StandardScaler",
    }
    meta_path = _OUT_DIR / "pre_simulate_metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    print(f"  Model: {model_path}")
    print(f"  Metadata: {meta_path}")
    print(f"  Recommended threshold: {recommended['threshold']}")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-recall", type=float, default=0.95,
                    help="threshold pick: keep ≥X%% of PASS (default 0.95)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, don't save model")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(target_recall=args.target_recall, dry_run=args.dry_run)))
