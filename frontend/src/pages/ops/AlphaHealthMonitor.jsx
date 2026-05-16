import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
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

// Color theme matches frontend/src/main.jsx tokens + Dashboard.jsx
// so the per-band bars are recognisable across pages.
const BAND_COLORS = {
  GREEN: '#00ff88',
  YELLOW: '#ffb700',
  ORANGE: '#ff8c00',
  RED: '#ff4d4f',
  UNKNOWN: '#9c88ff',
}

const BAND_ORDER = ['GREEN', 'YELLOW', 'ORANGE', 'RED', 'UNKNOWN']

/**
 * AlphaHealthMonitor — /ops/alpha-health page.
 *
 * Layout: 4 KPI Progress circles → per-region stacked BarChart →
 * 30d GREEN% LineChart → drill-down Table with band/region filter.
 * Click an alpha row to jump to its detail page.
 *
 * Data flow: useOpsData hooks pull /latest + /history + /alphas in
 * parallel; each card shows its own loading state. Rerun fires the
 * daily Celery task; on success we refetch all three.
 */
export default function AlphaHealthMonitor() {
  const navigate = useNavigate()
  const [filterBand, setFilterBand] = useState(null)
  const [filterRegion, setFilterRegion] = useState(null)

  const latest = useOpsData(() => api.getOpsAlphaHealthLatest(), [])
  const history = useOpsData(() => api.getOpsAlphaHealthHistory(30), [])
  const records = useOpsData(
    () => api.getOpsAlphaHealthRecords({
      band: filterBand,
      region: filterRegion,
      limit: 500,
    }),
    [filterBand, filterRegion],
  )

  const summary = latest.data?.summary || {}
  const source = latest.data?.source || 'missing'

  // ---- KPI circle data ----------------------------------------------------
  const total = summary.total_alphas || 0
  const bandPcts = summary.band_pcts || {}

  // ---- Per-region stacked bar data ---------------------------------------
  const byRegion = summary.by_region || {}
  const regionRows = useMemo(() => {
    return Object.entries(byRegion).map(([region, counts]) => {
      const row = { region }
      for (const band of BAND_ORDER) row[band] = counts[band] || 0
      return row
    })
  }, [byRegion])

  // ---- 30d GREEN% line ---------------------------------------------------
  const trendRows = useMemo(() => {
    if (!history.data) return []
    return history.data.map((d) => ({
      date: d._date,
      'GREEN%': d.band_pcts?.GREEN || 0,
      'RED%': (d.band_pcts?.RED || 0) + (d.band_pcts?.ORANGE || 0),
    }))
  }, [history.data])

  // ---- Table columns -----------------------------------------------------
  const columns = [
    {
      title: 'Alpha ID',
      dataIndex: 'alpha_id',
      width: 140,
      render: (id) => (
        <a onClick={() => navigate(`/alphas/${id}`)} style={{ fontFamily: 'monospace' }}>
          {id}
        </a>
      ),
    },
    { title: 'Region', dataIndex: 'region', width: 80 },
    {
      title: 'Band',
      dataIndex: 'health_band',
      width: 100,
      render: (b) => (
        <Tag color={BAND_COLORS[b] || 'default'} style={{ color: '#000', borderColor: 'transparent' }}>
          {b}
        </Tag>
      ),
    },
    {
      title: 'Score',
      dataIndex: 'health_score',
      width: 80,
      render: (s) => (typeof s === 'number' ? s.toFixed(1) : '—'),
    },
    {
      title: '推荐动作',
      dataIndex: 'recommended_action',
      ellipsis: true,
    },
  ]

  const handleRerunSuccess = () => {
    // Wait a few seconds for Celery beat to finish, then re-pull
    setTimeout(() => {
      latest.refetch()
      history.refetch()
      records.refetch()
    }, 3000)
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="Alpha 库健康度"
        source={source}
        staleDays={summary.stale_days}
        onRefresh={latest.refetch}
        loading={latest.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsAlphaHealth}
            label="重跑 health-check"
            onSuccess={handleRerunSuccess}
          />
        }
      >
        {latest.loading && !latest.data ? (
          <Spin />
        ) : total === 0 ? (
          <Empty description="今日尚无 health-check 数据;点右上 Rerun 触发" />
        ) : (
          <Row gutter={[16, 16]}>
            {BAND_ORDER.map((band) => (
              <Col key={band} xs={12} sm={8} md={4}>
                <Statistic
                  title={band}
                  value={summary.band_counts?.[band] || 0}
                  suffix={
                    <span style={{ fontSize: 12, color: '#888' }}>
                      / {total}
                    </span>
                  }
                />
                <Progress
                  percent={bandPcts[band] || 0}
                  showInfo={false}
                  strokeColor={BAND_COLORS[band]}
                  style={{ marginTop: 4 }}
                />
                <span style={{ fontSize: 12, color: '#888' }}>
                  {(bandPcts[band] || 0).toFixed(1)}%
                </span>
              </Col>
            ))}
          </Row>
        )}
      </OpsSectionCard>

      <OpsSectionCard title="各区域 band 分布" source={source}>
        {regionRows.length === 0 ? (
          <Empty description="无数据" />
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={regionRows}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis dataKey="region" stroke="#888" />
              <YAxis stroke="#888" />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
              />
              <Legend />
              {BAND_ORDER.map((band) => (
                <Bar
                  key={band}
                  dataKey={band}
                  stackId="b"
                  fill={BAND_COLORS[band]}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>

      <OpsSectionCard title="30 天 GREEN / (RED+ORANGE) 趋势" source="docs_archived">
        {trendRows.length === 0 ? (
          <Empty description="历史数据不足" />
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis dataKey="date" stroke="#888" />
              <YAxis stroke="#888" />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
              />
              <Legend />
              <Line type="monotone" dataKey="GREEN%" stroke="#00ff88" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="RED%" stroke="#ff4d4f" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>

      <OpsSectionCard
        title="问题 alpha 明细"
        source={records.data?.source || source}
        onRefresh={records.refetch}
        loading={records.loading}
      >
        <Space style={{ marginBottom: 12 }}>
          <span>过滤:</span>
          {BAND_ORDER.map((b) => (
            <Tag.CheckableTag
              key={b}
              checked={filterBand === b}
              onChange={(c) => setFilterBand(c ? b : null)}
              style={{
                background: filterBand === b ? BAND_COLORS[b] : undefined,
                color: filterBand === b ? '#000' : undefined,
              }}
            >
              {b}
            </Tag.CheckableTag>
          ))}
          {Object.keys(byRegion).map((r) => (
            <Tag.CheckableTag
              key={r}
              checked={filterRegion === r}
              onChange={(c) => setFilterRegion(c ? r : null)}
            >
              {r}
            </Tag.CheckableTag>
          ))}
        </Space>
        <Table
          rowKey={(r) => r.alpha_pk ?? r.alpha_id}
          size="small"
          columns={columns}
          dataSource={records.data?.records || []}
          loading={records.loading}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </OpsSectionCard>
    </Space>
  )
}
