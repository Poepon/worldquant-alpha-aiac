import axios from 'axios'

const API_BASE = '/api/v1'

const client = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
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

  // V-19 Persistent Mining Service singleton-per-region
  // Single button start/stop. Backend enforces uniqueness via partial index.
  listMiningSessions: async () => {
    const { data } = await client.get('/mining-session')
    return data
  },

  getMiningSession: async (region) => {
    try {
      const { data } = await client.get(`/mining-session/${region}`)
      return data
    } catch (err) {
      if (err?.response?.status === 404) return null
      throw err
    }
  },

  startMiningSession: async ({ region = 'USA', universe = 'TOP3000' } = {}) => {
    const { data } = await client.post('/mining-session/start', { region, universe })
    return data
  },

  stopMiningSession: async (taskId) => {
    const { data } = await client.post('/mining-session/stop', { task_id: taskId })
    return data
  },

  resumeMiningSession: async (taskId) => {
    const { data } = await client.post('/mining-session/resume', { task_id: taskId })
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
}

export default api
