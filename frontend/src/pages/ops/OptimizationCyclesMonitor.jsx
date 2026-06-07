import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Popconfirm,
  Progress,
  Row,
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
  ThunderboltOutlined,
  CheckCircleTwoTone,
  CloseCircleTwoTone,
  QuestionCircleTwoTone,
  ClockCircleTwoTone,
  StopOutlined,
  PlayCircleOutlined,
  PoweroffOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text, Paragraph } = Typography

/**
 * OptimizationCyclesMonitor — /ops/optimization-cycles
 *   (Phase 16-A, 2026-05-29; 四池世界冻结轻改 2026-06-07).
 *
 * 现状(四池世界):6h 自动优化 beat 已停(ENABLE_OPTIMIZATION_LOOP OFF),
 * 14d 转化率 KPI 不再更新。但「以 alpha 为蓝本」手动优化独立于此 flag 仍可用。
 * 顶部 Start 按钮被禁用 —— 它会启动与 HG/S/E 四池抢共享 BRAIN sim 槽的
 * 孤立 Celery 任务;Stop/abort 保留(紧急止血)。
 *
 * Stage A 的可观测面:14d 转化率(历史 GO/STOP gate 信号,现已冻结)+ 累计 KPI
 * + 最近 cycle 列表(含手动蓝本历史)+ 当下进行中 cycle。后端源:
 * `GET /ops/optimization/cycles?days=14`(backend/routers/ops.py:CycleSummary)。
 *
 * GO/STOP 判定语义(plan §6,现仅作历史参照):
 *   conversion_rate_14d > 0.20 → GO   (可考虑 Stage B 升级)
 *   conversion_rate_14d < 0.10 → STOP (selection-limited 实证,改抽 backlog)
 *   0.10-0.20 + total_cycles ≥ 30 → TUNE (调 SettingsSweepGenerator 参数)
 *   total_cycles < 30 → 样本不足,延长观察
 *
 * Refetch 每 30s,与 SubmitBacklogMonitor 同口径。
 */

const DECISION_LABELS = {
  GO: {
    color: 'success',
    icon: <CheckCircleTwoTone twoToneColor="#52c41a" />,
    label: 'GO — 转化率达标',
    desc: '14d 转化率 > 20% AND cycles ≥ 30 → 可考虑升级 Stage B(表达式 rewrite + auto-submit)',
  },
  STOP: {
    color: 'error',
    icon: <CloseCircleTwoTone twoToneColor="#ff4d4f" />,
    label: 'STOP — selection-limited 实证',
    desc: '14d 转化率 < 10% → 优化不是真杠杆,改抽 121 提交积压(/ops/submit-backlog),验证了 competitive_analysis_v3 诊断',
  },
  TUNE: {
    color: 'warning',
    icon: <QuestionCircleTwoTone twoToneColor="#faad14" />,
    label: 'TUNE — 调参再判',
    desc: '14d 转化率 10-20% → 调 SettingsSweepGenerator 参数(decay/window 取值)或延期观察,不直接升档',
  },
  SAMPLE: {
    // 'info' (合法 Alert type;decisionMeta.color 仅喂 line 528 的 <Alert>,
    // 不用于 Tag — antd Alert type 只接受 success/info/warning/error)
    color: 'info',
    icon: <ClockCircleTwoTone twoToneColor="#1890ff" />,
    label: 'SAMPLE — 样本不足',
    desc: '累计 cycles < 30,等更多数据(典型 4 cycle/天 × 8 天 ≈ 32 cycles 满阈)',
  },
}

function classifyDecision(rate, totalCycles) {
  if (totalCycles < 30) return 'SAMPLE'
  if (rate > 0.20) return 'GO'
  if (rate < 0.10) return 'STOP'
  return 'TUNE'
}

function statusTag(cycle) {
  if (cycle.error) {
    return (
      <Tooltip title={cycle.error}>
        <Tag color="error">FAIL</Tag>
      </Tooltip>
    )
  }
  if (!cycle.cycle_finished_at) {
    return <Tag color="processing">RUNNING</Tag>
  }
  if (cycle.n_winners > 0) {
    return <Tag color="success">WINNER ×{cycle.n_winners}</Tag>
  }
  return <Tag color="default">0 winner</Tag>
}

// trigger_source → distinguishable colored tag. 'manual' (user clicked
// 「以此为蓝本优化」on an alpha) vs 'beat' (6h auto scan) vs 'pipeline_hook'
// (Stage C near-miss push). Unknown sources fall back to the raw string.
const TRIGGER_META = {
  beat: { color: 'blue', label: '定时 beat', tip: '6h 自动 beat 扫描近门 alpha 触发(现已停)' },
  manual: { color: 'purple', label: '手动', tip: '用户在前端以某 alpha 为蓝本手动触发（POST /alphas/{id}/optimize）— 独立于 flag 仍可用' },
  pipeline_hook: { color: 'cyan', label: '管线', tip: 'Stage C pipeline-hook 推送 near-miss 触发' },
}

function triggerTag(source) {
  const meta = TRIGGER_META[source]
  if (!meta) return <Tag>{source || '—'}</Tag>
  return (
    <Tooltip title={meta.tip}>
      <Tag color={meta.color}>{meta.label}</Tag>
    </Tooltip>
  )
}

// Backend OptimizationRun.cycle_started_at / cycle_finished_at are
// SQLAlchemy DateTime (naive, server UTC). Pydantic serializes to ISO
// 8601 WITHOUT a 'Z' suffix or `+00:00` offset — JS Date() would then
// interpret as local time (in SH = UTC+8, this caused an 8h+ phantom
// duration on first ship 2026-05-29). Append 'Z' explicitly so JS
// parses as UTC.
function parseUTC(ts) {
  if (!ts) return null
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(ts)
  return new Date(hasTz ? ts : ts + 'Z')
}

function formatTs(ts) {
  if (!ts) return '—'
  try {
    const d = parseUTC(ts)
    return d.toLocaleString('zh-CN', {
      year: '2-digit', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    })
  } catch (_) {
    return ts
  }
}

function durationSec(start, end) {
  if (!start) return null
  const a = parseUTC(start).getTime()
  const b = end ? parseUTC(end).getTime() : Date.now()
  return Math.max(0, Math.round((b - a) / 1000))
}

function formatDuration(sec) {
  if (sec === null || sec === undefined) return '—'
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m${sec % 60}s`
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
}

export default function OptimizationCyclesMonitor() {
  const qc = useQueryClient()
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/optimization-cycles'],
    queryFn: () => api.getOpsOptimizationCycles(14, 100),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  // Stop only (2026-06-07). Stop = flag OFF + abort batch + Redis flag (3-step
  // no-auto-restart guarantee) — kept as an emergency 止血 control. Start is
  // DISABLED in the 四池世界:启动会跑与 HG/S/E 抢 sim 槽的孤立 Celery 任务。
  const stopMutation = useMutation({
    mutationFn: () => api.stopOpsOptimization(),
    onSuccess: (res) => {
      message.warning({ content: res?.message || 'Stage A 已停止', duration: 6 })
      qc.invalidateQueries({ queryKey: ['ops/optimization-cycles'] })
    },
    onError: (e) =>
      message.error(e?.response?.data?.detail || e?.message || '停止失败'),
  })
  // Abort batch — narrower 紧急止血(只中止当前 in-flight cycle,不翻 flag)。
  const abortMutation = useMutation({
    mutationFn: () => api.abortOpsOptimizationBatch(),
    onSuccess: (res) => {
      message.warning({ content: res?.message || '已中止当前批次', duration: 6 })
      qc.invalidateQueries({ queryKey: ['ops/optimization-cycles'] })
    },
    onError: (e) =>
      message.error(e?.response?.data?.detail || e?.message || '中止失败'),
  })

  const cycles = data?.cycles || []
  const conv = data?.conversion_rate_14d ?? 0
  const totalCycles = data?.total_cycles_14d ?? 0
  const totalVariants = data?.total_variants_14d ?? 0
  const totalWinners = data?.total_winners_14d ?? 0
  const totalSubmitted = data?.total_submitted_14d ?? 0
  // flag_enabled may be absent in the cycles response — when undefined we
  // can't trust it, so we still show the freeze banner unconditionally.
  const flagEnabled = data?.flag_enabled ?? false
  const flagPresent = data?.flag_enabled !== undefined && data?.flag_enabled !== null
  const flagSource = data?.flag_source || 'default'
  const flagNote = data?.flag_note || null
  const stopPending = stopMutation.isPending || abortMutation.isPending

  const decision = useMemo(
    () => classifyDecision(conv, totalCycles),
    [conv, totalCycles],
  )
  const decisionMeta = DECISION_LABELS[decision]

  const runningCycles = useMemo(
    () => cycles.filter((c) => !c.cycle_finished_at && !c.error),
    [cycles],
  )

  // Manual-blueprint history — trigger_source === 'manual'. These run
  // independently of ENABLE_OPTIMIZATION_LOOP (POST /alphas/{id}/optimize),
  // so they remain the live path in the 四池世界.
  const manualCycles = useMemo(
    () => cycles.filter((c) => c.trigger_source === 'manual'),
    [cycles],
  )

  // Today's sim spend — proxy for OPT_DAILY_SIM_BUDGET enforcement visibility.
  // Frontend sums sim_budget_used for cycles started after 00:00 SH today.
  // cycle_started_at is naive-UTC from backend → parseUTC() else JS treats
  // as local-time and gets the day boundary wrong on SH browsers.
  const todaySpend = useMemo(() => {
    const now = new Date()
    const todaySH = new Date(now.getTime() + 8 * 3600_000)
    const startSHIso = `${todaySH.getUTCFullYear()}-${String(todaySH.getUTCMonth() + 1).padStart(2, '0')}-${String(todaySH.getUTCDate()).padStart(2, '0')}T00:00:00+08:00`
    const startSHMs = new Date(startSHIso).getTime()
    let spent = 0
    for (const c of cycles) {
      const cycleMs = parseUTC(c.cycle_started_at)?.getTime()
      if (cycleMs && cycleMs >= startSHMs) {
        spent += c.sim_budget_used || 0
      }
    }
    return spent
  }, [cycles])

  // OPT_DAILY_SIM_BUDGET default 400 — backend Settings constant.
  // Hard-coded here to match (Settings not exposed via API for Stage A).
  const OPT_DAILY_BUDGET = 400
  const budgetPct = Math.min(100, Math.round((todaySpend / OPT_DAILY_BUDGET) * 100))

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
        message="加载优化 cycle 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }

  const columns = [
    { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
    {
      title: 'Parent',
      dataIndex: 'parent_alpha_id',
      key: 'parent_alpha_id',
      width: 80,
      render: (v) =>
        v ? (
          <Link to={`/alphas/${v}`} target="_blank">
            #{v}
          </Link>
        ) : (
          '—'
        ),
    },
    {
      title: 'Generator',
      dataIndex: 'generator_name',
      key: 'generator_name',
      width: 130,
      render: (v) => <Tag>{v}</Tag>,
    },
    {
      title: 'Trigger',
      dataIndex: 'trigger_source',
      key: 'trigger_source',
      width: 100,
      filters: [
        { text: '手动', value: 'manual' },
        { text: '定时 beat', value: 'beat' },
        { text: '管线', value: 'pipeline_hook' },
      ],
      onFilter: (value, c) => c.trigger_source === value,
      render: (v) => triggerTag(v),
    },
    {
      title: 'Variants',
      key: 'variants',
      width: 100,
      render: (_, c) => (
        <Tooltip title={`generated=${c.n_variants}, sims=${c.sim_budget_used}/${c.sim_budget_granted}`}>
          {c.n_variants} ({c.sim_budget_used} sims)
        </Tooltip>
      ),
    },
    {
      title: 'Winners',
      dataIndex: 'n_winners',
      key: 'n_winners',
      width: 150,
      render: (v, c) => {
        const ids = c.winner_alpha_ids || []
        const count =
          c.n_variants > 0 ? (
            <Tooltip title={`本 cycle 转化率 = ${v} / ${c.n_variants} = ${((v / c.n_variants) * 100).toFixed(1)}%`}>
              <Text strong={v > 0} type={v > 0 ? 'success' : undefined}>{v}</Text>
            </Tooltip>
          ) : (
            <Text>{v}</Text>
          )
        if (v === 0) return count
        return (
          <Space size={4} wrap>
            {count}
            {ids.length > 0 ? (
              ids.map((aid) => (
                <Link key={aid} to={`/alphas/${aid}`} target="_blank">
                  <Tag color="success" style={{ marginInlineEnd: 0, cursor: 'pointer' }}>#{aid}</Tag>
                </Link>
              ))
            ) : (
              <Tooltip title="cycle 记录有 winner,但其 alpha 行未回链(持久化失败或已被清理)— 无法跳转">
                <Tag color="warning" style={{ marginInlineEnd: 0 }}>ID 缺失</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: 'Submitted',
      dataIndex: 'n_submitted',
      key: 'n_submitted',
      width: 90,
      render: (v) =>
        v > 0 ? (
          <Text strong type="success">
            {v}
          </Text>
        ) : (
          <Tooltip title="Stage A SubmitPolicy 永远返回 queue → n_submitted 恒为 0。winner 进 /ops/submit-backlog 待人工确认">
            <Text type="secondary">0</Text>
          </Tooltip>
        ),
    },
    {
      title: '耗时',
      key: 'duration',
      width: 80,
      render: (_, c) => formatDuration(durationSec(c.cycle_started_at, c.cycle_finished_at)),
    },
    {
      title: '开始',
      dataIndex: 'cycle_started_at',
      key: 'cycle_started_at',
      width: 130,
      render: formatTs,
    },
    {
      title: '状态',
      key: 'status',
      width: 110,
      render: (_, c) => statusTag(c),
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {/* ── 冻结 banner(四池世界,2026-06-07)──
          flag 状态字段若存在则据它显示,否则无条件显示冻结提示。 */}
      <Alert
        type="warning"
        showIcon
        icon={<PoweroffOutlined />}
        message="6h 自动优化闭环已停(ENABLE_OPTIMIZATION_LOOP OFF)"
        description={
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Text>
              14d 转化率 KPI 不再更新(下方决策卡 / 累计 KPI 仅作历史参照)。
              手动「以 alpha 为蓝本」优化仍可用(独立于此 flag),入口在
              {' '}
              <Link to="/alphas">Alpha 详情页</Link>。
            </Text>
            {flagPresent && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                后端报告 flag 状态:
                <Tag
                  color={flagEnabled ? 'success' : 'default'}
                  style={{ marginInlineStart: 6, marginInlineEnd: 0 }}
                >
                  ENABLE_OPTIMIZATION_LOOP = {flagEnabled ? 'ON' : 'OFF'}
                </Tag>
                {flagEnabled && (
                  <Text type="warning" style={{ fontSize: 12, marginInlineStart: 8 }}>
                    ⚠️ flag 实际为 ON —— 6h beat 仍会 fire 并与四池抢 sim 槽,建议点【停止 Stage A】。
                  </Text>
                )}
                {flagNote && (
                  <Tooltip title={flagNote}>
                    <Text type="secondary" style={{ fontSize: 11, marginInlineStart: 8 }}>
                      ({flagSource})
                    </Text>
                  </Tooltip>
                )}
              </Text>
            )}
          </Space>
        }
      />

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 16,
        }}
      >
        <div style={{ flex: 1 }}>
          <Title level={4} style={{ marginBottom: 4 }}>
            <ThunderboltOutlined /> 优化闭环 Stage A — 14d 观测(已冻结)
            {isFetching && <Spin size="small" style={{ marginLeft: 12 }} />}
          </Title>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            SettingsSweepGenerator 对 1230 个 delay-1 近门 alpha(sharpe ∈ [1.0, 1.5))做 10 变异 sweep。
            6h beat × 10 候选 = 40 cycle/天 —— <Text type="warning">该自动 beat 现已停</Text>。
            NEVER auto-submit:winner 落 alpha 表 → 进 <Link to="/ops/submit-backlog">/ops/submit-backlog</Link> 人工 review。
          </Paragraph>
        </div>
        {/* 紧急止血控件(2026-06-07)。Start 被禁用 —— 四池世界下启动会跑与
            HG/S/E 抢 sim 槽的孤立 Celery 任务;Stop / abort 保留可用。 */}
        <Space direction="vertical" align="end" size={4} style={{ minWidth: 240 }}>
          <Space>
            <Tooltip title="四池世界下启动会跑与 HG/S/E 抢 sim 槽的孤立 Celery 任务,默认禁用;如需紧急手动跑,改 .env 开 flag(ENABLE_OPTIMIZATION_LOOP=true)后重启。">
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled
              >
                启动 Stage A(已禁用)
              </Button>
            </Tooltip>
            <Popconfirm
              title="停止 Stage A?(紧急止血)"
              description={
                <div style={{ maxWidth: 360 }}>
                  <Text>3 步停止保证<strong>不会自动再启动</strong>:</Text>
                  <ul style={{ paddingLeft: 18, margin: '4px 0' }}>
                    <li>翻 <code>ENABLE_OPTIMIZATION_LOOP=false</code>(DB 持久化,worker 15s 内读到)</li>
                    <li>设 Redis abort 标志(worker 当前 cycle 跑完即跳出 for-loop)</li>
                    <li>标记所有进行中 cycle 为 <Tag color="error" style={{ margin: 0 }}>aborted_by_user:stop</Tag></li>
                  </ul>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    已 dispatch 到 BRAIN 的 sim 自然完成(BRAIN 无 recall API)。
                    此操作仅在 flag 意外为 ON 时需要。
                  </Text>
                </div>
              }
              okText="停止"
              okType="danger"
              cancelText="取消"
              onConfirm={() => stopMutation.mutate()}
              disabled={stopPending}
            >
              <Button
                danger
                icon={<StopOutlined />}
                loading={stopMutation.isPending}
              >
                停止 Stage A
              </Button>
            </Popconfirm>
          </Space>
          <Popconfirm
            title="中止当前批次?(只停 in-flight cycle,不翻 flag)"
            description={
              <div style={{ maxWidth: 320 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  比【停止】更窄的紧急止血:设 Redis abort 标志让当前 batch 跑完即止,
                  但不翻 ENABLE_OPTIMIZATION_LOOP(下个 beat 仍可能 fire,要彻底停请用【停止 Stage A】)。
                </Text>
              </div>
            }
            okText="中止批次"
            okType="danger"
            cancelText="取消"
            onConfirm={() => abortMutation.mutate()}
            disabled={stopPending}
          >
            <Button
              size="small"
              danger
              icon={<StopOutlined />}
              loading={abortMutation.isPending}
            >
              中止当前批次
            </Button>
          </Popconfirm>
        </Space>
      </div>

      {/* Decision banner — 历史参照(KPI 已冻结) */}
      <Alert
        type={decisionMeta.color}
        showIcon
        icon={decisionMeta.icon}
        message={
          <Space>
            <Text strong>{decisionMeta.label}</Text>
            <Text>
              ({(conv * 100).toFixed(1)}% 转化 · {totalCycles} cycles · {totalWinners}/{totalVariants} winners)
            </Text>
            <Tag color="default">历史参照 · 已冻结</Tag>
          </Space>
        }
        description={decisionMeta.desc}
      />

      {/* KPI row */}
      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="14d 转化率(已冻结)"
              value={(conv * 100).toFixed(1)}
              suffix="%"
              valueStyle={{
                color:
                  conv > 0.2 ? '#3f8600'
                  : conv < 0.1 ? '#cf1322'
                  : '#faad14',
              }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              winners / variants ratio
            </Text>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="14d 累计 cycles" value={totalCycles} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              GO/STOP 阈 ≥ 30 cycles
            </Text>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="14d winners / variants"
              value={`${totalWinners} / ${totalVariants}`}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              winners 已落 alphas 表
            </Text>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="14d submitted"
              value={totalSubmitted}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              人工通过 backlog 提交数
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Today budget + currently running */}
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Card title="今日 sim 配额(OPT_DAILY_SIM_BUDGET=400)" size="small">
            <Space direction="vertical" style={{ width: '100%' }}>
              <Progress
                percent={budgetPct}
                status={
                  budgetPct >= 90 ? 'exception'
                  : budgetPct >= 70 ? 'active'
                  : 'normal'
                }
                format={() => `${todaySpend} / ${OPT_DAILY_BUDGET}`}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                Stage A 软限制(全程记录到 Redis aiac:opt:sim_budget:&lt;YYYYMMDD&gt;)。自动 beat 已停,
                正常应为 0;若 &gt; 0 说明仍有手动蓝本 / 残留批次在烧四池共享的 sim 槽。
              </Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card
            title={`当前进行中 cycle (${runningCycles.length})`}
            size="small"
          >
            {runningCycles.length === 0 ? (
              <Text type="secondary">
                无进行中 cycle。自动 6h beat 已停;手动「以 alpha 为蓝本」触发的 cycle 会显示在此(独立于 flag)。
              </Text>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                {runningCycles.map((c) => (
                  <div key={c.id}>
                    <Text>
                      <Tag color="processing">run #{c.id}</Tag>
                      {triggerTag(c.trigger_source)}
                      parent=<Link to={`/alphas/${c.parent_alpha_id}`} target="_blank">#{c.parent_alpha_id}</Link>
                      &nbsp;已 {formatDuration(durationSec(c.cycle_started_at, null))}
                      ({c.sim_budget_used}/{c.sim_budget_granted} sims)
                    </Text>
                  </div>
                ))}
              </Space>
            )}
          </Card>
        </Col>
      </Row>

      {/* Manual-blueprint history — 独立于 flag 的 live 路径 */}
      <Card
        title={
          <Space>
            <Text strong>手动「以 alpha 为蓝本」优化历史</Text>
            <Tag color="purple">{manualCycles.length}</Tag>
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>
              trigger_source = manual · 独立于 ENABLE_OPTIMIZATION_LOOP 仍可用
            </Text>
          </Space>
        }
        size="small"
      >
        {manualCycles.length === 0 ? (
          <Text type="secondary">
            近 14d 无手动蓝本优化记录。入口:在 <Link to="/alphas">Alpha 详情页</Link> 点【以此为蓝本优化】(POST /alphas/{'{'}id{'}'}/optimize)。
          </Text>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={6}>
            {manualCycles.map((c) => (
              <div key={c.id}>
                <Space size={6} wrap>
                  <Tag color="purple">run #{c.id}</Tag>
                  parent=
                  {c.parent_alpha_id ? (
                    <Link to={`/alphas/${c.parent_alpha_id}`} target="_blank">#{c.parent_alpha_id}</Link>
                  ) : (
                    '—'
                  )}
                  {statusTag(c)}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {c.n_winners > 0 ? `winner ×${c.n_winners}` : `${c.n_variants} 变异 / 0 winner`}
                    {' · '}
                    {c.sim_budget_used}/{c.sim_budget_granted} sims
                    {' · '}
                    {formatTs(c.cycle_started_at)}
                  </Text>
                </Space>
              </div>
            ))}
          </Space>
        )}
      </Card>

      {/* Cycle table */}
      <Card title={`最近 cycles(14d 内,最多 100 行)`} size="small">
        <Table
          dataSource={cycles}
          columns={columns}
          rowKey="id"
          pagination={{
            pageSize: 20,
            showSizeChanger: false,
            showTotal: (n) => `共 ${n} cycles`,
          }}
          size="small"
          locale={{
            emptyText: '无 cycle 数据 — ENABLE_OPTIMIZATION_LOOP=False(自动 beat 已停)时为空;手动蓝本优化产生的 cycle 仍会出现在此。',
          }}
        />
      </Card>
    </Space>
  )
}
