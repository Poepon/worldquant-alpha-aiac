import { Card, Typography, Tag, Button, Space, Statistic, Divider, Tooltip as AntTooltip } from 'antd'
import {
  ReloadOutlined, CloudUploadOutlined, ExperimentOutlined, LikeOutlined, DislikeOutlined,
} from '@ant-design/icons'
import CanSubmitTag from './CanSubmitTag'
import { pickMetric } from './HeroMetrics'

const { Text } = Typography

const REC_COLOR = { SUBMIT: 'success', SKIP: 'error', NEUTRAL: 'warning' }

export default function DecisionRail({
  alpha,
  metrics,
  analysis,
  marginalLoading,
  marginalAutoPending,
  onFetchMarginal,
  refreshLoading,
  onRefreshCanSubmit,
  submitDisabled,
  submitDisabledReason,
  submitLoading,
  onSubmit,
  optimizeLoading,
  onOpenOptimize,
  feedbackLoading,
  onFeedback,
}) {
  const selfCorr =
    typeof metrics._self_corr === 'number' ? metrics._self_corr
      : typeof metrics.selfCorrelation === 'number' ? metrics.selfCorrelation : null
  const sharpe = pickMetric(alpha, 'sharpe')
  const margin = pickMetric(alpha, 'margin')
  const marginBps = margin == null ? null : margin * 10000

  return (
    <div style={{ position: 'sticky', top: 16 }}>
      <Card className="glass-card" title={<Space><CloudUploadOutlined />提交决策</Space>}>
        <Space wrap size={8}>
          <CanSubmitTag
            canSubmit={alpha.can_submit}
            failed={metrics._brain_failed_checks || []}
            pending={metrics._brain_pending_checks || []}
            loading={refreshLoading}
            onRefresh={onRefreshCanSubmit}
          />
          <Button size="small" icon={<ReloadOutlined />} loading={refreshLoading} onClick={onRefreshCanSubmit}>
            刷新校验
          </Button>
        </Space>

        <Divider style={{ margin: '12px 0' }} />

        <div style={{ fontSize: 11, color: '#8a93a6', marginBottom: 6 }}>边际建议</div>
        {analysis ? (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Tag color={REC_COLOR[analysis.recommendation] || 'default'} style={{ fontSize: 16, padding: '4px 12px' }}>
              {analysis.label}
            </Tag>
            <Space wrap size={6}>
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
            </Space>
            <Text type="secondary" style={{ fontSize: 11 }}>详见右侧「边际贡献 &amp; 风险」</Text>
          </Space>
        ) : marginalLoading || marginalAutoPending ? (
          <Space><ReloadOutlined spin /><Text type="secondary" style={{ fontSize: 12 }}>BRAIN 计算中（5-20s）…</Text></Space>
        ) : (
          <Button size="small" icon={<ReloadOutlined />} disabled={!alpha?.alpha_id} onClick={onFetchMarginal}>
            获取边际建议
          </Button>
        )}

        <Divider style={{ margin: '12px 0' }} />

        <Space size={24}>
          <Statistic title="Sharpe" value={sharpe == null ? '—' : sharpe} precision={sharpe == null ? undefined : 2}
            valueStyle={{ fontSize: 18, color: sharpe == null ? undefined : sharpe >= 1.5 ? '#3f8600' : sharpe >= 1.0 ? '#d48806' : '#cf1322' }} />
          <Statistic title="Margin" value={marginBps == null ? '—' : marginBps} precision={marginBps == null ? undefined : 1} suffix={marginBps == null ? undefined : 'bps'}
            valueStyle={{ fontSize: 18, color: marginBps == null ? undefined : marginBps < 0 ? '#cf1322' : marginBps < 5 ? '#d48806' : '#3f8600' }} />
          <Statistic title="自相关" value={selfCorr == null ? '—' : selfCorr} precision={selfCorr == null ? undefined : 2}
            valueStyle={{ fontSize: 18, color: selfCorr == null ? undefined : selfCorr > 0.7 ? '#cf1322' : selfCorr > 0.5 ? '#d48806' : '#3f8600' }} />
        </Space>

        <Divider style={{ margin: '12px 0' }} />

        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <AntTooltip title={submitDisabledReason || '提交至 BRAIN（不可逆，消耗配额）'}>
            <Button type="primary" block icon={<CloudUploadOutlined />} disabled={submitDisabled} loading={submitLoading} onClick={onSubmit}>
              提交至 BRAIN
            </Button>
          </AntTooltip>
          <AntTooltip title="以该 alpha 为蓝本，对 decay/窗口/中性化 做设置扫描优化（消耗 BRAIN 配额）">
            <Button block icon={<ExperimentOutlined />} loading={optimizeLoading} onClick={onOpenOptimize}>
              以此为蓝本优化
            </Button>
          </AntTooltip>
        </Space>
      </Card>

      <Card className="glass-card" style={{ marginTop: 12 }}
        title={<Space>人工反馈{alpha.human_feedback === 'NONE' && <Tag color="gold">需要你的评价</Tag>}</Space>}>
        {alpha.human_feedback === 'NONE' && (
          <Text type="secondary" style={{ display: 'block', marginBottom: 10, fontSize: 12 }}>
            评价会进知识库：👍 升级为「成功经验」，👎 削弱已知模式，下一轮挖掘学习。
          </Text>
        )}
        <Space size="middle" wrap>
          <Button icon={<LikeOutlined />} type={alpha.human_feedback === 'LIKED' ? 'primary' : 'default'}
            loading={feedbackLoading} onClick={() => onFeedback('LIKED')}>👍 点赞</Button>
          <Button icon={<DislikeOutlined />} danger={alpha.human_feedback === 'DISLIKED'}
            loading={feedbackLoading} onClick={() => onFeedback('DISLIKED')}>👎 踩</Button>
        </Space>
        {alpha.feedback_comment && (
          <Text style={{ display: 'block', marginTop: 10 }} type="secondary">「{alpha.feedback_comment}」</Text>
        )}
      </Card>
    </div>
  )
}
