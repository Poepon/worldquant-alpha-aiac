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
  Alert,
  Divider,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import CrisisCorrelationPanel from './CrisisCorrelationPanel'

const { Text } = Typography

export default function MarginalRiskPanel({ alpha, marginal, loading, error, enabled, competition, setCompetition, onFetch, crisis }) {
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

      <Divider style={{ margin: '20px 0 12px' }} />
      <Space style={{ marginBottom: 10 }}>
        <Text strong>危机窗口相关性</Text>
        <Tag color="purple">压力测试 · 隐性集中度风险</Tag>
      </Space>
      <CrisisCorrelationPanel crisis={crisis} />
    </div>
  )
}
