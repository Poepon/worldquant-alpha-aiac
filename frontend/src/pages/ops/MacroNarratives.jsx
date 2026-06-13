import { useMemo, useState } from 'react'
import {
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import {
  RadialBar,
  RadialBarChart,
  ResponsiveContainer,
  Tooltip,
  Treemap,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import RerunButton from './components/RerunButton'
import useOpsData from './hooks/useOpsData'

const { Text } = Typography

const SCOPE_TABS = [
  { key: 'field', label: '按字段' },
  { key: 'dataset', label: '按数据集' },
  { key: 'category', label: '按类别' },
]

// Soft daily LLM budget for the macro extractor — used as the
// RadialBarChart denominator. Tunable in MACRO_EXTRACT_DAILY_TOKEN_BUDGET
// later if we move it to settings, but today's quota is 40k tokens.
const DAILY_BUDGET = 40000

/**
 * MacroNarratives — /ops/macro-narratives page (P3 P2-A).
 *
 * Three vertical sections:
 *  1. KPI strip + token budget RadialBar
 *  2. Coverage Treemap (datafield → has-narrative split)
 *  3. Three Tab Table by scope (field / dataset / category)
 */
export default function MacroNarratives() {
  const latest = useOpsData(() => api.getOpsMacroLatest(), [])
  const coverage = useOpsData(() => api.getOpsMacroCoverage(), [])
  const budget = useOpsData(() => api.getOpsMacroTokenBudget(), [])

  const [activeScope, setActiveScope] = useState('field')
  const records = useOpsData(
    () => api.getOpsMacroByScope(activeScope, { limit: 200 }),
    [activeScope],
  )

  const cov = coverage.data?.coverage || {}

  // Treemap rows: 1 box per scope, sized by count. Recharts Treemap
  // wants {name, size, fill} so we shape it here.
  const treemapData = useMemo(() => {
    const byScope = cov.by_scope || {}
    const colors = { field: '#00d4ff', dataset: '#9c88ff', category: '#ffb700' }
    const scopeLabel = { field: '字段', dataset: '数据集', category: '类别' }
    return Object.entries(byScope).map(([scope, n]) => ({
      name: `${scopeLabel[scope] || scope} · ${n}`,
      size: n,
      fill: colors[scope] || '#888',
    }))
  }, [cov])

  // Radial budget data — single bar going from 0 to (used/budget*100)
  const budgetPct = budget.data?.tokens_used
    ? Math.min(100, Math.round((budget.data.tokens_used / DAILY_BUDGET) * 100))
    : 0

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    {
      title: activeScope === 'field' ? '字段'
        : activeScope === 'dataset' ? '数据集'
          : '数据集类别',
      dataIndex: activeScope === 'field' ? 'field_id'
        : activeScope === 'dataset' ? 'dataset_id'
          : 'dataset_category',
      width: 200,
      render: (v) => <Text code>{v || '—'}</Text>,
    },
    { title: '地区', dataIndex: 'region', width: 80 },
    {
      title: '置信度',
      dataIndex: 'confidence',
      width: 100,
      render: (v) => (typeof v === 'number' ? v.toFixed(2) : '—'),
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 120,
      render: (v) => <Tag>{v || '未知'}</Tag>,
    },
    {
      title: '机制',
      dataIndex: 'mechanism',
      ellipsis: true,
      render: (v) => <Text type="secondary">{v || '—'}</Text>,
    },
  ]

  const refreshAll = () => {
    latest.refetch()
    coverage.refetch()
    budget.refetch()
    records.refetch()
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="宏观叙事知识库"
        source={latest.data?.source}
        onRefresh={refreshAll}
        loading={latest.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsMacro}
            label="重跑宏观叙事提取"
            onSuccess={() => setTimeout(refreshAll, 3000)}
          />
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Statistic title="叙事总数" value={cov.total ?? '—'} />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="覆盖字段数"
              value={`${cov.fields_with_narrative ?? 0}/${cov.fields_total ?? 0}`}
            />
            <Progress
              percent={cov.fields_coverage_pct || 0}
              showInfo={false}
              strokeColor="#00ff88"
              style={{ marginTop: 4 }}
            />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="今日 LLM token"
              value={budget.data?.tokens_used ?? '—'}
              suffix={`/ ${DAILY_BUDGET.toLocaleString()}`}
            />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="Redis 缓存"
              value={budget.data?.redis_ok ? '正常' : '离线'}
              valueStyle={{
                color: budget.data?.redis_ok ? '#00ff88' : '#ff4d4f',
              }}
            />
          </Col>
        </Row>
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={14}>
          <OpsSectionCard title="按范围计数（矩形树图）" source="service">
            {treemapData.length === 0 ? (
              <Empty description="暂无叙事" />
            ) : (
              <ResponsiveContainer width="100%" height={300}>
                <Treemap
                  data={treemapData}
                  dataKey="size"
                  stroke="#000"
                  fill="#888"
                />
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={10}>
          <OpsSectionCard title="今日 token 用量" source="service">
            {budget.loading && !budget.data ? (
              <Spin />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <RadialBarChart
                  innerRadius="60%"
                  outerRadius="100%"
                  data={[{ name: 'used', value: budgetPct, fill: budgetPct > 80 ? '#ff4d4f' : '#00d4ff' }]}
                  startAngle={90}
                  endAngle={-270}
                >
                  <RadialBar background dataKey="value" />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                </RadialBarChart>
              </ResponsiveContainer>
            )}
            <div style={{ textAlign: 'center', marginTop: 8 }}>
              <Text strong style={{ fontSize: 24 }}>{budgetPct}%</Text>
              <div style={{ color: '#888', fontSize: 12 }}>
                {budget.data?.utc_date || 'today'}
              </div>
            </div>
          </OpsSectionCard>
        </Col>
      </Row>

      <OpsSectionCard title="按范围浏览" source={records.data?.source}>
        <Tabs
          activeKey={activeScope}
          onChange={setActiveScope}
          items={SCOPE_TABS.map((t) => ({
            key: t.key,
            label: t.label,
            children: (
              <Table
                rowKey="id"
                size="small"
                columns={columns}
                dataSource={records.data?.records || []}
                loading={records.loading}
                pagination={{ pageSize: 20, showSizeChanger: false }}
              />
            ),
          }))}
        />
      </OpsSectionCard>
    </Space>
  )
}
