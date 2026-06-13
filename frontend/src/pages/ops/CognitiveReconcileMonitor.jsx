import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Alert, Badge, Button, Card, Col, Empty, Row, Space, Spin, Statistic,
  Tag, Tooltip, Typography,
} from 'antd'
import {
  ReloadOutlined, ExperimentOutlined, ClockCircleOutlined, WarningOutlined,
  BranchesOutlined, AimOutlined,
} from '@ant-design/icons'
import {
  Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer,
  Tooltip as ReTooltip, XAxis, YAxis,
} from 'recharts'

import api from '../../services/api'

const { Title, Text, Paragraph } = Typography

/**
 * CognitiveReconcileMonitor — /ops/cognitive-reconcile page (四池前端 P2).
 *
 * Surfaces the Pool-Phase-2 池原生 cognitive-reconcile beat. The beat is
 * gated behind ENABLE_POOL_COGNITIVE_RECONCILE and is currently OFF / has
 * never run (watermark=null), so every denorm lifecycle column
 * (can_submit_count / submitted_count / attribution) is structurally 0.
 *
 * The page is deliberately "dormant but ready", NOT a fault: it shows how
 * many pool-era hypotheses are already queued for reconciliation
 * (pool_era_total) and what will be populated once the beat ships.
 *
 * Data: api.getOpsCognitiveReconcileStatus() every 30s.
 */

// canonical lifecycle order so an empty status still shows a 0 chip / bar
const STATUS_ORDER = ['PROPOSED', 'ACTIVE', 'SUPERSEDED', 'ABANDONED', 'PROMOTED']
const STATUS_COLOR = {
  PROPOSED: '#1677ff',
  ACTIVE: '#52c41a',
  SUPERSEDED: '#faad14',
  ABANDONED: '#8c8c8c',
  PROMOTED: '#722ed1',
}
// 假设生命周期状态 → 中文 label(渲染处用 STATUS_LABEL[s] || s,后端 key 不改)
const STATUS_LABEL = {
  PROPOSED: '待定',
  ACTIVE: '生效中',
  SUPERSEDED: '被替代',
  ABANDONED: '已弃用',
  PROMOTED: '已提升复用',
}

function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

export default function CognitiveReconcileMonitor() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['cognitiveReconcileStatus'],
    queryFn: api.getOpsCognitiveReconcileStatus,
    refetchInterval: 30000,
  })

  const lc = data?.lifecycle || {}
  const byStatus = lc.by_status || {}
  const byAttribution = lc.by_attribution || {}

  const statusRows = useMemo(
    () => STATUS_ORDER.map((s) => ({ status: s, count: byStatus[s] || 0 })),
    [byStatus],
  )
  const hasStatusData = useMemo(
    () => statusRows.some((r) => r.count > 0),
    [statusRows],
  )
  const attributionEntries = useMemo(
    () => Object.entries(byAttribution || {}),
    [byAttribution],
  )

  if (isLoading) return <Spin tip="加载知识库对账状态..." style={{ marginTop: 80 }} />

  const enabled = !!data?.enabled
  const watermark = data?.watermark || null
  const watermarkLabel = watermark ? fmtTime(watermark) : '未运行'
  const poolEraTotal = lc.pool_era_total || 0
  const total = lc.total || 0
  const graceSec = data?.grace_sec ?? 0
  const windowDays = data?.window_days ?? 0

  const canSubmitGt0 = lc.can_submit_count_gt0 || 0
  const submittedGt0 = lc.submitted_count_gt0 || 0
  const attributionStamped = lc.attribution_stamped || 0

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>
          <ExperimentOutlined /> 知识库对账（第二阶段反馈环）
        </Title>
        <Space>
          {enabled
            ? <Badge status="processing" text="知识库对账开关 已开启" />
            : <Badge status="default" text="已关闭 — 休眠但就绪" />}
          <Button icon={<ReloadOutlined spin={isFetching} />} onClick={() => refetch()}>刷新</Button>
        </Space>
      </Row>

      {isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="拉取知识库对账状态失败（接口 /ops/cognitive-reconcile/status）。"
          description={String(error?.message || error || '')}
        />
      )}

      {data && !enabled && (
        <Alert
          type="warning"
          showIcon
          icon={<WarningOutlined />}
          style={{ marginBottom: 16 }}
          message="第二阶段流水线原生反馈环未启用（知识库对账开关关闭）"
          description={
            <Paragraph style={{ marginBottom: 0 }}>
              对账定时任务从未运行(处理进度位置={watermark ? fmtTime(watermark) : '未运行'});
              以下汇总预计算的生命周期列(可提交数 / 已提交数 / 归因)恒为 <b>0</b>;
              流水线已产出 <b>{poolEraTotal}</b> 个流水线原生假设待对账。
              开启有前置(先跑因子类别 A/B + 观测广度),属后端生产决策。
            </Paragraph>
          }
        />
      )}

      {data && enabled && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 16 }}
          message="第二阶段流水线原生反馈环已启用"
          description={
            <span>
              对账定时任务最近处理进度位置:<b>{watermarkLabel}</b>
            </span>
          }
        />
      )}

      {/* ---- 就绪度卡 -------------------------------------------------- */}
      <Card size="small" title="就绪度" style={{ marginBottom: 16 }}>
        <Row gutter={16}>
          <Col xs={12} sm={8} md={5}>
            <Statistic
              title="流水线原生假设（来自想法队列）"
              value={poolEraTotal}
              prefix={<AimOutlined />}
              valueStyle={{ color: poolEraTotal > 0 ? '#3f8600' : undefined }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>待本任务对账</Text>
          </Col>
          <Col xs={12} sm={8} md={5}>
            <Statistic
              title="假设总数（含历史遗留）"
              value={total}
              prefix={<BranchesOutlined />}
            />
          </Col>
          <Col xs={12} sm={8} md={5}>
            <Statistic title="宽限秒数" value={graceSec} suffix="s" />
          </Col>
          <Col xs={12} sm={8} md={4}>
            <Statistic title="统计窗口天数" value={windowDays} suffix="d" />
          </Col>
          <Col xs={24} sm={24} md={5}>
            <Statistic
              title="处理进度位置"
              value={watermarkLabel}
              valueStyle={{ fontSize: 16 }}
              prefix={<ClockCircleOutlined />}
            />
          </Col>
        </Row>
      </Card>

      {/* ---- 生命周期分布 --------------------------------------------- */}
      <Card
        size="small"
        title="假设生命周期分布"
        style={{ marginBottom: 16 }}
      >
        {!hasStatusData ? (
          <Empty description="暂无生命周期数据" />
        ) : (
          <>
            <Space size={[8, 8]} wrap style={{ marginBottom: 12 }}>
              {statusRows.map((r) => (
                <Tag
                  key={r.status}
                  color={r.count > 0 ? STATUS_COLOR[r.status] : 'default'}
                >
                  {STATUS_LABEL[r.status] || r.status}: <b>{r.count}</b>
                </Tag>
              ))}
            </Space>
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={statusRows}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                <XAxis dataKey="status" stroke="#888" tickFormatter={(s) => STATUS_LABEL[s] || s} />
                <YAxis stroke="#888" allowDecimals={false} />
                <ReTooltip contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
                  labelFormatter={(s) => STATUS_LABEL[s] || s} />
                <Bar dataKey="count" name="假设数">
                  {statusRows.map((r) => (
                    <Cell key={r.status} fill={STATUS_COLOR[r.status] || '#888'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </>
        )}
        <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          注脚:这些状态计数主要是 <b>历史遗留数据</b>(「已提升复用」等来自旧的同步更新),
          并非本对账任务的产出。本任务上线后会按归因类型重新归因流水线原生假设。
        </Paragraph>
      </Card>

      {/* ---- Phase 2 denorm 进度卡 ----------------------------------- */}
      <Card
        size="small"
        title="第二阶段汇总预计算进度（待对账任务上线后填充）"
      >
        {!enabled && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="以下指标由对账任务写入的汇总预计算列计算;任务未运行时结构性恒为 0。"
          />
        )}
        <Row gutter={16}>
          <Col xs={12} sm={8}>
            <Statistic
              title="有可提交 alpha 的假设"
              value={canSubmitGt0}
              valueStyle={{ color: canSubmitGt0 > 0 ? '#3f8600' : '#8c8c8c' }}
            />
          </Col>
          <Col xs={12} sm={8}>
            <Statistic
              title="有已提交 alpha 的假设"
              value={submittedGt0}
              valueStyle={{ color: submittedGt0 > 0 ? '#3f8600' : '#8c8c8c' }}
            />
          </Col>
          <Col xs={12} sm={8}>
            <Statistic
              title="已标注归因的假设"
              value={attributionStamped}
              valueStyle={{ color: attributionStamped > 0 ? '#3f8600' : '#8c8c8c' }}
            />
          </Col>
        </Row>

        <div style={{ marginTop: 16 }}>
          <Text strong>归因类型分布</Text>
          <div style={{ marginTop: 8 }}>
            {attributionEntries.length === 0 ? (
              <Tooltip title="对账任务上线并归因后,此处会按归因类型分桶填充。">
                <Tag color="default">待对账任务上线后填充(当前为空)</Tag>
              </Tooltip>
            ) : (
              <Space size={[8, 8]} wrap>
                {attributionEntries.map(([k, v]) => (
                  <Tag key={k} color="purple">
                    {k}: <b>{v || 0}</b>
                  </Tag>
                ))}
              </Space>
            )}
          </div>
        </div>

        <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
          这些汇总预计算列(可提交数 / 已提交数 / 归因)是第二阶段反馈环的
          核心产出:对账任务会把流水线原生假设的下游 alpha 结果(是否可提交 / 是否已提交)
          回写到假设层,并按归因(假设方向失败 vs 代码实现失败)归类,
          供后续知识库 / 数据集选择奖励学习。当前对账任务休眠,故全部为 0。
        </Paragraph>
      </Card>
    </div>
  )
}
