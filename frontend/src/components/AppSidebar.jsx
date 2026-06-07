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
  FunctionOutlined,
  DollarOutlined,
  CopyOutlined,
  ApartmentOutlined,
  FundOutlined,
  ReadOutlined,
  CheckSquareOutlined,
  RadarChartOutlined,
  SendOutlined,
  ApiOutlined,
} from '@ant-design/icons'

const { Sider } = Layout

// 2026-06-07 P1 (四池重设计 — docs/frontend_pool_redesign_2026-06-07.md):
// IA 从「按机制代号平铺 27 项」重组为「按池生命周期阶段分段」。
//  - 「提交中心」提为顶级组(execution-limited 系统的真瓶颈=提交)。
//  - 「运维监控」内用 type:'group' 分段:池总览 / 生成HG / 模拟S / 评估E /
//    知识库&RAG / 系统&配置 / 废弃待Phase2(G10/G3-v2 灰显归档)。
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
      { key: '/ops/submit-yield', label: '提交产率 (yield)' },
      { key: '/ops/auto-submit', label: '自动提交' },
      { key: '/ops/optimization-cycles', label: '优化 sweep 审计' },
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
          { key: '/ops/pool-pipeline', icon: <ThunderboltOutlined />, label: '挖掘池 (HG/S/E)' },
          { key: '/ops/pool-queue', icon: <FundOutlined />, label: '队列健康 / 积压' },
          { key: '/ops/pool-workers', icon: <ApiOutlined />, label: '工作器与心跳' },
          { key: '/ops/regime-monitor', icon: <RadarChartOutlined />, label: 'Regime 转向监测' },
        ],
      },
      {
        type: 'group',
        label: '生成阶段 · HG',
        children: [
          { key: '/ops/pillar-balance', label: '五支柱平衡' },
          { key: '/ops/g3-monitor', icon: <CopyOutlined />, label: 'AST 原创性' },
          { key: '/ops/g8-monitor', icon: <ApartmentOutlined />, label: '假设森林' },
          { key: '/ops/macro-narratives', label: '宏观叙事' },
          { key: '/ops/negative-knowledge', label: '失败模式沉淀' },
        ],
      },
      {
        type: 'group',
        label: '模拟阶段 · S',
        children: [
          { key: '/ops/r9-cache', icon: <DatabaseOutlined />, label: '模拟缓存 (R9)' },
        ],
      },
      {
        type: 'group',
        label: '评估阶段 · E',
        children: [
          { key: '/ops/alpha-health', label: 'Alpha 健康度' },
          { key: '/ops/r11-capacity', icon: <FundOutlined />, label: '容量估算 (R11)' },
          { key: '/ops/r13-factor-lens', icon: <ExperimentOutlined />, label: '因子透镜 (R13)' },
        ],
      },
      {
        type: 'group',
        label: '知识库 & RAG',
        children: [
          { key: '/ops/costeer', icon: <ReadOutlined />, label: '知识库与 RAG' },
          { key: '/ops/cognitive-reconcile', label: '池认知对账 (Phase 2)' },
          { key: '/ops/llm-op-monitor', label: 'LLM 算子监控' },
          { key: '/ops/hypothesis-health', label: 'Hypothesis 池漏斗' },
        ],
      },
      {
        type: 'group',
        label: '系统 & 配置',
        children: [
          { key: '/ops/llm-routing', icon: <FunctionOutlined />, label: 'LLM 路由' },
          { key: '/ops/cost-monitor', icon: <DollarOutlined />, label: 'LLM 成本' },
          { key: '/ops/feature-flags', label: 'Feature Flag' },
          { key: '/ops/brain-role', icon: <SwapOutlined />, label: 'BRAIN 模式' },
        ],
      },
      {
        type: 'group',
        label: '废弃 / 待 Phase 2',
        children: [
          { key: '/ops/g10-logic', icon: <ReadOutlined />, label: '[归档] 逻辑资产库 (G10)' },
          { key: '/ops/g3v2-monitor', icon: <CheckSquareOutlined />, label: '[归档] 语法校验 (G3-v2)' },
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
