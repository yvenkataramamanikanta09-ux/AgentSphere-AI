import { beforeEach, expect, it, vi } from 'vitest'
import { clearCsrf } from './client'
import { documentApi } from './documents'

function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

it('scopes list and detail requests to the selected workspace with supported query fields', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(json({ items: [], has_more: false, limit: 10, offset: 10 })))
  vi.stubGlobal('fetch', fetcher)
  await documentApi.list('workspace-a', 10, 'archived')
  await documentApi.get('workspace-b', 'document-id')
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/workspaces/workspace-a/documents?limit=10&offset=10&status=archived')
  expect(fetcher.mock.calls[1][0]).toBe('/api/v1/workspaces/workspace-b/documents/document-id')
})

it('uses existing POST/PATCH/DELETE contracts, cookies and CSRF', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(json({ csrf_token: 'token' }))
    .mockResolvedValueOnce(json({ id: 'record' }, 201))
    .mockResolvedValueOnce(json({ id: 'record', status: 'archived' }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
  vi.stubGlobal('fetch', fetcher)
  const metadata = { filename: 'research.pdf', content_type: 'application/pdf', size_bytes: 2048 }
  await documentApi.create('workspace', metadata)
  await documentApi.update('workspace', 'record', { status: 'archived' })
  await documentApi.remove('workspace', 'record')
  expect((fetcher.mock.calls[1][1] as RequestInit).body).toBe(JSON.stringify(metadata))
  expect((fetcher.mock.calls[2][1] as RequestInit).body).toBe(JSON.stringify({ status: 'archived' }))
  for (const call of fetcher.mock.calls.slice(1)) {
    const options = call[1] as RequestInit
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('token')
    expect(options.credentials).toBe('same-origin')
  }
  expect((fetcher.mock.calls[3][1] as RequestInit).method).toBe('DELETE')
})
