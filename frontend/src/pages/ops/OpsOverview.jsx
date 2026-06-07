import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Col,
  Empty,
  List,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip as ReTooltip,
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
//
// `stale: true` flags beats whose data source is retired / frozen — the
// grid still shows the row (it's a faithful echo of what the backend
// returns) but tags it so an operator doesn't trust a stale date.
//   - regime_infer: regime beat 已退役,数据永久停在 2026-05-19。
//   - hypothesis_health_check: 触发器口径已弃用(见 hypothesis-health 页 banner)。
const BEAT_META = {
  alpha_health_check: { label: 'Alpha 健康度', route: '/ops/alpha-health' },
  hypothesis_health_check: { label: 'Hypothesis 触发器', route: '/ops/hypothesis-health', stale: true },
  pillar_balance: { label: '五支柱平衡', route: '/ops/pillar-balance' },
  regime_infer: { label: '市场体制', route: '/ops/regime', stale: true },
  negative_knowledge_extract: { label: '失败模式沉淀', route: '/ops/negative-knowledge' },
  macro_narrative_extract: { label: '宏观叙事', route: '/ops/macro-narratives' },
  llm_op_monitor: { label: 'LLM 算子监控', route: '/ops/llm-op-monitor' },
}

/**
 * OpsOverview — /ops/overview top-of-funnel dashboard.
 *
 * One GET /ops/overview fills most of the page; the live "池队列快照" card
 * pulls GET /ops/pools/status separately (8s poll, same as
 * PoolPipelineMonitor). Each summary card is clickable and drills into its
 * dedicated /ops/<page>.
 *
 * Note (2026-06-07): the regime panel was removed — the regime beat is
 * retired and its data is frozen at 2026-05-19. The backend get_overview
 * still returns the `region_regime` slot; the frontend simply ignores it.
 */
export default function OpsOverview() {
  const navigate = useNavigate()
  const { data, loading, refetch } = useOpsData(() => api.getOpsOverview(), [])

  // Live pool-queue snapshot — its own poll so it stays fresh independent of
  // the (mostly daily-beat) overview payload.
  const { data: pool, isError: poolError } = useQuery({
    queryKey: ['poolStatus', 'opsOverview'],
    queryFn: api.getPoolStatus,
    refetchInterval: 8000,
  })

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

  // --- Pool-queue snapshot derived values (missing keys → 0) ---
  const intentPending = pool?.hyp_intent?.PENDING || 0
  const candPendingSim = pool?.candidate_queue?.PENDING_SIM || 0
  const candPendingEval = pool?.candidate_queue?.PENDING_EVAL || 0
  const candDone = pool?.candidate_queue?.DONE || 0
  const poolWorkers = pool?.workers_count || 0
  const poolExpected = pool?.expected_workers || 0
  const tpCand = pool?.throughput_90min?.candidates || 0
  const tpAlpha = pool?.throughput_90min?.alphas || 0
  const pendingSimHot = candPendingSim > 500

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
                    opacity: m.stale ? 0.6 : 1,
                  }}
                >
                  <Space size={4} align="center">
                    <Text style={{ fontSize: 13 }}>{m.label}</Text>
                    {m.stale && (
                      <Tooltip title="数据源已退役 / 冻结,以下日期不可信。">
                        <Tag color="default" style={{ margin: 0, fontSize: 10, lineHeight: '16px' }}>
                          已弃用
                        </Tag>
                      </Tooltip>
                    )}
                  </Space>
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
                    <ReTooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
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
          <OpsSectionCard
            title="池队列快照"
            source="live"
            rerunSlot={
              pool ? (
                pool.enabled ? (
                  <Tag color="green" style={{ margin: 0 }}>POOL ON</Tag>
                ) : (
                  <Tag color="default" style={{ margin: 0 }}>POOL OFF</Tag>
                )
              ) : null
            }
          >
            {poolError ? (
              <Empty description="池状态拉取失败(GET /ops/pools/status)" />
            ) : !pool ? (
              <div style={{ textAlign: 'center', padding: 24 }}>
                <Spin />
              </div>
            ) : (
              <>
                <Row gutter={[8, 12]}>
                  <Col span={8}>
                    <Statistic
                      title="hyp_intent PENDING"
                      value={intentPending}
                    />
                  </Col>
                  <Col span={8}>
                    <Tooltip title={pendingSimHot ? 'PENDING_SIM > 500:HG 产出严重快于 S 消费,sim 槽积压' : undefined}>
                      <Statistic
                        title="待模拟 PENDING_SIM"
                        value={candPendingSim}
                        valueStyle={{ color: pendingSimHot ? '#ff4d4f' : undefined }}
                      />
                    </Tooltip>
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="待评估 PENDING_EVAL"
                      value={candPendingEval}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="已完成 DONE"
                      value={candDone}
                      valueStyle={{ color: candDone > 0 ? '#3f8600' : undefined }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="Supervisor worker"
                      value={poolWorkers}
                      suffix={poolExpected ? `/ ${poolExpected}` : undefined}
                      valueStyle={{
                        color:
                          poolExpected && poolWorkers >= poolExpected
                            ? '#3f8600'
                            : poolExpected
                              ? '#cf1322'
                              : undefined,
                      }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="产出 (近 90min)"
                      value={tpAlpha}
                      suffix={`alpha / ${tpCand} 候选`}
                      valueStyle={{ color: tpAlpha > 0 ? '#3f8600' : undefined }}
                    />
                  </Col>
                </Row>
                {pendingSimHot && (
                  <div style={{ marginTop: 12 }}>
                    <Tag color="red">PENDING_SIM 积压</Tag>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      待模拟队列 &gt; 500 —— HG 远快于 S,考虑暂停 HG / 增 S worker。详见挖掘池页。
                    </Text>
                  </div>
                )}
              </>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={12}>
          <OpsSectionCard title="近期 Top 5 失败模式">
            {(data.top_pitfalls || []).length === 0 ? (
              <Empty description="尚无 negative knowledge 数据" />
            ) : (
              <List
                size="small"
                dataSource={data.top_pitfalls || []}
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
