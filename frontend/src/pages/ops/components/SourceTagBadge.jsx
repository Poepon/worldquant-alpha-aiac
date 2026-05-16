import { Tag, Tooltip } from 'antd'

/**
 * SourceTagBadge — colored chip explaining where a /ops/* payload came from.
 *
 * Mirrors backend OpsReportReader.SOURCE_* constants. The colors are
 * chosen so a quick glance tells the operator whether they're looking at
 * live data, last night's beat, or something older.
 */
const SOURCE_META = {
  service: {
    color: 'green',
    label: '实时',
    desc: '服务直接重新计算的最新数据',
  },
  docs_today: {
    color: 'blue',
    label: '今日',
    desc: '今天 beat 已落盘的 docs/<kind>/<today>.json',
  },
  docs_archived: {
    color: 'gold',
    label: '历史',
    desc: '当前日期文件缺失,回退到最近一份 archived JSON',
  },
  missing: {
    color: 'red',
    label: '缺失',
    desc: 'docs 目录没有任何可读文件;请点 Rerun 触发 beat',
  },
}

export default function SourceTagBadge({ source, staleDays = null, style }) {
  const meta = SOURCE_META[source] || { color: 'default', label: source, desc: source }
  const tooltip = staleDays
    ? `${meta.desc}(${staleDays} 天前的 archive)`
    : meta.desc
  return (
    <Tooltip title={tooltip}>
      <Tag color={meta.color} style={style}>
        {meta.label}
        {staleDays ? ` · ${staleDays}d` : ''}
      </Tag>
    </Tooltip>
  )
}
