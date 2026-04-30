import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Row,
  Col,
  Card,
  Table,
  Tag,
  Space,
  Typography,
  Select,
  Input,
  InputNumber,
  Button,
  message,
} from 'antd'
import {
  ExperimentOutlined,
  LikeOutlined,
  DislikeOutlined,
  SyncOutlined,
  ClearOutlined,
} from '@ant-design/icons'
import { useState, useMemo, useCallback } from 'react'
import api from '../services/api'

const { Title, Text } = Typography
const { Search } = Input

// External sort keys must match backend _SORT_COLUMN_MAP
const SORT_KEY_BY_DATA_INDEX = {
  sharpe: 'sharpe',
  fitness: 'fitness',
  turnover: 'turnover',
  returns: 'returns',
  drawdown: 'drawdown',
  created_at: 'created_at',
  region: 'region',
  quality_status: 'quality_status',
}

const INITIAL_FILTERS = {
  quality_status: undefined,
  region: undefined,
  human_feedback: undefined,
  expression: undefined,
  min_sharpe: undefined,
  min_fitness: undefined,
  max_turnover: undefined,
}

export default function AlphaLab() {
  const navigate = useNavigate()
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20 })
  const [filters, setFilters] = useState(INITIAL_FILTERS)
  const [sort, setSort] = useState({ sort_by: 'created_at', sort_order: 'desc' })
  const [syncing, setSyncing] = useState(false)

  // Build params for /alphas, dropping undefined/null/empty values so the
  // query string stays clean and matches FastAPI Optional defaults.
  const queryParams = useMemo(() => {
    const out = {
      limit: pagination.pageSize,
      offset: (pagination.current - 1) * pagination.pageSize,
      sort_by: sort.sort_by,
      sort_order: sort.sort_order,
    }
    Object.entries(filters).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') out[k] = v
    })
    return out
  }, [pagination.current, pagination.pageSize, sort, filters])

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['alphas', queryParams],
    queryFn: () => api.getAlphas(queryParams),
    keepPreviousData: true,
  })

  // Tolerate both legacy (Array) and new ({items, total}) shapes
  let alphas = []
  let total = 0
  if (Array.isArray(data)) {
    alphas = data
    total = data.length
  } else if (data && data.items && typeof data.total === 'number') {
    alphas = data.items
    total = data.total
  }

  const setFilter = useCallback((key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value }))
    setPagination((prev) => ({ ...prev, current: 1 })) // reset to first page on filter change
  }, [])

  const handleReset = () => {
    setFilters(INITIAL_FILTERS)
    setSort({ sort_by: 'created_at', sort_order: 'desc' })
    setPagination((prev) => ({ ...prev, current: 1 }))
  }

  const handleTableChange = (newPagination, _antdFilters, sorter) => {
    setPagination((prev) => ({
      ...prev,
      current: newPagination.current,
      pageSize: newPagination.pageSize,
    }))
    if (sorter && sorter.field && sorter.order) {
      const sort_by = SORT_KEY_BY_DATA_INDEX[sorter.field] || 'created_at'
      const sort_order = sorter.order === 'ascend' ? 'asc' : 'desc'
      setSort({ sort_by, sort_order })
    } else if (sorter && !sorter.order) {
      // Cleared sort
      setSort({ sort_by: 'created_at', sort_order: 'desc' })
    }
  }

  const handleSync = async () => {
    setSyncing(true)
    try {
      const res = await api.syncAlphas()
      message.success(`Sync started: ${res.message}`)
      setTimeout(() => refetch(), 2000)
    } catch (error) {
      message.error('Sync failed: ' + error.message)
    } finally {
      setSyncing(false)
    }
  }

  const columns = [
    {
      title: 'Name',
      dataIndex: 'name',
      key: 'name',
      width: 220,
      render: (text, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{text || 'anonymous'}</Text>
          <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
            {record.expression}
          </Text>
        </Space>
      ),
    },
    { title: 'Type', dataIndex: 'type', key: 'type', width: 80 },
    {
      title: 'Quality',
      dataIndex: 'quality_status',
      key: 'quality_status',
      width: 100,
      sorter: true,
      render: (v) => {
        const color = v === 'PASS' ? 'green' : v === 'PASS_PROVISIONAL' ? 'gold' : v === 'REJECT' || v === 'FAIL' ? 'red' : v === 'OPTIMIZE' ? 'orange' : 'default'
        return <Tag color={color}>{v || 'PENDING'}</Tag>
      },
    },
    {
      title: 'Date Created',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 130,
      sorter: true,
      defaultSortOrder: 'descend',
      render: (text) => (text ? new Date(text).toLocaleDateString() : '-'),
    },
    { title: 'Region', dataIndex: 'region', key: 'region', width: 80, sorter: true },
    {
      title: 'Sharpe',
      dataIndex: 'sharpe',
      key: 'sharpe',
      width: 90,
      sorter: true,
      render: (val) => (val != null ? val.toFixed(2) : '-'),
    },
    {
      title: 'Fitness',
      dataIndex: 'fitness',
      key: 'fitness',
      width: 90,
      sorter: true,
      render: (val) => (val != null ? val.toFixed(2) : '-'),
    },
    {
      title: 'Turnover',
      dataIndex: 'turnover',
      key: 'turnover',
      width: 100,
      sorter: true,
      render: (val) => (val != null ? `${(val * 100).toFixed(2)}%` : '-'),
    },
    {
      title: 'Returns',
      dataIndex: 'returns',
      key: 'returns',
      width: 100,
      sorter: true,
      render: (val) => (val != null ? `${(val * 100).toFixed(2)}%` : '-'),
    },
    {
      title: 'Drawdown',
      dataIndex: 'drawdown',
      key: 'drawdown',
      width: 100,
      sorter: true,
      render: (val) => (val != null ? `${(val * 100).toFixed(2)}%` : '-'),
    },
    {
      title: 'Margin',
      dataIndex: 'margin',
      key: 'margin',
      width: 100,
      render: (val) => (val != null ? `${(val * 10000).toFixed(2)}‱` : '-'),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ExperimentOutlined style={{ marginRight: 12, color: '#00d4ff' }} />
          Alpha Lab
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

      <Card className="glass-card" style={{ marginBottom: 16 }}>
        <Row gutter={[12, 12]}>
          <Col xs={24} sm={12} md={6}>
            <Select
              placeholder="Quality"
              style={{ width: '100%' }}
              allowClear
              value={filters.quality_status}
              onChange={(v) => setFilter('quality_status', v)}
              options={[
                { value: 'PASS', label: 'PASS' },
                { value: 'PASS_PROVISIONAL', label: 'PASS (Provisional)' },
                { value: 'OPTIMIZE', label: 'OPTIMIZE' },
                { value: 'FAIL', label: 'FAIL' },
                { value: 'REJECT', label: 'REJECT' },
                { value: 'PENDING', label: 'PENDING' },
              ]}
            />
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Select
              placeholder="Region"
              style={{ width: '100%' }}
              allowClear
              value={filters.region}
              onChange={(v) => setFilter('region', v)}
              options={[
                { value: 'USA', label: 'USA' },
                { value: 'CHN', label: 'China' },
                { value: 'ASI', label: 'Asia' },
                { value: 'EUR', label: 'Europe' },
                { value: 'GLB', label: 'Global' },
                { value: 'HKG', label: 'Hong Kong' },
                { value: 'JPN', label: 'Japan' },
                { value: 'KOR', label: 'Korea' },
                { value: 'TWN', label: 'Taiwan' },
                { value: 'IND', label: 'India' },
              ]}
            />
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Select
              placeholder="Feedback"
              style={{ width: '100%' }}
              allowClear
              value={filters.human_feedback}
              onChange={(v) => setFilter('human_feedback', v)}
              options={[
                { value: 'LIKED', label: '👍 Liked' },
                { value: 'DISLIKED', label: '👎 Disliked' },
                { value: 'NONE', label: 'Not Rated' },
              ]}
            />
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Search
              placeholder="Search expression..."
              allowClear
              defaultValue={filters.expression}
              onSearch={(v) => setFilter('expression', v || undefined)}
            />
          </Col>

          <Col xs={24} sm={12} md={6}>
            <InputNumber
              placeholder="Min Sharpe"
              style={{ width: '100%' }}
              step={0.1}
              value={filters.min_sharpe}
              onChange={(v) => setFilter('min_sharpe', v ?? undefined)}
            />
          </Col>
          <Col xs={24} sm={12} md={6}>
            <InputNumber
              placeholder="Min Fitness"
              style={{ width: '100%' }}
              step={0.1}
              value={filters.min_fitness}
              onChange={(v) => setFilter('min_fitness', v ?? undefined)}
            />
          </Col>
          <Col xs={24} sm={12} md={6}>
            <InputNumber
              placeholder="Max Turnover (e.g. 0.7)"
              style={{ width: '100%' }}
              step={0.05}
              min={0}
              max={1}
              value={filters.max_turnover}
              onChange={(v) => setFilter('max_turnover', v ?? undefined)}
            />
          </Col>
          <Col xs={24} sm={12} md={6}>
            <Button icon={<ClearOutlined />} onClick={handleReset} block>
              Reset filters
            </Button>
          </Col>
        </Row>
      </Card>

      <Card className="glass-card">
        <Table
          columns={columns}
          dataSource={alphas}
          rowKey="id"
          loading={isLoading}
          size="small"
          pagination={{
            ...pagination,
            total,
            showSizeChanger: true,
            showTotal: (t) => `Total ${t} items`,
          }}
          onChange={handleTableChange}
          title={() => <Text strong>Total Alphas: {total}</Text>}
          onRow={(record) => ({
            onClick: () => navigate(`/alphas/${record.id}`),
            style: { cursor: 'pointer' },
          })}
        />
      </Card>
    </div>
  )
}
