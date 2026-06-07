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
 * G8ForestMonitor — /ops/g8-monitor (2026-05-19, 四池世界诚实降级改写 2026-06-07).
 *
 * G8 Phase A hypothesis-forest telemetry. Surfaces the cross-task PROMOTED
 * pool that fetch_cross_task_promoted injects into the LLM prompt, plus the
 * reverse attribution stamp (_g8_forest_referenced_ids on alphas.metrics).
 *
 * 四池世界下的失真说明(选项 B,诚实降级,不碰后端):
 *   - 池 node_hypothesis 跑 LEVEL-0 → 状态卡在 PROPOSED 不达晋升阈值
 *     → 合格假设池 eligible_count 恒为 0;
 *   - 池 persister 未向 _incremental_save_alphas 传 cross_task_hyps
 *     → alphas.metrics._g8_forest_referenced_ids 不被写入
 *     → 反向引用明细(被引用 alpha 数 / 引用 PASS 率)恒空;
 *   - flag ENABLE_HYPOTHESIS_FOREST_REUSE 仍 ON,boolean 复用信号可能仍出现;
 *   - 复活依赖 Phase 2 池认知对账 beat(假设生命周期晋升)。
 * 因此本页对恒空区块给「池模式预期为空」的中性空态文案,而非看起来像故障。
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

  // 池模式下的诚实降级 banner — 任何加载/错误/空态都先呈现,提醒读数失真
  const poolDegradeBanner = (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message="四池世界下假设森林部分失真(诚实降级)"
      description={
        <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
          <Text style={{ fontSize: 12 }}>
            池 <Text code>node_hypothesis</Text> 跑 LEVEL-0,假设状态停在 PROPOSED 不达晋升阈值
            → <Text strong>合格假设池(eligible_count)恒为 0</Text>。
          </Text>
          <Text style={{ fontSize: 12 }}>
            池 persister 未传 cross_task_hyps,反向引用明细
            <Text code>_g8_forest_referenced_ids</Text> 未被写入
            → <Text strong>被引用 alpha 数 / 引用 PASS 率恒空</Text>(非故障,是池模式预期)。
          </Text>
          <Text style={{ fontSize: 12 }}>
            复活依赖 <Text strong>Phase 2 池认知对账 beat</Text>(假设生命周期晋升)。flag{' '}
            <Text code>ENABLE_HYPOTHESIS_FOREST_REUSE</Text> 仍 ON,boolean 复用信号可能仍出现。
          </Text>
        </Space>
      }
    />
  )

  if (isLoading) {
    return (
      <div>
        {poolDegradeBanner}
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Spin size="large" />
        </div>
      </div>
    )
  }
  if (error) {
    return (
      <div>
        {poolDegradeBanner}
        <Alert
          type="error"
          showIcon
          message="加载 hypothesis forest 失败"
          description={error?.response?.data?.detail || error?.message || '未知错误'}
        />
      </div>
    )
  }
  if (!data) {
    return (
      <div>
        {poolDegradeBanner}
        <Empty description="无 hypothesis forest 数据" />
      </div>
    )
  }

  const flagOn = !!data.flags?.ENABLE_HYPOTHESIS_FOREST_REUSE
  const eligible = data.eligible_count ?? 0
  const totalRef = data.total_referenced_alphas ?? 0
  const passRef = data.reference_pass_count ?? 0
  const passRate = (data.reference_pass_rate ?? 0) * 100
  // 池模式下 eligible/totalRef 恒 0 是预期 → 用中性紫色,不当作告警红
  const passRateColor =
    totalRef === 0 ? '#9c88ff' : passRate >= 30 ? '#00ff88' : passRate >= 10 ? '#ffb700' : '#ff4d4f'
  const flagsList = Object.entries(data.flags || {})

  const pillarBars = (data.pillar_breakdown || []).map((p) => ({
    pillar: p.pillar,
    eligible_count: p.eligible_count,
    avg_sharpe: p.avg_sharpe,
    total_pass: p.total_pass,
  }))

  // 池模式下「合格池为空」是预期状态,空态文案统一走这个,避免看起来像故障
  const POOL_EMPTY_TEXT =
    '池模式下合格假设池预期为空(node_hypothesis LEVEL-0,状态未晋升)— 非故障'
  const POOL_REF_EMPTY_TEXT =
    '池 persister 未写 _g8_forest_referenced_ids,引用明细预期为空 — 非故障'

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
        <Tooltip title="窗口内 alphas.metrics._g8_forest_referenced_ids 包含此 hypothesis_id 的 PASS+FAIL 总条数(池模式下未被 persister 写入,预期 0)">
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
          假设森林监控（G8 Phase A · 四池降级）
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

      {/* 四池世界诚实降级 banner — 永远置顶 */}
      {poolDegradeBanner}

      {/* Flag / 当前读数态 banner（描述性，不再宣称健康/故障） */}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space wrap>
            <strong>当前读数</strong>
            {flagsList.map(([k, v]) => (
              <Tag key={k} color={v ? 'success' : 'default'}>
                {k}: {v ? '开' : '关'}
              </Tag>
            ))}
            <Text type="secondary">
              {region} · 近 {days} 天:合格池 {eligible} · 被引用 alpha {totalRef}
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              ENABLE_HYPOTHESIS_FOREST_REUSE 关闭中,cross-task PROMOTED hypothesis
              不会注入 LLM prompt。
            </Text>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              flag 已开,但池模式下假设状态停在 PROPOSED(LEVEL-0 不晋升),
              因此 {region} 合格池与反向引用明细预期为空。这是池世界的预期状态,
              不代表 stamp 链路或 mining 故障;待 Phase 2 池认知对账 beat 上线后,
              假设晋升 → eligible_count / 引用明细才会重新有值。
            </Text>
          )
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title={`pass_count ≥ 2 AND sharpe_avg ≥ 1.0 的 ACTIVE/PROMOTED hypothesis 数 — fetch_cross_task_promoted 的实际池子(池模式 LEVEL-0 不晋升 → 预期 0)`}>
              <Statistic
                title={
                  <Space>
                    候选池规模
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={eligible}
                prefix={<ApartmentOutlined />}
                valueStyle={{ color: eligible > 0 ? '#00d4ff' : '#9c88ff' }}
              />
            </Tooltip>
            {eligible === 0 && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                池模式预期为 0(未晋升)
              </Text>
            )}
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="窗口内 metrics 包含 _g8_forest_referenced_ids 的 alpha 总数(PASS+FAIL)— 池 persister 未写,预期 0">
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
            {totalRef === 0 && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                池 persister 未写引用,预期为 0
              </Text>
            )}
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="其中 PASS"
              value={passRef}
              valueStyle={{ color: passRef > 0 ? '#00ff88' : '#9c88ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              quality_status ∈ PASS / PASS_PROVISIONAL
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="reference_pass_rate — 森林影响产出的 alpha PASS 率(池模式下引用明细恒空 → 无意义)">
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
            {totalRef === 0 && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                无引用样本,比率无意义
              </Text>
            )}
          </Card>
        </Col>
      </Row>

      {/* Per-pillar bar charts */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="森林按支柱分布（候选数量）" size="small">
            {pillarBars.length === 0 ? (
              <Empty description={POOL_EMPTY_TEXT} />
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
              候选最多的支柱 = 森林倾斜的方向。池模式下合格池为空时此图预期空白。
            </Text>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="森林按支柱分布（平均 Sharpe）" size="small">
            {pillarBars.length === 0 ? (
              <Empty description={POOL_EMPTY_TEXT} />
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
          locale={{ emptyText: POOL_EMPTY_TEXT }}
        />
      </Card>

      {/* Top entries table */}
      <Card
        className="glass-card"
        title={
          <Space>
            候选 hypothesis Top 10
            <Tooltip title="按 sharpe_avg DESC, pass_count DESC, updated_at DESC 排序 — 同 fetch_cross_task_promoted 注入 prompt 的顺序(池模式下合格池为空)">
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
          locale={{ emptyText: POOL_EMPTY_TEXT }}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {POOL_REF_EMPTY_TEXT}。Phase 2 池认知对账 beat 上线、假设晋升后,
          被引用次数才会重新累积。
        </Text>
      </Card>
    </div>
  )
}
