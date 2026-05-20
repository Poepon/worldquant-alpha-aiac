import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card,
  Table,
  Tag,
  Space,
  Typography,
  Input,
  Select,
  InputNumber,
  Button,
  Row,
  Col,
  Popconfirm,
  message,
  Tooltip as AntdTooltip,
} from 'antd'
import { EyeOutlined, ReloadOutlined, CloudSyncOutlined } from '@ant-design/icons'
import api from '../services/api'
import { formatRelative } from '../utils/time'

const { Title, Text } = Typography
const { Search } = Input

const STATUS_COLORS = {
  PASS: 'success',
  PASS_PROVISIONAL: 'gold',
  OPTIMIZE: 'processing',
  FAIL: 'default',
  PENDING: 'default',
  REJECT: 'error',
}

const STATUS_LABELS = {
  PASS: '通过',
  PASS_PROVISIONAL: '临时通过',
  OPTIMIZE: '待优化',
  FAIL: '失败',
  PENDING: '待处理',
  REJECT: '拒绝',
}

const REGIONS = ['USA', 'CHN', 'EUR', 'ASI', 'GLB', 'KOR', 'HKG', 'JPN']

export default function AlphaList() {
  const navigate = useNavigate()
  const [filters, setFilters] = useState({
    region: undefined,
    quality_status: undefined,
    min_sharpe: undefined,
    expression: '',
  })
  // Client-side filter: '' = all, 'submitted' = date_submitted not null,
  // 'submittable' = can_submit=true + not yet submitted, 'rejected' = can_submit=false
  const [submitFilter, setSubmitFilter] = useState('')
  const [sortBy, setSortBy] = useState('sharpe')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)

  const queryParams = {
    ...Object.fromEntries(
      Object.entries(filters).filter(([_, v]) => v !== undefined && v !== '' && v !== null),
    ),
    sort_by: sortBy,
    sort_order: 'desc',
    limit: pageSize,
    offset: (page - 1) * pageSize,
  }

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['alphas-list', queryParams],
    queryFn: () => api.getAlphas(queryParams),
    keepPreviousData: true,
    refetchInterval: 30_000,
  })

  const queryClient = useQueryClient()

  // Sync alphas from WorldQuant BRAIN. Fire-and-forget — POST /alphas/sync
  // dispatches the sync_user_alphas Celery task (IS + OS stages, ~minutes)
  // and returns immediately. We surface a "started" toast and auto-refetch
  // after a delay so the new rows surface without a manual refresh.
  const syncMutation = useMutation({
    mutationFn: () => api.syncAlphas(),
    onSuccess: () => {
      message.success('已启动 BRAIN 同步,后台运行中(约几分钟)。完成后列表会自动刷新')
      // Sync runs in the background; refetch a few times as it lands rows.
      setTimeout(() => queryClient.invalidateQueries(['alphas-list']), 30_000)
      setTimeout(() => queryClient.invalidateQueries(['alphas-list']), 120_000)
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message
      message.error(`同步启动失败: ${detail}`)
    },
  })

  const rawItems = data?.items || []
  const items = submitFilter
    ? rawItems.filter((a) => {
        if (submitFilter === 'submitted') return !!a.date_submitted
        if (submitFilter === 'submittable') return a.can_submit === true && !a.date_submitted
        if (submitFilter === 'rejected') return a.can_submit === false
        return true
      })
    : rawItems
  const total = data?.total || 0

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      render: (id) => <a onClick={() => navigate(`/alphas/${id}`)}>#{id}</a>,
    },
    {
      title: 'BRAIN 编号',
      dataIndex: 'alpha_id',
      width: 110,
      render: (aid) => aid ? <Text code style={{ fontSize: 11 }}>{aid}</Text> : '—',
    },
    {
      title: '表达式',
      dataIndex: 'expression',
      ellipsis: true,
      render: (expr) => (
        <AntdTooltip title={expr}>
          <Text code style={{ fontSize: 11 }}>
            {(expr || '').slice(0, 70)}{(expr || '').length > 70 ? '…' : ''}
          </Text>
        </AntdTooltip>
      ),
    },
    {
      title: '地区',
      dataIndex: 'region',
      width: 70,
      render: (r) => <Tag>{r}</Tag>,
    },
    {
      title: '数据集',
      dataIndex: 'dataset_id',
      width: 100,
      ellipsis: true,
      render: (d) => d ? <Tag color="cyan">{d}</Tag> : '—',
    },
    {
      title: '状态',
      dataIndex: 'quality_status',
      width: 130,
      render: (s) => <Tag color={STATUS_COLORS[s] || 'default'}>{STATUS_LABELS[s] || s}</Tag>,
    },
    {
      title: (
        <AntdTooltip title="Sharpe 比率 — 年化超额收益 / 年化波动率，衡量风险调整后收益">
          <span>Sharpe</span>
        </AntdTooltip>
      ),
      dataIndex: 'sharpe',
      width: 80,
      align: 'right',
      render: (v) => v != null ? <Text strong>{v.toFixed(2)}</Text> : '—',
    },
    {
      title: (
        <AntdTooltip title="Fitness — BRAIN 综合评分（Sharpe × √收益 / √换手率），越高越好">
          <span>Fitness</span>
        </AntdTooltip>
      ),
      dataIndex: 'fitness',
      width: 80,
      align: 'right',
      render: (v) => v != null ? v.toFixed(2) : '—',
    },
    {
      title: (
        <AntdTooltip title="换手率 — 日均持仓变化比例，越低交易成本越小">
          <span>换手率</span>
        </AntdTooltip>
      ),
      dataIndex: 'turnover',
      width: 80,
      align: 'right',
      render: (v) => v != null ? v.toFixed(2) : '—',
    },
    {
      title: (
        <AntdTooltip title="自相关性 — 与已提交 alpha 的最高相关度，> 0.7 不可提交">
          <span>自相关</span>
        </AntdTooltip>
      ),
      dataIndex: 'self_corr',
      width: 80,
      align: 'right',
      render: (v) => {
        if (v == null) return '—'
        const color = v > 0.7 ? '#cf1322' : v > 0.5 ? '#d48806' : '#389e0d'
        return <Text style={{ color }}>{v.toFixed(2)}</Text>
      },
    },
    {
      title: '提交状态',
      key: 'submit_state',
      width: 110,
      render: (_, row) => {
        if (row.date_submitted) {
          return (
            <AntdTooltip title={`已提交 ${new Date(row.date_submitted).toLocaleString()}`}>
              <Tag color="success">已提交</Tag>
            </AntdTooltip>
          )
        }
        if (row.can_submit === true) return <Tag color="processing">可提交</Tag>
        if (row.can_submit === false) return <Tag color="default">不可提交</Tag>
        return <Tag>未检</Tag>
      },
    },
    {
      title: '创建',
      dataIndex: 'created_at',
      width: 110,
      render: (t) => (
        <AntdTooltip title={t ? new Date(t).toLocaleString() : ''}>
          <Text type="secondary" style={{ fontSize: 11 }}>{formatRelative(t)}</Text>
        </AntdTooltip>
      ),
    },
    {
      title: '操作',
      width: 70,
      render: (_, row) => (
        <Button
          size="small"
          icon={<EyeOutlined />}
          onClick={() => navigate(`/alphas/${row.id}`)}
        >
          详情
        </Button>
      ),
    },
  ]

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>Alpha 列表</Title>
          <Text type="secondary">共 {total} 条 · 默认按 Sharpe 降序</Text>
        </Col>
        <Col>
          <Space>
            <Popconfirm
              title="同步 WorldQuant BRAIN Alphas"
              description="将从 BRAIN 拉取全部 alpha(IS + OS),后台运行约几分钟。确认同步?"
              okText="同步"
              cancelText="取消"
              onConfirm={() => syncMutation.mutate()}
            >
              <Button
                type="primary"
                icon={<CloudSyncOutlined />}
                loading={syncMutation.isLoading}
              >
                同步 BRAIN Alphas
              </Button>
            </Popconfirm>
            <Button
              icon={<ReloadOutlined />}
              loading={isFetching}
              onClick={() => refetch()}
            >
              刷新
            </Button>
          </Space>
        </Col>
      </Row>

      <Card className="glass-card" style={{ marginBottom: 16 }}>
        <Space wrap size={12}>
          <Space>
            <Text>地区:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 120 }}
              value={filters.region}
              onChange={(v) => { setFilters((f) => ({ ...f, region: v })); setPage(1) }}
              options={REGIONS.map((r) => ({ value: r, label: r }))}
            />
          </Space>
          <Space>
            <Text>状态:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 170 }}
              value={filters.quality_status}
              onChange={(v) => { setFilters((f) => ({ ...f, quality_status: v })); setPage(1) }}
              options={[
                { value: 'PASS', label: '通过' },
                { value: 'PASS_PROVISIONAL', label: '临时通过' },
                { value: 'OPTIMIZE', label: '待优化' },
                { value: 'FAIL', label: '失败' },
                { value: 'PENDING', label: '待处理' },
                { value: 'REJECT', label: '拒绝' },
              ]}
            />
          </Space>
          <Space>
            <Text>最小 Sharpe:</Text>
            <InputNumber
              placeholder="任意"
              step={0.1}
              style={{ width: 100 }}
              value={filters.min_sharpe}
              onChange={(v) => { setFilters((f) => ({ ...f, min_sharpe: v })); setPage(1) }}
            />
          </Space>
          <Space>
            <Text>提交:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 130 }}
              value={submitFilter || undefined}
              onChange={(v) => setSubmitFilter(v || '')}
              options={[
                { value: 'submitted', label: '已提交' },
                { value: 'submittable', label: '可提交 / 未提交' },
                { value: 'rejected', label: '不可提交' },
              ]}
            />
          </Space>
          <Space>
            <Text>排序:</Text>
            <Select
              style={{ width: 130 }}
              value={sortBy}
              onChange={(v) => { setSortBy(v); setPage(1) }}
              options={[
                { value: 'sharpe', label: 'sharpe' },
                { value: 'fitness', label: 'fitness' },
                { value: 'turnover', label: 'turnover' },
                { value: 'returns', label: 'returns' },
                { value: 'created_at', label: 'created_at' },
                { value: 'id', label: 'id' },
              ]}
            />
          </Space>
          <Search
            placeholder="搜索表达式 (substring)"
            allowClear
            enterButton
            style={{ width: 280 }}
            onSearch={(v) => { setFilters((f) => ({ ...f, expression: v })); setPage(1) }}
          />
        </Space>
      </Card>

      <Card className="glass-card">
        <Table
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={isLoading}
          size="small"
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: ['25', '50', '100'],
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps) },
          }}
          scroll={{ x: 1200 }}
        />
      </Card>
    </div>
  )
}
