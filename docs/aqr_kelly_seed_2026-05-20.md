# AQR / Bryan Kelly KB seed (Phase 4 Sprint 1 A4)

> **Date**:2026-05-19
> **Plan**:[`phase4_a_b_plan_v5_2026-05-19.md`](phase4_a_b_plan_v5_2026-05-19.md) §6.4
> **Files shipped**:
> - `backend/data/aqr_kelly_seed.json`(12 entries)
> - `scripts/seed_aqr_kelly_paper.py`(import + dry-run + rollback)
> - `backend/tests/unit/test_aqr_kelly_seed.py`(schema + idempotency)
> **Import batch tag**:`aqr_kelly_2026_05_20`

---

## Why these 5 papers

The Phase 4 competitive analysis v2 §5.8 identified AQR's Bryan Kelly as the
single richest academic-industry hybrid IP source AIAC could ingest for
free — 5 SSRN papers spanning factor zoo deduplication, machine learning
methodology, autoencoder factor models, and LLM-derived expected returns.
All five papers were published or extensively cited 2022-2025; they
represent the current frontier of *interpretable* ML asset pricing — the
flavor most compatible with AIAC's explainable hypothesis-driven pipeline.

| # | Paper | SSRN ID | Aspect captured |
|---|---|---|---|
| 1 | Giglio, Kelly, Xiu (2022) — *Factor Models, Machine Learning, and Asset Pricing* | [4267961](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4267961) | Factor zoo deduplication;cross-sectional predictability persistence;sector-neutralized value |
| 2 | Kelly, Xiu (2023) — *Financial Machine Learning* | [4501707](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4501707) | Tree/NN nonlinearity uplift;feature interactions;OOS regularization discipline |
| 3 | Kelly et al. — *Large (and Deep) Factor Models* | [4679269](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4679269) | Depth × breadth ablation;conditional autoencoder regime sensitivity |
| 4 | Chen, Kelly, Xiu — *Expected Returns and Large Language Models* | [4416687](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416687) | LLM sentiment from MD&A;embedding similarity as latent factor proxy |
| 5 | Gu, Kelly, Xiu — *Autoencoder Asset Pricing Models* | [AQR Working Paper](https://www.aqr.com/Insights/Research/Working-Paper/Autoencoder-Asset-Pricing-Models) | Latent factor recovery via autoencoder;industry/size/value emergence |

---

## Decision rationale

### Why SUCCESS_PATTERN + ANCHOR_METADATA mix (not all-or-nothing)

Out of 12 entries:
- **7 SUCCESS_PATTERN**(concrete BRAIN DSL expressions)— the paper either explicitly suggests a formula(e.g. industry-neutralized return zscore for autoencoder)or strongly implies one(e.g. squared rank for tree-model nonlinearity)
- **5 ANCHOR_METADATA**(English hypothesis statements)— the paper is methodological-survey grade(e.g. "factor zoo deduplication via sparse PCA");operator can later re-translate via R8 RAG + LLM per [[feedback_forward_compat_metadata_hook]]

R8 hierarchical RAG filters out ANCHOR_METADATA in the SUCCESS_PATTERN
retrieval path (per Q2 dual-path SQL), so the 5 anchor rows do not
"pollute" the LLM exemplar pool. They surface only when a future operator
explicitly queries the AQR research context (e.g. a dedicated R8 L1
pillar lookup).

### Why these specific 12 entries (not 25, not 5)

Each paper contributes 1-3 entries:
- 1 grounded "operational" entry (concrete formula reasonable for AIAC)
- 1 high-level methodology entry (anchor metadata)
- 1 composite-with-other-paper entry (where two papers' mechanisms compose)

Lower than 12 → undersamples the methodological diversity. Higher than 25
→ JSON gets noisy and dilutes the average quality / confidence. 12 was
chosen as the meeting point of "comprehensive enough to surface a useful
RAG hit for each AQR-flavored alpha query" + "small enough to manually
audit each entry's pattern/description for quality".

### Confidence scoring

All entries use `confidence ∈ [0.7, 0.95]` reflecting:
- 0.95 — anchor metadata for a methodological warning everyone agrees on (e.g. test-period regularization discipline)
- 0.85 — anchor metadata for a paper-headline finding (e.g. autoencoder dominates 5-factor)
- 0.75-0.80 — operational SUCCESS_PATTERN where the formula is well-aligned with the paper
- 0.70-0.72 — operational SUCCESS_PATTERN where the formula is a reasonable but loose embodiment (e.g. analyst-count as sentiment proxy when AIAC lacks direct text features)

No entry is set `verified=True` — these are research-derived, not BRAIN-
simulated and validated. Operator can flip `verified=True` for any entry
after BRAIN backtests confirm the pattern (1 SQL UPDATE per entry).

---

## Forward-compat metadata hooks

Per [[feedback_forward_compat_metadata_hook]] each entry's `meta_data`
contains:

```json
{
  "import_batch": "aqr_kelly_2026_05_20",
  "paper_citation": "Giglio, Kelly, Xiu (2022) ...",
  "theoretical_anchor": "Giglio-Kelly-Xiu 2022",
  "source_url": "https://papers.ssrn.com/...",
  "pattern_operators": ["rank", "group_neutralize", ...],
  "requires_role": "both",
  "regions": ["USA"]
}
```

Future operations that need to rewrite, retag, or rollback this batch
operate on a single SQL `WHERE meta_data->>'import_batch' = ...` filter
— no text matching against entries, no fuzzy lookup. The
`theoretical_anchor` field doubles as a short retrieval key for ops
console queries like "show me all AQR Kelly anchor metadata".

---

## Operator run-log placeholder

```
[ ] Step 1: scripts/seed_aqr_kelly_paper.py --dry-run
    → Confirmed 12 entries (7 SUCCESS_PATTERN + 5 ANCHOR_METADATA)
[ ] Step 2: scripts/seed_aqr_kelly_paper.py  (no --dry-run)
    → Imported: ____ / Already present: ____
[ ] Step 3: SQL verify
    → SELECT COUNT(*) FROM knowledge_entries
        WHERE meta_data->>'import_batch' = 'aqr_kelly_2026_05_20';
    → Expected: 12
[ ] Step 4: R8 retrieval test (after R8 ENABLE_HIERARCHICAL_RAG already ON)
    → POST /api/v1/ops/rag-test with query="autoencoder factor returns industry-neutralized"
    → Expected: hits at least 1 row with theoretical_anchor LIKE 'Gu-Kelly-Xiu%'
[ ] Step 5: baseline rebase
    → python backend/tests/test_suite.py --all --save-baseline
    → Commit baseline.json bump (kb_total_entries: prior + 12)
```

Rollback (if any step fails or operator regrets the import):

```sql
-- Soft rollback: deactivate (preserves audit trail)
UPDATE knowledge_entries
   SET is_active = FALSE
 WHERE meta_data->>'import_batch' = 'aqr_kelly_2026_05_20';

-- Hard rollback: delete (use only if operator confirms no downstream
-- references in alpha.metrics or trace_steps)
DELETE FROM knowledge_entries
 WHERE meta_data->>'import_batch' = 'aqr_kelly_2026_05_20';
```

---

## Idempotency guarantee

The seed script uses `ExternalKnowledgeSyncer.import_curated_patterns`,
which checks `compute_pattern_hash(pattern, region, dataset_id)` against
the UNIQUE index on `knowledge_entries.pattern_hash` and skips
already-present rows. Re-running the script is safe:

- First run:imports up to 12 new rows
- Second run(same JSON,no changes):imports 0,reports "already present: 12"
- After edit(e.g. tweak a confidence):second run imports 0(pattern_hash is computed from text, not confidence;the existing row is left untouched — operator can update via SQL or write a new entry with a new pattern variant)

---

## Audit links

- Plan section: [`phase4_a_b_plan_v5_2026-05-19.md`](phase4_a_b_plan_v5_2026-05-19.md) §6.4
- Forward-compat metadata pattern: `[[feedback_forward_compat_metadata_hook]]` (memory)
- Q2 dual-path source (anchor metadata vs SUCCESS_PATTERN): `backend/external_knowledge.py:608-629`
- pattern_hash uniqueness: `backend/models/knowledge.py:compute_pattern_hash` + Index `ix_kb_pattern_hash` (line 37)
- Competitive analysis v2 (why AQR specifically): [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md) §5.8
