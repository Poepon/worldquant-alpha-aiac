import { useQuery } from '@tanstack/react-query'
import {
  Alert, Card, Col, Empty, InputNumber, Row, Space, Spin, Statistic,
  Table, Tag, Typography,
} from 'antd'
import { ReadOutlined } from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text, Paragraph } = Typography

export default function G10LogicMonitor() {
  const [days, setDays] = useState(28)
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['ops/g10/logic-library', days],
    queryFn: () => api.getOpsG10LogicLibrary({ days, activeOnly: true, limit: 100 }),
    refetchInterval: 120_000,
  })

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (error) {
    return (
      <Alert type="error" showIcon message="加载逻辑库失败"
        description={error?.response?.data?.detail || error?.message} />
    )
  }
  if (!data) return <Empty description="暂无逻辑库数据" />

  const flagOn = data.flags?.ENABLE_G10_LOGIC_DISTILL
  const byRegion = data.by_region || {}

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ReadOutlined style={{ marginRight: 8 }} />
          逻辑资产库监控
        </Title>
        <Space>
          <Text type="secondary">窗口（天）：</Text>
          <InputNumber min={7} max={180} value={days} onChange={(v) => setDays(v || 28)} style={{ width: 90 }} />
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      <Alert
        type={flagOn ? 'success' : 'warning'} showIcon style={{ marginBottom: 16 }}
        message={
          <Space wrap>
            <span>逻辑提炼开关：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <Text type="secondary">每周日 03:00（北京时间）提炼 · 注入需另开「逻辑注入」开关</Text>
          </Space>
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="活跃条目" value={data.total_active ?? 0} valueStyle={{ color: '#00ff88' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="已退役条目" value={data.total_retired ?? 0} valueStyle={{ color: '#bfbfbf' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="窗口提炼成本" value={data.weekly_total_cost_usd ?? 0}
              precision={4} prefix="$" valueStyle={{ color: '#ffb700' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="覆盖地区" value={Object.keys(byRegion).length} valueStyle={{ color: '#9c88ff' }} />
          </Card>
        </Col>
      </Row>

      <Card className="glass-card" title={`活跃逻辑条目（前 ${data.entries?.length ?? 0}）`}>
        <Table
          rowKey="id" size="small" pagination={{ pageSize: 10 }}
          dataSource={data.entries || []}
          columns={[
            { title: '地区', dataIndex: 'region', width: 80, render: (v) => <Tag>{v}</Tag> },
            { title: '因子类别', dataIndex: 'pillar', width: 110,
              render: (v) => (v ? <Tag color="purple">{v}</Tag> : <Text type="secondary">—</Text>) },
            { title: '逻辑摘要', dataIndex: 'logic_text',
              render: (v) => <Paragraph style={{ margin: 0 }} ellipsis={{ rows: 2, tooltip: v }}>{v}</Paragraph> },
            { title: '来源 alpha 数', dataIndex: 'source_alpha_count', width: 100, align: 'right' },
            { title: '与上周相似度', dataIndex: 'similarity_jaccard_to_prev_week', width: 110, align: 'right',
              render: (v) => (v == null ? '—' : (v).toFixed(2)) },
            { title: '成本', dataIndex: 'llm_cost_usd', width: 90, align: 'right',
              render: (v) => (v == null ? '—' : `$${v.toFixed(4)}`) },
          ]}
        />
      </Card>
    </div>
  )
}
