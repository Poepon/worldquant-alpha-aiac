import { useMemo, useState } from 'react'
import {
  Col,
  Empty,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
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

// 6 negative-knowledge categories from the daily extractor — colors
// stay consistent with the rest of /ops/* so operators recognize tags.
const CATEGORY_COLORS = {
  static_finding: '#9c88ff',
  threshold: '#ff8c00',
  robustness: '#00d4ff',
  sim_error: '#ff4d4f',
  hyp_trigger: '#ffb700',
  attribution: '#00ff88',
}

const REGION_OPTIONS = ['USA', 'CHN', 'EUR', 'ASI', 'GLB']

/**
 * NegativeKnowledge — /ops/negative-knowledge page (P3 P2-D).
 *
 * Layout: KPI strip (active / 7d new / total fail) → Top 20 BarChart
 * + category PieChart side-by-side → 30d new pitfalls LineChart →
 * detail Table with per-row 禁用 Switch (PATCH /entries/{id}).
 *
 * Disabling a pitfall is the only mutation the page does; it goes
 * through OpsService.set_pitfall_active which mutates the underlying
 * KnowledgeEntry row.
 */
export default function NegativeKnowledge() {
  const [region, setRegion] = useState(null)
  const [category, setCategory] = useState(null)

  const top = useOpsData(
    () => api.getOpsNegativeTop({ region, category, limit: 50 }),
    [region, category],
  )
  const byCat = useOpsData(
    () => api.getOpsNegativeCategoryBreakdown(region),
    [region],
  )
  const timeline = useOpsData(
    () => api.getOpsNegativeTimeline(30, region),
    [region],
  )

  // Derived KPI numbers
  const activeCount = useMemo(
    () => Object.values(byCat.data?.by_category || {}).reduce((a, b) => a + b, 0),
    [byCat.data],
  )
  const sevenDayNew = useMemo(() => {
    if (!timeline.data) return 0
    const last7 = timeline.data.slice(-7)
    return last7.reduce((a, t) => a + (t.new_count || 0), 0)
  }, [timeline.data])
  const totalFail = useMemo(
    () => (top.data?.records || []).reduce((a, r) => a + (r.fail_count || 0), 0),
    [top.data],
  )

  // Top 20 bar data
  const top20 = useMemo(
    () => (top.data?.records || []).slice(0, 20).map((r) => ({
      label: r.rule_id || r.signature_key || `#${r.id}`,
      fail_count: r.fail_count || 0,
    })),
    [top.data],
  )

  // Pie data (only non-empty)
  const pieData = useMemo(
    () => Object.entries(byCat.data?.by_category || {})
      .filter(([, v]) => v > 0)
      .map(([cat, count]) => ({ name: cat, value: count })),
    [byCat.data],
  )

  const handleToggle = async (row, nextActive) => {
    try {
      await api.togglePitfall(row.id, nextActive)
      message.success(`#${row.id} → ${nextActive ? '启用' : '禁用'}`)
      top.refetch()
      byCat.refetch()
    } catch (e) {
      message.error(
        `操作失败:${e?.response?.data?.detail || e.message}`,
      )
    }
  }

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    {
      title: 'rule_id',
      dataIndex: 'rule_id',
      width: 180,
      render: (v) => <Text code>{v || '—'}</Text>,
    },
    {
      title: '类别',
      dataIndex: 'category',
      width: 130,
      render: (v) => (
        <Tag color={CATEGORY_COLORS[v] || 'default'}>{v || 'unknown'}</Tag>
      ),
    },
    { title: 'Region', dataIndex: 'region', width: 70 },
    {
      title: 'fail_count',
      dataIndex: 'fail_count',
      width: 100,
      sorter: (a, b) => a.fail_count - b.fail_count,
    },
    {
      title: 'skeleton',
      dataIndex: 'pattern',
      ellipsis: true,
      render: (v) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: '启用',
      dataIndex: 'is_active',
      width: 80,
      render: (v, row) => (
        <Switch
          size="small"
          checked={v !== false}
          onChange={(checked) => handleToggle(row, checked)}
        />
      ),
    },
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="失败模式沉淀(Negative Knowledge)"
        source={top.data?.source}
        onRefresh={() => {
          top.refetch()
          byCat.refetch()
          timeline.refetch()
        }}
        loading={top.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsNegative}
            label="重跑 negative-knowledge"
            onSuccess={() =>
              setTimeout(() => {
                top.refetch()
                byCat.refetch()
                timeline.refetch()
              }, 3000)
            }
          />
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <span>Region:</span>
          <Select
            allowClear
            placeholder="全部"
            value={region}
            onChange={setRegion}
            style={{ width: 120 }}
            options={REGION_OPTIONS.map((r) => ({ label: r, value: r }))}
          />
          <span style={{ marginLeft: 12 }}>Category:</span>
          <Select
            allowClear
            placeholder="全部"
            value={category}
            onChange={setCategory}
            style={{ width: 160 }}
            options={Object.keys(CATEGORY_COLORS).map((c) => ({ label: c, value: c }))}
          />
        </Space>
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Statistic title="active pitfalls" value={activeCount} />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic title="近 7 天新增" value={sevenDayNew} valueStyle={{ color: '#ff8c00' }} />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic title="Top 20 累计 fail_count" value={totalFail} valueStyle={{ color: '#ff4d4f' }} />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic
              title="覆盖 category 数"
              value={Object.keys(byCat.data?.by_category || {}).length}
            />
          </Col>
        </Row>
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={14}>
          <OpsSectionCard title="Top 20 高频 pitfall" source={top.data?.source}>
            {top20.length === 0 ? (
              <Empty description="无 pitfall" />
            ) : (
              <ResponsiveContainer width="100%" height={320}>
                <BarChart data={top20} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                  <XAxis type="number" stroke="#888" />
                  <YAxis dataKey="label" type="category" stroke="#888" width={150} />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                  <Bar dataKey="fail_count" fill="#ff4d4f" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={10}>
          <OpsSectionCard title="按类别分布" source={byCat.data?.source}>
            {pieData.length === 0 ? (
              <Empty description="无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={320}>
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={100}
                    label
                  >
                    {pieData.map((d) => (
                      <Cell
                        key={d.name}
                        fill={CATEGORY_COLORS[d.name] || '#888'}
                      />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
      </Row>

      <OpsSectionCard title="30 天新增 pitfall" source="docs_archived">
        {(timeline.data || []).length === 0 ? (
          <Empty description="近 30 天无新增" />
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={timeline.data}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis dataKey="date" stroke="#888" />
              <YAxis stroke="#888" />
              <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
              <Legend />
              <Line type="monotone" dataKey="new_count" stroke="#ff8c00" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>

      <OpsSectionCard title="明细" source={top.data?.source}>
        <Table
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={top.data?.records || []}
          loading={top.loading}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </OpsSectionCard>
    </Space>
  )
}
