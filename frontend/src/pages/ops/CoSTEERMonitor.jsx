import { useState } from 'react'
import { Alert, Col, Empty, Row, Select, Space, Statistic, Tag, Tooltip as AntdTooltip } from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import useOpsData from './hooks/useOpsData'

/**
 * CoSTEERMonitor — /ops/costeer 页面（2026-06-07 池世界改写）。
 *
 * 历史上本页可视化 R1a 归因 + R1b 重试链 + R8 RAG 三组遥测。在四池切换中
 * (1b-flip: R1A_HOOK / R1B_* OFF；1c-delete: r1b 模块删除) CoSTEER 反馈环
 * 被停用，原 r1a/r1b/chain-depth/deploy-recommendation 四个端点读的都是已
 * 冻结的死表。本次改写**弃用**那四个面板，只保留两个 live RAG 健康端点：
 *   - api.getOpsR8KbShape()   —— 知识库语料形状（条目类型 / 支柱覆盖 / 衰减）
 *   - api.getOpsR8QueryStats(days) —— RAG 检索运行期统计（层级命中 / 缓存 / 地区）
 * 页面重定位为「知识库与 RAG 健康」。
 */
export default function CoSTEERMonitor() {
  const [days, setDays] = useState(7)

  const r8 = useOpsData(() => api.getOpsR8KbShape(), [])
  const r8Query = useOpsData(() => api.getOpsR8QueryStats(days), [days])

  const r8Payload = r8.data || {}
  const r8QueryPayload = r8Query.data || {}

  // Flag 状态标签
  const flagTag = (label, on) => (
    <Tag color={on ? 'success' : 'default'} key={label}>
      {label}: {on ? '开' : '关'}
    </Tag>
  )

  // KB 条目类型柱图数据（活跃 vs 已衰减堆叠）
  const entryTypeBars = (r8Payload.entry_types || []).map((b) => ({
    type: b.entry_type,
    active: b.active_count,
    decayed: b.decayed_count,
  }))

  // 支柱覆盖饼图数据
  const pillarPie = (r8Payload.pillars || []).map((p) => ({
    name: p.pillar,
    value: p.entry_count,
  }))

  // 层级命中率柱图数据（rate ∈ [0,1] → 百分比）
  const layerBars = Object.entries(r8QueryPayload.layer_hit_rates || {}).map(
    ([layer, rate]) => ({
      layer,
      rate: Math.round((rate || 0) * 10000) / 100,
    }),
  )

  // 地区查询数柱图数据
  const regionBars = Object.entries(r8QueryPayload.by_region || {}).map(
    ([region, count]) => ({ region, count }),
  )

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {/* 诚实 banner — 说明知识库反馈环已停用 */}
      <Alert
        type="info"
        showIcon
        message="知识库反馈环已在流水线改造中停用"
        description={
          <span>
            知识库反馈环（归因分析 / 重试链）已在挖掘流水线改造中停用；
            将在第二阶段以流水线原生的知识库对账定时任务重新接入。
            本页现展示知识库与知识检索的健康状况。
          </span>
        }
      />

      <OpsSectionCard
        title="知识库与知识检索健康（实时遥测）"
        source="service"
        loading={r8.loading || r8Query.loading}
        onRefresh={() => {
          r8.refetch()
          r8Query.refetch()
        }}
      >
        <Space size="middle" style={{ marginBottom: 16 }}>
          <span>检索统计时间窗口：</span>
          <Select
            value={days}
            onChange={setDays}
            style={{ width: 130 }}
            options={[
              { value: 1, label: '近 1 天' },
              { value: 7, label: '近 7 天' },
              { value: 14, label: '近 14 天' },
              { value: 30, label: '近 30 天' },
            ]}
          />
        </Space>

        {/* 当前开关状态行 */}
        <Alert
          message={
            <Space wrap>
              <strong>知识检索开关状态：</strong>
              {Object.entries(r8Payload.flags || {}).map(([k, v]) => flagTag(k, v))}
              {Object.entries(r8QueryPayload.flags || {})
                .filter(([k]) => k === 'ENABLE_R8_QUERY_LOG')
                .map(([k, v]) => flagTag(k, v))}
            </Space>
          }
          type="info"
          showIcon={false}
          style={{ marginBottom: 16 }}
        />

        {/* KPI 行 */}
        <Row gutter={[16, 16]}>
          <Col xs={24} md={6}>
            <AntdTooltip title="知识库内活跃（未过期）条目总数 — 层级知识检索可匹配的语料深度">
              <Statistic title="活跃条目总数" value={r8Payload.total_active ?? 0} />
            </AntdTooltip>
          </Col>
          <Col xs={24} md={6}>
            <AntdTooltip title="已被标记过期的条目数 — 仅失败经验侧检索仍会包含，成功经验侧排除">
              <Statistic title="已过期条目总数" value={r8Payload.total_decayed ?? 0} />
            </AntdTooltip>
          </Col>
          <Col xs={24} md={6}>
            <AntdTooltip title="窗口内知识检索总次数（仅检索日志开关打开时记录）">
              <Statistic
                title={`知识检索次数（近 ${days} 天）`}
                value={r8QueryPayload.total_queries ?? 0}
              />
            </AntdTooltip>
          </Col>
          <Col xs={24} md={6}>
            <AntdTooltip title="窗口内任一层从缓存命中的查询比例">
              <Statistic
                title="缓存命中率"
                value={((r8QueryPayload.cache_hit_rate ?? 0) * 100).toFixed(2)}
                suffix="%"
              />
            </AntdTooltip>
          </Col>
        </Row>
      </OpsSectionCard>

      {/* R8 知识库 shape — 语料形状（活跃 vs 已衰减 + 支柱覆盖） */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <OpsSectionCard title="知识库条目类型（活跃 vs 已过期）">
            {entryTypeBars.length === 0 ? (
              <Empty description="知识库无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={entryTypeBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="type" />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Legend formatter={(v) => (v === 'active' ? '活跃' : '已过期')} />
                  <Bar dataKey="active" stackId="kb" fill="#1677ff" />
                  <Bar dataKey="decayed" stackId="kb" fill="#bfbfbf" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag color="blue">活跃总数：{r8Payload.total_active ?? 0}</Tag>
              <Tag>已过期总数：{r8Payload.total_decayed ?? 0}</Tag>
              <Tag color="success">
                活跃成功模式：{r8Payload.success_pattern_active ?? 0}
              </Tag>
              <Tag color="error">
                活跃失败教训：{r8Payload.failure_pitfall_active ?? 0}
              </Tag>
              <Tag color="purple">
                可排序成功条数：{r8Payload.r5_rankable_success_count ?? 0}
              </Tag>
            </Space>
          </OpsSectionCard>
        </Col>

        <Col xs={24} lg={10}>
          <OpsSectionCard title="因子类别覆盖（活跃条目）">
            {pillarPie.length === 0 ? (
              <Empty description="暂无因子类别数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie
                    data={pillarPie}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    label={({ name, value }) => `${name}: ${value}`}
                  >
                    {pillarPie.map((p) => (
                      <Cell
                        key={p.name}
                        fill={p.name === 'none' ? '#bfbfbf' : '#13c2c2'}
                      />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            )}
            <div style={{ marginTop: 12, color: '#8c8c8c', fontSize: 12 }}>
              「none」一桶表示条目缺少因子类别标签——知识检索的类别层无法命中，是待补全的候选。
            </div>
          </OpsSectionCard>
        </Col>
      </Row>

      {/* R8 实时层命中率 — 仅在 ENABLE_R8_QUERY_LOG 打开时有数据 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <OpsSectionCard title="知识检索实时层级命中率（窗口内每层被命中的比例）">
            {(r8QueryPayload.total_queries ?? 0) === 0 ? (
              <Empty
                description={
                  r8QueryPayload.flags?.ENABLE_R8_QUERY_LOG
                    ? '窗口内暂无查询'
                    : '检索日志开关未开启 — 打开后才会记录实时层级跳转'
                }
              />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={layerBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="layer" />
                  <YAxis
                    allowDecimals
                    label={{ value: '%', angle: -90, position: 'insideLeft' }}
                  />
                  <Tooltip formatter={(v) => `${Number(v).toFixed(2)}%`} />
                  <Bar dataKey="rate" fill="#13c2c2" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag>总查询数：{r8QueryPayload.total_queries ?? 0}</Tag>
              <Tag color={r8QueryPayload.cache_hit_rate > 0 ? 'success' : 'default'}>
                缓存命中率：{((r8QueryPayload.cache_hit_rate ?? 0) * 100).toFixed(2)}%
              </Tag>
              <Tag color="purple">
                失败树升级率：
                {((r8QueryPayload.failure_tree_elevation_rate ?? 0) * 100).toFixed(2)}%
              </Tag>
            </Space>
            <div style={{ marginTop: 8, color: '#8c8c8c', fontSize: 12 }}>
              健康状态下应由 L0、L1 层主导（高特异性命中），L2、L3 层只作兜底长尾；
              若 L3 层主导，说明语料对高层覆盖太薄。
            </div>
          </OpsSectionCard>
        </Col>

        <Col xs={24} lg={10}>
          <OpsSectionCard title="知识检索各地区查询数">
            {regionBars.length === 0 ? (
              <Empty description="暂无地区数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={regionBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="region" />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Bar dataKey="count" fill="#722ed1" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
      </Row>
    </Space>
  )
}
