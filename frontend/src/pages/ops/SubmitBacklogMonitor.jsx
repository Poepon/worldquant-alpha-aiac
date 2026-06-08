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

// Marginal ΔSharpe to the submitted-pool combined portfolio (>0 = adding this
// alpha improves combined Sharpe; <0 = dilutes; — = no local PnL / no base pool).
// significant=false → |ΔSharpe| within its block-bootstrap noise floor (k·SE):
// statistically indistinguishable from 0, NOT used to rank (rendered greyed).
function deltaSharpeTag(v, significant = true, se = null) {
  if (v === null || v === undefined) return <Tag>—</Tag>
  if (!significant) {
    return (
      <Tooltip title={`|ΔSharpe| 未超噪声地板(1.64·SE${se != null ? `, SE=${se.toFixed(3)}` : ''}) → 与 0 不可区分,未用于排序`}>
        <Tag color="default" style={{ opacity: 0.5 }}>{v > 0 ? '+' : ''}{v.toFixed(3)} ·噪声</Tag>
      </Tooltip>
    )
  }
  const color = v > 0.02 ? 'success' : v >= 0 ? 'gold' : 'error'
  return <Tag color={color}>{v > 0 ? '+' : ''}{v.toFixed(3)}</Tag>
}

// Robustness (#39) — per-alpha OS-survival proxy from sub-period Sharpe
// consistency (frozen-IS window). ROBUST=一致/无深亏段;FRAGILE=孤峰货。
function robustnessTag(score, verdict) {
  if (score === null || score === undefined) return <Tag color="default">无PnL</Tag>
  const meta = {
    ROBUST: 'success', MODERATE: 'gold', FRAGILE: 'error',
  }[verdict] || 'default'
  return (
    <Tooltip title={`稳健分 ${score.toFixed(3)}(子周期 Sharpe 一致性,冻结 IS 口径;提交前仍需 re-sim 当前数据确认)`}>
      <Tag color={meta}>{verdict || '—'} {score.toFixed(2)}</Tag>
    </Tooltip>
  )
}

// BRAIN-official sub-universe Sharpe (#39) — narrow-universe robustness.
// WQ 隐性标准要求 > ~0.7。
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
const CARD_DIM_LABEL = { overfit: '过拟合', liquidity: '流动性', crowding: '拥挤', sub_universe: '子宇宙' }

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
            <br />口径=IS(BRAIN 隐藏 OS,非 OS 预测)
          </div>
          {['overfit', 'liquidity', 'crowding', 'sub_universe'].map((k) => {
            const d = dims[k] || {}
            return (
              <div key={k} style={{ marginBottom: 2 }}>
                <Tag color={CARD_DIM_COLOR[d.level] || 'default'} style={{ marginRight: 4 }}>
                  {CARD_DIM_LABEL[k]} {d.level}
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
  // #39 (2026-06-07): robustness gate for the selector (0 = annotate-only / show
  // all; >0 = drop FRAGILE / no-PnL into the fragile bucket).
  const [minRobustness, setMinRobustness] = useState(0)

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
        <Tooltip title="IS 5 维体检卡(Phase C):聚合 过拟合/流动性/拥挤/子宇宙 + 边际打分 → 综合提交建议(经济门→硬风险维→边际)。口径=IS,非 OS 预测。悬停看各维。">
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
      title: (
        <Tooltip title="加入此 alpha 后「已提交池组合」的 Sharpe 增量（等风险加权，OS 窗口）。>0=改善组合、值得提交；<0=稀释。组合层(L2)排序依据。">
          <Space size={4}>Δ组合Sharpe <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
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
        <Tooltip title="基于经对账验证的 ΔSharpe 方向的排序层:增益(先提交)→中性→稀释(排最后,提交会拖累组合)→无PnL。对账 FALSIFIED/样本不足时退纯广度,此列为「—」。">
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
          3: { c: 'default', label: '无PnL' },
        }[t] || { c: 'default', label: String(t) }
        return <Tag color={meta.c}>{meta.label}</Tag>
      },
    },
    {
      title: (
        <Tooltip title="抗过拟合稳健性(#39):子周期 Sharpe 一致性 → ROBUST(一致)/ MODERATE / FRAGILE(孤峰货,易 OS 衰减)。BRAIN 隐藏 OS,这是提交前唯一可控质量代理。冻结 IS 口径。">
          <Space size={4}>稳健 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'robustness_score',
      key: 'robustness_score',
      width: 130,
      render: (v, r) => robustnessTag(v, r.robustness_verdict),
    },
    {
      title: (
        <Tooltip title="BRAIN 官方 sub-universe Sharpe — 窄宇宙稳健度(WQ 隐性标准 >0.7)。比冻结-PnL 稳健性更直接;auto-submit 以此为 G5b 软门。">
          <Space size={4}>Sub-univ <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'sub_universe_sharpe',
      key: 'sub_universe_sharpe',
      width: 96,
      align: 'right',
      render: (v) => subUnivTag(v),
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
            message={
              drainData?.objective === 'value'
                ? '质量×广度排序：在不撞相关墙的前提下，先提交最能提升组合 Sharpe 的 alpha'
                : '纯广度排序：每步挑与已选∪已提交集最不相关的 alpha 先提交'
            }
            description={
              <Text style={{ fontSize: 12 }}>
                {drainData?.objective === 'value' ? (
                  <>
                    组合层(L2)：广度作硬约束（与已选集 max-corr &lt; 阈值），<b>Δ组合Sharpe</b> 作目标——
                    每步挑「加入后最能抬升已提交池组合 Sharpe」的 alpha（基准池 {drainData?.n_base_pool} 个已提交，
                    等风险加权，OS 窗口）。ΔSharpe 与相关性均来自本地 PnL（零 BRAIN 成本）。
                  </>
                ) : (
                  <>
                    贪心：每步挑「与已选 ∪ 已提交集 最大相关性最低」的 alpha（Grinold-Kahn：有效广度 ≤ 1/ρ，
                    先提交最正交的才真正增加独立下注）。相关性来自本地 PnL + 已存 self_corr。
                  </>
                )}
                {' '}阻塞项=与已选集 max-corr ≥ 阈值，是近重复、提交价值低。
                {drainData?.note ? (<><br /><Text type="warning">{drainData.note}</Text></>) : null}
              </Text>
            }
          />
          <Space wrap style={{ marginBottom: 8 }}>
            <Tooltip title="抗过拟合稳健门(#39):>0 时把 FRAGILE / 无PnL 候选剔进 fragile 桶,只让稳健者进提交序。0 = 仅标注不过滤。口径=冻结 IS 子周期一致性。">
              <Space size={4}>
                <Text type="secondary" style={{ fontSize: 12 }}>稳健门:</Text>
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
                ? `质量×广度 (ΔSharpe · 基准池 ${drainData?.n_base_pool ?? 0})`
                : '纯广度 (无基准池)'}
            </Tag>
            <Tag color="cyan">候选 {drainData?.n_candidates ?? 0}</Tag>
            <Tag color="success">可提交 {drainData?.n_selected ?? 0}</Tag>
            <Tag color="error">相关性阻塞 {drainData?.n_blocked ?? 0}</Tag>
            {(drainData?.min_robustness ?? 0) > 0 && (
              <Tag color="volcano">稳健剔除 {drainData?.n_fragile ?? 0}</Tag>
            )}
            <Tag>有本地 PnL {drainData?.n_with_pnl ?? 0}/{drainData?.n_candidates ?? 0}</Tag>
            {drainData?.objective === 'value' && (
              <Tooltip title={`ΔSharpe 超出自身噪声地板(|Δ|>1.64·SE)的数量;越 deflated 期望最大值(${drainData?.deflated_threshold ?? '—'})的数量。0 显著 = ΔSharpe 幅度统计上全是噪声 → 不作精排,改用经对账验证的方向(sign)分层。`}>
                <Tag color={(drainData?.n_significant ?? 0) > 0 ? 'purple' : 'warning'}>
                  ΔSh 幅度显著 {drainData?.n_significant ?? 0}/{drainData?.n_with_pnl ?? 0} · 越deflated {drainData?.n_survives_deflation ?? 0}
                </Tag>
              </Tooltip>
            )}
            {drainData?.recon_verdict && (
              <Tooltip
                title={
                  `方法论 kill-switch（在 drain 端点内对本积压实时计算,真正 gating 排序）:` +
                  `离线 ΔSharpe 与 BRAIN 权威 before-and-after 边际的符号一致率 ` +
                  `${drainData.recon_sign_rate != null ? (drainData.recon_sign_rate * 100).toFixed(0) + '%' : '—'} ` +
                  `(n=${drainData.recon_n_compared})。verdict=${drainData.recon_verdict}。` +
                  `≤60%(FALSIFIED) ⇒ 离线代理失效,自动停用 sign 排序退纯广度。` +
                  `注意:这是 predicted↔BRAIN(两者都是回测-merge 估计),非 live realized。`
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
                  `Forward-test:提交时冻结的预测(metrics._recon_predicted_delta_sharpe),不随池增长漂移。` +
                  `已冻结 ${forwardReconData.n_frozen} 个(可度量 ${forwardReconData.n_measurable})。` +
                  `当前仅核验 predicted↔BRAIN-before-and-after(两者都是回测-merge 估计,非 live realized);` +
                  `predicted↔realized 尚无任何数据:${forwardReconData.realized_blocked_reason || ''}`
                }
              >
                <Tag color={forwardReconData.n_frozen > 0 ? 'geekblue' : 'default'}>
                  forward 冻结 {forwardReconData.n_frozen ?? 0}
                  {forwardReconData.n_with_realized > 0
                    ? ` · realized ${forwardReconData.n_with_realized}`
                    : ' · realized 结构性不可得'}
                </Tag>
              </Tooltip>
            )}
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
          {(drainData?.fragile?.length ?? 0) > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: 'pointer', color: '#ff7a45', fontSize: 12 }}>
                稳健门剔除 {drainData.fragile.length} 个(robustness_score &lt; {drainData?.min_robustness} 或无本地 PnL — 孤峰货,易 OS 衰减;提交风险高)
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
