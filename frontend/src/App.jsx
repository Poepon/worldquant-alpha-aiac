import { Routes, Route, Navigate } from 'react-router-dom'
import { Layout } from 'antd'
import AppSidebar from './components/AppSidebar'
import AppHeader from './components/AppHeader'
import Dashboard from './pages/Dashboard'
import TaskManagement from './pages/TaskManagement'
import TaskDetail from './pages/TaskDetail'
import AlphaDetail from './pages/AlphaDetail'
import FactorLibrary from './pages/FactorLibrary'
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
            {/* 因子实验室已并入因子库；/alphas 列表页重定向，详情页保留 */}
            <Route path="/alphas" element={<Navigate to="/factor-library" replace />} />
            <Route path="/alphas/:id" element={<AlphaDetail />} />
            <Route path="/factor-library" element={<FactorLibrary />} />
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
