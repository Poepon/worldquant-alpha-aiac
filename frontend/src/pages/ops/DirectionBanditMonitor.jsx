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
  BranchesOutlined,
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
 * DirectionBanditMonitor — /ops/direction-bandit-monitor (2026-05-19).
 *
 * G1 Phase A direction-bandit telemetry page. Surfaces per-arm pulls /
 * observed reward / approx regret + Phase 1 R2/Q7 GO-gate readiness
 * counter (segments with ≥ DIRECTION_BANDIT_GO_GATE_MIN_PULLS observed
 * selects).
 *
 * Mirrors CoSTEERMonitor layout: top healthy-gate banner + flag tags + KPI
 * row + per-arm BarChart + per-segment Table. Phase A is observation-only
 * — the bandit currently runs in shadow-soft (recommendation hint only).
 *
 * Refetches every 30s via react-query.
 */
export default function DirectionBanditMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/direction-bandit/telemetry', days],
    queryFn: () => api.getOpsDirectionBanditTelemetry(days, 10),
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
        message="加载 direction-bandit telemetry 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 direction-bandit telemetry 数据" />

  const flagOn = !!data.flags?.ENABLE_DIRECTION_BANDIT
  const healthy = !!data.is_healthy
  const gateMin = data.go_gate_min_pulls ?? 10
  const gateReady = data.go_gate_segments_ready ?? 0
  const gateColor = gateReady >= 1 ? '#00ff88' : '#ffb700'
  const flagsList = Object.entries(data.flags || {})

  // Per-arm bar — observed reward vs PASS rate side-by-side
  const armBars = (data.by_arm || []).map((a) => ({
    arm: a.arm,
    pulls: a.pulls,
    avg_reward: a.avg_observed_reward,
    pass_rate_pct:
      a.pass_rate !== null && a.pass_rate !== undefined
        ? Math.round(a.pass_rate * 10000) / 100
        : 0,
    sample_size: a.sample_size_for_reward,
    cold_pulls: a.cold_start_pulls,
    pass_sample: a.pass_sample_size,
  }))

  const armColumns = [
    {
      title: '臂 (arm)',
      dataIndex: 'arm',
      key: 'arm',
      width: 180,
      render: (v) => <Tag color="purple">{v || '(none)'}</Tag>,
    },
    {
      title: '拉取次数',
      dataIndex: 'pulls',
      key: 'pulls',
      width: 90,
      align: 'right',
    },
    {
      title: '冷启动占比',
      key: 'cold_pct',
      width: 110,
      align: 'right',
      render: (_, r) => {
        if (!r.pulls) return '—'
        const pct = (r.cold_pulls / r.pulls) * 100
        return `${pct.toFixed(1)}%`
      },
    },
    {
      title: '平均观测 reward',
      dataIndex: 'avg_reward',
      key: 'avg_reward',
      width: 140,
      align: 'right',
      render: (v, r) =>
        r.sample_size > 0 ? (
          <Tooltip title={`基于 ${r.sample_size} 个非空 reward 样本`}>
            {v?.toFixed(4) ?? '—'}
          </Tooltip>
        ) : (
          <Text type="secondary">尚无样本</Text>
        ),
    },
    {
      title: 'PASS 率',
      key: 'pass_rate_pct',
      width: 110,
      align: 'right',
      render: (_, r) =>
        r.pass_sample > 0 ? (
          <Tooltip title={`基于 ${r.pass_sample} 条已落地 alpha`}>
            {r.pass_rate_pct.toFixed(2)}%
          </Tooltip>
        ) : (
          <Text type="secondary">尚无样本</Text>
        ),
    },
  ]

  const segColumns = [
    {
      title: 'segment_id',
      dataIndex: 'segment_id',
      key: 'segment_id',
      ellipsis: true,
    },
    {
      title: '地区',
      dataIndex: 'region',
      key: 'region',
      width: 80,
      render: (v) => (v ? <Tag>{v}</Tag> : '—'),
    },
    {
      title: '数据集',
      dataIndex: 'dataset_category',
      key: 'dataset_category',
      width: 120,
      ellipsis: true,
    },
    {
      title: '失败模式',
      dataIndex: 'failure_pattern',
      key: 'failure_pattern',
      width: 140,
      ellipsis: true,
    },
    {
      title: '拉取',
      dataIndex: 'total_pulls',
      key: 'total_pulls',
      width: 70,
      align: 'right',
    },
    {
      title: '不同臂数',
      dataIndex: 'distinct_arms',
      key: 'distinct_arms',
      width: 90,
      align: 'right',
      render: (v) => (
        <Tooltip title="该 segment 经历过多少不同 arm — 单臂 segment 是过拟合候选">
          {v}
        </Tooltip>
      ),
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <BranchesOutlined style={{ marginRight: 8 }} />
          方向 Bandit 监控（G1 Phase A）
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
            <strong>健康状态：{healthy ? '健康' : '不健康'}</strong>
            {flagsList.map(([k, v]) => (
              <Tag key={k} color={v ? 'success' : 'default'}>
                {k}: {v ? '开' : '关'}
              </Tag>
            ))}
            <Text type="secondary">
              健康门槛: flag ON + total_log_rows &gt; 0 + 至少 1 个 segment 拉取 ≥ {gateMin}
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              ENABLE_DIRECTION_BANDIT 关闭中,direction_bandit_log 不会被写入。
              开启后 bandit 在 shadow-soft 模式运行 — LLM 收到 prompt hint,
              但可以 override。Phase A 仅观察,不阻塞任务。
            </Text>
          ) : (data.total_log_rows ?? 0) === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              flag 已开,但窗口内 direction_bandit_log 无数据。
              确认是否有任务在运行 / persistence 节点是否盖戳。
            </Text>
          ) : gateReady < 1 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              窗口内还没有任何 segment 达到 ≥ {gateMin} 次拉取 — Phase 1 R2/Q7 GO 门未达。
              继续观察或拉长窗口。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="窗口内拉取总次数"
              value={data.total_log_rows ?? 0}
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              覆盖 {data.distinct_tasks ?? 0} 个任务 · {data.distinct_segments ?? 0} 个 segment
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title={`至少 ${gateMin} 次拉取的 segment 数 — Phase 1 GO gate 信号`}>
              <Statistic
                title={
                  <Space>
                    GO Gate 达标 segment
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={gateReady}
                valueStyle={{ color: gateColor }}
              />
            </Tooltip>
            <Text type="secondary" style={{ fontSize: 12 }}>
              门槛: 拉取 ≥ {gateMin}
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="样本量最大 + 平均 reward 最高的 arm（基于非空 observed_reward）">
              <Statistic
                title={
                  <Space>
                    最佳 arm
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={data.best_arm || '—'}
                prefix={<TrophyOutlined />}
                valueStyle={{ color: '#ffb700', fontSize: 18 }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                平均 reward {(data.best_arm_avg_reward ?? 0).toFixed(4)}
              </Text>
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="mean(best arm) − mean(actual selections) — 越小越好。0 表示总是选最优臂">
              <Statistic
                title={
                  <Space>
                    近似 Regret
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={data.approx_regret ?? 0}
                precision={4}
                valueStyle={{ color: '#9c88ff' }}
              />
            </Tooltip>
          </Card>
        </Col>
      </Row>

      {/* Per-arm bar charts */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="每个 arm 的拉取次数" size="small">
            {armBars.length === 0 ? (
              <Empty description="窗口内暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={armBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="arm" interval={0} angle={-15} textAnchor="end" height={60} />
                  <YAxis allowDecimals={false} />
                  <RTooltip />
                  <Legend formatter={() => '拉取次数'} />
                  <Bar dataKey="pulls" fill="#00d4ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="每个 arm 的平均观测 Reward" size="small">
            {armBars.length === 0 ? (
              <Empty description="窗口内暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={armBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="arm" interval={0} angle={-15} textAnchor="end" height={60} />
                  <YAxis />
                  <RTooltip formatter={(v) => Number(v).toFixed(4)} />
                  <Legend formatter={() => '平均 reward'} />
                  <Bar dataKey="avg_reward" fill="#9c88ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>
      </Row>

      {/* Per-arm table */}
      <Card className="glass-card" title="每个 arm 明细" style={{ marginTop: 16 }} size="small">
        <Table
          size="small"
          rowKey="arm"
          dataSource={armBars}
          columns={armColumns}
          pagination={false}
          locale={{ emptyText: '窗口内暂无 arm 数据' }}
        />
      </Card>

      {/* Per-segment table */}
      <Card
        className="glass-card"
        title="活跃 segment Top 10（按拉取次数降序）"
        style={{ marginTop: 16 }}
        size="small"
      >
        <Table
          size="small"
          rowKey="segment_id"
          dataSource={data.by_segment || []}
          columns={segColumns}
          pagination={false}
          locale={{ emptyText: '窗口内暂无 segment 数据' }}
        />
      </Card>
    </div>
  )
}
