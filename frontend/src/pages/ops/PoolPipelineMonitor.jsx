import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Col, Popconfirm, Row, Space, Spin, Statistic,
  Tabs, Tag, Tooltip, Typography, message,
} from 'antd'
import {
  ReloadOutlined, ApiOutlined, ThunderboltOutlined, WarningOutlined,
  PauseCircleOutlined, PlayCircleOutlined, HeartOutlined, DesktopOutlined,
  DatabaseOutlined, FundProjectionScreenOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * PoolPipelineMonitor — /ops/pool-pipeline「挖掘池 (HG/S/E)」统一监控页.
 *
 * 2026-06-08 合并:原 /ops/pool-pipeline + /ops/pool-queue + /ops/pool-workers
 * 三页都是同一个端点 GET /ops/pools/status 的不同投影(KPI + drain 控件曾重复
 * 两份)。合一为单页 3 Tab,后两个路由重定向到此:
 *   - 总览     KPI(worker/sim/budget)+ 近 90min 吞吐 + drain/resume 控制(唯一一处)
 *   - 队列     candidate_queue / hyp_intent 深度 + PENDING_SIM 积压告警 + 产能对比
 *   - 工作器   per-pool worker 在线 vs 期望 + lease 健康(超-lease 退出判据)+ 失败行
 * 单一数据源 api.getPoolStatus()(queryKey ['poolStatus']),每 8s 刷新。
 */
const INTENT_STAGE_COLOR = {
  PENDING: 'blue', CLAIMED: 'gold', DONE: 'green', FAILED: 'red', PURGED: 'default',
}
const CAND_STAGE_COLOR = {
  PENDING_SIM: 'blue', SIMULATING: 'gold', PENDING_EVAL: 'cyan', EVALUATING: 'gold',
  DONE: 'green', FAILED: 'red', PURGED: 'default',
}
const INTENT_ORDER = ['PENDING', 'CLAIMED', 'DONE', 'FAILED', 'PURGED']
const CAND_ORDER = ['PENDING_SIM', 'SIMULATING', 'PENDING_EVAL', 'EVALUATING', 'DONE', 'FAILED', 'PURGED']
const DEFAULT_WORKERS = 4 // fallback if backend (pre-P1) omits expected_workers
const POOLS = ['hg', 's', 'e']
const POOL_LABEL = { hg: '想法生成', s: '回测模拟', e: '评估入库' }
// 队列状态码 → 中文 label(渲染处用 LABEL[s] || s,后端 key 不改)
const STAGE_LABEL = {
  PENDING: '排队中',
  PENDING_SIM: '排队待回测',
  SIMULATING: '回测中',
  PENDING_EVAL: '排队待评估',
  EVALUATING: '评估中',
  CLAIMED: '已认领（处理中）',
  DONE: '已完成',
  FAILED: '失败',
  PURGED: '已清除',
}
// PENDING_SIM 积压阈值
const BACKLOG_RED = 500
const BACKLOG_YELLOW = 100

function StageChips({ counts, order, colorMap }) {
  return (
    <Space size={[8, 8]} wrap>
      {order.map((s) => (
        <Tag key={s} color={(counts?.[s] || 0) > 0 ? colorMap[s] : 'default'}>
          {STAGE_LABEL[s] || s}: <b>{counts?.[s] || 0}</b>
        </Tag>
      ))}
    </Space>
  )
}

// group workers_alive (e.g. ["hg-1","s-1","s-2","e-1"]) by name prefix
function countByPrefix(workersAlive) {
  const acc = { hg: 0, s: 0, e: 0 }
  for (const w of workersAlive || []) {
    const m = String(w).match(/^(hg|s|e)-/i)
    if (m) acc[m[1].toLowerCase()] += 1
  }
  return acc
}

function PoolDrainControl({ name, draining, onToggle, pending }) {
  const label = POOL_LABEL[name] || name.toUpperCase()
  return draining ? (
    <Popconfirm title={`恢复「${label}」环节?`} onConfirm={() => onToggle(name, false)}>
      <Button size="small" danger icon={<PauseCircleOutlined />} loading={pending}>
        {label} 已暂停 — 恢复
      </Button>
    </Popconfirm>
  ) : (
    <Popconfirm
      title={`暂停「${label}」环节?(软停:停止接新活,正在处理的任务跑完)`}
      onConfirm={() => onToggle(name, true)}
    >
      <Button size="small" icon={<PlayCircleOutlined />} loading={pending}>
        {label} 运行中 — 暂停
      </Button>
    </Popconfirm>
  )
}

function PoolWorkerCard({ name, online, expected, draining }) {
  const healthy = online >= expected
  const color = expected === 0 ? undefined : (healthy ? '#3f8600' : '#cf1322')
  return (
    <Card size="small">
      <Statistic
        title={(
          <Space size={4}>
            <DesktopOutlined />
            {POOL_LABEL[name] || name.toUpperCase()}
            {draining && <Tag color="orange" style={{ marginLeft: 4 }}>已暂停</Tag>}
          </Space>
        )}
        value={online}
        suffix={`/ ${expected} 应有`}
        valueStyle={{ color }}
      />
      {!healthy && expected > 0 && (
        <Text type="danger" style={{ fontSize: 12 }}>缺 {expected - online} 个工作进程</Text>
      )}
    </Card>
  )
}

function DrainControlCard({ data, onToggle, pending }) {
  return (
    <Card size="small" title="流水线环节控制（暂停接新活 / 恢复）">
      <Space size={[12, 12]} wrap>
        {POOLS.map((n) => (
          <PoolDrainControl key={n} name={n} draining={!!data?.drain?.[n]}
            onToggle={onToggle} pending={pending} />
        ))}
      </Space>
      <div style={{ marginTop: 8 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          暂停 = 软停:该环节停止接新活,正在处理的任务跑完(或超时后被回收);不清空队列,也不影响定时调度。
        </Text>
      </div>
    </Card>
  )
}

export default function PoolPipelineMonitor() {
  const qc = useQueryClient()
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['poolStatus'],
    queryFn: api.getPoolStatus,
    refetchInterval: 8000,
  })

  const drainMut = useMutation({
    mutationFn: ({ name, drain }) => (drain ? api.drainPool(name) : api.resumePool(name)),
    onSuccess: (_r, v) => {
      message.success(`「${POOL_LABEL[v.name] || v.name.toUpperCase()}」环节已${v.drain ? '暂停' : '恢复'}`)
      qc.invalidateQueries({ queryKey: ['poolStatus'] })
    },
    onError: (e) => message.error(`操作失败: ${e?.message || e}`),
  })
  const onToggle = (name, drain) => drainMut.mutate({ name, drain })

  if (isLoading) return <Spin tip="加载流水线状态..." style={{ marginTop: 80 }} />

  // ── derived ──────────────────────────────────────────────────────────
  const stuckIntent = data?.stuck_past_lease?.hyp_intent || 0
  const stuckCand = data?.stuck_past_lease?.candidate_queue || 0
  const stuckTotal = stuckIntent + stuckCand
  const workers = data?.workers_count || 0
  const expectedWorkers = data?.expected_workers ?? DEFAULT_WORKERS
  const workersAlive = data?.workers_alive || []
  const expectedByPool = data?.expected_by_pool || {}
  const onlineByPool = countByPrefix(workersAlive)
  const workersHealthy = expectedWorkers > 0 && workers >= expectedWorkers

  const cand = data?.candidate_queue || {}
  const intent = data?.hyp_intent || {}
  const pendingSim = cand.PENDING_SIM || 0
  const simulating = cand.SIMULATING || 0
  const pendingEval = cand.PENDING_EVAL || 0
  const candDone = cand.DONE || 0
  const intentPending = intent.PENDING || 0
  const intentDone = intent.DONE || 0
  const concurrentSims = data?.concurrent_sims ?? 0
  const sWorkers = expectedByPool?.s ?? 0
  const backlogPerSWorker = pendingSim / Math.max(sWorkers, 1)
  const thrCand = data?.throughput_90min?.candidates ?? 0
  const thrAlpha = data?.throughput_90min?.alphas ?? 0
  const alphasPerHour = thrAlpha * (60 / 90)
  const candsPerHour = thrCand * (60 / 90)

  let backlogLevel = 'ok'
  if (pendingSim > BACKLOG_RED) backlogLevel = 'red'
  else if (pendingSim > BACKLOG_YELLOW) backlogLevel = 'yellow'
  const pendingSimColor =
    backlogLevel === 'red' ? '#cf1322' : backlogLevel === 'yellow' ? '#d48806' : '#1677ff'

  // ── Tab 1: 总览 ──────────────────────────────────────────────────────
  const overviewTab = (
    <>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card size="small"><Statistic title="在线工作进程"
          value={workers} suffix={`/ ${expectedWorkers}`}
          valueStyle={{ color: workersHealthy ? '#3f8600' : '#cf1322' }}
          prefix={<ThunderboltOutlined />} />
          <Tooltip title={workersAlive.join(', ') || '无'}>
            <Text type="secondary" style={{ fontSize: 12 }}>{workersAlive.join(', ').slice(0, 40) || '—'}</Text>
          </Tooltip>
        </Card></Col>
        <Col span={6}><Card size="small"><Statistic title="正在回测数（占用并发名额）"
          value={concurrentSims} prefix={<ApiOutlined />} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="今日回测次数（配额）"
          value={data?.budget_sims_today ?? 0} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="今日 token 用量（配额）"
          value={data?.budget_tokens_today ?? 0} /></Card></Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}><Card size="small"><Statistic title="候选数（近 90 分钟）" value={thrCand} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="alpha 产出（近 90 分钟）" value={thrAlpha}
          valueStyle={{ color: thrAlpha > 0 ? '#3f8600' : undefined }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="流水线失败记录数" value={data?.pool_failures_total ?? 0} /></Card></Col>
      </Row>

      <DrainControlCard data={data} onToggle={onToggle} pending={drainMut.isPending} />
    </>
  )

  // ── Tab 2: 队列 ──────────────────────────────────────────────────────
  const queueTab = (
    <>
      {backlogLevel === 'red' && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ 回测产能严重落后于想法生成 — 排队待回测 = ${pendingSim}(> ${BACKLOG_RED}）`}
          description={`每个回测工作进程名下约 ${backlogPerSWorker.toFixed(0)} 个待回测候选(回测工作进程 = ${sWorkers})。想法生成速率远超回测吞吐 → 队列单向膨胀。处置:增加回测工作进程 / 提高回测配额 / 暂停想法生成环节(见「总览」页)给回测追平时间。`} />
      )}
      {backlogLevel === 'yellow' && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`排队待回测 = ${pendingSim}(> ${BACKLOG_YELLOW}）— 回测开始落后于想法生成`}
          description={`每个回测工作进程名下约 ${backlogPerSWorker.toFixed(0)} 个待回测候选(回测工作进程 = ${sWorkers})。关注趋势,若持续上行考虑增加回测工作进程或暂停想法生成。`} />
      )}
      {backlogLevel === 'ok' && (
        <Alert type="success" showIcon style={{ marginBottom: 16 }}
          message={`队列健康 — 排队待回测 = ${pendingSim}(≤ ${BACKLOG_YELLOW}）`}
          description="回测吞吐与想法生成基本匹配,无明显积压。" />
      )}

      <Text type="secondary" style={{ fontSize: 12 }}>候选队列（想法生成 → 回测 → 评估的候选流）</Text>
      <Row gutter={16} style={{ marginBottom: 16, marginTop: 8 }}>
        <Col span={6}><Card><Statistic title="排队待回测" value={pendingSim}
          valueStyle={{ color: pendingSimColor, fontSize: 30 }} prefix={<DatabaseOutlined />} />
          <Text type="secondary" style={{ fontSize: 12 }}>每回测工作进程 ≈ {backlogPerSWorker.toFixed(0)}</Text>
        </Card></Col>
        <Col span={6}><Card><Statistic title="回测中" value={simulating}
          valueStyle={{ fontSize: 30 }} prefix={<ThunderboltOutlined />} />
          <Text type="secondary" style={{ fontSize: 12 }}>占用并发回测名额 {concurrentSims}</Text>
        </Card></Col>
        <Col span={6}><Card><Statistic title="排队待评估" value={pendingEval}
          valueStyle={{ fontSize: 30, color: '#08979c' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="已完成" value={candDone}
          valueStyle={{ fontSize: 30, color: '#3f8600' }} /></Card></Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card><Statistic title="想法队列：待认领" value={intentPending}
          valueStyle={{ fontSize: 30, color: '#1677ff' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="想法队列：已完成" value={intentDone}
          valueStyle={{ fontSize: 30, color: '#3f8600' }} /></Card></Col>
        <Col span={12}>
          <Card title={<span><ThunderboltOutlined /> 待回测 vs 正在回测（产能瓶颈对比）</span>}>
            <Row gutter={16}>
              <Col span={8}><Statistic title="排队待回测" value={pendingSim} valueStyle={{ color: pendingSimColor }} /></Col>
              <Col span={8}><Statistic title="正在回测数" value={concurrentSims} /></Col>
              <Col span={8}>
                <Tooltip title="排队待回测 ÷ max(正在回测数, 1):若极大,表示正在跑的回测相对积压杯水车薪">
                  <Statistic title="积压/在跑 倍数"
                    value={(pendingSim / Math.max(concurrentSims, 1)).toFixed(1)} suffix="x"
                    valueStyle={{ color: pendingSim / Math.max(concurrentSims, 1) > 50 ? '#cf1322' : undefined }} />
                </Tooltip>
              </Col>
            </Row>
            <Text type="secondary" style={{ fontSize: 12 }}>
              今日已用回测次数 {data?.budget_sims_today ?? 0};并发名额有限时,巨量待回测只能排队等待。
            </Text>
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}><Card size="small" title="想法队列（各状态）">
          <StageChips counts={intent} order={INTENT_ORDER} colorMap={INTENT_STAGE_COLOR} />
        </Card></Col>
        <Col span={12}><Card size="small" title="候选队列（各状态）">
          <StageChips counts={cand} order={CAND_ORDER} colorMap={CAND_STAGE_COLOR} />
        </Card></Col>
      </Row>

      <Card size="small" title={<span><FundProjectionScreenOutlined /> 吞吐（近 90 分钟）</span>}>
        <Row gutter={16}>
          <Col span={6}><Statistic title="候选数（近 90 分钟）" value={thrCand} /></Col>
          <Col span={6}><Statistic title="候选/小时" value={candsPerHour.toFixed(1)} suffix="/h" /></Col>
          <Col span={6}><Statistic title="alpha 产出（近 90 分钟）" value={thrAlpha}
            valueStyle={{ color: thrAlpha > 0 ? '#3f8600' : undefined }} /></Col>
          <Col span={6}><Statistic title="alpha/小时" value={alphasPerHour.toFixed(1)} suffix="/h"
            valueStyle={{ color: thrAlpha > 0 ? '#3f8600' : undefined }} /></Col>
        </Row>
      </Card>
    </>
  )

  // ── Tab 3: 工作器 ────────────────────────────────────────────────────
  const workersTab = (
    <>
      <Card size="small" title="各环节在线工作进程（在线数 vs 应有数量）" style={{ marginBottom: 16 }}>
        <Row gutter={16}>
          {POOLS.map((n) => (
            <Col span={8} key={n}>
              <PoolWorkerCard name={n} online={onlineByPool[n] || 0}
                expected={expectedByPool?.[n] || 0} draining={!!data?.drain?.[n]} />
            </Col>
          ))}
        </Row>
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            在线数按进程名前缀(想法生成 / 回测 / 评估)统计;某环节在线 &lt; 应有数量 ⇒ 该环节工作进程缺失或在反复崩溃。
          </Text>
        </div>
      </Card>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}><Card size="small"><Statistic title="想法队列处理超时（应为 0）"
          value={stuckIntent} valueStyle={{ color: stuckIntent > 0 ? '#cf1322' : '#3f8600' }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="候选队列处理超时（应为 0）"
          value={stuckCand} valueStyle={{ color: stuckCand > 0 ? '#cf1322' : '#3f8600' }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="流水线失败记录数（累计）"
          value={data?.pool_failures_total ?? 0} /></Card></Col>
      </Row>

      <DrainControlCard data={data} onToggle={onToggle} pending={drainMut.isPending} />
    </>
  )

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ApiOutlined /> 挖掘流水线（想法生成 / 回测 / 评估）
        </Title>
        <Space>
          {data?.enabled
            ? <Badge status="processing" text="挖掘流水线开关 已开启" />
            : <Badge status="default" text="已关闭" />}
          <Button icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Row>

      {/* 页级健康告警 — 任意 Tab 都常显 */}
      {isError && <Alert type="error" showIcon message="拉取流水线状态失败（接口 /ops/pools/status）。" style={{ marginBottom: 16 }} />}
      {data && !data.enabled && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="挖掘流水线开关已关闭(ENABLE_POOL_PIPELINE)— 流水线未启用(定时任务空转、进程守护空闲、下方队列深度为历史静态值)。需在 .env 设为 true 并重启。" />
      )}
      {stuckTotal > 0 && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ ${stuckTotal} 个任务认领后处理超时未完成(想法队列「已认领」: ${stuckIntent} / 候选队列「回测中·评估中」: ${stuckCand})`}
          description="正常情况下此处应恒为 0。超时回收的定时任务(每 2 分钟)应自动回收;若持续 >0,请检查工作进程是否假死 / 超时时限是否太短 / 心跳是否未续。" />
      )}
      {data?.enabled && workers < expectedWorkers && stuckTotal === 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`进程守护下只有 ${workers}/${expectedWorkers} 个工作进程存活 — 检查进程守护窗口是否在运行 + 是否在反复崩溃重启。`} />
      )}

      <Tabs
        defaultActiveKey="overview"
        items={[
          { key: 'overview', label: <span><ThunderboltOutlined /> 总览</span>, children: overviewTab },
          { key: 'queue', label: <span><DatabaseOutlined /> 队列健康 / 积压{backlogLevel === 'red' ? ' ⚠️' : ''}</span>, children: queueTab },
          { key: 'workers', label: <span><HeartOutlined /> 工作进程与心跳</span>, children: workersTab },
        ]}
      />
    </div>
  )
}
