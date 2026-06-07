import { useMemo } from 'react'
import {
  Alert,
  Col,
  Empty,
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
  Cell,
  ResponsiveContainer,
  Tooltip as RTooltip,
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
 * 改写为「Hypothesis 池漏斗」(2026-06-07,四池世界 P1)。
 *
 * 背景:池内 node_hypothesis 跑 LEVEL-0,Hypothesis 行恒 PROPOSED(晋升
 * beat ENABLE_POOL_COGNITIVE_RECONCILE 未部署),HypothesisRoundStats 不
 * 写 → 原触发器图表(trigger_histogram / recent_rounds)结构性恒空、失真,
 * 已移除。
 *
 * 现主体 = hyp_intent 池漏斗(getPoolStatus 的 PENDING→CLAIMED→DONE→FAILED)
 * + Hypothesis.pillar 分布(getOpsPillarLatest,live)。保留 rerun 按钮。
 */

// hyp_intent 漏斗阶段(顺序即漏斗走向)
const INTENT_FUNNEL = [
  { key: 'PENDING', label: '待认领 PENDING', color: '#1677ff', desc: 'scheduler beat 已落 hyp_intent,等 HG worker 认领' },
  { key: 'CLAIMED', label: '认领中 CLAIMED', color: '#faad14', desc: 'HG worker 已 lease,正在生成候选(应短暂;超 lease 见池监控页)' },
  { key: 'DONE', label: '已完成 DONE', color: '#52c41a', desc: 'HG 已据此 intent 产出候选(终态)' },
  { key: 'FAILED', label: '失败 FAILED', color: '#ff4d4f', desc: 'HG 处理失败(终态)' },
  { key: 'PURGED', label: '清理 PURGED', color: '#8c8c8c', desc: '被清理/作废(终态)' },
]

// pillar bar 调色板
const PILLAR_PALETTE = ['#00d4ff', '#9c88ff', '#ffb700', '#ff8c00', '#52c41a', '#ff4d4f', '#13c2c2']

export default function HypothesisHealthMonitor() {
  // 池状态(主体漏斗源) — 与池相关页一致 8s 轮询
  const pool = useOpsData(() => api.getPoolStatus(), [])
  // pillar 分布(live)
  const pillar = useOpsData(() => api.getOpsPillarLatest(), [])
  // 旧 hypothesis-health latest:仅保留仍可信的 active 计数;触发器维度已废弃
  const latest = useOpsData(() => api.getOpsHypothesisHealthLatest(), [])

  const intent = pool.data?.hyp_intent || {}
  const intentTotal = useMemo(
    () => INTENT_FUNNEL.reduce((acc, s) => acc + (intent[s.key] || 0), 0),
    [intent],
  )
  const intentMax = useMemo(
    () => Math.max(1, ...INTENT_FUNNEL.map((s) => intent[s.key] || 0)),
    [intent],
  )

  // ---- pillar 分布:跨 region 聚合绝对计数 ≈ shares × stamped_total -------
  const pillarRows = useMemo(() => {
    const regions = pillar.data?.payload?.regions || {}
    const agg = {}
    Object.values(regions).forEach((block) => {
      const shares = block?.shares || {}
      const stamped = block?.stamped_total || 0
      Object.entries(shares).forEach(([p, frac]) => {
        agg[p] = (agg[p] || 0) + Math.round((frac || 0) * stamped)
      })
    })
    return Object.entries(agg)
      .map(([pillar, count]) => ({ pillar, count }))
      .filter((d) => d.count > 0)
      .sort((a, b) => b.count - a.count)
  }, [pillar.data])

  const pillarTotal = useMemo(
    () => pillarRows.reduce((acc, r) => acc + r.count, 0),
    [pillarRows],
  )

  // 旧 health summary —— 仅 active 计数仍可信(触发/score 维度已废弃)
  const summary = latest.data?.summary || {}
  const pillarStale = pillar.data?.payload?._stale_days

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Alert
        type="info"
        showIcon
        message="Hypothesis 生命周期晋升已停用 — 当前池内假设恒为 PROPOSED"
        description={
          <span>
            Hypothesis 生命周期晋升(PROPOSED → ACTIVE → PROMOTED)依赖 Phase 2 池认知对账 beat
            (<Text code>ENABLE_POOL_COGNITIVE_RECONCILE</Text>,当前 OFF / 未部署);现阶段池内假设
            恒为 PROPOSED,触发器(trigger_histogram)/ RoundStats 维度结构性恒空,已停用。本页改以
            <Text strong> hyp_intent 池漏斗 </Text>(HG worker 认领源)+ <Text strong>Hypothesis.pillar 分布</Text>
            (live)观测假设供给侧。
          </span>
        }
      />

      <OpsSectionCard
        title="hyp_intent 池漏斗(HG 认领源)"
        source={pool.data ? 'service' : 'missing'}
        onRefresh={pool.refetch}
        loading={pool.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsHypothesisHealth}
            label="重跑 hypothesis-health"
            onSuccess={() =>
              setTimeout(() => {
                latest.refetch()
                pillar.refetch()
              }, 3000)
            }
          />
        }
      >
        {pool.loading && !pool.data ? (
          <Spin />
        ) : !pool.data?.enabled ? (
          <Empty description="ENABLE_POOL_PIPELINE OFF — 池未启用,hyp_intent 漏斗无数据" />
        ) : intentTotal === 0 ? (
          <Empty description="hyp_intent 队列为空(scheduler beat 尚未落 intent,或全部已 PURGED)" />
        ) : (
          <>
            <Row gutter={[16, 16]} style={{ marginBottom: 8 }}>
              {INTENT_FUNNEL.map((s) => {
                const v = intent[s.key] || 0
                return (
                  <Col xs={12} sm={Math.floor(24 / INTENT_FUNNEL.length) || 4} key={s.key}>
                    <Tooltip title={s.desc}>
                      <Statistic
                        title={s.label}
                        value={v}
                        valueStyle={{ color: v > 0 ? s.color : undefined }}
                      />
                    </Tooltip>
                  </Col>
                )
              })}
            </Row>

            {/* 横向漏斗条:用占最大阶段的比例画宽度 */}
            <Space direction="vertical" style={{ width: '100%' }} size={6}>
              {INTENT_FUNNEL.map((s) => {
                const v = intent[s.key] || 0
                const pct = Math.round((v / intentMax) * 100)
                const sharePct = intentTotal ? ((v / intentTotal) * 100).toFixed(1) : '0.0'
                return (
                  <div key={s.key}>
                    <Row justify="space-between" style={{ marginBottom: 2 }}>
                      <Text style={{ fontSize: 12 }}>{s.label}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {v.toLocaleString()} · {sharePct}%
                      </Text>
                    </Row>
                    <div style={{ background: 'rgba(255,255,255,0.06)', borderRadius: 4, height: 18 }}>
                      <div
                        style={{
                          width: `${pct}%`,
                          minWidth: v > 0 ? 4 : 0,
                          height: '100%',
                          background: s.color,
                          borderRadius: 4,
                          transition: 'width .3s',
                        }}
                      />
                    </div>
                  </div>
                )
              })}
            </Space>

            <div style={{ marginTop: 12 }}>
              <Space size={[8, 8]} wrap>
                <Tag>合计 intent: <b>{intentTotal.toLocaleString()}</b></Tag>
                {(pool.data?.stuck_past_lease?.hyp_intent || 0) > 0 ? (
                  <Tag color="red">
                    超 lease 卡死(CLAIMED): {pool.data.stuck_past_lease.hyp_intent}
                  </Tag>
                ) : (
                  <Tag color="green">无超 lease 卡死 ✓</Tag>
                )}
              </Space>
              <div style={{ marginTop: 6 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  漏斗源 = <Text code>GET /ops/pools/status</Text> 的 hyp_intent;CLAIMED 应短暂流转,
                  长期堆积或超 lease 卡死请到「挖掘池」监控页排查 worker / lease。
                </Text>
              </div>
            </div>
          </>
        )}
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={14}>
          <OpsSectionCard
            title="Hypothesis.pillar 分布(跨 region 聚合)"
            source={pillar.data?.source}
            staleDays={pillarStale}
            onRefresh={pillar.refetch}
            loading={pillar.loading}
          >
            {pillar.loading && !pillar.data ? (
              <Spin />
            ) : pillarRows.length === 0 ? (
              <Empty description="暂无 pillar 数据(7 日内无 stamped alpha,或 pillar beat 未跑过)" />
            ) : (
              <>
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={pillarRows} layout="vertical">
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                    <XAxis type="number" stroke="#888" allowDecimals={false} />
                    <YAxis dataKey="pillar" type="category" stroke="#888" width={140} />
                    <RTooltip
                      contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
                      formatter={(val) => [
                        `${val}  (${pillarTotal ? ((val / pillarTotal) * 100).toFixed(1) : 0}%)`,
                        '近似计数',
                      ]}
                    />
                    <Bar dataKey="count">
                      {pillarRows.map((row, i) => (
                        <Cell key={row.pillar} fill={PILLAR_PALETTE[i % PILLAR_PALETTE.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  近似计数 = 各 region <Text code>shares × stamped_total</Text> 求和;权威分桶/趋势/
                  deficit 见「五支柱平衡」页。
                </Text>
              </>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={10}>
          <OpsSectionCard
            title="Hypothesis 概览(仍 live 维度)"
            source={latest.data?.source}
            staleDays={summary.stale_days}
            onRefresh={latest.refetch}
            loading={latest.loading}
          >
            {latest.loading && !latest.data ? (
              <Spin />
            ) : (
              <Space direction="vertical" style={{ width: '100%' }} size="large">
                <Row gutter={[16, 16]}>
                  <Col span={12}>
                    <Tooltip title="ACTIVE + PROMOTED 计数(晋升 beat 未部署时通常为 0)">
                      <Statistic title="ACTIVE+PROMOTED" value={summary.total_active || 0} />
                    </Tooltip>
                  </Col>
                  <Col span={12}>
                    <Tooltip title="pillar 近似计数合计(跨 region)">
                      <Statistic title="pillar 计数合计" value={pillarTotal} />
                    </Tooltip>
                  </Col>
                </Row>
                <Alert
                  type="warning"
                  showIcon
                  banner
                  message="触发率 / thesis_score / 30d 趋势已停用"
                  description={
                    <Text style={{ fontSize: 12 }}>
                      trigger_histogram、score_buckets、recent_rounds 依赖 HypothesisRoundStats
                      (池内不写),结构性恒空,已从本页移除。
                    </Text>
                  }
                />
              </Space>
            )}
          </OpsSectionCard>
        </Col>
      </Row>
    </Space>
  )
}
