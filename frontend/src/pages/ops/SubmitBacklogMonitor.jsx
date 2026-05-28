import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  SendOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * SubmitBacklogMonitor — /ops/submit-backlog (2026-05-28).
 *
 * The #1 strategic lever is draining the can_submit backlog (submittable but
 * unsubmitted alphas). This page surfaces that queue ranked by the persisted
 * IQC marginal verdict (alpha.metrics._iqc_marginal.recommendation), lets the
 * operator kick a one-pass re-audit (POST /ops/submit-backlog/scan — BRAIN
 * backed, worker-async), and batch-submit the SUBMIT-graded ones via the
 * existing /alphas/{id}/submit endpoint.
 *
 * Refetches every 30s so a running re-audit's verdicts appear as they land.
 */
const VERDICT_META = {
  SUBMIT: { color: 'success', label: '建议提交' },
  NEUTRAL: { color: 'gold', label: '中性' },
  SKIP: { color: 'error', label: '不建议' },
  UNKNOWN: { color: 'default', label: '数据不足' },
}

function verdictTag(verdict, pending) {
  if (pending || !verdict) return <Tag color="default">待扫描</Tag>
  const m = VERDICT_META[verdict] || { color: 'default', label: verdict }
  return <Tag color={m.color}>{m.label}</Tag>
}

export default function SubmitBacklogMonitor() {
  const qc = useQueryClient()
  const [region, setRegion] = useState(null)
  const [selectedRowKeys, setSelectedRowKeys] = useState([])
  const [submitting, setSubmitting] = useState(false)

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/submit-backlog', region],
    queryFn: () => api.getOpsSubmitBacklog(region),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  const scanMutation = useMutation({
    mutationFn: () => api.scanSubmitBacklog(200),
    onSuccess: (res) => {
      message.success(res?.message || `已入队 ${res?.enqueued ?? 0} 个边际审计任务`)
      qc.invalidateQueries({ queryKey: ['ops/submit-backlog'] })
    },
    onError: (e) =>
      message.error(e?.response?.data?.detail || e?.message || '扫描触发失败'),
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
        message="加载提交积压失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'}
      />
    )
  }

  const summary = data?.summary || {}
  const items = data?.items || []
  const total = summary.total ?? 0
  const audited = summary.audited ?? 0
  const pending = summary.pending ?? 0
  const progressPct = total > 0 ? Math.round((audited / total) * 100) : 0

  // Batch submit — only allow selecting non-submitted rows; recommend SUBMIT.
  const onBatchSubmit = async () => {
    const picked = items.filter((it) => selectedRowKeys.includes(it.alpha_pk))
    if (picked.length === 0) return
    setSubmitting(true)
    let ok = 0
    let fail = 0
    const reasons = []
    for (const it of picked) {
      try {
        const res = await api.submitAlpha(it.alpha_pk)
        if (res?.submitted) {
          ok += 1
        } else {
          fail += 1
          reasons.push(`#${it.alpha_pk}: ${res?.reason || '被拒'}`)
        }
      } catch (e) {
        fail += 1
        reasons.push(`#${it.alpha_pk}: ${e?.response?.data?.detail || e?.message || '错误'}`)
      }
    }
    setSubmitting(false)
    setSelectedRowKeys([])
    if (ok > 0) message.success(`成功提交 ${ok} 个`)
    if (fail > 0) {
      message.warning(`${fail} 个未提交：${reasons.slice(0, 3).join('；')}${reasons.length > 3 ? ' …' : ''}`)
    }
    qc.invalidateQueries({ queryKey: ['ops/submit-backlog'] })
  }

  const pickedSubmitCount = items.filter(
    (it) => selectedRowKeys.includes(it.alpha_pk) && it.verdict === 'SUBMIT',
  ).length

  const columns = [
    {
      title: 'Alpha',
      dataIndex: 'alpha_pk',
      key: 'alpha_pk',
      width: 150,
      render: (pk, r) => (
        <Space direction="vertical" size={0}>
          <Link to={`/alphas/${pk}`}>
            <Text code style={{ fontSize: 12 }}>{r.brain_id || pk}</Text>
          </Link>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {r.region || '—'}{r.universe ? ` · ${r.universe}` : ''}
          </Text>
        </Space>
      ),
    },
    {
      title: '推荐',
      dataIndex: 'verdict',
      key: 'verdict',
      width: 100,
      render: (v, r) => verdictTag(v, r.pending),
    },
    {
      title: (
        <Tooltip title="边际综合评分（marginal composite_score）— 同推荐档内排序依据">
          <Space size={4}>综合评分 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'composite',
      key: 'composite',
      width: 110,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(3) : '—'),
    },
    {
      title: (
        <Tooltip title="alpha 自身 margin（bps）— ≥5bps 才扣成本盈利，是经济门">
          <Space size={4}>Margin <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'margin_bps',
      key: 'margin_bps',
      width: 100,
      align: 'right',
      render: (v) =>
        v !== null && v !== undefined ? (
          <Text type={v < 5 ? 'warning' : undefined}>{v.toFixed(1)} bps</Text>
        ) : '—',
    },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe',
      key: 'sharpe',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: 'Fitness',
      dataIndex: 'fitness',
      key: 'fitness',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: '换手',
      dataIndex: 'turnover',
      key: 'turnover',
      width: 90,
      align: 'right',
      render: (v) => (v !== null && v !== undefined ? `${(v * 100).toFixed(1)}%` : '—'),
    },
    {
      title: '审计时间 (UTC)',
      dataIndex: 'audited_at',
      key: 'audited_at',
      ellipsis: true,
      render: (v) => (v ? String(v).replace('T', ' ').slice(0, 19) : <Text type="secondary">未审计</Text>),
    },
  ]

  const rowSelection = {
    selectedRowKeys,
    onChange: setSelectedRowKeys,
    getCheckboxProps: (r) => ({ disabled: r.pending }),
  }

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <Title level={3} style={{ margin: 0 }}>
          <SendOutlined style={{ marginRight: 8 }} />
          提交积压抽干
        </Title>
        <Space wrap>
          <Text type="secondary">地区:</Text>
          <Select
            value={region}
            onChange={(v) => { setRegion(v); setSelectedRowKeys([]) }}
            style={{ width: 120 }}
            allowClear
            placeholder="全部"
            options={[
              { value: 'USA', label: 'USA' },
              { value: 'CHN', label: 'CHN' },
              { value: 'EUR', label: 'EUR' },
              { value: 'HKG', label: 'HKG' },
              { value: 'JPN', label: 'JPN' },
            ]}
          />
          <Popconfirm
            title="全量重新审计积压"
            description={`将对待扫描/陈旧的 alpha 逐个调 BRAIN 重算边际推荐（每个约 5-20s，消耗 BRAIN 配额）。worker 后台异步执行，确认触发？`}
            okText="确认扫描"
            cancelText="取消"
            onConfirm={() => scanMutation.mutate()}
          >
            <Button
              icon={<ThunderboltOutlined />}
              loading={scanMutation.isPending}
              type={pending > 0 ? 'primary' : 'default'}
            >
              扫描全部{pending > 0 ? `（${pending} 待扫描）` : ''}
            </Button>
          </Popconfirm>
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      {/* Progress + scan hint */}
      <Alert
        type={pending > 0 ? 'warning' : 'success'}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          <Space wrap align="center">
            <span>边际审计覆盖进度</span>
            <Progress
              percent={progressPct}
              size="small"
              style={{ width: 200 }}
              status={pending > 0 ? 'active' : 'success'}
            />
            <Text type="secondary">
              已审计 {audited} / {total}
              {pending > 0 ? `，${pending} 个待扫描（陈旧 schema 或未审计）` : '，全部已带推荐'}
            </Text>
          </Space>
        }
        description={
          pending > 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              「待扫描」是仍带旧赛季(IQC2026S1)审计或从未审计的 alpha — 点「扫描全部」用当前
              scope(IQC2026S2)+ 边际打分卡刷新出 SUBMIT/NEUTRAL/SKIP 推荐。周期 beat 也会逐批补,
              手动扫描更快。
            </Text>
          ) : null
        }
      />

      {/* KPI row */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="积压总数" value={total} valueStyle={{ color: '#00d4ff' }} />
            <Text type="secondary" style={{ fontSize: 12 }}>can_submit 且未提交</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="建议提交" value={summary.submit ?? 0} valueStyle={{ color: '#00ff88' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="中性" value={summary.neutral ?? 0} valueStyle={{ color: '#ffb700' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="不建议" value={summary.skip ?? 0} valueStyle={{ color: '#ff4d4f' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="待扫描" value={pending} valueStyle={{ color: '#9c88ff' }} />
          </Card>
        </Col>
        <Col xs={12} sm={6} lg={4}>
          <Card className="glass-card">
            <Statistic title="数据不足" value={summary.unknown ?? 0} valueStyle={{ color: '#888' }} />
          </Card>
        </Col>
      </Row>

      {/* Batch actions */}
      <Space style={{ marginTop: 16, marginBottom: 8 }} wrap>
        <Popconfirm
          title={`提交选中的 ${selectedRowKeys.length} 个 alpha`}
          description={
            pickedSubmitCount < selectedRowKeys.length
              ? `注意：选中项里有 ${selectedRowKeys.length - pickedSubmitCount} 个非「建议提交」档，仍会逐个尝试提交。`
              : '逐个调用 /alphas/{id}/submit 提交到 BRAIN。'
          }
          okText="确认提交"
          cancelText="取消"
          disabled={selectedRowKeys.length === 0 || submitting}
          onConfirm={onBatchSubmit}
        >
          <Button
            type="primary"
            icon={<SendOutlined />}
            disabled={selectedRowKeys.length === 0}
            loading={submitting}
          >
            提交选中（{selectedRowKeys.length}）
          </Button>
        </Popconfirm>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => qc.invalidateQueries({ queryKey: ['ops/submit-backlog'] })}
        >
          刷新
        </Button>
        {selectedRowKeys.length > 0 && (
          <Text type="secondary">
            其中「建议提交」档 {pickedSubmitCount} 个
          </Text>
        )}
      </Space>

      <Card className="glass-card" size="small">
        <Table
          size="small"
          rowKey="alpha_pk"
          rowSelection={rowSelection}
          dataSource={items}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: '无积压 alpha（can_submit 且未提交为空）' }}
        />
      </Card>
    </div>
  )
}
