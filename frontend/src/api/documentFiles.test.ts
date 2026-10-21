import { beforeEach, expect, it, vi } from 'vitest'
import { ApiError, obtainCsrf, request, requestBlob, uploadRequest } from './client'
import { fileApi } from './documentFiles'

vi.mock('./client', async (importOriginal) => ({
  ...await importOriginal<typeof import('./client')>(),
  obtainCsrf: vi.fn(),
  request: vi.fn(),
  requestBlob: vi.fn(),
  uploadRequest: vi.fn(),
}))

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(obtainCsrf).mockResolvedValue('token')
})

it.each([
  ['proposal.PDF', '', 'application/pdf'],
  ['proposal.docx', 'application/octet-stream', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  ['notes.txt', 'text/plain;charset=utf-8', 'text/plain'],
  ['notes.md', 'application/octet-stream', 'text/markdown'],
  ['notes.markdown', 'text/plain', 'text/markdown'],
])('uploads %s with the canonical multipart MIME type', async (filename, browserType, canonicalType) => {
  const result = { document: { id: 'document-id' }, file: { processing_status: 'queued' }, replayed: false }
  vi.mocked(uploadRequest).mockResolvedValue(result)
  const signal = new AbortController().signal
  const progress = vi.fn()
  const file = new File(['Original file bytes'], filename, { type: browserType })
  await expect(fileApi.upload('workspace/id', file, 'caller-intent', { signal, onProgress: progress })).resolves.toEqual(result)
  expect(obtainCsrf).toHaveBeenCalledWith(signal)
  expect(uploadRequest).toHaveBeenCalledOnce()
  const [path, body, options] = vi.mocked(uploadRequest).mock.calls[0]
  expect(path).toBe('/api/v1/workspaces/workspace%2Fid/documents/uploads')
  expect(Array.from(body.keys())).toEqual(['file'])
  const uploaded = body.get('file') as File
  expect(uploaded.name).toBe(filename)
  expect(uploaded.type).toBe(canonicalType)
  expect(uploaded.size).toBe(file.size)
  expect(options).toEqual({ signal, onProgress: progress, headers: { 'Idempotency-Key': 'caller-intent' } })
})

it('rejects an unsupported extension before obtaining a token or sending bytes', async () => {
  await expect(fileApi.upload('workspace', new File(['content'], 'notes.exe', { type: 'text/plain' }), 'intent'))
    .rejects.toMatchObject({ status: 422, fieldErrors: { file: expect.any(String) } })
  expect(obtainCsrf).not.toHaveBeenCalled()
  expect(uploadRequest).not.toHaveBeenCalled()
})

it('never retries automatically, and preserves the key while obtaining CSRF for a deliberate retry', async () => {
  const file = new File(['notes'], 'notes.txt')
  vi.mocked(uploadRequest)
    .mockRejectedValueOnce(new ApiError(403, 'Security token expired.'))
    .mockResolvedValueOnce({ replayed: true })
  await expect(fileApi.upload('workspace', file, 'same-intent')).rejects.toMatchObject({ status: 403 })
  expect(uploadRequest).toHaveBeenCalledOnce()
  await expect(fileApi.upload('workspace', file, 'same-intent')).resolves.toEqual({ replayed: true })
  expect(obtainCsrf).toHaveBeenCalledTimes(2)
  expect(vi.mocked(uploadRequest).mock.calls.map((call) => new Headers(call[2]?.headers).get('Idempotency-Key')))
    .toEqual(['same-intent', 'same-intent'])
})

it('does not begin a transfer if obtaining CSRF is cancelled', async () => {
  vi.mocked(obtainCsrf).mockRejectedValue(new DOMException('Cancelled', 'AbortError'))
  await expect(fileApi.upload('workspace', new File(['notes'], 'notes.txt'), 'intent')).rejects.toMatchObject({ name: 'AbortError' })
  expect(uploadRequest).not.toHaveBeenCalled()
})

it('scopes metadata, paginated sections, and authenticated blob downloads to encoded identifiers', async () => {
  const signal = new AbortController().signal
  await fileApi.get('work/one', 'doc?one', signal)
  expect(request).toHaveBeenLastCalledWith('/api/v1/workspaces/work%2Fone/documents/doc%3Fone/file', { signal })
  await fileApi.sections('work/one', 'doc?one')
  expect(request).toHaveBeenLastCalledWith('/api/v1/workspaces/work%2Fone/documents/doc%3Fone/sections?limit=20&offset=0', { signal: undefined })
  await fileApi.sections('work/one', 'doc?one', 10, 20, signal)
  expect(request).toHaveBeenLastCalledWith('/api/v1/workspaces/work%2Fone/documents/doc%3Fone/sections?limit=10&offset=20', { signal })
  const original = new Blob(['original'])
  vi.mocked(requestBlob).mockResolvedValue(original)
  await expect(fileApi.download('work/one', 'doc?one', signal)).resolves.toBe(original)
  expect(requestBlob).toHaveBeenCalledWith('/api/v1/workspaces/work%2Fone/documents/doc%3Fone/download', { signal })
  expect(obtainCsrf).not.toHaveBeenCalled()
})
