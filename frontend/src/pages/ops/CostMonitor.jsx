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
  DollarOutlined,
  InfoCircleOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'

const { Title, Text } = Typography

// 开关键名的中文显示（不改 key，仅用于展示）
const FLAG_LABEL = {
  ENABLE_COST_TELEMETRY: '成本遥测',
}

/**
 * CostMonitor — /ops/cost-monitor (2026-05-19).
 *
 * G2 Phase A LLM cost telemetry page. Surfaces the per-call cost stream
 * (llm_call_log) so operators can spot a runaway pillar / node_key / task
 * before COST_CEILING_USD_PER_TASK_DAY ever ships.
 *
 * Mirrors CoSTEERMonitor layout: top healthy-gate banner + flag tags + KPI
 * row + grouped Card+BarChart panels + top tasks Table + 24h hourly trend.
 *
 * Refetches every 30s via react-query.
 */
export default function CostMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ['ops/cost/telemetry', days],
    queryFn: () => api.getOpsCostTelemetry(days, 10),
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
        message="加载成本遥测数据失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无成本遥测数据" />

  const flagOn = !!data.flags?.ENABLE_COST_TELEMETRY
  const healthy = !!data.is_healthy
  const errPct = (data.error_rate ?? 0) * 100
  const errColor = errPct <= 5 ? '#00ff88' : errPct <= 10 ? '#ffb700' : '#ff4d4f'
  const costColor = '#00d4ff'
  const flagsList = Object.entries(data.flags || {})

  const byModelChart = (data.by_model || []).slice(0, 10).map((b) => ({
    label: b.label,
    cost: b.cost_usd,
    calls: b.calls,
  }))
  const byNodeChart = (data.by_node_key || []).slice(0, 10).map((b) => ({
    label: b.label,
    cost: b.cost_usd,
    calls: b.calls,
  }))
  const byPillarChart = (data.by_pillar || []).slice(0, 10).map((b) => ({
    label: b.label,
    cost: b.cost_usd,
    calls: b.calls,
  }))
  const hourly = (data.hourly_last_24h || []).map((h) => ({
    hour: h.hour_utc?.slice(11, 16) || '',
    cost: h.cost_usd,
    calls: h.calls,
  }))

  const groupColumns = [
    {
      title: '名称',
      dataIndex: 'label',
      key: 'label',
      ellipsis: true,
    },
    {
      title: '调用',
      dataIndex: 'calls',
      key: 'calls',
      width: 80,
      align: 'right',
    },
    {
      title: 'Token 数',
      dataIndex: 'tokens_total',
      key: 'tokens_total',
      width: 110,
      align: 'right',
    },
    {
      title: '成本 (USD)',
      dataIndex: 'cost_usd',
      key: 'cost_usd',
      width: 110,
      align: 'right',
      render: (v) => v?.toFixed(4) ?? '—',
    },
    {
      title: '成功率',
      dataIndex: 'success_rate',
      key: 'success_rate',
      width: 90,
      align: 'right',
      render: (v) => `${(v * 100).toFixed(1)}%`,
    },
    {
      title: '平均延迟 (ms)',
      dataIndex: 'avg_latency_ms',
      key: 'avg_latency_ms',
      width: 120,
      align: 'right',
      render: (v) => v?.toFixed(0) ?? '—',
    },
  ]

  const taskColumns = [
    { title: '任务 ID', dataIndex: 'task_id', key: 'task_id', width: 90 },
    { title: '调用', dataIndex: 'calls', key: 'calls', width: 80, align: 'right' },
    {
      title: 'Token',
      dataIndex: 'tokens_total',
      key: 'tokens_total',
      width: 100,
      align: 'right',
    },
    {
      title: '成本 (USD)',
      dataIndex: 'cost_usd',
      key: 'cost_usd',
      width: 120,
      align: 'right',
      render: (v) => v?.toFixed(4) ?? '—',
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <DollarOutlined style={{ marginRight: 8 }} />
          LLM 成本监控
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
                {FLAG_LABEL[k] || k}: {v ? '开' : '关'}
              </Tag>
            ))}
            <Text type="secondary">
              健康门槛：错误率 ≤ {((data.healthy_gates?.error_rate_max ?? 0.1) * 100).toFixed(0)}%
              {' · '}最少总调用数 ≥ {data.healthy_gates?.min_total_calls ?? 1}
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              成本遥测开关关闭中，调用日志不会被写入。需在功能开关控制台开启后才能采集数据。
            </Text>
          ) : !healthy ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              窗口内总调用数 = {data.total_calls}，错误率 = {errPct.toFixed(2)}%
              。请确认开关已生效或检查模型厂商服务是否健康。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="窗口内累计 LLM 成本(USD)。覆盖所有调用方：每轮挖掘 + 重试 + 宏观叙事提取 + 成功经验排序">
              <Statistic
                title={
                  <Space>
                    累计成本 (USD)
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={data.total_cost_usd ?? 0}
                precision={4}
                prefix={<DollarOutlined />}
                valueStyle={{ color: costColor }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="累计调用"
              value={data.total_calls ?? 0}
              prefix={<ThunderboltOutlined />}
              valueStyle={{ color: '#9c88ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              成功 {data.successful_calls ?? 0} · 失败 {data.failed_calls ?? 0}
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="失败比例(失败 / 总调用)。健康部署 ≤ 10%">
              <Statistic
                title={
                  <Space>
                    错误率
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={errPct}
                precision={2}
                suffix="%"
                valueStyle={{ color: errColor }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="单次平均成本 (USD)"
              value={data.avg_cost_per_call ?? 0}
              precision={6}
              valueStyle={{ color: '#ffb700' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              单次平均 Token {Math.round(data.avg_tokens_per_call ?? 0)}
            </Text>
          </Card>
        </Col>
      </Row>

      {/* By group bar charts */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="按模型分布（成本降序）" size="small">
            {byModelChart.length === 0 ? (
              <Empty description="窗口内暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={byModelChart}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="label" interval={0} angle={-15} textAnchor="end" height={60} />
                  <YAxis />
                  <RTooltip formatter={(v, k) => (k === 'cost' ? `$${v.toFixed(4)}` : v)} />
                  <Legend formatter={(v) => (v === 'cost' ? '成本 (USD)' : '调用数')} />
                  <Bar dataKey="cost" fill="#00d4ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="按功能模块分布（成本降序）" size="small">
            {byNodeChart.length === 0 ? (
              <Empty description="窗口内暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={byNodeChart}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="label" interval={0} angle={-15} textAnchor="end" height={60} />
                  <YAxis />
                  <RTooltip formatter={(v, k) => (k === 'cost' ? `$${v.toFixed(4)}` : v)} />
                  <Legend formatter={(v) => (v === 'cost' ? '成本 (USD)' : '调用数')} />
                  <Bar dataKey="cost" fill="#9c88ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="按因子类别分布（成本降序）" size="small">
            {byPillarChart.length === 0 ? (
              <Empty description="窗口内暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={byPillarChart}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="label" interval={0} angle={-15} textAnchor="end" height={60} />
                  <YAxis />
                  <RTooltip formatter={(v, k) => (k === 'cost' ? `$${v.toFixed(4)}` : v)} />
                  <Legend formatter={(v) => (v === 'cost' ? '成本 (USD)' : '调用数')} />
                  <Bar dataKey="cost" fill="#13c2c2" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="近 24 小时成本时间线（UTC）" size="small">
            {hourly.length === 0 ? (
              <Empty description="近 24 小时暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <LineChart data={hourly}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="hour" />
                  <YAxis />
                  <RTooltip formatter={(v) => `$${Number(v).toFixed(4)}`} />
                  <Legend formatter={() => '成本 (USD)'} />
                  <Line
                    type="monotone"
                    dataKey="cost"
                    stroke="#00d4ff"
                    strokeWidth={2}
                    dot={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>
      </Row>

      {/* Tables — per-model & top tasks */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card className="glass-card" title="按模型明细" size="small">
            <Table
              size="small"
              rowKey="label"
              dataSource={data.by_model || []}
              columns={groupColumns}
              pagination={false}
              locale={{ emptyText: '暂无数据' }}
            />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card
            className="glass-card"
            title="高成本任务 Top 10"
            size="small"
            extra={
              <Tag onClick={() => refetch()} style={{ cursor: 'pointer' }}>
                刷新
              </Tag>
            }
          >
            <Table
              size="small"
              rowKey={(r) => r.task_id ?? '(none)'}
              dataSource={data.top_tasks_by_cost || []}
              columns={taskColumns}
              pagination={false}
              locale={{ emptyText: '窗口内暂无任务消耗' }}
            />
          </Card>
        </Col>
      </Row>
    </div>
  )
}
