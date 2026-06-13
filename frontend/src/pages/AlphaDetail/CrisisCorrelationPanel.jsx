import { Space, Tag, Typography, Tooltip as AntTooltip } from 'antd'

const { Text } = Typography

const CRISIS_WINDOW_LABELS = {
  covid_2020: 'COVID 2020',
  rate_shock_2022: '利率冲击 2022',
  svb_2023: 'SVB 2023',
  tariff_2025: '关税 2025',
}

export default function CrisisCorrelationPanel({ crisis }) {
  // crisis: { [window]: { status, max_corr?, overlap_days?, counterpart_id? } }
  if (!crisis || Object.keys(crisis).length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        尚无危机相关性数据（仅「通过」级别的 alpha 在本地收益缓存命中时才会计算）。
      </Text>
    )
  }
  const order = ['covid_2020', 'rate_shock_2022', 'svb_2023', 'tariff_2025']
  const items = order
    .filter((k) => k in crisis)
    .concat(Object.keys(crisis).filter((k) => !order.includes(k)))

  return (
    <Space wrap size={[8, 8]}>
      {items.map((w) => {
        const info = crisis[w] || {}
        const label = CRISIS_WINDOW_LABELS[w] || w
        if (info.status !== 'ok') {
          return (
            <AntTooltip
              key={w}
              title={
                <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
                  状态: {info.status || '未知'}
                  {info.overlap_days !== undefined && (
                    <> · 重叠天数: {info.overlap_days}</>
                  )}
                </div>
              }
            >
              <Tag>{label} · 无数据</Tag>
            </AntTooltip>
          )
        }
        const v = info.max_corr
        const color = v >= 0.7 ? 'red' : v >= 0.5 ? 'orange' : v >= 0.3 ? 'gold' : 'green'
        return (
          <AntTooltip
            key={w}
            title={
              <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
                <div>最高相关度: {v.toFixed(4)}</div>
                <div>对手策略: {info.counterpart_id}</div>
                <div>重叠: {info.overlap_days} 天</div>
              </div>
            }
          >
            <Tag color={color} style={{ cursor: 'help' }}>
              {label} · {v.toFixed(2)}
            </Tag>
          </AntTooltip>
        )
      })}
    </Space>
  )
}
