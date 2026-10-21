import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import type { Workspace } from '../../api/client'
import { workspaceApi } from '../../api/workspaces'

const EMPTY_WORKSPACES: Workspace[] = []

export function useWorkspaces() {
  const [search, setSearch] = useSearchParams()
  const selectedId = search.get('workspace') ?? ''
  const [revision, setRevision] = useState(0)
  const [collection, setCollection] = useState<{
    revision: number; items: Workspace[]; hasMore: boolean; error: string | null; loadingMore: boolean
  }>({ revision: -1, items: [], hasMore: false, error: null, loadingMore: false })
  const [selection, setSelection] = useState<{
    id: string; revision: number; workspace: Workspace | null; error: string | null
  }>({ id: '', revision: -1, workspace: null, error: null })
  const loading = collection.revision !== revision
  const items = loading ? EMPTY_WORKSPACES : collection.items
  const error = loading ? null : collection.error
  const hasMore = !loading && collection.hasMore
  const loadingMore = !loading && collection.loadingMore
  const generation = useRef(0)
  const moreBusy = useRef(false)

  const reload = useCallback(() => setRevision((value) => value + 1), [])
  const select = useCallback((id: string) => {
    setSearch((current) => {
      const next = new URLSearchParams(current)
      next.set('workspace', id)
      return next
    })
  }, [setSearch])

  useEffect(() => {
    const controller = new AbortController()
    const current = ++generation.current
    moreBusy.current = false
    workspaceApi.list(0, controller.signal).then((page) => {
      if (current !== generation.current) return
      setCollection({ revision, items: page.items, hasMore: page.has_more, error: null, loadingMore: false })
    }).catch((cause) => {
      if (!controller.signal.aborted) {
        setCollection({ revision, items: [], hasMore: false, loadingMore: false,
          error: cause instanceof Error ? cause.message : 'Could not load workspaces.',
        })
      }
    })
    return () => { controller.abort(); generation.current += 1 }
  }, [revision])

  useEffect(() => {
    if (!loading && !error && !selectedId && items.length) select(items[0].id)
  }, [loading, error, selectedId, items, select])

  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    workspaceApi.get(selectedId, controller.signal).then((workspace) => {
      if (!controller.signal.aborted) setSelection({ id: selectedId, revision, workspace, error: null })
    }).catch((cause) => {
      if (!controller.signal.aborted) {
        const transient = cause instanceof ApiError && (cause.status === 0 || cause.status === 429 || cause.status >= 500)
        // Keep an uncertain upload's File/UUID during a temporary same-workspace
        // outage. Writes remain paused until a successful access recheck.
        setSelection((previous) => ({ id: selectedId, revision,
          workspace: transient && previous.id === selectedId ? previous.workspace : null,
          error: cause instanceof ApiError && cause.status === 404
            ? 'This workspace is unavailable or you no longer have access. Choose another workspace.'
            : 'Could not verify workspace access. Writing is paused. Retry the access check when the service is available.',
        }))
      }
    })
    return () => controller.abort()
  }, [selectedId, revision])

  useEffect(() => {
    window.addEventListener('focus', reload)
    return () => window.removeEventListener('focus', reload)
  }, [reload])

  async function loadMore() {
    if (moreBusy.current || loading || !hasMore) return
    moreBusy.current = true
    setCollection((previous) => ({ ...previous, loadingMore: true, error: null }))
    const current = generation.current
    try {
      const page = await workspaceApi.list(items.length)
      if (current !== generation.current) return
      setCollection((previous) => ({ ...previous,
        items: [...previous.items, ...page.items.filter((item) => !previous.items.some((old) => old.id === item.id))],
        hasMore: page.has_more,
      }))
    } catch (cause) {
      if (current === generation.current) setCollection((previous) => ({ ...previous,
        error: cause instanceof Error ? cause.message : 'Could not load more workspaces.',
      }))
    } finally {
      if (current === generation.current) {
        moreBusy.current = false
        setCollection((previous) => ({ ...previous, loadingMore: false }))
      }
    }
  }

  const matchesSelection = selectedId && selection.id === selectedId && selection.revision === revision
  // Preserve drafts during a recheck of the same workspace; writes pause until verified.
  const active = selectedId && selection.id === selectedId ? selection.workspace : null
  const selectionError = matchesSelection ? selection.error : null
  const checkingAccess = !!selectedId && (!matchesSelection || !!selectionError)
  return { items, loading, loadingMore, hasMore, error, active, selectedId, selectionError, checkingAccess, select, reload, loadMore }
}
