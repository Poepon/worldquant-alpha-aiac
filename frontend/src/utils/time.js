import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

export function formatRelative(input) {
  if (!input) return '—'
  const d = dayjs(input)
  if (!d.isValid()) return '—'
  return d.fromNow()
}

export function formatDateTime(input) {
  if (!input) return '—'
  const d = dayjs(input)
  if (!d.isValid()) return '—'
  return d.format('YYYY-MM-DD HH:mm')
}

export function formatTime(input) {
  if (!input) return '--:--:--'
  const d = dayjs(input)
  if (!d.isValid()) return '--:--:--'
  return d.format('HH:mm:ss')
}
