import { useQuery } from '@tanstack/react-query'
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
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DollarOutlined,
  ExperimentOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons'
import { useState } from 'react'
import api from '../../services/api'

const { Title, Text } = Typography

export default function LLMJudgeMonitor() {
  const [days, setDays] = useState(7)
  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ['ops/r5/judge-stats', days],
    queryFn: () => api.getOpsR5JudgeStats(days),
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
        message="加载 R5 LLM Judge stats 失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }
  if (!data) return <Empty description="无 R5 数据" />

  const flagOn = data.flags?.ENABLE_LLM_JUDGE
  const gates = data.healthy_gates || {}
  const buckets = data.composite_score_buckets || []
  const bucketTotal = buckets.reduce((s, b) => s + b.count, 0)

  // 每个健康度门槛: { key, ok, value, threshold, label }
  const gateChecks = [
    {
      key: 'cost',
      label: '每次评判平均成本',
      value: `$${data.avg_cost_per_judge.toFixed(6)}`,
      ok: data.avg_cost_per_judge <= gates.avg_cost_per_judge_max,
      threshold: `≤ $${gates.avg_cost_per_judge_max}`,
    },
    {
      key: 'agree',
      label: '两评官内部一致率',
      value: `${(data.c1_c2_internal_agreement * 100).toFixed(1)}%`,
      ok: data.c1_c2_internal_agreement >= gates.c1_c2_internal_agreement_min,
      threshold: `≥ ${(gates.c1_c2_internal_agreement_min * 100).toFixed(0)}%`,
    },
    {
      key: 'err',
      label: 'LLM 调用错误率',
      value: `${(data.error_rate * 100).toFixed(1)}%`,
      ok: data.error_rate <= gates.error_rate_max,
      threshold: `≤ ${(gates.error_rate_max * 100).toFixed(0)}%`,
    },
    {
      key: 'sample',
      label: '有效评判样本量',
      value: data.total_judges_run,
      ok: data.total_judges_run >= gates.min_judges_run,
      threshold: `≥ ${gates.min_judges_run}`,
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ExperimentOutlined style={{ marginRight: 8 }} />
          LLM 评判监控（R5）
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
        type={data.is_healthy ? 'success' : 'warning'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space>
            <span>LLM 评判开关 (ENABLE_LLM_JUDGE)：</span>
            <Tag color={flagOn ? 'green' : 'default'}>{flagOn ? '已开启' : '已关闭'}</Tag>
            <span>·</span>
            <span>部署健康度：</span>
            <Tag color={data.is_healthy ? 'success' : 'warning'}>
              {data.is_healthy ? '所有门槛通过' : '部分门槛未达标'}
            </Tag>
          </Space>
        }
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            R1a 监控页已含 R5 总览（与 R1a 一致率、平均评分、累计成本）；
            本页聚焦 R5 内部细节：每次评判成本、两评官（c1/c2）内部一致率、调用错误率、综合评分分布。
          </Text>
        }
      />

      {/* Healthy-gate 4 卡片 */}
      <Row gutter={[16, 16]}>
        {gateChecks.map((g) => (
          <Col key={g.key} xs={12} sm={6}>
            <Card className="glass-card">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Space>
                  {g.ok ? (
                    <CheckCircleOutlined style={{ color: '#00ff88' }} />
                  ) : (
                    <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
                  )}
                  <Text>{g.label}</Text>
                </Space>
                <Text strong style={{ fontSize: 22, color: g.ok ? '#00ff88' : '#ffb700' }}>
                  {g.value}
                </Text>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  健康门槛 {g.threshold}
                </Text>
              </Space>
            </Card>
          </Col>
        ))}
      </Row>

      {/* Cost + Volume 行 */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} sm={8}>
          <Card className="glass-card" size="small" title="累计成本 (USD)">
            <Statistic
              value={data.total_cost_usd}
              precision={4}
              prefix={<DollarOutlined />}
              valueStyle={{ color: '#9c88ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              单次评判最高 ${data.max_cost_per_judge.toFixed(6)}
            </Text>
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card className="glass-card" size="small" title="评判数量">
            <Statistic
              value={data.total_judges_run}
              suffix={
                <Text type="secondary" style={{ fontSize: 12 }}>
                  / 总尝试 {data.total_attempts} 次
                </Text>
              }
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              其中 LLM 调用失败 {data.error_count} 条
            </Text>
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card className="glass-card" size="small" title="平均综合评分">
            <Statistic
              value={data.avg_composite_score}
              precision={4}
              valueStyle={{ color: '#ffb700' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              两评官加权（权重 α=0.5，越高表示假设/代码越一致）
            </Text>
          </Card>
        </Col>
      </Row>

      {/* 两评官详情 */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} sm={12}>
          <Card
            className="glass-card"
            size="small"
            title={
              <Space>
                第 1 评官（假设 ↔ 描述）
                <Tooltip title="LLM 评判：alpha 的『假设』与其『描述』在语义上是否一致">
                  <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                </Tooltip>
              </Space>
            }
          >
            <Statistic
              title="对齐率"
              value={data.c1_align_rate * 100}
              precision={1}
              suffix="%"
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              评判时的平均信心 {data.c1_avg_confidence.toFixed(3)}
            </Text>
          </Card>
        </Col>
        <Col xs={24} sm={12}>
          <Card
            className="glass-card"
            size="small"
            title={
              <Space>
                第 2 评官（描述 ↔ 表达式）
                <Tooltip title="LLM 评判：alpha 的『描述』与最终生成的『表达式』在语义上是否一致">
                  <InfoCircleOutlined style={{ color: '#9c88ff' }} />
                </Tooltip>
              </Space>
            }
          >
            <Statistic
              title="对齐率"
              value={data.c2_align_rate * 100}
              precision={1}
              suffix="%"
              valueStyle={{ color: '#00d4ff' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              评判时的平均信心 {data.c2_avg_confidence.toFixed(3)}
            </Text>
          </Card>
        </Col>
      </Row>

      {/* 综合评分分布 */}
      <Card
        className="glass-card"
        title="综合评分分布（越高表示假设/描述/代码越一致）"
        style={{ marginTop: 16 }}
      >
        {bucketTotal === 0 ? (
          <Empty description="窗口内尚无综合评分数据" />
        ) : (
          <List
            size="small"
            dataSource={buckets}
            renderItem={(b) => {
              const pct = bucketTotal > 0 ? (b.count / bucketTotal) * 100 : 0
              const color =
                b.bucket === '0.7-1.0' ? '#00ff88' : b.bucket === '0.5-0.7' ? '#ffb700' : '#ff4d4f'
              const desc =
                b.bucket === '0.7-1.0' ? '（一致性高）'
                  : b.bucket === '0.5-0.7' ? '（一致性中等）'
                  : '（一致性低）'
              return (
                <List.Item>
                  <Space direction="vertical" style={{ width: '100%' }} size={4}>
                    <Space>
                      <Tag color={color}>{b.bucket} {desc}</Tag>
                      <Text>{b.count} 条</Text>
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
    </div>
  )
}
