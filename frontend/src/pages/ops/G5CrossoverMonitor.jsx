import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import {
  Alert,
  Card,
  Col,
  Empty,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ForkOutlined,
  InfoCircleOutlined,
  TrophyOutlined,
} from '@ant-design/icons'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * G5CrossoverMonitor — /ops/g5-monitor (2026-05-19).
 *
 * G5 Phase A trajectory crossover telemetry. Surfaces per-strategy +
 * per-pillar-pair crossover frequency, offspring volume, and PASS rate
 * (joined from alphas.metrics._g5_crossover_parent_ids).
 *
 * Mirrors CoSTEERMonitor / DirectionBanditMonitor / G3OriginalityMonitor:
 *   - top healthy-gate banner + flag tags
 *   - KPI row (4 stats)
 *   - per-strategy BarChart + per-pillar-pair BarChart
 *   - recent crossover events Table
 *
 * Healthy gate (descriptive, Phase A only):
 *   - ENABLE_G5_CROSSOVER flag ON
 *   - total_crossover_calls > 0 (调用真发生过)
 *   - offspring_pass_rate > 0 (至少有一个 offspring 真 PASS)
 *
 * Refetches every 30s via react-query.
 */
export default function G5CrossoverMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/g5/crossover-stats', days],
    queryFn: () => api.getOpsG5CrossoverStats(days),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }
  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        message="加载 G5 crossover stats 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 G5 crossover stats 数据" />

  const flagOn = !!data.flags?.ENABLE_G5_CROSSOVER
  const healthy = !!data.is_healthy
  const totalCalls = data.total_crossover_calls ?? 0
  const offspringTotal = data.offspring_total ?? 0
  const offspringPass = data.offspring_pass_count ?? 0
  const passRate = (data.offspring_pass_rate ?? 0) * 100
  // Phase A health hint — any PASS = green; calls present but 0 PASS = yellow; no calls = grey
  const passRateColor =
    offspringTotal === 0
      ? '#9c88ff'
      : passRate >= 20
      ? '#00ff88'
      : passRate >= 5
      ? '#ffb700'
      : '#ff4d4f'
  const flagsList = Object.entries(data.flags || {})

  // Pick the strategy with the most outcome_pass_count as "best"
  const perStrategy = data.per_strategy || []
  const bestStrategy = perStrategy.reduce(
    (acc, s) =>
      (s.outcome_pass_count || 0) > (acc.outcome_pass_count || 0) ? s : acc,
    { strategy: '—', outcome_pass_count: 0 },
  )

  const strategyBars = perStrategy.map((s) => ({
    strategy: s.strategy,
    calls: s.calls,
    avg_offspring: s.avg_offspring_count,
    pass: s.outcome_pass_count,
  }))

  const pillarPairBars = (data.per_pillar_pair || []).map((p) => ({
    pair: p.pillar_pair,
    calls: p.calls,
    pass: p.outcome_pass_count,
  }))

  const strategyColumns = [
    {
      title: '策略 (strategy)',
      dataIndex: 'strategy',
      key: 'strategy',
      width: 220,
      render: (v) => <Tag color="purple">{v}</Tag>,
    },
    {
      title: '调用次数',
      dataIndex: 'calls',
      key: 'calls',
      width: 100,
      align: 'right',
    },
    {
      title: '平均 offspring',
      dataIndex: 'avg_offspring',
      key: 'avg_offspring',
      width: 130,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: 'PASS 数',
      dataIndex: 'pass',
      key: 'pass',
      width: 100,
      align: 'right',
      render: (v) => (
        <Tag color={v > 0 ? 'success' : 'default'}>{v ?? 0}</Tag>
      ),
    },
  ]

  const pillarPairColumns = [
    {
      title: '支柱对 (pillar_pair)',
      dataIndex: 'pair',
      key: 'pair',
      ellipsis: true,
      render: (v) => <Tag color="cyan">{v || '(none)'}</Tag>,
    },
    {
      title: '调用次数',
      dataIndex: 'calls',
      key: 'calls',
      width: 100,
      align: 'right',
    },
    {
      title: 'PASS 数',
      dataIndex: 'pass',
      key: 'pass',
      width: 100,
      align: 'right',
      render: (v) => (
        <Tag color={v > 0 ? 'success' : 'default'}>{v ?? 0}</Tag>
      ),
    },
  ]

  const eventColumns = [
    {
      title: 'id',
      dataIndex: 'id',
      key: 'id',
      width: 70,
      align: 'right',
      render: (v) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: '任务',
      dataIndex: 'task_id',
      key: 'task_id',
      width: 80,
      align: 'right',
    },
    {
      title: 'round',
      dataIndex: 'round_idx',
      key: 'round_idx',
      width: 80,
      align: 'right',
    },
    {
      title: '父 A',
      dataIndex: 'parent_a_alpha_id',
      key: 'parent_a_alpha_id',
      width: 90,
      align: 'right',
      render: (v) =>
        v ? <Text code style={{ fontSize: 12 }}>{v}</Text> : '—',
    },
    {
      title: '父 B',
      dataIndex: 'parent_b_alpha_id',
      key: 'parent_b_alpha_id',
      width: 90,
      align: 'right',
      render: (v) =>
        v ? <Text code style={{ fontSize: 12 }}>{v}</Text> : '—',
    },
    {
      title: 'offspring',
      dataIndex: 'offspring_count',
      key: 'offspring_count',
      width: 100,
      align: 'right',
    },
    {
      title: 'LLM 成本 (USD)',
      dataIndex: 'llm_cost_usd',
      key: 'llm_cost_usd',
      width: 130,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? `$${Number(v).toFixed(4)}` : '—'),
    },
    {
      title: '时间 (UTC)',
      dataIndex: 'created_at',
      key: 'created_at',
      ellipsis: true,
      render: (v) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—'),
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ForkOutlined style={{ marginRight: 8 }} />
          交叉变异监控（G5 Phase A）
        </Title>
        <Space>
          <Text type="secondary">时间窗口:</Text>
          <Select
            value={days}
            onChange={setDays}
            style={{ width: 130 }}
            options={[
              { value: 7, label: '近 7 天' },
              { value: 14, label: '近 14 天' },
              { value: 30, label: '近 30 天' },
            ]}
          />
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      {/* Health banner */}
      <Alert
        type={healthy ? 'success' : 'warning'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space wrap>
            <strong>健康状态：{healthy ? '健康' : '需关注'}</strong>
            {flagsList.map(([k, v]) => (
              <Tag key={k} color={v ? 'success' : 'default'}>
                {k}: {v ? '开' : '关'}
              </Tag>
            ))}
            <Text type="secondary">
              健康门槛: flag ON + total_crossover_calls &gt; 0 + 至少 1 个 offspring 真 PASS
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              ENABLE_G5_CROSSOVER 关闭中,trajectory crossover 不会触发。
              开启后,2 个 PASS 父 alpha → LLM combine → BRAIN simulate,
              产出 offspring 通过 alphas.metrics._g5_crossover_parent_ids 反向 attribute。
            </Text>
          ) : totalCalls === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              flag 已开,但窗口内无 crossover 调用 — 检查是否有任务凑齐 ≥ 2 PASS父
              alpha 触发 combine。继续运行任务或拉长窗口。
            </Text>
          ) : offspringPass === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              {totalCalls} 次调用产生 {offspringTotal} 个 offspring,但 0 PASS —
              LLM combine 质量需要观察,或检查 BRAIN simulate 是否被
              EVAL_* 阈值拒绝。Phase B 标定前继续观察。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="窗口内 crossover 调用"
              value={totalCalls}
              prefix={<ForkOutlined />}
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              每次取 2 PASS 父 alpha
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="所有 crossover 调用产出 offspring 总数(SUM offspring_count)">
              <Statistic
                title={
                  <Space>
                    Offspring 总数
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={offspringTotal}
                valueStyle={{ color: '#9c88ff' }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                其中 PASS {offspringPass}
              </Text>
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="offspring_pass_rate — offspring 中真 PASS 的比例,Phase B 用于对比常规 alpha PASS 率">
              <Statistic
                title={
                  <Space>
                    Offspring PASS 率
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={passRate}
                precision={2}
                suffix="%"
                valueStyle={{ color: passRateColor }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="按 outcome_pass_count 降序最优的 crossover 策略">
              <Statistic
                title={
                  <Space>
                    最佳策略
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={bestStrategy.strategy || '—'}
                prefix={<TrophyOutlined />}
                valueStyle={{ color: '#ffb700', fontSize: 16 }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                PASS {bestStrategy.outcome_pass_count ?? 0}
              </Text>
            </Tooltip>
          </Card>
        </Col>
      </Row>

      {/* Per-strategy + per-pillar-pair bar charts */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="按策略分布（调用次数 / PASS）" size="small">
            {strategyBars.length === 0 ? (
              <Empty description="窗口内暂无 crossover 调用" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={strategyBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="strategy"
                    interval={0}
                    angle={-15}
                    textAnchor="end"
                    height={70}
                  />
                  <YAxis allowDecimals={false} />
                  <RTooltip />
                  <Legend
                    formatter={(v) =>
                      v === 'calls' ? '调用次数' : v === 'pass' ? 'PASS 数' : v
                    }
                  />
                  <Bar dataKey="calls" fill="#00d4ff" />
                  <Bar dataKey="pass" fill="#00ff88" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              5 种策略: weighted_sum / sequential_filter / cross_sectional_confirm /
              wrapper_graft / difference_filter
            </Text>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="按支柱对分布（调用次数 / PASS）" size="small">
            {pillarPairBars.length === 0 ? (
              <Empty description="窗口内暂无 crossover 调用" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={pillarPairBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="pair"
                    interval={0}
                    angle={-15}
                    textAnchor="end"
                    height={70}
                  />
                  <YAxis allowDecimals={false} />
                  <RTooltip />
                  <Legend
                    formatter={(v) =>
                      v === 'calls' ? '调用次数' : v === 'pass' ? 'PASS 数' : v
                    }
                  />
                  <Bar dataKey="calls" fill="#13c2c2" />
                  <Bar dataKey="pass" fill="#00ff88" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              形如 "momentum→value" — 跨支柱 crossover 是多样性产出主源。
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Per-strategy table */}
      <Card className="glass-card" title="按策略明细" style={{ marginTop: 16 }} size="small">
        <Table
          size="small"
          rowKey="strategy"
          dataSource={strategyBars}
          columns={strategyColumns}
          pagination={false}
          locale={{ emptyText: '窗口内暂无策略数据' }}
        />
      </Card>

      {/* Per-pillar-pair table */}
      <Card className="glass-card" title="按支柱对明细" style={{ marginTop: 16 }} size="small">
        <Table
          size="small"
          rowKey="pair"
          dataSource={pillarPairBars}
          columns={pillarPairColumns}
          pagination={false}
          locale={{ emptyText: '窗口内暂无支柱对数据' }}
        />
      </Card>

      {/* Recent events table */}
      <Card
        className="glass-card"
        title={
          <Space>
            最近 crossover 事件
            <Tooltip title="按 created_at DESC 排序,显示 task_id / round_idx / 父 alpha id / offspring 数 / LLM 成本">
              <InfoCircleOutlined style={{ color: '#9c88ff' }} />
            </Tooltip>
          </Space>
        }
        style={{ marginTop: 16 }}
        size="small"
      >
        <Table
          size="small"
          rowKey="id"
          dataSource={data.recent_events || []}
          columns={eventColumns}
          pagination={false}
          locale={{ emptyText: '窗口内暂无 crossover 事件' }}
        />
      </Card>
    </div>
  )
}
