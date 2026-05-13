import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Row,
  Col,
  Card,
  Statistic,
  Table,
  Tag,
  Select,
  Button,
  Typography,
  Space,
  Empty,
  Alert,
  Descriptions,
} from 'antd'
import {
  ReloadOutlined,
  WarningOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import api from '../services/api'

const { Title, Text } = Typography

const WINDOW_LABELS = {
  covid_2020: 'COVID 流动性挤兑 (2020-03)',
  rate_shock_2022: '利率冲击 (2022-H1)',
  svb_2023: 'SVB 银行传染 (2023-03)',
  tariff_2025: '关税冲击 (2025-04)',
}

const WINDOW_CHARACTER = {
  covid_2020: '流动性',
  rate_shock_2022: '利率/久期',
  svb_2023: '行业传染',
  tariff_2025: '地缘/政策',
}

const REGIONS = ['USA', 'CHN', 'EUR', 'HKG', 'JPN']

function corrColor(v) {
  if (v === null || v === undefined) return 'default'
  if (v >= 0.7) return 'red'
  if (v >= 0.5) return 'orange'
  if (v >= 0.3) return 'gold'
  return 'green'
}

function fmtCorr(v) {
  if (v === null || v === undefined) return '—'
  return v.toFixed(3)
}

function deltaTag(crisis, baseline) {
  if (crisis === null || baseline === null || crisis === undefined || baseline === undefined) {
    return null
  }
  const delta = crisis - baseline
  const arrow = delta > 0 ? '↑' : '↓'
  const color = delta > 0.1 ? 'red' : delta > 0.05 ? 'orange' : 'default'
  return (
    <Tag color={color} style={{ marginLeft: 4 }}>
      {arrow}{Math.abs(delta).toFixed(2)} vs baseline
    </Tag>
  )
}

function WindowCard({ name, summary, baseline }) {
  const empty = summary?.status !== 'ok'
  return (
    <Card
      size="small"
      title={
        <Space direction="vertical" size={0}>
          <Text strong>{WINDOW_LABELS[name] || name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            性格：{WINDOW_CHARACTER[name] || '—'} · {summary?.n_alphas ?? 0} alphas / {summary?.n_obs ?? 0} obs
          </Text>
        </Space>
      }
    >
      {empty ? (
        <Empty
          description={`数据不足 (status: ${summary?.status || 'empty'})`}
          styles={{ image: { height: 40 } }}
        />
      ) : (
        <Row gutter={8}>
          <Col span={8}>
            <Statistic
              title="Max pairwise"
              value={fmtCorr(summary.max_pairwise)}
              valueStyle={{ color: summary.max_pairwise >= 0.7 ? '#cf1322' : '#3f8600', fontSize: 18 }}
              suffix={deltaTag(summary.max_pairwise, baseline?.max_pairwise)}
            />
          </Col>
          <Col span={8}>
            <Statistic
              title="Median pairwise"
              value={fmtCorr(summary.median_pairwise)}
              valueStyle={{ fontSize: 18 }}
              suffix={deltaTag(summary.median_pairwise, baseline?.median_pairwise)}
            />
          </Col>
          <Col span={8}>
            <Statistic
              title="Mean pairwise"
              value={fmtCorr(summary.mean_pairwise)}
              valueStyle={{ fontSize: 18 }}
              suffix={deltaTag(summary.mean_pairwise, baseline?.mean_pairwise)}
            />
          </Col>
          {summary.hotspots && summary.hotspots.length > 0 && (
            <Col span={24} style={{ marginTop: 12 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                <WarningOutlined /> {summary.hotspots.length} 个 hotspot ≥ 阈值
              </Text>
            </Col>
          )}
        </Row>
      )}
    </Card>
  )
}

function HotspotsTable({ payload }) {
  // Aggregate all hotspots across windows + baseline; mark which window each
  // came from so the user sees where the spike materialised.
  const rows = []
  const push = (windowKey, items) => {
    for (const h of items || []) {
      rows.push({
        key: `${windowKey}-${h.a}-${h.b}`,
        window: windowKey,
        a: h.a,
        b: h.b,
        corr: h.corr,
      })
    }
  }
  push('baseline', payload?.baseline?.hotspots)
  Object.entries(payload?.windows || {}).forEach(([k, v]) => push(k, v.hotspots))

  if (!rows.length) {
    return <Empty description="No hotspots above threshold" />
  }

  const columns = [
    {
      title: '窗口',
      dataIndex: 'window',
      filters: [
        { text: 'Baseline (4yr)', value: 'baseline' },
        ...Object.keys(payload?.windows || {}).map((k) => ({
          text: WINDOW_LABELS[k] || k,
          value: k,
        })),
      ],
      onFilter: (val, row) => row.window === val,
      render: (val) =>
        val === 'baseline' ? (
          <Tag>Baseline</Tag>
        ) : (
          <Tag color="purple">{WINDOW_LABELS[val] || val}</Tag>
        ),
      width: 220,
    },
    {
      title: 'Alpha A',
      dataIndex: 'a',
      render: (v) => (
        <a href={`/alphas/${v}`} target="_blank" rel="noreferrer">
          {v}
        </a>
      ),
    },
    {
      title: 'Alpha B',
      dataIndex: 'b',
      render: (v) => (
        <a href={`/alphas/${v}`} target="_blank" rel="noreferrer">
          {v}
        </a>
      ),
    },
    {
      title: 'Correlation',
      dataIndex: 'corr',
      sorter: (a, b) => a.corr - b.corr,
      defaultSortOrder: 'descend',
      render: (v) => <Tag color={corrColor(v)}>{fmtCorr(v)}</Tag>,
      width: 140,
    },
  ]

  return (
    <Table
      size="small"
      columns={columns}
      dataSource={rows}
      pagination={{ pageSize: 20, showSizeChanger: true }}
    />
  )
}

export default function CrisisStressTest() {
  const [region, setRegion] = useState('USA')
  const queryClient = useQueryClient()

  const summaryQuery = useQuery({
    queryKey: ['crisis-summary', region],
    queryFn: () => api.getCrisisSummary(region, { refresh: false }),
    staleTime: 5 * 60 * 1000,
  })

  const refreshMutation = useMutation({
    mutationFn: () => api.getCrisisSummary(region, { refresh: true }),
    onSuccess: (data) => {
      queryClient.setQueryData(['crisis-summary', region], data)
    },
  })

  const payload = summaryQuery.data
  const empty = !payload || payload.status !== 'ok'

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>
            <ThunderboltOutlined /> 危机窗口压力测试
          </Title>
          <Text type="secondary">
            评估 OS 因子池在历史危机区间下的相关性收敛度。每日 06:30 自动刷新。
          </Text>
        </Col>
        <Col>
          <Space>
            <Select
              value={region}
              onChange={setRegion}
              options={REGIONS.map((r) => ({ value: r, label: r }))}
              style={{ width: 120 }}
            />
            <Button
              icon={<ReloadOutlined />}
              loading={refreshMutation.isPending}
              onClick={() => refreshMutation.mutate()}
            >
              立即重算
            </Button>
          </Space>
        </Col>
      </Row>

      {payload?.computed_at && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space>
              <Text>快照时间：{new Date(payload.computed_at).toLocaleString()}</Text>
              <Text type="secondary">·</Text>
              <Text>Hotspot 阈值：{payload.hotspot_threshold}</Text>
            </Space>
          }
        />
      )}

      {empty ? (
        <Empty
          description={
            <Space direction="vertical">
              <Text>
                还没有 {region} 区域的 OS PnL 缓存（status: {payload?.status || 'empty'}）。
              </Text>
              <Text type="secondary">
                需要先运行 Celery beat 任务 <code>refresh_os_correlation_cache</code>，
                或等待每日 06:30 的自动刷新。
              </Text>
            </Space>
          }
        />
      ) : (
        <>
          <Card
            size="small"
            style={{ marginBottom: 16 }}
            title={
              <Space>
                <Text strong>Baseline (LOOKBACK=4yr)</Text>
                <Tag>{payload.baseline.n_alphas} alphas · {payload.baseline.n_obs} obs</Tag>
              </Space>
            }
          >
            <Row gutter={16}>
              <Col span={6}>
                <Statistic
                  title="Max pairwise"
                  value={fmtCorr(payload.baseline.max_pairwise)}
                  valueStyle={{
                    color: payload.baseline.max_pairwise >= 0.7 ? '#cf1322' : '#3f8600',
                  }}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="Median pairwise"
                  value={fmtCorr(payload.baseline.median_pairwise)}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="Mean pairwise"
                  value={fmtCorr(payload.baseline.mean_pairwise)}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="Hotspots"
                  value={payload.baseline.hotspots?.length || 0}
                  valueStyle={{
                    color: (payload.baseline.hotspots?.length || 0) > 0 ? '#fa541c' : '#52c41a',
                  }}
                />
              </Col>
            </Row>
          </Card>

          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            {Object.entries(payload.windows).map(([name, summary]) => (
              <Col xs={24} md={12} lg={12} xl={6} key={name}>
                <WindowCard name={name} summary={summary} baseline={payload.baseline} />
              </Col>
            ))}
          </Row>

          <Card
            size="small"
            title={
              <Space>
                <Text strong>Hotspot pairs</Text>
                <Text type="secondary">
                  pairwise corr ≥ {payload.hotspot_threshold} 的 alpha 对
                </Text>
              </Space>
            }
          >
            <HotspotsTable payload={payload} />
          </Card>

          <Card size="small" style={{ marginTop: 16 }}>
            <Descriptions title="如何解读" column={1} size="small">
              <Descriptions.Item label="Baseline vs 危机窗口">
                Baseline 用 4 年滚动窗口；危机窗口在 baseline 之外独立切片。
                同一对 alpha 在 baseline 看起来不相关 (0.3)，但在 COVID 窗口跳到 0.85，
                意味着"正常时期独立、危机时期共振"——典型的隐性集中度风险。
              </Descriptions.Item>
              <Descriptions.Item label="使用方式">
                提交决策时优先检查待提交 alpha 在 4 个窗口下相对池子的 max-corr
                （AlphaDetail 页面的危机相关性面板）。若任一窗口 ≥ 0.7，慎重提交。
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </>
      )}
    </div>
  )
}
