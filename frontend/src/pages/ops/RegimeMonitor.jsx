import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Alert, Card, Col, Row, Space, Spin, Statistic, Table, Tag, Tooltip, Typography,
} from 'antd'
import { RadarChartOutlined, InfoCircleOutlined, ReloadOutlined } from '@ant-design/icons'

import api from '../../services/api'

const { Title, Text, Paragraph } = Typography

/**
 * RegimeMonitor — /ops/regime-monitor (#41, greenfield branch B, 2026-06-07).
 *
 * Production is paused in a regime trough. The daily beat (run_regime_monitor,
 * gated on ENABLE_REGIME_MONITOR) re-sims the submitted winners + a backlog
 * sample on CURRENT data and asks: have the old edges recovered? This page
 * surfaces the latest probe + history and ALARMS (green banner) when
 * verdict=REGIME_TURNING → re-engage candidate.
 *
 * 口径 = current IS (rolling test_period), NOT OS — a regime-decay sensor that
 * says WHEN to resume mining, not WHAT to submit.
 */
const VERDICT_META = {
  REGIME_TURNING: { color: 'success', label: '🟢 行情切换', alert: 'success' },
  REGIME_DOWN: { color: 'default', label: '行情低谷', alert: 'info' },
  INSUFFICIENT: { color: 'warning', label: '样本不足', alert: 'warning' },
}

function sharpeTag(v, gate) {
  if (v === null || v === undefined) return <Tag color="default">回测失败</Tag>
  const color = v >= (gate ?? 1.25) ? 'success' : v >= 0 ? 'gold' : 'error'
  return <Tag color={color}>{v >= 0 ? '+' : ''}{v.toFixed(2)}</Tag>
}

export default function RegimeMonitor() {
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['ops/regime-monitor'],
    queryFn: () => api.getOpsRegimeMonitor(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  if (isLoading) {
    return <div style={{ textAlign: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (error) {
    return (
      <Alert type="error" showIcon message="加载行情切换监测器失败"
        description={error?.response?.data?.detail || error?.message || '未知错误'} />
    )
  }

  const enabled = !!data?.enabled
  const gate = data?.recovery_gate ?? 1.25
  const latest = data?.latest || null
  const signal = latest?.signal || null
  const rows = latest?.rows || []
  const history = data?.history || []
  const verdict = signal?.verdict
  const vmeta = VERDICT_META[verdict] || { color: 'default', label: verdict || '—', alert: 'info' }
  const sub = signal?.submitted || {}

  const columns = [
    {
      title: 'Alpha', dataIndex: 'alpha_id', key: 'alpha_id', width: 130,
      render: (aid) => <Link to={`/alphas/${aid}`}><Text code style={{ fontSize: 12 }}>{aid}</Text></Link>,
    },
    {
      title: '类别', dataIndex: 'kind', key: 'kind', width: 90,
      render: (k) => <Tag color={k === 'submitted' ? 'blue' : 'default'}>{k === 'submitted' ? '已提交' : '积压'}</Tag>,
    },
    {
      title: '提交时 Sharpe', dataIndex: 'baseline_sharpe', key: 'baseline_sharpe', width: 120, align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(2) : '—'),
    },
    {
      title: (
        <Tooltip title="在当前数据上重新回测得到的样本内 Sharpe(滚动测试区间)。回到提交时水平 = 老策略优势恢复 = 行情切换。">
          <Space size={4}>当前重新回测 <InfoCircleOutlined style={{ color: '#9c88ff' }} /></Space>
        </Tooltip>
      ),
      dataIndex: 'resim_sharpe', key: 'resim_sharpe', width: 120, align: 'right',
      render: (v) => sharpeTag(v, gate),
    },
    {
      title: 'Δ vs 提交时', key: 'delta', width: 110, align: 'right',
      render: (_, r) => {
        if (r.resim_sharpe === null || r.resim_sharpe === undefined || r.baseline_sharpe === null) return '—'
        const d = r.resim_sharpe - r.baseline_sharpe
        return <Text type={d >= 0 ? 'success' : 'danger'}>{d >= 0 ? '+' : ''}{d.toFixed(2)}</Text>
      },
    },
    {
      title: '错误', dataIndex: 'error', key: 'error', ellipsis: true,
      render: (e) => (e ? <Text type="secondary" style={{ fontSize: 11 }}>{e}</Text> : ''),
    },
  ]

  const histColumns = [
    { title: '时间 (UTC)', dataIndex: 'computed_at', key: 'computed_at',
      render: (v) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—') },
    { title: '裁决', dataIndex: 'verdict', key: 'verdict',
      render: (v) => <Tag color={(VERDICT_META[v] || {}).color || 'default'}>{(VERDICT_META[v] || {}).label || v}</Tag> },
    { title: '提交集重新回测均值', dataIndex: 'submitted_mean_resim', key: 'submitted_mean_resim', align: 'right',
      render: (v) => (v !== null && v !== undefined ? v.toFixed(3) : '—') },
    { title: '恢复数', key: 'rec', align: 'right',
      render: (_, r) => `${r.n_recovered_total ?? 0}/${r.n_resimmed ?? 0}` },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <Title level={3} style={{ margin: 0 }}>
          <RadarChartOutlined style={{ marginRight: 8 }} />
          行情切换监测器
        </Title>
        <Space>
          <Tag color={enabled ? 'success' : 'default'}>{enabled ? '已启用' : '未启用(开关已关)'}</Tag>
          {isFetching && <Spin size="small" />}
        </Space>
      </Space>

      {/* 主告警 banner */}
      {!enabled ? (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="探针未激活"
          description={<span>在功能开关控制台打开「行情切换监测」开关(可热生效)+ 重启载入每日定时任务(07:30)即启动。定时任务不受挖掘流水线暂停影响,照跑。</span>} />
      ) : !signal ? (
        <Alert type="info" showIcon style={{ marginBottom: 16 }}
          message="已启用,等首次探针结果"
          description="每日定时任务在 07:30 跑;或重启后等下一次触发。结果出来后此处显示。" />
      ) : verdict === 'REGIME_TURNING' ? (
        <Alert type="success" showIcon style={{ marginBottom: 16 }}
          message={<strong>🟢 行情切换信号 — 老策略优势在当前数据上恢复了</strong>}
          description={
            <span>
              提交集当前重新回测均值 <strong>{sub.mean_resim}</strong>(提交时 {sub.mean_baseline})、
              <strong>{signal.n_recovered_total}</strong> 个重新回测 ≥ {gate}(可提交)。
              恢复的:{(signal.recovered_ids || []).join(', ') || '—'}。
              <br /><strong>建议:复核这些 alpha,考虑恢复挖掘生产</strong>(打开挖掘流水线开关 + 清除暂停 + 重启)。
              ⚠️ 口径=当前样本内,不是样本外 —— 这是「该重启了」的信号,提交决策仍走提交选择器 + 当前数据确认。
            </span>
          } />
      ) : (
        <Alert type={vmeta.alert} showIcon style={{ marginBottom: 16 }}
          message={`${vmeta.label} — 暂不重启`}
          description={
            verdict === 'INSUFFICIENT'
              ? '所有重新回测都失败(BRAIN 认证/名额/数据)— 检查工作进程与 BRAIN 的连通。'
              : `老策略优势仍未恢复:提交集当前重新回测均值 ${sub.mean_resim}(提交时 ${sub.mean_baseline}),0 个回到可提交。继续持有,等下次探针。`
          } />
      )}

      {/* KPI */}
      <Row gutter={[16, 16]}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="结论" value={vmeta.label}
              valueStyle={{ color: verdict === 'REGIME_TURNING' ? '#00ff88' : '#888', fontSize: 18 }} />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Tooltip title="已提交的历史优胜策略在当前数据上重新回测的 Sharpe 均值。回升=老策略优势恢复。">
              <Statistic title="提交集重新回测均值" value={sub.mean_resim ?? '—'}
                valueStyle={{ color: (sub.mean_resim ?? -1) >= 0.5 ? '#00ff88' : '#ff4d4f' }} />
            </Tooltip>
            <Text type="secondary" style={{ fontSize: 12 }}>提交时 {sub.mean_baseline ?? '—'}</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="恢复到可提交" value={signal ? `${signal.n_recovered_total}/${signal.n_resimmed}` : '—'}
              valueStyle={{ color: (signal?.n_recovered_total ?? 0) > 0 ? '#00ff88' : '#888' }} />
            <Text type="secondary" style={{ fontSize: 12 }}>重新回测 ≥ {gate}</Text>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic title="最近探针 (UTC)"
              value={signal?.computed_at ? String(signal.computed_at).replace('T', ' ').slice(5, 16) : '—'}
              valueStyle={{ fontSize: 16 }} />
          </Card>
        </Col>
      </Row>

      <Alert type="info" showIcon style={{ marginTop: 16, marginBottom: 16 }}
        message="口径说明"
        description={
          <Paragraph style={{ marginBottom: 0, fontSize: 12 }}>
            周期性地把已提交的优胜策略 + 积压抽样在<strong>当前数据</strong>上重新回测(滚动测试区间,
            非冻结的 2019-2023)。<strong>口径 = 当前样本内,不是样本外</strong>(BRAIN 隐藏真实样本外结果)——
            它是行情衰减传感器,判「<strong>何时</strong>重启生产」,不判「提交什么」(那走提交选择器)。
            切换条件:提交集重新回测均值 ≥ {data?.turn_mean_threshold ?? 0.5} 或 ≥1 个重新回测过 {gate}。
          </Paragraph>
        } />

      {/* per-alpha 探针明细 */}
      <Card className="glass-card" size="small" title={<Space><ReloadOutlined />本次探针明细 {rows.length ? `(${rows.length})` : ''}</Space>}
        style={{ marginBottom: 16 }}>
        <Table size="small" rowKey="alpha_id" dataSource={rows} columns={columns}
          pagination={{ pageSize: 25 }}
          locale={{ emptyText: enabled ? '尚无探针结果(等 beat 或重启)' : '未启用' }} />
      </Card>

      {/* 历史趋势 */}
      {history.length > 0 && (
        <Card className="glass-card" size="small" title="探针历史(最近 30 次)">
          <Table size="small" rowKey={(r, i) => `${r.computed_at}-${i}`} dataSource={history}
            columns={histColumns} pagination={{ pageSize: 10 }} />
        </Card>
      )}
    </div>
  )
}
