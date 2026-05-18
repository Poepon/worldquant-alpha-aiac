import { Routes, Route, Navigate } from 'react-router-dom'
import { Layout } from 'antd'
import AppSidebar from './components/AppSidebar'
import AppHeader from './components/AppHeader'
import Dashboard from './pages/Dashboard'
import TaskManagement from './pages/TaskManagement'
import TaskDetail from './pages/TaskDetail'
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
            <Route path="/tasks" element={<TaskManagement />} />
            <Route path="/tasks/:id" element={<TaskDetail />} />
            {/* /factor-library retired post tier-system removal (2026-05-18).
                /alphas list redirects to /tasks; detail page stays. */}
            <Route path="/alphas" element={<Navigate to="/tasks" replace />} />
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
