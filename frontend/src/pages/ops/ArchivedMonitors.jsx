import { Alert, Tabs, Typography } from 'antd'
import { ReadOutlined, CodeOutlined } from '@ant-design/icons'

import G10LogicMonitor from './G10LogicMonitor'
import G3v2Monitor from './G3v2Monitor'

const { Title } = Typography

/**
 * ArchivedMonitors — /ops/g10-logic「[归档] 监控页」(2026-06-08 合并).
 *
 * 把两个已归档的薄监控页合为单页 2 Tab(菜单 2→1):
 *   - 逻辑资产库 (G10)  — getOpsG10LogicLibrary
 *   - 语法校验 (G3-v2)  — getOpsG3v2ParseStats
 * 复用原组件零改动;g3v2-monitor 路由重定向到此。两者均为归档机制(flag OFF /
 * 机制已退役),保留只读可视化作历史追溯。
 */
export default function ArchivedMonitors() {
  return (
    <div>
      <Title level={3} style={{ marginTop: 0, marginBottom: 12 }}>
        [归档] 监控页
      </Title>
      <Alert
        type="default"
        showIcon
        style={{ marginBottom: 12 }}
        message="这些是已归档机制(flag OFF / 已退役)的只读可视化,保留作历史追溯,非活跃监控。"
      />
      <Tabs
        defaultActiveKey="g10"
        destroyOnHidden
        items={[
          {
            key: 'g10',
            label: <span><ReadOutlined /> 逻辑资产库 (G10)</span>,
            children: <G10LogicMonitor />,
          },
          {
            key: 'g3v2',
            label: <span><CodeOutlined /> 语法校验 (G3-v2)</span>,
            children: <G3v2Monitor />,
          },
        ]}
      />
    </div>
  )
}
