import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router'
import App from '../../App'
import { clearCsrf } from '../../api/client'
import type { Workspace } from '../../api/client'
import type { DocumentRecord } from '../../api/documents'
import { fileApi } from '../../api/documentFiles'
import type { DocumentFileMetadata } from '../../api/documentFiles'
import { DocumentLibrary } from './DocumentLibrary'

const workspace: Workspace = { id: 'space-a', name: 'Research', role: 'owner', owner_user_id: 'user-id', created_at: '2026-10-17T00:00:00Z' }
const record: DocumentRecord = { id: 'record-a', workspace_id: workspace.id, filename: 'research.pdf', content_type: 'application/pdf', size_bytes: 2048, status: 'registered', created_at: workspace.created_at, updated_at: workspace.created_at }
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
type Override = (path: string, options?: RequestInit) => Response | Promise<Response> | undefined

function backend(initial = [record], override?: Override) {
  const records = [...initial]
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    const overridden = override?.(path, options)
    if (overridden) return overridden
    if (path === '/api/v1/auth/csrf') return json({ csrf_token: 'document-token' })
    const url = new URL(path, 'http://localhost')
    const method = options?.method ?? 'GET'
    if (url.pathname.endsWith('/documents')) {
      if (method === 'POST') {
        const created = { ...record, ...JSON.parse(options?.body as string), id: 'new-record' }
        records.unshift(created)
        return json(created, 201)
      }
      const status = url.searchParams.get('status')
      const filtered = records.filter((item) => !status || item.status === status)
      const offset = Number(url.searchParams.get('offset'))
      return json({ items: filtered.slice(offset, offset + 10), limit: 10, offset, has_more: filtered.length > offset + 10 })
    }
    const index = records.findIndex((item) => url.pathname.endsWith('/' + item.id))
    if (index === -1) return json({}, 404)
    if (method === 'PATCH') { records[index] = { ...records[index], ...JSON.parse(options?.body as string) }; return json(records[index]) }
    if (method === 'DELETE') { records.splice(index, 1); return new Response(null, { status: 204 }) }
    return json(records[index])
  })
  vi.stubGlobal('fetch', fetcher)
  return { fetcher, records }
}
function mount(role: Workspace['role'] = 'owner') {
  const denied = vi.fn()
  const view = render(<DocumentLibrary workspace={{ ...workspace, role }} onPermissionFailure={denied} />)
  return { ...view, denied }
}
beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

it.each(['owner', 'editor', 'viewer'] as const)('allows %s to read and shows only permitted actions', async (role) => {
  backend()
  mount(role)
  await userEvent.click(await screen.findByRole('button', { name: 'View research.pdf' }))
  expect(await screen.findByRole('heading', { name: 'research.pdf' })).toBeInTheDocument()
  const writer = role !== 'viewer'
  expect(!!screen.queryByRole('button', { name: 'Add record' })).toBe(writer)
  expect(!!screen.queryByRole('button', { name: 'Upload document' })).toBe(writer)
  expect(!!screen.queryByRole('button', { name: 'Edit metadata' })).toBe(writer)
  expect(!!screen.queryByRole('button', { name: 'Archive record' })).toBe(writer)
  expect(!!screen.queryByRole('button', { name: 'Delete record' })).toBe(writer)
})

it('creates metadata, fetches details, edits, archives and restores', async () => {
  const { records } = backend([])
  mount()
  await screen.findByText('No document records yet')
  await userEvent.click(screen.getByRole('button', { name: 'Add record' }))
  await userEvent.type(screen.getByLabelText('Filename'), 'notes.txt')
  expect(screen.getByLabelText('Content type')).toHaveValue('text/plain')
  await userEvent.type(screen.getByLabelText('Declared size (bytes)'), '1024')
  await userEvent.click(screen.getByRole('button', { name: 'Create record' }))
  expect(await screen.findByText('“notes.txt” created.')).toBeInTheDocument()
  await screen.findByRole('heading', { name: 'notes.txt' })
  await userEvent.click(screen.getByRole('button', { name: 'Edit metadata' }))
  await userEvent.clear(screen.getByLabelText('Declared size (bytes)'))
  await userEvent.type(screen.getByLabelText('Declared size (bytes)'), '4096')
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  expect(await screen.findByText('“notes.txt” updated.')).toBeInTheDocument()
  await userEvent.click(await screen.findByRole('button', { name: 'Archive record' }))
  expect(await screen.findByText('“notes.txt” archived.')).toBeInTheDocument()
  await userEvent.click(await screen.findByRole('button', { name: 'Restore record' }))
  expect(await screen.findByText('“notes.txt” restored.')).toBeInTheDocument()
  expect(records[0].size_bytes).toBe(4096)
  expect(records[0].status).toBe('registered')
})

it('requires explicit confirmation before deleting a record', async () => {
  const { fetcher } = backend()
  mount()
  await userEvent.click(await screen.findByRole('button', { name: 'View research.pdf' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Delete record' }))
  expect(screen.getByRole('group', { name: 'Delete this record permanently?' })).toBeInTheDocument()
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
  await userEvent.click(screen.getByRole('button', { name: 'Keep record' }))
  expect(screen.queryByRole('button', { name: 'Confirm deletion' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Delete record' }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirm deletion' }))
  expect(await screen.findByText('“research.pdf” deleted.')).toBeInTheDocument()
  expect(await screen.findByText('No document records yet')).toBeInTheDocument()
})

it('paginates and resets to the first page when filtering by status', async () => {
  const records = Array.from({ length: 11 }, (_, index) => ({ ...record, id: `id-${index}`, filename: `file-${index}.pdf`, status: index === 10 ? 'archived' as const : 'registered' as const }))
  const { fetcher } = backend(records)
  mount()
  await screen.findByRole('button', { name: 'View file-0.pdf' })
  await userEvent.click(screen.getByRole('button', { name: 'Next' }))
  expect(await screen.findByRole('button', { name: 'View file-10.pdf' })).toBeInTheDocument()
  expect(screen.getByText('Page 2')).toBeInTheDocument()
  await userEvent.selectOptions(screen.getByLabelText('Status'), 'archived')
  expect(await screen.findByRole('button', { name: 'View file-10.pdf' })).toBeInTheDocument()
  expect(screen.getByText('Page 1')).toBeInTheDocument()
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('offset=0&status=archived'))).toBe(true)
})

it('keeps input on server size rejection and does not claim success', async () => {
  backend([], (_, options) => options?.method === 'POST' ? json({}, 413) : undefined)
  mount()
  await userEvent.click(screen.getByRole('button', { name: 'Add record' }))
  await userEvent.type(screen.getByLabelText('Filename'), 'large.pdf')
  await userEvent.type(screen.getByLabelText('Declared size (bytes)'), '10000001')
  await userEvent.click(screen.getByRole('button', { name: 'Create record' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('configured limit')
  expect(screen.getByLabelText('Filename')).toHaveValue('large.pdf')
  expect(screen.queryByText('“large.pdf” created.')).not.toBeInTheDocument()
})

it('maps backend field validation errors to the form', async () => {
  backend([], (_, options) => options?.method === 'POST' ? json({ detail: [{ loc: ['body', 'filename'], msg: 'Filename rejected by server.' }] }, 422) : undefined)
  mount()
  await userEvent.click(screen.getByRole('button', { name: 'Add record' }))
  await userEvent.type(screen.getByLabelText('Filename'), 'notes.pdf')
  await userEvent.type(screen.getByLabelText('Declared size (bytes)'), '100')
  await userEvent.click(screen.getByRole('button', { name: 'Create record' }))
  expect(await screen.findByText('Filename rejected by server.')).toBeInTheDocument()
})

it('refreshes permission state on 403 and only retries creation deliberately', async () => {
  let reject = true
  const { fetcher } = backend([], (_, options) => options?.method === 'POST' && reject ? json({}, 403) : undefined)
  const { denied } = mount()
  await userEvent.click(screen.getByRole('button', { name: 'Add record' }))
  await userEvent.type(screen.getByLabelText('Filename'), 'notes.pdf')
  await userEvent.type(screen.getByLabelText('Declared size (bytes)'), '100')
  await userEvent.click(screen.getByRole('button', { name: 'Create record' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Action denied')
  expect(denied).toHaveBeenCalledTimes(1)
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  reject = false
  await userEvent.click(screen.getByRole('button', { name: 'Create record' }))
  expect(await screen.findByText('“notes.pdf” created.')).toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/v1/auth/csrf')).toHaveLength(2)
})

it('shows missing details without retaining a previous document', async () => {
  backend([record], (path) => path.endsWith('/record-a') ? json({}, 404) : undefined)
  mount('viewer')
  await userEvent.click(await screen.findByRole('button', { name: 'View research.pdf' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('record is unavailable')
  expect(screen.queryByRole('heading', { name: 'research.pdf' })).not.toBeInTheDocument()
})

it('preserves drafts and disables writing while workspace access is rechecked', async () => {
  backend([])
  const view = mount()
  await userEvent.click(screen.getByRole('button', { name: 'Add record' }))
  await userEvent.type(screen.getByLabelText('Filename'), 'draft.pdf')
  view.rerender(<DocumentLibrary workspace={workspace} checkingAccess onPermissionFailure={view.denied} />)
  expect(screen.getByLabelText('Filename')).toHaveValue('draft.pdf')
  expect(screen.getByRole('button', { name: 'Create record' })).toBeDisabled()
})

it('drops old document state and ignores late responses after a workspace switch', async () => {
  let complete: (response: Response) => void = () => {}
  backend([], (path) => {
    if (path.includes('/space-a/documents?')) return new Promise<Response>((resolve) => { complete = resolve })
    if (path.includes('/space-b/documents?')) return json({ items: [], limit: 10, offset: 0, has_more: false })
    return undefined
  })
  const view = render(<DocumentLibrary key="space-a" workspace={workspace} onPermissionFailure={() => {}} />)
  await screen.findByText('Loading document records…')
  view.rerender(<DocumentLibrary key="space-b" workspace={{ ...workspace, id: 'space-b', name: 'Other project' }} onPermissionFailure={() => {}} />)
  await screen.findByText('No document records yet')
  await act(async () => { complete(json({ items: [record], limit: 10, offset: 0, has_more: false })) })
  expect(screen.queryByRole('button', { name: 'View research.pdf' })).not.toBeInTheDocument()
})

it('returns to sign-in if the document request discovers an expired session', async () => {
  const user = { id: 'user-id', display_name: 'Reader', email: 'reader@example.com', created_at: workspace.created_at }
  backend([], (path) => {
    if (path === '/api/v1/auth/me') return json(user)
    if (path.startsWith('/api/v1/workspaces?')) return json({ items: [workspace], limit: 20, offset: 0, has_more: false })
    if (path === '/api/v1/workspaces/space-a') return json(workspace)
    if (path.includes('/documents?')) return json({}, 401)
    return undefined
  })
  render(<MemoryRouter><App /></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: 'Sign in to your account' })).toBeInTheDocument()
  expect(screen.getByText('Your session has expired. Sign in again to continue.')).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Document library' })).not.toBeInTheDocument()
})

it('recovers a failed list through retry', async () => {
  let failed = true
  backend([record], (path) => path.includes('/documents?') && failed ? json({}, 503) : undefined)
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable')
  failed = false
  await userEvent.click(screen.getByRole('button', { name: 'Retry documents' }))
  expect(await screen.findByRole('button', { name: 'View research.pdf' })).toBeInTheDocument()
})

const attachment: DocumentFileMetadata = {
  id: 'attachment-a', document_id: record.id, original_filename: record.filename,
  sha256: 'a'.repeat(64), uploaded_by: 'user-id', uploaded_at: workspace.created_at,
  processing_status: 'ready', processed_at: workspace.created_at,
  error_code: null, warning_code: null, download_available: true,
}

it('keeps file-backed measurements immutable and sends only a renamed filename', async () => {
  const { fetcher } = backend([record], (path) => path.endsWith('/file') ? json(attachment) : undefined)
  mount()
  await userEvent.click(await screen.findByRole('button', { name: 'View research.pdf' }))
  await screen.findByText('Ready')
  await userEvent.click(screen.getByRole('button', { name: 'Edit metadata' }))
  expect(screen.getByLabelText('Content type')).toBeDisabled()
  expect(screen.getByLabelText('Measured size (bytes)')).toHaveAttribute('readonly')
  await userEvent.clear(screen.getByLabelText('Filename'))
  await userEvent.type(screen.getByLabelText('Filename'), 'renamed.pdf')
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await screen.findByText('“renamed.pdf” updated.')
  const patch = fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH')
  expect(JSON.parse(patch![1]!.body as string)).toEqual({ filename: 'renamed.pdf' })
})

it('restores keyboard focus to the upload launcher after cancellation', async () => {
  backend([])
  mount()
  await userEvent.click(screen.getByRole('button', { name: 'Upload document' }))
  expect(screen.getByLabelText('Choose document')).toHaveFocus()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.getByRole('button', { name: 'Upload document' })).toHaveFocus()
  expect(screen.queryByLabelText('Choose document')).not.toBeInTheDocument()
})

it('selects a received upload, clears an archived filter, and preserves the metadata-only action', async () => {
  const created = { ...record, id: 'uploaded-a', filename: 'new.txt', content_type: 'text/plain', size_bytes: 5 }
  const file = { ...attachment, document_id: created.id, original_filename: created.filename, processing_status: 'queued' as const, processed_at: null, download_available: false }
  const { records } = backend([], (path) => path.endsWith('/file') ? json(file) : undefined)
  const upload = vi.spyOn(fileApi, 'upload').mockImplementation(async () => {
    records.push(created)
    return { document: created, file, replayed: false }
  })
  try {
    mount()
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'archived')
    await screen.findByText('No matching records')
    await userEvent.click(screen.getByRole('button', { name: 'Upload document' }))
    await userEvent.upload(screen.getByLabelText('Choose document'), new File(['hello'], 'new.txt', { type: 'text/plain' }))
    await userEvent.click(screen.getByRole('button', { name: 'Upload file' }))
    expect(await screen.findByText(/Upload received\./)).toBeInTheDocument()
    expect(screen.getByLabelText('Status')).toHaveValue('')
    expect(await screen.findByRole('heading', { name: 'new.txt' })).toBeInTheDocument()
    expect(await screen.findByText('Queued for processing')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add record' })).toBeEnabled()
    expect(upload).toHaveBeenCalledTimes(1)
  } finally { upload.mockRestore() }
})
