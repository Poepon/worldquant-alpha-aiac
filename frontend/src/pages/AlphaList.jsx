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
import {
  EyeOutlined,
  ReloadOutlined,
  CloudSyncOutlined,
  SafetyCertificateOutlined,
  TrophyOutlined,
  ClearOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import api from '../services/api'
import { formatRelative } from '../utils/time'
import { STATUS_COLORS, STATUS_LABELS } from '../utils/alphaStatus'

const { Title, Text } = Typography
const { Search } = Input

const REGIONS = ['USA', 'CHN', 'EUR', 'ASI', 'GLB', 'KOR', 'HKG', 'JPN']

const EMPTY_FILTERS = {
  region: undefined,
  quality_status: undefined,
  human_feedback: undefined,
  delay: undefined,
  min_sharpe: undefined,
  max_sharpe: undefined,
  min_fitness: undefined,
  max_turnover: undefined,
  min_returns: undefined,
  expression: '',
}

export default function AlphaList() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  // Submit-state is now a SERVER-side filter (submitted / submittable /
  // rejected / unchecked) so the pagination total stays honest.
  const [submitState, setSubmitState] = useState(undefined)
  const [sortBy, setSortBy] = useState('sharpe')
  const [sortOrder, setSortOrder] = useState('desc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  // Bumped on reset to force-remount the uncontrolled expression Search so its
  // text clears too (Select/InputNumber clear via controlled state already).
  const [resetKey, setResetKey] = useState(0)

  const queryParams = {
    ...Object.fromEntries(
      Object.entries(filters).filter(([_, v]) => v !== undefined && v !== '' && v !== null),
    ),
    ...(submitState ? { submit_state: submitState } : {}),
    sort_by: sortBy,
    sort_order: sortOrder,
    limit: pageSize,
    offset: (page - 1) * pageSize,
  }

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['alphas-list', queryParams],
    queryFn: () => api.getAlphas(queryParams),
    keepPreviousData: true,
    refetchInterval: 30_000,
  })

  // Summary strip — region-scoped portfolio overview (independent of the
  // table's metric/expression filters by design).
  const { data: stats } = useQuery({
    queryKey: ['alpha-stats', filters.region],
    queryFn: () => api.getAlphaStats(filters.region),
    refetchInterval: 60_000,
  })

  // Sync alphas from WorldQuant BRAIN. Fire-and-forget — POST /alphas/sync
  // dispatches the sync_user_alphas Celery task (IS + OS stages, ~minutes)
  // and returns immediately.
  const syncMutation = useMutation({
    mutationFn: () => api.syncAlphas(),
    onSuccess: () => {
      message.success('已启动 BRAIN 同步,后台运行中(约几分钟)。完成后列表会自动刷新')
      setTimeout(() => {
        queryClient.invalidateQueries(['alphas-list'])
        queryClient.invalidateQueries(['alpha-stats'])
      }, 30_000)
      setTimeout(() => {
        queryClient.invalidateQueries(['alphas-list'])
        queryClient.invalidateQueries(['alpha-stats'])
      }, 120_000)
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message
      message.error(`同步启动失败: ${detail}`)
    },
  })

  // Bulk re-check can_submit for PASS alphas (sequential, ~1 req/sec server-side).
  const refreshCanSubmitMutation = useMutation({
    mutationFn: () => api.refreshCanSubmitBatch({ quality_status: 'PASS', limit: 50 }),
    onSuccess: (res) => {
      message.success(
        `批量校验完成:扫描 ${res.scanned} · 可提交 ${res.pass_count} · 不可提交 ${res.fail_count} · 跳过 ${res.skipped}`,
      )
      queryClient.invalidateQueries(['alphas-list'])
      queryClient.invalidateQueries(['alpha-stats'])
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message
      message.error(`批量校验失败: ${detail}`)
    },
  })

  // Blueprint optimization — run a settings-sweep cycle using a chosen row's
  // alpha as the template (trigger_source='manual'). Fire-and-forget; winners
  // land in the submit-backlog. mutate(alphaId) — variables tracks which row.
  const optimizeMutation = useMutation({
    mutationFn: (alphaId) => api.optimizeAlphaFromBlueprint(alphaId),
    onSuccess: (data) => {
      message.success(
        `已启动优化 #${data.alpha_id}：${data.n_variants ?? '?'} 个变体` +
          `（预算 ${data.budget}）。胜出变体进入「提交积压」队列。`,
        6,
      )
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message
      message.error(`启动优化失败: ${detail}`)
    },
  })

  // Re-audit IQC marginal Δscore for the submittable tranche. Fire-and-forget.
  const refreshIqcMutation = useMutation({
    mutationFn: () => api.refreshFactorIqc({ scope: 'submittable', limit: 50 }),
    onSuccess: (res) => {
      message.success(res.message || `已触发 ${res.enqueued} 个 IQC 审计`)
      setTimeout(() => queryClient.invalidateQueries(['alphas-list']), 30_000)
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail || err.message
      message.error(`IQC 审计触发失败: ${detail}`)
    },
  })

  const items = data?.items || []
  const total = data?.total || 0

  const resetFilters = () => {
    setFilters(EMPTY_FILTERS)
    setSubmitState(undefined)
    setSortBy('sharpe')
    setSortOrder('desc')
    setPage(1)
    setResetKey((k) => k + 1)
  }

  const hasActiveFilters =
    submitState ||
    Object.entries(filters).some(([_, v]) => v !== undefined && v !== '' && v !== null)

  // Clicking a submit-state chip in the strip drives the server filter.
  const toggleSubmitState = (value) => {
    setSubmitState((cur) => (cur === value ? undefined : value))
    setPage(1)
  }

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      fixed: 'left',
      render: (id) => <a onClick={() => navigate(`/alphas/${id}`)}>#{id}</a>,
    },
    {
      title: 'BRAIN 编号',
      dataIndex: 'alpha_id',
      width: 110,
      render: (aid) => (aid ? <Text code style={{ fontSize: 11 }}>{aid}</Text> : '—'),
    },
    {
      title: '表达式',
      dataIndex: 'expression',
      ellipsis: true,
      render: (expr) => (
        <AntdTooltip title={expr}>
          <Text code style={{ fontSize: 11 }}>
            {(expr || '').slice(0, 70)}
            {(expr || '').length > 70 ? '…' : ''}
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
      render: (d) => (d ? <Tag color="cyan">{d}</Tag> : '—'),
    },
    {
      title: '状态',
      dataIndex: 'quality_status',
      width: 100,
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
      render: (v) => {
        if (v == null) return '—'
        const color = v >= 1.5 ? '#389e0d' : v >= 1.0 ? '#d48806' : undefined
        return <Text strong style={{ color }}>{v.toFixed(2)}</Text>
      },
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
      render: (v) => (v != null ? v.toFixed(2) : '—'),
    },
    {
      title: (
        <AntdTooltip title="年化收益率">
          <span>收益率</span>
        </AntdTooltip>
      ),
      dataIndex: 'returns',
      width: 80,
      align: 'right',
      render: (v) => (v != null ? `${(v * 100).toFixed(1)}%` : '—'),
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
      render: (v) => (v != null ? v.toFixed(2) : '—'),
    },
    {
      title: (
        <AntdTooltip title="标准化 Margin — 每单位交易利润。WorldQuant 约 5bps 为盈亏成本线，<0 无提交价值，<5bps 通常不盈利">
          <span>Margin</span>
        </AntdTooltip>
      ),
      dataIndex: 'margin',
      width: 90,
      align: 'right',
      render: (v) => {
        if (v == null) return '—'
        const bps = v * 10000
        const color = bps < 0 ? '#cf1322' : bps < 5 ? '#d48806' : '#389e0d'
        return <Text style={{ color }}>{bps.toFixed(1)} bps</Text>
      },
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
      width: 100,
      render: (t) => (
        <AntdTooltip title={t ? new Date(t).toLocaleString() : ''}>
          <Text type="secondary" style={{ fontSize: 11 }}>{formatRelative(t)}</Text>
        </AntdTooltip>
      ),
    },
    {
      title: '操作',
      width: 150,
      fixed: 'right',
      render: (_, row) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => navigate(`/alphas/${row.id}`)}
          >
            详情
          </Button>
          <Popconfirm
            title="以此 alpha 为蓝本优化"
            description="对 decay/窗口/中性化 做设置扫描（最多 10 变体），消耗 BRAIN 配额；胜出变体进「提交积压」队列。确认?"
            okText="优化"
            cancelText="取消"
            onConfirm={() => optimizeMutation.mutate(row.id)}
          >
            <AntdTooltip title="以此 alpha 为蓝本做设置扫描优化">
              <Button
                size="small"
                icon={<ExperimentOutlined />}
                loading={optimizeMutation.isPending && optimizeMutation.variables === row.id}
              />
            </AntdTooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>Alpha 列表</Title>
          <Text type="secondary">
            当前筛选 {total} 条 · 按 {sortBy} {sortOrder === 'desc' ? '降序' : '升序'}
          </Text>
        </Col>
        <Col>
          <Space wrap>
            <Popconfirm
              title="批量刷新可提交性"
              description="对最近 50 个 PASS alpha 重新调用 BRAIN 校验 is.checks（顺序执行，约 1 个/秒，可能耗时近 1 分钟）。确认?"
              okText="开始"
              cancelText="取消"
              onConfirm={() => refreshCanSubmitMutation.mutate()}
            >
              <Button
                icon={<SafetyCertificateOutlined />}
                loading={refreshCanSubmitMutation.isPending}
              >
                批量刷新可提交
              </Button>
            </Popconfirm>
            <Popconfirm
              title="刷新 IQC Δscore"
              description="对可提交的 alpha 触发 IQC 边际贡献审计（后台排队，稍后刷新查看 Δscore）。确认?"
              okText="触发"
              cancelText="取消"
              onConfirm={() => refreshIqcMutation.mutate()}
            >
              <Button
                icon={<TrophyOutlined />}
                loading={refreshIqcMutation.isPending}
              >
                刷新 IQC Δscore
              </Button>
            </Popconfirm>
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
                loading={syncMutation.isPending}
              >
                同步 BRAIN Alphas
              </Button>
            </Popconfirm>
            <Button icon={<ReloadOutlined />} loading={isFetching} onClick={() => refetch()}>
              刷新
            </Button>
          </Space>
        </Col>
      </Row>

      {/* Summary strip — clickable submit-state chips drive the server filter */}
      <Card className="glass-card" size="small" style={{ marginBottom: 16 }}>
        <Space wrap size={[16, 8]} split={<span style={{ color: 'rgba(255,255,255,0.15)' }}>|</span>}>
          <Space size={6}>
            <Text type="secondary">总计</Text>
            <Text strong style={{ fontSize: 16 }}>{stats?.total ?? '—'}</Text>
            {filters.region && <Tag color="blue">{filters.region}</Tag>}
          </Space>
          {['PASS', 'PASS_PROVISIONAL', 'OPTIMIZE', 'FAIL'].map((s) => (
            <Space size={6} key={s}>
              <Text type="secondary">{STATUS_LABELS[s]}</Text>
              <Text strong>{stats?.by_status?.[s] ?? 0}</Text>
            </Space>
          ))}
          <AntdTooltip title="点击筛选已提交">
            <Tag
              color={submitState === 'submitted' ? 'success' : 'default'}
              style={{ cursor: 'pointer' }}
              onClick={() => toggleSubmitState('submitted')}
            >
              已提交 {stats?.submitted ?? 0}
            </Tag>
          </AntdTooltip>
          <AntdTooltip title="点击筛选可提交且未提交">
            <Tag
              color={submitState === 'submittable' ? 'processing' : 'default'}
              style={{ cursor: 'pointer' }}
              onClick={() => toggleSubmitState('submittable')}
            >
              可提交 {stats?.submittable ?? 0}
            </Tag>
          </AntdTooltip>
          <AntdTooltip title="点击筛选不可提交">
            <Tag
              color={submitState === 'rejected' ? 'error' : 'default'}
              style={{ cursor: 'pointer' }}
              onClick={() => toggleSubmitState('rejected')}
            >
              不可提交 {stats?.rejected ?? 0}
            </Tag>
          </AntdTooltip>
          <AntdTooltip title="点击筛选未校验 can_submit">
            <Tag
              style={{ cursor: 'pointer', opacity: submitState === 'unchecked' ? 1 : 0.7 }}
              color={submitState === 'unchecked' ? 'warning' : 'default'}
              onClick={() => toggleSubmitState('unchecked')}
            >
              未检 {stats?.unchecked ?? 0}
            </Tag>
          </AntdTooltip>
        </Space>
      </Card>

      <Card className="glass-card" style={{ marginBottom: 16 }}>
        <Space wrap size={12}>
          <Space size={6}>
            <Text>地区:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 110 }}
              value={filters.region}
              onChange={(v) => { setFilters((f) => ({ ...f, region: v })); setPage(1) }}
              options={REGIONS.map((r) => ({ value: r, label: r }))}
            />
          </Space>
          <Space size={6}>
            <Text>状态:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 140 }}
              value={filters.quality_status}
              onChange={(v) => { setFilters((f) => ({ ...f, quality_status: v })); setPage(1) }}
              options={Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }))}
            />
          </Space>
          <Space size={6}>
            <Text>反馈:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 120 }}
              value={filters.human_feedback}
              onChange={(v) => { setFilters((f) => ({ ...f, human_feedback: v })); setPage(1) }}
              options={[
                { value: 'LIKED', label: '👍 喜欢' },
                { value: 'DISLIKED', label: '👎 不喜欢' },
                { value: 'NONE', label: '未评价' },
              ]}
            />
          </Space>
          <Space size={6}>
            <Text>Delay:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 100 }}
              value={filters.delay}
              onChange={(v) => { setFilters((f) => ({ ...f, delay: v })); setPage(1) }}
              options={[
                { value: 0, label: '0 (原生)' },
                { value: 1, label: '1' },
              ]}
            />
          </Space>
          <Space size={6}>
            <Text>提交:</Text>
            <Select
              allowClear
              placeholder="全部"
              style={{ width: 130 }}
              value={submitState}
              onChange={(v) => { setSubmitState(v || undefined); setPage(1) }}
              options={[
                { value: 'submitted', label: '已提交' },
                { value: 'submittable', label: '可提交 / 未提交' },
                { value: 'rejected', label: '不可提交' },
                { value: 'unchecked', label: '未校验' },
              ]}
            />
          </Space>
          <Space size={6}>
            <Text>Sharpe:</Text>
            <InputNumber
              placeholder="≥"
              step={0.1}
              style={{ width: 80 }}
              value={filters.min_sharpe}
              onChange={(v) => { setFilters((f) => ({ ...f, min_sharpe: v })); setPage(1) }}
            />
            <Text type="secondary">~</Text>
            <InputNumber
              placeholder="≤"
              step={0.1}
              style={{ width: 80 }}
              value={filters.max_sharpe}
              onChange={(v) => { setFilters((f) => ({ ...f, max_sharpe: v })); setPage(1) }}
            />
          </Space>
          <Space size={6}>
            <Text>Fitness ≥</Text>
            <InputNumber
              placeholder="任意"
              step={0.1}
              style={{ width: 80 }}
              value={filters.min_fitness}
              onChange={(v) => { setFilters((f) => ({ ...f, min_fitness: v })); setPage(1) }}
            />
          </Space>
          <Space size={6}>
            <Text>换手率 ≤</Text>
            <InputNumber
              placeholder="任意"
              step={0.05}
              style={{ width: 80 }}
              value={filters.max_turnover}
              onChange={(v) => { setFilters((f) => ({ ...f, max_turnover: v })); setPage(1) }}
            />
          </Space>
          <Space size={6}>
            <Text>收益率 ≥</Text>
            {/* Edited in % for readability; min_returns is stored as the ratio
                the backend expects (e.g. 8 % → 0.08). */}
            <InputNumber
              placeholder="任意"
              step={1}
              style={{ width: 80 }}
              value={filters.min_returns != null ? Number((filters.min_returns * 100).toFixed(4)) : undefined}
              onChange={(v) => { setFilters((f) => ({ ...f, min_returns: v != null ? v / 100 : undefined })); setPage(1) }}
            />
            <Text type="secondary">%</Text>
          </Space>
          <Space size={6}>
            <Text>排序:</Text>
            <Select
              style={{ width: 120 }}
              value={sortBy}
              onChange={(v) => { setSortBy(v); setPage(1) }}
              options={[
                { value: 'sharpe', label: 'Sharpe' },
                { value: 'fitness', label: 'Fitness' },
                { value: 'turnover', label: '换手率' },
                { value: 'returns', label: '收益率' },
                { value: 'drawdown', label: '回撤' },
                { value: 'created_at', label: '创建时间' },
                { value: 'id', label: 'ID' },
              ]}
            />
            <AntdTooltip title={sortOrder === 'desc' ? '降序(点击切换升序)' : '升序(点击切换降序)'}>
              <Button
                onClick={() => { setSortOrder((o) => (o === 'desc' ? 'asc' : 'desc')); setPage(1) }}
              >
                {sortOrder === 'desc' ? '↓ 降序' : '↑ 升序'}
              </Button>
            </AntdTooltip>
          </Space>
          <Search
            key={resetKey}
            placeholder="搜索表达式 (substring)"
            allowClear
            enterButton
            defaultValue={filters.expression}
            style={{ width: 260 }}
            onSearch={(v) => { setFilters((f) => ({ ...f, expression: v })); setPage(1) }}
          />
          {hasActiveFilters && (
            <Button icon={<ClearOutlined />} onClick={resetFilters}>
              重置
            </Button>
          )}
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
          scroll={{ x: 1580 }}
        />
      </Card>
    </div>
  )
}
