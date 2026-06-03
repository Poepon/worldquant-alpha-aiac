import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  SendOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  InfoCircleOutlined,
  OrderedListOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * SubmitBacklogMonitor — /ops/submit-backlog (2026-05-28).
 *
 * The #1 strategic lever is draining the can_submit backlog (submittable but
 * unsubmitted alphas). This page surfaces that queue ranked by the persisted
 * IQC marginal verdict (alpha.metrics._iqc_marginal.recommendation), lets the
 * operator kick a one-pass re-audit (POST /ops/submit-backlog/scan — BRAIN
 * backed, worker-async), and batch-submit the SUBMIT-graded ones via the
 * existing /alphas/{id}/submit endpoint.
 *
 * Refetches every 30s so a running re-audit's verdicts appear as they land.
 */
const VERDICT_META = {
  SUBMIT: { color: 'success', label: '建议提交' },
  NEUTRAL: { color: 'gold', label: '中性' },
  SKIP: { color: 'error', label: '不建议' },
  UNKNOWN: { color: 'default', label: '数据不足' },
}

function verdictTag(verdict, pending, selfCorr) {
  if (pending || !verdict) return <Tag color="default">待扫描</Tag>
  const m = VERDICT_META[verdict] || { color: 'default', label: verdict }
  // Self-corr breach overrides the verdict visually — submission will be
  // rejected at BRAIN regardless of the marginal scorecard. The verdict
  // (quality gate) stays informative under tooltip: a SUBMIT-but-breach
  // means "the factor idea is good, but the implementation collides with
  // an already-submitted alpha — rework to de-correlate, don't discard".
  if (selfCorr !== null && selfCorr !== undefined && selfCorr >= 0.7) {
    return (
      <Tooltip
        title={
          <span>
            边际推荐本应 <strong>{m.label}</strong>(质量门),但 self-corr ≥ 0.7
            已撞门(资格门)→ 实际无法提交。<br />
            意味"想法对但实现重复" — 可重做去相关,而非删除。
          </span>
        }
      >
        <Tag color="default" style={{ opacity: 0.5, textDecoration: 'line-through' }}>
          {m.label}
        </Tag>
      </Tooltip>
    )
  }
  return <Tag color={m.color}>{m.label}</Tag>
}

// Self-correlation gate: ≥0.7 vs already-submitted is BRAIN's hard reject.
function selfCorrTag(v) {
  if (v === null || v === undefined) return <Tag color="default">未算</Tag>
  if (v >= 0.7) return <Tag color="error">撞门 {v.toFixed(3)}</Tag>
  if (v >= 0.5) return <Tag color="gold">近门槛 {v.toFixed(3)}</Tag>
  return <Tag color="success">{v.toFixed(3)}</Tag>
}

// Max-corr to the already-selected ∪ submitted set (lower = more breadth added).
function maxCorrTag(v) {
  if (v === null || v === undefined) return <Tag>—</Tag>
  const color = v < 0.3 ? 'success' : v < 0.5 ? 'gold' : v < 0.7 ? 'orange' : 'error'
  return <Tag color={color}>{v.toFixed(3)}</Tag>
}

// Self-corr 状态分桶:与 KPI 卡(撞门/近门槛/安全/未算)同口径,客户端过滤复用。
const SELF_CORR_BUCKETS = {
  breach: { label: '撞门(≥0.7)', test: (v) => v !== null && v !== undefined && v >= 0.7 },
  near: { label: '近门槛(0.5-0.7)', test: (v) => v !== null && v !== undefined && v >= 0.5 && v < 0.7 },
  safe: { label: '安全(<0.5)', test: (v) => v !== null && v !== undefined && v < 0.5 },
  unknown: { label: '未算', test: (v) => v === null || v === undefined },
}

// verdict 桶包含 pending(待扫描)— pending 行没 verdict,单独成档。
const VERDICT_BUCKETS = ['SUBMIT', 'NEUTRAL', 'SKIP', 'UNKNOWN', 'PENDING']
const VERDICT_BUCKET_LABEL = {
  SUBMIT: '建议提交',
  NEUTRAL: '中性',
  SKIP: '不建议',
  UNKNOWN: '数据不足',
  PENDING: '待扫描',
}

export default function SubmitBacklogMonitor() {
  const qc = useQueryClient()
  const [region, setRegion] = useState(null)
  const [selectedRowKeys, setSelectedRowKeys] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [verdictFilter, setVerdictFilter] = useState([])
  const [selfCorrFilter, setSelfCorrFilter] = useState([])
  const [universeFilter, setUniverseFilter] = useState([])
  // P0-1 (2026-06-03): set-level orthogonal drain-order panel.
  const [showDrain, setShowDrain] = useState(false)
  const [drainSelectedKeys, setDrainSelectedKeys] = useState([])

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/submit-backlog', region],
    queryFn: () => api.getOpsSubmitBacklog(region),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  // Orthogonal drain order — lazy (only when the panel is opened). Greedy
  // breadth-maximising submit sequence; zero BRAIN cost (local PnL + stored
  // self_corr). See GET /ops/submit-backlog/drain-order.
  const { data: drainData, isFetching: drainFetching } = useQuery({
    queryKey: ['ops/submit-backlog/drain-order', region],
    queryFn: () => api.getOpsSubmitBacklogDrainOrder({ region }),
    enabled: showDrain,
    staleTime: 30_000,
  })

  const scanMutation = useMutation({
    mutationFn: () => api.scanSubmitBacklog(200),
    onSuccess: (res) => {
      message.success(res?.message || `已入队 ${res?.enqueued ?? 0} 个边际审计任务`)
      qc.invalidateQueries({ queryKey: ['ops/submit-backlog'] })
    },
    onError: (e) =>
      message.error(e?.response?.data?.detail || e?.message || '扫描触发失败'),
  })

  // Hooks must run unconditionally — derive items + memos BEFORE any early
  // return below, otherwise loading→loaded re-render changes hook count and
  // trips Rules of Hooks.
  const items = data?.items || []

  const universeOptions = useMemo(() => {
    const seen = new Set()
    for (const it of items) {
      if (it.universe) seen.add(it.universe)
    }
    return Array.from(seen).sort().map((u) => ({ value: u, label: u }))
  }, [items])

  const filteredItems = useMemo(() => {
    return items.filter((it) => {
      if (verdictFilter.length > 0) {
        const bucket = it.pending ? 'PENDING' : (it.verdict || 'UNKNOWN')
        if (!verdictFilter.includes(bucket)) return false
      }
      if (selfCorrFilter.length > 0) {
        const matched = selfCorrFilter.some((key) =>
          SELF_CORR_BUCKETS[key]?.test(it.self_corr),
        )
        if (!matched) return false
      }
      if (universeFilter.length > 0) {
        if (!it.universe || !universeFilter.includes(it.universe)) return false
      }
      return true
    })
  }, [items, verdictFilter, selfCorrFilter, universeFilter])

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
        message="加载提交积压失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }

  const summary = data?.summary || {}
  const total = summary.total ?? 0
  const audited = summary.audited ?? 0
  const pending = summary.pending ?? 0
  const progressPct = total > 0 ? Math.round((audited / total) * 100) : 0

  const anyFilterActive =
    verdictFilter.length > 0 || selfCorrFilter.length > 0 || universeFilter.length > 0

  // 切筛选时清空选中(避免对已隐藏行的 stale selection 误提交)。
  const resetFilter = (next) => {
    next()
    setSelectedRowKeys([])
  }

  // Submit a list of alpha PKs IN ORDER (one at a time, BRAIN-irreversible).
  // Shared by the verdict-table batch submit and the orthogonal drain panel.
  const submitPks = async (pks) => {
    if (!pks || pks.length === 0) return
    setSubmitting(true)
    let ok = 0
    let fail = 0
    const reasons = []
    for (const pk of pks) {
      try {
        const res = await api.submitAlpha(pk)
        if (res?.submitted) {
          ok += 1
        } else {
          fail += 1
          reasons.push(`#${pk}: ${res?.reason || '被拒'}`)
        }
      } catch (e) {
        fail += 1
        reasons.push(`#${pk}: ${e?.response?.data?.detail || e?.message || '错误'}`)
      }
    }
    setSubmitting(false)
    if (ok > 0) message.success(`成功提交 ${ok} 个`)
    if (fail > 0) {
      message.warning(`${fail} 个未提交：${reasons.slice(0, 3).join('；')}${reasons.length > 3 ? ' …' : ''}`)
    }
    qc.invalidateQueries({ queryKey: ['ops/submit-backlog'] })
    qc.invalidateQueries({ queryKey: ['ops/submit-backlog/drain-order'] })
  }

  // Batch submit — only allow selecting non-submitted rows; recommend SUBMIT.
  const onBatchSubmit = async () => {
    const picked = items
      .filter((it) => selectedRowKeys.includes(it.alpha_pk))
      .map((it) => it.alpha_pk)
    await submitPks(picked)
    setSelectedRowKeys([])
  }

  const pickedSubmitCount = items.filter(
    (it) => selectedRowKeys.includes(it.alpha_pk) && it.verdict === 'SUBMIT',
  ).length

  const columns = [
    {
      title: 'Alpha',
      dataIndex: 'alpha_pk',
      key: 'alpha_pk',
      width: 150,
      render: (pk, r) => (
        <Space direction="vertical" size={0}>
          <Link to={`/alphas/${pk}`}>
            <Text code style={{ fontSize: 12 }}>{r.brain_id || pk}</Text>
          </Link>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {r.region || '—'}{r.universe ? ` · ${r.universe}` : ''}
          </Text>
        </Space>
      ),
    },
    {
      title: '推荐',
      dataIndex: 'verdict',
      key: 'verdict',
      width: 100,
      render: (v, r) => verdictTag(v, r.pending, r.self_corr),
    },
    {
      title: (
        <Tooltip title="Self-correlation vs 已提交集(BRAIN 硬门 < 0.7)。BRAIN 端 SELF_CORRELATION 经常 PENDING 不出值,can_submit 仍 true,但本地算的撞门会让真实 submit 被 BRAIN 拒。">
          <Space size={4}>Self-corr <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'self_corr',
      key: 'self_corr',
      width: 130,
      render: (v, r) => (
        <Space direction="vertical" size={0}>
          {selfCorrTag(v)}
          {r.self_corr_counterpart ? (
            <Text type="secondary" style={{ fontSize: 11 }}>
              撞: <Text code style={{ fontSize: 11 }}>{r.self_corr_counterpart}</Text>
            </Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: (
        <Tooltip title="边际综合评分（marginal composite_score）— 同推荐档内排序依据">
          <Space size={4}>综合评分 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'composite',
      key: 'composite',
      width: 110,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(3) : '—'),
    },
    {
      title: (
        <Tooltip title="alpha 自身 margin（bps）— ≥5bps 才扣成本盈利，是经济门">
          <Space size={4}>Margin <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'margin_bps',
      key: 'margin_bps',
      width: 100,
      align: 'right',
      render: (v) =>
        v !== null && v !== undefined ? (
          <Text type={v < 5 ? 'warning' : undefined}>{v.toFixed(1)} bps</Text>
        ) : '—',
    },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe',
      key: 'sharpe',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: 'Fitness',
      dataIndex: 'fitness',
      key: 'fitness',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: '换手',
      dataIndex: 'turnover',
      key: 'turnover',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? `${(v * 100).toFixed(1)}%` : '—'),
    },
    {
      title: '审计时间 (UTC)',
      dataIndex: 'audited_at',
      key: 'audited_at',
      ellipsis: true,
      render: (v) => (v ? String(v).replace('T', ' ').slice(0, 19) : <Text type="secondary">未审计</Text>),
    },
  ]

  // Drain-order tables (orthogonal submit sequence + correlation-blocked tail).
  const drainAlphaCol = {
    title: 'Alpha',
    dataIndex: 'alpha_pk',
    key: 'alpha_pk',
    width: 150,
    render: (pk, r) => (
      <Space direction="vertical" size={0}>
        <Link to={`/alphas/${pk}`}>
          <Text code style={{ fontSize: 12 }}>{r.brain_id || pk}</Text>
        </Link>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {r.region || '—'}{r.pnl_covered ? '' : ' · 无PnL'}
        </Text>
      </Space>
    ),
  }
  const drainMetricCols = [
    {
      title: (
        <Tooltip title="与「已选 ∪ 已提交」集的最大相关性 — 越低这次提交加的独立广度越多">
          <Space size={4}>Δ广度(max-corr) <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'max_corr_to_selected',
      key: 'max_corr_to_selected',
      width: 130,
      align: 'right',
      render: (v) => maxCorrTag(v),
    },
    {
      title: 'self-corr',
      dataIndex: 'self_corr',
      key: 'self_corr',
      width: 110,
      render: (v) => selfCorrTag(v),
    },
    {
      title: 'Margin',
      dataIndex: 'margin_bps',
      key: 'margin_bps',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? <Text type={v < 5 ? 'warning' : undefined}>{v.toFixed(1)} bps</Text> : '—'),
    },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe',
      key: 'sharpe',
      width: 80,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: '推荐',
      dataIndex: 'verdict',
      key: 'verdict',
      width: 90,
      render: (v) => verdictTag(v, false, null),
    },
  ]
  const drainColumns = [
    {
      title: '序',
      dataIndex: 'rank',
      key: 'rank',
      width: 56,
      render: (v) => <Tag color="blue">{v}</Tag>,
    },
    drainAlphaCol,
    ...drainMetricCols,
  ]
  const drainBlockedColumns = [drainAlphaCol, ...drainMetricCols]

  const rowSelection = {
    selectedRowKeys,
    onChange: setSelectedRowKeys,
    // Disable selection for pending-rescan rows AND self-corr breach rows —
    // the latter will be rejected by BRAIN at submit time regardless of verdict.
    getCheckboxProps: (r) => ({
      disabled: r.pending || (r.self_corr !== null && r.self_corr !== undefined && r.self_corr >= 0.7),
    }),
  }

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <Title level={3} style={{ margin: 0 }}>
          <SendOutlined style={{ marginRight: 8 }} />
          提交积压抽干
        </Title>
        <Space wrap>
          <Text type="secondary">地区:</Text>
          <Select
            value={region}
            onChange={(v) => { setRegion(v); setSelectedRowKeys([]) }}
            style={{ width: 120 }}
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
          <Popconfirm
            title="全量重新审计积压"
            description={`将对待扫描/陈旧的 alpha 逐个调 BRAIN 重算边际推荐（每个约 5-20s，消耗 BRAIN 配额）。worker 后台异步执行，确认触发？`}
            okText="确认扫描"
            cancelText="取消"
            onConfirm={() => scanMutation.mutate()}
          >
            <Button
              icon={<ThunderboltOutlined />}
              loading={scanMutation.isPending}
              type={pending > 0 ? 'primary' : 'default'}
            >
              扫描全部{pending > 0 ? `（${pending} 待扫描）` : ''}
            </Button>
          </Popconfirm>
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      {/* Progress + scan hint */}
      <Alert
        type={pending > 0 ? 'warning' : 'success'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space wrap align="center">
            <span>边际审计覆盖进度</span>
            <Progress
              percent={progressPct}
              size="small"
              style={{ width: 200 }}
              status={pending > 0 ? 'active' : 'success'}
            />
            <Text type="secondary">
              已审计 {audited} / {total}
              {pending > 0 ? `，${pending} 个待扫描（陈旧 schema 或未审计）` : '，全部已带推荐'}
            </Text>
          </Space>
        }
        description={
          pending > 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              「待扫描」是仍带旧赛季(IQC2026S1)审计或从未审计的 alpha — 点「扫描全部」用当前
              scope(IQC2026S2)+ 边际打分卡刷新出 SUBMIT/NEUTRAL/SKIP 推荐。周期 beat 也会逐批补,
              手动扫描更快。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="积压总数" value={total} valueStyle={{ color: '#00d4ff' }} />
            <Text type="secondary" style={{ fontSize: 12 }}>can_submit 且未提交</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="建议提交" value={summary.submit ?? 0} valueStyle={{ color: '#00ff88' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="中性" value={summary.neutral ?? 0} valueStyle={{ color: '#ffb700' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="不建议" value={summary.skip ?? 0} valueStyle={{ color: '#ff4d4f' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="待扫描" value={pending} valueStyle={{ color: '#9c88ff' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="数据不足" value={summary.unknown ?? 0} valueStyle={{ color: '#888' }} />
          </Card>
        </Col>
      </Row>

      {/* Self-correlation gate row — BRAIN's hard reject ≥0.7. */}
      <Row gutter={[16, 16]} style={{ marginTop: 12 }}>
        <Col xs={12} sm={6} lg={6}>
          <Card className="glass-card">
            <Tooltip title="本地算 self-corr ≥ 0.7 vs 已提交集 → BRAIN 提交时硬门拒。这些不应被选去批量提交,checkbox 已禁用。">
              <Statistic
                title={
                  <Space>
                    Self-corr 撞门
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={summary.self_corr_breach ?? 0}
                valueStyle={{ color: '#ff4d4f' }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>≥ 0.7,提交会被拒</Text>
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={6}>
          <Card className="glass-card">
            <Statistic
              title="近门槛"
              value={summary.self_corr_near ?? 0}
              valueStyle={{ color: '#ffb700' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>0.5 - 0.7,谨慎</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={6}>
          <Card className="glass-card">
            <Statistic
              title="安全"
              value={summary.self_corr_safe ?? 0}
              valueStyle={{ color: '#00ff88' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>&lt; 0.5</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={6}>
          <Card className="glass-card">
            <Tooltip title="本地未算 self-corr(BRAIN 端可能 PENDING 不出值)。提交前需用 refresh-can-submit 触发本地重算,或冒险提交。">
              <Statistic
                title={
                  <Space>
                    未算
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={summary.self_corr_unknown ?? 0}
                valueStyle={{ color: '#888' }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>无本地 _self_corr</Text>
            </Tooltip>
          </Card>
        </Col>
      </Row>

      {/* Self-corr trap warning — only when meaningful breach exists. */}
      {(summary.self_corr_breach ?? 0) > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message={
            <span>
              <strong>提交陷阱</strong>:积压里有 <strong>{summary.self_corr_breach}</strong> 个本地算 self-corr ≥ 0.7,
              虽然 <code>can_submit=true</code>(因为 BRAIN 端 SELF_CORRELATION 常 PENDING 不出值)但
              真实提交时会被 BRAIN 拒。表格里这些行 checkbox 已禁用、且已沉到队列末尾。
              真正可提交 ≈ <strong>{(summary.self_corr_safe ?? 0) + (summary.self_corr_near ?? 0)}</strong> 个
              + <strong>{summary.self_corr_unknown ?? 0}</strong> 个未算(冒险或先 refresh)。
            </span>
          }
        />
      )}

      {/* Batch actions */}
      <Space style={{ marginTop: 16, marginBottom: 8 }} wrap>
        <Popconfirm
          title={`提交选中的 ${selectedRowKeys.length} 个 alpha`}
          description={
            pickedSubmitCount < selectedRowKeys.length
              ? `注意：选中项里有 ${selectedRowKeys.length - pickedSubmitCount} 个非「建议提交」档，仍会逐个尝试提交。`
              : '逐个调用 /alphas/{id}/submit 提交到 BRAIN。'
          }
          okText="确认提交"
          cancelText="取消"
          disabled={selectedRowKeys.length === 0 || submitting}
          onConfirm={onBatchSubmit}
        >
          <Button
            type="primary"
            icon={<SendOutlined />}
            disabled={selectedRowKeys.length === 0}
            loading={submitting}
          >
            提交选中（{selectedRowKeys.length}）
          </Button>
        </Popconfirm>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => qc.invalidateQueries({ queryKey: ['ops/submit-backlog'] })}
        >
          刷新
        </Button>
        <Tooltip title="把积压重排成「广度最大化」的提交顺序：每步挑与已选∪已提交集最不相关的 alpha 先提交（零 BRAIN 成本）">
          <Button
            icon={<OrderedListOutlined />}
            type={showDrain ? 'primary' : 'default'}
            onClick={() => setShowDrain((s) => !s)}
          >
            {showDrain ? '隐藏正交抽干顺序' : '正交抽干顺序'}
          </Button>
        </Tooltip>
        {selectedRowKeys.length > 0 && (
          <Text type="secondary">
            其中「建议提交」档 {pickedSubmitCount} 个
          </Text>
        )}
      </Space>

      {/* P0-1: set-level orthogonal drain order — breadth-maximising submit
          sequence (Grinold-Kahn: effective breadth ≤ 1/ρ). Lazy-loaded. */}
      {showDrain && (
        <Card
          className="glass-card"
          size="small"
          style={{ marginBottom: 12, borderColor: '#9c88ff55' }}
          title={
            <Space>
              <OrderedListOutlined />
              <span>正交抽干顺序（最大化组合广度）</span>
              {drainFetching && <Spin size="small" />}
            </Space>
          }
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="按此顺序提交，每个都最大化增量正交性"
            description={
              <Text style={{ fontSize: 12 }}>
                贪心选择：每步挑「与已选 ∪ 已提交集 最大相关性最低」的 alpha 先提交
                （Grinold-Kahn：有效广度 ≤ 1/ρ，先提交最正交的才真正增加独立下注）。相关性来自
                本地 PnL（零 BRAIN 成本）+ 已存 self_corr；阻塞项=与已选集 max-corr ≥ 阈值，提交价值低。
                {drainData?.note ? (<><br /><Text type="warning">{drainData.note}</Text></>) : null}
              </Text>
            }
          />
          <Space wrap style={{ marginBottom: 8 }}>
            <Tag color="cyan">候选 {drainData?.n_candidates ?? 0}</Tag>
            <Tag color="success">可正交提交 {drainData?.n_selected ?? 0}</Tag>
            <Tag color="error">相关性阻塞 {drainData?.n_blocked ?? 0}</Tag>
            <Tag>有本地 PnL {drainData?.n_with_pnl ?? 0}/{drainData?.n_candidates ?? 0}</Tag>
            <Popconfirm
              title={`按正交顺序提交选中的 ${drainSelectedKeys.length} 个`}
              description="逐个调 /alphas/{id}/submit 提交到 BRAIN（不可逆，消耗配额）。"
              okText="确认提交"
              cancelText="取消"
              disabled={drainSelectedKeys.length === 0 || submitting}
              onConfirm={async () => {
                // submit in the DRAIN (orthogonal) order, not click order
                const order = (drainData?.selected || []).map((d) => d.alpha_pk)
                const pks = order.filter((pk) => drainSelectedKeys.includes(pk))
                await submitPks(pks)
                setDrainSelectedKeys([])
              }}
            >
              <Button
                type="primary"
                size="small"
                icon={<SendOutlined />}
                disabled={drainSelectedKeys.length === 0}
                loading={submitting}
              >
                按顺序提交选中（{drainSelectedKeys.length}）
              </Button>
            </Popconfirm>
            <Button
              size="small"
              disabled={(drainData?.selected?.length ?? 0) === 0}
              onClick={() => setDrainSelectedKeys((drainData?.selected || []).map((d) => d.alpha_pk))}
            >
              全选可提交 {drainData?.n_selected ?? 0}
            </Button>
          </Space>
          <Table
            size="small"
            rowKey="alpha_pk"
            rowSelection={{ selectedRowKeys: drainSelectedKeys, onChange: setDrainSelectedKeys }}
            dataSource={drainData?.selected || []}
            columns={drainColumns}
            pagination={{ pageSize: 20 }}
            locale={{ emptyText: drainFetching ? '计算中…' : '无可正交提交的干净 alpha' }}
          />
          {(drainData?.blocked?.length ?? 0) > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: 'pointer', color: '#888', fontSize: 12 }}>
                相关性阻塞 {drainData.blocked.length} 个（与已选集 max-corr ≥ {drainData?.threshold ?? 0.7}，提交近重复、价值低）
              </summary>
              <Table
                size="small"
                rowKey="alpha_pk"
                dataSource={drainData.blocked}
                columns={drainBlockedColumns}
                pagination={{ pageSize: 10 }}
                style={{ marginTop: 8 }}
              />
            </details>
          )}
        </Card>
      )}

      {/* Conditional query — client-side filter on the loaded backlog. KPI 卡
          和进度条仍显示全量(综合视图),只有下方表格收窄到筛选子集。 */}
      <Card className="glass-card" size="small" style={{ marginBottom: 12 }}>
        <Space wrap align="center" style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>条件查询：</Text>
          <Select
            mode="multiple"
            allowClear
            value={verdictFilter}
            onChange={(v) => resetFilter(() => setVerdictFilter(v))}
            placeholder="推荐"
            style={{ minWidth: 200 }}
            maxTagCount="responsive"
            options={VERDICT_BUCKETS.map((k) => ({ value: k, label: VERDICT_BUCKET_LABEL[k] }))}
          />
          <Select
            mode="multiple"
            allowClear
            value={selfCorrFilter}
            onChange={(v) => resetFilter(() => setSelfCorrFilter(v))}
            placeholder="Self-corr 状态"
            style={{ minWidth: 220 }}
            maxTagCount="responsive"
            options={Object.entries(SELF_CORR_BUCKETS).map(([k, m]) => ({ value: k, label: m.label }))}
          />
          <Select
            mode="multiple"
            allowClear
            value={universeFilter}
            onChange={(v) => resetFilter(() => setUniverseFilter(v))}
            placeholder={universeOptions.length ? 'Universe' : '当前无 universe 数据'}
            style={{ minWidth: 200 }}
            maxTagCount="responsive"
            disabled={universeOptions.length === 0}
            options={universeOptions}
          />
          {anyFilterActive && (
            <>
              <Text type="secondary" style={{ fontSize: 12 }}>
                筛选后 {filteredItems.length} / {items.length}
              </Text>
              <Button
                size="small"
                onClick={() => resetFilter(() => {
                  setVerdictFilter([])
                  setSelfCorrFilter([])
                  setUniverseFilter([])
                })}
              >
                清空筛选
              </Button>
            </>
          )}
        </Space>
      </Card>

      <Card className="glass-card" size="small">
        <Table
          size="small"
          rowKey="alpha_pk"
          rowSelection={rowSelection}
          dataSource={filteredItems}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{
            emptyText: anyFilterActive
              ? '当前筛选条件下无 alpha — 试着放宽或清空筛选'
              : '无积压 alpha（can_submit 且未提交为空）',
          }}
        />
      </Card>
    </div>
  )
}
