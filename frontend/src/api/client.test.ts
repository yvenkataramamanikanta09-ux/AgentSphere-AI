import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, authApi, clearCsrf, obtainCsrf } from './client'

const user = { id: 'user-id', email: 'person@example.com', display_name: 'Person', created_at: '2026-10-17T00:00:00Z' }
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

describe('auth API client', () => {
  it('sends same-origin cookies and rotates CSRF after login', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ csrf_token: 'anonymous-token' }))
      .mockResolvedValueOnce(json({ user, csrf_token: 'logged-in-token', expires_at: '2026-10-17T12:00:00Z' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetcher)

    expect(await authApi.login(user.email, 'long passphrase')).toEqual(user)
    const loginOptions = fetcher.mock.calls[1][1] as RequestInit
    expect(loginOptions.credentials).toBe('same-origin')
    expect(loginOptions.cache).toBe('no-store')
    expect(new Headers(loginOptions.headers).get('X-CSRF-Token')).toBe('anonymous-token')
    expect(loginOptions.body).toBe(JSON.stringify({ email: user.email, password: 'long passphrase' }))

    await authApi.logout()
    expect(new Headers((fetcher.mock.calls[2][1] as RequestInit).headers).get('X-CSRF-Token')).toBe('logged-in-token')
  })

  it('reuses one CSRF request and restores a session through me', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json(user))
      .mockResolvedValueOnce(json({ csrf_token: 'restored-token' }))
    vi.stubGlobal('fetch', fetcher)
    expect(await authApi.me()).toEqual(user)
    expect(await Promise.all([obtainCsrf(), obtainCsrf()])).toEqual(['restored-token', 'restored-token'])
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect((fetcher.mock.calls[0][1] as RequestInit).credentials).toBe('same-origin')
  })

  it('registers without claiming login and reports server errors', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ csrf_token: 'anonymous-token' }))
      .mockResolvedValueOnce(json({ user, workspace: { id: 'work-id', name: 'Personal workspace', owner_user_id: user.id, role: 'owner', created_at: user.created_at } }, 201))
      .mockResolvedValueOnce(json({ detail: 'Account could not be registered.' }, 409))
    vi.stubGlobal('fetch', fetcher)
    const result = await authApi.register(user.email, 'long passphrase!', 'Person')
    expect(result.workspace.role).toBe('owner')
    expect(new Headers((fetcher.mock.calls[1][1] as RequestInit).headers).get('X-CSRF-Token')).toBe('anonymous-token')
    await expect(authApi.register(user.email, 'long passphrase!', 'Person')).rejects.toMatchObject({ status: 409 })
  })

  it('does not reflect unknown authentication response text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'internal-database-secret' }, 503)))
    await expect(authApi.me()).rejects.toEqual(new ApiError(503, 'The service is temporarily unavailable. Please try again shortly.'))
  })
})
