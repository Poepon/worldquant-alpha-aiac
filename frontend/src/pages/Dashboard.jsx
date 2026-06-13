import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
// 池原生重写 (2026-06-07): 三个死区已替换 —
//   ① getActiveTasks 当前任务卡(池常驻 task=ACTIVE→永空)→ 池运行状态卡(getPoolStatus)
//   ② mockPnLData 假折线 → 真实吞吐数字卡(throughput_90min)
//   ③ 硬编码绿灯系统健康 → 真实布尔信号上色
// 保留 live:getSimSlots / SSE live-feed / getDailyStats / getKPIMetrics。
import {
  Row,
  Col,
  Card,
  Statistic,
  Progress,
  Typography,
  Tag,
  List,
  Space,
  Spin,
  Tooltip,
  Alert,
  Badge,
} from 'antd'
import {
  RocketOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  LineChartOutlined,
  ThunderboltOutlined,
  ApiOutlined,
  WarningOutlined,
  DatabaseOutlined,
} from '@ant-design/icons'
import api from '../services/api'
import { formatTime } from '../utils/time'

const { Title, Text } = Typography

// PENDING_SIM 积压告警阈值(HG≫S 严重积压时高亮)
const PENDING_SIM_BACKLOG = 500

export default function Dashboard() {
  const [liveFeed, setLiveFeed] = useState([])

  // Fetch daily stats (池 persister 在写 alpha 计数)
  const { data: dailyStats } = useQuery({
    queryKey: ['dailyStats'],
    queryFn: () => api.getDailyStats(),
    refetchInterval: 30000, // Refresh every 30 seconds
  })

  // Fetch KPI metrics (池 persister 在写)
  const { data: kpi } = useQuery({
    queryKey: ['kpiMetrics'],
    queryFn: () => api.getKPIMetrics(),
    refetchInterval: 30000,
  })

  // Live BRAIN sim-slot concurrency (cross-process Redis counter brain:concurrent_sims)
  const { data: simSlots } = useQuery({
    queryKey: ['simSlots'],
    queryFn: () => api.getSimSlots(),
    refetchInterval: 3000, // near-real-time
  })

  // 池运行状态(唯一池状态端点 /ops/pools/status)
  const {
    data: poolStatus,
    isLoading: poolLoading,
    isError: poolError,
    isSuccess: poolSuccess,
  } = useQuery({
    queryKey: ['poolStatus'],
    queryFn: api.getPoolStatus,
    refetchInterval: 8000, // 池相关页约定 8s
  })

  // Live feed SSE connection (trace_steps 池在写)
  useEffect(() => {
    const eventSource = new EventSource('/api/v1/stats/live-feed')

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data)
      setLiveFeed(prev => [data, ...prev.slice(0, 49)]) // Keep last 50
    }

    eventSource.onerror = () => {
      console.log('SSE connection error, will retry...')
    }

    return () => eventSource.close()
  }, [])

  const stats = dailyStats || { goal: 4, current: 0, success_rate: 0, avg_sharpe: 0 }
  const metrics = kpi || { today_simulations: 0, today_success_rate: 0, today_avg_sharpe: 0, week_total_alphas: 0 }
  const slots = simSlots || { current: 0, limit: 3, available: 3, role: 'USER' }
  const slotPct = slots.limit > 0 ? Math.round((slots.current / slots.limit) * 100) : 0

  const goalPercent = Math.round((stats.current / stats.goal) * 100)

  // ── 池派生信号 ─────────────────────────────────────────────
  const cq = poolStatus?.candidate_queue || {}
  const pendingSim = cq.PENDING_SIM || 0
  const simulating = cq.SIMULATING || 0
  const pendingEval = cq.PENDING_EVAL || 0
  const cqDone = cq.DONE || 0
  const workersCount = poolStatus?.workers_count || 0
  const expectedWorkers = poolStatus?.expected_workers || 0
  const stuckIntent = poolStatus?.stuck_past_lease?.hyp_intent || 0
  const stuckCand = poolStatus?.stuck_past_lease?.candidate_queue || 0
  const stuckTotal = stuckIntent + stuckCand
  const tp = poolStatus?.throughput_90min || {}
  const tpAlphas = tp.alphas || 0
  const tpCandidates = tp.candidates || 0

  const poolEnabled = !!poolStatus?.enabled
  // worker 健康:期望数已知且实际 >= 期望(期望为 0 时不下判断)
  const workersHealthy = expectedWorkers > 0 && workersCount >= expectedWorkers
  const noStuck = stuckTotal === 0
  const backlogged = pendingSim > PENDING_SIM_BACKLOG

  // 健康灯统一渲染助手:真实布尔上色,不硬编码 success
  const HealthRow = ({ label, ok, okText, badText, neutral, neutralText, sub }) => {
    let color = 'success'
    let icon = <CheckCircleOutlined />
    let text = okText
    if (neutral) {
      color = 'default'
      icon = <ClockCircleOutlined />
      text = neutralText
    } else if (!ok) {
      color = 'error'
      icon = <CloseCircleOutlined />
      text = badText
    }
    return (
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text>
          {label}
          {sub && (
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>{sub}</Text>
          )}
        </Text>
        <Tag color={color} icon={icon}>{text}</Tag>
      </div>
    )
  }

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>
            <RocketOutlined style={{ marginRight: 12, color: '#00d4ff' }} />
            仪表盘
          </Title>
        </Col>
      </Row>

      {/* Top Row: Goal + Pool Status + System Health */}
      <Row gutter={[16, 16]}>
        {/* Daily Goal Card (live: getDailyStats) */}
        <Col xs={24} sm={12} lg={8}>
          <Card className="glass-card">
            <div style={{ textAlign: 'center' }}>
              <Text type="secondary">今日挖掘目标</Text>
              <div style={{ margin: '16px 0' }}>
                <Progress
                  type="circle"
                  percent={goalPercent}
                  format={() => `${stats.current}/${stats.goal}`}
                  strokeColor="#00d4ff"
                  trailColor="rgba(255,255,255,0.1)"
                  size={120}
                />
              </div>
              <Text style={{ color: '#00ff88' }}>
                {stats.current} 个今日新 Alpha
              </Text>
            </div>
          </Card>
        </Col>

        {/* Pool Status Card — 替换原「当前任务状态」(getActiveTasks 池下永空) */}
        <Col xs={24} sm={12} lg={8}>
          <Card className="glass-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Text type="secondary">挖掘流水线状态（想法生成 / 回测 / 评估）</Text>
              {poolStatus && (
                poolEnabled
                  ? <Badge status="processing" text="运行中" />
                  : <Badge status="default" text="已停用" />
              )}
            </div>
            {poolLoading ? (
              <div style={{ textAlign: 'center', padding: 40 }}>
                <Spin />
              </div>
            ) : poolError ? (
              <div style={{ textAlign: 'center', padding: 32 }}>
                <Text type="secondary">无法获取流水线状态</Text>
              </div>
            ) : (
              <div style={{ marginTop: 16 }}>
                <Space direction="vertical" style={{ width: '100%' }} size={10}>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Text type="secondary">常驻工作进程</Text>
                    <Tag color={workersHealthy ? 'processing' : 'warning'} icon={<ThunderboltOutlined />}>
                      {workersCount} / {expectedWorkers || '?'}
                    </Tag>
                  </div>
                  <Row gutter={[8, 8]}>
                    <Col span={12}>
                      <Tooltip title="候选队列中排队待回测（想法生成已产出，等回测环节处理）">
                        <Tag color={backlogged ? 'red' : pendingSim > 0 ? 'blue' : 'default'} style={{ width: '100%', textAlign: 'center', margin: 0 }}>
                          待回测 <b>{pendingSim}</b>
                        </Tag>
                      </Tooltip>
                    </Col>
                    <Col span={12}>
                      <Tooltip title="候选队列回测中（回测环节正在处理）">
                        <Tag color={simulating > 0 ? 'gold' : 'default'} style={{ width: '100%', textAlign: 'center', margin: 0 }}>
                          回测中 <b>{simulating}</b>
                        </Tag>
                      </Tooltip>
                    </Col>
                    <Col span={12}>
                      <Tooltip title="候选队列中排队待评估（等评估环节处理）">
                        <Tag color={pendingEval > 0 ? 'cyan' : 'default'} style={{ width: '100%', textAlign: 'center', margin: 0 }}>
                          待评估 <b>{pendingEval}</b>
                        </Tag>
                      </Tooltip>
                    </Col>
                    <Col span={12}>
                      <Tooltip title="候选队列已完成（累计）">
                        <Tag color={cqDone > 0 ? 'green' : 'default'} style={{ width: '100%', textAlign: 'center', margin: 0 }}>
                          已完成 <b>{cqDone}</b>
                        </Tag>
                      </Tooltip>
                    </Col>
                  </Row>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Text type="secondary">近 90 分钟产出</Text>
                    <Text>
                      <Text style={{ color: '#9c88ff' }}>{tpCandidates}</Text> 候选 /{' '}
                      <Text style={{ color: tpAlphas > 0 ? '#00ff88' : undefined }}>{tpAlphas}</Text> alpha
                    </Text>
                  </div>
                  {backlogged && (
                    <Alert
                      type="warning"
                      showIcon
                      icon={<WarningOutlined />}
                      style={{ padding: '4px 8px' }}
                      message={
                        <Text style={{ fontSize: 12 }}>
                          待回测积压 {pendingSim}：回测环节消费跟不上想法生成的产出
                        </Text>
                      }
                    />
                  )}
                  {!poolEnabled && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      挖掘流水线未启用
                    </Text>
                  )}
                </Space>
              </div>
            )}
          </Card>
        </Col>

        {/* System Health Card — 真实布尔信号上色,不再硬编码 success */}
        <Col xs={24} sm={24} lg={8}>
          <Card className="glass-card">
            <Text type="secondary">系统健康状态</Text>
            <div style={{ marginTop: 16 }}>
              <Space direction="vertical" style={{ width: '100%' }} size={10}>
                {/* 池开关:enabled=ON 绿,OFF 灰中性(关闭非故障) */}
                <HealthRow
                  label="挖掘流水线"
                  ok={poolEnabled}
                  neutral={poolSuccess && !poolEnabled}
                  okText="已启用"
                  neutralText="未启用"
                  badText="未启用"
                />
                {/* worker 健康:实际 >= 期望 */}
                <HealthRow
                  label="常驻工作进程"
                  ok={workersHealthy}
                  neutral={poolSuccess && expectedWorkers === 0}
                  okText={`${workersCount}/${expectedWorkers} 健康`}
                  neutralText="无应有数量基线"
                  badText={`${workersCount}/${expectedWorkers || '?'} 缺失`}
                  sub={poolSuccess ? undefined : '加载中'}
                />
                {/* 无卡死:stuck_past_lease 合计==0 */}
                <HealthRow
                  label="队列处理超时"
                  ok={poolSuccess ? noStuck : true}
                  okText={poolSuccess ? '无超时' : '—'}
                  badText={`${stuckTotal} 行超时卡住`}
                />
                {/* DB:请求成功隐性证明(任一池/stats 请求 200 即在线) */}
                <HealthRow
                  label="数据库"
                  ok={poolSuccess || !!dailyStats}
                  okText="在线(请求成功隐性证明)"
                  badText="无成功请求"
                  neutral={!poolSuccess && !dailyStats}
                  neutralText="等待请求"
                />
                {/* BRAIN 模拟并发槽(live: getSimSlots) */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <Text>
                    BRAIN 并发回测名额{' '}
                    <Text type="secondary" style={{ fontSize: 12 }}>({slots.role === 'USER' ? '普通用户' : slots.role === 'CONSULTANT' ? '顾问' : slots.role})</Text>
                  </Text>
                  <Tag color={slots.current >= slots.limit ? 'error' : slots.current > 0 ? 'processing' : 'default'}>
                    {slots.current} / {slots.limit}
                  </Tag>
                </div>
                <Progress
                  percent={slotPct}
                  size="small"
                  status={slots.current >= slots.limit ? 'exception' : 'active'}
                  strokeColor={slots.current >= slots.limit ? '#ff4d4f' : '#00d4ff'}
                  trailColor="rgba(255,255,255,0.1)"
                  format={() => `${slots.current}/${slots.limit}`}
                />
              </Space>
            </div>
          </Card>
        </Col>
      </Row>

      {/* Second Row: KPI Cards (live: getKPIMetrics) */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="今日回测次数"
              value={metrics.today_simulations}
              prefix={<ClockCircleOutlined />}
              valueStyle={{ color: '#00d4ff' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="成功率"
              value={metrics.today_success_rate * 100}
              precision={1}
              suffix="%"
              valueStyle={{ color: '#00ff88' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="平均夏普比率"
              value={metrics.today_avg_sharpe}
              precision={2}
              prefix={<LineChartOutlined />}
              valueStyle={{ color: '#ffb700' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="本周 Alpha 总数"
              value={metrics.week_total_alphas}
              prefix={<CheckCircleOutlined />}
              valueStyle={{ color: '#9c88ff' }}
            />
          </Card>
        </Col>
      </Row>

      {/* Third Row: Live Feed + 池吞吐真实数字(替换 mock 折线) */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        {/* Live Activity Feed (live: SSE) */}
        <Col xs={24} lg={12}>
          <Card
            className="glass-card"
            title="实时活动动态"
            style={{ height: 400 }}
          >
            <div style={{ height: 320, overflow: 'auto' }}>
              <List
                size="small"
                dataSource={liveFeed.length > 0 ? liveFeed : [
                  { message: '⏳ 等待活动...', timestamp: new Date().toISOString() }
                ]}
                renderItem={(item) => (
                  <List.Item className="feed-item" style={{
                    padding: '8px 0',
                    borderBottom: '1px solid rgba(255,255,255,0.05)',
                  }}>
                    <Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {formatTime(item.timestamp)}
                      </Text>
                      <Text>{item.message}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Card>
        </Col>

        {/* 池吞吐 — 真实数字卡(替换硬编码 mockPnLData 折线图) */}
        <Col xs={24} lg={12}>
          <Card
            className="glass-card"
            title={<span><ApiOutlined style={{ marginRight: 8 }} />流水线吞吐与队列概览</span>}
            style={{ height: 400 }}
          >
            {poolLoading ? (
              <div style={{ textAlign: 'center', padding: 80 }}>
                <Spin />
              </div>
            ) : poolError ? (
              <Alert
                type="error"
                showIcon
                message="无法获取流水线吞吐数据"
                description="流水线状态请求失败。"
              />
            ) : (
              <div style={{ height: 320, overflow: 'auto' }}>
                <Row gutter={[12, 12]}>
                  <Col xs={12}>
                    <Card size="small" bordered={false} style={{ background: 'rgba(255,255,255,0.03)' }}>
                      <Statistic
                        title="alpha 产出（近 90 分钟）"
                        value={tpAlphas}
                        valueStyle={{ color: tpAlphas > 0 ? '#00ff88' : '#888' }}
                        prefix={<RocketOutlined />}
                      />
                    </Card>
                  </Col>
                  <Col xs={12}>
                    <Card size="small" bordered={false} style={{ background: 'rgba(255,255,255,0.03)' }}>
                      <Statistic
                        title="候选产出（近 90 分钟）"
                        value={tpCandidates}
                        valueStyle={{ color: '#9c88ff' }}
                        prefix={<ThunderboltOutlined />}
                      />
                    </Card>
                  </Col>
                  <Col xs={12}>
                    <Card size="small" bordered={false} style={{ background: 'rgba(255,255,255,0.03)' }}>
                      <Statistic
                        title="排队待回测"
                        value={pendingSim}
                        valueStyle={{ color: backlogged ? '#ff4d4f' : '#00d4ff' }}
                        prefix={<ClockCircleOutlined />}
                      />
                    </Card>
                  </Col>
                  <Col xs={12}>
                    <Card size="small" bordered={false} style={{ background: 'rgba(255,255,255,0.03)' }}>
                      <Statistic
                        title="排队待评估"
                        value={pendingEval}
                        valueStyle={{ color: '#13c2c2' }}
                        prefix={<LineChartOutlined />}
                      />
                    </Card>
                  </Col>
                  <Col xs={12}>
                    <Card size="small" bordered={false} style={{ background: 'rgba(255,255,255,0.03)' }}>
                      <Statistic
                        title="队列已完成"
                        value={cqDone}
                        valueStyle={{ color: '#52c41a' }}
                        prefix={<CheckCircleOutlined />}
                      />
                    </Card>
                  </Col>
                  <Col xs={12}>
                    <Card size="small" bordered={false} style={{ background: 'rgba(255,255,255,0.03)' }}>
                      <Statistic
                        title="今日回测次数"
                        value={poolStatus?.budget_sims_today ?? 0}
                        valueStyle={{ color: '#ffb700' }}
                        prefix={<DatabaseOutlined />}
                      />
                    </Card>
                  </Col>
                </Row>
                {backlogged && (
                  <Alert
                    type="warning"
                    showIcon
                    icon={<WarningOutlined />}
                    style={{ marginTop: 12 }}
                    message={`待回测积压 ${pendingSim} 超 ${PENDING_SIM_BACKLOG}`}
                    description="想法生成产出远快于回测环节消费，回测侧瓶颈。"
                  />
                )}
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
                  注：暂无每日 alpha 产出的时间序列数据，此处展示实时 / 近况数字而非折线图。
                </Text>
              </div>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
