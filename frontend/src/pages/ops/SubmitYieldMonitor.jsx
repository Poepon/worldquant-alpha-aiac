import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Alert, Button, Card, Col, Empty, Row, Select, Space, Spin, Statistic, Typography,
} from 'antd'
import {
  ReloadOutlined, FunnelPlotOutlined, ArrowRightOutlined, WarningOutlined,
} from '@ant-design/icons'
import {
  Bar,
  CartesianGrid,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
  ComposedChart,
} from 'recharts'

import api from '../../services/api'

const { Title, Text } = Typography

/**
 * SubmitYieldMonitor — /ops/submit-yield 「提交产率(submit-yield)」.
 *
 * Visualises the execution-limited bottleneck: produced → can_submit →
 * submitted funnel + conversion rates + daily trend. The vast majority of
 * mined alphas never get submitted (submit is the real bottleneck, not
 * discovery). Recent can_submit collapse (疑字段卫生) is surfaced as a warning.
 *
 * Data: api.getOpsSubmitYield({ days, region }) — refetch every 30s.
 */

const DAYS_OPTIONS = [
  { value: 7, label: '近 7 天' },
  { value: 14, label: '近 14 天' },
  { value: 30, label: '近 30 天' },
  { value: 90, label: '近 90 天' },
]

const REGION_OPTIONS = [
  { value: '', label: '全部区域' },
  { value: 'USA', label: 'USA' },
  { value: 'CHN', label: 'CHN' },
  { value: 'EUR', label: 'EUR' },
  { value: 'HKG', label: 'HKG' },
  { value: 'JPN', label: 'JPN' },
]

// Friendly percentage: 0.0058 → "0.58%"; tiny values fall back to bps.
function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return '—'
  const pct = v * 100
  if (pct > 0 && pct < 0.01) return `${(v * 10000).toFixed(1)} bps`
  return `${pct.toFixed(2)}%`
}

function fmtInt(v) {
  if (v == null || Number.isNaN(v)) return '0'
  return Number(v).toLocaleString()
}

// Arrow with a conversion-rate label between two funnel stages.
function FunnelArrow({ label, value }) {
  return (
    <Col flex="none" style={{ textAlign: 'center', minWidth: 110 }}>
      <ArrowRightOutlined style={{ fontSize: 24, color: '#888' }} />
      <div style={{ fontSize: 12, color: '#888', marginTop: 4 }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 600 }}>{fmtPct(value)}</div>
    </Col>
  )
}

export default function SubmitYieldMonitor() {
  const [days, setDays] = useState(30)
  const [region, setRegion] = useState('')

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['opsSubmitYield', days, region],
    queryFn: () => api.getOpsSubmitYield({ days, region: region || undefined }),
    refetchInterval: 30000,
  })

  const totals = data?.totals || {}
  const conversion = data?.conversion || {}
  const daily = data?.daily || []

  const produced = totals.produced || 0
  const canSubmit = totals.can_submit || 0
  const submitted = totals.submitted || 0

  // Recent 7-day can_submit sum: detect the submit-yield collapse.
  const recent7CanSubmit = useMemo(() => {
    if (!daily.length) return null
    return daily.slice(-7).reduce((acc, d) => acc + (d.can_submit || 0), 0)
  }, [daily])

  const hasAnyData = produced > 0 || daily.length > 0

  if (isLoading) {
    return <Spin tip="加载提交产率..." style={{ marginTop: 80, display: 'block' }} />
  }

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <FunnelPlotOutlined /> 提交产率 (submit-yield)
        </Title>
        <Space>
          <Select
            value={days}
            onChange={setDays}
            options={DAYS_OPTIONS}
            style={{ width: 120 }}
          />
          <Select
            value={region}
            onChange={setRegion}
            options={REGION_OPTIONS}
            style={{ width: 130 }}
          />
          <Button icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>
            刷新
          </Button>
        </Space>
      </Row>

      {isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="拉取提交产率失败(端点 /ops/submit-yield)。"
          description={String(error?.message || error || '未知错误')}
        />
      )}

      {!isError && !hasAnyData && (
        <Empty description="所选窗口内无数据" style={{ marginTop: 60 }} />
      )}

      {!isError && hasAnyData && (
        <>
          {/* ---- 核心 Alert：瓶颈定性 ---------------------------------- */}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              `全窗口 ${fmtInt(produced)} 产出 → ${fmtInt(canSubmit)} 可提交 → ${fmtInt(submitted)} 已提交，` +
              `提交是真瓶颈(execution-limited)——绝大多数挖出的 alpha 从不被提交。`
            }
            description={
              `可提交率 ${fmtPct(conversion.can_submit_rate)} · ` +
              `可提交→提交 ${fmtPct(conversion.submit_of_can_submit)} · ` +
              `提交率 ${fmtPct(conversion.submit_rate)}`
            }
          />

          {recent7CanSubmit != null && recent7CanSubmit <= 0 && (
            <Alert
              type="warning"
              showIcon
              icon={<WarningOutlined />}
              style={{ marginBottom: 16 }}
              message="近 7 天 can_submit 合计 ≈ 0 — submit-yield 塌方(疑字段卫生缺失),需排查生成层字段过滤。"
            />
          )}

          {/* ---- 漏斗:produced → can_submit → submitted -------------- */}
          <Card size="small" style={{ marginBottom: 16 }}>
            <Row align="middle" justify="space-around" gutter={[8, 16]} wrap>
              <Col flex="none">
                <Statistic
                  title="产出 (produced)"
                  value={produced}
                  valueStyle={{ color: '#1677ff' }}
                />
              </Col>

              <FunnelArrow label="可提交率" value={conversion.can_submit_rate} />

              <Col flex="none">
                <Statistic
                  title="可提交 (can_submit)"
                  value={canSubmit}
                  valueStyle={{ color: '#faad14' }}
                />
              </Col>

              <FunnelArrow label="可提交→提交" value={conversion.submit_of_can_submit} />

              <Col flex="none">
                <Statistic
                  title="已提交 (submitted)"
                  value={submitted}
                  valueStyle={{ color: submitted > 0 ? '#3f8600' : '#cf1322' }}
                />
              </Col>
            </Row>
            <div style={{ textAlign: 'center', marginTop: 12 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                端到端提交率(submit_rate)= 已提交 / 产出 ={' '}
                <b>{fmtPct(conversion.submit_rate)}</b>
                {region ? ` · 区域 ${region}` : ' · 全部区域'} · 近 {data?.window_days ?? days} 天
              </Text>
            </div>
          </Card>

          {/* ---- daily 趋势图(双轴)---------------------------------- */}
          <Card size="small" title="每日趋势(produced 左轴 · can_submit/submitted 右轴)">
            {daily.length === 0 ? (
              <Empty description="窗口内无每日数据" />
            ) : (
              <ResponsiveContainer width="100%" height={340}>
                <ComposedChart data={daily}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                  <XAxis dataKey="date" stroke="#888" />
                  <YAxis
                    yAxisId="left"
                    stroke="#1677ff"
                    label={{ value: 'produced', angle: -90, position: 'insideLeft', fill: '#1677ff', fontSize: 12 }}
                  />
                  <YAxis
                    yAxisId="right"
                    orientation="right"
                    stroke="#faad14"
                    label={{ value: 'can_submit / submitted', angle: 90, position: 'insideRight', fill: '#faad14', fontSize: 12 }}
                  />
                  <ReTooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                  <Legend />
                  <Bar
                    yAxisId="left"
                    dataKey="produced"
                    name="产出 (produced)"
                    fill="#1677ff"
                    fillOpacity={0.35}
                    barSize={18}
                  />
                  <Line
                    yAxisId="right"
                    type="monotone"
                    dataKey="can_submit"
                    name="可提交 (can_submit)"
                    stroke="#faad14"
                    strokeWidth={2}
                    dot={{ r: 2 }}
                  />
                  <Line
                    yAxisId="right"
                    type="monotone"
                    dataKey="submitted"
                    name="已提交 (submitted)"
                    stroke="#3f8600"
                    strokeWidth={2}
                    dot={{ r: 3 }}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
