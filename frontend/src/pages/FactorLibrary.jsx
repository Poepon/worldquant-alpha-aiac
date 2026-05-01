import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
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
} from 'antd'
import {
  ArrowRightOutlined,
  EyeOutlined,
  ApartmentOutlined,
  ReloadOutlined,
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


function TierAlphaTable({ tier }) {
  const navigate = useNavigate()
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20 })
  const [filters, setFilters] = useState({
    region: undefined,
    quality_status: undefined,
    min_sharpe: undefined,
    expression_search: undefined,
  })

  const queryParams = useMemo(() => {
    const out = {
      tier,
      limit: pagination.pageSize,
      offset: (pagination.current - 1) * pagination.pageSize,
      sort_by: 'is_sharpe',
      sort_order: 'desc',
    }
    Object.entries(filters).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') out[k] = v
    })
    return out
  }, [tier, pagination, filters])

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['factor-library/alphas', queryParams],
    queryFn: () => api.getFactorLibraryAlphas(queryParams),
    keepPreviousData: true,
  })

  const items = data?.items ?? []
  const total = data?.total ?? 0

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 64,
      render: (id) => (
        <a onClick={() => navigate(`/alphas/${id}`)}>#{id}</a>
      ),
    },
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
      title: '状态',
      dataIndex: 'quality_status',
      width: 130,
      render: (s) => <Tag color={STATUS_COLORS[s] || 'default'}>{s}</Tag>,
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
    ...(tier > 1
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
      width: 180,
      render: (_, row) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => navigate(`/alphas/${row.id}`)}
          >
            详情
          </Button>
          {tier < 3 && row.quality_status === 'PASS' && (
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
        onChange={(p) => setPagination(p)}
        scroll={{ x: 1100 }}
      />
    </>
  )
}


export default function FactorLibrary() {
  const { data: stats } = useQuery({
    queryKey: ['factor-library/stats'],
    queryFn: api.getFactorLibraryStats,
    refetchInterval: 30_000, // refresh every 30s
  })

  const tiers = stats?.tiers ?? []
  const tierMap = Object.fromEntries(tiers.map((t) => [t.tier, t]))

  return (
    <div>
      <Title level={3}>
        <ApartmentOutlined /> 因子库（T1 / T2 / T3）
      </Title>
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
          defaultActiveKey="1"
          items={[1, 2, 3].map((t) => ({
            key: String(t),
            label: (
              <Space>
                <Tag color={TIER_COLORS[t]}>T{t}</Tag>
                {TIER_LABELS[t]}
              </Space>
            ),
            children: <TierAlphaTable tier={t} />,
          }))}
        />
      </Card>
    </div>
  )
}
