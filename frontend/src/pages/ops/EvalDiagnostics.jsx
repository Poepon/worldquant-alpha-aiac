import { Tabs, Typography } from 'antd'
import {
  HeartOutlined, FundOutlined, ExperimentOutlined,
} from '@ant-design/icons'

import AlphaHealthMonitor from './AlphaHealthMonitor'
import R11CapacityMonitor from './R11CapacityMonitor'
import R13FactorLensMonitor from './R13FactorLensMonitor'

const { Title } = Typography

/**
 * EvalDiagnostics — /ops/alpha-health「评估阶段 · E 诊断」统一页 (2026-06-08 合并).
 *
 * 把评估阶段三个独立薄页合为单页 3 Tab(各自端点不同,纯 IA 整理,菜单 3→1):
 *   - Alpha 健康度  (getOpsAlphaHealth* — 库健康 band 分布 / 30d 趋势 / 问题明细)
 *   - 容量估算 R11  (getOpsR11CapacityStats — 容量 log-scale 分布)
 *   - 因子透镜 R13  (getOpsR13FactorResiduals / snapshot-stale — 风格因子残差)
 * 复用三个原组件,零内容改动;r11-capacity / r13-factor-lens 路由重定向到此。
 */
export default function EvalDiagnostics() {
  return (
    <div>
      <Title level={3} style={{ marginTop: 0, marginBottom: 12 }}>
        评估诊断 (E 阶段)
      </Title>
      <Tabs
        defaultActiveKey="alpha-health"
        destroyOnHidden
        items={[
          {
            key: 'alpha-health',
            label: <span><HeartOutlined /> Alpha 健康度</span>,
            children: <AlphaHealthMonitor />,
          },
          {
            key: 'r11-capacity',
            label: <span><FundOutlined /> 容量估算 (R11)</span>,
            children: <R11CapacityMonitor />,
          },
          {
            key: 'r13-factor-lens',
            label: <span><ExperimentOutlined /> 因子透镜 (R13)</span>,
            children: <R13FactorLensMonitor />,
          },
        ]}
      />
    </div>
  )
}
