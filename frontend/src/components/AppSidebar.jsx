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
  FunctionOutlined,
  DollarOutlined,
  CopyOutlined,
  ApartmentOutlined,
  ReadOutlined,
  HeartOutlined,
  RadarChartOutlined,
  SendOutlined,
} from '@ant-design/icons'

const { Sider } = Layout

// 2026-06-07 P1 (四池重设计 — docs/frontend_pool_redesign_2026-06-07.md):
// IA 从「按机制代号平铺 27 项」重组为「按池生命周期阶段分段」。
//  - 「提交中心」提为顶级组(execution-limited 系统的真瓶颈=提交)。
//  - 「运维监控」内用 type:'group' 分段:池总览 / 生成HG / 模拟S / 评估E /
//    知识库&RAG / 系统&配置。(归档页 G10/G3-v2 仍存于路由 /ops/g10-logic,仅从菜单移除)
//  - P0 已删 4 死页(R5/方向Bandit/G5/R8-v3);此处不再出现。
// 提交相关路由仍是 /ops/* 路径(route 不改,避免书签失效),仅在菜单里归组。
const SUBMIT_PATHS = ['/ops/submit-backlog', '/ops/submit-yield', '/ops/auto-submit', '/ops/optimization-cycles']

const menuItems = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: '/alphas', icon: <FunctionOutlined />, label: 'Alpha 列表' },
  {
    key: 'submit-center',
    icon: <SendOutlined />,
    label: '提交中心',
    children: [
      { key: '/ops/submit-backlog', label: '提交积压' },
      { key: '/ops/submit-yield', label: '提交产出率' },
      { key: '/ops/auto-submit', label: '自动提交' },
      { key: '/ops/optimization-cycles', label: '参数优化审计' },
    ],
  },
  { key: '/data', icon: <DatabaseOutlined />, label: '数据管理' },
  { key: '/correlation', icon: <WarningOutlined />, label: '危机压力测试' },
  {
    key: '/ops',
    icon: <MonitorOutlined />,
    label: '运维监控',
    children: [
      {
        type: 'group',
        label: '池总览',
        children: [
          { key: '/ops/overview', label: '总览' },
          { key: '/ops/pool-pipeline', icon: <ThunderboltOutlined />, label: '挖掘流水线（想法生成/回测/评估 · 总览/队列/工作进程）' },
          { key: '/ops/regime-monitor', icon: <RadarChartOutlined />, label: '行情切换监测' },
        ],
      },
      {
        type: 'group',
        label: '想法生成阶段',
        children: [
          { key: '/ops/pillar-balance', label: '五大因子类别平衡' },
          { key: '/ops/g3-monitor', icon: <CopyOutlined />, label: '代码结构去重' },
          { key: '/ops/g8-monitor', icon: <ApartmentOutlined />, label: '假设森林' },
          { key: '/ops/macro-narratives', label: '宏观叙事' },
          { key: '/ops/negative-knowledge', label: '失败经验库' },
        ],
      },
      {
        type: 'group',
        label: '回测模拟阶段',
        children: [
          { key: '/ops/r9-cache', icon: <DatabaseOutlined />, label: '回测缓存' },
        ],
      },
      {
        type: 'group',
        label: '评估入库阶段',
        children: [
          { key: '/ops/alpha-health', icon: <HeartOutlined />, label: '评估诊断 (健康/容量/因子)' },
        ],
      },
      {
        type: 'group',
        label: '知识库 & 检索',
        children: [
          { key: '/ops/costeer', icon: <ReadOutlined />, label: '知识库与检索' },
          { key: '/ops/cognitive-reconcile', label: '知识库对账（第二阶段）' },
          { key: '/ops/llm-op-monitor', label: 'LLM 算子监控' },
          { key: '/ops/hypothesis-health', label: '假设队列漏斗' },
        ],
      },
      {
        type: 'group',
        label: '系统 & 配置',
        children: [
          { key: '/ops/llm-routing', icon: <FunctionOutlined />, label: 'LLM 路由' },
          { key: '/ops/cost-monitor', icon: <DollarOutlined />, label: 'LLM 成本' },
          { key: '/ops/feature-flags', label: '功能开关' },
          { key: '/ops/brain-role', icon: <SwapOutlined />, label: 'BRAIN 账号模式' },
        ],
      },
    ],
  },
  { key: '/config', icon: <SettingOutlined />, label: '配置中心' },
]

export default function AppSidebar() {
  const [collapsed, setCollapsed] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  const handleMenuClick = ({ key }) => {
    navigate(key)
  }

  // Which top-level SubMenu should be open on load: submit routes live under
  // the 「提交中心」group; everything else /ops/* under 「运维监控」.
  const initialOpenKeys = SUBMIT_PATHS.includes(location.pathname)
    ? ['submit-center']
    : location.pathname.startsWith('/ops')
      ? ['/ops']
      : []

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
        defaultOpenKeys={initialOpenKeys}
        items={menuItems}
        onClick={handleMenuClick}
        style={{ background: 'transparent', borderRight: 'none' }}
      />
    </Sider>
  )
}
