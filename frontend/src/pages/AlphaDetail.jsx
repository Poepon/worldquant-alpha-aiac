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
  Statistic,
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
  HistoryOutlined,
  ReloadOutlined,
  TrophyOutlined,
  LineChartOutlined,
  CloudUploadOutlined,
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
import api from '../services/api'
import { formatRelative, formatDateTime } from '../utils/time'
import { STATUS_COLORS } from '../utils/alphaStatus'

const { Title, Text, Paragraph } = Typography

const CRISIS_WINDOW_LABELS = {
  covid_2020: 'COVID 2020',
  rate_shock_2022: '利率冲击 2022',
  svb_2023: 'SVB 2023',
  tariff_2025: '关税 2025',
}

// Pull a numeric metric, preferring alpha.metrics then is_metrics. Returns null
// for missing / NaN so renderers can show an em-dash instead of "NaN".
function pickMetric(alpha, key) {
  const m = alpha?.metrics || {}
  const ism = alpha?.is_metrics || {}
  const a = m[key]
  if (typeof a === 'number' && !Number.isNaN(a)) return a
  const b = ism[key]
  if (typeof b === 'number' && !Number.isNaN(b)) return b
  return null
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

// ---------------------------------------------------------------------------
// Hero metric strip — headline IS metrics at the top of the page.
// returns/drawdown are ratios (×100 → %), margin is a ratio (×10000 → bps),
// self_corr / sharpe / fitness / turnover are shown raw.
// ---------------------------------------------------------------------------
function HeroMetrics({ alpha }) {
  const sharpe = pickMetric(alpha, 'sharpe')
  const fitness = pickMetric(alpha, 'fitness')
  const returns = pickMetric(alpha, 'returns')
  const turnover = pickMetric(alpha, 'turnover')
  const drawdown = pickMetric(alpha, 'drawdown')
  const margin = pickMetric(alpha, 'margin')
  const m = alpha.metrics || {}
  const selfCorr =
    typeof m._self_corr === 'number'
      ? m._self_corr
      : typeof m.selfCorrelation === 'number'
        ? m.selfCorrelation
        : null

  const tiles = [
    {
      key: 'sharpe', title: 'Sharpe', tip: '年化超额收益 / 年化波动率',
      value: sharpe, precision: 2,
      color: sharpe == null ? undefined : sharpe >= 1.5 ? '#3f8600' : sharpe >= 1.0 ? '#d48806' : '#cf1322',
    },
    {
      key: 'fitness', title: 'Fitness', tip: 'BRAIN 综合评分 (Sharpe × √收益 / √换手率)',
      value: fitness, precision: 2,
      color: fitness == null ? undefined : fitness >= 1.0 ? '#3f8600' : '#d48806',
    },
    {
      key: 'returns', title: '年化收益', tip: '年化收益率',
      value: returns == null ? null : returns * 100, precision: 1, suffix: '%',
    },
    {
      key: 'turnover', title: '换手率', tip: '日均持仓变化比例 (越低交易成本越小)',
      value: turnover, precision: 2,
    },
    {
      key: 'drawdown', title: '最大回撤', tip: '回测期内最大回撤',
      value: drawdown == null ? null : drawdown * 100, precision: 1, suffix: '%',
      color: drawdown == null ? undefined : '#cf1322',
    },
    {
      key: 'margin', title: 'Margin', tip: '每单位交易利润；约 5bps 为盈亏成本线',
      value: margin == null ? null : margin * 10000, precision: 1, suffix: ' bps',
      color: margin == null ? undefined : margin < 0 ? '#cf1322' : margin * 10000 < 5 ? '#d48806' : '#3f8600',
    },
    {
      key: 'self_corr', title: '自相关', tip: '与已提交 alpha 的最高相关度；> 0.7 不可提交',
      value: selfCorr, precision: 2,
      color: selfCorr == null ? undefined : selfCorr > 0.7 ? '#cf1322' : selfCorr > 0.5 ? '#d48806' : '#3f8600',
    },
  ]

  return (
    <Card className="glass-card" style={{ marginBottom: 16 }}>
      <Row gutter={[16, 16]}>
        {tiles.map((t) => (
          <Col key={t.key} xs={12} sm={8} md={6} lg={3} style={{ minWidth: 104 }}>
            <Statistic
              title={<AntTooltip title={t.tip}><span>{t.title}</span></AntTooltip>}
              value={t.value == null ? '—' : t.value}
              precision={t.value == null ? undefined : t.precision}
              suffix={t.value == null ? undefined : t.suffix}
              valueStyle={{ color: t.color, fontSize: 20 }}
            />
          </Col>
        ))}
      </Row>
    </Card>
  )
}

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
        message.error(`⚠️ 不可提交：${data.failed_checks.length} 个 FAIL`)
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
              {alpha.quality_status}
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
              ↓ 在下方「边际贡献」拉取 BRAIN before-and-after 数据，获取 SUBMIT/NEUTRAL/SKIP 建议
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
                <AntTooltip title="该 alpha 在 4 个历史危机窗口下相对 OS 池的 max-corr。任一窗口 ≥ 0.7 (红) 提示隐性集中度风险，慎重提交。">
                  <Tag color="purple">stress test</Tag>
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
                            <Tag color={STATUS_COLORS[t.old_status]}>{t.old_status}</Tag>
                          )}
                          <span>→</span>
                          <Tag color={STATUS_COLORS[t.new_status]}>{t.new_status}</Tag>
                          {t.sharpe_at_transition != null && (
                            <Text type="secondary">
                              sharpe@trans={t.sharpe_at_transition.toFixed(2)}
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
            你的反馈会直接进知识库：👍 LIKED 升级为 SUCCESS_PATTERN（+confidence），
            👎 DISLIKED 削弱已知模式（-confidence）。每条评价都会被下一轮 mining 学习。
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
              👍 点赞 (LIKED)
            </Button>
            <Button
              size="large"
              icon={<DislikeOutlined />}
              danger={alpha.human_feedback === 'DISLIKED'}
              onClick={() => handleFeedback('DISLIKED')}
              loading={feedbackMutation.isPending}
            >
              👎 踩 (DISLIKED)
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
        message="BRAIN before-and-after-performance"
        description={
          <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
            <span>
              Standalone(独立运行)vs Merged(并入组合)的 sharpe/fitness/turnover
              对比 — 决定是否值得提交。
            </span>
            <span style={{ color: '#888' }}>
              竞赛 scope 下含 Δscore；以 merged 后的 stats 增量衡量边际贡献。turnover/drawdown 越低越好。
            </span>
          </Space>
        }
        style={{ marginBottom: 16 }}
        showIcon
      />
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="competition ID（空=默认 scope）"
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
          <Descriptions title={`Scope: ${marginal.scope}`} bordered size="small" column={1} style={{ marginBottom: 16 }}>
            {marginal.raw?.score != null && (
              <Descriptions.Item label="竞赛 Δscore（排名分，越高越好）">
                <Space size={8} wrap>
                  <Text type="secondary">before: {Number(marginal.raw.score.before).toLocaleString()}</Text>
                  <Text strong>after: {Number(marginal.raw.score.after).toLocaleString()}</Text>
                  {marginal.deltas?.score != null && (
                    <Tag color={marginal.deltas.score >= 0 ? 'green' : 'red'}>
                      Δ {marginal.deltas.score > 0 ? '+' : ''}{Number(marginal.deltas.score).toLocaleString()}
                    </Tag>
                  )}
                </Space>
              </Descriptions.Item>
            )}
            <Descriptions.Item label="Partition">
              <Text code>{marginal.partition_name ?? marginal.raw?.partitionName ?? '—'}</Text>
            </Descriptions.Item>
          </Descriptions>
          <Row gutter={16}>
            {['sharpe', 'fitness', 'margin', 'returns', 'pnl', 'turnover', 'drawdown'].map((k) => {
              const before = marginal.raw?.stats?.before?.[k]
              const after = marginal.raw?.stats?.after?.[k]
              const delta = marginal.deltas?.[k]
              const isMoney = k === 'pnl'
              const fmt = (v) =>
                typeof v === 'number' ? (isMoney ? v.toLocaleString() : v.toFixed(4)) : '—'
              return (
                <Col span={8} key={k} style={{ marginBottom: 12 }}>
                  <Card size="small" title={k.toUpperCase()}>
                    <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
                      <Text type="secondary">before: {fmt(before)}</Text>
                      <Text strong>after: {fmt(after)}</Text>
                      {delta != null && (
                        <Tag
                          color={
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
      {!enabled && !loading && !marginal && (
        <Empty description="点击「拉取 BRAIN 数据」开始，首次调用 BRAIN 可能 5-20s" />
      )}
    </div>
  )
}
