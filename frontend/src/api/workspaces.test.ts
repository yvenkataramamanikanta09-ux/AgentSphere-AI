import { beforeEach, expect, it, vi } from 'vitest'
import { authApi, clearCsrf, obtainCsrf, onSessionExpired } from './client'
import { workspaceApi } from './workspaces'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

it('uses workspace pagination and sends only the name on create', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ items: [], offset: 20, limit: 20, has_more: false }))
    .mockResolvedValueOnce(json({ csrf_token: 'create-token' }))
    .mockResolvedValueOnce(json({ id: 'new-workspace', name: 'Research', role: 'owner' }, 201))
  vi.stubGlobal('fetch', fetcher)
  await workspaceApi.list(20)
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/workspaces?limit=20&offset=20')
  await workspaceApi.create('Research')
  const options = fetcher.mock.calls[2][1] as RequestInit
  expect(options.body).toBe(JSON.stringify({ name: 'Research' }))
  expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('create-token')
  expect(options.credentials).toBe('same-origin')
})

it('does not replay a rejected write and obtains fresh CSRF for a manual retry', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ csrf_token: 'old-token' }))
    .mockResolvedValueOnce(json({ detail: 'Missing or invalid CSRF token.' }, 403))
    .mockResolvedValueOnce(json({ csrf_token: 'fresh-token' }))
    .mockResolvedValueOnce(json({ id: 'created', role: 'owner' }, 201))
  vi.stubGlobal('fetch', fetcher)
  await expect(workspaceApi.create('Research')).rejects.toMatchObject({ status: 403 })
  expect(fetcher).toHaveBeenCalledTimes(2)
  await workspaceApi.create('Research')
  expect(new Headers((fetcher.mock.calls[3][1] as RequestInit).headers).get('X-CSRF-Token')).toBe('fresh-token')
})

it('signals expiry for protected 401 responses but not incorrect login credentials', async () => {
  const expired = vi.fn()
  const unsubscribe = onSessionExpired(expired)
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(json({ detail: 'Authentication required.' }, 401))
    .mockResolvedValueOnce(json({ csrf_token: 'anonymous-token' }))
    .mockResolvedValueOnce(json({ detail: 'Invalid credentials.' }, 401)))
  try {
    await expect(workspaceApi.list()).rejects.toMatchObject({ status: 401 })
    expect(expired).toHaveBeenCalledTimes(1)
    await expect(authApi.login('person@example.com', 'wrong')).rejects.toMatchObject({ status: 401 })
    expect(expired).toHaveBeenCalledTimes(1)
  } finally { unsubscribe() }
})

it('discards a CSRF request that completes after session invalidation', async () => {
  let resolve: (response: Response) => void = () => {}
  vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>((done) => { resolve = done })))
  const pending = obtainCsrf()
  clearCsrf()
  resolve(json({ csrf_token: 'stale-token' }))
  await expect(pending).rejects.toMatchObject({ status: 403 })
})
