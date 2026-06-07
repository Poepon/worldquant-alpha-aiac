import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Alert, Badge, Button, Card, Col, Row, Space, Spin, Statistic,
  Tag, Tooltip, Typography,
} from 'antd'
import {
  ReloadOutlined, ApiOutlined, WarningOutlined, DatabaseOutlined,
  ThunderboltOutlined, FundProjectionScreenOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * PoolQueueMonitor — /ops/pool-queue「队列健康与积压告警」.
 *
 * 直击 PENDING_SIM 积压痛点(基线 2026-06-07 ≈3533):把 candidate_queue /
 * hyp_intent 的队列深度,以及「HG 生成灌爆 S 模拟」的产能错配在一屏可视化告警。
 * 数据全部来自唯一池状态端点 api.getPoolStatus()(GET /ops/pools/status)。
 * drain/resume 控件不在本页(见 /ops/pool-pipeline),只读监控。每 8s 刷新。
 */

// canonical stage order so an empty stage still shows a 0 chip
const INTENT_ORDER = ['PENDING', 'CLAIMED', 'DONE', 'FAILED', 'PURGED']
const CAND_ORDER = ['PENDING_SIM', 'SIMULATING', 'PENDING_EVAL', 'EVALUATING', 'DONE', 'FAILED', 'PURGED']

const INTENT_STAGE_COLOR = {
  PENDING: 'blue', CLAIMED: 'gold', DONE: 'green', FAILED: 'red', PURGED: 'default',
}
const CAND_STAGE_COLOR = {
  PENDING_SIM: 'blue', SIMULATING: 'gold', PENDING_EVAL: 'cyan', EVALUATING: 'gold',
  DONE: 'green', FAILED: 'red', PURGED: 'default',
}

// 积压阈值(每条说明见 Alert 文案)
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

export default function PoolQueueMonitor() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['poolStatus'],
    queryFn: api.getPoolStatus,
    refetchInterval: 8000,
  })

  if (isLoading) return <Spin tip="加载队列状态..." style={{ marginTop: 80 }} />

  const cand = data?.candidate_queue || {}
  const intent = data?.hyp_intent || {}
  const pendingSim = cand.PENDING_SIM || 0
  const simulating = cand.SIMULATING || 0
  const pendingEval = cand.PENDING_EVAL || 0
  const candDone = cand.DONE || 0
  const intentPending = intent.PENDING || 0
  const intentDone = intent.DONE || 0

  const concurrentSims = data?.concurrent_sims ?? 0
  const sWorkers = data?.expected_by_pool?.s ?? 0
  // 每个 S worker 名下待处理的模拟量(产能错配比)
  const backlogPerSWorker = pendingSim / Math.max(sWorkers, 1)

  const thrCand = data?.throughput_90min?.candidates ?? 0
  const thrAlpha = data?.throughput_90min?.alphas ?? 0
  // 近 90min → 每小时(×60/90)
  const alphasPerHour = (thrAlpha * (60 / 90))
  const candsPerHour = (thrCand * (60 / 90))

  let backlogLevel = 'ok'
  if (pendingSim > BACKLOG_RED) backlogLevel = 'red'
  else if (pendingSim > BACKLOG_YELLOW) backlogLevel = 'yellow'

  const pendingSimColor =
    backlogLevel === 'red' ? '#cf1322' : backlogLevel === 'yellow' ? '#d48806' : '#1677ff'

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <DatabaseOutlined /> 队列健康与积压告警
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
          message="拉取池状态失败(端点 /ops/pools/status)。"
          description={error?.message || '请检查后端 / 网络。'} />
      )}
      {data && !data.enabled && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="ENABLE_POOL_PIPELINE OFF — 池未启用(beats no-op、supervisor idle)。队列深度为静态历史值,需 .env 设 true + 重启。" />
      )}

      {/* 积压告警 — 核心 */}
      {backlogLevel === 'red' && (
        <Alert type="error" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
          message={`⚠️ S 模拟产能严重落后于 HG 生成 — candidate_queue.PENDING_SIM = ${pendingSim} (> ${BACKLOG_RED})`}
          description={`每个 S worker 名下约 ${backlogPerSWorker.toFixed(0)} 个待模拟候选(S worker = ${sWorkers})。HG 生成速率远超 S 模拟吞吐 → 队列单向膨胀。处置:扩 K_S / 提 sim 配额 / 软停 HG 池(见挖掘池页)给 S 追平时间。`} />
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

      {/* candidate_queue 大号 Statistic 卡 */}
      <Text type="secondary" style={{ fontSize: 12 }}>candidate_queue(HG → S → E 的候选流)</Text>
      <Row gutter={16} style={{ marginBottom: 16, marginTop: 8 }}>
        <Col span={6}>
          <Card>
            <Statistic title="PENDING_SIM(待模拟)" value={pendingSim}
              valueStyle={{ color: pendingSimColor, fontSize: 30 }}
              prefix={<DatabaseOutlined />} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              每 S worker ≈ {backlogPerSWorker.toFixed(0)}
            </Text>
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="SIMULATING(在飞模拟)" value={simulating}
              valueStyle={{ fontSize: 30 }} prefix={<ThunderboltOutlined />} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              占共享 sim 槽 {concurrentSims}
            </Text>
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="PENDING_EVAL(待评估)" value={pendingEval}
              valueStyle={{ fontSize: 30, color: '#08979c' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="DONE(已完成)" value={candDone}
              valueStyle={{ fontSize: 30, color: '#3f8600' }} />
          </Card>
        </Col>
      </Row>

      {/* hyp_intent 大号卡 + PENDING_SIM vs concurrent_sims 对比 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card>
            <Statistic title="hyp_intent PENDING(待 HG 认领)" value={intentPending}
              valueStyle={{ fontSize: 30, color: '#1677ff' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="hyp_intent DONE" value={intentDone}
              valueStyle={{ fontSize: 30, color: '#3f8600' }} />
          </Card>
        </Col>
        <Col span={12}>
          <Card title={
            <span><ThunderboltOutlined /> 待模拟 vs 在用 sim 槽(产能瓶颈对比)</span>
          }>
            <Row gutter={16}>
              <Col span={8}>
                <Statistic title="PENDING_SIM" value={pendingSim}
                  valueStyle={{ color: pendingSimColor }} />
              </Col>
              <Col span={8}>
                <Statistic title="concurrent_sims" value={concurrentSims} />
              </Col>
              <Col span={8}>
                <Tooltip title="PENDING_SIM ÷ max(concurrent_sims, 1):若极大,表示在用槽相对积压杯水车薪">
                  <Statistic title="积压/在用 倍数"
                    value={(pendingSim / Math.max(concurrentSims, 1)).toFixed(1)}
                    suffix="x"
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

      {/* 全 stage chips */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}>
          <Card size="small" title="hyp_intent(全 stage)">
            <StageChips counts={intent} order={INTENT_ORDER} colorMap={INTENT_STAGE_COLOR} />
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small" title="candidate_queue(全 stage)">
            <StageChips counts={cand} order={CAND_ORDER} colorMap={CAND_STAGE_COLOR} />
          </Card>
        </Col>
      </Row>

      {/* throughput */}
      <Card size="small" title={<span><FundProjectionScreenOutlined /> 吞吐(近 90min)</span>}
        style={{ marginBottom: 16 }}>
        <Row gutter={16}>
          <Col span={6}>
            <Statistic title="候选数 (近 90min)" value={thrCand} />
          </Col>
          <Col span={6}>
            <Statistic title="候选/小时" value={candsPerHour.toFixed(1)} suffix="/h" />
          </Col>
          <Col span={6}>
            <Statistic title="alpha 产出 (近 90min)" value={thrAlpha}
              valueStyle={{ color: thrAlpha > 0 ? '#3f8600' : undefined }} />
          </Col>
          <Col span={6}>
            <Statistic title="alpha/小时" value={alphasPerHour.toFixed(1)} suffix="/h"
              valueStyle={{ color: thrAlpha > 0 ? '#3f8600' : undefined }} />
          </Col>
        </Row>
      </Card>

      {/* 池暂停/恢复指引 — 不重复 drain 控件 */}
      <Card size="small">
        <Space>
          <ApiOutlined />
          <Text type="secondary">
            本页只读监控,不含池暂停/恢复(drain)控件 —— 池暂停/恢复见
          </Text>
          <Link to="/ops/pool-pipeline">挖掘池页 (/ops/pool-pipeline)</Link>
        </Space>
      </Card>
    </div>
  )
}
