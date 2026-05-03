"""Pilot baseline query for Spike — Plan v5+ V-3 mitigation.

Queries the current alpha table to establish baseline distributions
before launching Quasi-T1 implementation. Output drives Gate 1-4
threshold calibration in spike_baseline_report.md.
"""
import psycopg2

conn = psycopg2.connect(
    host="localhost", port=5433,
    user="postgres", password="postgres",
    dbname="alpha_gpt",
)
cur = conn.cursor()

print("=" * 70)
print("Plan v5+ Pilot Baseline — current alphas table snapshot")
print("=" * 70)

# Aggregate stats
cur.execute("""
    SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE quality_status = 'PASS') AS passed,
        COUNT(*) FILTER (WHERE quality_status = 'PASS_PROVISIONAL') AS prov,
        COUNT(*) FILTER (WHERE quality_status = 'PASS' AND can_submit = TRUE) AS pass_can_submit,
        COUNT(*) FILTER (WHERE factor_tier = 1) AS t1,
        COUNT(*) FILTER (WHERE factor_tier = 1 AND quality_status='PASS') AS t1_pass,
        COUNT(*) FILTER (WHERE factor_tier = 2) AS t2,
        COUNT(*) FILTER (WHERE factor_tier = 2 AND quality_status='PASS') AS t2_pass,
        COUNT(*) FILTER (WHERE factor_tier = 3) AS t3,
        COUNT(*) FILTER (WHERE factor_tier = 3 AND quality_status='PASS') AS t3_pass,
        COUNT(*) FILTER (WHERE factor_tier IS NULL) AS tier_none,
        COUNT(*) FILTER (WHERE factor_tier IS NULL AND quality_status='PASS') AS tier_none_pass
    FROM alphas
""")
row = cur.fetchone()
cols = [d[0] for d in cur.description]
agg = dict(zip(cols, row))
print("\n[Aggregate]")
for k, v in agg.items():
    print(f"  {k:30} = {v}")

total = agg["total"] or 1
passed = agg["passed"] or 0
prov = agg["prov"] or 0
print(f"\n  PASS rate              = {passed/total*100:.2f}%")
print(f"  PASS+PROV rate         = {(passed+prov)/total*100:.2f}%")
if passed:
    print(f"  can_submit rate        = {agg['pass_can_submit']/passed*100:.2f}% (of PASS)")
print(f"  T1 PASS rate           = {agg['t1_pass']/(agg['t1'] or 1)*100:.2f}% (of T1)")
print(f"  T2 PASS rate           = {agg['t2_pass']/(agg['t2'] or 1)*100:.2f}% (of T2)")
print(f"  T3 PASS rate           = {agg['t3_pass']/(agg['t3'] or 1)*100:.2f}% (of T3)")
print(f"  tier=None PASS rate    = {agg['tier_none_pass']/(agg['tier_none'] or 1)*100:.2f}% (of None)")

# Cross-dataset rate — alpha.fields_used (jsonb array) spanning ≥2 datasets
cur.execute("""
    WITH alpha_field_dsets AS (
        SELECT
            a.id,
            a.quality_status,
            COUNT(DISTINCT df.dataset_id) AS n_datasets
        FROM alphas a
        LEFT JOIN LATERAL jsonb_array_elements_text(a.fields_used) AS f(field_id) ON TRUE
        LEFT JOIN datafields df ON df.field_id = f.field_id AND df.region = a.region
        WHERE a.fields_used IS NOT NULL
          AND jsonb_typeof(a.fields_used) = 'array'
          AND jsonb_array_length(a.fields_used) > 0
        GROUP BY a.id, a.quality_status
    )
    SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE n_datasets >= 2) AS cross_dataset,
        COUNT(*) FILTER (WHERE n_datasets >= 2 AND quality_status='PASS') AS cross_pass
    FROM alpha_field_dsets
""")
row = cur.fetchone()
cd_total, cd_cross, cd_pass = row
print("\n[Cross-dataset]")
print(f"  total alphas with fields_used   = {cd_total}")
print(f"  cross-dataset alphas (≥2 dsets) = {cd_cross} ({cd_cross/(cd_total or 1)*100:.2f}%)")
print(f"  cross-dataset PASS              = {cd_pass}")

# Recent task / time window
cur.execute("""
    SELECT MIN(created_at), MAX(created_at), COUNT(DISTINCT task_id)
    FROM alphas
""")
mn, mx, ntask = cur.fetchone()
print(f"\n[Time window]  {mn}  →  {mx}  ({ntask} distinct tasks)")

# Per-tier sharpe distribution (PASS only)
cur.execute("""
    SELECT
        factor_tier,
        COUNT(*) AS n,
        ROUND(AVG(is_sharpe)::numeric, 3) AS avg_sharpe,
        ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY is_sharpe)::numeric, 3) AS median_sharpe,
        ROUND(MAX(is_sharpe)::numeric, 3) AS max_sharpe
    FROM alphas
    WHERE quality_status='PASS' AND is_sharpe IS NOT NULL
    GROUP BY factor_tier
    ORDER BY factor_tier NULLS LAST
""")
print("\n[Sharpe by tier — PASS only]")
print(f"  {'tier':<10}{'n':<8}{'avg':<10}{'median':<10}{'max':<10}")
for r in cur.fetchall():
    print(f"  {str(r[0]):<10}{r[1]:<8}{str(r[2]):<10}{str(r[3]):<10}{str(r[4]):<10}")

cur.close()
conn.close()
