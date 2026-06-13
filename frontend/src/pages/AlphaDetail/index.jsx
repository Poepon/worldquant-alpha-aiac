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
  Spin,
  Empty,
  InputNumber,
  Modal,
  message,
  Alert,
  Tabs,
  Tooltip as AntTooltip,
} from 'antd'
import {
  ArrowLeftOutlined,
  CopyOutlined,
  HistoryOutlined,
  TrophyOutlined,
  LineChartOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import api from '../../services/api'
import { formatDateTime } from '../../utils/time'
import { STATUS_COLORS, STATUS_LABELS } from '../../utils/alphaStatus'
import HeroMetrics from './HeroMetrics'
import DecisionRail from './DecisionRail'
import MarginalRiskPanel from './MarginalRiskPanel'
import PnlPanel from './PnlPanel'
import TransitionsPanel from './TransitionsPanel'
import DetailsPanel from './DetailsPanel'

const { Title, Text } = Typography

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
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/alphas')}>返回</Button>
            <Title level={3} style={{ margin: 0 }}>Alpha #{alpha.id}</Title>
            {alpha.alpha_id && (
              <AntTooltip title="点击复制 BRAIN Alpha ID">
                <Tag color="geekblue" style={{ cursor: 'pointer', fontFamily: 'monospace' }} onClick={copyBrainId} icon={<CopyOutlined />}>
                  BRAIN: {alpha.alpha_id}
                </Tag>
              </AntTooltip>
            )}
            <Tag color={STATUS_COLORS[alpha.quality_status] || 'default'}>
              {STATUS_LABELS[alpha.quality_status] || alpha.quality_status}
            </Tag>
            {alpha.region && <Tag>{alpha.region} · {alpha.universe}</Tag>}
            {alreadySubmitted
              ? <AntTooltip title={`已提交至 BRAIN：${formatDateTime(alpha.date_submitted)}`}><Tag color="green">✅ 已提交</Tag></AntTooltip>
              : <Tag>⚪ 未提交</Tag>}
          </Space>
        </Col>
      </Row>

      {/* Hero 指标条（全宽） */}
      <HeroMetrics alpha={alpha} />

      {/* 双栏：决策栏 | 诊断工作区 */}
      <Row gutter={[16, 16]} align="top">
        <Col xs={24} lg={7} xl={6}>
          <DecisionRail
            alpha={alpha}
            metrics={metrics}
            analysis={analysis}
            marginalLoading={marginalLoading}
            marginalAutoPending={marginalEnabled && !marginal && !marginalError}
            onFetchMarginal={() => { setMarginalEnabled(true); if (marginalEnabled) refetchMarginal() }}
            refreshLoading={refreshCanSubmitMutation.isPending}
            onRefreshCanSubmit={() => refreshCanSubmitMutation.mutate()}
            submitDisabled={submitDisabled}
            submitDisabledReason={submitDisabledReason}
            submitLoading={submitMutation.isPending}
            onSubmit={() => submitMutation.mutate()}
            optimizeLoading={optimizeMutation.isPending}
            onOpenOptimize={() => setOptimizeOpen(true)}
            feedbackLoading={feedbackMutation.isPending}
            onFeedback={handleFeedback}
          />
        </Col>

        <Col xs={24} lg={17} xl={18}>
          {/* 表达式常驻 */}
          <Card className="glass-card" title="表达式"
            extra={<Button icon={<CopyOutlined />} size="small" onClick={copyExpression}>复制</Button>}>
            <pre style={{ fontSize: 14, lineHeight: 1.6, overflow: 'auto', maxHeight: 200, margin: 0 }}>{alpha.expression}</pre>
          </Card>

          {/* 诊断 tab */}
          <Card className="glass-card" style={{ marginTop: 16 }}>
            <Tabs
              defaultActiveKey="marginal"
              items={[
                {
                  key: 'marginal',
                  label: <Space><TrophyOutlined />边际贡献 &amp; 风险{alpha.can_submit && <Tag color="green">可提交</Tag>}</Space>,
                  children: (
                    <MarginalRiskPanel
                      alpha={alpha}
                      marginal={marginal}
                      loading={marginalLoading}
                      error={marginalError}
                      enabled={marginalEnabled}
                      competition={marginalCompetition}
                      setCompetition={setMarginalCompetition}
                      onFetch={() => { setMarginalEnabled(true); if (marginalEnabled) refetchMarginal() }}
                      crisis={metrics?._crisis_correlations}
                    />
                  ),
                },
                {
                  key: 'pnl',
                  label: <Space><LineChartOutlined />收益曲线{pnlData.length > 0 && <Tag>{pnlData.length}d</Tag>}</Space>,
                  children: <PnlPanel pnlData={pnlData} loading={pnlLoading} />,
                },
                {
                  key: 'details',
                  label: <Space>详情</Space>,
                  children: <DetailsPanel alpha={alpha} />,
                },
                {
                  key: 'transitions',
                  label: <Space><HistoryOutlined />状态变迁{transitions.length > 0 && <Tag>{transitions.length}</Tag>}</Space>,
                  children: <TransitionsPanel transitions={transitions} loading={transLoading} />,
                },
              ]}
            />
          </Card>
        </Col>
      </Row>

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

