import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Col, Popconfirm, Row, Space, Spin, Statistic,
  Tag, Tooltip, Typography, message,
} from 'antd'
import {
  ReloadOutlined, ApiOutlined, ThunderboltOutlined, WarningOutlined,
  PauseCircleOutlined, PlayCircleOutlined, HeartOutlined, DesktopOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * PoolWorkersMonitor — /ops/pool-workers「工作器与心跳健康」(四池解耦,P1).
 *
 * Focused on supervisor liveness + lease health rather than the full pipeline:
 *   - workers_count vs expected_workers (supervisor crash-loop detector)
 *   - per-pool online vs expected_by_pool.{hg,s,e}, grouped by workers_alive prefix
 *   - stuck_past_lease (hyp_intent + candidate_queue) — the cutover exit criterion,
 *     should be恒 0; >0 means lease-recycle owes回收 / a worker假死
 *   - day budgets (sims / tokens) + shared concurrent BRAIN sim slot
 *   - per-pool drain / resume
 * Single source: api.getPoolStatus() → GET /ops/pools/status. Refetches every 8s.
 */
const POOLS = ['hg', 's', 'e']
const POOL_LABEL = { hg: 'HG (假设·生成)', s: 'S (模拟)', e: 'E (评估)' }

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

export default function PoolWorkersMonitor() {
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

  if (isLoading) return <Spin tip="加载工作器状态..." style={{ marginTop: 80 }} />

  const workers = data?.workers_count || 0
  const expected = data?.expected_workers || 0
  const workersAlive = data?.workers_alive || []
  const expectedByPool = data?.expected_by_pool || {}
  const onlineByPool = countByPrefix(workersAlive)
  const workersHealthy = expected > 0 && workers >= expected

  const stuckIntent = data?.stuck_past_lease?.hyp_intent || 0
  const stuckCand = data?.stuck_past_lease?.candidate_queue || 0
  const stuckTotal = stuckIntent + stuckCand

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <HeartOutlined /> 工作器与心跳健康 (HG/S/E supervisor)
        </Title>
        <Space>
          {data?.enabled
            ? <Badge status="processing" text="ENABLE_POOL_PIPELINE ON" />
            : <Badge status="default" text="OFF" />}
          <Button icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Row>

      {isError && (
        <Alert type="error" showIcon style={{ marginBottom: 16 }}
          message="拉取池状态失败(端点 /ops/pools/status)。" />
      )}
      {data && !data.enabled && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="ENABLE_POOL_PIPELINE OFF — 池未启用(supervisor idle、worker 不会拉起)。需 .env 设 true + 重启。" />
      )}

      {/* stuck-past-lease 是退出判据,应恒 0 —— 优先于 worker 缺失告警 */}
      {stuckTotal > 0 && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ ${stuckTotal} 行卡 in-flight 超 lease(hyp_intent: ${stuckIntent} / candidate_queue: ${stuckCand})`}
          description="退出判据应恒为 0:lease-recycle 应回收这些行。若持续 >0,查 worker 假死 / 心跳未续 / lease 太短。" />
      )}
      {data?.enabled && !workersHealthy && (
        <Alert type="error" showIcon style={{ marginBottom: 16 }}
          message={`supervisor 有 worker 未存活(${workers}/${expected})`}
          description="查 supervisor 窗口是否在跑 + crash-loop backoff;worker 反复重启会拉低存活数。" />
      )}

      {/* 总览大卡 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small">
            <Statistic title="存活 worker / 期望"
              value={workers} suffix={`/ ${expected}`}
              valueStyle={{ color: workersHealthy ? '#3f8600' : '#cf1322' }}
              prefix={<ThunderboltOutlined />} />
            <Tooltip title={workersAlive.join(', ') || '无在线 worker'}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {workersAlive.join(', ').slice(0, 48) || '— 无在线 worker'}
              </Text>
            </Tooltip>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="并发 BRAIN sim (共享槽在用)"
              value={data?.concurrent_sims ?? 0} prefix={<ApiOutlined />} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small"><Statistic title="今日 sim 计数 (budget)" value={data?.budget_sims_today ?? 0} /></Card>
        </Col>
        <Col span={6}>
          <Card size="small"><Statistic title="今日 token (budget)" value={data?.budget_tokens_today ?? 0} /></Card>
        </Col>
      </Row>

      {/* 按池在线 vs 期望 */}
      <Card size="small" title="按池在线 worker (workers_alive 前缀分组 vs expected_by_pool)" style={{ marginBottom: 16 }}>
        <Row gutter={16}>
          {POOLS.map((n) => (
            <Col span={8} key={n}>
              <PoolWorkerCard
                name={n}
                online={onlineByPool[n] || 0}
                expected={expectedByPool?.[n] || 0}
                draining={!!data?.drain?.[n]}
              />
            </Col>
          ))}
        </Row>
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            在线数按 workers_alive 名字前缀(hg-/s-/e-)统计;某池在线 &lt; 期望 ⇒ 该池 worker 缺失或在 crash-loop。
          </Text>
        </div>
      </Card>

      {/* lease 健康明细 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Card size="small">
            <Statistic title="hyp_intent 超 lease (应 0)"
              value={stuckIntent}
              valueStyle={{ color: stuckIntent > 0 ? '#cf1322' : '#3f8600' }} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title="candidate_queue 超 lease (应 0)"
              value={stuckCand}
              valueStyle={{ color: stuckCand > 0 ? '#cf1322' : '#3f8600' }} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title="pool 归因失败行 (累计)"
              value={data?.pool_failures_total ?? 0} />
          </Card>
        </Col>
      </Row>

      {/* drain / resume 控件 */}
      <Card size="small" title="池控制 (drain 软停 / resume)">
        <Space size={[12, 12]} wrap>
          {POOLS.map((n) => (
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
