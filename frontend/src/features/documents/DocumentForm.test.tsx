import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { documentApi } from '../../api/documents'
import type { DocumentRecord } from '../../api/documents'
import { DocumentForm } from './DocumentForm'

const record: DocumentRecord = {
  id: 'legacy-record', workspace_id: 'workspace-a', filename: 'CON .txt',
  content_type: 'text/plain', size_bytes: 2048, status: 'registered',
  created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z',
}

function mount(filename = record.filename, fileBacked = false) {
  const onSaved = vi.fn()
  render(<DocumentForm workspaceId={record.workspace_id} record={{ ...record, filename }} fileBacked={fileBacked} disabled={false} onSaved={onSaved} onCancel={vi.fn()} onPermissionFailure={vi.fn()} />)
  return { onSaved }
}

beforeEach(() => {
  vi.spyOn(documentApi, 'update').mockImplementation(async (_workspaceId, id, patch) => ({ ...record, ...patch, id }))
})
afterEach(() => { vi.restoreAllMocks() })

it.each(['CON .txt', 'COM¹.txt', 'LPT².txt', 'CONIN$.txt', 'CONOUT$.txt'])('preserves an unchanged server-provided legacy filename when editing size: %j', async (filename) => {
  const { onSaved } = mount(filename)
  await userEvent.clear(screen.getByLabelText('Declared size (bytes)'))
  await userEvent.type(screen.getByLabelText('Declared size (bytes)'), '4096')
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  expect(documentApi.update).toHaveBeenCalledWith(record.workspace_id, record.id, {
    filename, content_type: 'text/plain', size_bytes: 4096,
  })
})

it('allows renaming a legacy device filename to an ordinary filename', async () => {
  const { onSaved } = mount()
  await userEvent.clear(screen.getByLabelText('Filename'))
  await userEvent.type(screen.getByLabelText('Filename'), 'safe-notes.txt')
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  expect(documentApi.update).toHaveBeenCalledWith(record.workspace_id, record.id, {
    filename: 'safe-notes.txt', content_type: 'text/plain', size_bytes: record.size_bytes,
  })
})

it.each(['CON .txt', 'COM¹.txt', 'LPT².txt', 'CONIN$.txt', 'CONOUT$.txt'])('rejects renaming another record to a reserved device name: %j', async (filename) => {
  const { onSaved } = mount('ordinary.txt')
  await userEvent.clear(screen.getByLabelText('Filename'))
  await userEvent.type(screen.getByLabelText('Filename'), filename)
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Reserved device names are not allowed.')
  expect(documentApi.update).not.toHaveBeenCalled()
  expect(onSaved).not.toHaveBeenCalled()
})

it('keeps legacy file-backed measurements locked and sends only its unchanged filename', async () => {
  const { onSaved } = mount(record.filename, true)
  expect(screen.getByLabelText('Content type')).toBeDisabled()
  expect(screen.getByLabelText('Measured size (bytes)')).toHaveAttribute('readonly')
  await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  expect(documentApi.update).toHaveBeenCalledWith(record.workspace_id, record.id, { filename: record.filename })
})
