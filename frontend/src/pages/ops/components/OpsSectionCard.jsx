import { Button, Card, Space, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

import SourceTagBadge from './SourceTagBadge'

const { Title } = Typography

/**
 * OpsSectionCard — common page-header card with title, source badge,
 * refresh button, and an optional Rerun slot on the right.
 *
 * Every /ops/* page composes from these so visual rhythm + interaction
 * grammar stay consistent across Phase 2 and Phase 3 dashboards.
 */
export default function OpsSectionCard({
  title,
  source,
  staleDays,
  onRefresh,
  rerunSlot = null,
  children,
  loading,
}) {
  return (
    <Card
      className="glass-card"
      title={
        <Space>
          <Title level={4} style={{ margin: 0 }}>
            {title}
          </Title>
          {source && <SourceTagBadge source={source} staleDays={staleDays} />}
        </Space>
      }
      extra={
        <Space>
          {onRefresh && (
            <Button
              icon={<ReloadOutlined />}
              size="small"
              loading={loading}
              onClick={onRefresh}
            >
              刷新
            </Button>
          )}
          {rerunSlot}
        </Space>
      }
    >
      {children}
    </Card>
  )
}
