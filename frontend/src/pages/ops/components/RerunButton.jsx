import { useState } from 'react'
import { Button, Popconfirm, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

/**
 * RerunButton — wraps a POST /ops/<page>/rerun call with confirm + toast.
 *
 * - Popconfirm prevents accidental fires (per-task SETNX locks for 60s).
 * - 409 / 429 from backend surface as warning toasts with the wait time.
 * - onSuccess receives the {task_id, ...} payload so the caller can
 *   invalidate React-Query caches / poll the latest endpoint.
 *
 * Note: the per-task 60s lock + 10/min global throttle live server-side.
 * We don't shadow them here; the backend is the single source of truth.
 */
export default function RerunButton({
  triggerFn,
  label = '重新计算',
  confirmTitle = '触发后台任务?(60s 内同任务无法再次触发)',
  onSuccess,
  size,
}) {
  const [busy, setBusy] = useState(false)

  const handle = async () => {
    setBusy(true)
    try {
      const result = await triggerFn()
      message.success(`已加入队列 · task_id=${result.task_id}(几秒后刷新页面查看结果)`)
      if (onSuccess) onSuccess(result)
    } catch (e) {
      const detail = e?.response?.data?.detail || e.message
      const status = e?.response?.status
      if (status === 409) {
        message.warning(`节流中:${detail}`)
      } else if (status === 429) {
        message.warning(`全局限流:${detail}`)
      } else {
        message.error(`触发失败:${detail}`)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popconfirm title={confirmTitle} onConfirm={handle}>
      <Button icon={<ReloadOutlined />} loading={busy} size={size}>
        {label}
      </Button>
    </Popconfirm>
  )
}
