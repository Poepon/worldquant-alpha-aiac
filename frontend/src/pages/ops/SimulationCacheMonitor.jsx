import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Card,
  Col,
  Empty,
  InputNumber,
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
  DatabaseOutlined,
  InfoCircleOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text } = Typography

export default function SimulationCacheMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['ops/r9/cache-stats', days],
    queryFn: () => api.getOpsR9CacheStats(days),
    refetchInterval: 60_000,
  })

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
        message="加载 R9 cache stats 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 R9 cache 数据" />

  const flagOn = data.flags?.ENABLE_SIMULATION_CACHE
  const hitRate = data.hit_rate_approx ?? 0
  const hitColor = hitRate >= 0.3 ? '#00ff88' : hitRate >= 0.1 ? '#ffb700' : '#bfbfbf'
  const savedColor = data.saved_brain_calls > 0 ? '#00d4ff' : '#bfbfbf'

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <DatabaseOutlined style={{ marginRight: 8 }} />
          R9 Simulation Cache
        </Title>
        <Space>
          <Text type="secondary">窗口(天):</Text>
          <InputNumber
            min={1}
            max={90}
            value={days}
            onChange={(v) => setDays(v || 7)}
            style={{ width: 80 }}
          />
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      <Alert
        type={flagOn ? 'success' : 'warning'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space>
            <span>模拟缓存开关 (ENABLE_SIMULATION_CACHE)：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <Text type="secondary">缓存有效期 {data.ttl_days} 天 · 已过期但未清理 {data.expired_rows} 条</Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              开关当前已关闭，新的 BRAIN 模拟调用不会走缓存。可在 Feature Flag 控制台开启。
            </Text>
          ) : null
        }
      />

      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="『至少被复用过一次』的缓存条目占比（健康部署 ≥ 30%）。注意这不是传统的命中/未命中比 — 复用 1000 次和复用 1 次的条目都只算一次。要看真实复用深度请看右侧『每条缓存平均命中数』">
              <Statistic
                title={
                  <Space>
                    缓存复用率
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={hitRate * 100}
                precision={1}
                suffix="%"
                valueStyle={{ color: hitColor }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="重复命中次数 ≈ 节约的 BRAIN 调用次数（首次写入不计，每次复用记 1 次节约）">
              <Statistic
                title={
                  <Space>
                    节约的 BRAIN 调用
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={data.saved_brain_calls}
                prefix={<ThunderboltOutlined />}
                valueStyle={{ color: savedColor }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="缓存总条数"
              value={data.total_cached_rows}
              suffix={
                <Text type="secondary" style={{ fontSize: 12 }}>
                  / 近 {days} 天新增 {data.rows_in_window}
                </Text>
              }
              valueStyle={{ color: '#00d4ff' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="每条缓存平均被命中几次（≥ 1.5 说明跨任务、跨轮次复用充分）">
              <Statistic
                title={
                  <Space>
                    每条缓存平均命中数
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={data.avg_accesses_per_entry}
                precision={2}
                valueStyle={{ color: '#ffb700' }}
              />
            </Tooltip>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} sm={12}>
          <Card className="glass-card" size="small" title="模拟成功率">
            <Statistic
              value={data.success_rate * 100}
              precision={1}
              suffix="%"
              valueStyle={{ color: data.success_rate >= 0.5 ? '#00ff88' : '#ffb700' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              缓存中模拟成功的占比（失败的模拟结果也会被缓存以避免重复浪费）
            </Text>
          </Card>
        </Col>
        <Col xs={24} sm={12}>
          <Card className="glass-card" size="small" title="累计访问总数">
            <Statistic
              value={data.total_accesses_lifetime}
              valueStyle={{ color: '#9c88ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              所有缓存被命中的累计次数（含首次写入）
            </Text>
          </Card>
        </Col>
      </Row>

      <Card
        className="glass-card"
        title="按地区 / 股票池分布（按访问量降序，前 20）"
        style={{ marginTop: 16 }}
        extra={
          <Tag onClick={() => refetch()} style={{ cursor: 'pointer' }}>
            刷新
          </Tag>
        }
      >
        <Table
          rowKey={(r) => `${r.region}/${r.universe}`}
          size="small"
          dataSource={data.by_region || []}
          pagination={false}
          columns={[
            {
              title: '地区',
              dataIndex: 'region',
              width: 100,
              render: (r) => <Tag>{r}</Tag>,
            },
            { title: '股票池', dataIndex: 'universe', width: 140 },
            {
              title: '缓存条数',
              dataIndex: 'entries',
              width: 110,
              align: 'right',
            },
            {
              title: '总访问次数',
              dataIndex: 'accesses',
              width: 110,
              align: 'right',
            },
            {
              title: '节约 BRAIN 调用',
              dataIndex: 'saved_brain_calls',
              width: 150,
              align: 'right',
              render: (v) => (
                <Text strong style={{ color: v > 0 ? '#00d4ff' : undefined }}>
                  {v}
                </Text>
              ),
            },
            {
              title: '复用倍数',
              key: 'reuse',
              width: 110,
              align: 'right',
              render: (_, r) => (
                <Tooltip title="访问总数 / 缓存条数 — 越高说明缓存越值钱">
                  <span>{r.entries > 0 ? (r.accesses / r.entries).toFixed(2) : '—'}</span>
                </Tooltip>
              ),
            },
          ]}
        />
      </Card>
    </div>
  )
}
