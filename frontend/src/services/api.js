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

  getAlphaTrace: async (id) => {
    const { data } = await client.get(`/alphas/${id}/trace`)
    return data
  },

  // PR3: Tier-aware lineage tree + transition history (used by AlphaDetail)
  getAlphaLineage: async (id) => {
    const { data } = await client.get(`/alphas/${id}/lineage`)
    return data
  },

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

  refreshCanSubmitBatch: async (params = {}) => {
    const { data } = await client.post('/factor-library/refresh-can-submit', null, { params })
    return data
  },

  syncAlphas: async () => {
    const { data } = await client.post('/alphas/sync')
    return data
  },

  // PR3: Factor Library (tier system analytics + seed availability)
  getFactorLibraryStats: async () => {
    const { data } = await client.get('/factor-library/stats')
    return data
  },

  getFactorLibraryAlphas: async (params = {}) => {
    const { data } = await client.get('/factor-library/alphas', { params })
    return data
  },

  // Re-audit IQC marginal Δscore for the 可提交 tab. Fire-and-forget — the
  // backend enqueues Celery audits and the table is refetched after a delay.
  refreshFactorIqc: async (params = {}) => {
    const { data } = await client.post('/factor-library/refresh-iqc', null, { params })
    return data
  },

  getFactorPromotionCount: async (days = 30) => {
    const { data } = await client.get('/factor-library/promotion-count', { params: { days } })
    return data
  },

  getSeedAvailability: async (tier, region, datasetId = null) => {
    const params = { tier, region }
    if (datasetId) params.dataset_id = datasetId
    const { data } = await client.get('/factor-library/seed-availability', { params })
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

  setLLMCredentials: async (apiKey, baseUrl, model) => {
    const { data } = await client.post('/config/credentials/llm', { 
      api_key: apiKey, 
      base_url: baseUrl, 
      model 
    })
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

  // flat-F1 advanced kickoff (2026-05-18). Gated server-side by
  // ENABLE_FLAT_CONTINUOUS — flag OFF returns HTTP 400 with detail string.
  startFlatSession: async ({ region, universe, datasets = [] }) => {
    const { data } = await client.post('/ops/start-flat-session', {
      region,
      universe,
      datasets,
    })
    return data
  },

  resumeFlatSession: async (taskId) => {
    const { data } = await client.post(`/ops/flat-sessions/${taskId}/resume`)
    return data
  },
}

export default api
