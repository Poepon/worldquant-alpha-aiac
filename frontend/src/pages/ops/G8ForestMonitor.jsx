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
  ApartmentOutlined,
  InfoCircleOutlined,
  LinkOutlined,
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
 * G8ForestMonitor — /ops/g8-monitor (2026-05-19).
 *
 * G8 Phase A hypothesis-forest telemetry. Surfaces the cross-task PROMOTED
 * pool that fetch_cross_task_promoted injects into the LLM prompt, plus the
 * reverse attribution stamp (_g8_forest_referenced_ids on alphas.metrics).
 *
 * Mirrors CoSTEERMonitor / CostMonitor layout: top healthy-gate banner +
 * flag tags + KPI row + per-pillar BarChart + top forest entries Table.
 *
 * Healthy gate (descriptive, Phase A only):
 *   - ENABLE_HYPOTHESIS_FOREST_REUSE flag ON
 *   - eligible_count > 0 (pool has qualifying rows)
 *   - total_referenced_alphas > 0 (prompt block actually reaching alpha
 *     persistence within window)
 *
 * Refetches every 30s via react-query.
 */
export default function G8ForestMonitor() {
  const [days, setDays] = useState(7)
  const [region, setRegion] = useState('USA')

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/hypothesis/forest', days, region],
    queryFn: () => api.getOpsHypothesisForest(days, region, 10, 2, 1.0),
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
        message="加载 hypothesis forest 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 hypothesis forest 数据" />

  const flagOn = !!data.flags?.ENABLE_HYPOTHESIS_FOREST_REUSE
  const healthy = !!data.is_healthy
  const eligible = data.eligible_count ?? 0
  const totalRef = data.total_referenced_alphas ?? 0
  const passRef = data.reference_pass_count ?? 0
  const passRate = (data.reference_pass_rate ?? 0) * 100
  // descriptive coloring — green if any alpha PASSed under forest influence
  const passRateColor =
    totalRef === 0 ? '#9c88ff' : passRate >= 30 ? '#00ff88' : passRate >= 10 ? '#ffb700' : '#ff4d4f'
  const flagsList = Object.entries(data.flags || {})

  const pillarBars = (data.pillar_breakdown || []).map((p) => ({
    pillar: p.pillar,
    eligible_count: p.eligible_count,
    avg_sharpe: p.avg_sharpe,
    total_pass: p.total_pass,
  }))

  const entryColumns = [
    {
      title: 'ID',
      dataIndex: 'hypothesis_id',
      key: 'hypothesis_id',
      width: 70,
      align: 'right',
      render: (v) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: '陈述 (statement)',
      dataIndex: 'statement',
      key: 'statement',
      ellipsis: true,
      render: (v) => (
        <Tooltip title={v}>
          <Text style={{ fontSize: 12 }}>{v}</Text>
        </Tooltip>
      ),
    },
    {
      title: '支柱',
      dataIndex: 'pillar',
      key: 'pillar',
      width: 110,
      render: (v) =>
        v ? <Tag color="cyan">{v}</Tag> : <Tag color="default">(none)</Tag>,
    },
    {
      title: '地区',
      dataIndex: 'region',
      key: 'region',
      width: 70,
      render: (v) => <Tag>{v}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v) => (
        <Tag color={v === 'PROMOTED' ? 'gold' : v === 'ACTIVE' ? 'green' : 'default'}>
          {v || '—'}
        </Tag>
      ),
    },
    {
      title: '平均 Sharpe',
      dataIndex: 'sharpe_avg',
      key: 'sharpe_avg',
      width: 110,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(3) : '—'),
    },
    {
      title: 'PASS',
      dataIndex: 'pass_count',
      key: 'pass_count',
      width: 70,
      align: 'right',
    },
    {
      title: 'Alpha 数',
      dataIndex: 'alpha_count',
      key: 'alpha_count',
      width: 90,
      align: 'right',
    },
    {
      title: '被引用次数',
      dataIndex: 'times_referenced',
      key: 'times_referenced',
      width: 120,
      align: 'right',
      render: (v) => (
        <Tooltip title="窗口内 alphas.metrics._g8_forest_referenced_ids 包含此 hypothesis_id 的 PASS+FAIL 总条数">
          <Tag color={v > 0 ? 'success' : 'default'} icon={<LinkOutlined />}>
            {v}
          </Tag>
        </Tooltip>
      ),
    },
  ]

  const pillarColumns = [
    {
      title: '支柱',
      dataIndex: 'pillar',
      key: 'pillar',
      width: 140,
      render: (v) => (
        <Tag color={v === '(none)' ? 'default' : 'cyan'}>{v}</Tag>
      ),
    },
    {
      title: '候选数量',
      dataIndex: 'eligible_count',
      key: 'eligible_count',
      width: 100,
      align: 'right',
    },
    {
      title: '平均 Sharpe',
      dataIndex: 'avg_sharpe',
      key: 'avg_sharpe',
      width: 120,
      align: 'right',
      render: (v) => v?.toFixed(3) ?? '—',
    },
    {
      title: 'PASS 累计',
      dataIndex: 'total_pass',
      key: 'total_pass',
      width: 110,
      align: 'right',
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ApartmentOutlined style={{ marginRight: 8 }} />
          假设森林监控（G8 Phase A）
        </Title>
        <Space>
          <Text type="secondary">地区:</Text>
          <Select
            value={region}
            onChange={setRegion}
            style={{ width: 110 }}
            options={[
              { value: 'USA', label: 'USA' },
              { value: 'CHN', label: 'CHN' },
              { value: 'HKG', label: 'HKG' },
              { value: 'JPN', label: 'JPN' },
              { value: 'EUR', label: 'EUR' },
            ]}
          />
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
              健康门槛：flag ON + eligible_count ≥ {data.healthy_gates?.min_eligible_count ?? 1}
              {' · '}total_referenced_alphas ≥ {data.healthy_gates?.min_total_referenced ?? 1}
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              ENABLE_HYPOTHESIS_FOREST_REUSE 关闭中,cross-task PROMOTED hypothesis
              不会注入 LLM prompt。Feature Flag 控制台开启后,prompt block 会引用
              森林,并通过 alphas.metrics._g8_forest_referenced_ids 反向 attribute。
            </Text>
          ) : eligible === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              flag 已开,但 {region} 在 (pass_count ≥ 2 AND sharpe_avg ≥ 1.0) 门槛下
              暂无候选 hypothesis。等更多任务跑出 PASS 后,森林池会自然填充。
            </Text>
          ) : totalRef === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              森林池有 {eligible} 条候选,但窗口内无 alpha 被 stamp 引用 —
              检查 node_hypothesis 是否传 cross_task_hyps 到 _incremental_save_alphas,
              或加大时间窗口观察 reference 累积。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title={`pass_count ≥ 2 AND sharpe_avg ≥ 1.0 的 ACTIVE/PROMOTED hypothesis 数 — fetch_cross_task_promoted 的实际池子`}>
              <Statistic
                title={
                  <Space>
                    候选池规模
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={eligible}
                prefix={<ApartmentOutlined />}
                valueStyle={{ color: '#00d4ff' }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="窗口内 metrics 包含 _g8_forest_referenced_ids 的 alpha 总数(PASS+FAIL)">
              <Statistic
                title={
                  <Space>
                    被引用 alpha 数
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={totalRef}
                prefix={<LinkOutlined />}
                valueStyle={{ color: '#9c88ff' }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="其中 PASS"
              value={passRef}
              valueStyle={{ color: '#00ff88' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              quality_status ∈ PASS / PASS_PROVISIONAL
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="reference_pass_rate — 森林影响产出的 alpha PASS 率，用于 Phase B 对比 control 组">
              <Statistic
                title={
                  <Space>
                    引用 PASS 率
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
      </Row>

      {/* Per-pillar bar charts */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="森林按支柱分布（候选数量）" size="small">
            {pillarBars.length === 0 ? (
              <Empty description="森林池无候选" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={pillarBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="pillar"
                    interval={0}
                    angle={-15}
                    textAnchor="end"
                    height={60}
                  />
                  <YAxis allowDecimals={false} />
                  <RTooltip />
                  <Legend formatter={() => '候选数'} />
                  <Bar dataKey="eligible_count" fill="#00d4ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              候选最多的支柱 = 森林倾斜的方向。过窄分布 → 考虑刺激其他 pillar。
            </Text>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="森林按支柱分布（平均 Sharpe）" size="small">
            {pillarBars.length === 0 ? (
              <Empty description="森林池无候选" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={pillarBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="pillar"
                    interval={0}
                    angle={-15}
                    textAnchor="end"
                    height={60}
                  />
                  <YAxis />
                  <RTooltip formatter={(v) => Number(v).toFixed(3)} />
                  <Legend formatter={() => '平均 Sharpe'} />
                  <Bar dataKey="avg_sharpe" fill="#9c88ff" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              平均 Sharpe 最高的支柱 = 最有信号密度的 reference 来源。
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Pillar table */}
      <Card className="glass-card" title="按支柱明细" style={{ marginTop: 16 }} size="small">
        <Table
          size="small"
          rowKey="pillar"
          dataSource={pillarBars}
          columns={pillarColumns}
          pagination={false}
          locale={{ emptyText: '森林池无候选' }}
        />
      </Card>

      {/* Top entries table */}
      <Card
        className="glass-card"
        title={
          <Space>
            候选 hypothesis Top 10
            <Tooltip title="按 sharpe_avg DESC, pass_count DESC, updated_at DESC 排序 — 同 fetch_cross_task_promoted 注入 prompt 的顺序">
              <InfoCircleOutlined style={{ color: '#9c88ff' }} />
            </Tooltip>
          </Space>
        }
        style={{ marginTop: 16 }}
        size="small"
      >
        <Table
          size="small"
          rowKey="hypothesis_id"
          dataSource={data.top_entries || []}
          columns={entryColumns}
          pagination={false}
          locale={{ emptyText: '森林池无候选' }}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          被引用次数长尾分布 → 少数 PROMOTED 占大头是预期; 全 0 引用 = stamp 链路断了。
        </Text>
      </Card>
    </div>
  )
}
