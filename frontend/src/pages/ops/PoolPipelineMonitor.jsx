import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Col, Popconfirm, Row, Space, Spin, Statistic,
  Tag, Tooltip, Typography, message,
} from 'antd'
import {
  ReloadOutlined, ApiOutlined, ThunderboltOutlined, WarningOutlined,
  PauseCircleOutlined, PlayCircleOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * PoolPipelineMonitor — /ops/pool-pipeline (四池解耦,2026-06-06 cutover).
 *
 * Observes the resident HG/S/E pool: supervisor workers alive, shared BRAIN
 * sim-slot + day budgets, per-pool drain controls, hyp_intent / candidate_queue
 * stage depth, throughput, and the cutover exit criterion — rows stuck
 * SIMULATING/EVALUATING/CLAIMED past their lease (should be 0). Refetches every 8s.
 *
 * Pipeline: scheduler beat → hyp_intent(PENDING) → HG claims → candidate_queue
 * (PENDING_SIM → SIMULATING → PENDING_EVAL → EVALUATING → DONE) → alphas.
 */
const INTENT_STAGE_COLOR = {
  PENDING: 'blue', CLAIMED: 'gold', DONE: 'green', FAILED: 'red', PURGED: 'default',
}
const CAND_STAGE_COLOR = {
  PENDING_SIM: 'blue', SIMULATING: 'gold', PENDING_EVAL: 'cyan', EVALUATING: 'gold',
  DONE: 'green', FAILED: 'red', PURGED: 'default',
}
// canonical stage order so an empty stage still shows a 0 chip
const INTENT_ORDER = ['PENDING', 'CLAIMED', 'DONE', 'FAILED', 'PURGED']
const CAND_ORDER = ['PENDING_SIM', 'SIMULATING', 'PENDING_EVAL', 'EVALUATING', 'DONE', 'FAILED', 'PURGED']
const EXPECTED_WORKERS = 4 // N_HG(1) + K_S(2) + K_E(1)

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

  const stuckIntent = data?.stuck_past_lease?.hyp_intent || 0
  const stuckCand = data?.stuck_past_lease?.candidate_queue || 0
  const stuckTotal = stuckIntent + stuckCand
  const workers = data?.workers_count || 0

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ApiOutlined /> 挖掘池 (HG/S/E 四池流水线)
        </Title>
        <Space>
          {data?.enabled
            ? <Badge status="processing" text="ENABLE_POOL_PIPELINE ON" />
            : <Badge status="default" text="OFF" />}
          <Button icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Row>

      {isError && <Alert type="error" showIcon message="拉取池状态失败(端点 /ops/pools/status)。" style={{ marginBottom: 16 }} />}
      {data && !data.enabled && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="ENABLE_POOL_PIPELINE OFF — 池未启用(beats no-op、supervisor idle)。需 .env 设 true + 重启。" />
      )}
      {stuckTotal > 0 && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ ${stuckTotal} 行卡在 in-flight 超 lease(hyp_intent CLAIMED: ${stuckIntent} / candidate_queue SIMULATING|EVALUATING: ${stuckCand})`}
          description="cutover 退出判据:此处应恒为 0。lease-recycle beat(每 2min)应回收;若持续 >0,查 worker 是否假死 / lease 太短 / 心跳未续。" />
      )}
      {data?.enabled && workers < EXPECTED_WORKERS && stuckTotal === 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`supervisor 仅 ${workers}/${EXPECTED_WORKERS} 个 worker 存活 — 检查 supervisor 窗口是否在跑 + crash-loop backoff。`} />
      )}

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}><Card size="small"><Statistic title="Supervisor worker"
          value={workers} suffix={`/ ${EXPECTED_WORKERS}`}
          valueStyle={{ color: workers >= EXPECTED_WORKERS ? '#3f8600' : '#cf1322' }}
          prefix={<ThunderboltOutlined />} />
          <Tooltip title={(data?.workers_alive || []).join(', ') || '无'}>
            <Text type="secondary" style={{ fontSize: 12 }}>{(data?.workers_alive || []).join(', ').slice(0, 40) || '—'}</Text>
          </Tooltip>
        </Card></Col>
        <Col span={6}><Card size="small"><Statistic title="并发 BRAIN sim (共享槽)"
          value={data?.concurrent_sims ?? 0} prefix={<ApiOutlined />} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="今日 sim 计数 (budget)"
          value={data?.budget_sims_today ?? 0} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="今日 token (budget)"
          value={data?.budget_tokens_today ?? 0} /></Card></Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}>
          <Card size="small" title="hyp_intent(HG 认领源)">
            <StageChips counts={data?.hyp_intent} order={INTENT_ORDER} colorMap={INTENT_STAGE_COLOR} />
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small" title="candidate_queue(HG → S → E)">
            <StageChips counts={data?.candidate_queue} order={CAND_ORDER} colorMap={CAND_STAGE_COLOR} />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}><Card size="small"><Statistic title="候选 (近 90min)" value={data?.throughput_90min?.candidates ?? 0} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="alpha 产出 (近 90min)" value={data?.throughput_90min?.alphas ?? 0}
          valueStyle={{ color: (data?.throughput_90min?.alphas || 0) > 0 ? '#3f8600' : undefined }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="pool 归因失败行 (cand_id)" value={data?.pool_failures_total ?? 0} /></Card></Col>
      </Row>

      <Card size="small" title="池控制 (drain 软停 / resume)">
        <Space size={[12, 12]} wrap>
          {['hg', 's', 'e'].map((n) => (
            <PoolDrainControl key={n} name={n} draining={!!data?.drain?.[n]}
              onToggle={onToggle} pending={drainMut.isPending} />
          ))}
        </Space>
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            drain = 软停(停认领新活,在飞的跑完 / 被 lease-recycle 回收);不 purge 队列、不停 scheduler beat。
          </Text>
        </div>
      </Card>
    </div>
  )
}
