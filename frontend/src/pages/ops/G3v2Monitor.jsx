import { useQuery } from '@tanstack/react-query'
import {
  Alert, Card, Col, Empty, InputNumber, Row, Space, Spin, Statistic,
  Table, Tag, Typography,
} from 'antd'
import { CheckSquareOutlined } from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text } = Typography

export default function G3v2Monitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['ops/g3v2/parse-stats', days],
    queryFn: () => api.getOpsG3v2ParseStats(days),
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (error) {
    return (
      <Alert type="error" showIcon message="加载语法校验统计失败"
        description={error?.response?.data?.detail || error?.message} />
    )
  }
  if (!data) return <Empty description="暂无语法校验数据" />

  const flagOn = data.flags?.ENABLE_GRAMMAR_VALIDATOR
  const readmit = data.degrade_open_readmit_count ?? 0
  const unknownOps = data.top_unknown_ops || {}
  const unknownRows = Object.entries(unknownOps).map(([op, count]) => ({ op, count }))

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <CheckSquareOutlined style={{ marginRight: 8 }} />
          语法校验监控
        </Title>
        <Space>
          <Text type="secondary">窗口（天）：</Text>
          <InputNumber min={1} max={90} value={days} onChange={(v) => setDays(v || 7)} style={{ width: 80 }} />
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      <Alert
        type={flagOn ? 'success' : 'warning'} showIcon style={{ marginBottom: 16 }}
        message={
          <Space wrap>
            <span>语法校验开关：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
          </Space>
        }
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            注：语法解析失败的候选在入库前就被丢弃，无法在本页统计 ——
            真实丢弃率请查工作进程日志。本页展示<b>可统计</b>的信号：降级放行次数 + 未知算子频率。
          </Text>
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic
              title="降级放行次数"
              value={readmit}
              valueStyle={{ color: readmit > 0 ? '#ffb700' : '#00ff88' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {readmit > 0 ? '丢弃率超过 50% 触发了降级放行 —— 语法规则可能过严' : '无降级放行（健康）'}
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={8}>
          <Card className="glass-card">
            <Statistic title="含未知算子的 alpha" value={data.unknown_ops_alpha_count ?? 0}
              valueStyle={{ color: '#9c88ff' }} />
          </Card>
        </Col>
      </Row>

      <Card className="glass-card" title="未知算子频率（仅告警，前 20）">
        {unknownRows.length === 0 ? (
          <Empty description="无未知算子（语法白名单覆盖充分）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Table
            rowKey="op" size="small" pagination={false}
            dataSource={unknownRows.sort((a, b) => b.count - a.count)}
            columns={[
              { title: '算子', dataIndex: 'op', render: (v) => <Tag color="orange">{v}</Tag> },
              { title: '出现次数', dataIndex: 'count', align: 'right', width: 120 },
            ]}
          />
        )}
      </Card>
    </div>
  )
}
