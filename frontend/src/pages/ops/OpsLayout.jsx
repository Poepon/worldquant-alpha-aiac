import { Route, Routes, Navigate } from 'react-router-dom'
import { Alert } from 'antd'

import FeatureFlagsConsole from './FeatureFlagsConsole'
import EvalDiagnostics from './EvalDiagnostics'
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
import CostMonitor from './CostMonitor'
import G3OriginalityMonitor from './G3OriginalityMonitor'
import G8ForestMonitor from './G8ForestMonitor'
// AlphaHealth / R11 / R13 merged into EvalDiagnostics (3-tab) 2026-06-08;
// G10 / G3-v2 merged into ArchivedMonitors (2-tab). Sub-components imported there.
import ArchivedMonitors from './ArchivedMonitors'
// LLMJudgeMonitor / DirectionBanditMonitor / G5CrossoverMonitor / R8v3Monitor
// retired 2026-06-07 (P0 四池重设计) — mechanisms deleted (1c) or flipped OFF (1b).
import SubmitBacklogMonitor from './SubmitBacklogMonitor'
import AutoSubmitMonitor from './AutoSubmitMonitor'
import OptimizationCyclesMonitor from './OptimizationCyclesMonitor'
// OrchestratorMonitor page retired in Phase 1c-delete follow-up
import LLMRoutingConsole from './LLMRoutingConsole'
import PoolPipelineMonitor from './PoolPipelineMonitor'
// pool-queue / pool-workers merged into PoolPipelineMonitor (3-tab) 2026-06-08;
// their routes redirect to pool-pipeline (deep-links preserved).
import SubmitYieldMonitor from './SubmitYieldMonitor'
import CognitiveReconcileMonitor from './CognitiveReconcileMonitor'
import RegimeMonitor from './RegimeMonitor'

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
          message="未配置运维访问令牌（X-Ops-Token，存于 localStorage.ops_token）。开发模式可忽略；生产环境设置访问令牌后，此处请求会被拒绝（401 未授权）。"
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          closable
        />
      )}
      <Routes>
        <Route index element={<Navigate to="overview" replace />} />
        {/* Four-pool pipeline monitor (2026-06-06 cutover; 2026-06-08 merged
            queue + workers as the 队列/工作器 tabs) — HG/S/E live health */}
        <Route path="pool-pipeline" element={<PoolPipelineMonitor />} />
        {/* pool-queue / pool-workers merged into pool-pipeline tabs — redirect. */}
        <Route path="pool-queue" element={<Navigate to="../pool-pipeline" replace />} />
        <Route path="pool-workers" element={<Navigate to="../pool-pipeline" replace />} />
        <Route path="regime-monitor" element={<RegimeMonitor />} />
        {/* P2 (2026-06-07) — submission-yield funnel + Phase-2 cognitive-reconcile status */}
        <Route path="submit-yield" element={<SubmitYieldMonitor />} />
        <Route path="cognitive-reconcile" element={<CognitiveReconcileMonitor />} />
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
        {/* 评估阶段 E 诊断 (2026-06-08 合并 Alpha健康/R11容量/R13因子透镜 为 3-tab) */}
        <Route path="alpha-health" element={<EvalDiagnostics />} />
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
        {/* G2 Phase A — LLM cost telemetry (2026-05-19) */}
        <Route path="cost-monitor" element={<CostMonitor />} />
        {/* G3 Phase A — AST originality stats (2026-05-19) */}
        <Route path="g3-monitor" element={<G3OriginalityMonitor />} />
        {/* G8 Phase A — hypothesis forest telemetry (2026-05-19) */}
        <Route path="g8-monitor" element={<G8ForestMonitor />} />
        {/* r11 / r13 merged into alpha-health (EvalDiagnostics tabs) — redirect. */}
        <Route path="r11-capacity" element={<Navigate to="../alpha-health" replace />} />
        <Route path="r13-factor-lens" element={<Navigate to="../alpha-health" replace />} />
        {/* g10 / g3v2 merged into ArchivedMonitors tabs — g3v2 redirects to g10-logic. */}
        <Route path="g10-logic" element={<ArchivedMonitors />} />
        <Route path="g3v2-monitor" element={<Navigate to="../g10-logic" replace />} />
        {/* Mining Orchestrator route retired in Phase 1c-delete follow-up */}
        {/* Catch-all: deleted/unknown ops routes (e.g. bookmarked r5-judge /
            direction-bandit-monitor / g5-monitor / r8v3-monitor, removed
            2026-06-07) → overview instead of a blank page. */}
        <Route path="*" element={<Navigate to="overview" replace />} />
      </Routes>
    </div>
  )
}
