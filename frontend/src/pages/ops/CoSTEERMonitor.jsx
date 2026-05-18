import { useState } from 'react'
import {
  Alert,
  Col,
  Empty,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip as AntdTooltip,
} from 'antd'
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
 * CoSTEERMonitor — /ops/costeer 页面 (2026-05-18).
 *
 * 可视化 R1a + R1b + R8 三组遥测端点。Operator 用此页判断
 * 是否可推进 flag (例如 R1b 重试成功率 ≥15% 才打开
 * ENABLE_R1B_HYPOTHESIS_MUTATE)。顶部时间窗口下拉同时
 * 驱动所有相关接口,确保 KPI 可比。
 */
export default function CoSTEERMonitor() {
  const [days, setDays] = useState(7)

  const r1a = useOpsData(() => api.getOpsR1aTelemetry(days), [days])
  const r1b = useOpsData(() => api.getOpsR1bTelemetry(days, 5), [days])
  const chainDepth = useOpsData(() => api.getOpsR1bChainDepth(), [])
  const r8 = useOpsData(() => api.getOpsR8KbShape(), [])
  const r8Query = useOpsData(() => api.getOpsR8QueryStats(days), [days])
  const deployRec = useOpsData(
    () => api.getOpsCoSTEERDeployRecommendation(days),
    [days],
  )

  const r1aPayload = r1a.data || {}
  const r1bPayload = r1b.data || {}
  const chainPayload = chainDepth.data || {}
  const r8Payload = r8.data || {}
  const r8QueryPayload = r8Query.data || {}
  const recPayload = deployRec.data || {}

  // R1a 归因饼图
  const ATTR_COLORS = {
    hypothesis: '#1677ff',
    implementation: '#52c41a',
    both: '#722ed1',
    unknown: '#faad14',
    null: '#bfbfbf',
  }
  const ATTR_LABELS = {
    hypothesis: '假设问题',
    implementation: '实现问题',
    both: '两者都有',
    unknown: '未能识别',
    null: '无数据',
  }
  const pieData = (r1aPayload.distribution || []).map((b) => ({
    name: ATTR_LABELS[b.attribution] || b.attribution,
    value: b.count,
    raw: b.attribution,
  }))

  // R1b 变异链深度柱图
  const chainBars = (chainPayload.distribution || []).map((b) => ({
    depth: `第 ${b.mutation_depth} 层`,
    count: b.hypothesis_count,
  }))

  // R1b 尝试统计表
  const OUTCOME_LABELS = {
    pass: '通过',
    fail: '失败',
    pending: '进行中',
  }
  const ATTEMPT_LABELS = {
    retry_implementation: '重试实现',
    mutate_hypothesis: '变异假设',
  }
  const attemptColumns = [
    {
      title: '尝试类型',
      dataIndex: 'attempt_type',
      key: 'attempt_type',
      width: 130,
      render: (t) => ATTEMPT_LABELS[t] || t,
    },
    {
      title: '结果',
      dataIndex: 'outcome',
      key: 'outcome',
      width: 120,
      render: (oc) => {
        const c =
          oc === 'pass'
            ? 'success'
            : oc === 'fail'
            ? 'error'
            : oc === 'pending'
            ? 'processing'
            : 'default'
        return <Tag color={c}>{OUTCOME_LABELS[oc] || oc}</Tag>
      },
    },
    { title: '数量', dataIndex: 'count', key: 'count', width: 80 },
    {
      title: '成本 (USD)',
      dataIndex: 'total_cost_usd',
      key: 'total_cost_usd',
      width: 120,
      render: (v) => v?.toFixed(4) ?? '—',
    },
    {
      title: 'Token 数',
      dataIndex: 'total_tokens_used',
      key: 'total_tokens_used',
      width: 110,
    },
  ]

  // R1b 高消耗任务 Top N 表
  const taskColumns = [
    { title: '任务 ID', dataIndex: 'task_id', key: 'task_id', width: 100 },
    { title: '重试次数', dataIndex: 'retries_total', key: 'retries_total', width: 100 },
    { title: '变异次数', dataIndex: 'mutations_total', key: 'mutations_total', width: 100 },
    {
      title: '总成本 (USD)',
      dataIndex: 'cost_usd_total',
      key: 'cost_usd_total',
      width: 130,
      render: (v) => v?.toFixed(4) ?? '—',
    },
  ]

  // Flag 状态标签
  const flagTag = (label, on) => (
    <Tag color={on ? 'success' : 'default'} key={label}>
      {label}: {on ? '开' : '关'}
    </Tag>
  )

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <OpsSectionCard
        title="归因与重试监控（聚合 R1a / R1b / R8 数据）"
        source="live"
        loading={
          r1a.loading || r1b.loading || chainDepth.loading || r8.loading ||
          r8Query.loading || deployRec.loading
        }
        onRefresh={() => {
          r1a.refetch()
          r1b.refetch()
          chainDepth.refetch()
          r8.refetch()
          r8Query.refetch()
          deployRec.refetch()
        }}
      >
        <Space size="middle" style={{ marginBottom: 16 }}>
          <span>时间窗口：</span>
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

        {/* 部署推荐 — 顶部提示下一步操作 */}
        {recPayload.next_action && (
          <Alert
            type={
              (recPayload.ready_flags_to_flip || []).length > 0
                ? 'success'
                : (recPayload.blockers || []).length > 0
                ? 'warning'
                : 'info'
            }
            showIcon
            style={{ marginBottom: 12 }}
            message={
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <strong>下一步操作：{recPayload.next_action}</strong>
                {(recPayload.ready_flags_to_flip || []).length > 0 && (
                  <Space wrap>
                    <span>可翻转的 flag：</span>
                    {recPayload.ready_flags_to_flip.map((f) => (
                      <Tag color="success" key={f}>
                        {f}
                      </Tag>
                    ))}
                  </Space>
                )}
                {(recPayload.blockers || []).slice(0, 3).map((b, i) => (
                  <div key={i} style={{ color: '#8c8c8c', fontSize: 12 }}>
                    · {b}
                  </div>
                ))}
              </Space>
            }
          />
        )}

        {/* 当前 Flag 状态行 */}
        <Alert
          message={
            <Space wrap>
              <strong>Flag 状态：</strong>
              {Object.entries(r1aPayload.flags || {}).map(([k, v]) => flagTag(k, v))}
              {Object.entries(r1bPayload.flags || {}).map(([k, v]) => flagTag(k, v))}
              {Object.entries(r8Payload.flags || {}).map(([k, v]) => flagTag(k, v))}
              {Object.entries(r8QueryPayload.flags || {})
                .filter(([k]) => k === 'ENABLE_R8_QUERY_LOG' || k === 'ENABLE_HIERARCHICAL_RAG_CACHE')
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
            <AntdTooltip title="窗口内被 R1a 钩子捕获的 alpha 数（含成功与失败）">
              <Statistic
                title="R1a 窗口内总条数"
                value={r1aPayload.total_in_window ?? 0}
              />
            </AntdTooltip>
          </Col>
          <Col xs={24} md={6}>
            <AntdTooltip title="R1a 能区分『假设问题 vs 实现问题』的比例。健康部署 ≥ 70%">
              <Statistic
                title="R1a 已识别归因占比"
                value={((r1aPayload.non_unknown_pct ?? 0) * 100).toFixed(2)}
                suffix="%"
              />
            </AntdTooltip>
          </Col>
          <Col xs={24} md={6}>
            <AntdTooltip title="R1b『重试实现』后通过的比例。≥ 15% 才可推进 mutate_hypothesis flag">
              <Statistic
                title="R1b 重试实现成功率"
                value={((r1bPayload.success_rate_retry_impl ?? 0) * 100).toFixed(2)}
                suffix="%"
              />
            </AntdTooltip>
          </Col>
          <Col xs={24} md={6}>
            <AntdTooltip title="R1b『变异假设』后通过的比例">
              <Statistic
                title="R1b 变异假设成功率"
                value={((r1bPayload.success_rate_mutate_hyp ?? 0) * 100).toFixed(2)}
                suffix="%"
              />
            </AntdTooltip>
          </Col>
        </Row>
      </OpsSectionCard>

      <Row gutter={[16, 16]}>
        {/* R1a 归因分布饼图 */}
        <Col xs={24} lg={12}>
          <OpsSectionCard title="R1a 归因分布（窗口内失败/优化的 alpha 归因）">
            {pieData.length === 0 ? (
              <Empty description="窗口内暂无 R1a 数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    label={({ name, value }) => `${name}: ${value}`}
                  >
                    {pieData.map((entry) => (
                      <Cell key={entry.raw} fill={ATTR_COLORS[entry.raw] || '#8c8c8c'} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              {r1aPayload.r5_sample_size > 0 && (
                <>
                  <Tag color="purple">
                    R5 样本数：{r1aPayload.r5_sample_size}
                  </Tag>
                  <Tag color="purple">
                    R5 与 R1a 一致率：
                    {((r1aPayload.r5_agrees_r1a_pct ?? 0) * 100).toFixed(1)}%
                  </Tag>
                  <Tag color="purple">
                    R5 平均评分：
                    {(r1aPayload.r5_avg_composite_score ?? 0).toFixed(3)}
                  </Tag>
                  <Tag color="purple">
                    R5 累计成本：${(r1aPayload.r5_total_cost_usd ?? 0).toFixed(4)}
                  </Tag>
                </>
              )}
              {r1aPayload.errs_count_total > 0 && (
                <Tag color="error">
                  钩子错误数：{r1aPayload.errs_count_total}
                </Tag>
              )}
            </Space>
          </OpsSectionCard>
        </Col>

        {/* R1b 变异链深度柱图 */}
        <Col xs={24} lg={12}>
          <OpsSectionCard title="R1b 变异链深度分布">
            {chainBars.length === 0 ? (
              <Empty description="数据库内暂无假设" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={chainBars}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="depth" />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Bar dataKey="count" fill="#722ed1" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag>根假设：{chainPayload.total_root_hypotheses ?? 0}</Tag>
              <Tag color="purple">
                已变异：{chainPayload.total_mutated_hypotheses ?? 0}
              </Tag>
              <Tag>最大深度：{chainPayload.max_depth_observed ?? 0}</Tag>
              <Tag>
                平均深度：{(chainPayload.chain_depth_avg ?? 0).toFixed(3)}
              </Tag>
            </Space>
          </OpsSectionCard>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        {/* R1b 尝试统计表 */}
        <Col xs={24} lg={14}>
          <OpsSectionCard title="R1b 尝试统计（按重试 / 变异分类）">
            <Table
              size="small"
              dataSource={r1bPayload.attempt_stats || []}
              columns={attemptColumns}
              rowKey={(r) => `${r.attempt_type}::${r.outcome}`}
              pagination={false}
              locale={{ emptyText: '窗口内暂无 R1b 尝试' }}
            />
          </OpsSectionCard>
        </Col>

        {/* R1b 高消耗任务 */}
        <Col xs={24} lg={10}>
          <OpsSectionCard title="R1b 高消耗任务 Top 5">
            <Table
              size="small"
              dataSource={r1bPayload.top_tasks_by_budget || []}
              columns={taskColumns}
              rowKey="task_id"
              pagination={false}
              locale={{ emptyText: '尚无消耗 R1b 预算的任务' }}
            />
          </OpsSectionCard>
        </Col>
      </Row>

      {/* R8 知识库 shape — 是 ENABLE_HIERARCHICAL_RAG flag 翻转的证据 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <OpsSectionCard title="R8 知识库条目类型（活跃 vs 已衰减）">
            {(r8Payload.entry_types || []).length === 0 ? (
              <Empty description="知识库无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart
                  data={(r8Payload.entry_types || []).map((b) => ({
                    type: b.entry_type,
                    active: b.active_count,
                    decayed: b.decayed_count,
                  }))}
                >
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="type" />
                  <YAxis allowDecimals={false} />
                  <Tooltip />
                  <Legend formatter={(v) => (v === 'active' ? '活跃' : '已衰减')} />
                  <Bar dataKey="active" stackId="kb" fill="#1677ff" />
                  <Bar dataKey="decayed" stackId="kb" fill="#bfbfbf" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag color="blue">
                活跃总数：{r8Payload.total_active ?? 0}
              </Tag>
              <Tag>已衰减总数：{r8Payload.total_decayed ?? 0}</Tag>
              <Tag color="success">
                活跃成功模式：{r8Payload.success_pattern_active ?? 0}
              </Tag>
              <Tag color="error">
                活跃失败教训：{r8Payload.failure_pitfall_active ?? 0}
              </Tag>
              <Tag color="purple">
                R5 可排序条数：{r8Payload.r5_rankable_success_count ?? 0}
              </Tag>
            </Space>
          </OpsSectionCard>
        </Col>

        <Col xs={24} lg={10}>
          <OpsSectionCard title="R8 支柱覆盖（活跃）">
            {(r8Payload.pillars || []).length === 0 ? (
              <Empty description="暂无支柱数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie
                    data={(r8Payload.pillars || []).map((p) => ({
                      name: p.pillar,
                      value: p.entry_count,
                    }))}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    label={({ name, value }) => `${name}: ${value}`}
                  >
                    {(r8Payload.pillars || []).map((p) => (
                      <Cell
                        key={p.pillar}
                        fill={p.pillar === 'none' ? '#bfbfbf' : '#13c2c2'}
                      />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            )}
          </OpsSectionCard>
        </Col>
      </Row>

      {/* R8 实时层命中率 — 仅在 ENABLE_R8_QUERY_LOG 打开时有数据 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <OpsSectionCard title="R8 实时层级命中率（窗口内每层被命中的比例）">
            {(r8QueryPayload.total_queries ?? 0) === 0 ? (
              <Empty
                description={
                  r8QueryPayload.flags?.ENABLE_R8_QUERY_LOG
                    ? '窗口内暂无查询'
                    : 'ENABLE_R8_QUERY_LOG 未开启 — 打开后才会记录实时层级跳转'
                }
              />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart
                  data={Object.entries(r8QueryPayload.layer_hit_rates || {}).map(
                    ([layer, rate]) => ({
                      layer,
                      rate: Math.round(rate * 10000) / 100,
                    }),
                  )}
                >
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="layer" />
                  <YAxis
                    allowDecimals
                    label={{ value: '%', angle: -90, position: 'insideLeft' }}
                  />
                  <Tooltip formatter={(v) => `${v.toFixed(2)}%`} />
                  <Bar dataKey="rate" fill="#13c2c2" />
                </BarChart>
              </ResponsiveContainer>
            )}
            <Space wrap style={{ marginTop: 12 }}>
              <Tag>总查询数：{r8QueryPayload.total_queries ?? 0}</Tag>
              <Tag color={r8QueryPayload.cache_hit_rate > 0 ? 'success' : 'default'}>
                缓存命中率：
                {((r8QueryPayload.cache_hit_rate ?? 0) * 100).toFixed(2)}%
              </Tag>
              <Tag color="purple">
                失败树升级率：
                {((r8QueryPayload.failure_tree_elevation_rate ?? 0) * 100).toFixed(2)}%
              </Tag>
            </Space>
          </OpsSectionCard>
        </Col>

        <Col xs={24} lg={10}>
          <OpsSectionCard title="R8 各地区查询数">
            {Object.keys(r8QueryPayload.by_region || {}).length === 0 ? (
              <Empty description="暂无地区数据" />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart
                  data={Object.entries(r8QueryPayload.by_region || {}).map(
                    ([region, count]) => ({ region, count }),
                  )}
                >
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
