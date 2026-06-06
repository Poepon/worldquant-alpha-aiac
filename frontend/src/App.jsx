import { Routes, Route, Navigate } from 'react-router-dom'
import { Layout } from 'antd'
import AppSidebar from './components/AppSidebar'
import AppHeader from './components/AppHeader'
import Dashboard from './pages/Dashboard'
// TaskManagement / TaskDetail retired in Phase 1d (pool is autonomous; /tasks → /ops/pool-pipeline)
import AlphaList from './pages/AlphaList'
import AlphaDetail from './pages/AlphaDetail'
import CrisisStressTest from './pages/CrisisStressTest'
import ConfigCenter from './pages/ConfigCenter'
import DataManagement from './pages/DataManagement'
import OpsLayout from './pages/ops/OpsLayout'

const { Content } = Layout

function App() {
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <AppSidebar />
      <Layout>
        <AppHeader />
        <Content style={{ padding: '24px', overflow: 'auto' }}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            {/* Tasks pages retired in Phase 1d — pool runs autonomously; the live
                mining view is /ops/pool-pipeline, alpha browsing is /alphas. */}
            <Route path="/tasks" element={<Navigate to="/ops/pool-pipeline" replace />} />
            <Route path="/tasks/:id" element={<Navigate to="/ops/pool-pipeline" replace />} />
            {/* /factor-library retired post tier-system removal (2026-05-18).
                /alphas is now a flat list view (no tier filter); detail page
                stays at /alphas/:id. */}
            <Route path="/alphas" element={<AlphaList />} />
            <Route path="/alphas/:id" element={<AlphaDetail />} />
            <Route path="/correlation" element={<CrisisStressTest />} />
            <Route path="/data" element={<DataManagement />} />
            <Route path="/config" element={<ConfigCenter />} />
            {/* P3 (2026-05-16): Ops Console — feature flags + monitoring */}
            <Route path="/ops/*" element={<OpsLayout />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}

export default App
