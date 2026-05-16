import { Route, Routes, Navigate } from 'react-router-dom'
import { Alert } from 'antd'

import FeatureFlagsConsole from './FeatureFlagsConsole'
import AlphaHealthMonitor from './AlphaHealthMonitor'
import HypothesisHealthMonitor from './HypothesisHealthMonitor'
import OpsOverview from './OpsOverview'

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
        {/* Phase 1 — Feature Flag Console */}
        <Route path="feature-flags" element={<FeatureFlagsConsole />} />
        {/* Phase 2 — P1 visualizations */}
        <Route path="overview" element={<OpsOverview />} />
        <Route path="alpha-health" element={<AlphaHealthMonitor />} />
        <Route path="hypothesis-health" element={<HypothesisHealthMonitor />} />
        {/* Phase 3 — P2 pages still pending (redirect until shipped) */}
        <Route path="pillar-balance" element={<Navigate to="../overview" replace />} />
        <Route path="negative-knowledge" element={<Navigate to="../overview" replace />} />
        <Route path="macro-narratives" element={<Navigate to="../overview" replace />} />
        <Route path="regime" element={<Navigate to="../overview" replace />} />
        <Route path="llm-op-monitor" element={<Navigate to="../overview" replace />} />
      </Routes>
    </div>
  )
}
