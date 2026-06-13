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

// 拦截模式 label 映射（勿改 key，仅显示用）
const MODE_LABEL = {
  shadow: '影子（只记录不拦截）',
  soft: '软拦截（提示）',
  hard: '硬拦截（直接拒绝）',
}

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
        message="加载代码结构去重统计失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="暂无代码结构去重统计数据" />

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
      title: '最相似历史 alpha 指纹',
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
      title: '因子类别',
      dataIndex: 'pillar',
      key: 'pillar',
      width: 130,
      render: (v) => <Tag color={v === 'unknown' ? 'default' : 'cyan'}>{v === 'unknown' ? '未知' : v}</Tag>,
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
          代码结构去重监控
        </Title>
        <Space>
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

      {/* Health banner */}
      <Alert
        type={healthy ? 'success' : 'warning'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space wrap>
            <strong>健康状态：{healthy ? '健康' : '需关注'}</strong>
            <Tag color={mode === 'hard' ? 'red' : mode === 'soft' ? 'orange' : 'blue'}>
              模式：{MODE_LABEL[mode] || mode}
            </Tag>
            <Tag color="purple">阈值 τ = {threshold.toFixed(3)}</Tag>
            <Tag color={flagOn ? 'success' : 'default'}>
              去重开关：{flagOn ? '开' : '关'}
            </Tag>
            <Tag color={diversityFlagOn ? 'success' : 'default'}>
              多样性维度：{diversityFlagOn ? '开' : '关'}
            </Tag>
            <Text type="secondary">
              健康门槛：开关开启 + 拦截率落在 5-15% 区间（影子模式校准的最佳区间）
            </Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              代码结构去重开关关闭中，生成候选时不会检查代码结构相似度。
              开启后（默认为影子模式）只记录相似度但不拦截，
              此页可看到拦截率是否需要升级到软拦截 / 硬拦截。
            </Text>
          ) : total === 0 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              开关已开，但窗口内没有相似度记录数据。
              确认是否有候选在生成 / 是否需要开启多样性维度提供基准向量。
            </Text>
          ) : blockPct > 15 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              拦截率 {blockPct.toFixed(2)}% 偏高 —— 阈值 τ = {threshold} 可能过严。
              先降低 τ 再考虑升级拦截模式，否则硬拦截会拒掉大量正常候选。
            </Text>
          ) : blockPct < 5 ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              拦截率 {blockPct.toFixed(2)}% 偏低 —— 阈值 τ = {threshold} 可能过松。
              提高 τ 才能抓住"换皮重复"候选，或者继续观察直到样本量充足。
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
              已计算结构相似度的候选条数
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
              结构相似度过高（最近距离 &lt; 阈值 τ）
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="拦截数 / 总数。影子模式校准的最佳区间在 5-15%">
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
              模式：{MODE_LABEL[mode] || mode}
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Distance histogram */}
      <Card
        className="glass-card"
        title={
          <Space>
            结构相似度（最近距离）分布直方图
            <Tooltip title={`阈值 τ 位置已用 ⚑ 标在 X 轴（${threshold.toFixed(3)}）。落在 τ 左侧的区间都会被拦截`}>
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
                formatter={() => `结构相似度分布（阈值 τ = ${threshold.toFixed(3)}）`}
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
          红色区间位于阈值 τ 左侧，会被硬拦截模式拦截。调参目标：让 τ 抓住底部 5-10% 的"换皮重复"候选。
        </Text>
      </Card>

      {/* Per-pillar block rate */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card className="glass-card" title="各因子类别拦截率" size="small">
            {pillarBars.length === 0 ? (
              <Empty description="暂无去重结论标注数据" />
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
              拦截率最高的因子类别 = 需要推动多样性的下一目标。
            </Text>
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card className="glass-card" title="各因子类别明细" size="small">
            <Table
              size="small"
              rowKey="pillar"
              dataSource={pillarBars}
              columns={pillarColumns}
              pagination={false}
              locale={{ emptyText: '暂无因子类别数据' }}
            />
          </Card>
        </Col>
      </Row>

      {/* Top nearest-neighbor magnets */}
      <Card
        className="glass-card"
        title={
          <Space>
            高频"换皮重复"来源 Top 10
            <Tooltip title="被拦截次数最多的历史 alpha —— 新候选频繁与这些 alpha 的代码结构相似">
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
          locale={{ emptyText: '窗口内暂无被拦截的候选' }}
        />
      </Card>
    </div>
  )
}
