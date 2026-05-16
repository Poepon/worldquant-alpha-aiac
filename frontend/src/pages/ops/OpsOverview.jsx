import { useNavigate } from 'react-router-dom'
import {
  Col,
  Empty,
  List,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import SourceTagBadge from './components/SourceTagBadge'
import useOpsData from './hooks/useOpsData'

const { Text, Title } = Typography

// Same band palette as AlphaHealthMonitor for consistency.
const BAND_COLORS = {
  GREEN: '#00ff88',
  YELLOW: '#ffb700',
  ORANGE: '#ff8c00',
  RED: '#ff4d4f',
  UNKNOWN: '#9c88ff',
}
const BAND_ORDER = ['GREEN', 'YELLOW', 'ORANGE', 'RED', 'UNKNOWN']

// Map beat key → human label + target route. Keeps the grid wiring in
// one place so renaming a beat or moving a page is a single edit.
const BEAT_META = {
  alpha_health_check: { label: 'Alpha 健康度', route: '/ops/alpha-health' },
  hypothesis_health_check: { label: 'Hypothesis 触发器', route: '/ops/hypothesis-health' },
  pillar_balance: { label: '五支柱平衡', route: '/ops/pillar-balance' },
  regime_infer: { label: '市场体制', route: '/ops/regime' },
  negative_knowledge_extract: { label: '失败模式沉淀', route: '/ops/negative-knowledge' },
  macro_narrative_extract: { label: '宏观叙事', route: '/ops/macro-narratives' },
  llm_op_monitor: { label: 'LLM 算子监控', route: '/ops/llm-op-monitor' },
}

const REGIME_COLORS = {
  crisis: 'red',
  elevated: 'orange',
  normal: 'blue',
  calm: 'green',
  very_calm: 'cyan',
}

/**
 * OpsOverview — /ops/overview top-of-funnel dashboard.
 *
 * One GET /ops/overview fills the entire page; no chained calls. Each
 * card is clickable and drills into its dedicated /ops/<page>.
 */
export default function OpsOverview() {
  const navigate = useNavigate()
  const { data, loading, refetch } = useOpsData(() => api.getOpsOverview(), [])

  if (loading && !data) {
    return (
      <div style={{ textAlign: 'center', padding: 40 }}>
        <Spin />
      </div>
    )
  }
  if (!data) {
    return <Empty description="加载失败" />
  }

  const alphaSummary = data.alpha_health_summary || {}
  const hypSummary = data.hypothesis_health_summary || {}

  // Pre-shape by-region rows for the stacked bar — same code path as the
  // dedicated AlphaHealthMonitor so visual matches across both pages.
  const byRegion = alphaSummary.by_region || {}
  const regionRows = Object.entries(byRegion).map(([region, counts]) => {
    const row = { region }
    for (const band of BAND_ORDER) row[band] = counts[band] || 0
    return row
  })

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="昨夜 daily-beat 执行状态"
        onRefresh={refetch}
        loading={loading}
      >
        <Row gutter={[12, 12]}>
          {Object.entries(data.beat_status || {}).map(([key, meta]) => {
            const m = BEAT_META[key] || { label: key, route: null }
            return (
              <Col key={key} xs={12} sm={8} md={6} lg={4}>
                <div
                  onClick={() => m.route && navigate(m.route)}
                  style={{
                    padding: 12,
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: 8,
                    cursor: m.route ? 'pointer' : 'default',
                    background: 'rgba(255,255,255,0.02)',
                  }}
                >
                  <Text style={{ fontSize: 13 }}>{m.label}</Text>
                  <div style={{ marginTop: 6 }}>
                    <SourceTagBadge source={meta.source} />
                  </div>
                  <div style={{ marginTop: 4, fontSize: 11, color: '#888' }}>
                    {meta.date || '—'}
                  </div>
                </div>
              </Col>
            )
          })}
        </Row>
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <OpsSectionCard
            title="Alpha 健康度快照"
            source={alphaSummary.source}
            staleDays={alphaSummary.stale_days}
          >
            <Row gutter={[8, 8]}>
              <Col span={6}>
                <Statistic
                  title="总数"
                  value={alphaSummary.total_alphas || 0}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="GREEN"
                  value={alphaSummary.band_counts?.GREEN || 0}
                  valueStyle={{ color: BAND_COLORS.GREEN }}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="RED"
                  value={alphaSummary.band_counts?.RED || 0}
                  valueStyle={{ color: BAND_COLORS.RED }}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="UNKNOWN"
                  value={alphaSummary.band_counts?.UNKNOWN || 0}
                  valueStyle={{ color: BAND_COLORS.UNKNOWN }}
                />
              </Col>
            </Row>
            {regionRows.length > 0 && (
              <div style={{ marginTop: 16 }}>
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={regionRows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                    <XAxis dataKey="region" stroke="#888" />
                    <YAxis stroke="#888" />
                    <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                    <Legend />
                    {BAND_ORDER.map((b) => (
                      <Bar key={b} dataKey={b} stackId="b" fill={BAND_COLORS[b]} />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={12}>
          <OpsSectionCard
            title="Hypothesis 触发概览"
            source={hypSummary.source}
            staleDays={hypSummary.stale_days}
          >
            <Row gutter={[8, 8]}>
              <Col span={8}>
                <Statistic title="ACTIVE+PROMOTED" value={hypSummary.total_active || 0} />
              </Col>
              <Col span={8}>
                <Statistic
                  title="今日触发"
                  value={hypSummary.total_triggered || 0}
                  valueStyle={{ color: '#ff4d4f' }}
                />
              </Col>
              <Col span={8}>
                <Statistic
                  title="平均 score"
                  value={hypSummary.avg_thesis_score ?? '—'}
                  precision={2}
                />
              </Col>
            </Row>
            {Object.keys(hypSummary.trigger_histogram || {}).length > 0 && (
              <div style={{ marginTop: 12 }}>
                <Title level={5} style={{ margin: '8px 0' }}>触发类型</Title>
                {Object.entries(hypSummary.trigger_histogram).map(([k, v]) => (
                  <Tag key={k} color="orange" style={{ marginBottom: 4 }}>
                    {k} · {v}
                  </Tag>
                ))}
              </div>
            )}
          </OpsSectionCard>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <OpsSectionCard title="各区域市场体制" source="docs_today">
            {Object.keys(data.region_regime || {}).length === 0 ? (
              <Empty description="regime 数据尚未生成" />
            ) : (
              <Space wrap>
                {Object.entries(data.region_regime).map(([region, regime]) => (
                  <Tag
                    key={region}
                    color={REGIME_COLORS[regime] || 'default'}
                    style={{ padding: '4px 12px', fontSize: 13 }}
                  >
                    {region}: {regime || '—'}
                  </Tag>
                ))}
              </Space>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={12}>
          <OpsSectionCard title="近期 Top 5 失败模式">
            {data.top_pitfalls.length === 0 ? (
              <Empty description="尚无 negative knowledge 数据" />
            ) : (
              <List
                size="small"
                dataSource={data.top_pitfalls}
                renderItem={(p) => (
                  <List.Item>
                    <Space direction="vertical" size={0} style={{ width: '100%' }}>
                      <Space>
                        <Tag color="red">{p.fail_count} 次</Tag>
                        <Text strong style={{ fontFamily: 'monospace' }}>
                          {p.rule_id || p.signature_key}
                        </Text>
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {p.skeleton || p.remediation_hint || '—'}
                      </Text>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </OpsSectionCard>
        </Col>
      </Row>
    </Space>
  )
}
