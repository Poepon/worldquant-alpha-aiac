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
  InputNumber,
  Modal,
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
  HistoryOutlined,
  ReloadOutlined,
  TrophyOutlined,
  LineChartOutlined,
  CloudUploadOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import api from '../../services/api'
import { formatRelative, formatDateTime } from '../../utils/time'
import { STATUS_COLORS, STATUS_LABELS } from '../../utils/alphaStatus'
import HeroMetrics from './HeroMetrics'
import CanSubmitTag from './CanSubmitTag'
import CrisisCorrelationPanel from './CrisisCorrelationPanel'

const { Title, Text, Paragraph } = Typography

export default function AlphaDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data: alpha, isLoading } = useQuery({
    queryKey: ['alpha', id],
    queryFn: () => api.getAlpha(id),
  })

  const { data: transitionsResp, isLoading: transLoading } = useQuery({
    queryKey: ['alpha', id, 'transitions'],
    queryFn: () => api.getAlphaTransitions(id, 50),
    enabled: !!id,
  })

  const { data: pnlResp, isLoading: pnlLoading } = useQuery({
    queryKey: ['alpha', id, 'pnl'],
    queryFn: () => api.getAlphaPnl(id),
    enabled: !!id,
  })

  // IQC marginal contribution — lazy (BRAIN poll can be slow). State lifted here
  // so the decision card and the marginal tab share one fetch.
  const [marginalEnabled, setMarginalEnabled] = useState(false)
  const [marginalCompetition, setMarginalCompetition] = useState('')
  const {
    data: marginal,
    isLoading: marginalLoading,
    error: marginalError,
    refetch: refetchMarginal,
  } = useQuery({
    queryKey: ['alpha', id, 'marginal', marginalCompetition],
    queryFn: () =>
      api.getAlphaMarginalContribution(id, {
        competition: marginalCompetition || undefined,
      }),
    enabled: marginalEnabled && !!alpha?.alpha_id,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

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
        message.error(`⚠️ 不可提交：${data.failed_checks.length} 项检查未通过`)
      }
      queryClient.invalidateQueries(['alpha', id])
    },
    onError: (e) => message.error(`刷新失败：${e?.message || e}`),
  })

  // Submit to BRAIN. Server runs pre-flight gates; a gate failure returns
  // { submitted:false, reason } with HTTP 200 (not an error).
  const submitMutation = useMutation({
    mutationFn: () => api.submitAlpha(id),
    onSuccess: (data) => {
      if (data.submitted) {
        message.success('✅ 已提交至 BRAIN')
      } else {
        message.warning(`未提交：${data.reason || '未知原因'}`)
      }
      queryClient.invalidateQueries(['alpha', id])
    },
    onError: (e) =>
      message.error(`提交失败：${e?.response?.data?.detail || e?.message || e}`),
  })

  // Blueprint optimization — run a settings-sweep cycle using THIS alpha as
  // the template (trigger_source='manual'). Winners land in submit-backlog.
  const [optimizeOpen, setOptimizeOpen] = useState(false)
  const [optimizeBudget, setOptimizeBudget] = useState(null)
  const optimizeMutation = useMutation({
    mutationFn: () => api.optimizeAlphaFromBlueprint(id, { budget: optimizeBudget }),
    onSuccess: (data) => {
      setOptimizeOpen(false)
      message.success(
        `已启动优化：生成 ${data.n_variants ?? '?'} 个变体（预算 ${data.budget}）。` +
          `胜出变体将进入「提交积压」队列；进度见 运维 → 优化周期。`,
        8,
      )
    },
    onError: (e) => {
      const detail = e?.response?.data?.detail || e?.message || e
      message.error(`启动优化失败：${detail}`)
    },
  })

  const handleFeedback = (rating) => feedbackMutation.mutate({ rating })

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
        <Button onClick={() => navigate('/alphas')}>返回列表</Button>
      </Empty>
    )
  }

  const metrics = alpha.metrics || {}
  const transitions = transitionsResp?.transitions || []
  const analysis = marginal?.analysis

  // PnL series → cumulative-PnL line chart.
  const pnlData = (pnlResp?.points || []).map((p) => ({
    date: (p.trade_date || '').slice(0, 10),
    cum: p.cumulative_pnl,
  }))

  const alreadySubmitted = !!alpha.date_submitted
  const submitDisabled =
    alreadySubmitted || alpha.can_submit !== true || !alpha.alpha_id || submitMutation.isPending
  const submitDisabledReason = alreadySubmitted
    ? '已提交至 BRAIN'
    : !alpha.alpha_id
      ? '该 alpha 无 BRAIN ID，无法提交'
      : alpha.can_submit !== true
        ? '需先确认可提交（点击右侧刷新校验）'
        : null

  return (
    <div>
      {/* Header */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Space wrap>
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
            <Tag color={STATUS_COLORS[alpha.quality_status] || 'default'}>
              {STATUS_LABELS[alpha.quality_status] || alpha.quality_status}
            </Tag>
            {alpha.region && <Tag>{alpha.region}</Tag>}
            {alreadySubmitted ? (
              <AntTooltip title={`已提交至 BRAIN：${formatDateTime(alpha.date_submitted)}`}>
                <Tag color="green">✅ 已提交</Tag>
              </AntTooltip>
            ) : (
              <Tag>⚪ 未提交</Tag>
            )}
          </Space>
        </Col>
      </Row>

      {/* Hero metric strip */}
      <HeroMetrics alpha={alpha} />

      {/* Submit decision — the point of this page */}
      <Card
        className="glass-card"
        style={{ marginBottom: 16 }}
        title={
          <Space>
            <CloudUploadOutlined />
            <span>提交决策</span>
          </Space>
        }
      >
        <Row gutter={[16, 16]} align="middle">
          <Col xs={24} md={14}>
            <Space wrap size={12}>
              <CanSubmitTag
                canSubmit={alpha.can_submit}
                failed={metrics._brain_failed_checks || []}
                pending={metrics._brain_pending_checks || []}
                loading={refreshCanSubmitMutation.isPending}
                onRefresh={() => refreshCanSubmitMutation.mutate()}
              />
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={refreshCanSubmitMutation.isPending}
                onClick={() => refreshCanSubmitMutation.mutate()}
              >
                刷新校验
              </Button>
              {alreadySubmitted && (
                <Text type="secondary">
                  已于 {formatDateTime(alpha.date_submitted)} 提交
                </Text>
              )}
            </Space>
          </Col>
          <Col xs={24} md={10} style={{ textAlign: 'right' }}>
            <Space wrap>
              <AntTooltip title="以该 alpha 为蓝本，对 decay/窗口/中性化 做设置扫描优化（消耗 BRAIN 配额）">
                <Button
                  icon={<ExperimentOutlined />}
                  loading={optimizeMutation.isPending}
                  onClick={() => setOptimizeOpen(true)}
                >
                  以此为蓝本优化
                </Button>
              </AntTooltip>
              <AntTooltip title={submitDisabledReason || '提交至 BRAIN（不可逆，消耗配额）'}>
                <Button
                  type="primary"
                  icon={<CloudUploadOutlined />}
                  disabled={submitDisabled}
                  loading={submitMutation.isPending}
                  onClick={() => submitMutation.mutate()}
                >
                  提交至 BRAIN
                </Button>
              </AntTooltip>
            </Space>
          </Col>
        </Row>

        {/* Marginal recommendation summary — appears once fetched in the tab */}
        {analysis && (
          <>
            <Divider style={{ margin: '12px 0' }} />
            <Space wrap size={8}>
              <Text type="secondary">边际建议:</Text>
              <Tag
                color={
                  { SUBMIT: 'success', SKIP: 'error', NEUTRAL: 'warning' }[
                    analysis.recommendation
                  ] || 'default'
                }
              >
                {analysis.label}
              </Tag>
              {analysis.composite_score != null && (
                <Tag color={analysis.composite_score > 0 ? 'green' : analysis.composite_score < 0 ? 'red' : 'default'}>
                  综合 {analysis.composite_score > 0 ? '+' : ''}{analysis.composite_score}
                </Tag>
              )}
              {analysis.margin_bps != null && (
                <Tag color={analysis.margin_bps < 0 ? 'red' : analysis.margin_bps < 5 ? 'orange' : 'blue'}>
                  Margin {analysis.margin_bps}bps
                </Tag>
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>（详见下方「边际贡献」）</Text>
            </Space>
          </>
        )}
        {!analysis && !alreadySubmitted && (
          <>
            <Divider style={{ margin: '12px 0' }} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              ↓ 在下方「边际贡献」拉取 BRAIN 加入组合前后的对比数据，获取「建议提交 / 中性 / 建议跳过」建议
            </Text>
          </>
        )}
      </Card>

      {/* Expression + analysis (left) · metadata + crisis (right) */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card
            className="glass-card"
            title="表达式"
            extra={
              <Button icon={<CopyOutlined />} size="small" onClick={copyExpression}>
                复制
              </Button>
            }
          >
            <pre style={{ fontSize: 14, lineHeight: 1.6, overflow: 'auto', maxHeight: 220, margin: 0 }}>
              {alpha.expression}
            </pre>
          </Card>

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
        </Col>

        <Col xs={24} lg={10}>
          <Card className="glass-card" title="元数据">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="BRAIN Alpha ID">
                {alpha.alpha_id ? (
                  <Text code copyable={{ text: alpha.alpha_id, tooltips: ['复制', '已复制'] }}>
                    {alpha.alpha_id}
                  </Text>
                ) : (
                  <Text type="secondary">未提交至 BRAIN</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="地区 / 股票池">
                {alpha.region} · {alpha.universe}
              </Descriptions.Item>
              <Descriptions.Item label="数据集">{alpha.dataset_id || '—'}</Descriptions.Item>
              <Descriptions.Item label="使用字段">
                <Space wrap size={[4, 4]}>
                  {(alpha.fields_used || []).length
                    ? alpha.fields_used.map((f) => <Tag key={f}>{f}</Tag>)
                    : '—'}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="使用算子">
                <Space wrap size={[4, 4]}>
                  {(alpha.operators_used || []).length
                    ? alpha.operators_used.map((o) => <Tag key={o} color="blue">{o}</Tag>)
                    : '—'}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">
                <AntTooltip title={formatDateTime(alpha.created_at)}>
                  <span>{formatRelative(alpha.created_at)}</span>
                </AntTooltip>
              </Descriptions.Item>
            </Descriptions>
          </Card>

          <Card
            className="glass-card"
            title={
              <Space>
                <span>危机窗口相关性</span>
                <AntTooltip title="该 alpha 在 4 个历史危机窗口下，与样本外策略池的最高相关度。任一窗口 ≥ 0.7（红）提示隐性集中度风险，慎重提交。">
                  <Tag color="purple">压力测试</Tag>
                </AntTooltip>
              </Space>
            }
            style={{ marginTop: 16 }}
          >
            <CrisisCorrelationPanel crisis={metrics?._crisis_correlations} />
          </Card>
        </Col>
      </Row>

      {/* Tabs: marginal contribution · transitions · PnL curve */}
      <Card className="glass-card" style={{ marginTop: 16 }}>
        <Tabs
          defaultActiveKey="marginal"
          items={[
            {
              key: 'marginal',
              label: (
                <Space>
                  <TrophyOutlined />
                  边际贡献
                  {alpha.can_submit && <Tag color="green">可提交</Tag>}
                </Space>
              ),
              children: (
                <MarginalPanel
                  alpha={alpha}
                  marginal={marginal}
                  loading={marginalLoading}
                  error={marginalError}
                  enabled={marginalEnabled}
                  competition={marginalCompetition}
                  setCompetition={setMarginalCompetition}
                  onFetch={() => {
                    setMarginalEnabled(true)
                    if (marginalEnabled) refetchMarginal()
                  }}
                />
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
                            <Tag color={STATUS_COLORS[t.old_status]}>{STATUS_LABELS[t.old_status] || t.old_status}</Tag>
                          )}
                          <span>→</span>
                          <Tag color={STATUS_COLORS[t.new_status]}>{STATUS_LABELS[t.new_status] || t.new_status}</Tag>
                          {t.sharpe_at_transition != null && (
                            <Text type="secondary">
                              当时 Sharpe={t.sharpe_at_transition.toFixed(2)}
                            </Text>
                          )}
                        </Space>
                        {t.reason && <Text type="secondary">{t.reason}</Text>}
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
            {
              key: 'pnl',
              label: (
                <Space>
                  <LineChartOutlined />
                  收益曲线
                  {pnlData.length > 0 && <Tag>{pnlData.length}d</Tag>}
                </Space>
              ),
              children: pnlLoading ? (
                <Spin />
              ) : pnlData.length === 0 ? (
                <Empty description="尚无 PnL 数据（挖掘 / 同步命中本地缓存后落库）" />
              ) : (
                <ResponsiveContainer width="100%" height={320}>
                  <LineChart data={pnlData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                    <XAxis dataKey="date" stroke="rgba(255,255,255,0.5)" minTickGap={40} />
                    <YAxis stroke="rgba(255,255,255,0.5)" width={70} />
                    <Tooltip
                      contentStyle={{
                        background: '#131a2b',
                        border: '1px solid rgba(255,255,255,0.1)',
                        borderRadius: 8,
                      }}
                      formatter={(v) => [Number(v).toLocaleString(), '累计 PnL']}
                    />
                    <Line type="monotone" dataKey="cum" stroke="#00ff88" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ),
            },
          ]}
        />
      </Card>

      {/* Human feedback */}
      <Card
        className="glass-card"
        style={{ marginTop: 16, borderColor: alpha.human_feedback === 'NONE' ? '#faad14' : undefined }}
        title={
          <Space>
            <span>人工反馈</span>
            {alpha.human_feedback === 'NONE' && <Tag color="gold">需要你的评价</Tag>}
          </Space>
        }
      >
        {alpha.human_feedback === 'NONE' && (
          <Paragraph type="secondary" style={{ marginBottom: 16 }}>
            你的反馈会直接进知识库：👍 喜欢 会把它升级为「成功经验」（提高置信度），
            👎 不喜欢 会削弱已知模式（降低置信度）。每条评价都会被下一轮挖掘学习。
          </Paragraph>
        )}
        <Space size="middle" wrap>
          <Text>当前评价:</Text>
          {alpha.human_feedback === 'LIKED' && <Tag icon={<LikeOutlined />} color="success">喜欢</Tag>}
          {alpha.human_feedback === 'DISLIKED' && <Tag icon={<DislikeOutlined />} color="error">不喜欢</Tag>}
          {alpha.human_feedback === 'NONE' && <Text type="secondary">未评价</Text>}
        </Space>
        <div style={{ marginTop: 16 }}>
          <Space size="middle" wrap>
            <Button
              size="large"
              icon={<LikeOutlined />}
              type={alpha.human_feedback === 'LIKED' ? 'primary' : 'default'}
              onClick={() => handleFeedback('LIKED')}
              loading={feedbackMutation.isPending}
              style={alpha.human_feedback === 'NONE' ? { boxShadow: '0 0 0 2px #52c41a44' } : undefined}
            >
              👍 点赞
            </Button>
            <Button
              size="large"
              icon={<DislikeOutlined />}
              danger={alpha.human_feedback === 'DISLIKED'}
              onClick={() => handleFeedback('DISLIKED')}
              loading={feedbackMutation.isPending}
            >
              👎 踩
            </Button>
          </Space>
        </div>
        {alpha.feedback_comment && (
          <>
            <Divider />
            <Text strong>评论:</Text>
            <Paragraph style={{ marginTop: 8 }}>{alpha.feedback_comment}</Paragraph>
          </>
        )}
      </Card>

      {/* Blueprint-optimization modal — one-click settings sweep on this alpha */}
      <Modal
        title={
          <Space>
            <ExperimentOutlined />
            <span>以此 alpha 为蓝本优化</span>
          </Space>
        }
        open={optimizeOpen}
        onOk={() => optimizeMutation.mutate()}
        onCancel={() => setOptimizeOpen(false)}
        okText="开始优化"
        cancelText="取消"
        confirmLoading={optimizeMutation.isPending}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="参数扫描优化（第一阶段）"
            description="以该 alpha 表达式为蓝本，对衰减 / 时间窗口 / 中性化 做最多 10 个参数变体，在 BRAIN 上回测。胜出变体落库并进入「提交积压」队列等待人工提交——不会自动提交。与每 6 小时的自动定时任务相互独立。"
          />
          <div>
            <Text type="secondary">BRAIN 回测配额（可选，留空 = 默认覆盖全部变体）:</Text>
            <br />
            <InputNumber
              min={1}
              max={30}
              step={1}
              placeholder="默认 16"
              value={optimizeBudget}
              onChange={setOptimizeBudget}
              style={{ width: 160, marginTop: 6 }}
            />
          </div>
          <Text type="warning" style={{ fontSize: 12 }}>
            ⚠ 本操作消耗 BRAIN 回测配额；优化周期在后台运行，完成情况见 运维 → 优化周期。
          </Text>
        </Space>
      </Modal>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Marginal-contribution panel (BRAIN before-and-after). Fetch is lazy and
// owned by the parent so the decision card can mirror the recommendation.
// ---------------------------------------------------------------------------
function MarginalPanel({ alpha, marginal, loading, error, enabled, competition, setCompetition, onFetch }) {
  const analysis = marginal?.analysis
  return (
    <div>
      <Alert
        type="info"
        message="BRAIN 加入组合前后的表现对比"
        description={
          <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
            <span>
              独立运行 vs 并入组合 的 Sharpe / Fitness / 换手率
              对比 — 决定是否值得提交。
            </span>
            <span style={{ color: '#888' }}>
              竞赛范围下含「评分变化」；以并入组合后的指标增量衡量边际贡献。换手率 / 回撤 越低越好。
            </span>
          </Space>
        }
        style={{ marginBottom: 16 }}
        showIcon
      />
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="竞赛 ID（留空=默认范围）"
          value={competition}
          onChange={(e) => setCompetition(e.target.value)}
          style={{ width: 220 }}
        />
        <Button
          type="primary"
          icon={<ReloadOutlined />}
          loading={loading}
          disabled={!alpha?.alpha_id}
          onClick={onFetch}
        >
          拉取 BRAIN 数据
        </Button>
        {!alpha?.alpha_id && <Text type="warning">该 alpha 无 BRAIN ID，不能拉取</Text>}
      </Space>

      {error && (
        <Alert type="error" message="拉取失败" description={error.message || '未知错误'} style={{ marginBottom: 16 }} />
      )}
      {loading && <Spin tip="BRAIN 计算中(可能 5-20s)..." />}

      {marginal && (
        <>
          {analysis && (
            <Alert
              type={{ SUBMIT: 'success', SKIP: 'error', NEUTRAL: 'warning' }[analysis.recommendation] || 'info'}
              showIcon
              style={{ marginBottom: 16 }}
              message={
                <Space wrap>
                  <Text strong style={{ fontSize: 15 }}>{analysis.label}</Text>
                  {analysis.composite_score != null && (
                    <Tag color={analysis.composite_score > 0 ? 'green' : analysis.composite_score < 0 ? 'red' : 'default'}>
                      综合边际评分 {analysis.composite_score > 0 ? '+' : ''}{analysis.composite_score}
                    </Tag>
                  )}
                  {analysis.margin_bps != null && (
                    <Tag color={analysis.margin_bps < 0 ? 'red' : analysis.margin_bps < 5 ? 'orange' : 'blue'}>
                      Margin {analysis.margin_bps}bps（门槛 5bps）
                    </Tag>
                  )}
                </Space>
              }
              description={
                <Space direction="vertical" size={6} style={{ fontSize: 12, width: '100%' }}>
                  {analysis.rationale && <Text>{analysis.rationale}</Text>}
                  {(analysis.guardrails || []).length > 0 && (
                    <div>
                      {analysis.guardrails.map((g, i) => (
                        <div key={i} style={{ color: '#cf1322' }}>⚠ {g}</div>
                      ))}
                    </div>
                  )}
                  <Row gutter={16}>
                    <Col span={12}>
                      <Text strong style={{ color: '#389e0d' }}>
                        ✓ 正向贡献 ({(analysis.positives || []).length})
                      </Text>
                      {(analysis.positives || []).length === 0 ? (
                        <div style={{ color: '#999' }}>—</div>
                      ) : (
                        analysis.positives.map((p) => <div key={p.metric}>· {p.text}</div>)
                      )}
                    </Col>
                    <Col span={12}>
                      <Text strong style={{ color: '#cf1322' }}>
                        ✗ 负向拖累 ({(analysis.negatives || []).length})
                      </Text>
                      {(analysis.negatives || []).length === 0 ? (
                        <div style={{ color: '#999' }}>—</div>
                      ) : (
                        analysis.negatives.map((n) => <div key={n.metric}>· {n.text}</div>)
                      )}
                    </Col>
                  </Row>
                  {(analysis.reference || []).length > 0 && (
                    <div style={{ color: '#888' }}>
                      参考（不计入评分）：{analysis.reference.map((r) => r.text).join('；')}
                    </div>
                  )}
                </Space>
              }
            />
          )}
          <Descriptions title={`范围：${marginal.scope}`} bordered size="small" column={1} style={{ marginBottom: 16 }}>
            {marginal.raw?.score != null && (
              <Descriptions.Item label="竞赛评分变化（排名分，越高越好）">
                <Space size={8} wrap>
                  <Text type="secondary">加入前: {Number(marginal.raw.score.before).toLocaleString()}</Text>
                  <Text strong>加入后: {Number(marginal.raw.score.after).toLocaleString()}</Text>
                  {marginal.deltas?.score != null && (
                    <Tag color={marginal.deltas.score >= 0 ? 'green' : 'red'}>
                      变化 {marginal.deltas.score > 0 ? '+' : ''}{Number(marginal.deltas.score).toLocaleString()}
                    </Tag>
                  )}
                </Space>
              </Descriptions.Item>
            )}
            <Descriptions.Item label="分区">
              <Text code>{marginal.partition_name ?? marginal.raw?.partitionName ?? '—'}</Text>
            </Descriptions.Item>
          </Descriptions>
          <Row gutter={16}>
            {['sharpe', 'fitness', 'margin', 'returns', 'pnl', 'turnover', 'drawdown'].map((k) => {
              const METRIC_LABELS = {
                sharpe: 'Sharpe', fitness: 'Fitness', margin: 'Margin',
                returns: '收益', pnl: '盈亏 PnL', turnover: '换手率', drawdown: '回撤',
              }
              const before = marginal.raw?.stats?.before?.[k]
              const after = marginal.raw?.stats?.after?.[k]
              const delta = marginal.deltas?.[k]
              const isMoney = k === 'pnl'
              const fmt = (v) =>
                typeof v === 'number' ? (isMoney ? v.toLocaleString() : v.toFixed(4)) : '—'
              return (
                <Col span={8} key={k} style={{ marginBottom: 12 }}>
                  <Card size="small" title={METRIC_LABELS[k] || k.toUpperCase()}>
                    <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
                      <Text type="secondary">加入前: {fmt(before)}</Text>
                      <Text strong>加入后: {fmt(after)}</Text>
                      {delta != null && (
                        <Tag
                          color={
                            (k === 'turnover' || k === 'drawdown')
                              ? (delta <= 0 ? 'green' : 'red')
                              : (delta >= 0 ? 'green' : 'red')
                          }
                        >
                          变化 {delta > 0 ? '+' : ''}{fmt(delta)}
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
      {!enabled && !loading && !marginal && (
        <Empty description="点击「拉取 BRAIN 数据」开始，首次调用 BRAIN 可能 5-20s" />
      )}
    </div>
  )
}
