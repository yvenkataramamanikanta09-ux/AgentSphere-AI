import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { fileApi } from '../../api/documentFiles'
import type { DocumentFileMetadata, DocumentSectionPage, UploadResult } from '../../api/documentFiles'
import type { DocumentRecord } from '../../api/documents'
import { AuthContext } from '../auth/context'
import type { AuthContextValue } from '../auth/context'
import { DocumentFilePanel } from './DocumentFilePanel'
import { DocumentUpload } from './DocumentUpload'

vi.mock('../../api/documentFiles', () => ({ fileApi: { upload: vi.fn(), get: vi.fn(), sections: vi.fn(), download: vi.fn() } }))

const record: DocumentRecord = { id: 'doc-a', workspace_id: 'space-a', filename: 'notes.txt', content_type: 'text/plain', size_bytes: 5, status: 'registered', created_at: '2026-10-17T00:00:00Z', updated_at: '2026-10-17T00:00:00Z' }
const attachment: DocumentFileMetadata = { id: 'file-a', document_id: record.id, original_filename: 'original.txt', sha256: 'a'.repeat(64), uploaded_by: 'user-a', uploaded_at: record.created_at, processing_status: 'ready', processed_at: record.created_at, error_code: null, warning_code: null, download_available: true }
const result: UploadResult = { document: record, file: { ...attachment, processing_status: 'queued', download_available: false }, replayed: false }
const page: DocumentSectionPage = { document_id: record.id, items: [{ id: 'section-a', ordinal: 0, text: '<script>alert("unsafe")</script>\n[link](https://example.invalid)', page_number: null, heading: 'Source heading', line_start: 7, line_end: 9 }], limit: 20, offset: 0, has_more: true }
const auth: AuthContextValue = { status: 'authenticated', user: { id: 'user-a', display_name: 'Reader', email: 'reader@example.com', created_at: record.created_at }, error: null, login: vi.fn(), logout: vi.fn() }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function settle() { await act(async () => { await Promise.resolve() }) }
function mountUpload() {
  const uploaded = vi.fn(), cancel = vi.fn(), denied = vi.fn()
  const view = render(<DocumentUpload workspaceId="space-a" disabled={false} onUploaded={uploaded} onCancel={cancel} onPermissionFailure={denied} />)
  return { ...view, uploaded, cancel, denied }
}
function selectFile(file = new File(['hello'], 'notes.txt', { type: 'text/plain' })) {
  fireEvent.change(screen.getByLabelText('Choose document'), { target: { files: [file] } })
  return file
}
function mountPanel(file: DocumentFileMetadata = attachment) {
  vi.mocked(fileApi.get).mockResolvedValue(file)
  const changed = vi.fn(), denied = vi.fn()
  const view = render(<DocumentFilePanel workspaceId="space-a" record={record} onFileChange={changed} onPermissionFailure={denied} />)
  return { ...view, changed, denied }
}
beforeEach(() => { vi.resetAllMocks() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('validates selection immediately, focuses feedback, and never sends invalid files', async () => {
  mountUpload()
  selectFile(new File([], 'empty.txt'))
  expect(await screen.findByRole('alert')).toHaveTextContent('not empty')
  expect(screen.getByRole('alert')).toHaveFocus()
  expect(fileApi.upload).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: /Upload file|Retry same upload/ })).toBeDisabled()
})

it('guards concurrent submits during preflight, reports progress, and distinguishes received from ready', async () => {
  const pending = deferred<UploadResult>()
  vi.mocked(fileApi.upload).mockReturnValue(pending.promise)
  const view = mountUpload()
  const file = selectFile()
  fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  fireEvent.submit(screen.getByRole('button', { name: 'Uploading…' }).closest('form')!)
  expect(fileApi.upload).toHaveBeenCalledTimes(1)
  expect(fileApi.upload).toHaveBeenCalledWith('space-a', file, expect.any(String), expect.objectContaining({ signal: expect.any(AbortSignal) }))
  expect(screen.getByRole('progressbar')).not.toHaveAttribute('value')
  act(() => { vi.mocked(fileApi.upload).mock.calls[0][3]?.onProgress?.(100) })
  expect(screen.getByText(/Waiting for server confirmation/)).toBeInTheDocument()
  expect(view.uploaded).not.toHaveBeenCalled()
  await act(async () => pending.resolve(result))
  expect(screen.getByText(/File received/)).toBeInTheDocument()
  expect(view.uploaded).toHaveBeenCalledWith(result)
  expect(screen.queryByText('Ready')).not.toBeInTheDocument()
})

it('retains the same File and UUID for explicit retry and makes a new UUID only on a new selection', async () => {
  vi.mocked(fileApi.upload).mockRejectedValueOnce(new ApiError(0, 'offline')).mockRejectedValueOnce(new ApiError(503, 'unavailable')).mockResolvedValue(result)
  mountUpload()
  const selected = selectFile()
  fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('may have received')
  expect(fileApi.upload).toHaveBeenCalledTimes(1)
  const key = vi.mocked(fileApi.upload).mock.calls[0][2]
  expect(key).toMatch(/^[0-9a-f-]{36}$/)
  fireEvent.click(screen.getByRole('button', { name: 'Retry same upload' }))
  await settle()
  expect(vi.mocked(fileApi.upload).mock.calls[1].slice(0, 3)).toEqual(['space-a', selected, key])
  selectFile(new File(['other'], 'other.txt'))
  fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  await settle()
  expect(vi.mocked(fileApi.upload).mock.calls[2][2]).not.toBe(key)
})

it('aborts cancellation, warns of an uncertain server result, and retries with the retained key', async () => {
  const pending = deferred<UploadResult>()
  vi.mocked(fileApi.upload).mockReturnValueOnce(pending.promise).mockResolvedValue(result)
  const view = mountUpload()
  selectFile()
  fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  const initial = vi.mocked(fileApi.upload).mock.calls[0]
  fireEvent.click(screen.getByRole('button', { name: 'Cancel upload' }))
  expect(initial[3]?.signal?.aborted).toBe(true)
  expect(screen.getByRole('alert')).toHaveTextContent('server may already have received')
  await act(async () => pending.resolve(result))
  expect(view.uploaded).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry same upload' }))
  await settle()
  expect(vi.mocked(fileApi.upload).mock.calls[1][2]).toBe(initial[2])
  expect(view.uploaded).toHaveBeenCalledTimes(1)
})

it('retains selection on permission rejection and requests an access check without automatic retry', async () => {
  vi.mocked(fileApi.upload).mockRejectedValue(new ApiError(403, 'denied'))
  const view = mountUpload()
  selectFile()
  fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Action denied')
  expect(view.denied).toHaveBeenCalledTimes(1)
  expect(screen.getByText(/notes.txt/)).toBeInTheDocument()
  expect(fileApi.upload).toHaveBeenCalledTimes(1)
})

it('aborts upload and drops old selection after workspace change, logout, and unmount', async () => {
  const pending = deferred<UploadResult>()
  vi.mocked(fileApi.upload).mockReturnValue(pending.promise)
  const uploaded = vi.fn()
  const element = (workspaceId: string, session = auth) => <AuthContext.Provider value={session}><DocumentUpload workspaceId={workspaceId} disabled={false} onUploaded={uploaded} onCancel={vi.fn()} onPermissionFailure={vi.fn()} /></AuthContext.Provider>
  const view = render(element('space-a'))
  selectFile(); fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  const firstSignal = vi.mocked(fileApi.upload).mock.calls[0][3]?.signal
  view.rerender(element('space-b'))
  expect(firstSignal?.aborted).toBe(true)
  expect(screen.queryByText(/5 bytes/)).not.toBeInTheDocument()
  selectFile(); fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  const secondSignal = vi.mocked(fileApi.upload).mock.calls[1][3]?.signal
  view.rerender(element('space-b', { ...auth, status: 'guest', user: null }))
  expect(secondSignal?.aborted).toBe(true)
  view.rerender(element('space-b'))
  selectFile(); fireEvent.click(screen.getByRole('button', { name: 'Upload file' }))
  const thirdSignal = vi.mocked(fileApi.upload).mock.calls[2][3]?.signal
  view.unmount()
  expect(thirdSignal?.aborted).toBe(true)
  await act(async () => pending.resolve(result))
  expect(uploaded).not.toHaveBeenCalled()
})

it('treats missing attachments as metadata-only and schema failure as a safe manual retry', async () => {
  vi.mocked(fileApi.get).mockRejectedValueOnce(new ApiError(404, 'missing')).mockRejectedValueOnce(new ApiError(503, '/private/storage'))
  render(<DocumentFilePanel workspaceId="space-a" record={record} onPermissionFailure={vi.fn()} />)
  expect(await screen.findByText(/Metadata-only record/)).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh file status' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('schema')
  expect(screen.getByRole('alert')).not.toHaveTextContent('/private/storage')
  expect(fileApi.get).toHaveBeenCalledTimes(2)
})

it('keeps the refresh trigger focused while loading and never steals focus after a delayed response', async () => {
  const first = deferred<DocumentFileMetadata>(), second = deferred<DocumentFileMetadata>()
  vi.mocked(fileApi.get).mockResolvedValueOnce(attachment).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  render(<><button type="button">Other action</button><DocumentFilePanel workspaceId="space-a" record={record} onPermissionFailure={vi.fn()} /></>)
  await screen.findByText('Ready')
  const refresh = screen.getByRole('button', { name: 'Refresh file status' })
  refresh.focus()
  fireEvent.click(refresh)
  expect(screen.getByRole('button', { name: 'Refresh file status' })).toBe(refresh)
  expect(refresh).toHaveFocus()
  expect(refresh).toHaveAttribute('aria-disabled', 'true')
  fireEvent.click(refresh)
  expect(fileApi.get).toHaveBeenCalledTimes(2)
  await act(async () => first.resolve(attachment))
  expect(refresh).toHaveFocus()
  expect(refresh).toHaveAttribute('aria-disabled', 'false')
  fireEvent.click(refresh)
  const elsewhere = screen.getByRole('button', { name: 'Other action' })
  elsewhere.focus()
  await act(async () => second.resolve(attachment))
  expect(elsewhere).toHaveFocus()
  expect(fileApi.get).toHaveBeenCalledTimes(3)
})

it('polls queued and processing serially and stops permanently at ready', async () => {
  vi.useFakeTimers()
  const slow = deferred<DocumentFileMetadata>()
  vi.mocked(fileApi.get).mockResolvedValueOnce({ ...attachment, processing_status: 'queued', download_available: false }).mockReturnValueOnce(slow.promise).mockResolvedValue(attachment)
  render(<DocumentFilePanel workspaceId="space-a" record={record} onPermissionFailure={vi.fn()} />)
  await settle()
  expect(screen.getByText('Queued for processing')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000) })
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(fileApi.get).toHaveBeenCalledTimes(2)
  await act(async () => slow.resolve({ ...attachment, processing_status: 'processing', download_available: false }))
  expect(screen.getByText('Processing file')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000) })
  expect(screen.getByText('Ready')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(fileApi.get).toHaveBeenCalledTimes(3)
})

it('stops on polling errors, resumes only on manual refresh, and stops on failed processing', async () => {
  vi.useFakeTimers()
  vi.mocked(fileApi.get).mockResolvedValueOnce({ ...attachment, processing_status: 'queued', download_available: false }).mockRejectedValueOnce(new ApiError(503, 'down')).mockResolvedValueOnce({ ...attachment, processing_status: 'failed', error_code: 'encrypted_pdf', download_available: false })
  render(<DocumentFilePanel workspaceId="space-a" record={record} onPermissionFailure={vi.fn()} />)
  await settle()
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(screen.getByRole('alert')).toHaveTextContent('temporarily unavailable')
  expect(fileApi.get).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh file status' }))
  await settle()
  expect(screen.getByText(/password protected/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Download original' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: /Retry processing/ })).not.toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(fileApi.get).toHaveBeenCalledTimes(3)
})

it('aborts old file reads and polling on document/workspace change, logout, and unmount', async () => {
  vi.useFakeTimers()
  const pending = deferred<DocumentFileMetadata>()
  vi.mocked(fileApi.get).mockReturnValueOnce(pending.promise).mockResolvedValue({ ...attachment, processing_status: 'queued', download_available: false })
  const element = (workspaceId: string, documentRecord = record, session = auth) => <AuthContext.Provider value={session}><DocumentFilePanel workspaceId={workspaceId} record={documentRecord} onPermissionFailure={vi.fn()} /></AuthContext.Provider>
  const view = render(element('space-a'))
  const oldSignal = vi.mocked(fileApi.get).mock.calls[0][2]
  view.rerender(element('space-b', { ...record, id: 'doc-b', workspace_id: 'space-b' }))
  expect(oldSignal?.aborted).toBe(true)
  await settle()
  await act(async () => pending.resolve(attachment))
  expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  view.rerender(element('space-b', { ...record, id: 'doc-b', workspace_id: 'space-b' }, { ...auth, status: 'guest', user: null }))
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(fileApi.get).toHaveBeenCalledTimes(2)
  view.rerender(element('space-a'))
  await settle()
  view.unmount()
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(fileApi.get).toHaveBeenCalledTimes(3)
})

it('loads ready text in pages of 20, renders supplied references, and treats markup as plain text', async () => {
  vi.mocked(fileApi.sections).mockResolvedValueOnce(page).mockResolvedValueOnce({ ...page, items: [], offset: 20, has_more: false })
  mountPanel()
  await screen.findByText('Ready')
  expect(fileApi.sections).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Preview extracted text' }))
  expect(await screen.findByText('Lines 7–9')).toBeInTheDocument()
  expect(screen.getByText(/<script>/)).toBeInTheDocument()
  expect(document.querySelector('script')).toBeNull()
  expect(document.querySelector('.file-sections a')).toBeNull()
  expect(document.querySelector('.file-sections img')).toBeNull()
  expect(screen.queryByText(/Page 1 ·/)).not.toBeInTheDocument()
  expect(fileApi.sections).toHaveBeenLastCalledWith('space-a', 'doc-a', 20, 0, expect.any(AbortSignal))
  fireEvent.click(screen.getByRole('button', { name: 'Next sections' }))
  expect(await screen.findByText(/No extractable text was found on this page/)).toBeInTheDocument()
  expect(fileApi.sections).toHaveBeenLastCalledWith('space-a', 'doc-a', 20, 20, expect.any(AbortSignal))
  expect(screen.getByRole('button', { name: 'Next sections' })).toHaveAttribute('aria-disabled', 'true')
})

it('keeps next and previous section controls focused through delayed requests and announces the loaded page', async () => {
  const nextPage = deferred<DocumentSectionPage>(), previousPage = deferred<DocumentSectionPage>()
  vi.mocked(fileApi.sections).mockResolvedValueOnce(page).mockReturnValueOnce(nextPage.promise).mockReturnValueOnce(previousPage.promise)
  mountPanel()
  await screen.findByText('Ready')
  fireEvent.click(screen.getByRole('button', { name: 'Preview extracted text' }))
  await screen.findByText('Source heading')
  const next = screen.getByRole('button', { name: 'Next sections' })
  const previous = screen.getByRole('button', { name: 'Previous sections' })
  next.focus()
  fireEvent.click(next)
  expect(screen.getByRole('button', { name: 'Next sections' })).toBe(next)
  expect(next).toHaveFocus()
  expect(next).toHaveAttribute('aria-disabled', 'true')
  expect(previous).toHaveAttribute('aria-disabled', 'true')
  fireEvent.click(next)
  fireEvent.click(previous)
  expect(fileApi.sections).toHaveBeenCalledTimes(2)
  await act(async () => nextPage.resolve({ ...page, offset: 20, has_more: false }))
  expect(next).toHaveFocus()
  expect(screen.getByText('Page 2')).toHaveAttribute('role', 'status')
  expect(next).toHaveAttribute('aria-disabled', 'true')
  previous.focus()
  fireEvent.click(previous)
  expect(screen.getByRole('button', { name: 'Previous sections' })).toBe(previous)
  expect(previous).toHaveFocus()
  expect(previous).toHaveAttribute('aria-disabled', 'true')
  await act(async () => previousPage.resolve(page))
  expect(previous).toHaveFocus()
  expect(screen.getByText('Page 1')).toHaveAttribute('role', 'status')
  expect(previous).toHaveAttribute('aria-disabled', 'true')
  expect(fileApi.sections).toHaveBeenLastCalledWith('space-a', 'doc-a', 20, 0, expect.any(AbortSignal))
})

it('keeps previews unavailable until both ready and downloadable, and reports empty extraction warnings', async () => {
  mountPanel({ ...attachment, download_available: false, warning_code: 'no_extractable_text' })
  await screen.findByText('Ready')
  expect(screen.getByRole('button', { name: 'Preview extracted text' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Download original' })).toBeDisabled()
  expect(screen.getByText(/Scanned documents may need OCR/)).toBeInTheDocument()
  expect(fileApi.sections).not.toHaveBeenCalled()
})

it('downloads an authenticated Blob with a safe filename and revokes its temporary URL', async () => {
  vi.useFakeTimers()
  const create = vi.fn(() => 'blob:test-original'), revoke = vi.fn()
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = revoke })
  vi.mocked(fileApi.download).mockResolvedValue(new Blob(['hello']))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { expect(this.download).toBe('notes.txt'); expect(this.href).toBe('blob:test-original') })
  const view = mountPanel()
  await settle()
  fireEvent.click(screen.getByRole('button', { name: 'Download original' }))
  await settle()
  expect(fileApi.download).toHaveBeenCalledWith('space-a', 'doc-a', expect.any(AbortSignal))
  expect(click).toHaveBeenCalledTimes(1)
  expect(screen.getByText('Original file download started.')).toBeInTheDocument()
  expect(revoke).not.toHaveBeenCalled()
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
  expect(revoke).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Download original' }))
  await settle()
  view.unmount()
  expect(revoke).toHaveBeenCalledWith('blob:test-original')
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
  expect(revoke).toHaveBeenCalledTimes(2)
})

it('stops status requests and refreshes workspace access on a permission revocation', async () => {
  vi.useFakeTimers()
  vi.mocked(fileApi.get).mockResolvedValueOnce({ ...attachment, processing_status: 'processing', download_available: false }).mockRejectedValueOnce(new ApiError(403, 'denied'))
  const denied = vi.fn(), changed = vi.fn()
  render(<DocumentFilePanel workspaceId="space-a" record={record} onFileChange={changed} onPermissionFailure={denied} />)
  await settle()
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(fileApi.get).toHaveBeenCalledTimes(2)
  expect(denied).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('alert')).toHaveTextContent('Action denied')
  expect(changed).not.toHaveBeenCalledWith(null)
})

it('aborts extracted text after a workspace change and ignores its late response', async () => {
  const pending = deferred<DocumentSectionPage>()
  vi.mocked(fileApi.sections).mockReturnValue(pending.promise)
  const view = mountPanel()
  await screen.findByText('Ready')
  fireEvent.click(screen.getByRole('button', { name: 'Preview extracted text' }))
  await screen.findByText('Loading extracted text…')
  const signal = vi.mocked(fileApi.sections).mock.calls[0][4]
  view.rerender(<DocumentFilePanel workspaceId="space-b" record={{ ...record, id: 'doc-b', workspace_id: 'space-b' }} onPermissionFailure={vi.fn()} />)
  expect(signal?.aborted).toBe(true)
  await act(async () => pending.resolve(page))
  expect(screen.queryByText('Source heading')).not.toBeInTheDocument()
})

it('aborts downloads on logout before creating a browser URL', async () => {
  const pending = deferred<Blob>()
  const create = vi.fn(), revoke = vi.fn()
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = revoke })
  vi.mocked(fileApi.get).mockResolvedValue(attachment)
  vi.mocked(fileApi.download).mockReturnValue(pending.promise)
  const element = (session: AuthContextValue) => <AuthContext.Provider value={session}><DocumentFilePanel workspaceId="space-a" record={record} onPermissionFailure={vi.fn()} /></AuthContext.Provider>
  const view = render(element(auth))
  await screen.findByText('Ready')
  fireEvent.click(screen.getByRole('button', { name: 'Download original' }))
  const signal = vi.mocked(fileApi.download).mock.calls[0][2]
  view.rerender(element({ ...auth, status: 'guest', user: null }))
  expect(signal?.aborted).toBe(true)
  await act(async () => pending.resolve(new Blob(['private'])))
  expect(create).not.toHaveBeenCalled()
  expect(screen.queryByText('Original file download started.')).not.toBeInTheDocument()
})

it.each([401, 403, 409])('requires a metadata refresh after download authority error %s', async (status) => {
  vi.mocked(fileApi.download).mockRejectedValue(new ApiError(status, 'private exception'))
  const view = mountPanel()
  await screen.findByText('Ready')
  fireEvent.click(screen.getByRole('button', { name: 'Download original' }))
  expect(await screen.findByRole('alert')).not.toHaveTextContent('private exception')
  expect(screen.getByRole('button', { name: 'Download original' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Preview extracted text' })).toBeDisabled()
  expect(fileApi.download).toHaveBeenCalledTimes(1)
  expect(view.denied).toHaveBeenCalledTimes(status === 403 ? 1 : 0)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh file status' }))
  await screen.findByText('Ready')
  expect(screen.getByRole('button', { name: 'Download original' })).not.toBeDisabled()
})
