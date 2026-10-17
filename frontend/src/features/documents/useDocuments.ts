import { useCallback, useEffect, useState } from 'react'
import { documentApi } from '../../api/documents'
import type { DocumentPage, DocumentRecord, DocumentStatus } from '../../api/documents'
import { documentError } from './errors'

export function useDocuments(workspaceId: string) {
  const [query, setQuery] = useState({ status: '' as DocumentStatus | '', offset: 0, revision: 0 })
  const [selectedId, select] = useState('')
  const key = `${workspaceId}:${query.status}:${query.offset}:${query.revision}`
  const [listing, setListing] = useState<{ key: string; page: DocumentPage | null; error: string | null }>({ key: '', page: null, error: null })
  const detailKey = `${workspaceId}:${selectedId}:${query.revision}`
  const [detail, setDetail] = useState<{ key: string; record: DocumentRecord | null; error: string | null }>({ key: '', record: null, error: null })

  useEffect(() => {
    const controller = new AbortController()
    documentApi.list(workspaceId, query.offset, query.status, controller.signal).then((page) => {
      if (!controller.signal.aborted) setListing({ key, page, error: null })
    }).catch((error) => {
      if (!controller.signal.aborted) setListing({ key, page: null, error: documentError(error) })
    })
    return () => controller.abort()
  }, [workspaceId, query.offset, query.status, key])

  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    documentApi.get(workspaceId, selectedId, controller.signal).then((record) => {
      if (!controller.signal.aborted) setDetail({ key: detailKey, record, error: null })
    }).catch((error) => {
      if (!controller.signal.aborted) setDetail({ key: detailKey, record: null, error: documentError(error) })
    })
    return () => controller.abort()
  }, [workspaceId, selectedId, detailKey])

  const refresh = useCallback((reset = false) => setQuery((current) => ({ ...current, offset: reset ? 0 : current.offset, revision: current.revision + 1 })), [])
  useEffect(() => {
    const onFocus = () => refresh()
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [refresh])
  const setStatus = (status: DocumentStatus | '') => setQuery((current) => ({ ...current, status, offset: 0 }))
  const setOffset = (offset: number) => setQuery((current) => ({ ...current, offset }))
  return {
    query, refresh, setStatus, setOffset, selectedId, select,
    loading: listing.key !== key,
    page: listing.key === key ? listing.page : null,
    error: listing.key === key ? listing.error : null,
    detailLoading: !!selectedId && detail.key !== detailKey,
    record: selectedId && detail.key === detailKey ? detail.record : null,
    detailError: selectedId && detail.key === detailKey ? detail.error : null,
  }
}
