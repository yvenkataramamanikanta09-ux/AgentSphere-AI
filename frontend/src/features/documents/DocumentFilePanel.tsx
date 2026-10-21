import { useContext, useEffect, useRef, useState } from 'react'
import { ApiError, onSessionExpired } from '../../api/client'
import { fileApi } from '../../api/documentFiles'
import type { DocumentFileMetadata, DocumentSection, DocumentSectionPage } from '../../api/documentFiles'
import type { DocumentRecord } from '../../api/documents'
import { AuthContext } from '../auth/context'
import { fileRequestError, processingFailure } from './fileErrors'
import { safeDownloadFilename } from './fileValidation'
import './files.css'

interface Props {
  workspaceId: string
  record: DocumentRecord
  onFileChange?: (file: DocumentFileMetadata | null) => void
  onPermissionFailure: () => void
}
interface FileState { scope: string; metadata: DocumentFileMetadata | null; loading: boolean; error: string | null }
interface SectionsState { scope: string; page: DocumentSectionPage | null; loading: boolean; error: string | null }
const POLL_MS = 3_000

function SourceReference({ section }: { section: DocumentSection }) {
  const references: string[] = []
  if (section.page_number !== null) references.push(`Page ${section.page_number}`)
  if (section.line_start !== null && section.line_end !== null) references.push(section.line_start === section.line_end ? `Line ${section.line_start}` : `Lines ${section.line_start}–${section.line_end}`)
  return references.length ? <p className="file-source">{references.join(' · ')}</p> : null
}

export function DocumentFilePanel(props: Props) {
  const auth = useContext(AuthContext)
  const authenticated = !auth || auth.status === 'authenticated'
  return <FilePanel key={`${props.workspaceId}/${props.record.id}/${auth?.user?.id ?? ''}/${authenticated}`} {...props} authenticated={authenticated} />
}

function FilePanel({ workspaceId, record, onFileChange, onPermissionFailure, authenticated }: Props & { authenticated: boolean }) {
  const scope = `${workspaceId}/${record.id}`
  const [refresh, setRefresh] = useState(0)
  const [state, setState] = useState<FileState>({ scope, metadata: null, loading: authenticated, error: authenticated ? null : 'Sign in again to view this file.' })
  const [refreshRequired, setRefreshRequired] = useState(false)
  const [sessionEnded, setSessionEnded] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [sectionOffset, setSectionOffset] = useState(0)
  const [sectionRetry, setSectionRetry] = useState(0)
  const [sections, setSections] = useState<SectionsState>({ scope, page: null, loading: false, error: null })
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [downloadNotice, setDownloadNotice] = useState<string | null>(null)
  const callbacks = useRef({ onFileChange, onPermissionFailure })
  useEffect(() => { callbacks.current = { onFileChange, onPermissionFailure } }, [onFileChange, onPermissionFailure])
  const generation = useRef(0)
  const active = useRef(false)
  const metadataRequest = useRef<AbortController | null>(null)
  const sectionRequest = useRef<AbortController | null>(null)
  const downloadRequest = useRef<AbortController | null>(null)
  const downloadBusy = useRef(false)
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null)
  const objectUrls = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  useEffect(() => {
    const current = ++generation.current
    active.current = authenticated
    downloadBusy.current = false

    function stop() {
      active.current = false
      generation.current += 1
      if (poll.current !== null) clearTimeout(poll.current)
      poll.current = null
      metadataRequest.current?.abort(); sectionRequest.current?.abort(); downloadRequest.current?.abort()
      for (const [url, timeout] of objectUrls.current) { clearTimeout(timeout); URL.revokeObjectURL(url) }
      objectUrls.current.clear()
      downloadBusy.current = false
    }

    const unsubscribe = onSessionExpired(() => {
      stop()
      setSessionEnded(true); setPreviewOpen(false); setDownloading(false); setRefreshRequired(true)
      setState({ scope, metadata: null, loading: false, error: 'Your session has expired. Sign in again to continue.' })
      setSections({ scope, page: null, loading: false, error: null })
    })

    async function load() {
      if (!active.current || generation.current !== current) return
      const request = new AbortController()
      metadataRequest.current = request
      try {
        const metadata = await fileApi.get(workspaceId, record.id, request.signal)
        if (!active.current || generation.current !== current || request.signal.aborted) return
        setState({ scope, metadata, loading: false, error: null })
        setRefreshRequired(false)
        callbacks.current.onFileChange?.(metadata)
        if (metadata.processing_status === 'queued' || metadata.processing_status === 'processing') {
          // Schedule only after this request finishes; a slow request cannot overlap a poll.
          poll.current = setTimeout(() => { poll.current = null; void load() }, POLL_MS)
        }
      } catch (cause) {
        if (!active.current || generation.current !== current || request.signal.aborted) return
        if (cause instanceof ApiError && cause.status === 404) {
          setState({ scope, metadata: null, loading: false, error: null })
          callbacks.current.onFileChange?.(null)
        } else {
          setState((previous) => ({ ...previous, scope, loading: false, error: fileRequestError(cause, 'file') }))
          setRefreshRequired(true)
          if (cause instanceof ApiError && cause.status === 403) callbacks.current.onPermissionFailure()
        }
        // Errors deliberately stop polling. The user can refresh after checking access or service health.
      } finally {
        if (metadataRequest.current === request) metadataRequest.current = null
      }
    }
    if (authenticated) void load()
    return () => { stop(); unsubscribe() }
  }, [workspaceId, record.id, scope, authenticated, refresh])

  const currentState = state.scope === scope ? state : { scope, metadata: null, loading: true, error: null }
  const file = authenticated && !sessionEnded ? currentState.metadata : null
  const canRead = !!file && file.processing_status === 'ready' && file.download_available && !refreshRequired && !currentState.loading && !currentState.error && authenticated && !sessionEnded

  function refreshFile() {
    if (currentState.loading || !authenticated || sessionEnded) return
    setState((previous) => ({ ...previous, loading: true, error: null }))
    setRefreshRequired(true)
    setPreviewOpen(false)
    setSections({ scope, page: null, loading: false, error: null })
    setSectionOffset(0)
    setSectionRetry(0)
    setDownloading(false)
    setDownloadError(null)
    setDownloadNotice(null)
    setRefresh((value) => value + 1)
  }

  useEffect(() => {
    if (!previewOpen || !canRead) return
    const current = generation.current
    const request = new AbortController()
    sectionRequest.current = request
    async function loadSections() {
      try {
        const page = await fileApi.sections(workspaceId, record.id, 20, sectionOffset, request.signal)
        if (!active.current || generation.current !== current || request.signal.aborted) return
        setSections({ scope, page, loading: false, error: null })
      } catch (cause) {
        if (!active.current || generation.current !== current || request.signal.aborted) return
        setSections({ scope, page: null, loading: false, error: fileRequestError(cause, 'sections') })
        if (cause instanceof ApiError && [401, 403, 404, 409].includes(cause.status)) setRefreshRequired(true)
        if (cause instanceof ApiError && cause.status === 403) callbacks.current.onPermissionFailure()
      }
    }
    void loadSections()
    return () => { request.abort(); if (sectionRequest.current === request) sectionRequest.current = null }
  }, [workspaceId, record.id, scope, canRead, previewOpen, sectionOffset, sectionRetry])

  function showSections(offset: number) {
    setSections((previous) => ({ ...previous, scope, loading: true, error: null }))
    setSectionOffset(offset)
  }

  async function download() {
    if (!canRead || downloadBusy.current || !active.current) return
    downloadBusy.current = true
    setDownloading(true); setDownloadError(null); setDownloadNotice(null)
    const current = generation.current
    const request = new AbortController()
    downloadRequest.current = request
    try {
      const blob = await fileApi.download(workspaceId, record.id, request.signal)
      if (!active.current || generation.current !== current || request.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const timeout = setTimeout(() => { URL.revokeObjectURL(url); objectUrls.current.delete(url) }, 1_000)
      objectUrls.current.set(url, timeout)
      const link = document.createElement('a')
      link.href = url; link.download = safeDownloadFilename(record.filename)
      document.body.append(link)
      try { link.click() } finally { link.remove() }
      setDownloadNotice('Original file download started.')
    } catch (cause) {
      if (!active.current || generation.current !== current || request.signal.aborted) return
      setDownloadError(fileRequestError(cause, 'download'))
      setDownloadNotice(null); setRefreshRequired(true)
      if (cause instanceof ApiError && cause.status === 403) callbacks.current.onPermissionFailure()
    } finally {
      if (generation.current === current) { downloadBusy.current = false; setDownloading(false) }
      if (downloadRequest.current === request) downloadRequest.current = null
    }
  }

  const currentSections = sections.scope === scope ? sections : { scope, page: null, loading: false, error: null }
  const previousUnavailable = currentSections.loading || !currentSections.page || currentSections.page.offset === 0
  const nextUnavailable = currentSections.loading || !currentSections.page?.has_more
  return <section className="document-file-panel" aria-labelledby="file-processing-title">
    <div className="document-panel-heading"><h4 id="file-processing-title">File processing</h4><button type="button" className="refresh-button" aria-disabled={currentState.loading || !authenticated || sessionEnded} onClick={refreshFile}>Refresh file status</button></div>
    {currentState.loading ? <p className="document-help" role="status">Loading file status…</p> : currentState.error ? <div className="notice error" role="alert">{currentState.error}</div> : !file ? <p className="document-help">Metadata-only record. No file has been uploaded.</p> : <>
      <p className={`file-processing-status file-processing-${file.processing_status}`} role="status" aria-live="polite">{file.processing_status === 'queued' ? 'Queued for processing' : file.processing_status === 'processing' ? 'Processing file' : file.processing_status === 'ready' ? 'Ready' : 'Processing failed'}</p>
      {file.processing_status === 'queued' && <p className="document-help">The file has been received and is waiting for a worker. Processing starts when a worker is available.</p>}
      {file.processing_status === 'processing' && <p className="document-help">A worker is extracting text. Download and preview become available after successful processing.</p>}
      {file.processing_status === 'failed' && <p className="file-processing-failure">{processingFailure(file.error_code)}</p>}
      {file.processing_status === 'ready' && !file.download_available && <p className="document-help">Processing is ready, but the original and extracted text are not currently available. Refresh file status to check again.</p>}
      {file.warning_code === 'no_extractable_text' && <p className="file-warning" role="status">No extractable text was found. Scanned documents may need OCR, which is not available here.</p>}
      <dl className="file-metadata"><dt>Original filename</dt><dd>{file.original_filename}</dd><dt>Uploaded</dt><dd>{new Date(file.uploaded_at).toLocaleString()}</dd></dl>
      <div className="file-actions"><button className="secondary-button" type="button" disabled={!canRead || downloading} onClick={() => void download()}>{downloading ? 'Preparing download…' : 'Download original'}</button><button className="secondary-button" type="button" aria-expanded={previewOpen && canRead} aria-controls="document-extracted-text" disabled={!canRead} onClick={() => { setPreviewOpen((value) => !value); showSections(0) }}>{previewOpen && canRead ? 'Hide extracted text' : 'Preview extracted text'}</button></div>
    </>}
    {refreshRequired && file && <p className="file-warning" role="status">Refresh file status before trying file actions again.</p>}
    {downloadError && <div className="notice error" role="alert">{downloadError}</div>}
    {downloadNotice && <p className="document-help" role="status">{downloadNotice}</p>}
    {currentSections.error && <div className="file-sections-error"><div className="notice error" role="alert">{currentSections.error}</div>{canRead && <button className="secondary-button" type="button" onClick={() => { showSections(sectionOffset); setSectionRetry((value) => value + 1) }}>Retry extracted text</button>}</div>}
    {previewOpen && canRead && <div id="document-extracted-text" className="file-sections" aria-label="Extracted text">
      <h4>Extracted text</h4>
      {currentSections.loading ? <p role="status">Loading extracted text…</p> : currentSections.page && <>
        {!currentSections.page.items.length && <p className="file-warning" role="status">No extractable text was found on this page.</p>}
        {currentSections.page.items.map((section) => <article className="file-section" key={section.id}><h5>{section.heading || `Section ${section.ordinal + 1}`}</h5><SourceReference section={section} /><pre>{section.text}</pre></article>)}
      </>}
      <div className="file-section-pagination"><button className="secondary-button" type="button" aria-disabled={previousUnavailable} onClick={() => { if (!previousUnavailable) showSections(Math.max(0, currentSections.page!.offset - 20)) }}>Previous sections</button><span role="status" aria-live="polite" aria-atomic="true">{currentSections.loading ? 'Loading section page…' : currentSections.page ? `Page ${Math.floor(currentSections.page.offset / 20) + 1}` : 'No section page loaded'}</span><button className="secondary-button" type="button" aria-disabled={nextUnavailable} onClick={() => { if (!nextUnavailable) showSections(currentSections.page!.offset + 20) }}>Next sections</button></div>
    </div>}
  </section>
}
