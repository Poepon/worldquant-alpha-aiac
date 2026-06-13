import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Row,
  Segmented,
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
  RobotOutlined,
  ReloadOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * AutoSubmitMonitor — /ops/auto-submit (2026-06-04).
 *
 * The human-review surface for the auto-submit SHADOW observation. The beat
 * (every 6h :35) runs the fail-closed guard stack and records would_submit /
 * skipped rows to auto_submit_audit WITHOUT submitting (shadow). This page lets
 * the operator eyeball the would-submit list for N days before flipping
 * AUTO_SUBMIT_MODE='live', and shows BOTH faces per candidate: portfolio
 * marginal value (Δsharpe/margin/composite) AND the competition Δscore (which
 * the policy does NOT gate on — informational, per the portfolio-value goal).
 *
 * Refetches every 30s.
 */
const OUTCOME_META = {
  would_submit: { color: 'success', label: '将提交' },
  submitted: { color: 'processing', label: '已提交' },
  rejected: { color: 'warning', label: 'BRAIN 拒绝' },
  skipped: { color: 'default', label: '已跳过' },
  error: { color: 'error', label: '错误' },
}

function outcomeTag(v) {
  const m = OUTCOME_META[v] || { color: 'default', label: v }
  return <Tag color={m.color}>{m.label}</Tag>
}

// Competition before-and-after Δscore — informational only (NOT a gate). The
// policy optimizes portfolio marginal value per the user's goal; Δscore<0 means
// submitting this alpha lowers your IQC competition score (the submitted pool IS
// the competition portfolio) — surfaced so the trade-off is conscious.
function deltaScoreTag(v) {
  if (v === null || v === undefined) return <Tag>—</Tag>
  const positive = v >= 0
  return (
    <Tooltip title="竞赛评分变化(只展示、不作为提交门槛)。小于 0 = 提交这个 alpha 会拉低你的竞赛分(已提交的策略集合就是竞赛组合);你已选择「组合价值优先」,所以这一项不参与提交决策。">
      <Tag color={positive ? 'green' : 'volcano'}>
        {positive ? '+' : ''}{v.toFixed(0)}
      </Tag>
    </Tooltip>
  )
}

function valueTierTag(t) {
  if (t === null || t === undefined) return <Text type="secondary">—</Text>
  const meta = {
    0: { c: 'green', label: '增益' },
    1: { c: 'gold', label: '中性' },
    2: { c: 'red', label: '稀释' },
    3: { c: 'default', label: '无收益数据' },
  }[t] || { c: 'default', label: String(t) }
  return <Tag color={meta.c}>{meta.label}</Tag>
}

function selfCorrTag(v) {
  if (v === null || v === undefined) return <Tag color="default">未算</Tag>
  if (v >= 0.7) return <Tag color="error">{v.toFixed(3)}</Tag>
  if (v >= 0.5) return <Tag color="gold">{v.toFixed(3)}</Tag>
  return <Tag color="success">{v.toFixed(3)}</Tag>
}

const OUTCOME_OPTIONS = [
  { value: 'would_submit', label: '将提交' },
  { value: 'submitted', label: '已提交' },
  { value: 'rejected', label: 'BRAIN 拒绝' },
  { value: 'skipped', label: '已跳过' },
  { value: 'all', label: '全部' },
]

export default function AutoSubmitMonitor() {
  const qc = useQueryClient()
  const [outcome, setOutcome] = useState('would_submit')
  const [region, setRegion] = useState(null)

  // would_submit / skipped are per-run snapshots (each 6h beat re-writes a row
  // per candidate) → pin to the latest beat firing so the same alpha isn't shown
  // once per historical run. submitted/rejected are real events → show history.
  const snapshotView = outcome === 'would_submit' || outcome === 'skipped'

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/auto-submit/audit', outcome, region],
    queryFn: () =>
      api.getOpsAutoSubmitAudit({
        outcome: outcome === 'all' ? null : outcome,
        region,
        limit: 200,
        latestOnly: snapshotView,
      }),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  const items = data?.items || []

  // skip_reason distribution over the loaded set (useful when viewing skipped).
  const skipDist = useMemo(() => {
    const m = {}
    for (const it of items) {
      if (it.outcome === 'skipped' && it.skip_reason) {
        m[it.skip_reason] = (m[it.skip_reason] || 0) + 1
      }
    }
    return Object.entries(m).sort((a, b) => b[1] - a[1])
  }, [items])

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
        message="加载自动提交审计失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }

  const tally = data?.tally_24h || {}
  const snap = data?.snapshot_tally || {}   // latest beat firing's outcome breakdown
  const enabled = data?.enabled
  const mode = data?.mode || 'shadow'

  const sig = (r) => r?.gate_results?.signals || {}

  const columns = [
    {
      title: 'Alpha',
      dataIndex: 'alpha_pk',
      key: 'alpha_pk',
      width: 150,
      render: (pk, r) => (
        <Space direction="vertical" size={0}>
          <Link to={`/alphas/${pk}`}>
            <Text code style={{ fontSize: 12 }}>{r.alpha_brain_id || pk}</Text>
          </Link>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {r.region || '—'}{r.mode ? ` · ${r.mode}` : ''}
          </Text>
        </Space>
      ),
    },
    { title: '结果', dataIndex: 'outcome', key: 'outcome', width: 90, render: outcomeTag },
    {
      title: 'Sharpe',
      key: 'sharpe',
      width: 80,
      align: 'right',
      render: (_, r) => { const v = sig(r).sharpe; return v != null ? v.toFixed(2) : '—' },
    },
    {
      title: 'Fitness',
      key: 'fitness',
      width: 80,
      align: 'right',
      render: (_, r) => { const v = sig(r).fitness; return v != null ? v.toFixed(2) : '—' },
    },
    {
      title: (
        <Tooltip title="alpha 自身的 Margin(每单位交易利润,单位 bps)— 要 ≥5bps 才能覆盖交易成本盈利(经济门槛)">
          <Space size={4}>Margin <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      key: 'margin_bps',
      width: 100,
      align: 'right',
      render: (_, r) => {
        const v = sig(r).margin_bps
        return v != null ? <Text type={v < 5 ? 'warning' : undefined}>{v.toFixed(1)} bps</Text> : '—'
      },
    },
    {
      title: (
        <Tooltip title="与已提交策略的相关度 — ≥0.7 BRAIN 会硬性拒绝">
          <Space size={4}>相关度 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      key: 'self_corr',
      width: 100,
      render: (_, r) => selfCorrTag(sig(r).self_corr),
    },
    {
      title: (
        <Tooltip title="边际贡献综合评分(大于 0 才推荐)— 提交决策按这个来(以组合价值为目标)">
          <Space size={4}>综合 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      key: 'composite',
      width: 90,
      align: 'right',
      render: (_, r) => { const v = sig(r).composite; return v != null ? v.toFixed(3) : '—' },
    },
    {
      title: '方向层',
      key: 'value_tier',
      width: 84,
      render: (_, r) => valueTierTag(sig(r).value_tier),
    },
    {
      title: (
        <Tooltip title="竞赛评分变化(只展示、不作为提交门槛)。小于 0 = 提交会拉低竞赛分;你已选择组合价值优先。">
          <Space size={4}>竞赛评分变化 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      key: 'delta_score',
      width: 110,
      align: 'right',
      render: (_, r) => deltaScoreTag(sig(r).delta_score),
    },
    {
      title: '跳过原因',
      dataIndex: 'skip_reason',
      key: 'skip_reason',
      width: 150,
      render: (v) => (v ? <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text> : '—'),
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
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <Title level={3} style={{ margin: 0 }}>
          <RobotOutlined style={{ marginRight: 8 }} />
          自动提交监控
        </Title>
        <Space wrap>
          <Tag color={enabled ? 'green' : 'default'}>{enabled ? '已启用' : '未启用'}</Tag>
          <Tag color={mode === 'live' ? 'red' : mode === 'shadow' ? 'blue' : 'default'}>
            模式: {mode === 'live' ? '正式提交' : mode === 'shadow' ? '影子观察' : mode}
          </Tag>
          <Text type="secondary">地区:</Text>
          <Select
            value={region}
            onChange={setRegion}
            style={{ width: 110 }}
            allowClear
            placeholder="全部"
            options={[
              { value: 'USA', label: 'USA' },
              { value: 'CHN', label: 'CHN' },
              { value: 'EUR', label: 'EUR' },
              { value: 'HKG', label: 'HKG' },
              { value: 'JPN', label: 'JPN' },
            ]}
          />
          <Button
            icon={<ReloadOutlined />}
            onClick={() => qc.invalidateQueries({ queryKey: ['ops/auto-submit/audit'] })}
          >
            刷新
          </Button>
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      <Alert
        type={mode === 'live' ? 'warning' : 'info'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          mode === 'shadow'
            ? '影子观察模式:跑完整的失败即拦截守门流程并记录「将提交」名单,但绝不真的提交。观察名单干净后再切到正式提交。'
            : mode === 'live'
              ? '正式提交模式:符合守门流程的候选会按每日上限真的提交到 BRAIN(不可逆)。'
              : '自动提交未启用。'
        }
        description={
          <Text style={{ fontSize: 12 }}>
            提交决策按<strong>组合边际价值</strong>(Sharpe 增量 / 综合评分 / 是否带来增益);
            <strong>竞赛评分变化只展示、不作为门槛</strong> —— 提交即进入竞赛组合,评分变化小于 0 会拉低竞赛分,
            按你「组合价值优先」的选择不参与决策,但每行都列出以便你随时知道竞赛分的代价。
          </Text>
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="将提交 (本次定时任务)" value={snap.would_submit ?? 0} valueStyle={{ color: '#00ff88' }} />
            <Text type="secondary" style={{ fontSize: 11 }}>最近一次评估的快照</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="已跳过 (本次定时任务)" value={snap.skipped ?? 0} valueStyle={{ color: '#888' }} />
            <Text type="secondary" style={{ fontSize: 11 }}>被守门流程挡下</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="已提交 (24h)" value={tally.submitted ?? 0} valueStyle={{ color: '#00d4ff' }} />
            <Text type="secondary" style={{ fontSize: 11 }}>真实提交事件(累计)</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="BRAIN 拒绝 (24h)" value={tally.rejected ?? 0} valueStyle={{ color: '#ffb700' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="错误 (24h)" value={tally.error ?? 0} valueStyle={{ color: '#ff4d4f' }} />
          </Card>
        </Col>
      </Row>

      <Space style={{ marginBottom: 12 }} wrap>
        <Text type="secondary">查看:</Text>
        <Segmented value={outcome} onChange={setOutcome} options={OUTCOME_OPTIONS} />
        {snapshotView && (
          <Tooltip title="只显示最近一次定时任务的快照(每 6 小时一次定时任务会给每个候选重写一行,否则同一 alpha 会按历史执行批次重复出现)。看「全部」查跨批次历史。">
            <Tag color="blue">最近一次定时任务快照</Tag>
          </Tooltip>
        )}
        {outcome === 'skipped' && skipDist.length > 0 && (
          <Space wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>各门挡下:</Text>
            {skipDist.map(([reason, n]) => (
              <Tag key={reason}>{reason}: {n}</Tag>
            ))}
          </Space>
        )}
      </Space>

      <Card className="glass-card" size="small">
        <Table
          size="small"
          rowKey="id"
          dataSource={items}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{
            emptyText:
              outcome === 'would_submit'
                ? '当前无「将提交」候选 — 定时任务尚未跑、或全部被守门流程挡下(看「已跳过」+各门分布)'
                : '无记录',
          }}
        />
      </Card>
    </div>
  )
}
