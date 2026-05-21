import { useQuery } from '@tanstack/react-query'
import {
  Alert, Card, Col, Empty, InputNumber, Row, Space, Spin, Statistic,
  Table, Tag, Typography,
} from 'antd'
import { BulbOutlined } from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text } = Typography

export default function R8v3Monitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['ops/r8-v3/cognitive-layer-stats', days],
    queryFn: () => api.getOpsR8v3CognitiveLayerStats(days),
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (error) {
    return (
      <Alert type="error" showIcon message="加载 R8-v3 认知层统计失败"
        description={error?.response?.data?.detail || error?.message} />
    )
  }
  if (!data) return <Empty description="无 R8-v3 数据" />

  const flagOn = data.flags?.ENABLE_COGNITIVE_LAYER_PROMPT
  const total = data.total_stamped_alphas ?? 0

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <BulbOutlined style={{ marginRight: 8 }} />
          认知层监控（R8-v3）
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
            <span>认知层开关 (ENABLE_COGNITIVE_LAYER_PROMPT)：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <Text type="secondary">近 {days} 天 stamped alpha {total} 个</Text>
          </Space>
        }
        description={
          !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              开关关闭时 hypothesis prompt 不注入研究透镜。累积 ≥7d bandit 数据后可将 SELECT_MODE 切到 'bandit'。
            </Text>
          ) : null
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic title="近窗口 stamped alpha 总数" value={total} valueStyle={{ color: '#00d4ff' }} />
          </Card>
        </Col>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic title="已 fire 的层数" value={(data.by_layer || []).length} suffix="/ 7" valueStyle={{ color: '#9c88ff' }} />
          </Card>
        </Col>
      </Row>

      <Card className="glass-card" title="按认知层分布（fire 次数降序）">
        <Table
          rowKey="layer_id" size="small" pagination={false}
          dataSource={data.by_layer || []}
          columns={[
            { title: '认知层', dataIndex: 'layer_id', render: (v) => <Tag color="purple">{v}</Tag> },
            { title: 'fire 次数', dataIndex: 'fired_count', align: 'right', width: 100,
              sorter: (a, b) => a.fired_count - b.fired_count, defaultSortOrder: 'descend' },
            { title: 'PASS', dataIndex: 'pass_count', align: 'right', width: 80 },
            { title: 'FAIL', dataIndex: 'fail_count', align: 'right', width: 80 },
            { title: 'PASS 率', dataIndex: 'pass_rate', align: 'right', width: 110,
              render: (v) => (
                <Text strong style={{ color: v >= 0.3 ? '#00ff88' : v >= 0.1 ? '#ffb700' : '#bfbfbf' }}>
                  {(v * 100).toFixed(1)}%
                </Text>
              ) },
          ]}
        />
      </Card>
    </div>
  )
}
