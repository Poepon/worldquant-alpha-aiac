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

// 假设状态 label 映射（勿改 key，仅显示用）
const HYP_STATUS_LABEL = {
  PROMOTED: '已提升复用',
  ACTIVE: '生效中',
  PROPOSED: '已提出',
}

// 开关名 label 映射（勿改 key，仅显示用）
const FLAG_LABEL = {
  ENABLE_HYPOTHESIS_FOREST_REUSE: '假设库跨任务复用',
}

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

  // 流水线模式下的诚实降级提示 — 任何加载/错误/空态都先呈现,提醒读数失真
  const poolDegradeBanner = (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message="当前流水线模式下假设库部分数据失真（诚实降级说明）"
      description={
        <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
          <Text style={{ fontSize: 12 }}>
            假设节点在当前模式下不跨任务复用，假设状态停在"提出"阶段、不达提升阈值
            → <Text strong>合格假设数量恒为 0</Text>。
          </Text>
          <Text style={{ fontSize: 12 }}>
            流水线评估入库环节未回写假设引用关系
            → <Text strong>被引用 alpha 数 / 引用通过率恒空</Text>（非故障，是当前模式的预期）。
          </Text>
          <Text style={{ fontSize: 12 }}>
            恢复依赖 <Text strong>第二阶段「知识库对账」定时任务</Text>（假设生命周期提升）。
            假设库复用开关仍开启，复用信号可能仍出现。
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
          message="加载假设库数据失败"
          description={error?.response?.data?.detail || error?.message || '未知错误'}
        />
      </div>
    )
  }
  if (!data) {
    return (
      <div>
        {poolDegradeBanner}
        <Empty description="暂无假设库数据" />
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
    '当前模式下合格假设库预期为空（假设不跨任务复用、状态未提升）—— 非故障'
  const POOL_REF_EMPTY_TEXT =
    '当前流水线未回写假设引用关系，引用明细预期为空 —— 非故障'

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
      title: '假设陈述',
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
      title: '因子类别',
      dataIndex: 'pillar',
      key: 'pillar',
      width: 110,
      render: (v) =>
        v ? <Tag color="cyan">{v}</Tag> : <Tag color="default">（无）</Tag>,
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
          {v ? (HYP_STATUS_LABEL[v] || v) : '—'}
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
      title: '通过数',
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
        <Tooltip title="窗口内引用了此假设的 alpha 总数（含通过与未通过；当前流水线未回写引用关系，预期为 0）">
          <Tag color={v > 0 ? 'success' : 'default'} icon={<LinkOutlined />}>
            {v}
          </Tag>
        </Tooltip>
      ),
    },
  ]

  const pillarColumns = [
    {
      title: '因子类别',
      dataIndex: 'pillar',
      key: 'pillar',
      width: 140,
      render: (v) => (
        <Tag color={v === '(none)' ? 'default' : 'cyan'}>{v === '(none)' ? '（无）' : v}</Tag>
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
      title: '累计通过数',
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
          假设库分布监控（当前模式降级）
        </Title>
        <Space>
          <Text type="secondary">地区：</Text>
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
          <Text type="secondary">时间窗口：</Text>
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
                {FLAG_LABEL[k] || k}：{v ? '开' : '关'}
              </Tag>
            ))}
            <Text type="secondary">
              {region} · 近 {days} 天：合格假设 {eligible} 条 · 被引用 alpha {totalRef} 个
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              假设库跨任务复用开关关闭中，跨任务已提升的假设不会注入到模型提示词。
            </Text>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              开关已开，但当前模式下假设状态停在"已提出"（不跨任务提升），
              因此 {region} 的合格假设与引用明细预期为空。这是当前模式的预期状态，
              不代表标注链路或挖掘故障；待第二阶段「知识库对账」定时任务上线后，
              假设提升 → 合格假设数 / 引用明细才会重新有值。
            </Text>
          )
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title={`通过数 ≥ 2 且 平均 Sharpe ≥ 1.0 的「生效中/已提升」假设数量 —— 可供跨任务复用注入的实际假设池（当前模式不提升 → 预期 0）`}>
              <Statistic
                title={
                  <Space>
                    合格假设数
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
                当前模式预期为 0（未提升）
              </Text>
            )}
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="窗口内引用了假设的 alpha 总数（含通过与未通过）—— 当前流水线未回写引用，预期 0">
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
                当前流水线未回写引用，预期为 0
              </Text>
            )}
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="其中通过数"
              value={passRef}
              valueStyle={{ color: passRef > 0 ? '#00ff88' : '#9c88ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              质量状态为「通过」或「暂定通过」
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="引用了假设而产出的 alpha 通过率（当前模式下引用明细恒空 → 无意义）">
              <Statistic
                title={
                  <Space>
                    引用通过率
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
                无引用样本，比率无意义
              </Text>
            )}
          </Card>
        </Col>
      </Row>

      {/* Per-pillar bar charts */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="假设库按因子类别分布（候选数量）" size="small">
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
              候选最多的因子类别 = 假设库倾斜的方向。当前模式下合格假设为空时此图预期空白。
            </Text>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card className="glass-card" title="假设库按因子类别分布（平均 Sharpe）" size="small">
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
              平均 Sharpe 最高的因子类别 = 信号密度最高的参考来源。
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Pillar table */}
      <Card className="glass-card" title="各因子类别明细" style={{ marginTop: 16 }} size="small">
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
            候选假设 Top 10
            <Tooltip title="按平均 Sharpe、通过数、更新时间倒序排列 —— 与跨任务复用注入提示词的顺序一致（当前模式下合格假设为空）">
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
          {POOL_REF_EMPTY_TEXT}。第二阶段「知识库对账」定时任务上线、假设提升后，
          被引用次数才会重新累积。
        </Text>
      </Card>
    </div>
  )
}
