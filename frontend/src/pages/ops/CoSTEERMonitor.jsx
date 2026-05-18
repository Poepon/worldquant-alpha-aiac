import { useState } from 'react'
import {
  Alert,
  Col,
  Empty,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import useOpsData from './hooks/useOpsData'

/**
 * CoSTEERMonitor — /ops/costeer page (2026-05-18).
 *
 * Visualizes the three R1a/R1b telemetry endpoints (e52a381 + 1858db1):
 *  - GET /ops/r1a/telemetry?days=N — attribution distribution + R5 stats
 *  - GET /ops/r1b/telemetry?days=N&top_n=M — retry/mutate success rates
 *  - GET /ops/r1b/chain-depth-distribution — mutation chain histogram
 *
 * Operators use this page to decide flag promotions per plan §10 deploy
 * sequence (e.g. flip ENABLE_R1B_HYPOTHESIS_MUTATE only after retry loop
 * shows ≥15% success in a 7d window). The single days dropdown drives
 * both telemetry calls so KPIs stay comparable.
 */
export default function CoSTEERMonitor() {
  const [days, setDays] = useState(7)

  const r1a = useOpsData(() => api.getOpsR1aTelemetry(days), [days])
  const r1b = useOpsData(() => api.getOpsR1bTelemetry(days, 5), [days])
  const chainDepth = useOpsData(() => api.getOpsR1bChainDepth(), [])
  const r8 = useOpsData(() => api.getOpsR8KbShape(), [])

  const r1aPayload = r1a.data || {}
  const r1bPayload = r1b.data || {}
  const chainPayload = chainDepth.data || {}
  const r8Payload = r8.data || {}

  // ---- R1a attribution pie ------------------------------------------------
  const ATTR_COLORS = {
    hypothesis: '#1677ff',
    implementation: '#52c41a',
    both: '#722ed1',
    unknown: '#faad14',
    null: '#bfbfbf',
  }
  const pieData = (r1aPayload.distribution || []).map((b) => ({
    name: b.attribution,
    value: b.count,
  }))

  // ---- R1b chain depth bar ------------------------------------------------
  const chainBars = (chainPayload.distribution || []).map((b) => ({
    depth: `d${b.mutation_depth}`,
    count: b.hypothesis_count,
  }))

  // ---- R1b attempt stats table -------------------------------------------
  const attemptColumns = [
    { title: 'Attempt', dataIndex: 'attempt_type', key: 'attempt_type', width: 130 },
    {
      title: 'Outcome',
      dataIndex: 'outcome',
      key: 'outcome',
      width: 140,
      render: (oc) => {
        const c =
          oc === 'pass'
            ? 'success'
            : oc === 'fail'
            ? 'error'
            : oc === 'pending'
            ? 'processing'
            : 'default'
        return <Tag color={c}>{oc}</Tag>
      },
    },
    { title: 'Count', dataIndex: 'count', key: 'count', width: 80 },
    {
      title: 'Cost (USD)',
      dataIndex: 'total_cost_usd',
      key: 'total_cost_usd',
      width: 120,
      render: (v) => v?.toFixed(4) ?? '—',
    },
    {
      title: 'Tokens',
      dataIndex: 'total_tokens_used',
      key: 'total_tokens_used',
      width: 100,
    },
  ]

  // ---- R1b top tasks by budget table -------------------------------------
  const taskColumns = [
    { title: 'Task ID', dataIndex: 'task_id', key: 'task_id', width: 100 },
    { title: 'Retries', dataIndex: 'retries_total', key: 'retries_total', width: 100 },
    { title: 'Mutations', dataIndex: 'mutations_total', key: 'mutations_total', width: 110 },
    {
      title: 'Cost (USD)',
      dataIndex: 'cost_usd_total',
      key: 'cost_usd_total',
      width: 120,
      render: (v) => v?.toFixed(4) ?? '—',
    },
  ]

  // ---- Flag summary helper -----------------------------------------------
  const flagTag = (label, on) => (
    <Tag color={on ? 'success' : 'default'} key={label}>
      {label}: {on ? 'ON' : 'OFF'}
    </Tag>
  )

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <OpsSectionCard
        title="CoSTEER Loop Monitor (R1a + R1b + R8)"
        source="live"
        loading={r1a.loading || r1b.loading || chainDepth.loading || r8.loading}
        onRefresh={() => {
          r1a.refetch()
          r1b.refetch()
          chainDepth.refetch()
          r8.refetch()
        }}
      >
        <Space size="middle" style={{ marginBottom: 16 }}>
          <span>Window:</span>
          <Select
            value={days}
            onChange={setDays}
            style={{ width: 120 }}
            options={[
              { value: 1, label: 'Last 1 day' },
              { value: 7, label: 'Last 7 days' },
              { value: 14, label: 'Last 14 days' },
              { value: 30, label: 'Last 30 days' },
            ]}
          />
        </Space>

        {/* Flag state row — operator sees current ON/OFF at a glance */}
        <Alert
          message={
            <Space wrap>
              <strong>Flag state:</strong>
              {Object.entries(r1aPayload.flags || {}).map(([k, v]) => flagTag(k, v))}
              {Object.entries(r1bPayload.flags || {}).map(([k, v]) => flagTag(k, v))}
              {Object.entries(r8Payload.flags || {}).map(([k, v]) => flagTag(k, v))}
            </Space>
          }
          type="info"
          showIcon={false}
          style={{ marginBottom: 16 }}
        />

        {/* KPI row */}
        <Row gutter={[16, 16]}>
          <Col xs={24} md={6}>
            <Statistic
              title="R1a total in window"
              value={r1aPayload.total_in_window ?? 0}
            />
          </Col>
          <Col xs={24} md={6}>
            <Statistic
              title="R1a non-unknown %"
              value={((r1aPayload.non_unknown_pct ?? 0) * 100).toFixed(2)}
              suffix="%"
            />
          </Col>
          <Col xs={24} md={6}>
            <Statistic
              title="R1b retry pass rate"
              value={((r1bPayload.success_rate_retry_impl ?? 0) * 100).toFixed(2)}
              suffix="%"
            />
          </Col>
          <Col xs={24} md={6}>
            <Statistic
              title="R1b mutate pass rate"
              value={((r1bPayload.success_rate_mutate_hyp ?? 0) * 100).toFixed(2)}
              suffix="%"
            />
          </Col>
        </Row>
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        {/* R1a attribution distribution pie */}
        <Col xs={24} lg={12}>
          <OpsSectionCard title="R1a Attribution Distribution">
            {pieData.length === 0 ? (
              <Empty description="No R1a data in window" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    label={({ name, value }) => `${name}: ${value}`}
                  >
                    {pieData.map((entry) => (
                      <Cell key={entry.name} fill={ATTR_COLORS[entry.name] || '#8c8c8c'} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              {r1aPayload.r5_sample_size > 0 && (
                <>
                  <Tag color="purple">
                    R5 sample: {r1aPayload.r5_sample_size}
                  </Tag>
                  <Tag color="purple">
                    R5 agrees R1a:{' '}
                    {((r1aPayload.r5_agrees_r1a_pct ?? 0) * 100).toFixed(1)}%
                  </Tag>
                  <Tag color="purple">
                    R5 avg score:{' '}
                    {(r1aPayload.r5_avg_composite_score ?? 0).toFixed(3)}
                  </Tag>
                  <Tag color="purple">
                    R5 cost: ${(r1aPayload.r5_total_cost_usd ?? 0).toFixed(4)}
                  </Tag>
                </>
              )}
              {r1aPayload.errs_count_total > 0 && (
                <Tag color="error">
                  Hook errors: {r1aPayload.errs_count_total}
                </Tag>
              )}
            </Space>
          </OpsSectionCard>
        </Col>

        {/* R1b chain depth histogram */}
        <Col xs={24} lg={12}>
          <OpsSectionCard title="R1b CoSTEER Chain Depth">
            {chainBars.length === 0 ? (
              <Empty description="No hypotheses in DB" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={chainBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="depth" />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Bar dataKey="count" fill="#722ed1" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag>Roots: {chainPayload.total_root_hypotheses ?? 0}</Tag>
              <Tag color="purple">
                Mutated: {chainPayload.total_mutated_hypotheses ?? 0}
              </Tag>
              <Tag>Max depth: {chainPayload.max_depth_observed ?? 0}</Tag>
              <Tag>
                Avg depth: {(chainPayload.chain_depth_avg ?? 0).toFixed(3)}
              </Tag>
            </Space>
          </OpsSectionCard>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        {/* R1b attempt stats table */}
        <Col xs={24} lg={14}>
          <OpsSectionCard title="R1b Attempt Stats">
            <Table
              size="small"
              dataSource={r1bPayload.attempt_stats || []}
              columns={attemptColumns}
              rowKey={(r) => `${r.attempt_type}::${r.outcome}`}
              pagination={false}
              locale={{ emptyText: 'No R1b attempts in window' }}
            />
          </OpsSectionCard>
        </Col>

        {/* R1b top tasks by budget */}
        <Col xs={24} lg={10}>
          <OpsSectionCard title="R1b Top Tasks by Budget">
            <Table
              size="small"
              dataSource={r1bPayload.top_tasks_by_budget || []}
              columns={taskColumns}
              rowKey="task_id"
              pagination={false}
              locale={{ emptyText: 'No tasks with R1b budget yet' }}
            />
          </OpsSectionCard>
        </Col>
      </Row>

      {/* R8 hierarchical RAG KB shape — gate evidence for ENABLE_HIERARCHICAL_RAG flip */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <OpsSectionCard title="R8 KB Entry Types (active vs decayed)">
            {(r8Payload.entry_types || []).length === 0 ? (
              <Empty description="No KB rows" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart
                  data={(r8Payload.entry_types || []).map((b) => ({
                    type: b.entry_type,
                    active: b.active_count,
                    decayed: b.decayed_count,
                  }))}
                >
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="type" />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="active" stackId="kb" fill="#1677ff" />
                  <Bar dataKey="decayed" stackId="kb" fill="#bfbfbf" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag color="blue">
                Total active: {r8Payload.total_active ?? 0}
              </Tag>
              <Tag>Total decayed: {r8Payload.total_decayed ?? 0}</Tag>
              <Tag color="success">
                SUCCESS active: {r8Payload.success_pattern_active ?? 0}
              </Tag>
              <Tag color="error">
                FAILURE active: {r8Payload.failure_pitfall_active ?? 0}
              </Tag>
              <Tag color="purple">
                R5-rankable SUCCESS: {r8Payload.r5_rankable_success_count ?? 0}
              </Tag>
            </Space>
          </OpsSectionCard>
        </Col>

        <Col xs={24} lg={10}>
          <OpsSectionCard title="R8 Pillar Coverage (active)">
            {(r8Payload.pillars || []).length === 0 ? (
              <Empty description="No pillar data" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie
                    data={(r8Payload.pillars || []).map((p) => ({
                      name: p.pillar,
                      value: p.entry_count,
                    }))}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    label={({ name, value }) => `${name}: ${value}`}
                  >
                    {(r8Payload.pillars || []).map((p) => (
                      <Cell
                        key={p.pillar}
                        fill={p.pillar === 'none' ? '#bfbfbf' : '#13c2c2'}
                      />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
      </Row>
    </Space>
  )
}
