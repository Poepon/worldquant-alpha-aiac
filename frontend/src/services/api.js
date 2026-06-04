import axios from 'axios'

const API_BASE = '/api/v1'

const client = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
})

// P3 (2026-05-16): inject the ops console token into every request when
// it's present in localStorage. Backend reads X-Ops-Token and matches
// against OPS_API_TOKEN env var; empty env disables the check in dev.
// We attach the header unconditionally — backend ignores it on non-/ops
// routes, and dev mode treats any value as fine.
client.interceptors.request.use((config) => {
  try {
    const token = window.localStorage.getItem('ops_token')
    if (token) {
      config.headers = config.headers || {}
      config.headers['X-Ops-Token'] = token
    }
  } catch (_) {
    // localStorage unavailable (private mode etc.) — silently skip
  }
  return config
})

// API functions
const api = {
  // Datasets & Fields
  getDatasets: async (params = {}) => {
    const { data } = await client.get('/datasets', { params })
    return data
  },

  getDataset: async (id) => {
    const { data } = await client.get(`/datasets/${id}`)
    return data
  },

  syncDatasets: async (region, universe) => {
    const { data } = await client.post('/datasets/sync', null, { params: { region, universe } })
    return data
  },

  getDatasetCategories: async () => {
    const { data } = await client.get('/datasets/categories')
    return data
  },

  getDatasetFields: async (datasetId, params = {}) => {
    const { data } = await client.get(`/datasets/${datasetId}/fields`, { params })
    return data
  },

  syncDatasetFields: async (datasetId, region, universe) => {
    const { data } = await client.post(`/datasets/${datasetId}/sync-fields`, null, { 
      params: { region, universe } 
    })
    return data
  },

  // Operators
  getOperators: async (params = {}) => {
    const { data } = await client.get('/operators', { params })
    return data
  },

  syncOperators: async () => {
    const { data } = await client.post('/operators/sync')
    return data
  },

  // Dashboard / Stats
  getDailyStats: async (date) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/stats/daily', { params })
    return data
  },

  getKPIMetrics: async () => {
    const { data } = await client.get('/stats/kpi')
    return data
  },

  getActiveTasks: async () => {
    const { data } = await client.get('/stats/active-tasks')
    return data
  },

  getSimSlots: async () => {
    const { data } = await client.get('/stats/sim-slots')
    return data
  },

  // Tasks
  getTasks: async (params = {}) => {
    const { data } = await client.get('/tasks', { params })
    return data
  },

  getTask: async (id) => {
    const { data } = await client.get(`/tasks/${id}`)
    return data
  },

  getTaskTrace: async (id) => {
    const { data } = await client.get(`/tasks/${id}/trace`)
    return data
  },

  createTask: async (taskData) => {
    const { data } = await client.post('/tasks', taskData)
    return data
  },

  startTask: async (id) => {
    const { data } = await client.post(`/tasks/${id}/start`)
    return data
  },

  interveneTask: async (id, action, parameters = {}) => {
    const { data } = await client.post(`/tasks/${id}/intervene`, { action, parameters })
    return data
  },

  getTaskRuns: async (taskId) => {
    const { data } = await client.get(`/tasks/${taskId}/runs`)
    return data
  },

  getRun: async (runId) => {
    const { data } = await client.get(`/runs/${runId}`)
    return data
  },

  getRunTrace: async (runId) => {
    const { data } = await client.get(`/runs/${runId}/trace`)
    return data
  },

  getRunAlphas: async (runId, params = {}) => {
    const { data } = await client.get(`/runs/${runId}/alphas`, { params })
    return data
  },

  // Alphas
  getAlphas: async (params = {}) => {
    const { data } = await client.get('/alphas', { params })
    return data
  },

  getAlpha: async (id) => {
    const { data } = await client.get(`/alphas/${id}`)
    return data
  },

  // Summary-strip aggregates (total + per-status + submit-state buckets).
  // Optionally scoped to a region.
  getAlphaStats: async (region) => {
    const params = region ? { region } : {}
    const { data } = await client.get('/alphas/stats', { params })
    return data
  },

  getAlphaTrace: async (id) => {
    const { data } = await client.get(`/alphas/${id}/trace`)
    return data
  },

  // Daily PnL series (cumulative + daily) from the alpha_pnl table. Empty
  // points list when none stored yet.
  getAlphaPnl: async (id) => {
    const { data } = await client.get(`/alphas/${id}/pnl`)
    return data
  },

  // Status transition history (used by AlphaDetail).
  // getAlphaLineage retired post tier-system removal (2026-05-18) — backend
  // /alphas/{id}/lineage endpoint deleted (Ship #3).
  getAlphaTransitions: async (id, limit = 50) => {
    const { data } = await client.get(`/alphas/${id}/transitions`, { params: { limit } })
    return data
  },

  // IQC submission strategy — standalone vs merged marginal-contribution.
  // BRAIN: GET /{scope}/alphas/{brain_id}/before-and-after-performance
  // scope defaults to users/self; pass competition or team_id to scope.
  getAlphaMarginalContribution: async (id, { competition, teamId } = {}) => {
    const params = {}
    if (competition) params.competition = competition
    if (teamId) params.team_id = teamId
    const { data } = await client.get(`/alphas/${id}/marginal-contribution`, { params })
    return data
  },

  submitAlphaFeedback: async (id, rating, comment = null) => {
    const { data } = await client.post(`/alphas/${id}/feedback`, { rating, comment })
    return data
  },

  refreshCanSubmit: async (id) => {
    const { data } = await client.post(`/alphas/${id}/refresh-can-submit`)
    return data
  },

  // Submit an alpha to BRAIN. Server runs pre-flight gates (can_submit,
  // not-already-submitted, self_corr < 0.7); a gate failure comes back as
  // { submitted: false, reason } with HTTP 200, not an error.
  submitAlpha: async (id) => {
    const { data } = await client.post(`/alphas/${id}/submit`)
    return data
  },

  // Bulk maintenance endpoints (migrated from /factor-library/* to /alphas/*
  // post tier-system removal, 2026-05-18).
  refreshCanSubmitBatch: async (params = {}) => {
    const { data } = await client.post('/alphas/refresh-can-submit', null, { params })
    return data
  },

  // Re-audit IQC marginal Δscore for the 可提交 tab. Fire-and-forget — the
  // backend enqueues Celery audits and the table is refetched after a delay.
  refreshFactorIqc: async (params = {}) => {
    const { data } = await client.post('/alphas/refresh-iqc', null, { params })
    return data
  },

  syncAlphas: async () => {
    const { data } = await client.post('/alphas/sync')
    return data
  },

  // Knowledge
  getKnowledgeEntries: async (params = {}) => {
    const { data } = await client.get('/knowledge', { params })
    return data
  },

  getSuccessPatterns: async (limit = 20) => {
    const { data } = await client.get('/knowledge/success-patterns', { params: { limit } })
    return data
  },

  getFailurePitfalls: async (limit = 50) => {
    const { data } = await client.get('/knowledge/failure-pitfalls', { params: { limit } })
    return data
  },

  createKnowledgeEntry: async (entryData) => {
    const { data } = await client.post('/knowledge', entryData)
    return data
  },

  updateKnowledgeEntry: async (id, updates) => {
    const { data } = await client.put(`/knowledge/${id}`, updates)
    return data
  },

  deleteKnowledgeEntry: async (id) => {
    const { data } = await client.delete(`/knowledge/${id}`)
    return data
  },

  // Config
  getConfig: async () => {
    const { data } = await client.get('/config')
    return data
  },

  updateThresholds: async (thresholds) => {
    const { data } = await client.put('/config/thresholds', thresholds)
    return data
  },

  // Credentials Management
  getCredentialsStatus: async () => {
    const { data } = await client.get('/config/credentials')
    return data
  },

  setBrainCredentials: async (email, password) => {
    const { data } = await client.post('/config/credentials/brain', { email, password })
    return data
  },

  testBrainCredentials: async () => {
    const { data } = await client.post('/config/credentials/brain/test')
    return data
  },

  deleteCredential: async (key) => {
    const { data } = await client.delete(`/config/credentials/${key}`)
    return data
  },

  // LLM Provider registry (named endpoint+key profiles for per-function routing)
  listLLMProviders: async () => {
    const { data } = await client.get('/config/llm-providers')
    return data
  },

  saveLLMProvider: async ({ name, label, sdk, baseUrl, apiKey }) => {
    const body = { name, label, sdk, base_url: baseUrl }
    // Omit api_key when blank so an edit keeps the existing stored secret.
    if (apiKey) body.api_key = apiKey
    const { data } = await client.post('/config/llm-providers', body)
    return data
  },

  deleteLLMProvider: async (name) => {
    const { data } = await client.delete(`/config/llm-providers/${name}`)
    return data
  },

  // Crisis-window correlation stress test
  listCrisisWindows: async () => {
    const { data } = await client.get('/correlation/windows')
    return data
  },

  getPortfolioMatrix: async (region = 'USA', window = null) => {
    const params = { region }
    if (window) params.window = window
    const { data } = await client.get('/correlation/portfolio-matrix', { params })
    return data
  },

  getCrisisSummary: async (region = 'USA', { refresh = false, topNHotspots = 20 } = {}) => {
    const { data } = await client.get('/correlation/crisis-summary', {
      params: { region, refresh, top_n_hotspots: topNHotspots },
    })
    return data
  },

  getAlphaCrisisCorrelations: async (alphaId, region = 'USA') => {
    const { data } = await client.get(`/correlation/alpha/${alphaId}/crisis`, {
      params: { region },
    })
    return data
  },

  // ---------------------------------------------------------------------
  // Ops Console (P3 — 2026-05-16)
  // Feature flags + manual task triggers backing /ops/* dashboards.
  // ---------------------------------------------------------------------

  // Feature flags
  listFeatureFlags: async () => {
    const { data } = await client.get('/ops/flags')
    return data
  },

  setFeatureFlag: async (name, value, note = null) => {
    const { data } = await client.patch(`/ops/flags/${name}`, { value, note })
    return data
  },

  clearFeatureFlag: async (name) => {
    const { data } = await client.delete(`/ops/flags/${name}/override`)
    return data
  },

  listFeatureFlagAudit: async (limit = 50) => {
    const { data } = await client.get('/ops/flags/audit', { params: { limit } })
    return data
  },

  refreshAllFlags: async () => {
    const { data } = await client.post('/ops/flags/refresh-all')
    return data
  },

  // BRAIN role switch (P3-Brain — manual Consultant mode toggle)
  getBrainRoleState: async () => {
    const { data } = await client.get('/ops/brain/role-state')
    return data
  },

  activateConsultant: async () => {
    const { data } = await client.post('/ops/brain/activate-consultant')
    return data
  },

  deactivateConsultant: async () => {
    const { data } = await client.post('/ops/brain/deactivate-consultant')
    return data
  },

  // Ops task triggers
  triggerOpsTask: async (name, kwargs = null) => {
    const { data } = await client.post('/ops/tasks/trigger', { name, kwargs })
    return data
  },

  listRecentOpsRuns: async (taskName = null, limit = 20) => {
    const params = { limit }
    if (taskName) params.task_name = taskName
    const { data } = await client.get('/ops/tasks/recent-runs', { params })
    return data
  },

  // Mining Orchestrator (Phase 1 Sub-phase 4 — 2026-05-29)
  getOrchestratorStatus: async () => {
    const { data } = await client.get('/ops/orchestrator/status')
    return data
  },

  // Ops Phase 2 — Alpha Health
  getOpsAlphaHealthLatest: async (date = null) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/ops/alpha-health/latest', { params })
    return data
  },

  getOpsAlphaHealthHistory: async (days = 30) => {
    const { data } = await client.get('/ops/alpha-health/history', { params: { days } })
    return data
  },

  getOpsAlphaHealthRecords: async ({ band = null, region = null, limit = 200, date = null } = {}) => {
    const params = { limit }
    if (band) params.band = band
    if (region) params.region = region
    if (date) params.date = date
    const { data } = await client.get('/ops/alpha-health/alphas', { params })
    return data
  },

  rerunOpsAlphaHealth: async () => {
    const { data } = await client.post('/ops/alpha-health/rerun')
    return data
  },

  // Ops Phase 2 — Hypothesis Health
  getOpsHypothesisHealthLatest: async (date = null) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/ops/hypothesis-health/latest', { params })
    return data
  },

  getOpsHypothesisHealthHistory: async (days = 30) => {
    const { data } = await client.get('/ops/hypothesis-health/history', { params: { days } })
    return data
  },

  getOpsHypothesisTransitions: async (hypothesisId = null, limit = 100) => {
    const params = { limit }
    if (hypothesisId) params.hypothesis_id = hypothesisId
    const { data } = await client.get('/ops/hypothesis-health/transitions', { params })
    return data
  },

  rerunOpsHypothesisHealth: async () => {
    const { data } = await client.post('/ops/hypothesis-health/rerun')
    return data
  },

  // Ops Phase 2 — Overview
  getOpsOverview: async () => {
    const { data } = await client.get('/ops/overview')
    return data
  },

  // Ops Phase 3 — P2-B Pillar Balance
  getOpsPillarLatest: async (date = null) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/ops/pillar/latest', { params })
    return data
  },
  getOpsPillarHistory: async (days = 14) => {
    const { data } = await client.get('/ops/pillar/history', { params: { days } })
    return data
  },
  getOpsPillarDeficit: async (region, skewThreshold = 0) => {
    const { data } = await client.get('/ops/pillar/deficit-recommendation', {
      params: { region, skew_threshold: skewThreshold },
    })
    return data
  },
  rerunOpsPillar: async () => {
    const { data } = await client.post('/ops/pillar/rerun')
    return data
  },

  // Ops Phase 3 — P2-D Negative Knowledge
  getOpsNegativeTop: async ({ region = null, category = null, limit = 20 } = {}) => {
    const params = { limit }
    if (region) params.region = region
    if (category) params.category = category
    const { data } = await client.get('/ops/negative-knowledge/top', { params })
    return data
  },
  getOpsNegativeCategoryBreakdown: async (region = null) => {
    const params = region ? { region } : {}
    const { data } = await client.get('/ops/negative-knowledge/category-breakdown', { params })
    return data
  },
  getOpsNegativeTimeline: async (days = 30, region = null) => {
    const params = { days }
    if (region) params.region = region
    const { data } = await client.get('/ops/negative-knowledge/timeline', { params })
    return data
  },
  togglePitfall: async (entryId, isActive) => {
    const { data } = await client.patch(
      `/ops/negative-knowledge/entries/${entryId}`,
      { is_active: isActive },
    )
    return data
  },
  rerunOpsNegative: async () => {
    const { data } = await client.post('/ops/negative-knowledge/rerun')
    return data
  },

  // Ops Phase 3 — P2-A Macro Narrative
  getOpsMacroLatest: async (date = null) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/ops/macro/latest', { params })
    return data
  },
  getOpsMacroCoverage: async () => {
    const { data } = await client.get('/ops/macro/coverage')
    return data
  },
  getOpsMacroByScope: async (scope, { datasetCategory = null, limit = 200 } = {}) => {
    const params = { scope, limit }
    if (datasetCategory) params.dataset_category = datasetCategory
    const { data } = await client.get('/ops/macro/by-scope', { params })
    return data
  },
  getOpsMacroTokenBudget: async (utcDate = null) => {
    const params = utcDate ? { utc_date: utcDate } : {}
    const { data } = await client.get('/ops/macro/token-budget', { params })
    return data
  },
  rerunOpsMacro: async () => {
    const { data } = await client.post('/ops/macro/rerun')
    return data
  },

  // Ops Phase 3 — P2-C Regime
  getOpsRegimeCurrent: async (region = 'USA') => {
    const { data } = await client.get('/ops/regime/current', { params: { region } })
    return data
  },
  getOpsRegimeSnapshot: async (region = 'USA') => {
    const { data } = await client.get('/ops/regime/snapshot', { params: { region } })
    return data
  },
  getOpsRegimeHistory: async (region = 'USA', days = 14) => {
    const { data } = await client.get('/ops/regime/history', {
      params: { region, days },
    })
    return data
  },
  rerunOpsRegime: async (region = null) => {
    const params = region ? { region } : {}
    const { data } = await client.post('/ops/regime/rerun', null, { params })
    return data
  },

  // Ops Phase 4 — LLM op hallucination monitor
  getOpsLLMOpLatest: async (date = null) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/ops/llm-op/latest', { params })
    return data
  },
  getOpsLLMOpDeactivatedKB: async (date = null) => {
    const params = date ? { date } : {}
    const { data } = await client.get('/ops/llm-op/deactivated-kb', { params })
    return data
  },
  rerunOpsLLMOp: async () => {
    const { data } = await client.post('/ops/llm-op/rerun')
    return data
  },

  // CoSTEER loop telemetry — R1a + R1b + chain depth (2026-05-18)
  getOpsR1aTelemetry: async (days = 7) => {
    const { data } = await client.get('/ops/r1a/telemetry', { params: { days } })
    return data
  },
  getOpsR1bTelemetry: async (days = 7, topN = 5) => {
    const { data } = await client.get('/ops/r1b/telemetry', {
      params: { days, top_n: topN },
    })
    return data
  },
  getOpsR1bChainDepth: async () => {
    const { data } = await client.get('/ops/r1b/chain-depth-distribution')
    return data
  },
  getOpsR8KbShape: async () => {
    const { data } = await client.get('/ops/r8/kb-shape')
    return data
  },
  getOpsR8QueryStats: async (days = 7) => {
    const { data } = await client.get('/ops/r8/query-stats', { params: { days } })
    return data
  },
  getOpsCoSTEERDeployRecommendation: async (days = 7) => {
    const { data } = await client.get('/ops/costeer/deploy-recommendation', {
      params: { days },
    })
    return data
  },

  // R9 simulation cache telemetry (2026-05-18)
  getOpsR9CacheStats: async (days = 7) => {
    const { data } = await client.get('/ops/r9/cache-stats', { params: { days } })
    return data
  },

  // R5 LLM Judge telemetry (2026-05-18) — complements /ops/r1a/telemetry
  // with per-judge cost + c1/c2 internal agreement + composite distribution
  getOpsR5JudgeStats: async (days = 7) => {
    const { data } = await client.get('/ops/r5/judge-stats', { params: { days } })
    return data
  },


  // G2 Phase A cost telemetry (2026-05-19) — per-call LLM cost across all callers
  // (普通 round + R1b + macro + R5 + future). Window-aggregated by_model /
  // by_node_key / by_pillar plus 24h hourly bucket. Healthy gate: flag ON +
  // total_calls > 0 + error_rate ≤ 0.10.
  getOpsCostTelemetry: async (days = 7, topN = 10) => {
    const { data } = await client.get('/ops/cost/telemetry', {
      params: { days, top_n: topN },
    })
    return data
  },

  // G1 Phase A direction-bandit telemetry (2026-05-19) — per-arm pulls /
  // observed reward / PASS rate joined from alphas.metrics. GO-gate readiness
  // signal exposed via go_gate_segments_ready.
  getOpsDirectionBanditTelemetry: async (days = 7, topSegments = 10) => {
    const { data } = await client.get('/ops/direction-bandit/telemetry', {
      params: { days, top_segments: topSegments },
    })
    return data
  },

  // G3 Phase A AST originality stats (2026-05-19) — shadow-mode block rate at
  // current τ + min_distance histogram + top-N nearest-neighbor + per-pillar
  // block rate. Operator uses this to calibrate τ before promoting MODE.
  getOpsG3OriginalityStats: async (days = 7, histogramBins = 10, topNeighbors = 10) => {
    const { data } = await client.get('/ops/g3/originality-stats', {
      params: {
        days,
        histogram_bins: histogramBins,
        top_neighbors: topNeighbors,
      },
    })
    return data
  },

  // G8 Phase A hypothesis forest telemetry (2026-05-19) — eligible pool +
  // top-N entries + per-pillar breakdown + reverse-attribution stats
  // (alphas.metrics._g8_forest_referenced_ids). Healthy gate: flag ON +
  // eligible_count > 0 + total_referenced_alphas > 0.
  getOpsHypothesisForest: async (
    days = 7,
    region = 'USA',
    topN = 10,
    minPassCount = 2,
    minSharpeAvg = 1.0,
  ) => {
    const params = {
      days,
      top_n: topN,
      min_pass_count: minPassCount,
      min_sharpe_avg: minSharpeAvg,
    }
    if (region) params.region = region
    const { data } = await client.get('/ops/hypothesis/forest', { params })
    return data
  },

  // G5 Phase A trajectory crossover telemetry (2026-05-19) — per-strategy +
  // per-pillar-pair calls / offspring volume / PASS rate (joined from
  // alphas.metrics._g5_crossover_parent_ids). Healthy gate: flag ON +
  // total_crossover_calls > 0 + offspring_pass_rate > 0.
  getOpsG5CrossoverStats: async (days = 7) => {
    const { data } = await client.get('/ops/g5/crossover-stats', {
      params: { days },
    })
    return data
  },

  // flat-F1 advanced kickoff (2026-05-18). Gated server-side by
  // ENABLE_FLAT_CONTINUOUS — flag OFF returns HTTP 400 with detail string.
  startFlatSession: async ({ region, universe, datasets = [], delay = 1, dailyGoal = null }) => {
    const { data } = await client.post('/ops/start-flat-session', {
      region,
      universe,
      datasets,
      delay,
      // null/absent → backend falls back to the global FLAT_CONTINUOUS_DAILY_GOAL.
      ...(dailyGoal != null ? { daily_goal: dailyGoal } : {}),
    })
    return data
  },

  resumeFlatSession: async (taskId) => {
    const { data } = await client.post(`/ops/flat-sessions/${taskId}/resume`)
    return data
  },

  pauseFlatSession: async (taskId) => {
    const { data } = await client.post(`/ops/flat-sessions/${taskId}/pause`)
    return data
  },

  // ---- Phase 4 Sprint 3-5 + Tier B/C telemetry ----
  // R8-v3 cognitive layer per-layer fire + PASS rate (Sprint 3 B5).
  getOpsR8v3CognitiveLayerStats: async (days = 7) => {
    const { data } = await client.get('/ops/r8-v3/cognitive-layer-stats', {
      params: { days },
    })
    return data
  },
  // R11 alpha-capacity log-scale histogram + PASS rate (Sprint 2 B1 / Tier B).
  getOpsR11CapacityStats: async (days = 7) => {
    const { data } = await client.get('/ops/r11/capacity-stats', { params: { days } })
    return data
  },
  // R13 factor-lens residual-sharpe distribution (Sprint 2 B2 / Tier B).
  getOpsR13FactorResiduals: async (days = 7) => {
    const { data } = await client.get('/ops/r13/factor-residuals', { params: { days } })
    return data
  },
  // R13 factor-returns snapshot staleness (Tier B). No DB — filesystem mtime.
  getOpsR13SnapshotStaleCheck: async (staleDays = 90) => {
    const { data } = await client.get('/ops/r13/snapshot-stale-check', {
      params: { stale_days: staleDays },
    })
    return data
  },
  // G10 distilled-logic library (Sprint 3 A5.1 / Sprint 4 A5.2).
  getOpsG10LogicLibrary: async (
    { days = 28, region = null, pillar = null, activeOnly = true, limit = 100 } = {},
  ) => {
    const params = { days, active_only: activeOnly, limit }
    if (region) params.region = region
    if (pillar) params.pillar = pillar
    const { data } = await client.get('/ops/g10/logic-library', { params })
    return data
  },
  // G3-v2 grammar parse telemetry (Sprint 4 B4.1 / Tier B).
  getOpsG3v2ParseStats: async (days = 7) => {
    const { data } = await client.get('/ops/g3v2/parse-stats', { params: { days } })
    return data
  },

  // Submit-backlog drain (2026-05-28) — verdict-ranked can_submit queue.
  getOpsSubmitBacklog: async (region = null) => {
    const params = {}
    if (region) params.region = region
    const { data } = await client.get('/ops/submit-backlog', { params })
    return data
  },
  // Kick a one-pass IQC marginal re-audit across the backlog (BRAIN-backed,
  // worker-async). Returns the enqueued count; re-poll getOpsSubmitBacklog.
  scanSubmitBacklog: async (limit = 200) => {
    const { data } = await client.post('/ops/submit-backlog/scan', null, { params: { limit } })
    return data
  },
  // Set-level ORTHOGONAL drain order (P0-1, 2026-06-03) — among the clean
  // backlog (can_submit, unsubmitted, self_corr<threshold, margin≥min), greedily
  // orders submissions to maximise incremental breadth (lowest max-corr to the
  // already-selected ∪ submitted set). Zero BRAIN cost (local PnL + stored
  // self_corr). Returns { selected[], blocked[], n_* counts, note }.
  getOpsSubmitBacklogDrainOrder: async ({ region = null, marginBpsMin = 5, threshold = 0.7 } = {}) => {
    const params = { margin_bps_min: marginBpsMin, threshold }
    if (region) params.region = region
    const { data } = await client.get('/ops/submit-backlog/drain-order', { params })
    return data
  },
  // Auto-submit audit (2026-06-04) — the shadow would-submit review surface.
  // Returns { enabled, mode, tally_24h, count, items[] } where each item carries
  // outcome + gate_results.{gates,signals} (incl. competition delta_score). Use
  // outcome='would_submit' to review the shadow list before flipping to live.
  getOpsAutoSubmitAudit: async ({ outcome = null, region = null, limit = 100 } = {}) => {
    const params = { limit }
    if (outcome) params.outcome = outcome
    if (region) params.region = region
    const { data } = await client.get('/ops/auto-submit/audit', { params })
    return data
  },

  // Marginal-value reconciliation (methodology-audit kill-switch, 2026-06-03) —
  // does our OFFLINE marginal ΔSharpe agree (sign+rank) with BRAIN's
  // AUTHORITATIVE before-and-after Δsharpe? verdict ∈ supported|weak|FALSIFIED|
  // insufficient_sample; sign-agreement ≤ 60% ⇒ stop routing on the offline proxy.
  getOpsMarginalReconciliation: async ({ region = 'USA', limit = 300 } = {}) => {
    const params = { limit }
    if (region) params.region = region
    const { data } = await client.get('/ops/marginal-reconciliation', { params })
    return data
  },

  // Forward-test reconciliation (2026-06-03) — predictions FROZEN at submit time
  // (metrics._recon_predicted_delta_sharpe). predicted↔BRAIN computable now;
  // predicted↔realized structurally blocked (no live post-submission PnL).
  getOpsMarginalReconciliationForward: async ({ region = null, limit = 300 } = {}) => {
    const params = { limit }
    if (region) params.region = region
    const { data } = await client.get('/ops/marginal-reconciliation/forward', { params })
    return data
  },

  // Dataset breadth coverage (P1, 2026-06-03) — per-dataset available/in-rotation/
  // untapped/new at a (region,universe,delay) cell. Surfaces fresh & starved
  // orthogonal data surfaces (e.g. a new 375-field dataset) to force-mine.
  getOpsDatasetsCoverage: async ({ region = 'USA', universe = 'TOP3000', delay = 1, newWithinDays = 3 } = {}) => {
    const params = { universe, delay, new_within_days: newWithinDays }
    if (region) params.region = region
    const { data } = await client.get('/ops/datasets/coverage', { params })
    return data
  },

  // Optimization closure Stage A (2026-05-29) — cycles + 14d conversion rate.
  // Phase 16-A telemetry for the GO/STOP gate. conversion_rate_14d > 20% →
  // Stage B; < 10% → STOP. ENABLE_OPTIMIZATION_LOOP=False → empty cycles.
  getOpsOptimizationCycles: async (days = 14, limit = 50) => {
    const { data } = await client.get('/ops/optimization/cycles', {
      params: { days, limit },
    })
    return data
  },
  // Stop the in-flight Stage A beat. Sets Redis abort flag (consumed
  // one-shot by worker between cycles) + stamps all RUNNING cycle rows
  // error='aborted_by_user:batch'. Does NOT flip ENABLE_OPTIMIZATION_LOOP.
  abortOpsOptimizationBatch: async () => {
    const { data } = await client.post('/ops/optimization/abort-batch')
    return data
  },
  // Unified Stage A toggle (2026-05-31). /stop = flag OFF + abort batch +
  // Redis flag (3-step no-auto-restart guarantee). /start = flag ON.
  // Both are audit-noted via FeatureFlagService.set with actor + note.
  startOpsOptimization: async () => {
    const { data } = await client.post('/ops/optimization/start')
    return data
  },
  stopOpsOptimization: async () => {
    const { data } = await client.post('/ops/optimization/stop')
    return data
  },

  // Blueprint optimization (2026-06-03) — use a chosen alpha as a blueprint and
  // run ONE settings-sweep optimization cycle (trigger_source='manual'). Runs
  // INDEPENDENTLY of the 6h beat (ENABLE_OPTIMIZATION_LOOP). Dispatches a Celery
  // cycle; winners land in the submit-backlog (never auto-submitted). Returns a
  // preview { n_variants, variant_tags, budget } + the dispatched task_id.
  // 409 if a cycle for this alpha is already in flight; 404 unknown alpha.
  optimizeAlphaFromBlueprint: async (alphaId, { budget = null } = {}) => {
    const body = {}
    if (budget != null) body.budget = budget
    const { data } = await client.post(`/alphas/${alphaId}/optimize`, body)
    return data
  },
}

export default api
