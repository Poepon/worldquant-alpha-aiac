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
  CopyOutlined,
  InfoCircleOutlined,
  StopOutlined,
} from '@ant-design/icons'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * G3OriginalityMonitor — /ops/g3-monitor (2026-05-19).
 *
 * G3 Phase A AST originality gate telemetry. Operators use this page to
 * calibrate AST_ORIGINALITY_MIN_DISTANCE (τ) and decide when to promote
 * AST_ORIGINALITY_MODE shadow → soft → hard.
 *
 * Mirrors CoSTEERMonitor layout: top healthy-gate banner + flag tags + KPI
 * row + min_distance histogram BarChart + per-pillar block-rate BarChart +
 * top nearest-neighbor "magnet" table.
 *
 * Refetches every 30s via react-query.
 */
export default function G3OriginalityMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/g3/originality-stats', days],
    queryFn: () => api.getOpsG3OriginalityStats(days, 10, 10),
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
        message="加载 G3 originality stats 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 G3 originality stats 数据" />

  const flagOn = !!data.flags?.ENABLE_AST_ORIGINALITY_GATE
  const diversityFlagOn = !!data.flags?.ENABLE_AST_DIVERSITY_DIM
  const mode = data.mode || 'shadow'
  const threshold = data.threshold ?? 0.15
  const total = data.total_candidates ?? 0
  const blocked = data.blocked_candidates ?? 0
  const blockRate = data.block_rate ?? 0
  const blockPct = blockRate * 100
  // 健康指引: shadow 5-15% 比较健康 (能抓住磁铁但不过头). hard 后阈值另算.
  const blockRateColor =
    blockPct >= 5 && blockPct <= 15
      ? '#00ff88'
      : blockPct < 5
      ? '#ffb700'
      : '#ff4d4f'
  // Healthy gate (descriptive): flag ON + total > 0 + 在 5-15% 区间(shadow 校准 sweet spot)
  const healthy =
    flagOn && total > 0 && blockPct >= 5 && blockPct <= 15

  const histBars = (data.distance_histogram || []).map((b) => ({
    bucket: `[${b.lo.toFixed(2)}, ${b.hi.toFixed(2)})`,
    lo: b.lo,
    count: b.count,
    blocked: b.hi <= threshold,
  }))

  const pillarBars = (data.by_pillar || []).map((p) => ({
    pillar: p.pillar,
    blocked: p.blocked,
    total: p.total,
    block_pct: Math.round(p.block_rate * 10000) / 100,
  }))

  const neighborColumns = [
    {
      title: '最近邻 hash',
      dataIndex: 'nearest_neighbor_hash',
      key: 'nearest_neighbor_hash',
      render: (v) => (
        <Tooltip title={v}>
          <Text code style={{ fontSize: 12 }}>
            {v?.slice(0, 16)}…
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '被拦截次数',
      dataIndex: 'blocked_count',
      key: 'blocked_count',
      width: 130,
      align: 'right',
      render: (v) => (
        <Tag color="error">
          <StopOutlined /> {v}
        </Tag>
      ),
    },
  ]

  const pillarColumns = [
    {
      title: '支柱',
      dataIndex: 'pillar',
      key: 'pillar',
      width: 130,
      render: (v) => <Tag color={v === 'unknown' ? 'default' : 'cyan'}>{v}</Tag>,
    },
    {
      title: '拦截 / 总数',
      key: 'ratio',
      width: 130,
      align: 'right',
      render: (_, r) => `${r.blocked} / ${r.total}`,
    },
    {
      title: '拦截率',
      dataIndex: 'block_pct',
      key: 'block_pct',
      width: 100,
      align: 'right',
      render: (v) => `${v.toFixed(2)}%`,
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <CopyOutlined style={{ marginRight: 8 }} />
          AST 原创性监控（G3 Phase A）
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
            <Tag color={mode === 'hard' ? 'red' : mode === 'soft' ? 'orange' : 'blue'}>
              MODE: {mode}
            </Tag>
            <Tag color="purple">τ = {threshold.toFixed(3)}</Tag>
            <Tag color={flagOn ? 'success' : 'default'}>
              ENABLE_AST_ORIGINALITY_GATE: {flagOn ? '开' : '关'}
            </Tag>
            <Tag color={diversityFlagOn ? 'success' : 'default'}>
              ENABLE_AST_DIVERSITY_DIM: {diversityFlagOn ? '开' : '关'}
            </Tag>
            <Text type="secondary">
              健康门槛: flag ON + 拦截率落在 5-15% 区间（shadow 校准 sweet spot）
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              ENABLE_AST_ORIGINALITY_GATE 关闭中,候选时不会检查 AST 相似度。
              开启后(默认 mode=shadow)将记录 ast_distance_log 但不阻断,
              此页可看到 block_rate 是否需要升级到 soft / hard。
            </Text>
          ) : total === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              flag 已开,但窗口内 ast_distance_log 无数据。
              确认是否有候选在生成 / 是否需要 enable_ast_diversity_dim 提供基准向量。
            </Text>
          ) : blockPct > 15 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              拦截率 {blockPct.toFixed(2)}% 偏高 — τ = {threshold} 可能过严。
              先降 τ 再考虑升级 mode,否则 hard 模式会拒掉大量正常候选。
            </Text>
          ) : blockPct < 5 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              拦截率 {blockPct.toFixed(2)}% 偏低 — τ = {threshold} 可能过松。
              升 τ 才能抓住"换皮"候选,或者继续观察直到样本量充足。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="窗口内候选总数"
              value={total}
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              ast_distance_log 行数（非空 distance）
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="拦截候选数"
              value={blocked}
              prefix={<StopOutlined />}
              valueStyle={{ color: '#ff4d4f' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              ast_distance_min &lt; τ
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="拦截 / 总数。Shadow 校准 sweet spot 在 5-15% 区间">
              <Statistic
                title={
                  <Space>
                    拦截率
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={blockPct}
                precision={2}
                suffix="%"
                valueStyle={{ color: blockRateColor }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="当前阈值 τ"
              value={threshold}
              precision={3}
              valueStyle={{ color: '#ffb700' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              MODE: {mode}
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Distance histogram */}
      <Card
        className="glass-card"
        title={
          <Space>
            min_distance 直方图
            <Tooltip title={`τ 位置已用 ⚑ 标在 X 轴 (${threshold.toFixed(3)})。落在 τ 左侧的桶都会被拦截`}>
              <InfoCircleOutlined style={{ color: '#9c88ff' }} />
            </Tooltip>
          </Space>
        }
        style={{ marginTop: 16 }}
        size="small"
      >
        {histBars.length === 0 ? (
          <Empty description="窗口内暂无距离样本" />
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={histBars}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="bucket"
                interval={0}
                angle={-15}
                textAnchor="end"
                height={70}
              />
              <YAxis allowDecimals={false} />
              <RTooltip />
              <Legend
                formatter={() => `min_distance 分布 (τ = ${threshold.toFixed(3)})`}
              />
              <Bar dataKey="count" fill="#00d4ff">
                {histBars.map((b, i) => (
                  <Cell
                    key={`hist-${i}`}
                    fill={b.blocked ? '#ff4d4f' : '#00d4ff'}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          红色桶位于 τ 左侧,会被 hard 模式拦截。Operator 目标:τ 抓住底部 5-10% 的"换皮"候选。
        </Text>
      </Card>

      {/* Per-pillar block rate */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card className="glass-card" title="按支柱拦截率（post-gate 信号）" size="small">
            {pillarBars.length === 0 ? (
              <Empty description="alphas.metrics 暂无 _g3_verdict 标签" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={pillarBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="pillar" />
                  <YAxis
                    label={{ value: '%', angle: -90, position: 'insideLeft' }}
                  />
                  <RTooltip formatter={(v) => `${Number(v).toFixed(2)}%`} />
                  <Legend formatter={() => '拦截率'} />
                  <Bar dataKey="block_pct" fill="#13c2c2" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              拦截率最高的支柱 = 需要推动多样性的下一目标。
            </Text>
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card className="glass-card" title="按支柱明细" size="small">
            <Table
              size="small"
              rowKey="pillar"
              dataSource={pillarBars}
              columns={pillarColumns}
              pagination={false}
              locale={{ emptyText: '暂无支柱数据' }}
            />
          </Card>
        </Col>
      </Row>

      {/* Top nearest-neighbor magnets */}
      <Card
        className="glass-card"
        title={
          <Space>
            高频"换皮"磁铁 Top 10
            <Tooltip title="最近邻 hash 被拦截次数最多的历史 alpha — 这些是 AST-isomorphism 磁铁,新候选频繁与之相似">
              <InfoCircleOutlined style={{ color: '#9c88ff' }} />
            </Tooltip>
          </Space>
        }
        style={{ marginTop: 16 }}
        size="small"
      >
        <Table
          size="small"
          rowKey="nearest_neighbor_hash"
          dataSource={data.top_neighbors || []}
          columns={neighborColumns}
          pagination={false}
          locale={{ emptyText: '窗口内暂无被拦截候选' }}
        />
      </Card>
    </div>
  )
}
