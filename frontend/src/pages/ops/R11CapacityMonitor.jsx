import { useQuery } from '@tanstack/react-query'
import {
  Alert, Card, Col, Empty, InputNumber, Progress, Row, Space, Spin,
  Statistic, Tag, Typography,
} from 'antd'
import { FundOutlined } from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text } = Typography

export default function R11CapacityMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['ops/r11/capacity-stats', days],
    queryFn: () => api.getOpsR11CapacityStats(days),
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (error) {
    return (
      <Alert type="error" showIcon message="加载 R11 capacity 统计失败"
        description={error?.response?.data?.detail || error?.message} />
    )
  }
  if (!data) return <Empty description="无 R11 数据" />

  const flagOn = data.flags?.ENABLE_CAPACITY_SCORE
  const total = data.total_with_capacity ?? 0
  const buckets = data.buckets || []
  const maxCount = Math.max(1, ...buckets.map((b) => b.count))
  // saturation warn: > 60% in a single bucket (Sprint 2 review concern)
  const topBucket = buckets.reduce((a, b) => (b.count > (a?.count || 0) ? b : a), null)
  const saturated = total > 0 && topBucket && topBucket.count / total > 0.6

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <FundOutlined style={{ marginRight: 8 }} />
          容量估算监控（R11）
        </Title>
        <Space>
          <Text type="secondary">窗口(天):</Text>
          <InputNumber min={1} max={90} value={days} onChange={(v) => setDays(v || 7)} style={{ width: 80 }} />
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      <Alert
        type={flagOn ? 'success' : 'warning'} showIcon style={{ marginBottom: 16 }}
        message={
          <Space>
            <span>容量评分开关 (ENABLE_CAPACITY_SCORE)：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <Text type="secondary">近 {days} 天有容量估算的 alpha {total} 个</Text>
          </Space>
        }
        description={
          saturated ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              ⚠️ {((topBucket.count / total) * 100).toFixed(0)}% 集中在「{topBucket.bucket_label}」桶 —
              可能是公式饱和（Sprint 2 review 指出 USA 大盘易顶到 top 桶），考虑调整 ADV 表或 sub-linear scaling。
            </Text>
          ) : null
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic title="有容量估算的 alpha" value={total} valueStyle={{ color: '#00d4ff' }} />
          </Card>
        </Col>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic title="其中 PASS 数" value={data.pass_count_with_capacity ?? 0} valueStyle={{ color: '#00ff88' }} />
          </Card>
        </Col>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic title="容量样本 PASS 率" value={(data.capacity_pass_rate ?? 0) * 100}
              precision={1} suffix="%" valueStyle={{ color: '#ffb700' }} />
          </Card>
        </Col>
      </Row>

      <Card className="glass-card" title="容量 log-scale 分布（USD）">
        {total === 0 ? (
          <Empty description="窗口内无容量估算数据（开关关闭或无 PASS alpha）" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            {buckets.map((b) => (
              <div key={b.bucket_label}>
                <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                  <Text>{b.bucket_label}</Text>
                  <Text type="secondary">{b.count}</Text>
                </Space>
                <Progress
                  percent={Math.round((b.count / maxCount) * 100)}
                  showInfo={false}
                  strokeColor="#9c88ff"
                />
              </div>
            ))}
          </Space>
        )}
      </Card>
    </div>
  )
}
