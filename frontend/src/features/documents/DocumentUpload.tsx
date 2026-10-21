import { useContext, useEffect, useRef, useState } from 'react'
import { ApiError, onSessionExpired } from '../../api/client'
import { fileApi } from '../../api/documentFiles'
import type { UploadResult } from '../../api/documentFiles'
import { AuthContext } from '../auth/context'
import { fileRequestError } from './fileErrors'
import { DOCUMENT_FILE_ACCEPT, uploadFileError } from './fileValidation'
import './files.css'

interface Props {
  workspaceId: string
  disabled: boolean
  onUploaded: (result: UploadResult) => void
  onCancel: () => void
  onPermissionFailure: () => void
}
type Intent = { file: File; key: string }
type Phase = 'idle' | 'uploading' | 'received' | 'error' | 'cancelled'

export function DocumentUpload(props: Props) {
  const auth = useContext(AuthContext)
  const authenticated = !auth || auth.status === 'authenticated'
  return <UploadForm key={`${props.workspaceId}/${auth?.user?.id ?? ''}/${authenticated}`} {...props} authenticated={authenticated} />
}

function UploadForm({ workspaceId, disabled, onUploaded, onCancel, onPermissionFailure, authenticated }: Props & { authenticated: boolean }) {
  const [intent, setIntent] = useState<Intent | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sessionEnded, setSessionEnded] = useState(false)
  const chooser = useRef<HTMLInputElement>(null)
  const feedback = useRef<HTMLDivElement>(null)
  const controller = useRef<AbortController | null>(null)
  const operation = useRef(0)
  const busy = useRef(false)
  const active = useRef(false)

  useEffect(() => {
    active.current = authenticated
    operation.current += 1
    busy.current = false
    chooser.current?.focus()
    const unsubscribe = onSessionExpired(() => {
      active.current = false
      operation.current += 1
      controller.current?.abort()
      busy.current = false
      setIntent(null); setPhase('error'); setSessionEnded(true)
      setError('Your session has expired. Sign in again to continue.')
    })
    return () => {
      active.current = false
      operation.current += 1
      controller.current?.abort()
      busy.current = false
      unsubscribe()
    }
  }, [authenticated])

  useEffect(() => { if (phase === 'error' || phase === 'cancelled' || phase === 'received') feedback.current?.focus() }, [phase, error])

  function choose(file?: File) {
    if (busy.current || disabled || !authenticated || sessionEnded) return
    setProgress(null); setPhase('idle'); setIntent(null)
    if (!file) { setError(null); return }
    const invalid = uploadFileError(file)
    if (invalid) { setError(invalid); setPhase('error'); return }
    setIntent({ file, key: crypto.randomUUID() })
    setError(null)
  }

  async function upload() {
    if (!intent || busy.current || disabled || !authenticated || !active.current || sessionEnded) return
    busy.current = true // Includes the API's asynchronous CSRF preflight.
    const current = ++operation.current
    const request = new AbortController()
    controller.current = request
    setPhase('uploading'); setProgress(null); setError(null)
    try {
      const result = await fileApi.upload(workspaceId, intent.file, intent.key, {
        signal: request.signal,
        onProgress: (percent) => {
          if (active.current && current === operation.current) setProgress(percent === null ? null : Math.max(0, Math.min(100, Math.round(percent))))
        },
      })
      if (!active.current || current !== operation.current || request.signal.aborted) return
      setPhase('received')
      onUploaded(result)
    } catch (cause) {
      if (!active.current || current !== operation.current || request.signal.aborted) return
      setPhase('error'); setError(fileRequestError(cause, 'upload'))
      if (cause instanceof ApiError && cause.status === 403) onPermissionFailure()
    } finally {
      if (current === operation.current) { busy.current = false; controller.current = null }
    }
  }

  function cancelTransfer() {
    operation.current += 1
    controller.current?.abort()
    controller.current = null
    busy.current = false
    setPhase('cancelled'); setProgress(null)
    setError('Transfer cancelled. The server may already have received the file. Retry this same upload to check its result without creating another document.')
  }

  const uploading = phase === 'uploading'
  const unavailable = disabled || !authenticated || sessionEnded
  return <section className="document-upload document-form panel" aria-labelledby="document-upload-title">
    <div className="document-panel-heading"><div><span className="section-label">DOCUMENT FILE</span><h3 id="document-upload-title">Upload document</h3></div><button className="refresh-button" type="button" onClick={uploading ? cancelTransfer : onCancel}>{uploading ? 'Cancel upload' : 'Cancel'}</button></div>
    <p className="document-help" id="document-upload-help">PDF, DOCX, UTF-8 text, or Markdown. Maximum 10,000,000 bytes (10 MB). Files are stored privately; processing runs after the server receives them.</p>
    <form onSubmit={(event) => { event.preventDefault(); void upload() }}>
      <div className="field"><label htmlFor="document-file">Choose document</label><input ref={chooser} id="document-file" type="file" accept={DOCUMENT_FILE_ACCEPT} disabled={unavailable || uploading || phase === 'received'} aria-invalid={!!error && !intent} aria-describedby={error ? 'document-upload-help document-upload-feedback' : 'document-upload-help'} onChange={(event) => choose(event.currentTarget.files?.[0])} /></div>
      {intent && <p className="file-selection">{intent.file.name} <span>· {intent.file.size.toLocaleString()} bytes</span></p>}
      {uploading && <div className="file-transfer" role="status" aria-live="polite"><label htmlFor="document-upload-progress">{progress === 100 ? 'Transfer complete. Waiting for server confirmation…' : progress === null ? 'Uploading file…' : `Uploading file… ${progress}%`}</label><progress id="document-upload-progress" max={100} value={progress ?? undefined} /></div>}
      {error && <div ref={feedback} tabIndex={-1} id="document-upload-feedback" className="notice error" role="alert">{error}</div>}
      {phase === 'received' && <div ref={feedback} tabIndex={-1} className="notice success" role="status">File received. Check File processing for extraction status.</div>}
      <div className="form-actions"><button className="primary-button" type="submit" disabled={!intent || unavailable || uploading || phase === 'received'}>{uploading ? 'Uploading…' : intent && (phase === 'error' || phase === 'cancelled') ? 'Retry same upload' : 'Upload file'}</button></div>
    </form>
  </section>
}
