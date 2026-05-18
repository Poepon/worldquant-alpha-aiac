import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu } from 'antd'
import {
  DashboardOutlined,
  ThunderboltOutlined,
  SettingOutlined,
  RocketOutlined,
  DatabaseOutlined,
  WarningOutlined,
  MonitorOutlined,
  SwapOutlined,
  ExperimentOutlined,
  BranchesOutlined,
  FunctionOutlined,
} from '@ant-design/icons'

const { Sider } = Layout

// P3 (2026-05-16): "运维监控" SubMenu groups the 9 ops-console pages so the
// sidebar stays at 7 top-level entries instead of bloating to 14+. The
// sub-pages map 1:1 to /ops/* routes registered in App.jsx.
const menuItems = [
  {
    key: '/dashboard',
    icon: <DashboardOutlined />,
    label: '仪表盘',
  },
  {
    key: '/tasks',
    icon: <ThunderboltOutlined />,
    label: '任务管理',
  },
  {
    key: '/alphas',
    icon: <FunctionOutlined />,
    label: 'Alpha 列表',
  },
  {
    key: '/correlation',
    icon: <WarningOutlined />,
    label: '危机压力测试',
  },
  {
    key: '/data',
    icon: <DatabaseOutlined />,
    label: '数据管理',
  },
  {
    key: '/ops',
    icon: <MonitorOutlined />,
    label: '运维监控',
    children: [
      { key: '/ops/overview', label: '总览' },
      { key: '/ops/feature-flags', label: 'Feature Flag' },
      { key: '/ops/alpha-health', label: 'Alpha 健康度' },
      { key: '/ops/hypothesis-health', label: 'Hypothesis 触发器' },
      { key: '/ops/pillar-balance', label: '五支柱平衡' },
      { key: '/ops/negative-knowledge', label: '失败模式沉淀' },
      { key: '/ops/macro-narratives', label: '宏观叙事' },
      { key: '/ops/regime', label: '市场体制' },
      { key: '/ops/llm-op-monitor', label: 'LLM 算子监控' },
      { key: '/ops/costeer', label: 'CoSTEER 循环监控 (R1a/R1b)' },
      { key: '/ops/r5-judge', icon: <ExperimentOutlined />, label: 'R5 LLM Judge' },
      { key: '/ops/r6-dag', icon: <BranchesOutlined />, label: 'R6 DAG Trace' },
      { key: '/ops/r9-cache', icon: <DatabaseOutlined />, label: 'R9 模拟缓存' },
      { key: '/ops/brain-role', icon: <SwapOutlined />, label: 'BRAIN 模式' },
    ],
  },
  {
    key: '/config',
    icon: <SettingOutlined />,
    label: '配置中心',
  },
]

export default function AppSidebar() {
  const [collapsed, setCollapsed] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  const handleMenuClick = ({ key }) => {
    navigate(key)
  }

  return (
    <Sider
      collapsible
      collapsed={collapsed}
      onCollapse={setCollapsed}
      style={{
        background: 'linear-gradient(180deg, #131a2b 0%, #0a0e17 100%)',
        borderRight: '1px solid rgba(255, 255, 255, 0.1)',
      }}
    >
      <div style={{
        height: 64,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        borderBottom: '1px solid rgba(255, 255, 255, 0.1)',
      }}>
        <RocketOutlined style={{ fontSize: 24, color: '#00d4ff' }} />
        {!collapsed && (
          <span style={{
            marginLeft: 12,
            fontSize: 18,
            fontWeight: 600,
            color: '#00d4ff',
          }}>
            AIAC 2.0
          </span>
        )}
      </div>
      <Menu
        theme="dark"
        mode="inline"
        selectedKeys={[location.pathname]}
        defaultOpenKeys={location.pathname.startsWith('/ops') ? ['/ops'] : []}
        items={menuItems}
        onClick={handleMenuClick}
        style={{ background: 'transparent', borderRight: 'none' }}
      />
    </Sider>
  )
}
