// Shared alpha quality_status presentation maps. Single source of truth for
// AlphaList (table/filters/strip) and AlphaDetail (transitions timeline) so the
// two pages can't drift when a status color/label changes.

export const STATUS_COLORS = {
  PASS: 'success',
  PASS_PROVISIONAL: 'gold',
  OPTIMIZE: 'processing',
  FAIL: 'default',
  PENDING: 'default',
  REJECT: 'error',
}

export const STATUS_LABELS = {
  PASS: '通过',
  PASS_PROVISIONAL: '临时通过',
  OPTIMIZE: '待优化',
  FAIL: '失败',
  PENDING: '待处理',
  REJECT: '拒绝',
}
