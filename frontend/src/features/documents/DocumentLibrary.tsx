import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import type { Workspace } from '../../api/client'
import { documentApi } from '../../api/documents'
import type { DocumentRecord, DocumentStatus } from '../../api/documents'
import { DocumentForm } from './DocumentForm'
import { documentError } from './errors'
import { useDocuments } from './useDocuments'
import './documents.css'

function StatusBadge({ status }: { status: DocumentStatus }) {
  return <span className={`document-status document-status-${status}`}>{status}</span>
}
const date = (value: string) => new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export function DocumentLibrary({ workspace, checkingAccess = false, onPermissionFailure }: {
  workspace: Workspace; checkingAccess?: boolean; onPermissionFailure: () => void
}) {
  const data = useDocuments(workspace.id)
  const [editor, setEditor] = useState<DocumentRecord | 'new' | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<DocumentRecord | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const mounted = useRef(true)
  const addButton = useRef<HTMLButtonElement>(null)
  const detailHeading = useRef<HTMLHeadingElement>(null)
  const statusFilter = useRef<HTMLSelectElement>(null)
  const deleteButton = useRef<HTMLButtonElement>(null)
  const previousEditor = useRef<typeof editor>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    if (data.selectedId && !editor) detailHeading.current?.focus()
  }, [data.selectedId, editor])
  useEffect(() => {
    if (previousEditor.current && !editor) {
      if (previousEditor.current === 'new') addButton.current?.focus()
      else detailHeading.current?.focus()
    }
    previousEditor.current = editor
  }, [editor])
  const canWrite = workspace.role === 'owner' || workspace.role === 'editor'
  const writesDisabled = !canWrite || checkingAccess || busy

  function saved(record: DocumentRecord) {
    const editing = editor !== 'new'
    setEditor(null)
    setActionError(null)
    setNotice(`“${record.filename}” ${editing ? 'updated' : 'created'}.`)
    data.select(record.id)
    data.refresh(!editing)
  }

  async function perform(action: 'archive' | 'restore' | 'delete', record: DocumentRecord) {
    if (writesDisabled || busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setActionError(null)
    try {
      if (action === 'delete') await documentApi.remove(workspace.id, record.id)
      else await documentApi.update(workspace.id, record.id, { status: action === 'archive' ? 'archived' : 'registered' })
      if (!mounted.current) return
      setConfirmDelete(null)
      setNotice(`“${record.filename}” ${action === 'delete' ? 'deleted' : action === 'archive' ? 'archived' : 'restored'}.`)
      if (action === 'delete') {
        data.select('')
        statusFilter.current?.focus()
        if (data.page?.items.length === 1 && data.query.offset > 0) data.setOffset(Math.max(0, data.query.offset - 10))
      }
      data.refresh()
    } catch (cause) {
      if (!mounted.current) return
      setActionError(documentError(cause))
      if (cause instanceof ApiError && cause.status === 403) onPermissionFailure()
    } finally {
      busyRef.current = false
      if (mounted.current) setBusy(false)
    }
  }

  const record = data.record
  return <section className="document-library" id="document-library" aria-labelledby="library-title">
    <div className="section-heading document-library-heading"><div><span className="section-label">KNOWLEDGE RECORDS</span><h2 id="library-title">Document library</h2><p>Metadata for {workspace.name}. Files are not uploaded.</p></div>{canWrite && <button type="button" ref={addButton} className="primary-button" disabled={writesDisabled || !!editor} onClick={() => { setEditor('new'); setConfirmDelete(null); setActionError(null); setNotice(null) }}><span aria-hidden="true">＋</span>Add record</button>}</div>
    {!canWrite && <p className="document-readonly">Viewer access · You can browse records and details. Editing requires an owner or editor.</p>}
    {checkingAccess && <p className="document-help" role="status">Rechecking workspace access. Writing is temporarily paused.</p>}
    {notice && <div className="notice success dashboard-notice" role="status"><span>{notice}</span><button type="button" aria-label="Dismiss document message" onClick={() => setNotice(null)}>×</button></div>}
    {actionError && <div className="notice error" role="alert">{actionError}</div>}
    {editor && <DocumentForm key={editor === 'new' ? 'new' : editor.id} workspaceId={workspace.id} record={editor === 'new' ? undefined : editor} disabled={writesDisabled} onSaved={saved} onCancel={() => setEditor(null)} onPermissionFailure={onPermissionFailure} />}
    <div className="document-library-body">
      <div className="document-list panel"><div className="document-toolbar"><div><label htmlFor="document-status-filter">Status</label><select ref={statusFilter} id="document-status-filter" value={data.query.status} onChange={(event) => data.setStatus(event.target.value as DocumentStatus | '')}><option value="">All records</option><option value="registered">Registered</option><option value="archived">Archived</option></select></div><button type="button" className="refresh-button" onClick={() => data.refresh()} disabled={data.loading}>Refresh records</button></div>
        {data.loading ? <div className="document-empty" role="status"><span className="empty-symbol" aria-hidden="true">▤</span><p>Loading document records…</p></div>
          : data.error ? <div className="document-empty"><div className="notice error" role="alert">{data.error}</div><button className="secondary-button" type="button" onClick={() => data.refresh()}>Retry documents</button><button className="inline-button" type="button" onClick={onPermissionFailure}>Check workspace access</button></div>
            : !data.page?.items.length ? <div className="document-empty"><span className="empty-symbol" aria-hidden="true">▤</span><h3>{data.query.offset > 0 ? 'No records on this page' : data.query.status ? 'No matching records' : 'No document records yet'}</h3><p>{data.query.status ? 'Choose another status or add a matching record.' : canWrite ? 'Add a filename, content type, and declared size to get started.' : 'Records added by an owner or editor will appear here.'}</p></div>
              : <div className="document-table-wrap"><table className="document-table"><thead><tr><th scope="col">Document</th><th scope="col">Declared size</th><th scope="col">Status</th></tr></thead><tbody>{data.page.items.map((item) => <tr key={item.id} className={data.selectedId === item.id ? 'document-row-active' : ''}><td><button type="button" className="document-name" disabled={!!editor || busy} aria-label={`View ${item.filename}`} onClick={() => { data.select(item.id); setConfirmDelete(null); setActionError(null) }}><span className="document-file-icon" aria-hidden="true">▤</span><span>{item.filename}</span></button><small className="document-content-type">{item.content_type}</small></td><td className="document-size">{item.size_bytes.toLocaleString()} bytes</td><td><StatusBadge status={item.status} /></td></tr>)}</tbody></table></div>}
        <div className="document-pagination"><button type="button" className="secondary-button" disabled={data.query.offset === 0 || data.loading} onClick={() => data.setOffset(Math.max(0, data.query.offset - 10))}>Previous</button><span>Page {Math.floor(data.query.offset / 10) + 1}</span><button type="button" className="secondary-button" disabled={!data.page?.has_more || data.loading} onClick={() => data.setOffset(data.query.offset + 10)}>Next</button></div>
      </div>
      {data.selectedId && !editor && <aside className="document-detail panel" aria-label="Document details"><div className="document-panel-heading"><h3 ref={detailHeading} tabIndex={-1}>Record details</h3><button type="button" className="refresh-button" disabled={busy} onClick={() => { data.select(''); setConfirmDelete(null); setActionError(null); statusFilter.current?.focus() }}>Close details</button></div>
        {data.detailLoading ? <p role="status">Loading record details…</p> : data.detailError ? <><div className="notice error" role="alert">{data.detailError}</div><button type="button" className="secondary-button" onClick={() => data.refresh()}>Retry details</button></> : record && <>
          <h4>{record.filename}</h4><StatusBadge status={record.status} /><dl><dt>Content type</dt><dd>{record.content_type}</dd><dt>Declared size</dt><dd>{record.size_bytes.toLocaleString()} bytes</dd><dt>Created</dt><dd>{date(record.created_at)}</dd><dt>Updated</dt><dd>{date(record.updated_at)}</dd><dt>Record ID</dt><dd className="document-id">{record.id}</dd><dt>Workspace ID</dt><dd className="document-id">{record.workspace_id}</dd></dl>
          {canWrite && <div className="document-actions"><button type="button" className="secondary-button" disabled={writesDisabled} onClick={() => { setEditor(record); setConfirmDelete(null); setActionError(null) }}>Edit metadata</button><button type="button" className="secondary-button" disabled={writesDisabled} onClick={() => perform(record.status === 'registered' ? 'archive' : 'restore', record)}>{record.status === 'registered' ? 'Archive record' : 'Restore record'}</button><button type="button" ref={deleteButton} className="danger-button" disabled={writesDisabled} onClick={() => { setConfirmDelete(record); setActionError(null) }}>Delete record</button></div>}
          {confirmDelete && <div className="document-delete-confirm" role="group" aria-labelledby="delete-confirm-title" onKeyDown={(event) => { if (event.key === 'Escape' && !busy) { event.preventDefault(); setConfirmDelete(null); deleteButton.current?.focus() } }}><h4 id="delete-confirm-title">Delete this record permanently?</h4><p>“{confirmDelete.filename}” will be removed from this workspace. This action cannot be undone.</p><div className="form-actions"><button type="button" autoFocus className="secondary-button" disabled={busy} onClick={() => { setConfirmDelete(null); deleteButton.current?.focus() }}>Keep record</button><button type="button" className="danger-button" disabled={writesDisabled} onClick={() => perform('delete', confirmDelete)}>{busy ? 'Deleting…' : 'Confirm deletion'}</button></div></div>}
        </>}
      </aside>}
    </div>
  </section>
}
