import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, useMemo } from 'react'
import {
  Row,
  Col,
  Card,
  Statistic,
  Tabs,
  Table,
  Tag,
  Select,
  Input,
  InputNumber,
  Button,
  Typography,
  Space,
  Tooltip,
  Empty,
  Popconfirm,
  message,
} from 'antd'
import {
  ArrowRightOutlined,
  EyeOutlined,
  ApartmentOutlined,
  ReloadOutlined,
  CloudUploadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import api from '../services/api'

const { Title, Text } = Typography
const { Search } = Input

const TIER_COLORS = { 1: '#1677ff', 2: '#722ed1', 3: '#fa541c' }
const TIER_LABELS = {
  1: 'T1 一阶 — 单 ts_op 信号',
  2: 'T2 二阶 — 横截面/平滑包装',
  3: 'T3 三阶 — trade_when 择时',
}

const STATUS_COLORS = {
  PASS: 'success',
  PASS_PROVISIONAL: 'gold',
  OPTIMIZE: 'processing',
  FAIL: 'default',
  PENDING: 'default',
  REJECT: 'error',
}


function TierKpiCard({ tier, kpi }) {
  if (!kpi) return null
  const accent = TIER_COLORS[tier]
  return (
    <Card
      size="small"
      style={{ borderLeft: `4px solid ${accent}` }}
      title={
        <Space>
          <Tag color={accent}>T{tier}</Tag>
          <Text strong>{TIER_LABELS[tier]}</Text>
        </Space>
      }
      extra={
        <Tooltip title="今日转入 PASS 数（来自 alpha_status_transitions）">
          <Tag color="green">+{kpi.today_pass_increment} 今日</Tag>
        </Tooltip>
      }
    >
      <Row gutter={16}>
        <Col span={8}>
          <Statistic title="PASS" value={kpi.pass_count} valueStyle={{ color: '#52c41a' }} />
        </Col>
        <Col span={8}>
          <Statistic
            title="PROVISIONAL"
            value={kpi.provisional_count}
            valueStyle={{ color: '#faad14' }}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="FAIL"
            value={kpi.fail_count}
            valueStyle={{ color: '#bfbfbf' }}
          />
        </Col>
      </Row>
      <Row gutter={16} style={{ marginTop: 12 }}>
        <Col span={8}>
          <Statistic
            title="平均 sharpe"
            value={kpi.avg_sharpe ?? 0}
            precision={2}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="中位 sharpe"
            value={kpi.median_sharpe ?? 0}
            precision={2}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="最高 sharpe"
            value={kpi.max_sharpe ?? 0}
            precision={2}
            valueStyle={{ color: accent }}
          />
        </Col>
      </Row>
    </Card>
  )
}


function PromotionChart({ days = 30 }) {
  const { data } = useQuery({
    queryKey: ['factor-library/promotion', days],
    queryFn: () => api.getFactorPromotionCount(days),
  })
  const points = data?.points ?? []
  return (
    <Card
      size="small"
      title="晋级数（事件流，近 30 天）"
      style={{ height: '100%' }}
    >
      {points.length === 0 ? (
        <Empty
          description="尚无晋级事件"
          style={{ paddingTop: 20 }}
        />
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={points} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} />
            <RechartsTooltip />
            <Legend />
            <Line
              type="monotone"
              dataKey="t1_to_t2"
              stroke="#1677ff"
              name="T1→T2"
              dot={{ r: 2 }}
            />
            <Line
              type="monotone"
              dataKey="t2_to_t3"
              stroke="#722ed1"
              name="T2→T3"
              dot={{ r: 2 }}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}


// Self-correlation cell — shared by every table mode. A null value means
// "never measured" (NOT "uncorrelated"); colour bands mirror the 0.7 BRAIN
// gate so the 可提交 tab reads at a glance.
function SelfCorrCell({ value, source }) {
  if (value == null) {
    return (
      <Tooltip title="本地 OS PnL 未实测自相关性（≠ 不相关）">
        <Tag>未测</Tag>
      </Tooltip>
    )
  }
  const color = value >= 0.7 ? 'red' : value >= 0.5 ? 'orange' : 'green'
  const srcColor = source === 'local' ? 'cyan' : source === 'brain' ? 'blue' : 'default'
  return (
    <Space size={2}>
      <Tag color={color}>{value.toFixed(3)}</Tag>
      <Tooltip
        title={
          source === 'local'
            ? '本地 OS PnL 矩阵实测'
            : source === 'brain'
              ? 'BRAIN /correlations/SELF'
              : '来源未知'
        }
      >
        <Tag color={srcColor}>{source || '?'}</Tag>
      </Tooltip>
    </Space>
  )
}


// mode: 'tier' (needs tier prop) | 'submittable' | 'submitted'
function AlphaTable({ tier, mode = 'tier' }) {
  const navigate = useNavigate()
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20 })
  const [filters, setFilters] = useState({
    region: undefined,
    quality_status: undefined,
    min_sharpe: undefined,
    expression_search: undefined,
    submitted: undefined,
    can_submit: undefined,
  })
  const [sorter, setSorter] = useState({
    field: 'created_at',
    order: 'descend',
  })
  const [submittingId, setSubmittingId] = useState(null)
  const [refreshingIqc, setRefreshingIqc] = useState(false)

  const queryParams = useMemo(() => {
    const sortKeyMap = {
      is_sharpe: 'is_sharpe',
      is_fitness: 'is_fitness',
      is_turnover: 'is_turnover',
      iqc_delta_score: 'iqc_delta_score',
      created_at: 'created_at',
      metrics_snapshot_at: 'metrics_snapshot_at',
    }
    const sort_by = sortKeyMap[sorter.field] || 'created_at'
    const sort_order = sorter.order === 'ascend' ? 'asc' : 'desc'
    const out = {
      limit: pagination.pageSize,
      offset: (pagination.current - 1) * pagination.pageSize,
      sort_by,
      sort_order,
    }
    if (mode === 'tier') out.tier = tier
    else if (mode === 'submittable') out.submittable = true
    else if (mode === 'submitted') out.submitted = true
    Object.entries(filters).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') out[k] = v
    })
    return out
  }, [tier, mode, pagination, filters, sorter])

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['factor-library/alphas', queryParams],
    queryFn: () => api.getFactorLibraryAlphas(queryParams),
    keepPreviousData: true,
  })

  const items = data?.items ?? []
  const total = data?.total ?? 0

  const handleSubmit = async (row) => {
    setSubmittingId(row.id)
    try {
      const res = await api.submitAlpha(row.id)
      if (res.submitted) {
        message.success(`#${row.id} 已提交到 BRAIN`)
      } else {
        message.error(`#${row.id} 未提交：${res.reason}`)
      }
    } catch (e) {
      message.error(`提交请求失败：${e?.response?.data?.detail || e.message}`)
    } finally {
      setSubmittingId(null)
      refetch()
    }
  }

  const handleRefreshIqc = async () => {
    setRefreshingIqc(true)
    try {
      const res = await api.refreshFactorIqc({ scope: 'submittable' })
      message.success(res.message)
      // IQC audits run as staggered Celery tasks; wait for the batch to
      // drain (≈ enqueued × 2s) before refetching, capped at 30s.
      const delay = Math.min((res.enqueued || 0) * 2000 + 3000, 30000)
      setTimeout(() => refetch(), delay)
    } catch (e) {
      message.error(`刷新 IQC 失败：${e?.response?.data?.detail || e.message}`)
    } finally {
      setRefreshingIqc(false)
    }
  }

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 64,
      render: (id) => (
        <a onClick={() => navigate(`/alphas/${id}`)}>#{id}</a>
      ),
    },
    // Cross-tier modes need the tier column since rows mix T1/T2/T3.
    ...(mode !== 'tier'
      ? [
          {
            title: 'Tier',
            dataIndex: 'factor_tier',
            width: 70,
            render: (t) =>
              t ? <Tag color={TIER_COLORS[t]}>T{t}</Tag> : <Text type="secondary">—</Text>,
          },
        ]
      : []),
    {
      title: '表达式',
      dataIndex: 'expression',
      ellipsis: true,
      render: (expr) => (
        <Tooltip title={expr}>
          <Text code style={{ fontSize: 12 }}>
            {expr?.length > 80 ? `${expr.slice(0, 80)}...` : expr}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: 'sharpe',
      dataIndex: 'is_sharpe',
      width: 90,
      align: 'right',
      render: (v) => (v != null ? v.toFixed(2) : '—'),
      sorter: true,
    },
    {
      title: 'fitness',
      dataIndex: 'is_fitness',
      width: 90,
      align: 'right',
      render: (v) => (v != null ? v.toFixed(2) : '—'),
    },
    {
      title: 'turnover',
      dataIndex: 'is_turnover',
      width: 90,
      align: 'right',
      render: (v) => (v != null ? v.toFixed(2) : '—'),
    },
    {
      title: 'Self-corr',
      dataIndex: 'self_corr',
      width: 140,
      render: (v, row) => <SelfCorrCell value={v} source={row.self_corr_source} />,
    },
    {
      title: '状态',
      dataIndex: 'quality_status',
      width: 130,
      render: (s) => <Tag color={STATUS_COLORS[s] || 'default'}>{s}</Tag>,
    },
    {
      title: '提交',
      dataIndex: 'date_submitted',
      width: 100,
      render: (ts) =>
        ts ? (
          <Tooltip title={`已提交：${new Date(ts).toLocaleString()}`}>
            <Tag color="green">✅ 已提交</Tag>
          </Tooltip>
        ) : (
          <Tag>未提交</Tag>
        ),
    },
    {
      title: '可提交',
      dataIndex: 'can_submit',
      width: 110,
      render: (v) => {
        if (v === true) {
          return <Tooltip title="BRAIN is.checks 全无 FAIL"><Tag color="success">✅ 可提交</Tag></Tooltip>
        }
        if (v === false) {
          return <Tooltip title="BRAIN is.checks 含 FAIL，未达提交门槛"><Tag color="error">⚠️ 不可提交</Tag></Tooltip>
        }
        return <Tooltip title="未调 BRAIN 检查"><Tag>—</Tag></Tooltip>
      },
    },
    {
      // V-23.A (2026-05-13): IQC marginal Δscore — *dynamic* signal,
      // not a quality label. Δscore reflects current portfolio state;
      // changes on every team submission. Use as ranker, not filter.
      title: 'IQC Δscore',
      dataIndex: 'iqc_delta_score',
      key: 'iqc_delta_score',
      width: 140,
      align: 'right',
      sorter: true,
      render: (v, row) => {
        if (v == null) {
          return (
            <Tooltip title="尚未审计 IQC marginal contribution">
              <Text type="secondary">—</Text>
            </Tooltip>
          )
        }
        const stale = row.iqc_stale === true
        const sign = v > 0 ? '+' : ''
        const color = v > 0 ? '#52c41a' : (v < 0 ? '#ff4d4f' : undefined)
        const audited = row.iqc_audited_at
          ? new Date(row.iqc_audited_at).toLocaleString()
          : '—'
        return (
          <Tooltip
            title={
              <>
                <div>audited as of {audited}</div>
                {row.iqc_delta_sharpe != null && (
                  <div>Δsharpe: {row.iqc_delta_sharpe.toFixed(3)}</div>
                )}
                <div style={{ marginTop: 4, fontSize: 11 }}>
                  IQC marginal Δscore 反映把这个 alpha 加入当前 portfolio 的
                  累加 score 变化。<strong>Δscore 会随 team 提交其他 alpha
                  动态变化</strong>。当前为负不代表 alpha 本身没价值。
                </div>
              </>
            }
          >
            <Space size={4}>
              <Text strong style={{ color }}>
                {sign}{v.toFixed(0)}
              </Text>
              {stale && (
                <Tag color="orange" style={{ marginLeft: 0, fontSize: 10, padding: '0 4px' }}>
                  stale
                </Tag>
              )}
            </Space>
          </Tooltip>
        )
      },
    },
    {
      title: 'Region',
      dataIndex: 'region',
      width: 80,
    },
    {
      title: 'Dataset',
      dataIndex: 'dataset_id',
      width: 140,
      ellipsis: true,
    },
    ...(mode === 'tier' && tier > 1
      ? [
          {
            title: '父 alpha',
            dataIndex: 'parent_alpha_id',
            width: 100,
            render: (pid) =>
              pid ? (
                <a onClick={() => navigate(`/alphas/${pid}`)}>#{pid}</a>
              ) : (
                <Text type="secondary">—</Text>
              ),
          },
        ]
      : []),
    {
      title: '快照时间',
      dataIndex: 'metrics_snapshot_at',
      width: 160,
      render: (ts) =>
        ts ? (
          <Tooltip title={`metric as of ${ts}`}>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {new Date(ts).toLocaleString()}
            </Text>
          </Tooltip>
        ) : (
          '—'
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: mode === 'submittable' ? 200 : 180,
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
          {mode === 'tier' && tier < 3 && row.quality_status === 'PASS' && (
            <Tooltip
              title={`基于此 ${tier === 1 ? 'T1' : 'T2'} 种子派生 T${tier + 1}`}
            >
              <Button
                size="small"
                type="primary"
                icon={<ArrowRightOutlined />}
                onClick={() =>
                  navigate(
                    `/tasks?mode=AUTONOMOUS_TIER${tier + 1}&seed_alpha_id=${row.id}`
                  )
                }
              >
                派生 T{tier + 1}
              </Button>
            </Tooltip>
          )}
          {mode === 'submittable' && (
            <Popconfirm
              title="提交到 BRAIN？"
              description={
                <div style={{ maxWidth: 280 }}>
                  #{row.id} 将提交到 BRAIN 评估。
                  <br />
                  <Text type="danger">此操作不可逆且消耗提交配额。</Text>
                  <br />
                  服务端会再次校验 can_submit 与 self_corr&lt;0.7。
                </div>
              }
              okText="确认提交"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleSubmit(row)}
            >
              <Button
                size="small"
                type="primary"
                danger
                icon={<CloudUploadOutlined />}
                loading={submittingId === row.id}
              >
                提交
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          allowClear
          placeholder="region"
          style={{ width: 110 }}
          value={filters.region}
          onChange={(v) =>
            setFilters((f) => ({ ...f, region: v }))
          }
          options={['USA', 'CHN', 'EUR', 'ASI', 'GLB'].map((r) => ({
            value: r,
            label: r,
          }))}
        />
        <Select
          allowClear
          placeholder="quality_status"
          style={{ width: 170 }}
          value={filters.quality_status}
          onChange={(v) =>
            setFilters((f) => ({ ...f, quality_status: v }))
          }
          options={[
            { value: 'PASS', label: 'PASS' },
            { value: 'PASS_PROVISIONAL', label: 'PASS_PROVISIONAL' },
            { value: 'OPTIMIZE', label: 'OPTIMIZE' },
            { value: 'FAIL', label: 'FAIL' },
            { value: 'PENDING', label: 'PENDING' },
          ]}
        />
        <InputNumber
          placeholder="min sharpe"
          style={{ width: 110 }}
          step={0.1}
          value={filters.min_sharpe}
          onChange={(v) =>
            setFilters((f) => ({ ...f, min_sharpe: v }))
          }
        />
        {/* submitted / can_submit 筛选器只在「按 Tier 浏览」模式有意义 —
            可提交 / 已提交 tab 已经在 query 层强制了这两个口径。 */}
        {mode === 'tier' && (
          <>
            <Select
              allowClear
              placeholder="提交状态"
              style={{ width: 130 }}
              value={filters.submitted}
              onChange={(v) =>
                setFilters((f) => ({ ...f, submitted: v }))
              }
              options={[
                { value: true, label: '已提交' },
                { value: false, label: '未提交' },
              ]}
            />
            <Select
              allowClear
              placeholder="可提交性"
              style={{ width: 140 }}
              value={filters.can_submit}
              onChange={(v) =>
                setFilters((f) => ({ ...f, can_submit: v }))
              }
              options={[
                { value: 'true', label: '✅ 可提交' },
                { value: 'false', label: '⚠️ 不可提交' },
                { value: 'null', label: '未检查' },
              ]}
            />
          </>
        )}
        <Search
          placeholder="表达式包含..."
          allowClear
          style={{ width: 240 }}
          onSearch={(v) =>
            setFilters((f) => ({
              ...f,
              expression_search: v || undefined,
            }))
          }
        />
        <Button
          icon={<ReloadOutlined />}
          onClick={() => refetch()}
        >
          刷新
        </Button>
      </Space>
      {mode === 'submittable' && (
        <div style={{ marginBottom: 12 }}>
          <Space align="start">
            <Button
              icon={<ReloadOutlined />}
              loading={refreshingIqc}
              onClick={handleRefreshIqc}
            >
              刷新 IQC Δscore
            </Button>
            <Text type="secondary" style={{ fontSize: 12 }}>
              「可提交」口径：can_submit=true 且未提交 且本地 self_corr&lt;0.7（或未测）。
              self_corr≥0.7 的已知高相关 alpha 已被排除。
              <br />
              <Text type="warning" style={{ fontSize: 12 }}>
                ⚠ 提交前还要看 IQC Δscore 列：负值（红）= 加入 portfolio 会拉低
                team score，不应提交。Δscore 随 team 提交动态变化，点左侧按钮重新审计。
              </Text>
            </Text>
          </Space>
        </div>
      )}
      <Table
        rowKey="id"
        size="small"
        dataSource={items}
        columns={columns}
        loading={isLoading}
        pagination={{
          ...pagination,
          total,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
        }}
        onChange={(p, _f, s) => {
          setPagination(p)
          if (s && (s.field || s.columnKey)) {
            setSorter({
              field: s.field || s.columnKey,
              order: s.order || 'descend',
            })
          } else {
            setSorter({ field: 'created_at', order: 'descend' })
          }
        }}
        scroll={{ x: 1400 }}
      />
    </>
  )
}


export default function FactorLibrary() {
  const queryClient = useQueryClient()
  const [syncing, setSyncing] = useState(false)

  const { data: stats } = useQuery({
    queryKey: ['factor-library/stats'],
    queryFn: api.getFactorLibraryStats,
    refetchInterval: 30_000, // refresh every 30s
  })

  const tiers = stats?.tiers ?? []
  const tierMap = Object.fromEntries(tiers.map((t) => [t.tier, t]))

  // Sync Alphas from BRAIN — moved here from the retired 因子实验室 page.
  // It's a background Celery job, so invalidate the factor-library queries
  // a couple seconds later to pick up the newly synced rows.
  const handleSync = async () => {
    setSyncing(true)
    try {
      const res = await api.syncAlphas()
      message.success(`Sync started: ${res.message}`)
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['factor-library/stats'] })
        queryClient.invalidateQueries({ queryKey: ['factor-library/alphas'] })
      }, 2000)
    } catch (error) {
      message.error('Sync failed: ' + error.message)
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          marginBottom: 8,
        }}
      >
        <Title level={3} style={{ margin: 0 }}>
          <ApartmentOutlined /> 因子库（T1 / T2 / T3）
        </Title>
        <Button
          type="primary"
          icon={<SyncOutlined spin={syncing} />}
          onClick={handleSync}
          loading={syncing}
        >
          Sync Alphas
        </Button>
      </div>
      {stats?.last_refreshed_at && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 16 }}>
          Last refreshed: {new Date(stats.last_refreshed_at).toLocaleString()}
        </Text>
      )}
      <Row gutter={16}>
        <Col span={16}>
          <Row gutter={[12, 12]}>
            {[1, 2, 3].map((t) => (
              <Col key={t} span={24}>
                <TierKpiCard tier={t} kpi={tierMap[t]} />
              </Col>
            ))}
          </Row>
        </Col>
        <Col span={8}>
          <PromotionChart days={30} />
        </Col>
      </Row>

      <Card style={{ marginTop: 16 }}>
        <Tabs
          defaultActiveKey="browse"
          items={[
            {
              key: 'browse',
              label: (
                <Space>
                  <ApartmentOutlined />
                  按 Tier 浏览
                </Space>
              ),
              children: (
                <Tabs
                  defaultActiveKey="1"
                  items={[1, 2, 3].map((t) => ({
                    key: String(t),
                    label: (
                      <Space>
                        <Tag color={TIER_COLORS[t]}>T{t}</Tag>
                        {TIER_LABELS[t]}
                      </Space>
                    ),
                    children: <AlphaTable tier={t} mode="tier" />,
                  }))}
                />
              ),
            },
            {
              key: 'submittable',
              label: (
                <Space>
                  <CloudUploadOutlined />
                  <Tag color="green">可提交</Tag>
                </Space>
              ),
              children: <AlphaTable mode="submittable" />,
            },
            {
              key: 'submitted',
              label: (
                <Space>
                  <Tag color="blue">已提交</Tag>
                </Space>
              ),
              children: <AlphaTable mode="submitted" />,
            },
          ]}
        />
      </Card>
    </div>
  )
}
