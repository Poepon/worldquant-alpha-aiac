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
  { key: 'PENDING', label: '待认领', color: '#1677ff', desc: '调度器已把想法放入队列,等待想法生成环节认领' },
  { key: 'CLAIMED', label: '认领中', color: '#faad14', desc: '想法生成环节已领取,正在生成候选(应短暂;处理超时见挖掘流水线监控页)' },
  { key: 'DONE', label: '已完成', color: '#52c41a', desc: '想法生成环节已据此产出候选(终态)' },
  { key: 'FAILED', label: '失败', color: '#ff4d4f', desc: '想法生成处理失败(终态)' },
  { key: 'PURGED', label: '已清除', color: '#8c8c8c', desc: '被清理 / 作废(终态)' },
]

// pillar bar 调色板
const PILLAR_PALETTE = ['#00d4ff', '#9c88ff', '#ffb700', '#ff8c00', '#52c41a', '#ff4d4f', '#13c2c2']

export default function HypothesisHealthMonitor() {
  // 池状态(主体漏斗源)。注:useOpsData 仅 mount/手动 fetch,无自动轮询;
  // 需实时刷新点右上「重新运行」或刷新页面(漏斗类指标非秒级敏感)。
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
        message="假设生命周期晋升已停用 — 当前流水线内假设恒为「待定」状态"
        description={
          <span>
            假设生命周期晋升(待定 → 生效中 → 提升复用)依赖第二阶段的知识库对账定时任务
            (功能开关 <Text code>ENABLE_POOL_COGNITIVE_RECONCILE</Text>,当前关闭 / 未部署);现阶段流水线内假设
            恒为「待定」,触发器分布 / 每轮统计维度结构性恒空,已停用。本页改以
            <Text strong> 想法队列漏斗 </Text>(想法生成环节认领源)+ <Text strong>假设的因子类别分布</Text>
            (实时)观测假设供给侧。
          </span>
        }
      />

      <OpsSectionCard
        title="想法队列漏斗（想法生成环节认领源）"
        source={pool.data ? 'service' : 'missing'}
        onRefresh={pool.refetch}
        loading={pool.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsHypothesisHealth}
            label="重新统计假设健康度"
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
          <Empty description="挖掘流水线开关已关闭 — 流水线未启用,想法队列漏斗无数据" />
        ) : intentTotal === 0 ? (
          <Empty description="想法队列为空(调度器尚未放入想法,或全部已清除)" />
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
                <Tag>合计想法: <b>{intentTotal.toLocaleString()}</b></Tag>
                {(pool.data?.stuck_past_lease?.hyp_intent || 0) > 0 ? (
                  <Tag color="red">
                    处理超时卡死（认领后未完成）: {pool.data.stuck_past_lease.hyp_intent}
                  </Tag>
                ) : (
                  <Tag color="green">无处理超时卡死 ✓</Tag>
                )}
              </Space>
              <div style={{ marginTop: 6 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  漏斗数据源 = <Text code>GET /ops/pools/status</Text> 的想法队列;「认领中」应短暂流转,
                  长期堆积或处理超时卡死请到「挖掘流水线」监控页排查工作进程 / 超时时限。
                </Text>
              </div>
            </div>
          </>
        )}
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={14}>
          <OpsSectionCard
            title="假设的因子类别分布（跨地区聚合）"
            source={pillar.data?.source}
            staleDays={pillarStale}
            onRefresh={pillar.refetch}
            loading={pillar.loading}
          >
            {pillar.loading && !pillar.data ? (
              <Spin />
            ) : pillarRows.length === 0 ? (
              <Empty description="暂无因子类别数据(7 日内无已标注 alpha,或因子类别统计任务未跑过)" />
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
                  近似计数 = 各地区「占比 × 已标注总数」求和;权威分桶 / 趋势 /
                  缺口见「五大因子类别平衡」页。
                </Text>
              </>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={10}>
          <OpsSectionCard
            title="假设概览（仍实时的维度）"
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
                    <Tooltip title="「生效中 + 已提升复用」计数(晋升定时任务未部署时通常为 0)">
                      <Statistic title="生效中 + 已提升复用" value={summary.total_active || 0} />
                    </Tooltip>
                  </Col>
                  <Col span={12}>
                    <Tooltip title="因子类别近似计数合计(跨地区)">
                      <Statistic title="因子类别计数合计" value={pillarTotal} />
                    </Tooltip>
                  </Col>
                </Row>
                <Alert
                  type="warning"
                  showIcon
                  banner
                  message="触发率 / 假设评分 / 30 天趋势已停用"
                  description={
                    <Text style={{ fontSize: 12 }}>
                      触发器分布、评分分桶、近期各轮统计依赖「每轮假设统计」
                      (流水线内不写),结构性恒空,已从本页移除。
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
