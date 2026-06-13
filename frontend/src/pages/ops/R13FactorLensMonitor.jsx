import { useQuery } from '@tanstack/react-query'
import {
  Alert, Card, Col, Empty, InputNumber, Row, Space, Spin, Statistic,
  Table, Tag, Typography,
} from 'antd'
import { ExperimentOutlined } from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text } = Typography

// 因子透镜运行模式的中文显示（不改 key，仅用于展示）
const MODE_LABEL = {
  shadow: '影子模式（只观测）',
  soft: '软约束',
  hard: '硬约束',
}

export default function R13FactorLensMonitor() {
  const [days, setDays] = useState(7)
  const residualsQ = useQuery({
    queryKey: ['ops/r13/factor-residuals', days],
    queryFn: () => api.getOpsR13FactorResiduals(days),
    refetchInterval: 60_000,
  })
  const staleQ = useQuery({
    queryKey: ['ops/r13/snapshot-stale-check'],
    queryFn: () => api.getOpsR13SnapshotStaleCheck(90),
    refetchInterval: 300_000,
  })

  if (residualsQ.isLoading) {
    return <div style={{ textAlign: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (residualsQ.error) {
    return (
      <Alert type="error" showIcon message="加载风格因子透镜统计失败"
        description={residualsQ.error?.response?.data?.detail || residualsQ.error?.message} />
    )
  }
  const data = residualsQ.data
  if (!data) return <Empty description="无风格因子透镜数据" />

  const flagOn = data.flags?.ENABLE_FACTOR_LENS
  const mode = data.factor_lens_mode || 'shadow'
  const total = data.total_decomposed ?? 0
  const stale = staleQ.data
  const byMode = data.by_mode || {}

  const staleRows = stale
    ? Object.entries(stale.per_region || {}).map(([region, st]) => ({ region, ...st }))
    : []

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ExperimentOutlined style={{ marginRight: 8 }} />
          风格因子透镜监控
        </Title>
        <Space>
          <Text type="secondary">窗口(天):</Text>
          <InputNumber min={1} max={90} value={days} onChange={(v) => setDays(v || 7)} style={{ width: 80 }} />
          {residualsQ.isFetching && <Spin size="small" />}
        </Space>
      </Space>

      <Alert
        type={flagOn ? 'success' : 'warning'} showIcon style={{ marginBottom: 16 }}
        message={
          <Space wrap>
            <span>因子透镜开关：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <span>模式:</span>
            <Tag color={mode === 'hard' ? 'red' : mode === 'soft' ? 'orange' : 'blue'}>
              {MODE_LABEL[mode] || mode}
            </Tag>
            <Text type="secondary">近 {days} 天分解 {total} 个 alpha</Text>
          </Space>
        }
        description={
          total === 0 && flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              开关已开启但窗口内 0 个分解 — 多半是因子收益快照文件缺失（见下方「新鲜度」）或当前不是正式数据库。
            </Text>
          ) : !flagOn ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              影子模式 → 软约束 → 硬约束 三阶段：先在影子模式累计 ≥30 个 alpha 的残差，再校准残差 Sharpe 的最低门槛。
            </Text>
          ) : null
        }
      />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="分解总数" value={total} valueStyle={{ color: '#00d4ff' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="残差 Sharpe 中位" value={data.residual_sharpe_p50 ?? 0}
              precision={3} valueStyle={{ color: '#ffb700' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="残差 Sharpe p95" value={data.residual_sharpe_p95 ?? 0}
              precision={3} valueStyle={{ color: '#00ff88' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="残差 Sharpe 均值" value={data.residual_sharpe_mean ?? 0}
              precision={3} valueStyle={{ color: '#9c88ff' }} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12}>
          <Card className="glass-card" size="small" title="按阶段分布">
            {Object.keys(byMode).length === 0 ? (
              <Empty description="无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                {Object.entries(byMode).map(([k, v]) => (
                  <Space key={k} style={{ width: '100%', justifyContent: 'space-between' }}>
                    <Tag>{MODE_LABEL[k] || k}</Tag><Text strong>{v}</Text>
                  </Space>
                ))}
              </Space>
            )}
          </Card>
        </Col>
        <Col xs={24} sm={12}>
          <Card className="glass-card" size="small"
            title={
              <Space>
                因子快照新鲜度
                {stale?.any_stale && <Tag color="red">有过期</Tag>}
              </Space>
            }>
            <Table
              rowKey="region" size="small" pagination={false} dataSource={staleRows}
              columns={[
                { title: '地区', dataIndex: 'region', render: (v) => <Tag>{v.toUpperCase()}</Tag> },
                { title: '存在', dataIndex: 'exists', width: 70,
                  render: (v) => <Tag color={v ? 'green' : 'red'}>{v ? '是' : '缺'}</Tag> },
                { title: '入库天数', dataIndex: 'age_days', width: 90, align: 'right',
                  render: (v) => (v == null ? '—' : v) },
                { title: '过期', dataIndex: 'stale', width: 70,
                  render: (v) => <Tag color={v ? 'red' : 'green'}>{v ? '是' : '否'}</Tag> },
              ]}
            />
            {stale && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                过期阈值 {stale.stale_threshold_days} 天 · 运维每月刷新因子收益快照
              </Text>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
