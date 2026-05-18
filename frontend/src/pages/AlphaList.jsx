import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
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
  Tooltip as AntdTooltip,
} from 'antd'
import { EyeOutlined, ReloadOutlined } from '@ant-design/icons'
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

const REGIONS = ['USA', 'CHN', 'EUR', 'ASI', 'GLB', 'KOR', 'HKG', 'JPN']

export default function AlphaList() {
  const navigate = useNavigate()
  const [filters, setFilters] = useState({
    region: undefined,
    quality_status: undefined,
    min_sharpe: undefined,
    expression: '',
  })
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

  const items = data?.items || []
  const total = data?.total || 0

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      render: (id) => <a onClick={() => navigate(`/alphas/${id}`)}>#{id}</a>,
    },
    {
      title: 'BRAIN id',
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
      title: 'Dataset',
      dataIndex: 'dataset_id',
      width: 100,
      ellipsis: true,
      render: (d) => d ? <Tag color="cyan">{d}</Tag> : '—',
    },
    {
      title: 'Status',
      dataIndex: 'quality_status',
      width: 130,
      render: (s) => <Tag color={STATUS_COLORS[s] || 'default'}>{s}</Tag>,
    },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe',
      width: 80,
      align: 'right',
      render: (v) => v != null ? <Text strong>{v.toFixed(2)}</Text> : '—',
    },
    {
      title: 'Fitness',
      dataIndex: 'fitness',
      width: 80,
      align: 'right',
      render: (v) => v != null ? v.toFixed(2) : '—',
    },
    {
      title: 'Turnover',
      dataIndex: 'turnover',
      width: 80,
      align: 'right',
      render: (v) => v != null ? v.toFixed(2) : '—',
    },
    {
      title: 'self_corr',
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
          <Button
            icon={<ReloadOutlined />}
            loading={isFetching}
            onClick={() => refetch()}
          >
            刷新
          </Button>
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
                { value: 'PASS', label: 'PASS' },
                { value: 'PASS_PROVISIONAL', label: 'PASS_PROVISIONAL' },
                { value: 'OPTIMIZE', label: 'OPTIMIZE' },
                { value: 'FAIL', label: 'FAIL' },
                { value: 'PENDING', label: 'PENDING' },
                { value: 'REJECT', label: 'REJECT' },
              ]}
            />
          </Space>
          <Space>
            <Text>min sharpe:</Text>
            <InputNumber
              placeholder="任意"
              step={0.1}
              style={{ width: 100 }}
              value={filters.min_sharpe}
              onChange={(v) => { setFilters((f) => ({ ...f, min_sharpe: v })); setPage(1) }}
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
