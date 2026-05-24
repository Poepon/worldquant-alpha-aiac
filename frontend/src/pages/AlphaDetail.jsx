import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Row,
  Col,
  Card,
  Typography,
  Tag,
  Button,
  Space,
  Descriptions,
  Spin,
  Empty,
  Input,
  message,
  Divider,
  Alert,
  Timeline,
  Tabs,
  Tooltip as AntTooltip,
} from 'antd'
import {
  ArrowLeftOutlined,
  LikeOutlined,
  DislikeOutlined,
  CopyOutlined,
  BranchesOutlined,
  HistoryOutlined,
  ReloadOutlined,
  TrophyOutlined,
} from '@ant-design/icons'
import { 
  LineChart, 
  Line, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  ResponsiveContainer 
} from 'recharts'
import api from '../services/api'
import { formatRelative, formatDateTime } from '../utils/time'

const { Title, Text, Paragraph } = Typography

// Mock PnL data for demo
const mockPnL = [
  { date: '2025-01', returns: 0 },
  { date: '2025-02', returns: 1.5 },
  { date: '2025-03', returns: 3.2 },
  { date: '2025-04', returns: 2.8 },
  { date: '2025-05', returns: 5.1 },
  { date: '2025-06', returns: 6.8 },
  { date: '2025-07', returns: 8.2 },
  { date: '2025-08', returns: 7.5 },
  { date: '2025-09', returns: 9.1 },
  { date: '2025-10', returns: 11.3 },
  { date: '2025-11', returns: 10.8 },
  { date: '2025-12', returns: 12.5 },
]

const CRISIS_WINDOW_LABELS = {
  covid_2020: 'COVID 2020',
  rate_shock_2022: '利率冲击 2022',
  svb_2023: 'SVB 2023',
  tariff_2025: '关税 2025',
}

function CrisisCorrelationPanel({ crisis }) {
  // crisis: { [window]: { status, max_corr?, overlap_days?, counterpart_id? } }
  if (!crisis || Object.keys(crisis).length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        尚无危机相关性数据（仅 PASS-eligible alpha 在本地 PnL 缓存命中时计算）。
      </Text>
    )
  }
  const order = ['covid_2020', 'rate_shock_2022', 'svb_2023', 'tariff_2025']
  const items = order
    .filter((k) => k in crisis)
    .concat(Object.keys(crisis).filter((k) => !order.includes(k)))

  return (
    <Space wrap size={[8, 8]}>
      {items.map((w) => {
        const info = crisis[w] || {}
        const label = CRISIS_WINDOW_LABELS[w] || w
        if (info.status !== 'ok') {
          return (
            <AntTooltip
              key={w}
              title={
                <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
                  status: {info.status || 'unknown'}
                  {info.overlap_days !== undefined && (
                    <> · overlap_days: {info.overlap_days}</>
                  )}
                </div>
              }
            >
              <Tag>{label} · n/a</Tag>
            </AntTooltip>
          )
        }
        const v = info.max_corr
        const color = v >= 0.7 ? 'red' : v >= 0.5 ? 'orange' : v >= 0.3 ? 'gold' : 'green'
        return (
          <AntTooltip
            key={w}
            title={
              <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
                <div>max_corr: {v.toFixed(4)}</div>
                <div>counterpart: {info.counterpart_id}</div>
                <div>overlap: {info.overlap_days} days</div>
              </div>
            }
          >
            <Tag color={color} style={{ cursor: 'help' }}>
              {label} · {v.toFixed(2)}
            </Tag>
          </AntTooltip>
        )
      })}
    </Space>
  )
}

function CanSubmitTag({ canSubmit, failed, pending, loading, onRefresh }) {
  if (canSubmit === true) {
    const pendN = pending?.length || 0
    const tip = pendN > 0
      ? `is.checks 全无 FAIL；仍有 ${pendN} 项 PENDING（如 SELF_CORRELATION）— 通过后才是最终结论。`
      : 'is.checks 全部 PASS — 满足 BRAIN 提交门槛。'
    return (
      <AntTooltip title={tip}>
        <Tag color="success" icon={loading ? <ReloadOutlined spin /> : null} onClick={onRefresh} style={{ cursor: 'pointer' }}>
          ✅ 可提交{pendN > 0 ? ` (${pendN} pending)` : ''}
        </Tag>
      </AntTooltip>
    )
  }
  if (canSubmit === false) {
    const tip = (
      <div>
        <div style={{ marginBottom: 4 }}>{failed.length} 个 BRAIN 检查 FAIL：</div>
        {failed.map((c) => (
          <div key={c.name} style={{ fontFamily: 'monospace', fontSize: 12 }}>
            • {c.name}
            {c.value !== undefined && c.limit !== undefined
              ? ` (value=${c.value}, limit=${c.limit})`
              : ''}
          </div>
        ))}
      </div>
    )
    return (
      <AntTooltip title={tip}>
        <Tag color="error" icon={loading ? <ReloadOutlined spin /> : null} onClick={onRefresh} style={{ cursor: 'pointer' }}>
          ⚠️ 不可提交 ({failed.length} FAIL)
        </Tag>
      </AntTooltip>
    )
  }
  return (
    <AntTooltip title="尚未调用 BRAIN GET /alphas/{id} 校验 is.checks，点击立即检查">
      <Tag onClick={onRefresh} style={{ cursor: 'pointer' }} icon={loading ? <ReloadOutlined spin /> : <ReloadOutlined />}>
        🔍 检查可提交性
      </Tag>
    </AntTooltip>
  )
}


export default function AlphaDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // Fetch alpha details
  const { data: alpha, isLoading } = useQuery({
    queryKey: ['alpha', id],
    queryFn: () => api.getAlpha(id),
  })

  // Feedback mutation
  const feedbackMutation = useMutation({
    mutationFn: ({ rating, comment }) => api.submitAlphaFeedback(id, rating, comment),
    onSuccess: () => {
      message.success('反馈已提交')
      queryClient.invalidateQueries(['alpha', id])
    },
  })

  const refreshCanSubmitMutation = useMutation({
    mutationFn: () => api.refreshCanSubmit(id),
    onSuccess: (data) => {
      if (data.can_submit === null || data.can_submit === undefined) {
        message.warning(data.message || 'BRAIN 未返回结果，未更新')
      } else if (data.can_submit) {
        message.success(`✅ 可提交（${data.pending_checks?.length || 0} 项待定）`)
      } else {
        message.error(`⚠️ 不可提交：${data.failed_checks.length} 个 FAIL`)
      }
      queryClient.invalidateQueries(['alpha', id])
    },
    onError: (e) => message.error(`刷新失败：${e?.message || e}`),
  })

  const handleFeedback = (rating) => {
    feedbackMutation.mutate({ rating })
  }

  const copyExpression = () => {
    navigator.clipboard.writeText(alpha.expression)
    message.success('表达式已复制到剪贴板')
  }

  const copyBrainId = () => {
    if (!alpha?.alpha_id) return
    navigator.clipboard.writeText(alpha.alpha_id)
    message.success('BRAIN Alpha ID 已复制')
  }

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 100 }}>
        <Spin size="large" />
      </div>
    )
  }

  if (!alpha) {
    return (
      <Empty description="未找到 Alpha">
        <Button onClick={() => navigate('/alphas')}>返回实验室</Button>
      </Empty>
    )
  }

  const metrics = alpha.metrics || {}

  return (
    <div>
      {/* Header */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
        <Col>
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/alphas')}>
              返回
            </Button>
            <Title level={3} style={{ margin: 0 }}>
              Alpha #{alpha.id}
            </Title>
            {alpha.alpha_id && (
              <AntTooltip title="点击复制 BRAIN Alpha ID">
                <Tag
                  color="geekblue"
                  style={{ cursor: 'pointer', fontFamily: 'monospace' }}
                  onClick={copyBrainId}
                  icon={<CopyOutlined />}
                >
                  BRAIN: {alpha.alpha_id}
                </Tag>
              </AntTooltip>
            )}
            <Tag color={alpha.quality_status === 'PASS' ? 'success' : 'default'}>
              {alpha.quality_status}
            </Tag>
            {alpha.date_submitted ? (
              <AntTooltip title={`已提交至 BRAIN：${formatDateTime(alpha.date_submitted)}`}>
                <Tag color="green">✅ 已提交</Tag>
              </AntTooltip>
            ) : (
              <AntTooltip title="尚未提交至 BRAIN">
                <Tag>⚪ 未提交</Tag>
              </AntTooltip>
            )}
            <CanSubmitTag
              canSubmit={alpha.can_submit}
              failed={alpha.metrics?._brain_failed_checks || []}
              pending={alpha.metrics?._brain_pending_checks || []}
              loading={refreshCanSubmitMutation.isPending}
              onRefresh={() => refreshCanSubmitMutation.mutate()}
              alphaId={alpha.alpha_id}
            />
          </Space>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        {/* Left: Expression & Info */}
        <Col xs={24} lg={14}>
          {/* Expression Card */}
          <Card 
            className="glass-card" 
            title="表达式"
            extra={
              <Button icon={<CopyOutlined />} size="small" onClick={copyExpression}>
                复制
              </Button>
            }
          >
            <pre style={{ 
              fontSize: 14,
              lineHeight: 1.6,
              overflow: 'auto',
              maxHeight: 200,
            }}>
              {alpha.expression}
            </pre>
          </Card>

          {/* Hypothesis & Explanation */}
          {(alpha.hypothesis || alpha.logic_explanation) && (
            <Card className="glass-card" title="分析" style={{ marginTop: 16 }}>
              {alpha.hypothesis && (
                <>
                  <Text strong>假设 (Hypothesis):</Text>
                  <Paragraph style={{ color: 'rgba(255,255,255,0.85)' }}>
                    {alpha.hypothesis}
                  </Paragraph>
                </>
              )}
              {alpha.logic_explanation && (
                <>
                  <Text strong>逻辑解释:</Text>
                  <Paragraph style={{ color: 'rgba(255,255,255,0.85)' }}>
                    {alpha.logic_explanation}
                  </Paragraph>
                </>
              )}
            </Card>
          )}

          {/* PnL Chart */}
          <Card className="glass-card" title="累计收益" style={{ marginTop: 16 }}>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={mockPnL}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                <XAxis dataKey="date" stroke="rgba(255,255,255,0.5)" />
                <YAxis stroke="rgba(255,255,255,0.5)" unit="%" />
                <Tooltip 
                  contentStyle={{ 
                    background: '#131a2b', 
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: 8,
                  }}
                />
                <Line 
                  type="monotone" 
                  dataKey="returns" 
                  stroke="#00ff88" 
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </Card>
        </Col>

        {/* Right: Metrics & Feedback */}
        <Col xs={24} lg={10}>
          {/* Metrics */}
          <Card className="glass-card" title="绩效指标">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="夏普比率">
                <Text style={{ 
                  fontSize: 18, 
                  fontWeight: 600,
                  color: metrics.sharpe >= 1.5 ? '#00ff88' : '#ffb700'
                }}>
                  {metrics.sharpe?.toFixed(2) || '--'}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="收益率">
                {metrics.returns?.toFixed(2)}%
              </Descriptions.Item>
              <Descriptions.Item label="换手率">
                {metrics.turnover?.toFixed(2) || '--'}
              </Descriptions.Item>
              <Descriptions.Item label="最大回撤">
                {metrics.max_dd?.toFixed(2)}%
              </Descriptions.Item>
              <Descriptions.Item label="Fitness">
                {metrics.fitness?.toFixed(2) || '--'}
              </Descriptions.Item>
              <Descriptions.Item label="Self-corr">
                {(() => {
                  const v = metrics._self_corr
                  const src = metrics._self_corr_source
                  if (v == null) {
                    return (
                      <AntTooltip title="本地 OS PnL cache 未命中且未走 BRAIN 兜底,提交前请刷新 cache 或人工核对">
                        <Tag color="default">unknown</Tag>
                      </AntTooltip>
                    )
                  }
                  const color = v >= 0.7 ? 'red' : v >= 0.5 ? 'orange' : 'green'
                  const srcColor = src === 'local' ? 'cyan' : src === 'brain' ? 'blue' : 'default'
                  return (
                    <Space size={4}>
                      <Tag color={color}>{v.toFixed(4)}</Tag>
                      <AntTooltip title={src === 'local' ? '本地 OS PnL cache 实测' : src === 'brain' ? 'BRAIN /correlations/SELF API' : '来源未知'}>
                        <Tag color={srcColor}>{src || '?'}</Tag>
                      </AntTooltip>
                    </Space>
                  )
                })()}
              </Descriptions.Item>
            </Descriptions>
          </Card>

          {/* Crisis-window correlation strip — sourced from
              alpha.metrics._crisis_correlations populated in evaluation.py
              when local PnL cache hit. */}
          <Card
            className="glass-card"
            title={
              <Space>
                <span>危机窗口相关性</span>
                <AntTooltip title="该 alpha 在 4 个历史危机窗口下相对 OS 池的 max-corr。任一窗口 ≥ 0.7 (红) 提示隐性集中度风险，慎重提交。">
                  <Tag color="purple">stress test</Tag>
                </AntTooltip>
              </Space>
            }
            style={{ marginTop: 16 }}
          >
            <CrisisCorrelationPanel crisis={metrics?._crisis_correlations} />
          </Card>

          {/* Metadata */}
          <Card className="glass-card" title="元数据" style={{ marginTop: 16 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="BRAIN Alpha ID">
                {alpha.alpha_id ? (
                  <Space size={4}>
                    <Text code copyable={{ text: alpha.alpha_id, tooltips: ['复制', '已复制'] }}>
                      {alpha.alpha_id}
                    </Text>
                  </Space>
                ) : (
                  <Text type="secondary">未提交至 BRAIN</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="地区">{alpha.region}</Descriptions.Item>
              <Descriptions.Item label="股票池">{alpha.universe}</Descriptions.Item>
              <Descriptions.Item label="数据集">{alpha.dataset_id}</Descriptions.Item>
              <Descriptions.Item label="使用字段">
                <Space wrap>
                  {(alpha.fields_used || []).map(f => (
                    <Tag key={f} size="small">{f}</Tag>
                  ))}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="使用算子">
                <Space wrap>
                  {(alpha.operators_used || []).map(o => (
                    <Tag key={o} size="small" color="blue">{o}</Tag>
                  ))}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">
                <AntTooltip title={formatDateTime(alpha.created_at)}>
                  <span>{formatRelative(alpha.created_at)}</span>
                </AntTooltip>
              </Descriptions.Item>
              <Descriptions.Item label="提交时间">
                {alpha.date_submitted ? (
                  <AntTooltip title={formatDateTime(alpha.date_submitted)}>
                    <span>{formatRelative(alpha.date_submitted)}</span>
                  </AntTooltip>
                ) : (
                  <Text type="secondary">未提交</Text>
                )}
              </Descriptions.Item>
            </Descriptions>
          </Card>

          {/* Human Feedback — W3: prominent HITL guidance per plan R3 #1 */}
          <Card
            className="glass-card"
            title={
              <Space>
                <span>人工反馈</span>
                {alpha.human_feedback === 'NONE' && (
                  <Tag color="gold">需要你的评价</Tag>
                )}
              </Space>
            }
            style={{ marginTop: 16, borderColor: alpha.human_feedback === 'NONE' ? '#faad14' : undefined }}
          >
            {alpha.human_feedback === 'NONE' && (
              <Paragraph type="secondary" style={{ marginBottom: 16 }}>
                你的反馈会直接进知识库：👍 LIKED 升级为 SUCCESS_PATTERN（+confidence），
                👎 DISLIKED 削弱已知模式（-confidence）。每条评价都会被下一轮 mining 学习。
              </Paragraph>
            )}
            <div style={{ marginBottom: 16 }}>
              <Text>当前评价: </Text>
              {alpha.human_feedback === 'LIKED' && (
                <Tag icon={<LikeOutlined />} color="success">喜欢</Tag>
              )}
              {alpha.human_feedback === 'DISLIKED' && (
                <Tag icon={<DislikeOutlined />} color="error">不喜欢</Tag>
              )}
              {alpha.human_feedback === 'NONE' && (
                <Text type="secondary">未评价</Text>
              )}
            </div>

            <Space size="middle">
              <Button
                size="large"
                icon={<LikeOutlined />}
                type={alpha.human_feedback === 'LIKED' ? 'primary' : 'default'}
                onClick={() => handleFeedback('LIKED')}
                loading={feedbackMutation.isLoading}
                style={alpha.human_feedback === 'NONE' ? { boxShadow: '0 0 0 2px #52c41a44' } : undefined}
              >
                👍 点赞 (LIKED)
              </Button>
              <Button
                size="large"
                icon={<DislikeOutlined />}
                danger={alpha.human_feedback === 'DISLIKED'}
                onClick={() => handleFeedback('DISLIKED')}
                loading={feedbackMutation.isLoading}
              >
                👎 踩 (DISLIKED)
              </Button>
            </Space>

            {alpha.feedback_comment && (
              <>
                <Divider />
                <Text strong>评论:</Text>
                <Paragraph style={{ marginTop: 8 }}>
                  {alpha.feedback_comment}
                </Paragraph>
              </>
            )}
          </Card>
        </Col>
      </Row>

      <LineageSection alphaId={id} />
    </div>
  )
}


function LineageSection({ alphaId }) {
  // Same queryKey as parent — react-query dedupes, so this is free
  const { data: alpha } = useQuery({
    queryKey: ['alpha', alphaId],
    queryFn: () => api.getAlpha(alphaId),
    enabled: !!alphaId,
  })

  const { data: transitionsResp, isLoading: transLoading } = useQuery({
    queryKey: ['alpha', alphaId, 'transitions'],
    queryFn: () => api.getAlphaTransitions(alphaId, 50),
    enabled: !!alphaId,
  })

  // IQC marginal contribution — lazy fetch (BRAIN poll can be slow)
  const [marginalEnabled, setMarginalEnabled] = useState(false)
  const [marginalCompetition, setMarginalCompetition] = useState('IQC2026S1')
  const {
    data: marginal,
    isLoading: marginalLoading,
    error: marginalError,
    refetch: refetchMarginal,
  } = useQuery({
    queryKey: ['alpha', alphaId, 'marginal', marginalCompetition],
    queryFn: () =>
      api.getAlphaMarginalContribution(alphaId, {
        competition: marginalCompetition || undefined,
      }),
    enabled: marginalEnabled && !!alpha?.alpha_id,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  const transitions = transitionsResp?.transitions || []

  return (
    <Card
      className="glass-card"
      style={{ marginTop: 16 }}
      title={
        <Space>
          <BranchesOutlined />
          <span>状态变迁 & 边际贡献</span>
        </Space>
      }
    >
      <Tabs
        defaultActiveKey="marginal"
        items={[
          {
            key: 'marginal',
            label: (
              <Space>
                <TrophyOutlined />
                边际贡献
                {alpha?.can_submit && <Tag color="green">可提交</Tag>}
              </Space>
            ),
            children: (
              <div>
                <Alert
                  type="info"
                  message="BRAIN before-and-after-performance"
                  description={
                    <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
                      <span>
                        Standalone(独立运行)vs Merged(并入组合)
                        的 sharpe/fitness/turnover 对比 — 决定是否值得提交。
                      </span>
                      <span style={{ color: '#888' }}>
                        BRAIN 已移除竞赛 score 字段(2026-05-24);现以 merged 后的
                        stats 增量衡量边际贡献。turnover/drawdown 越低越好。
                      </span>
                    </Space>
                  }
                  style={{ marginBottom: 16 }}
                  showIcon
                />
                <Space style={{ marginBottom: 16 }}>
                  <Input
                    placeholder="competition ID (空=个人组合)"
                    value={marginalCompetition}
                    onChange={(e) => setMarginalCompetition(e.target.value)}
                    style={{ width: 200 }}
                  />
                  <Button
                    type="primary"
                    icon={<ReloadOutlined />}
                    loading={marginalLoading}
                    disabled={!alpha?.alpha_id}
                    onClick={() => {
                      setMarginalEnabled(true)
                      // useQuery picks up on next render; force refetch when already enabled
                      if (marginalEnabled) refetchMarginal()
                    }}
                  >
                    拉取 BRAIN 数据
                  </Button>
                  {!alpha?.alpha_id && (
                    <Text type="warning">该 alpha 无 BRAIN ID,不能拉取</Text>
                  )}
                </Space>
                {marginalError && (
                  <Alert
                    type="error"
                    message="拉取失败"
                    description={marginalError.message || '未知错误'}
                    style={{ marginBottom: 16 }}
                  />
                )}
                {marginalLoading && (
                  <Spin tip="BRAIN 计算中(可能 5-20s)..." />
                )}
                {marginal && (
                  <>
                    {marginal.analysis && (
                      <Alert
                        type={
                          { SUBMIT: 'success', SKIP: 'error', NEUTRAL: 'warning' }[
                            marginal.analysis.recommendation
                          ] || 'info'
                        }
                        showIcon
                        style={{ marginBottom: 16 }}
                        message={
                          <Space>
                            <Text strong style={{ fontSize: 15 }}>
                              {marginal.analysis.label}
                            </Text>
                            {marginal.analysis.composite_score != null && (
                              <Tag
                                color={
                                  marginal.analysis.composite_score > 0
                                    ? 'green'
                                    : marginal.analysis.composite_score < 0
                                      ? 'red'
                                      : 'default'
                                }
                              >
                                综合边际评分 {marginal.analysis.composite_score > 0 ? '+' : ''}
                                {marginal.analysis.composite_score}
                              </Tag>
                            )}
                          </Space>
                        }
                        description={
                          <Space direction="vertical" size={6} style={{ fontSize: 12, width: '100%' }}>
                            {marginal.analysis.rationale && (
                              <Text>{marginal.analysis.rationale}</Text>
                            )}
                            {(marginal.analysis.guardrails || []).length > 0 && (
                              <div>
                                {marginal.analysis.guardrails.map((g, i) => (
                                  <div key={i} style={{ color: '#cf1322' }}>
                                    ⚠ {g}
                                  </div>
                                ))}
                              </div>
                            )}
                            <Row gutter={16}>
                              <Col span={12}>
                                <Text strong style={{ color: '#389e0d' }}>
                                  ✓ 正向贡献 ({(marginal.analysis.positives || []).length})
                                </Text>
                                {(marginal.analysis.positives || []).length === 0 ? (
                                  <div style={{ color: '#999' }}>—</div>
                                ) : (
                                  marginal.analysis.positives.map((p) => (
                                    <div key={p.metric}>· {p.text}</div>
                                  ))
                                )}
                              </Col>
                              <Col span={12}>
                                <Text strong style={{ color: '#cf1322' }}>
                                  ✗ 负向拖累 ({(marginal.analysis.negatives || []).length})
                                </Text>
                                {(marginal.analysis.negatives || []).length === 0 ? (
                                  <div style={{ color: '#999' }}>—</div>
                                ) : (
                                  marginal.analysis.negatives.map((n) => (
                                    <div key={n.metric}>· {n.text}</div>
                                  ))
                                )}
                              </Col>
                            </Row>
                          </Space>
                        }
                      />
                    )}
                    <Descriptions
                      title={`Scope: ${marginal.scope}`}
                      bordered
                      size="small"
                      column={1}
                      style={{ marginBottom: 16 }}
                    >
                      <Descriptions.Item label="Partition">
                        <Text code>
                          {marginal.partition_name ?? marginal.raw?.partitionName ?? '—'}
                        </Text>
                      </Descriptions.Item>
                    </Descriptions>
                    <Row gutter={16}>
                      {['sharpe', 'fitness', 'margin', 'returns', 'pnl', 'turnover', 'drawdown'].map((k) => {
                        const before = marginal.raw?.stats?.before?.[k]
                        const after = marginal.raw?.stats?.after?.[k]
                        const delta = marginal.deltas?.[k]
                        const isMoney = k === 'pnl'
                        const fmt = (v) =>
                          typeof v === 'number'
                            ? (isMoney ? v.toLocaleString() : v.toFixed(4))
                            : '—'
                        return (
                          <Col span={8} key={k} style={{ marginBottom: 12 }}>
                            <Card size="small" title={k.toUpperCase()}>
                              <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
                                <Text type="secondary">before: {fmt(before)}</Text>
                                <Text strong>after: {fmt(after)}</Text>
                                {delta != null && (
                                  <Tag
                                    color={
                                      // for turnover/drawdown lower is better — invert color
                                      (k === 'turnover' || k === 'drawdown')
                                        ? (delta <= 0 ? 'green' : 'red')
                                        : (delta >= 0 ? 'green' : 'red')
                                    }
                                  >
                                    Δ {delta > 0 ? '+' : ''}{fmt(delta)}
                                  </Tag>
                                )}
                              </Space>
                            </Card>
                          </Col>
                        )
                      })}
                    </Row>
                  </>
                )}
                {!marginalEnabled && !marginalLoading && !marginal && (
                  <Empty description="点击「拉取 BRAIN 数据」开始,首次调用 BRAIN 可能 5-20s" />
                )}
              </div>
            ),
          },
          {
            key: 'transitions',
            label: (
              <Space>
                <HistoryOutlined />
                状态变迁
                {transitions.length > 0 && <Tag>{transitions.length}</Tag>}
              </Space>
            ),
            children: transLoading ? (
              <Spin />
            ) : transitions.length === 0 ? (
              <Empty description="尚无状态变迁记录" />
            ) : (
              <Timeline
                items={transitions.map((t) => ({
                  color: STATUS_COLORS[t.new_status] || 'gray',
                  children: (
                    <Space direction="vertical" size={2}>
                      <Space>
                        {t.old_status && (
                          <Tag color={STATUS_COLORS[t.old_status]}>
                            {t.old_status}
                          </Tag>
                        )}
                        <span>→</span>
                        <Tag color={STATUS_COLORS[t.new_status]}>
                          {t.new_status}
                        </Tag>
                        {t.sharpe_at_transition != null && (
                          <Text type="secondary">
                            sharpe@trans={t.sharpe_at_transition.toFixed(2)}
                          </Text>
                        )}
                      </Space>
                      {t.reason && (
                        <Text type="secondary">{t.reason}</Text>
                      )}
                      <Space>
                        {t.source && <Tag>{t.source}</Tag>}
                        <AntTooltip title={formatDateTime(t.transitioned_at)}>
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            {formatRelative(t.transitioned_at)}
                          </Text>
                        </AntTooltip>
                      </Space>
                    </Space>
                  ),
                }))}
              />
            ),
          },
        ]}
      />
    </Card>
  )
}
