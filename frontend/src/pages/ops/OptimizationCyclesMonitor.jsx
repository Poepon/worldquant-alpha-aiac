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
    label: '继续 — 转化率达标',
    desc: '近 14 天转化率 > 20% 且优化轮次 ≥ 30 → 可考虑升级到第二阶段(表达式改写 + 自动提交)',
  },
  STOP: {
    color: 'error',
    icon: <CloseCircleTwoTone twoToneColor="#ff4d4f" />,
    label: '停止 — 证明瓶颈在选择而非优化',
    desc: '近 14 天转化率 < 10% → 优化不是真正的杠杆,改去抽干提交积压,验证了竞品分析的诊断',
  },
  TUNE: {
    color: 'warning',
    icon: <QuestionCircleTwoTone twoToneColor="#faad14" />,
    label: '调参 — 调整后再判',
    desc: '近 14 天转化率 10-20% → 调整参数优化器的参数(衰减/窗口取值)或延期观察,不直接升档',
  },
  SAMPLE: {
    // 'info' (合法 Alert type;decisionMeta.color 仅喂 line 528 的 <Alert>,
    // 不用于 Tag — antd Alert type 只接受 success/info/warning/error)
    color: 'info',
    icon: <ClockCircleTwoTone twoToneColor="#1890ff" />,
    label: '样本不足',
    desc: '累计优化轮次 < 30,等更多数据(典型 4 轮/天 × 8 天 ≈ 32 轮满足阈值)',
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
        <Tag color="error">失败</Tag>
      </Tooltip>
    )
  }
  if (!cycle.cycle_finished_at) {
    return <Tag color="processing">进行中</Tag>
  }
  if (cycle.n_winners > 0) {
    return <Tag color="success">优胜 ×{cycle.n_winners}</Tag>
  }
  return <Tag color="default">0 优胜</Tag>
}

// trigger_source → distinguishable colored tag. 'manual' (user clicked
// 「以此为蓝本优化」on an alpha) vs 'beat' (6h auto scan) vs 'pipeline_hook'
// (Stage C near-miss push). Unknown sources fall back to the raw string.
const TRIGGER_META = {
  beat: { color: 'blue', label: '定时任务', tip: '每 6 小时自动定时任务扫描接近门槛的 alpha 触发(现已停)' },
  manual: { color: 'purple', label: '手动', tip: '用户在前端以某 alpha 为蓝本手动触发 — 独立于开关仍可用' },
  pipeline_hook: { color: 'cyan', label: '流水线', tip: '第三阶段流水线推送接近门槛的候选触发' },
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
      message.warning({ content: res?.message || '优化已停止', duration: 6 })
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
      title: '蓝本',
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
      title: '生成器',
      dataIndex: 'generator_name',
      key: 'generator_name',
      width: 130,
      render: (v) => <Tag>{v}</Tag>,
    },
    {
      title: '触发来源',
      dataIndex: 'trigger_source',
      key: 'trigger_source',
      width: 100,
      filters: [
        { text: '手动', value: 'manual' },
        { text: '定时任务', value: 'beat' },
        { text: '流水线', value: 'pipeline_hook' },
      ],
      onFilter: (value, c) => c.trigger_source === value,
      render: (v) => triggerTag(v),
    },
    {
      title: '变异数',
      key: 'variants',
      width: 100,
      render: (_, c) => (
        <Tooltip title={`生成=${c.n_variants}, 回测=${c.sim_budget_used}/${c.sim_budget_granted}`}>
          {c.n_variants} ({c.sim_budget_used} 次回测)
        </Tooltip>
      ),
    },
    {
      title: '优胜数',
      dataIndex: 'n_winners',
      key: 'n_winners',
      width: 150,
      render: (v, c) => {
        const ids = c.winner_alpha_ids || []
        const count =
          c.n_variants > 0 ? (
            <Tooltip title={`本轮转化率 = ${v} / ${c.n_variants} = ${((v / c.n_variants) * 100).toFixed(1)}%`}>
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
              <Tooltip title="本轮记录有优胜,但对应的 alpha 行没有回链(入库失败或已被清理)— 无法跳转">
                <Tag color="warning" style={{ marginInlineEnd: 0 }}>ID 缺失</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: '已提交',
      dataIndex: 'n_submitted',
      key: 'n_submitted',
      width: 90,
      render: (v) =>
        v > 0 ? (
          <Text strong type="success">
            {v}
          </Text>
        ) : (
          <Tooltip title="本阶段的提交策略永远只入队、不直接提交 → 已提交数恒为 0。优胜者进提交积压页待人工确认">
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
        message="每 6 小时的自动优化闭环已停(优化闭环开关已关闭)"
        description={
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Text>
              近 14 天转化率指标不再更新(下方决策卡 / 累计指标仅作历史参照)。
              手动「以 alpha 为蓝本」优化仍可用(独立于此开关),入口在
              {' '}
              <Link to="/alphas">Alpha 详情页</Link>。
            </Text>
            {flagPresent && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                后端报告的开关状态:
                <Tag
                  color={flagEnabled ? 'success' : 'default'}
                  style={{ marginInlineStart: 6, marginInlineEnd: 0 }}
                >
                  优化闭环开关 = {flagEnabled ? '开' : '关'}
                </Tag>
                {flagEnabled && (
                  <Text type="warning" style={{ fontSize: 12, marginInlineStart: 8 }}>
                    ⚠️ 开关实际为「开」—— 每 6 小时的定时任务仍会触发并与挖掘流水线抢并发回测名额,建议点【停止优化】。
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
            <ThunderboltOutlined /> 优化闭环 第一阶段 — 近 14 天观测(已冻结)
            {isFetching && <Spin size="small" style={{ marginLeft: 12 }} />}
          </Title>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            参数优化器对 1230 个接近门槛的 alpha(Sharpe ∈ [1.0, 1.5))各做 10 个变体的参数扫描。
            每 6 小时定时任务 × 10 候选 = 40 轮/天 —— <Text type="warning">该自动定时任务现已停</Text>。
            绝不自动提交:优胜者落入 alpha 表 → 进 <Link to="/ops/submit-backlog">提交积压页</Link> 人工复核。
          </Paragraph>
        </div>
        {/* 紧急止血控件(2026-06-07)。Start 被禁用 —— 四池世界下启动会跑与
            HG/S/E 抢 sim 槽的孤立 Celery 任务;Stop / abort 保留可用。 */}
        <Space direction="vertical" align="end" size={4} style={{ minWidth: 240 }}>
          <Space>
            <Tooltip title="当前架构下启动会跑一个与挖掘流水线抢并发回测名额的孤立后台任务,默认禁用;如需紧急手动跑,改配置打开优化闭环开关后重启。">
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled
              >
                启动优化(已禁用)
              </Button>
            </Tooltip>
            <Popconfirm
              title="停止优化?(紧急止血)"
              description={
                <div style={{ maxWidth: 360 }}>
                  <Text>3 步停止保证<strong>不会自动再启动</strong>:</Text>
                  <ul style={{ paddingLeft: 18, margin: '4px 0' }}>
                    <li>关闭优化闭环开关(持久化到数据库,工作进程 15 秒内读到)</li>
                    <li>设置中止标志(工作进程当前一轮跑完即跳出循环)</li>
                    <li>标记所有进行中的轮次为 <Tag color="error" style={{ margin: 0 }}>已被用户中止</Tag></li>
                  </ul>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    已下发到 BRAIN 的回测会自然完成(BRAIN 没有撤回接口)。
                    此操作仅在开关意外为「开」时需要。
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
                停止优化
              </Button>
            </Popconfirm>
          </Space>
          <Popconfirm
            title="中止当前批次?(只停进行中的轮次,不动开关)"
            description={
              <div style={{ maxWidth: 320 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  比【停止】更窄的紧急止血:设置中止标志让当前批次跑完即止,
                  但不关闭优化闭环开关(下次定时任务仍可能触发,要彻底停请用【停止优化】)。
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
              ({(conv * 100).toFixed(1)}% 转化 · {totalCycles} 轮 · {totalWinners}/{totalVariants} 优胜)
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
              title="近 14 天转化率(已冻结)"
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
              优胜数 / 变异数 比值
            </Text>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="近 14 天累计轮次" value={totalCycles} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              继续/停止 判定阈值 ≥ 30 轮
            </Text>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="近 14 天 优胜 / 变异"
              value={`${totalWinners} / ${totalVariants}`}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              优胜者已落入 alpha 表
            </Text>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="近 14 天 已提交"
              value={totalSubmitted}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              人工通过积压页提交数
            </Text>
          </Card>
        </Col>
      </Row>

      {/* Today budget + currently running */}
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Card title="今日回测配额(每日上限 400 次)" size="small">
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
                优化的软限制(全程记录)。自动定时任务已停,
                正常应为 0;若 &gt; 0 说明仍有手动蓝本 / 残留批次在烧挖掘流水线共享的回测名额。
              </Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card
            title={`当前进行中的轮次 (${runningCycles.length})`}
            size="small"
          >
            {runningCycles.length === 0 ? (
              <Text type="secondary">
                无进行中的轮次。每 6 小时的自动定时任务已停;手动「以 alpha 为蓝本」触发的轮次会显示在此(独立于开关)。
              </Text>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                {runningCycles.map((c) => (
                  <div key={c.id}>
                    <Text>
                      <Tag color="processing">第 #{c.id} 轮</Tag>
                      {triggerTag(c.trigger_source)}
                      蓝本=<Link to={`/alphas/${c.parent_alpha_id}`} target="_blank">#{c.parent_alpha_id}</Link>
                      &nbsp;已 {formatDuration(durationSec(c.cycle_started_at, null))}
                      ({c.sim_budget_used}/{c.sim_budget_granted} 次回测)
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
              触发来源 = 手动 · 独立于优化闭环开关仍可用
            </Text>
          </Space>
        }
        size="small"
      >
        {manualCycles.length === 0 ? (
          <Text type="secondary">
            近 14 天无手动蓝本优化记录。入口:在 <Link to="/alphas">Alpha 详情页</Link> 点【以此为蓝本优化】。
          </Text>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={6}>
            {manualCycles.map((c) => (
              <div key={c.id}>
                <Space size={6} wrap>
                  <Tag color="purple">第 #{c.id} 轮</Tag>
                  蓝本=
                  {c.parent_alpha_id ? (
                    <Link to={`/alphas/${c.parent_alpha_id}`} target="_blank">#{c.parent_alpha_id}</Link>
                  ) : (
                    '—'
                  )}
                  {statusTag(c)}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {c.n_winners > 0 ? `优胜 ×${c.n_winners}` : `${c.n_variants} 个变异 / 0 优胜`}
                    {' · '}
                    {c.sim_budget_used}/{c.sim_budget_granted} 次回测
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
      <Card title={`最近轮次(近 14 天内,最多 100 行)`} size="small">
        <Table
          dataSource={cycles}
          columns={columns}
          rowKey="id"
          pagination={{
            pageSize: 20,
            showSizeChanger: false,
            showTotal: (n) => `共 ${n} 轮`,
          }}
          size="small"
          locale={{
            emptyText: '无轮次数据 — 优化闭环开关关闭(自动定时任务已停)时为空;手动蓝本优化产生的轮次仍会出现在此。',
          }}
        />
      </Card>
    </Space>
  )
}
