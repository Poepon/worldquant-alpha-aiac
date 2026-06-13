import { Spin, Empty } from 'antd'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'

export default function PnlPanel({ pnlData, loading }) {
  if (loading) return <Spin />
  if (pnlData.length === 0) return <Empty description="尚无 PnL 数据（挖掘 / 同步命中本地缓存后落库）" />
  return (
    <ResponsiveContainer width="100%" height={320}>
      <LineChart data={pnlData}>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
        <XAxis dataKey="date" stroke="rgba(255,255,255,0.5)" minTickGap={40} />
        <YAxis stroke="rgba(255,255,255,0.5)" width={70} />
        <Tooltip
          contentStyle={{
            background: '#131a2b',
            border: '1px solid rgba(255,255,255,0.1)',
            borderRadius: 8,
          }}
          formatter={(v) => [Number(v).toLocaleString(), '累计 PnL']}
        />
        <Line type="monotone" dataKey="cum" stroke="#00ff88" strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}
