import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Card,
  Col,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ThunderboltOutlined,
  CheckCircleTwoTone,
  CloseCircleTwoTone,
  QuestionCircleTwoTone,
  ClockCircleTwoTone,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text, Paragraph } = Typography

/**
 * OptimizationCyclesMonitor — /ops/optimization-cycles (Phase 16-A, 2026-05-29).
 *
 * Stage A 的可观测面:14d 转化率(GO/STOP gate 信号)+ 累计 KPI + 最近 cycle
 * 列表 + 当下进行中 cycle。后端源:`GET /ops/optimization/cycles?days=14`
 * (backend/routers/ops.py:CycleSummary)。
 *
 * GO/STOP 判定语义(plan §6):
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
    color: 'processing',
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
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/optimization-cycles'],
    queryFn: () => api.getOpsOptimizationCycles(14, 100),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  const cycles = data?.cycles || []
  const conv = data?.conversion_rate_14d ?? 0
  const totalCycles = data?.total_cycles_14d ?? 0
  const totalVariants = data?.total_variants_14d ?? 0
  const totalWinners = data?.total_winners_14d ?? 0
  const totalSubmitted = data?.total_submitted_14d ?? 0

  const decision = useMemo(
    () => classifyDecision(conv, totalCycles),
    [conv, totalCycles],
  )
  const decisionMeta = DECISION_LABELS[decision]

  const runningCycles = useMemo(
    () => cycles.filter((c) => !c.cycle_finished_at && !c.error),
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
      width: 90,
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
      width: 80,
      render: (v, c) =>
        c.n_variants > 0 ? (
          <Tooltip title={`本 cycle 转化率 = ${v} / ${c.n_variants} = ${((v / c.n_variants) * 100).toFixed(1)}%`}>
            <Text strong={v > 0} type={v > 0 ? 'success' : undefined}>
              {v}
            </Text>
          </Tooltip>
        ) : (
          v
        ),
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
      <div>
        <Title level={4} style={{ marginBottom: 4 }}>
          <ThunderboltOutlined /> 优化闭环 Stage A — 14d 观测
          {isFetching && <Spin size="small" style={{ marginLeft: 12 }} />}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          SettingsSweepGenerator 对 1230 个 delay-1 近门 alpha(sharpe ∈ [1.0, 1.5))做 10 变异 sweep。
          6h beat × 10 候选 = 40 cycle/天。14d GO/STOP 决策门见下卡。
          NEVER auto-submit:winner 落 alpha 表 → 进 <Link to="/ops/submit-backlog">/ops/submit-backlog</Link> 人工 review。
        </Paragraph>
      </div>

      {/* Decision banner */}
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
          </Space>
        }
        description={decisionMeta.desc}
      />

      {/* KPI row */}
      <Row gutter={16}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="14d 转化率"
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
                Stage A 软限制(全程记录到 Redis aiac:opt:sim_budget:&lt;YYYYMMDD&gt;),Stage B 才硬限。剩 {Math.max(0, OPT_DAILY_BUDGET - todaySpend)} sim 给后续 cycle。
              </Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title={`当前进行中 cycle (${runningCycles.length})`} size="small">
            {runningCycles.length === 0 ? (
              <Text type="secondary">
                无进行中 cycle。下个 beat: 北京时间 02:15 / 08:15 / 14:15 / 20:15 每 6h fire 一次,单 beat 串行跑 10 个候选 cycle(~2.5-3h 跑完)。
              </Text>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                {runningCycles.map((c) => (
                  <div key={c.id}>
                    <Text>
                      <Tag color="processing">run #{c.id}</Tag>
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
            emptyText: '无 cycle 数据 — ENABLE_OPTIMIZATION_LOOP=False 时为空。',
          }}
        />
      </Card>
    </Space>
  )
}
