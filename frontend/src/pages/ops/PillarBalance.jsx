import { useMemo, useState } from 'react'
import {
  Col,
  Empty,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import RerunButton from './components/RerunButton'
import useOpsData from './hooks/useOpsData'

/**
 * PillarBalance — /ops/pillar-balance page (P3 P2-B).
 *
 * Region dropdown drives all three charts (Radar / Deficit bar /
 * 14d shares LineChart). The deficit chip in the KPI strip surfaces
 * the next pillar that the mining nudge would prefer.
 *
 * Data flow:
 *  - /pillar/latest is the freshest single-snapshot read (today =
 *    fresh service path).
 *  - /pillar/history powers the shares LineChart.
 *  - /pillar/deficit-recommendation is a tiny extra call so the KPI
 *    chip can stay in sync with what the mining loop would do — uses
 *    PillarService.get_next_pillar_for_region.
 */
export default function PillarBalance() {
  const latest = useOpsData(() => api.getOpsPillarLatest(), [])
  const history = useOpsData(() => api.getOpsPillarHistory(14), [])

  const regions = useMemo(
    () => Object.keys(latest.data?.payload?.regions || {}).sort(),
    [latest.data],
  )
  const [selectedRegion, setSelectedRegion] = useState(null)
  // Default to first region on first successful load
  const activeRegion = selectedRegion || regions[0] || null

  const deficit = useOpsData(
    () => (activeRegion ? api.getOpsPillarDeficit(activeRegion) : Promise.resolve(null)),
    [activeRegion],
  )

  const regionBlock = activeRegion
    ? latest.data?.payload?.regions?.[activeRegion] || {}
    : {}

  // ---- Radar data: target vs actual share per pillar -----------------
  const radarRows = useMemo(() => {
    if (!regionBlock.shares) return []
    return Object.keys(regionBlock.target || {}).map((pillar) => ({
      pillar,
      target: (regionBlock.target[pillar] || 0) * 100,
      actual: (regionBlock.shares[pillar] || 0) * 100,
    }))
  }, [regionBlock])

  // ---- Deficit bar (sorted desc) -------------------------------------
  const deficitRows = useMemo(() => {
    if (!regionBlock.deficits) return []
    return Object.entries(regionBlock.deficits)
      .map(([pillar, gap]) => ({ pillar, gap: gap * 100 }))
      .filter((d) => d.gap > 0)
      .sort((a, b) => b.gap - a.gap)
  }, [regionBlock])

  // ---- 14d shares trend ----------------------------------------------
  const trendRows = useMemo(() => {
    if (!history.data || !activeRegion) return []
    return history.data
      .map((entry) => {
        const block = entry.regions?.[activeRegion]
        if (!block?.shares) return null
        return {
          date: entry._date,
          ...Object.fromEntries(
            Object.entries(block.shares).map(([k, v]) => [k, v * 100]),
          ),
        }
      })
      .filter(Boolean)
  }, [history.data, activeRegion])

  const pillarKeys = Object.keys(regionBlock.target || {})

  const handleRerunSuccess = () =>
    setTimeout(() => {
      latest.refetch()
      history.refetch()
      deficit.refetch()
    }, 3000)

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title="五支柱平衡"
        source={latest.data?.source}
        staleDays={latest.data?.payload?._stale_days}
        onRefresh={latest.refetch}
        loading={latest.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsPillar}
            label="重跑 pillar-balance"
            onSuccess={handleRerunSuccess}
          />
        }
      >
        {latest.loading && !latest.data ? (
          <Spin />
        ) : regions.length === 0 ? (
          <Empty description="暂无 pillar 数据(7 日内无 alpha 或 beat 未跑过)" />
        ) : (
          <Row gutter={[16, 16]} align="middle">
            <Col xs={24} sm={8}>
              <span style={{ marginRight: 8 }}>Region:</span>
              <Select
                value={activeRegion}
                onChange={setSelectedRegion}
                options={regions.map((r) => ({ label: r, value: r }))}
                style={{ width: 160 }}
              />
            </Col>
            <Col xs={12} sm={4}>
              <Statistic title="已 stamped" value={regionBlock.stamped_total || 0} />
            </Col>
            <Col xs={12} sm={4}>
              <Statistic title="legacy inferred" value={regionBlock.legacy_inferred_total || 0} />
            </Col>
            <Col xs={12} sm={4}>
              <Statistic
                title="skew"
                value={regionBlock.skew}
                precision={3}
              />
            </Col>
            <Col xs={12} sm={4}>
              <span style={{ fontSize: 12, color: '#888' }}>Nudge target:</span>
              <div>
                {deficit.data?.next_pillar ? (
                  <Tag color="orange" style={{ fontSize: 13 }}>
                    {deficit.data.next_pillar}
                  </Tag>
                ) : (
                  <Tag>平衡</Tag>
                )}
              </div>
            </Col>
          </Row>
        )}
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <OpsSectionCard
            title={`Radar · target vs actual${activeRegion ? ` · ${activeRegion}` : ''}`}
            source={latest.data?.source}
          >
            {radarRows.length === 0 ? (
              <Empty description="无 region 数据" />
            ) : (
              <ResponsiveContainer width="100%" height={320}>
                <RadarChart data={radarRows}>
                  <PolarGrid stroke="rgba(255,255,255,0.1)" />
                  <PolarAngleAxis dataKey="pillar" stroke="#888" />
                  <PolarRadiusAxis stroke="#888" />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                  <Legend />
                  <Radar
                    name="target %"
                    dataKey="target"
                    stroke="#9c88ff"
                    fill="#9c88ff"
                    fillOpacity={0.3}
                  />
                  <Radar
                    name="actual %"
                    dataKey="actual"
                    stroke="#00d4ff"
                    fill="#00d4ff"
                    fillOpacity={0.3}
                  />
                </RadarChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>

        <Col xs={24} md={12}>
          <OpsSectionCard title="Deficit 排序" source={latest.data?.source}>
            {deficitRows.length === 0 ? (
              <Empty description="所有 pillar 已达标 ✓" />
            ) : (
              <ResponsiveContainer width="100%" height={320}>
                <BarChart data={deficitRows} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                  <XAxis type="number" stroke="#888" unit="%" />
                  <YAxis dataKey="pillar" type="category" stroke="#888" width={100} />
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
                  <Bar dataKey="gap" fill="#ff8c00" name="deficit %" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
      </Row>

      <OpsSectionCard title="14 天 shares 趋势(按 pillar)" source="docs_archived">
        {trendRows.length === 0 ? (
          <Empty description="历史不足" />
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={trendRows}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis dataKey="date" stroke="#888" />
              <YAxis stroke="#888" unit="%" />
              <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }} />
              <Legend />
              {pillarKeys.map((p, i) => {
                const palette = ['#00ff88', '#00d4ff', '#ffb700', '#ff8c00', '#9c88ff', '#ff4d4f']
                return (
                  <Line
                    key={p}
                    type="monotone"
                    dataKey={p}
                    stroke={palette[i % palette.length]}
                    strokeWidth={2}
                    dot={false}
                  />
                )
              })}
            </LineChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>
    </Space>
  )
}
