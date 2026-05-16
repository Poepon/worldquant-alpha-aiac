import { useMemo, useState } from 'react'
import {
  Col,
  Drawer,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import RerunButton from './components/RerunButton'
import useOpsData from './hooks/useOpsData'

const { Text } = Typography

/**
 * HypothesisHealthMonitor — /ops/hypothesis-health page.
 *
 * Layout:
 *  - KPI strip: active / fired / avg thesis_score
 *  - Trigger-type horizontal BarChart (replaces the heatmap from the
 *    original plan; Recharts has no native heatmap and the histogram
 *    conveys the same "what's firing the most" question more directly)
 *  - thesis_score bucket histogram
 *  - 30d trend line (triggered count + avg score)
 *  - Fired hypotheses Table with click-to-open audit-transitions Drawer
 *
 * The Drawer pulls /transitions?hypothesis_id=… so the operator can see
 * the precise edge history (False→True) without leaving the page.
 */
export default function HypothesisHealthMonitor() {
  const latest = useOpsData(() => api.getOpsHypothesisHealthLatest(), [])
  const history = useOpsData(() => api.getOpsHypothesisHealthHistory(30), [])

  const [drawerHypId, setDrawerHypId] = useState(null)
  const transitions = useOpsData(
    () => (drawerHypId ? api.getOpsHypothesisTransitions(drawerHypId, 50) : Promise.resolve([])),
    [drawerHypId],
  )

  const summary = latest.data?.summary || {}
  const source = latest.data?.source || 'missing'
  const hyps = latest.data?.payload?.hypotheses || []
  const firedHyps = hyps.filter((h) => h.is_triggered)

  // ---- Derived chart data -------------------------------------------------
  const triggerHistRows = useMemo(() => {
    const hist = summary.trigger_histogram || {}
    return Object.entries(hist)
      .map(([k, v]) => ({ trigger: k, count: v }))
      .sort((a, b) => b.count - a.count)
  }, [summary.trigger_histogram])

  const scoreBucketRows = useMemo(() => {
    const buckets = summary.score_buckets || {}
    // Sort by the bucket start number (e.g. "0-20" → 0)
    return Object.entries(buckets)
      .map(([k, v]) => ({
        bucket: k,
        count: v,
        sortKey: parseInt(k.split('-')[0], 10) || 0,
      }))
      .sort((a, b) => a.sortKey - b.sortKey)
  }, [summary.score_buckets])

  const trendRows = useMemo(() => {
    if (!history.data) return []
    return history.data.map((d) => ({
      date: d._date,
      triggered: d.total_triggered,
      avgScore: d.avg_thesis_score,
    }))
  }, [history.data])

  // ---- Table columns -----------------------------------------------------
  const columns = [
    {
      title: 'Hyp ID',
      dataIndex: 'hypothesis_id',
      width: 80,
      render: (id) => (
        <a onClick={() => setDrawerHypId(id)}>{id}</a>
      ),
    },
    { title: 'Pillar', dataIndex: 'pillar', width: 100 },
    { title: 'Region', dataIndex: 'region', width: 80 },
    {
      title: 'Triggered',
      dataIndex: 'is_triggered',
      width: 90,
      render: (v) => (v ? <Tag color="red">FIRED</Tag> : <Tag>—</Tag>),
    },
    {
      title: 'Thesis score',
      dataIndex: 'thesis_score',
      width: 110,
      render: (s) => (typeof s === 'number' ? s.toFixed(1) : '—'),
    },
    {
      title: 'Triggered fires',
      dataIndex: 'trigger_detail',
      ellipsis: true,
      render: (d) => {
        const fired = d?.fired || []
        if (!fired.length) return <Text type="secondary">—</Text>
        return fired.map((f) => <Tag key={f} color="orange">{f}</Tag>)
      },
    },
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="Hypothesis 触发器健康度"
        source={source}
        staleDays={summary.stale_days}
        onRefresh={latest.refetch}
        loading={latest.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsHypothesisHealth}
            label="重跑 hypothesis-health"
            onSuccess={() =>
              setTimeout(() => {
                latest.refetch()
                history.refetch()
              }, 3000)
            }
          />
        }
      >
        {latest.loading && !latest.data ? (
          <Spin />
        ) : summary.total_active === 0 ? (
          <Empty description="今日尚无 hypothesis-health 数据;点右上 Rerun 触发" />
        ) : (
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={6}>
              <Statistic title="ACTIVE+PROMOTED 数" value={summary.total_active || 0} />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="今日触发数"
                value={summary.total_triggered || 0}
                valueStyle={{ color: '#ff4d4f' }}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="平均 thesis_score"
                value={summary.avg_thesis_score ?? '—'}
                precision={2}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="触发率"
                value={
                  summary.total_active
                    ? (100 * summary.total_triggered / summary.total_active).toFixed(1)
                    : 0
                }
                suffix="%"
              />
            </Col>
          </Row>
        )}
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <OpsSectionCard title="触发类型频次" source={source}>
            {triggerHistRows.length === 0 ? (
              <Empty description="无触发记录" />
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={triggerHistRows} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                  <XAxis type="number" stroke="#888" />
                  <YAxis dataKey="trigger" type="category" stroke="#888" width={160} />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                  <Bar dataKey="count" fill="#ff8c00" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
        <Col xs={24} md={12}>
          <OpsSectionCard title="thesis_score 分布(20 分桶)" source={source}>
            {scoreBucketRows.length === 0 ? (
              <Empty description="无打分数据" />
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={scoreBucketRows}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                  <XAxis dataKey="bucket" stroke="#888" />
                  <YAxis stroke="#888" />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                  <Bar dataKey="count" fill="#9c88ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
      </Row>

      <OpsSectionCard title="30 天触发数 + 平均 score 趋势" source="docs_archived">
        {trendRows.length === 0 ? (
          <Empty description="历史不足" />
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis dataKey="date" stroke="#888" />
              <YAxis yAxisId="left" stroke="#ff4d4f" />
              <YAxis yAxisId="right" orientation="right" stroke="#00d4ff" />
              <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
              <Legend />
              <Line yAxisId="left" type="monotone" dataKey="triggered" stroke="#ff4d4f" strokeWidth={2} dot={false} />
              <Line yAxisId="right" type="monotone" dataKey="avgScore" stroke="#00d4ff" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>

      <OpsSectionCard title={`已触发 hypothesis 明细(${firedHyps.length} 条)`} source={source}>
        <Table
          rowKey={(r) => r.hypothesis_id ?? r.id}
          size="small"
          columns={columns}
          dataSource={firedHyps}
          expandable={{
            expandedRowRender: (row) => (
              <div>
                <Text strong>trigger_detail JSON:</Text>
                <pre style={{ background: '#1f2937', padding: 12, marginTop: 4, fontSize: 12 }}>
                  {JSON.stringify(row.trigger_detail || {}, null, 2)}
                </pre>
                {row.ai_feedback && (
                  <>
                    <Text strong>AI feedback:</Text>
                    <p style={{ fontStyle: 'italic', color: '#aaa' }}>{row.ai_feedback}</p>
                  </>
                )}
              </div>
            ),
          }}
          pagination={{ pageSize: 10, showSizeChanger: false }}
        />
      </OpsSectionCard>

      <Drawer
        title={`Hypothesis ${drawerHypId} 触发审计`}
        open={!!drawerHypId}
        onClose={() => setDrawerHypId(null)}
        width={560}
      >
        {transitions.loading ? (
          <Spin />
        ) : (transitions.data || []).length === 0 ? (
          <Empty description="该 hypothesis 无触发边记录" />
        ) : (
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={transitions.data}
            columns={[
              { title: '时间', dataIndex: 'transitioned_at', width: 170 },
              {
                title: '边',
                width: 110,
                render: (_, r) => (
                  <Tag color={r.new_is_triggered ? 'red' : 'green'}>
                    {String(r.old_is_triggered)} → {String(r.new_is_triggered)}
                  </Tag>
                ),
              },
              { title: 'Reason', dataIndex: 'reason', ellipsis: true },
              { title: 'Source', dataIndex: 'source', width: 110 },
            ]}
          />
        )}
      </Drawer>
    </Space>
  )
}
