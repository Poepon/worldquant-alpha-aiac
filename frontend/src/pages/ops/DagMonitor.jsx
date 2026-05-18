import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Card,
  Col,
  Empty,
  InputNumber,
  List,
  Progress,
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
  BranchesOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'
import { formatRelative, formatDateTime } from '../../utils/time'

const { Title, Text } = Typography

export default function DagMonitor() {
  const navigate = useNavigate()
  const [days, setDays] = useState(7)
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['ops/r6/dag-stats', days],
    queryFn: () => api.getOpsR6DagStats(days),
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
        message="加载 R6 DAG stats 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 R6 数据" />

  const flagOn = data.flags?.ENABLE_DAG_TRACE
  const avg = data.avg_nodes_per_run
  // Healthy: avg 5-40 nodes AND max depth >= 3 AND has runs at all
  const avgOk = avg >= 5 && avg <= 40
  const depthOk = data.max_depth_observed >= 3
  const hasRuns = data.total_runs_with_dag > 0
  const isHealthy = hasRuns && avgOk && depthOk

  const dist = data.depth_distribution || []
  const distTotal = dist.reduce((s, b) => s + b.run_count, 0)

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <BranchesOutlined style={{ marginRight: 8 }} />
          R6 DAG Trace
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
        type={!hasRuns ? 'info' : isHealthy ? 'success' : 'warning'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space>
            <span>DAG 追踪开关 (ENABLE_DAG_TRACE)：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <span>·</span>
            <span>窗口内带 DAG 的运行批次：</span>
            <Tag color={hasRuns ? 'blue' : 'default'}>{data.total_runs_with_dag}</Tag>
            <span>·</span>
            <span>健康度：</span>
            <Tag color={isHealthy ? 'success' : hasRuns ? 'warning' : 'default'}>
              {!hasRuns ? '无数据' : isHealthy ? '健康' : '需关注'}
            </Tag>
          </Space>
        }
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            健康门槛：每批次平均节点数在 [5, 40] 之间（太少 = 探索不足；
            太多 = 剪枝失效），且 DAG 最大层级 ≥ 3（真多层而非只有根节点 + 一层子节点）。
            点击运行批次 ID 可查看完整 DAG 详情。
          </Text>
        }
      />

      {/* 4 KPI 卡 */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="带 DAG 的运行批次数"
              value={data.total_runs_with_dag}
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>近 {days} 天</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="DAG 节点累计数"
              value={data.total_nodes_across_runs}
              valueStyle={{ color: '#9c88ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              单批次最高 {data.max_node_count} 个节点
            </Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="平均每个运行批次产生的节点数（健康区间 5-40，过少说明探索不足，过多说明剪枝失效）">
              <Statistic
                title={
                  <Space>
                    每批次平均节点数
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={avg}
                precision={2}
                valueStyle={{ color: avgOk ? '#00ff88' : '#ffb700' }}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="窗口内观察到的 DAG 最大层级（健康 ≥ 3，表示有真多层假设演化）">
              <Statistic
                title={
                  <Space>
                    DAG 最大层级
                    <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                  </Space>
                }
                value={data.max_depth_observed}
                valueStyle={{ color: depthOk ? '#00ff88' : '#ffb700' }}
              />
            </Tooltip>
          </Card>
        </Col>
      </Row>

      {/* DAG 层级分布 */}
      <Card
        className="glass-card"
        title="DAG 层级分布（按运行批次统计）"
        style={{ marginTop: 16 }}
      >
        {distTotal === 0 ? (
          <Empty description="窗口内尚无 DAG 数据" />
        ) : (
          <List
            size="small"
            dataSource={dist}
            renderItem={(b) => {
              const pct = distTotal > 0 ? (b.run_count / distTotal) * 100 : 0
              const color =
                b.depth >= 3 ? '#00ff88' : b.depth >= 1 ? '#ffb700' : '#bfbfbf'
              return (
                <List.Item>
                  <Space direction="vertical" style={{ width: '100%' }} size={4}>
                    <Space>
                      <Tag color={color}>第 {b.depth} 层</Tag>
                      <Text>{b.run_count} 个批次</Text>
                      <Text type="secondary">({pct.toFixed(1)}%)</Text>
                    </Space>
                    <Progress
                      percent={pct}
                      showInfo={false}
                      strokeColor={color}
                      size="small"
                    />
                  </Space>
                </List.Item>
              )
            }}
          />
        )}
      </Card>

      {/* 最近运行批次表 */}
      <Card
        className="glass-card"
        title="最近 20 条带 DAG 的运行批次"
        style={{ marginTop: 16 }}
      >
        <Table
          rowKey="run_id"
          size="small"
          dataSource={data.recent_runs || []}
          pagination={false}
          scroll={{ x: 900 }}
          columns={[
            {
              title: '运行批次',
              dataIndex: 'run_id',
              width: 100,
              render: (id) => (
                <a onClick={() => navigate(`/tasks?run=${id}`)}>#{id}</a>
              ),
            },
            {
              title: '所属任务',
              dataIndex: 'task_id',
              width: 100,
              render: (tid) =>
                tid ? (
                  <a onClick={() => navigate(`/tasks/${tid}`)}>#{tid}</a>
                ) : (
                  <Text type="secondary">—</Text>
                ),
            },
            {
              title: '节点数',
              dataIndex: 'node_count',
              width: 90,
              align: 'right',
            },
            {
              title: '最大层级',
              dataIndex: 'max_depth',
              width: 100,
              align: 'right',
              render: (d) => (
                <Tag color={d >= 3 ? 'green' : d >= 1 ? 'gold' : 'default'}>
                  第 {d} 层
                </Tag>
              ),
            },
            {
              title: '根假设 ID',
              dataIndex: 'root_id',
              ellipsis: true,
              render: (r) => (
                <Text code style={{ fontSize: 11 }}>
                  {r || '—'}
                </Text>
              ),
            },
            {
              title: '当前选中节点',
              dataIndex: 'current_selection',
              ellipsis: true,
              render: (s) => (
                <Tooltip title={s}>
                  <Text code style={{ fontSize: 11 }}>
                    {s || '—'}
                  </Text>
                </Tooltip>
              ),
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              width: 130,
              render: (t) => (
                <Tooltip title={formatDateTime(t)}>
                  <span>{formatRelative(t)}</span>
                </Tooltip>
              ),
            },
          ]}
        />
      </Card>
    </div>
  )
}
