import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Empty,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * OrchestratorMonitor — /ops/orchestrator (Phase 1 Sub-phase 4, 2026-05-29).
 *
 * Mining orchestrator 监控看板。读 GET /ops/orchestrator/status 实时显示
 * flag / 阈值 / pool / 配额 / 7d region PASS rate / 最近 20 个决策。
 * operator 翻 ENABLE_AUTO_ORCHESTRATOR 前需要观察这里(plan §4 前置)。
 *
 * 刷新:30s 轮询(orchestrator 决策频率是事件 + 1h cron,30s 足够)。
 */
function poolUsageColor(used, max) {
  const pct = max > 0 ? used / max : 0
  if (pct >= 0.9) return '#ff4d4f'
  if (pct >= 0.7) return '#faad14'
  return '#52c41a'
}

function quotaTag(quota) {
  if (!quota) return <Tag>未知</Tag>
  if (quota.error) return <Tag color="warning">读取失败</Tag>
  if (quota.over_threshold) {
    return (
      <Tag color="error">
        超阈值 {quota.today_count}/{quota.threshold}
      </Tag>
    )
  }
  return (
    <Tag color="success">
      正常 {quota.today_count}/{quota.threshold}
    </Tag>
  )
}

function sourceTag(source) {
  if (source === 'event') return <Tag color="processing">事件</Tag>
  if (source === 'cron_fallback') return <Tag color="warning">cron 兜底</Tag>
  return <Tag>{source || '-'}</Tag>
}

function launchedByTag(by) {
  if (by === 'orchestrator') return <Tag color="purple">orchestrator</Tag>
  if (by === 'manual') return <Tag>manual</Tag>
  return <Tag>{by || '历史(无标记)'}</Tag>
}

function statusTag(s) {
  const colors = {
    COMPLETED: 'success',
    PAUSED: 'warning',
    STOPPED: 'default',
    EARLY_STOPPED: 'default',
    FAILED: 'error',
    RUNNING: 'processing',
  }
  return <Tag color={colors[s] || 'default'}>{s}</Tag>
}

export default function OrchestratorMonitor() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['orchestrator-status'],
    queryFn: () => api.getOrchestratorStatus(),
    refetchInterval: 30000,
  })

  if (isLoading) return <Spin tip="加载中..." />
  if (error) {
    return (
      <Alert
        type="error"
        message="读取 orchestrator 状态失败"
        description={String(error.message || error)}
      />
    )
  }
  if (!data) return <Empty description="无数据" />

  const th = data.thresholds || {}
  const pool = data.pool || {}
  const regionRates = data.region_pass_rates_7d || {}
  const decisions = data.recent_decisions || []
  const regionRows = Object.entries(regionRates).map(([region, s]) => ({
    region,
    passes: s.passes,
    total: s.total,
    weight: s.weight,
  }))

  return (
    <div>
      <Title level={3}>Mining Orchestrator</Title>

      {!data.enabled && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="ENABLE_AUTO_ORCHESTRATOR=OFF — 当前 orchestrator 不会自动 launch task"
          description={
            <span>
              翻 flag 前置:流水线 ≥48h soak(Phase B.1/B.2/B.3)+ heartbeat-abort
              ≥1 周无 false-positive。看本页 KPI 卡 + 最近决策(cron 1h fallback
              tick 仍会产生 skipped=flag_off 记录)。
            </span>
          }
        />
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card>
            <Statistic
              title="状态"
              value={data.enabled ? '已启用' : '已禁用'}
              valueStyle={{ color: data.enabled ? '#52c41a' : '#8c8c8c' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="orchestrator-launched 在跑"
              value={pool.orchestrator_running ?? 0}
              suffix={`/ ${th.max_running ?? '?'}`}
              valueStyle={{
                color: poolUsageColor(pool.orchestrator_running ?? 0, th.max_running ?? 0),
              }}
            />
            <Progress
              percent={
                th.max_running
                  ? Math.round((100 * (pool.orchestrator_running ?? 0)) / th.max_running)
                  : 0
              }
              size="small"
              showInfo={false}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="今日 launch"
              value={pool.today_orchestrator_launches ?? 0}
              suffix={`/ ${th.daily_limit ?? '?'}`}
              valueStyle={{
                color: poolUsageColor(
                  pool.today_orchestrator_launches ?? 0,
                  th.daily_limit ?? 0
                ),
              }}
            />
            <Progress
              percent={
                th.daily_limit
                  ? Math.round((100 * (pool.today_orchestrator_launches ?? 0)) / th.daily_limit)
                  : 0
              }
              size="small"
              showInfo={false}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="BRAIN 配额"
              valueRender={() => quotaTag(data.quota)}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              limit={data.quota?.limit ?? '?'} threshold={data.quota?.threshold ?? '?'}
            </Text>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={12}>
          <Card title="阈值 / 配置(Q5 + Sub-phase 3 DECIDED)" size="small">
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="max_running">{th.max_running}</Descriptions.Item>
              <Descriptions.Item label="daily_limit">{th.daily_limit}</Descriptions.Item>
              <Descriptions.Item label="short_lived_min">{th.short_lived_min}</Descriptions.Item>
              <Descriptions.Item label="idempotency_min">{th.idempotency_min}</Descriptions.Item>
              <Descriptions.Item label="lookback_days">{th.lookback_days}</Descriptions.Item>
              <Descriptions.Item label="datasets_per_task">{th.datasets_per_task}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
        <Col span={12}>
          <Card
            title={`7d region PASS rate(Beta-Bernoulli posterior)`}
            size="small"
          >
            {regionRows.length === 0 ? (
              <Empty description="7d 无 mining-direct alpha — cold-start 走 finalize 触发 task 的 region" />
            ) : (
              <Table
                size="small"
                pagination={false}
                rowKey="region"
                dataSource={regionRows.sort((a, b) => b.weight - a.weight)}
                columns={[
                  { title: 'Region', dataIndex: 'region', width: 80 },
                  {
                    title: 'PASS / 总',
                    width: 100,
                    render: (_, r) => `${r.passes} / ${r.total}`,
                  },
                  {
                    title: 'weight',
                    render: (_, r) => (
                      <Space>
                        <Progress
                          percent={Math.round(r.weight * 100)}
                          size="small"
                          style={{ width: 100 }}
                        />
                        <Text type="secondary">{r.weight.toFixed(3)}</Text>
                      </Space>
                    ),
                  },
                ]}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Card title="最近 20 个 orchestrator 决策" size="small">
        {decisions.length === 0 ? (
          <Empty description="无决策记录(flag 翻开 + finalize 投递事件后会有记录)" />
        ) : (
          <Table
            size="small"
            pagination={false}
            rowKey="task_id"
            dataSource={decisions}
            columns={[
              { title: 'task_id', dataIndex: 'task_id', width: 80 },
              { title: 'region', dataIndex: 'region', width: 80 },
              {
                title: 'status',
                dataIndex: 'status',
                width: 120,
                render: (s) => statusTag(s),
              },
              {
                title: 'launched_by',
                dataIndex: 'launched_by',
                width: 140,
                render: (b) => launchedByTag(b),
              },
              {
                title: 'source',
                dataIndex: 'processed_source',
                width: 110,
                render: (s) => sourceTag(s),
              },
              {
                title: 'processed_at',
                dataIndex: 'processed_at',
                render: (t) => (t ? new Date(t).toLocaleString() : '-'),
              },
            ]}
          />
        )}
      </Card>
    </div>
  )
}
