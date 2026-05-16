import { useMemo, useState } from 'react'
import {
  Card,
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
} from 'antd'
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import RerunButton from './components/RerunButton'
import useOpsData from './hooks/useOpsData'

// Same 5-bucket scheme as backend/regime_classifier.py. Order matches
// the severity ramp; colors match the SourceTagBadge palette so the
// dashboard reads "red = stressed, green = calm" at a glance.
const REGIME_ORDER = ['crisis', 'elevated', 'normal', 'calm', 'very_calm']
const REGIME_COLORS = {
  crisis: '#ff4d4f',
  elevated: '#ff8c00',
  normal: '#9c88ff',
  calm: '#00ff88',
  very_calm: '#00d4ff',
}
const REGIONS = ['USA', 'CHN', 'EUR', 'ASI', 'GLB']

/**
 * Regime — /ops/regime page (P3 P2-C).
 *
 * Five region cards along the top (one per BRAIN region), each showing
 * the current regime + confidence + cold-start tag. Below: per-region
 * 14d pass_rate trend line with regime band in the background, plus
 * a per-day distribution stacked bar across all regions.
 */
export default function Regime() {
  // One /current call per region — cheap (Redis read each). We render
  // each region card independently so partial failures stay isolated.
  const usaCurrent = useOpsData(() => api.getOpsRegimeCurrent('USA'), [])
  const chnCurrent = useOpsData(() => api.getOpsRegimeCurrent('CHN'), [])
  const eurCurrent = useOpsData(() => api.getOpsRegimeCurrent('EUR'), [])
  const asiCurrent = useOpsData(() => api.getOpsRegimeCurrent('ASI'), [])
  const glbCurrent = useOpsData(() => api.getOpsRegimeCurrent('GLB'), [])
  const currents = {
    USA: usaCurrent, CHN: chnCurrent, EUR: eurCurrent,
    ASI: asiCurrent, GLB: glbCurrent,
  }

  const [selectedRegion, setSelectedRegion] = useState('USA')
  const snapshot = useOpsData(
    () => api.getOpsRegimeSnapshot(selectedRegion),
    [selectedRegion],
  )
  const history = useOpsData(
    () => api.getOpsRegimeHistory(selectedRegion, 14),
    [selectedRegion],
  )

  // ---- trend rows: pass_rate + regime tag for chart shading ---------
  const trendRows = useMemo(
    () => (history.data || []).map((d) => ({
      date: d.date,
      pass_rate: typeof d.pass_rate === 'number' ? d.pass_rate * 100 : null,
      pass_rate_7d: typeof d.pass_rate_7d_mean === 'number'
        ? d.pass_rate_7d_mean * 100
        : null,
      regime: d.regime,
      confidence: d.confidence,
    })),
    [history.data],
  )

  // ---- per-day regime distribution across all 5 regions -------------
  // We aggregate from all 5 current calls + historical data is not
  // available cross-region without another round-trip, so this card
  // just shows "today" — useful for quick "all regions in crisis?" read.
  const distRows = useMemo(() => {
    const counts = Object.fromEntries(REGIME_ORDER.map((r) => [r, 0]))
    for (const [, hook] of Object.entries(currents)) {
      const r = hook.data?.regime
      if (r && counts[r] !== undefined) counts[r] += 1
    }
    return [{ date: 'today', ...counts }]
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, Object.values(currents).map((c) => c.data?.regime))

  const handleRerunSuccess = () =>
    setTimeout(() => {
      Object.values(currents).forEach((h) => h.refetch())
      snapshot.refetch()
      history.refetch()
    }, 3000)

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="市场体制"
        source="service"
        rerunSlot={
          <RerunButton
            triggerFn={() => api.rerunOpsRegime()}
            label="重跑 regime-infer"
            onSuccess={handleRerunSuccess}
          />
        }
      >
        <Row gutter={[12, 12]}>
          {REGIONS.map((region) => {
            const hook = currents[region]
            const regime = hook.data?.regime
            const isSelected = selectedRegion === region
            return (
              <Col key={region} xs={12} sm={8} md={4}>
                <Card
                  hoverable
                  onClick={() => setSelectedRegion(region)}
                  style={{
                    background: isSelected
                      ? 'rgba(0,212,255,0.1)'
                      : 'rgba(255,255,255,0.02)',
                    borderColor: isSelected ? '#00d4ff' : 'rgba(255,255,255,0.1)',
                  }}
                >
                  <Statistic
                    title={region}
                    value={regime || '—'}
                    valueStyle={{
                      color: REGIME_COLORS[regime] || '#888',
                      fontSize: 18,
                    }}
                  />
                  {hook.loading ? (
                    <Spin size="small" />
                  ) : !regime ? (
                    <Tag>cold-start</Tag>
                  ) : (
                    <Tag color={REGIME_COLORS[regime] ? 'success' : 'default'}>
                      live
                    </Tag>
                  )}
                </Card>
              </Col>
            )
          })}
        </Row>
      </OpsSectionCard>

      <OpsSectionCard
        title={`${selectedRegion} · 14 天 pass_rate 趋势`}
        source={snapshot.data?.source}
      >
        {trendRows.length === 0 ? (
          <Empty description="历史不足" />
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis dataKey="date" stroke="#888" />
              <YAxis stroke="#888" unit="%" />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
                formatter={(v, name) =>
                  typeof v === 'number' ? [v.toFixed(2), name] : [v, name]
                }
              />
              <Legend />
              <Area
                type="monotone"
                dataKey="pass_rate"
                stroke="#00d4ff"
                fill="#00d4ff"
                fillOpacity={0.25}
                name="pass_rate %"
              />
              <Line
                type="monotone"
                dataKey="pass_rate_7d"
                stroke="#ff8c00"
                strokeWidth={2}
                dot={false}
                name="pass_rate 7d EWMA %"
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <OpsSectionCard title="今日各 region 分布" source="service">
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={distRows}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                <XAxis dataKey="date" stroke="#888" />
                <YAxis stroke="#888" allowDecimals={false} />
                <Tooltip
                  contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
                />
                <Legend />
                {REGIME_ORDER.map((r) => (
                  <Bar key={r} dataKey={r} stackId="a" fill={REGIME_COLORS[r]} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={12}>
          <OpsSectionCard
            title={`${selectedRegion} 推断快照`}
            source={snapshot.data?.source}
          >
            {snapshot.loading && !snapshot.data ? (
              <Spin />
            ) : Object.keys(snapshot.data?.snapshot || {}).length === 0 ? (
              <Empty description="尚无 snapshot(等待今日 beat)" />
            ) : (
              <Row gutter={[12, 12]}>
                <Col span={12}>
                  <Statistic
                    title="regime"
                    value={snapshot.data.snapshot.regime || '—'}
                    valueStyle={{
                      color: REGIME_COLORS[snapshot.data.snapshot.regime] || '#888',
                    }}
                  />
                </Col>
                <Col span={12}>
                  <Statistic
                    title="confidence"
                    value={snapshot.data.snapshot.confidence}
                    precision={2}
                  />
                </Col>
                <Col span={12}>
                  <Statistic
                    title="pass_rate"
                    value={snapshot.data.snapshot.pass_rate}
                    precision={3}
                  />
                </Col>
                <Col span={12}>
                  <Statistic
                    title="7d EWMA"
                    value={snapshot.data.snapshot.pass_rate_7d_mean}
                    precision={3}
                  />
                </Col>
                <Col span={24}>
                  <Tag color={snapshot.data.snapshot.cold_start ? 'warning' : 'success'}>
                    {snapshot.data.snapshot.cold_start ? 'COLD START' : 'WARM'}
                  </Tag>
                </Col>
              </Row>
            )}
          </OpsSectionCard>
        </Col>
      </Row>
    </Space>
  )
}
