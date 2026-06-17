import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import utc from 'dayjs/plugin/utc'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.extend(utc)
dayjs.locale('zh-cn')

// Naive timestamps from naive-UTC `DateTime` columns (e.g. alpha.created_at /
// updated_at, server_default=func.now()) serialize WITHOUT a tz marker
// ("2026-06-16T15:31:22"). Plain dayjs() parses those as *local* time → an 8h
// Shanghai skew. Use the *UTC variants below for those fields. They interpret a
// naive string as UTC then convert to local. (Do NOT use these for the
// naive-BEIJING columns date_created/date_modified/date_submitted — those are
// already local and the plain formatters are correct for them.)
function asUtcLocal(input) {
  return dayjs.utc(input).local()
}

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

// UTC variants — for naive-UTC columns (created_at / updated_at). See asUtcLocal.
export function formatRelativeUTC(input) {
  if (!input) return '—'
  const d = asUtcLocal(input)
  if (!d.isValid()) return '—'
  return d.fromNow()
}

export function formatDateTimeUTC(input) {
  if (!input) return '—'
  const d = asUtcLocal(input)
  if (!d.isValid()) return '—'
  return d.format('YYYY-MM-DD HH:mm')
}
