"""Monthly Quasi-T1 whitelist audit — Plan v5+ §V-4 mitigation.

Quasi-T1 v1.0 ships with 15 hardcoded finance-classical two-field arithmetic
patterns. The v5+ adversarial review (V-4) flagged this as a "prior-filling
trap" — once frozen, no data-driven path lets new high-Sharpe two-field
constructs join the whitelist, even if the alpha mining loop discovers them.

This audit script runs monthly (manual trigger or via celery beat) and:
  1. Scans alphas table for "looks like two-field arithmetic + classify_tier=None
     + is_sharpe>=1.0 + quality_status in ('PASS','PASS_PROVISIONAL')"
  2. Groups candidates by structural pattern (op + arg shape)
  3. Outputs docs/quasi_t1_candidates_<YYYY-MM>.md for engineer review
  4. Engineer decides which patterns to promote into _QUASI_T1_PATTERNS v1.1

Usage:
    python scripts/quasi_t1_candidates_audit.py [--days 30] [--min-sharpe 1.0]

Status: BACKLOG STUB. Plan v5+ ships Quasi-T1 v1.0 frozen at 15 patterns;
this script is a placeholder so the monthly audit obligation is visible in
the codebase. Implementation pending Phase 1 baseline data.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="lookback window")
    parser.add_argument("--min-sharpe", type=float, default=1.0)
    args = parser.parse_args()

    print("=" * 70)
    print("Quasi-T1 Candidates Audit — STUB (Plan v5+ §V-4 backlog)")
    print("=" * 70)
    print(f"Window: last {args.days} days  /  min sharpe: {args.min_sharpe}")
    print()
    print("This script is a placeholder. Implementation pending Phase 1 baseline.")
    print()
    print("Designed flow:")
    print("  1. SELECT expression, is_sharpe, quality_status FROM alphas")
    print("     WHERE factor_tier IS NULL")
    print("       AND is_sharpe >= --min-sharpe")
    print("       AND quality_status IN ('PASS','PASS_PROVISIONAL')")
    print("       AND created_at > NOW() - INTERVAL '--days days'")
    print("  2. Parse each expression into mini-AST (reuse factor_tier_classifier")
    print("     ._top_level_call recursively)")
    print("  3. Filter to those matching '<allowed_op>(<field|nested>, <field|nested>)'")
    print("     where allowed_op in {add, subtract, multiply, divide, signed_power}")
    print("  4. Group by structural fingerprint (op + arg-type tuple)")
    print("  5. Rank groups by count + median sharpe; output top 20 to")
    print("     docs/quasi_t1_candidates_<YYYY-MM>.md with sample expressions")
    print("  6. Engineer reviews monthly; promote winners to _QUASI_T1_PATTERNS v1.1")
    print()
    print("Trigger: manual or celery beat (monthly).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
