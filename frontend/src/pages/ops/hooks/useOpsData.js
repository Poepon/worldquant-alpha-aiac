import { useCallback, useEffect, useState } from 'react'
import { message } from 'antd'

/**
 * useOpsData — minimal React-Query-style hook for /ops/* GET endpoints.
 *
 * Deliberately a thin wrapper (not React Query proper) — the project
 * doesn't currently bundle @tanstack/react-query, and pulling it in for
 * Phase 2 alone would add ~30KB to the bundle. If we later add it
 * project-wide we can swap this hook's body without touching consumers.
 *
 * Behaviour:
 * - Auto-fetches on mount and when any `deps` change.
 * - Exposes { data, loading, error, refetch } so the page can render
 *   skeletons / errors itself.
 * - Errors surface as Ant message toasts AND go into `error` for
 *   in-component handling.
 */
export default function useOpsData(fetchFn, deps = []) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const refetch = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchFn()
      setData(result)
      return result
    } catch (e) {
      setError(e)
      const detail = e?.response?.data?.detail || e.message
      message.error(`加载失败:${detail}`)
      return null
    } finally {
      setLoading(false)
    }
    // fetchFn changes per render in many cases; we intentionally exclude
    // it from the dep array so consumers control re-fetch via `deps`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    refetch()
  }, [refetch])

  return { data, loading, error, refetch }
}
