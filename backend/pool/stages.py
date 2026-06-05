"""Pool queue stage constants + transition maps (four-pool decoupling, Phase 1b).

SINGLE SOURCE OF TRUTH for the lifecycle stage literals of hyp_intent and
candidate_queue. These strings MUST match, byte-for-byte:
  - the model server_defaults (backend/models/pipeline.py:
    HypothesisIntent.stage='PENDING', CandidateQueue.stage='PENDING_SIM'), and
  - the Phase 0 migration's partial-index WHERE clauses
    (p1d3f5a7c9e2: ix_hyp_intent_claim WHERE stage IN ('PENDING','CLAIMED');
     ix_candidate_queue_claim WHERE stage IN
     ('PENDING_SIM','SIMULATING','PENDING_EVAL','EVALUATING')).

If the in-flight stage names ever drift from the partial-index WHERE, the
claim/recycle scans seq-scan or miss rows (plan §6.5 contract). Keep them here.
"""

# --- hyp_intent (HG pool claim source) ---
INTENT_PENDING = "PENDING"
INTENT_CLAIMED = "CLAIMED"      # in-flight (HG running)
INTENT_DONE = "DONE"
INTENT_FAILED = "FAILED"
INTENT_PURGED = "PURGED"

# --- candidate_queue (HG -> S -> E lease queue) ---
SIM_PENDING = "PENDING_SIM"
SIM_INFLIGHT = "SIMULATING"
EVAL_PENDING = "PENDING_EVAL"
EVAL_INFLIGHT = "EVALUATING"
CAND_DONE = "DONE"
CAND_FAILED = "FAILED"
CAND_PURGED = "PURGED"

# claim: a PENDING-family stage -> its in-flight stage (set at claim time).
INFLIGHT_FOR = {
    INTENT_PENDING: INTENT_CLAIMED,
    SIM_PENDING: SIM_INFLIGHT,
    EVAL_PENDING: EVAL_INFLIGHT,
}

# lease-recycle: an in-flight stage -> the PENDING stage it returns to on retry.
PENDING_FOR_INFLIGHT = {
    INTENT_CLAIMED: INTENT_PENDING,
    SIM_INFLIGHT: SIM_PENDING,
    EVAL_INFLIGHT: EVAL_PENDING,
}

# Active (non-terminal) sets — MUST equal the partial-index WHERE membership.
INTENT_ACTIVE = (INTENT_PENDING, INTENT_CLAIMED)
CAND_ACTIVE = (SIM_PENDING, SIM_INFLIGHT, EVAL_PENDING, EVAL_INFLIGHT)

# Terminal stages (never re-claimed; drain purge skips in-flight, not these).
INTENT_TERMINAL = (INTENT_DONE, INTENT_FAILED, INTENT_PURGED)
CAND_TERMINAL = (CAND_DONE, CAND_FAILED, CAND_PURGED)
