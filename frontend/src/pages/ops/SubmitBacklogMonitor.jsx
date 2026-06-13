import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
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
  ExperimentOutlined,
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
            边际贡献推荐本应 <strong>{m.label}</strong>(质量门槛),但与已提交策略的相关度 ≥ 0.7
            已撞资格门槛 → 实际无法提交。<br />
            意味"想法对但实现重复" — 可重做以降低相关度,而非删除。
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
  if (v === null || v === undefined) return <Tag color="default">未计算</Tag>
  if (v >= 0.7) return <Tag color="error">撞门槛 {v.toFixed(3)}</Tag>
  if (v >= 0.5) return <Tag color="gold">接近门槛 {v.toFixed(3)}</Tag>
  return <Tag color="success">{v.toFixed(3)}</Tag>
}

// Max-corr to the already-selected ∪ submitted set (lower = more breadth added).
function maxCorrTag(v) {
  if (v === null || v === undefined) return <Tag>—</Tag>
  const color = v < 0.3 ? 'success' : v < 0.5 ? 'gold' : v < 0.7 ? 'orange' : 'error'
  return <Tag color={color}>{v.toFixed(3)}</Tag>
}

// Marginal ΔSharpe to the submitted-pool combined portfolio (>0 = adding this
// alpha improves combined Sharpe; <0 = dilutes; — = no local PnL / no base pool).
// significant=false → |ΔSharpe| within its block-bootstrap noise floor (k·SE):
// statistically indistinguishable from 0, NOT used to rank (rendered greyed).
function deltaSharpeTag(v, significant = true, se = null) {
  if (v === null || v === undefined) return <Tag>—</Tag>
  if (!significant) {
    return (
      <Tooltip title={`Sharpe 增量的绝对值未超过噪声地板(1.64·标准误${se != null ? `, 标准误=${se.toFixed(3)}` : ''}) → 与 0 无法区分,未用于排序`}>
        <Tag color="default" style={{ opacity: 0.5 }}>{v > 0 ? '+' : ''}{v.toFixed(3)} ·噪声</Tag>
      </Tooltip>
    )
  }
  const color = v > 0.02 ? 'success' : v >= 0 ? 'gold' : 'error'
  return <Tag color={color}>{v > 0 ? '+' : ''}{v.toFixed(3)}</Tag>
}

// Robustness (#39) — per-alpha OS-survival proxy from sub-period Sharpe
// consistency (frozen-IS window). ROBUST=一致/无深亏段;FRAGILE=孤峰货。
const ROBUSTNESS_LABEL = { ROBUST: '稳健', MODERATE: '一般', FRAGILE: '脆弱' }
function robustnessTag(score, verdict) {
  if (score === null || score === undefined) return <Tag color="default">无收益数据</Tag>
  const meta = {
    ROBUST: 'success', MODERATE: 'gold', FRAGILE: 'error',
  }[verdict] || 'default'
  return (
    <Tooltip title={`稳健性评分 ${score.toFixed(3)}(各子周期 Sharpe 一致性,样本内口径;提交前仍需在当前数据上重新回测确认)`}>
      <Tag color={meta}>{ROBUSTNESS_LABEL[verdict] || verdict || '—'} {score.toFixed(2)}</Tag>
    </Tooltip>
  )
}

// BRAIN-official sub-universe Sharpe (#39) — narrow-universe robustness.
// WQ 隐性标准要求 > ~0.7。
// 子股票池 Sharpe — 窄股票池稳健度。
function subUnivTag(v) {
  if (v === null || v === undefined) return <Tag>—</Tag>
  const color = v >= 1.0 ? 'success' : v >= 0.7 ? 'gold' : 'error'
  return <Tag color={color}>{v.toFixed(2)}</Tag>
}

// IS diagnostic card (Phase C, 2026-06-08) — aggregated 5-dim submit-selection
// summary. overall = SUBMIT/REVIEW/HOLD/SKIP (economic margin gate → hard risk
// dims → marginal scorecard). Tooltip expands the 4 supporting dims. 口径=IS.
const CARD_OVERALL_META = {
  SUBMIT: { color: 'success', label: '可提交' },
  REVIEW: { color: 'gold', label: '复核' },
  HOLD: { color: 'orange', label: '暂缓' },
  SKIP: { color: 'error', label: '不提交' },
}
const CARD_DIM_COLOR = { ok: 'success', warn: 'gold', risk: 'error', unknown: 'default' }
const CARD_DIM_LEVEL_LABEL = { ok: '正常', warn: '注意', risk: '风险', unknown: '未知' }
const CARD_DIM_LABEL = { overfit: '过拟合', liquidity: '流动性', crowding: '拥挤', sub_universe: '子股票池' }

function cardTag(card) {
  if (!card) return <Tag>—</Tag>
  const m = CARD_OVERALL_META[card.overall] || { color: 'default', label: card.overall }
  const dims = card.dims || {}
  return (
    <Tooltip
      title={
        <div style={{ fontSize: 12 }}>
          <div style={{ marginBottom: 6 }}>
            综合 <strong>{m.label}</strong> — {card.reason}
            <br />口径=样本内(BRAIN 隐藏样本外数据,这不是样本外预测)
          </div>
          {['overfit', 'liquidity', 'crowding', 'sub_universe'].map((k) => {
            const d = dims[k] || {}
            return (
              <div key={k} style={{ marginBottom: 2 }}>
                <Tag color={CARD_DIM_COLOR[d.level] || 'default'} style={{ marginRight: 4 }}>
                  {CARD_DIM_LABEL[k]} {CARD_DIM_LEVEL_LABEL[d.level] || d.level}
                </Tag>
                <Text style={{ fontSize: 11 }} type="secondary">{d.note}</Text>
              </div>
            )
          })}
        </div>
      }
    >
      <Tag color={m.color}>{m.label}</Tag>
    </Tooltip>
  )
}

// Current-data re-sim verdict (v2, 2026-06-08) — on-demand re-sim vs frozen-IS
// baseline. 口径=当前 IS(非 OS)。「持平」≠ should-submit(仍被 self_corr/marginal 门挡)。
const RESIM_META = {
  stable: { color: 'success', label: '持平' },
  hold_gated: { color: 'gold', label: '持平·仍被门挡' },
  soft_decay: { color: 'orange', label: '软衰减' },
  hard_decay: { color: 'error', label: '硬衰减' },
  margin_killed: { color: 'error', label: 'margin死' },
  unmeasurable_cached: { color: 'default', label: '缓存·无法测' },
  error: { color: 'default', label: '失败' },
}
function resimTag(row) {
  if (!row) return null
  const m = RESIM_META[row.verdict] || { color: 'default', label: row.verdict }
  const showPct = ['stable', 'hold_gated', 'soft_decay', 'hard_decay'].includes(row.verdict)
  const pctTxt =
    showPct && row.resim_pct != null ? ` ${((row.resim_pct - 1) * 100).toFixed(0)}%` : ''
  const rs = row.resim_sharpe != null ? row.resim_sharpe.toFixed(2) : '—'
  const bs = row.baseline_sharpe != null ? row.baseline_sharpe.toFixed(2) : '—'
  return (
    <Tooltip
      title={
        <div style={{ fontSize: 12 }}>
          <div>
            提交时基准 <strong>{bs}</strong> → 当前 <strong>{rs}</strong>
            {row.resim_pct != null ? `(为基准的 ${(row.resim_pct * 100).toFixed(0)}%)` : ''}
          </div>
          <div style={{ marginTop: 2 }}>{row.reason}</div>
          <div style={{ marginTop: 4 }} className="">
            口径=当前样本内(BRAIN 隐藏样本外数据,这不是样本外预测)。
            {row.reused_from_regime ? '复用了行情监测探针的结果(6 小时内)。' : ''}
          </div>
          <div style={{ color: '#ff7875', marginTop: 2 }}>
            ⚠️ 「持平」≠ 该提交 — 仍需通过相关度&lt;0.7 + 边际贡献不稀释组合这两道门。
          </div>
        </div>
      }
    >
      <Tag color={m.color}>{m.label}{pctTxt}</Tag>
    </Tooltip>
  )
}

// Self-corr 状态分桶:与 KPI 卡(撞门/近门槛/安全/未算)同口径,客户端过滤复用。
const SELF_CORR_BUCKETS = {
  breach: { label: '撞门槛(≥0.7)', test: (v) => v !== null && v !== undefined && v >= 0.7 },
  near: { label: '接近门槛(0.5-0.7)', test: (v) => v !== null && v !== undefined && v >= 0.5 && v < 0.7 },
  safe: { label: '安全(<0.5)', test: (v) => v !== null && v !== undefined && v < 0.5 },
  unknown: { label: '未计算', test: (v) => v === null || v === undefined },
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
  // #39 (2026-06-07): robustness gate for the selector (0 = annotate-only / show
  // all; >0 = drop FRAGILE / no-PnL into the fragile bucket).
  const [minRobustness, setMinRobustness] = useState(0)
  // Current-data re-sim (v2, 2026-06-08): on-demand decay check. resimResults maps
  // alpha_pk → verdict row; resimJobId drives the poll; resimActivePks = the pks
  // in the in-flight batch (for per-row spinners).
  const [resimJobId, setResimJobId] = useState(null)
  const [resimResults, setResimResults] = useState({})
  const [resimActivePks, setResimActivePks] = useState([])
  const [resimPosting, setResimPosting] = useState(false)

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
    queryKey: ['ops/submit-backlog/drain-order', region, minRobustness],
    queryFn: () => api.getOpsSubmitBacklogDrainOrder({ region, minRobustness }),
    enabled: showDrain,
    staleTime: 30_000,
  })

  // Methodology-audit kill-switch is now computed INSIDE the drain endpoint over
  // this backlog's offline↔BRAIN pairs (drainData.recon_verdict) and actually
  // gates the sign-routing — so no separate /marginal-reconciliation call here.

  // Forward-test: predictions frozen at submit time (accumulates as new alphas
  // are submitted). predicted↔realized is structurally blocked today (no live
  // post-submission PnL) — the tag surfaces the accumulation + blocked status.
  const { data: forwardReconData } = useQuery({
    queryKey: ['ops/marginal-reconciliation/forward', region],
    queryFn: () => api.getOpsMarginalReconciliationForward({ region }),
    enabled: showDrain,
    staleTime: 60_000,
  })

  // Poll the current-data re-sim job while it runs; stop on done/error.
  const { data: resimJob } = useQuery({
    queryKey: ['ops/resim-backlog', resimJobId],
    queryFn: () => api.getResimBacklogStatus(resimJobId),
    enabled: !!resimJobId,
    refetchInterval: (q) => {
      const s = q?.state?.data?.status
      return s === 'done' || s === 'error' ? false : 2500
    },
  })
  const resimJobRunning =
    !!resimJobId && resimJob && resimJob.status !== 'done' && resimJob.status !== 'error'

  // Merge each poll's partial results into the pk→verdict map; toast on terminal.
  useEffect(() => {
    if (!resimJob) return
    if (Array.isArray(resimJob.results) && resimJob.results.length) {
      setResimResults((prev) => {
        const next = { ...prev }
        for (const r of resimJob.results) next[r.alpha_pk] = r
        return next
      })
    }
    if (resimJob.status === 'done') {
      message.success(`当前数据重新回测完成（${resimJob.done ?? 0}/${resimJob.total ?? 0}）`)
    } else if (resimJob.status === 'error') {
      message.error(`重新回测失败：${resimJob.error || '未知'}`)
    }
  }, [resimJob])

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

  // REVIEW shortlist pks from the drain panel (diagnostic_card.overall==='REVIEW',
  // across selected ∪ blocked) — the batch re-sim target.
  const reviewPks = useMemo(() => {
    const all = [...(drainData?.selected || []), ...(drainData?.blocked || [])]
    return all
      .filter((d) => d.diagnostic_card?.overall === 'REVIEW')
      .map((d) => d.alpha_pk)
  }, [drainData])

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

  // Trigger an on-demand current-data re-sim of `pks` (read-only; never submits).
  // A single Redis NX lock serialises batches server-side → 409 if one is running.
  const triggerResim = async (pks) => {
    const uniq = Array.from(new Set((pks || []).filter((p) => p != null)))
    if (uniq.length === 0) {
      message.info('无候选可重新回测')
      return
    }
    setResimPosting(true)
    try {
      const res = await api.resimBacklogCurrent(uniq)
      setResimActivePks(uniq)
      setResimJobId(res.job_id)
      message.success(`已入队 ${res.enqueued} 个当前数据重新回测（工作进程后台执行，约 ${Math.max(1, Math.ceil(uniq.length / 2)) * 2} 分钟）`)
    } catch (e) {
      message.error(e?.response?.data?.detail || e?.message || '重新回测触发失败')
    } finally {
      setResimPosting(false)
    }
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
        <Tooltip title="与已提交策略集的相关度(BRAIN 硬门槛 < 0.7)。BRAIN 端这个值经常迟迟算不出来,导致系统仍标为可提交,但本地算出的撞门槛会让真实提交被 BRAIN 拒绝。">
          <Space size={4}>相关度 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
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
        <Tooltip title="边际贡献综合评分 — 同一推荐档内的排序依据">
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
        <Tooltip title="alpha 自身的 Margin(每单位交易利润,单位 bps)— 要 ≥5bps 才能覆盖交易成本盈利,是经济门槛">
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
        <Tooltip title="样本内 5 维体检卡:聚合 过拟合/流动性/拥挤/子股票池 + 边际打分 → 综合提交建议(经济门槛→硬风险维度→边际贡献)。口径=样本内,不是样本外预测。悬停看各维度。">
          <Space size={4}>体检卡 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'diagnostic_card',
      key: 'diagnostic_card',
      width: 96,
      render: (card) => cardTag(card),
    },
    {
      title: (
        <Tooltip title="按需在当前数据上重新回测,对比提交时的样本内基准判断是否衰减(持平/软衰减/硬衰减/margin死/缓存无法测)。只读、不提交。口径=当前样本内,不是样本外。点「测」跑单个;批量见上方「复核」按钮。⚠️「持平」≠ 该提交。">
          <Space size={4}>当前数据 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'alpha_pk',
      key: 'resim_current',
      width: 130,
      render: (pk) => {
        const row = resimResults[pk]
        if (row) return resimTag(row)
        const pending = resimJobRunning && resimActivePks.includes(pk)
        return (
          <Button
            size="small"
            icon={<ExperimentOutlined />}
            loading={pending}
            disabled={resimJobRunning && !pending}
            onClick={() => triggerResim([pk])}
          >
            测
          </Button>
        )
      },
    },
    {
      title: (
        <Tooltip title="与「已选 ∪ 已提交」集合的最大相关性 — 越低,这次提交带来的独立广度越多">
          <Space size={4}>新增广度(最大相关) <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'max_corr_to_selected',
      key: 'max_corr_to_selected',
      width: 130,
      align: 'right',
      render: (v) => maxCorrTag(v),
    },
    {
      title: (
        <Tooltip title="加入这个 alpha 后「已提交组合」的 Sharpe 增量（等风险加权，样本外窗口）。大于 0=改善组合、值得提交；小于 0=稀释。组合层排序依据。">
          <Space size={4}>组合Sharpe增量 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'delta_sharpe',
      key: 'delta_sharpe',
      width: 130,
      align: 'right',
      render: (v, r) => deltaSharpeTag(v, r.delta_sharpe_significant, r.delta_sharpe_se),
    },
    {
      title: (
        <Tooltip title="基于经对账验证的 Sharpe 增量方向的排序层:增益(先提交)→中性→稀释(排最后,提交会拖累组合)→无收益数据。对账被证伪 / 样本不足时退回纯广度排序,此列显示「—」。">
          <Space size={4}>方向层 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'value_tier',
      key: 'value_tier',
      width: 92,
      render: (t) => {
        if (t === null || t === undefined) return <Text type="secondary">—</Text>
        const meta = {
          0: { c: 'green', label: '增益' },
          1: { c: 'gold', label: '中性' },
          2: { c: 'red', label: '稀释' },
          3: { c: 'default', label: '无收益数据' },
        }[t] || { c: 'default', label: String(t) }
        return <Tag color={meta.c}>{meta.label}</Tag>
      },
    },
    {
      title: (
        <Tooltip title="抗过拟合稳健性:各子周期 Sharpe 一致性 → 稳健(一致)/ 一般 / 脆弱(孤峰货,样本外容易衰减)。BRAIN 隐藏样本外数据,这是提交前唯一可控的质量代理。样本内口径。">
          <Space size={4}>稳健性 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'robustness_score',
      key: 'robustness_score',
      width: 130,
      render: (v, r) => robustnessTag(v, r.robustness_verdict),
    },
    {
      title: (
        <Tooltip title="BRAIN 官方的子股票池 Sharpe — 窄股票池稳健度(WQ 隐性标准 >0.7)。比基于历史收益的稳健性更直接;自动提交以此作为一道软门槛。">
          <Space size={4}>子股票池 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'sub_universe_sharpe',
      key: 'sub_universe_sharpe',
      width: 96,
      align: 'right',
      render: (v) => subUnivTag(v),
    },
    {
      title: '相关度',
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
            description={`将对待扫描/陈旧的 alpha 逐个调 BRAIN 重算边际贡献推荐（每个约 5-20 秒，消耗 BRAIN 配额）。工作进程后台执行，确认触发？`}
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
            <span>边际贡献审计覆盖进度</span>
            <Progress
              percent={progressPct}
              size="small"
              style={{ width: 200 }}
              status={pending > 0 ? 'active' : 'success'}
            />
            <Text type="secondary">
              已审计 {audited} / {total}
              {pending > 0 ? `，${pending} 个待扫描（旧数据格式或未审计）` : '，全部已带推荐'}
            </Text>
          </Space>
        }
        description={
          pending > 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              「待扫描」是仍带旧赛季审计结果或从未审计的 alpha — 点「扫描全部」用当前
              赛季 + 边际贡献打分卡刷新出「建议提交 / 中性 / 不建议」推荐。定时任务也会逐批补,
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
            <Text type="secondary" style={{ fontSize: 12 }}>可提交但还没提交</Text>
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
            <Tooltip title="本地算出与已提交集的相关度 ≥ 0.7 → BRAIN 提交时硬门槛拒绝。这些不应被选去批量提交,勾选框已禁用。">
              <Statistic
                title={
                  <Space>
                    相关度撞门槛
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
              title="接近门槛"
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
            <Tooltip title="本地还没算出相关度(BRAIN 端可能一直算不出值)。提交前需触发本地重算,或冒险提交。">
              <Statistic
                title={
                  <Space>
                    未计算
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={summary.self_corr_unknown ?? 0}
                valueStyle={{ color: '#888' }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>本地还没有相关度</Text>
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
              <strong>提交陷阱</strong>:积压里有 <strong>{summary.self_corr_breach}</strong> 个本地算出与已提交策略相关度 ≥ 0.7,
              虽然系统标为可提交(因为 BRAIN 端的相关度常常一直算不出值)但
              真实提交时会被 BRAIN 拒。表格里这些行勾选框已禁用、且已沉到队列末尾。
              真正可提交 ≈ <strong>{(summary.self_corr_safe ?? 0) + (summary.self_corr_near ?? 0)}</strong> 个
              + <strong>{summary.self_corr_unknown ?? 0}</strong> 个未计算(冒险或先重算相关度)。
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
              : '逐个提交到 BRAIN。'
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
        <Tooltip title="把积压重排成「广度最大化」的提交顺序：每步挑与已选∪已提交集最不相关的 alpha 先提交（不消耗 BRAIN 配额）">
          <Button
            icon={<OrderedListOutlined />}
            type={showDrain ? 'primary' : 'default'}
            onClick={() => setShowDrain((s) => !s)}
          >
            {showDrain ? '隐藏差异化抽干顺序' : '差异化抽干顺序'}
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
              <span>差异化抽干顺序（最大化组合广度）</span>
              {drainFetching && <Spin size="small" />}
            </Space>
          }
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              drainData?.objective === 'value'
                ? '质量×广度排序：在不撞相关性墙的前提下，先提交最能提升组合 Sharpe 的 alpha'
                : '纯广度排序：每步挑与已选∪已提交集最不相关的 alpha 先提交'
            }
            description={
              <Text style={{ fontSize: 12 }}>
                {drainData?.objective === 'value' ? (
                  <>
                    组合层：广度作硬约束（与已选集的最大相关性 &lt; 阈值），<b>组合 Sharpe 增量</b> 作目标——
                    每步挑「加入后最能抬升已提交组合 Sharpe」的 alpha（基准组合 {drainData?.n_base_pool} 个已提交，
                    等风险加权，样本外窗口）。Sharpe 增量与相关性均来自本地收益数据（不消耗 BRAIN 配额）。
                  </>
                ) : (
                  <>
                    贪心策略：每步挑「与已选 ∪ 已提交集 最大相关性最低」的 alpha（有效广度 ≤ 1/相关性，
                    先提交差异最大的才真正增加独立下注）。相关性来自本地收益数据 + 已存的相关度。
                  </>
                )}
                {' '}被阻塞项=与已选集最大相关性 ≥ 阈值，属于近重复、提交价值低。
                {drainData?.note ? (<><br /><Text type="warning">{drainData.note}</Text></>) : null}
              </Text>
            }
          />
          <Space wrap style={{ marginBottom: 8 }}>
            <Tooltip title="抗过拟合稳健门槛:>0 时把脆弱 / 无收益数据的候选剔进脆弱桶,只让稳健者进提交序列。0 = 仅标注不过滤。口径=样本内各子周期一致性。">
              <Space size={4}>
                <Text type="secondary" style={{ fontSize: 12 }}>稳健门槛:</Text>
                <Select
                  size="small"
                  value={minRobustness}
                  onChange={(v) => { setMinRobustness(v); setDrainSelectedKeys([]) }}
                  style={{ width: 130 }}
                  options={[
                    { value: 0, label: '仅标注(0)' },
                    { value: 0.4, label: '≥0.4' },
                    { value: 0.5, label: '≥0.5' },
                    { value: 0.6, label: '≥0.6(严)' },
                  ]}
                />
              </Space>
            </Tooltip>
            <Tag color={drainData?.objective === 'value' ? 'purple' : 'default'}>
              {drainData?.objective === 'value'
                ? `质量×广度 (Sharpe 增量 · 基准组合 ${drainData?.n_base_pool ?? 0})`
                : '纯广度 (无基准组合)'}
            </Tag>
            <Tag color="cyan">候选 {drainData?.n_candidates ?? 0}</Tag>
            <Tag color="success">可提交 {drainData?.n_selected ?? 0}</Tag>
            <Tag color="error">相关性阻塞 {drainData?.n_blocked ?? 0}</Tag>
            {(drainData?.min_robustness ?? 0) > 0 && (
              <Tag color="volcano">稳健剔除 {drainData?.n_fragile ?? 0}</Tag>
            )}
            <Tag>有本地收益数据 {drainData?.n_with_pnl ?? 0}/{drainData?.n_candidates ?? 0}</Tag>
            {drainData?.objective === 'value' && (
              <Tooltip title={`Sharpe 增量超出自身噪声地板(绝对值>1.64·标准误)的数量;以及超过去偏后期望最大值(${drainData?.deflated_threshold ?? '—'})的数量。0 个显著 = Sharpe 增量幅度统计上全是噪声 → 不作精细排序,改用经对账验证的方向分层。`}>
                <Tag color={(drainData?.n_significant ?? 0) > 0 ? 'purple' : 'warning'}>
                  Sharpe 增量显著 {drainData?.n_significant ?? 0}/{drainData?.n_with_pnl ?? 0} · 去偏后仍超 {drainData?.n_survives_deflation ?? 0}
                </Tag>
              </Tooltip>
            )}
            {drainData?.recon_verdict && (
              <Tooltip
                title={
                  `方法论自检开关（对本积压实时计算,真正决定排序方式）:` +
                  `本地离线算的 Sharpe 增量,与 BRAIN 权威的提交前后边际,两者符号一致率 ` +
                  `${drainData.recon_sign_rate != null ? (drainData.recon_sign_rate * 100).toFixed(0) + '%' : '—'} ` +
                  `(样本数=${drainData.recon_n_compared})。结论=${drainData.recon_verdict}。` +
                  `≤60%(被证伪) ⇒ 离线代理失效,自动停用方向排序、退回纯广度排序。` +
                  `注意:这是本地预测↔BRAIN(两者都是回测估计),不是真实样本外结果。`
                }
              >
                <Tag
                  color={
                    drainData.recon_verdict === 'supported' ? 'green'
                    : drainData.recon_verdict === 'weak' ? 'gold'
                    : drainData.recon_verdict === 'FALSIFIED' ? 'red'
                    : 'default'
                  }
                >
                  对账 {drainData.recon_verdict === 'supported' ? '✓' : drainData.recon_verdict === 'FALSIFIED' ? '✗ 退广度' : '·'}{' '}
                  {drainData.recon_sign_rate != null ? (drainData.recon_sign_rate * 100).toFixed(0) + '%同号' : drainData.recon_verdict}
                </Tag>
              </Tooltip>
            )}
            {forwardReconData && (
              <Tooltip
                title={
                  `前瞻验证:提交时冻结的预测,不随组合增长漂移。` +
                  `已冻结 ${forwardReconData.n_frozen} 个(可度量 ${forwardReconData.n_measurable})。` +
                  `当前仅核验 本地预测↔BRAIN 提交前后(两者都是回测估计,不是真实样本外结果);` +
                  `本地预测↔真实结果 尚无任何数据:${forwardReconData.realized_blocked_reason || ''}`
                }
              >
                <Tag color={forwardReconData.n_frozen > 0 ? 'geekblue' : 'default'}>
                  前瞻冻结 {forwardReconData.n_frozen ?? 0}
                  {forwardReconData.n_with_realized > 0
                    ? ` · 真实结果 ${forwardReconData.n_with_realized}`
                    : ' · 真实结果结构性不可得'}
                </Tag>
              </Tooltip>
            )}
            <Popconfirm
              title={`按差异化顺序提交选中的 ${drainSelectedKeys.length} 个`}
              description="逐个提交到 BRAIN（不可逆，消耗配额）。"
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
            <Tooltip title="对当前所有「复核」档(体检卡=复核)候选批量在当前数据上重新回测,判断是否衰减。只读、不提交。约 2 分钟/2 个(分块并发)。">
              <Button
                size="small"
                icon={<ExperimentOutlined />}
                loading={resimPosting || resimJobRunning}
                disabled={reviewPks.length === 0 || resimJobRunning}
                onClick={() => triggerResim(reviewPks)}
              >
                复核「复核」档当前数据（{reviewPks.length}）
              </Button>
            </Tooltip>
            {resimJobRunning && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                重新回测中 {resimJob?.done ?? 0}/{resimJob?.total ?? 0}…
              </Text>
            )}
          </Space>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 8 }}
            message={
              <Text style={{ fontSize: 12 }}>
                「当前数据」列 = 按需在当前 BRAIN 数据上重新回测(口径 <b>当前样本内,不是样本外</b>),对比提交时的样本内基准判断是否衰减。
                <b>「持平」≠ 该提交</b> — 个体不衰减不代表对组合有边际贡献,仍需通过相关度&lt;0.7 + 不稀释组合这两道门。
                命中 BRAIN 去重缓存的标「无法测」(返回的是存储值,不是当前数据)。此功能只读、绝不提交。
              </Text>
            }
          />
          <Table
            size="small"
            rowKey="alpha_pk"
            rowSelection={{ selectedRowKeys: drainSelectedKeys, onChange: setDrainSelectedKeys }}
            dataSource={drainData?.selected || []}
            columns={drainColumns}
            pagination={{ pageSize: 20 }}
            locale={{ emptyText: drainFetching ? '计算中…' : '无可差异化提交的干净 alpha' }}
          />
          {(drainData?.blocked?.length ?? 0) > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: 'pointer', color: '#888', fontSize: 12 }}>
                相关性阻塞 {drainData.blocked.length} 个（与已选集最大相关性 ≥ {drainData?.threshold ?? 0.7}，提交近重复、价值低）
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
          {(drainData?.fragile?.length ?? 0) > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: 'pointer', color: '#ff7a45', fontSize: 12 }}>
                稳健门槛剔除 {drainData.fragile.length} 个(稳健性评分 &lt; {drainData?.min_robustness} 或无本地收益数据 — 孤峰货,样本外容易衰减;提交风险高)
              </summary>
              <Table
                size="small"
                rowKey="alpha_pk"
                dataSource={drainData.fragile}
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
            placeholder="相关度状态"
            style={{ minWidth: 220 }}
            maxTagCount="responsive"
            options={Object.entries(SELF_CORR_BUCKETS).map(([k, m]) => ({ value: k, label: m.label }))}
          />
          <Select
            mode="multiple"
            allowClear
            value={universeFilter}
            onChange={(v) => resetFilter(() => setUniverseFilter(v))}
            placeholder={universeOptions.length ? '股票池' : '当前无股票池数据'}
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
              : '无积压 alpha（可提交但未提交的为空）',
          }}
        />
      </Card>
    </div>
  )
}
