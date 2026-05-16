import { useMemo } from 'react'
import {
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import api from '../../services/api'
import OpsSectionCard from './components/OpsSectionCard'
import RerunButton from './components/RerunButton'
import useOpsData from './hooks/useOpsData'

const { Text } = Typography

/**
 * LLMOpMonitor — /ops/llm-op-monitor page (P3 Phase 4).
 *
 * Wraps the daily ``monitor_llm_op_hallucinations`` task's Markdown
 * report. Backend's OpsService._parse_llm_op_md does the markdown
 * walking; the page just renders the parsed dict.
 *
 * Layout:
 *  - 4 KPI Statistic (scanned / clean / template_halluc / deactivated)
 *  - Top "bad ops" horizontal BarChart (hallucinated op name → frequency)
 *  - Affected entries Table — KB#, source, bad_ops chips, pattern snippet
 */
export default function LLMOpMonitor() {
  const latest = useOpsData(() => api.getOpsLLMOpLatest(), [])

  const summary = latest.data?.summary || {}
  const source = latest.data?.source || 'missing'

  // Top "bad op" frequencies sorted desc, capped at 20 — same chart
  // grammar as NegativeKnowledge.Top20.
  const opRows = useMemo(
    () => (summary.hallucinated_ops || [])
      .slice(0, 20)
      .map((r) => ({ op: r.op, count: r.count })),
    [summary.hallucinated_ops],
  )

  const columns = [
    { title: 'KB#', dataIndex: 'kb_id', width: 80 },
    {
      title: 'source',
      dataIndex: 'source',
      width: 110,
      render: (v) => <Tag>{v || 'unknown'}</Tag>,
    },
    {
      title: 'bad_ops',
      dataIndex: 'bad_ops',
      render: (ops) =>
        (ops || []).map((op) => (
          <Tag key={op} color="red">
            {op}
          </Tag>
        )),
    },
    {
      title: 'pattern (first 80)',
      dataIndex: 'pattern',
      ellipsis: true,
      render: (v) => (
        <Text code style={{ fontSize: 12 }}>
          {v}
        </Text>
      ),
    },
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <OpsSectionCard
        title={`LLM 算子幻觉监控${latest.data?.report_date ? ` · ${latest.data.report_date}` : ''}`}
        source={source}
        staleDays={latest.data?.stale_days}
        onRefresh={latest.refetch}
        loading={latest.loading}
        rerunSlot={
          <RerunButton
            triggerFn={api.rerunOpsLLMOp}
            label="重跑 llm-op-monitor"
            onSuccess={() => setTimeout(latest.refetch, 3000)}
          />
        }
      >
        {latest.loading && !latest.data ? (
          <Spin />
        ) : summary.scanned === 0 && source === 'missing' ? (
          <Empty description="今日尚无 llm-op-monitor 数据;点右上 Rerun 触发" />
        ) : (
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={6}>
              <Statistic title="扫描的 KB 条目" value={summary.scanned || 0} />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="干净条目"
                value={summary.clean || 0}
                valueStyle={{ color: '#00ff88' }}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="幻觉 (pattern + template)"
                value={(summary.pattern_halluc || 0) + (summary.template_halluc || 0)}
                valueStyle={{ color: '#ff8c00' }}
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="已 deactivated"
                value={summary.deactivated || 0}
                valueStyle={{ color: '#ff4d4f' }}
              />
            </Col>
            <Col xs={24}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                Registry 中合法 BRAIN op 数:{summary.valid_ops_in_registry || 0}
              </Text>
            </Col>
          </Row>
        )}
      </OpsSectionCard>

      <OpsSectionCard title="Top 20 幻觉 op 频次" source={source}>
        {opRows.length === 0 ? (
          <Empty description="无幻觉 op(所有 KB 模式均使用合法 BRAIN op)" />
        ) : (
          <ResponsiveContainer width="100%" height={Math.max(220, opRows.length * 26)}>
            <BarChart data={opRows} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
              <XAxis type="number" stroke="#888" allowDecimals={false} />
              <YAxis dataKey="op" type="category" stroke="#888" width={180} />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: '1px solid #444' }}
              />
              <Bar dataKey="count" fill="#ff4d4f" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </OpsSectionCard>

      <OpsSectionCard
        title={`受影响 KB 条目(${(summary.affected_entries || []).length})`}
        source={source}
      >
        <Table
          rowKey="kb_id"
          size="small"
          columns={columns}
          dataSource={summary.affected_entries || []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </OpsSectionCard>
    </Space>
  )
}
