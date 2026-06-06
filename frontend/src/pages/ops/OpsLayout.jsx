import { Route, Routes, Navigate } from 'react-router-dom'
import { Alert } from 'antd'

import FeatureFlagsConsole from './FeatureFlagsConsole'
import AlphaHealthMonitor from './AlphaHealthMonitor'
import HypothesisHealthMonitor from './HypothesisHealthMonitor'
import OpsOverview from './OpsOverview'
import PillarBalance from './PillarBalance'
import NegativeKnowledge from './NegativeKnowledge'
import MacroNarratives from './MacroNarratives'
// Regime page retired in Phase 1c-delete follow-up
import LLMOpMonitor from './LLMOpMonitor'
import CoSTEERMonitor from './CoSTEERMonitor'
import BrainRoleSwitch from './BrainRoleSwitch'
import SimulationCacheMonitor from './SimulationCacheMonitor'
import LLMJudgeMonitor from './LLMJudgeMonitor'
import CostMonitor from './CostMonitor'
import DirectionBanditMonitor from './DirectionBanditMonitor'
import G3OriginalityMonitor from './G3OriginalityMonitor'
import G8ForestMonitor from './G8ForestMonitor'
import G5CrossoverMonitor from './G5CrossoverMonitor'
import R8v3Monitor from './R8v3Monitor'
import R11CapacityMonitor from './R11CapacityMonitor'
import R13FactorLensMonitor from './R13FactorLensMonitor'
import G10LogicMonitor from './G10LogicMonitor'
import G3v2Monitor from './G3v2Monitor'
import SubmitBacklogMonitor from './SubmitBacklogMonitor'
import AutoSubmitMonitor from './AutoSubmitMonitor'
import OptimizationCyclesMonitor from './OptimizationCyclesMonitor'
// OrchestratorMonitor page retired in Phase 1c-delete follow-up
import LLMRoutingConsole from './LLMRoutingConsole'
import PoolPipelineMonitor from './PoolPipelineMonitor'

/**
 * OpsLayout — root for all /ops/* pages.
 *
 * P3 Phase 1 (2026-05-16) only ships Feature Flag Console; the other 8
 * sub-pages get registered as we deliver them. Until then the missing
 * routes redirect back to /ops/feature-flags so the SubMenu items don't
 * land on blank pages — clearer for ops than a 404.
 *
 * The top-of-page Alert reminds the operator to set the X-Ops-Token in
 * localStorage if backend OPS_API_TOKEN is non-empty. In dev mode (empty
 * env var) the requests succeed without it — the banner is informational.
 */
export default function OpsLayout() {
  const hasToken = (() => {
    try {
      return !!window.localStorage.getItem('ops_token')
    } catch (_) {
      return false
    }
  })()

  return (
    <div>
      {!hasToken && (
        <Alert
          message="未配置 X-Ops-Token (localStorage.ops_token)。Dev 模式可忽略;生产 OPS_API_TOKEN 设置后此处请求会 401。"
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          closable
        />
      )}
      <Routes>
        <Route index element={<Navigate to="overview" replace />} />
        {/* Four-pool pipeline monitor (2026-06-06 cutover) — HG/S/E live health */}
        <Route path="pool-pipeline" element={<PoolPipelineMonitor />} />
        {/* Submit-backlog drain (2026-05-28) — verdict-ranked can_submit queue */}
        <Route path="submit-backlog" element={<SubmitBacklogMonitor />} />
        {/* Auto-submit shadow review (2026-06-04) — would-submit list + Δscore */}
        <Route path="auto-submit" element={<AutoSubmitMonitor />} />
        {/* Phase 16-A optimization Stage A (2026-05-29) — cycles + 14d GO/STOP */}
        <Route path="optimization-cycles" element={<OptimizationCyclesMonitor />} />
        {/* Phase 1 — Feature Flag Console */}
        <Route path="feature-flags" element={<FeatureFlagsConsole />} />
        {/* LLM-Routing PR4 (2026-05-29) — per-function model routing editor */}
        <Route path="llm-routing" element={<LLMRoutingConsole />} />
        {/* Phase 2 — P1 visualizations */}
        <Route path="overview" element={<OpsOverview />} />
        <Route path="alpha-health" element={<AlphaHealthMonitor />} />
        <Route path="hypothesis-health" element={<HypothesisHealthMonitor />} />
        {/* Phase 3 — P2-A/B/C/D dashboards */}
        <Route path="pillar-balance" element={<PillarBalance />} />
        <Route path="negative-knowledge" element={<NegativeKnowledge />} />
        <Route path="macro-narratives" element={<MacroNarratives />} />
        {/* regime route retired in Phase 1c-delete follow-up */}
        {/* Phase 4 — LLM op hallucination monitor */}
        <Route path="llm-op-monitor" element={<LLMOpMonitor />} />
        {/* Phase 3 R1b — CoSTEER loop telemetry (R1a + R1b + chain depth) */}
        <Route path="costeer" element={<CoSTEERMonitor />} />
        {/* P3-Brain — BRAIN Consultant mode dedicated page (2026-05-18) */}
        <Route path="brain-role" element={<BrainRoleSwitch />} />
        {/* Phase 3 R9 — simulation cache telemetry (2026-05-18) */}
        <Route path="r9-cache" element={<SimulationCacheMonitor />} />
        {/* Phase 2 R5 — LLM judge cost + c1/c2 telemetry (2026-05-18) */}
        <Route path="r5-judge" element={<LLMJudgeMonitor />} />
        {/* G2 Phase A — LLM cost telemetry (2026-05-19) */}
        <Route path="cost-monitor" element={<CostMonitor />} />
        {/* G1 Phase A — direction-bandit telemetry (2026-05-19) */}
        <Route path="direction-bandit-monitor" element={<DirectionBanditMonitor />} />
        {/* G3 Phase A — AST originality stats (2026-05-19) */}
        <Route path="g3-monitor" element={<G3OriginalityMonitor />} />
        {/* G8 Phase A — hypothesis forest telemetry (2026-05-19) */}
        <Route path="g8-monitor" element={<G8ForestMonitor />} />
        {/* G5 Phase A — trajectory crossover telemetry (2026-05-19) */}
        <Route path="g5-monitor" element={<G5CrossoverMonitor />} />

        <Route path="r8v3-monitor" element={<R8v3Monitor />} />
        <Route path="r11-capacity" element={<R11CapacityMonitor />} />
        <Route path="r13-factor-lens" element={<R13FactorLensMonitor />} />
        <Route path="g10-logic" element={<G10LogicMonitor />} />
        <Route path="g3v2-monitor" element={<G3v2Monitor />} />
        {/* Mining Orchestrator route retired in Phase 1c-delete follow-up */}
      </Routes>
    </div>
  )
}
