import { Spin, Empty, Timeline, Tag, Typography, Space, Tooltip as AntTooltip } from 'antd'
import { formatRelative, formatDateTime } from '../../utils/time'
import { STATUS_COLORS, STATUS_LABELS } from '../../utils/alphaStatus'

const { Text } = Typography

export default function TransitionsPanel({ transitions, loading }) {
  if (loading) return <Spin />
  if (transitions.length === 0) return <Empty description="尚无状态变迁记录" />
  return (
    <Timeline
      items={transitions.map((t) => ({
        color: STATUS_COLORS[t.new_status] || 'gray',
        children: (
          <Space direction="vertical" size={2}>
            <Space>
              {t.old_status && (
                <Tag color={STATUS_COLORS[t.old_status]}>{STATUS_LABELS[t.old_status] || t.old_status}</Tag>
              )}
              <span>→</span>
              <Tag color={STATUS_COLORS[t.new_status]}>{STATUS_LABELS[t.new_status] || t.new_status}</Tag>
              {t.sharpe_at_transition != null && (
                <Text type="secondary">
                  当时 Sharpe={t.sharpe_at_transition.toFixed(2)}
                </Text>
              )}
            </Space>
            {t.reason && <Text type="secondary">{t.reason}</Text>}
            <Space>
              {t.source && <Tag>{t.source}</Tag>}
              <AntTooltip title={formatDateTime(t.transitioned_at)}>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {formatRelative(t.transitioned_at)}
                </Text>
              </AntTooltip>
            </Space>
          </Space>
        ),
      }))}
    />
  )
}
