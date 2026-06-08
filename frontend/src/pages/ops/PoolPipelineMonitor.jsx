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
const POOL_LABEL = { hg: 'HG (假设·生成)', s: 'S (模拟)', e: 'E (评估)' }
// PENDING_SIM 积压阈值
const BACKLOG_RED = 500
const BACKLOG_YELLOW = 100

function StageChips({ counts, order, colorMap }) {
  return (
    <Space size={[8, 8]} wrap>
      {order.map((s) => (
        <Tag key={s} color={(counts?.[s] || 0) > 0 ? colorMap[s] : 'default'}>
          {s}: <b>{counts?.[s] || 0}</b>
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
  return draining ? (
    <Popconfirm title={`恢复 ${name.toUpperCase()} 池?`} onConfirm={() => onToggle(name, false)}>
      <Button size="small" danger icon={<PauseCircleOutlined />} loading={pending}>
        {name.toUpperCase()} 已暂停 — 恢复
      </Button>
    </Popconfirm>
  ) : (
    <Popconfirm
      title={`暂停 ${name.toUpperCase()} 池?(软停:停认领新活,在飞的跑完)`}
      onConfirm={() => onToggle(name, true)}
    >
      <Button size="small" icon={<PlayCircleOutlined />} loading={pending}>
        {name.toUpperCase()} 运行中 — 暂停
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
            {draining && <Tag color="orange" style={{ marginLeft: 4 }}>drain</Tag>}
          </Space>
        )}
        value={online}
        suffix={`/ ${expected} 期望`}
        valueStyle={{ color }}
      />
      {!healthy && expected > 0 && (
        <Text type="danger" style={{ fontSize: 12 }}>缺 {expected - online} 个 worker</Text>
      )}
    </Card>
  )
}

function DrainControlCard({ data, onToggle, pending }) {
  return (
    <Card size="small" title="池控制 (drain 软停 / resume)">
      <Space size={[12, 12]} wrap>
        {POOLS.map((n) => (
          <PoolDrainControl key={n} name={n} draining={!!data?.drain?.[n]}
            onToggle={onToggle} pending={pending} />
        ))}
      </Space>
      <div style={{ marginTop: 8 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          drain = 软停(停认领新活,在飞的跑完 / 被 lease-recycle 回收);不 purge 队列、不停 scheduler beat。
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
      message.success(`${v.name.toUpperCase()} 池已${v.drain ? '暂停' : '恢复'}`)
      qc.invalidateQueries({ queryKey: ['poolStatus'] })
    },
    onError: (e) => message.error(`操作失败: ${e?.message || e}`),
  })
  const onToggle = (name, drain) => drainMut.mutate({ name, drain })

  if (isLoading) return <Spin tip="加载池状态..." style={{ marginTop: 80 }} />

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
        <Col span={6}><Card size="small"><Statistic title="Supervisor worker"
          value={workers} suffix={`/ ${expectedWorkers}`}
          valueStyle={{ color: workersHealthy ? '#3f8600' : '#cf1322' }}
          prefix={<ThunderboltOutlined />} />
          <Tooltip title={workersAlive.join(', ') || '无'}>
            <Text type="secondary" style={{ fontSize: 12 }}>{workersAlive.join(', ').slice(0, 40) || '—'}</Text>
          </Tooltip>
        </Card></Col>
        <Col span={6}><Card size="small"><Statistic title="并发 BRAIN sim (共享槽)"
          value={concurrentSims} prefix={<ApiOutlined />} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="今日 sim 计数 (budget)"
          value={data?.budget_sims_today ?? 0} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="今日 token (budget)"
          value={data?.budget_tokens_today ?? 0} /></Card></Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}><Card size="small"><Statistic title="候选 (近 90min)" value={thrCand} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="alpha 产出 (近 90min)" value={thrAlpha}
          valueStyle={{ color: thrAlpha > 0 ? '#3f8600' : undefined }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="pool 归因失败行 (cand_id)" value={data?.pool_failures_total ?? 0} /></Card></Col>
      </Row>

      <DrainControlCard data={data} onToggle={onToggle} pending={drainMut.isPending} />
    </>
  )

  // ── Tab 2: 队列 ──────────────────────────────────────────────────────
  const queueTab = (
    <>
      {backlogLevel === 'red' && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ S 模拟产能严重落后于 HG 生成 — candidate_queue.PENDING_SIM = ${pendingSim} (> ${BACKLOG_RED})`}
          description={`每个 S worker 名下约 ${backlogPerSWorker.toFixed(0)} 个待模拟候选(S worker = ${sWorkers})。HG 生成速率远超 S 模拟吞吐 → 队列单向膨胀。处置:扩 K_S / 提 sim 配额 / 软停 HG 池(见「总览」Tab)给 S 追平时间。`} />
      )}
      {backlogLevel === 'yellow' && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`PENDING_SIM = ${pendingSim} (> ${BACKLOG_YELLOW}) — S 模拟开始落后于 HG 生成`}
          description={`每个 S worker 名下约 ${backlogPerSWorker.toFixed(0)} 个待模拟候选(S worker = ${sWorkers})。关注趋势,若持续上行考虑扩 K_S 或软停 HG。`} />
      )}
      {backlogLevel === 'ok' && (
        <Alert type="success" showIcon style={{ marginBottom: 16 }}
          message={`队列健康 — PENDING_SIM = ${pendingSim} (≤ ${BACKLOG_YELLOW})`}
          description="S 模拟吞吐与 HG 生成基本匹配,无明显积压。" />
      )}

      <Text type="secondary" style={{ fontSize: 12 }}>candidate_queue(HG → S → E 的候选流)</Text>
      <Row gutter={16} style={{ marginBottom: 16, marginTop: 8 }}>
        <Col span={6}><Card><Statistic title="PENDING_SIM(待模拟)" value={pendingSim}
          valueStyle={{ color: pendingSimColor, fontSize: 30 }} prefix={<DatabaseOutlined />} />
          <Text type="secondary" style={{ fontSize: 12 }}>每 S worker ≈ {backlogPerSWorker.toFixed(0)}</Text>
        </Card></Col>
        <Col span={6}><Card><Statistic title="SIMULATING(在飞模拟)" value={simulating}
          valueStyle={{ fontSize: 30 }} prefix={<ThunderboltOutlined />} />
          <Text type="secondary" style={{ fontSize: 12 }}>占共享 sim 槽 {concurrentSims}</Text>
        </Card></Col>
        <Col span={6}><Card><Statistic title="PENDING_EVAL(待评估)" value={pendingEval}
          valueStyle={{ fontSize: 30, color: '#08979c' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="DONE(已完成)" value={candDone}
          valueStyle={{ fontSize: 30, color: '#3f8600' }} /></Card></Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card><Statistic title="hyp_intent PENDING(待 HG 认领)" value={intentPending}
          valueStyle={{ fontSize: 30, color: '#1677ff' }} /></Card></Col>
        <Col span={6}><Card><Statistic title="hyp_intent DONE" value={intentDone}
          valueStyle={{ fontSize: 30, color: '#3f8600' }} /></Card></Col>
        <Col span={12}>
          <Card title={<span><ThunderboltOutlined /> 待模拟 vs 在用 sim 槽(产能瓶颈对比)</span>}>
            <Row gutter={16}>
              <Col span={8}><Statistic title="PENDING_SIM" value={pendingSim} valueStyle={{ color: pendingSimColor }} /></Col>
              <Col span={8}><Statistic title="concurrent_sims" value={concurrentSims} /></Col>
              <Col span={8}>
                <Tooltip title="PENDING_SIM ÷ max(concurrent_sims, 1):若极大,表示在用槽相对积压杯水车薪">
                  <Statistic title="积压/在用 倍数"
                    value={(pendingSim / Math.max(concurrentSims, 1)).toFixed(1)} suffix="x"
                    valueStyle={{ color: pendingSim / Math.max(concurrentSims, 1) > 50 ? '#cf1322' : undefined }} />
                </Tooltip>
              </Col>
            </Row>
            <Text type="secondary" style={{ fontSize: 12 }}>
              共享 sim 槽今日计数 {data?.budget_sims_today ?? 0};槽位有限时巨量 PENDING_SIM 只能排队等待。
            </Text>
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}><Card size="small" title="hyp_intent(全 stage)">
          <StageChips counts={intent} order={INTENT_ORDER} colorMap={INTENT_STAGE_COLOR} />
        </Card></Col>
        <Col span={12}><Card size="small" title="candidate_queue(全 stage)">
          <StageChips counts={cand} order={CAND_ORDER} colorMap={CAND_STAGE_COLOR} />
        </Card></Col>
      </Row>

      <Card size="small" title={<span><FundProjectionScreenOutlined /> 吞吐(近 90min)</span>}>
        <Row gutter={16}>
          <Col span={6}><Statistic title="候选数 (近 90min)" value={thrCand} /></Col>
          <Col span={6}><Statistic title="候选/小时" value={candsPerHour.toFixed(1)} suffix="/h" /></Col>
          <Col span={6}><Statistic title="alpha 产出 (近 90min)" value={thrAlpha}
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
      <Card size="small" title="按池在线 worker (workers_alive 前缀分组 vs expected_by_pool)" style={{ marginBottom: 16 }}>
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
            在线数按 workers_alive 名字前缀(hg-/s-/e-)统计;某池在线 &lt; 期望 ⇒ 该池 worker 缺失或在 crash-loop。
          </Text>
        </div>
      </Card>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}><Card size="small"><Statistic title="hyp_intent 超 lease (应 0)"
          value={stuckIntent} valueStyle={{ color: stuckIntent > 0 ? '#cf1322' : '#3f8600' }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="candidate_queue 超 lease (应 0)"
          value={stuckCand} valueStyle={{ color: stuckCand > 0 ? '#cf1322' : '#3f8600' }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="pool 归因失败行 (累计)"
          value={data?.pool_failures_total ?? 0} /></Card></Col>
      </Row>

      <DrainControlCard data={data} onToggle={onToggle} pending={drainMut.isPending} />
    </>
  )

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ApiOutlined /> 挖掘池 (HG/S/E)
        </Title>
        <Space>
          {data?.enabled
            ? <Badge status="processing" text="ENABLE_POOL_PIPELINE ON" />
            : <Badge status="default" text="OFF" />}
          <Button icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Row>

      {/* 页级健康告警 — 任意 Tab 都常显 */}
      {isError && <Alert type="error" showIcon message="拉取池状态失败(端点 /ops/pools/status)。" style={{ marginBottom: 16 }} />}
      {data && !data.enabled && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="ENABLE_POOL_PIPELINE OFF — 池未启用(beats no-op、supervisor idle、队列深度为静态历史值)。需 .env 设 true + 重启。" />
      )}
      {stuckTotal > 0 && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ ${stuckTotal} 行卡在 in-flight 超 lease(hyp_intent CLAIMED: ${stuckIntent} / candidate_queue SIMULATING|EVALUATING: ${stuckCand})`}
          description="cutover 退出判据:此处应恒为 0。lease-recycle beat(每 2min)应回收;若持续 >0,查 worker 是否假死 / lease 太短 / 心跳未续。" />
      )}
      {data?.enabled && workers < expectedWorkers && stuckTotal === 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`supervisor 仅 ${workers}/${expectedWorkers} 个 worker 存活 — 检查 supervisor 窗口是否在跑 + crash-loop backoff。`} />
      )}

      <Tabs
        defaultActiveKey="overview"
        items={[
          { key: 'overview', label: <span><ThunderboltOutlined /> 总览</span>, children: overviewTab },
          { key: 'queue', label: <span><DatabaseOutlined /> 队列健康 / 积压{backlogLevel === 'red' ? ' ⚠️' : ''}</span>, children: queueTab },
          { key: 'workers', label: <span><HeartOutlined /> 工作器与心跳</span>, children: workersTab },
        ]}
      />
    </div>
  )
}
