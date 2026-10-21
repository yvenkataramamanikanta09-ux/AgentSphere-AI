import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, authApi, clearCsrf, obtainCsrf, onSessionExpired, request, requestBlob, uploadRequest } from './client'

const user = { id: 'user-id', email: 'person@example.com', display_name: 'Person', created_at: '2026-10-17T00:00:00Z' }
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

class TestUpload {
  static instances: TestUpload[] = []
  headers = new Headers()
  withCredentials = false
  timeout = 0
  status = 0
  responseText = ''
  responseURL = ''
  retryAfter: string | null = null
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  ontimeout: (() => void) | null = null
  onabort: (() => void) | null = null
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null }
  open = vi.fn()
  send = vi.fn()
  abort = vi.fn(() => this.onabort?.())
  setRequestHeader = (name: string, value: string) => { this.headers.set(name, value) }
  getResponseHeader = (name: string) => name === 'Retry-After' ? this.retryAfter : null
  constructor() { TestUpload.instances.push(this) }
  complete(body: unknown, status = 202) {
    this.status = status
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

async function prepareUpload(token = 'upload-token') {
  TestUpload.instances = []
  vi.stubGlobal('XMLHttpRequest', TestUpload)
  const fetcher = vi.fn().mockResolvedValue(json({ csrf_token: token }))
  vi.stubGlobal('fetch', fetcher)
  await obtainCsrf()
  const body = new FormData()
  body.append('file', new File(['Notes'], 'notes.txt', { type: 'text/plain' }))
  return { fetcher, body }
}

function latestUpload(): TestUpload {
  return TestUpload.instances[TestUpload.instances.length - 1]
}

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

describe('authenticated transfers', () => {
  it('lets the browser set multipart boundaries for fetch and XHR', async () => {
    const { body, fetcher } = await prepareUpload()
    fetcher.mockResolvedValueOnce(json({ accepted: true }))
    await request('/api/v1/uploads', { method: 'POST', body, headers: { 'Content-Type': 'application/json' } })
    const init = fetcher.mock.calls[1][1] as RequestInit
    expect(new Headers(init.headers).has('Content-Type')).toBe(false)
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('upload-token')
    expect(init.body).toBe(body)
    expect(init.redirect).toBe('error')

    const pending = uploadRequest('/api/v1/uploads', body, {
      headers: { 'Content-Type': 'multipart/form-data; boundary=wrong', 'Idempotency-Key': 'same-intent' },
    })
    const xhr = latestUpload()
    expect(xhr.headers.has('Content-Type')).toBe(false)
    expect(xhr.headers.get('X-CSRF-Token')).toBe('upload-token')
    expect(xhr.headers.get('Idempotency-Key')).toBe('same-intent')
    expect(xhr.withCredentials).toBe(true)
    expect(xhr.timeout).toBeGreaterThan(0)
    expect(xhr.open).toHaveBeenCalledWith('POST', '/api/v1/uploads', true)
    expect(xhr.send).toHaveBeenCalledOnce()
    expect(xhr.send).toHaveBeenCalledWith(body)
    xhr.complete({ accepted: true })
    await expect(pending).resolves.toEqual({ accepted: true })
  })

  it('reports actual transfer bytes and indeterminate progress, and removes progress handlers afterward', async () => {
    const { body } = await prepareUpload()
    const progress = vi.fn()
    const pending = uploadRequest('/api/v1/uploads', body, { onProgress: progress })
    const xhr = latestUpload()
    expect(progress).not.toHaveBeenCalled()
    xhr.upload.onprogress?.(new ProgressEvent('progress', { loaded: 37, total: 100, lengthComputable: true }))
    xhr.upload.onprogress?.(new ProgressEvent('progress', { loaded: 70, total: 0, lengthComputable: false }))
    xhr.upload.onprogress?.(new ProgressEvent('progress', { loaded: 100, total: 100, lengthComputable: true }))
    expect(progress.mock.calls).toEqual([[37], [null], [100]])
    xhr.complete({ accepted: true })
    await pending
    expect(xhr.upload.onprogress).toBeNull()
    expect(xhr.onload).toBeNull()
  })

  it('rejects external URLs before sending credentials or a security token', async () => {
    const { body, fetcher } = await prepareUpload()
    for (const path of ['https://other.example/api', '//other.example/api', 'https://person:secret@localhost:3000/api', 'http://[invalid']) {
      await expect(request(path)).rejects.toMatchObject({ status: 0 })
      await expect(requestBlob(path)).rejects.toMatchObject({ status: 0 })
      await expect(uploadRequest(path, body)).rejects.toMatchObject({ status: 0 })
    }
    expect(fetcher).toHaveBeenCalledOnce() // Only the same-origin CSRF request.
    expect(TestUpload.instances).toHaveLength(0)
  })

  it('never replays a rejected transfer and uses a newly obtained CSRF token on deliberate retry', async () => {
    const { body, fetcher } = await prepareUpload('old-token')
    const first = uploadRequest('/api/v1/uploads', body, { headers: { 'Idempotency-Key': 'retained-intent' } })
    latestUpload().complete({ detail: 'Missing or invalid token.' }, 403)
    await expect(first).rejects.toMatchObject({ status: 403 })
    expect(TestUpload.instances).toHaveLength(1)
    expect(fetcher).toHaveBeenCalledOnce()

    fetcher.mockResolvedValueOnce(json({ csrf_token: 'rotated-token' }))
    await obtainCsrf()
    const retry = uploadRequest('/api/v1/uploads', body, { headers: { 'Idempotency-Key': 'retained-intent' } })
    expect(latestUpload().headers.get('X-CSRF-Token')).toBe('rotated-token')
    expect(latestUpload().headers.get('Idempotency-Key')).toBe('retained-intent')
    latestUpload().complete({ replayed: true })
    await expect(retry).resolves.toEqual({ replayed: true })
    expect(TestUpload.instances).toHaveLength(2)
  })

  it('uses shared sanitized validation and retry-after errors for transfers', async () => {
    const { body } = await prepareUpload()
    const pending = uploadRequest('/api/v1/uploads', body)
    latestUpload().retryAfter = '2'
    latestUpload().complete({ detail: [{ loc: ['body', 'file'], msg: 'Invalid file.' }] }, 422)
    await expect(pending).rejects.toMatchObject({ status: 422, fieldErrors: { file: 'Invalid file.' }, retryAfter: '2' })

    const unavailable = uploadRequest('/api/v1/uploads', body)
    latestUpload().complete({ detail: 'internal-private-storage-path' }, 503)
    await expect(unavailable).rejects.toEqual(new ApiError(503, 'The service is temporarily unavailable. Please try again shortly.'))
  })

  it('expires the current session on upload 401 and clears its token without retrying', async () => {
    const { body } = await prepareUpload()
    const expired = vi.fn()
    const unsubscribe = onSessionExpired(expired)
    try {
      const pending = uploadRequest('/api/v1/uploads', body)
      latestUpload().complete({ detail: 'Authentication required.' }, 401)
      await expect(pending).rejects.toMatchObject({ status: 401 })
      expect(expired).toHaveBeenCalledOnce()
      await expect(uploadRequest('/api/v1/uploads', body)).rejects.toMatchObject({ status: 403 })
      expect(TestUpload.instances).toHaveLength(1)
    } finally { unsubscribe() }
  })

  it.each([200, 401, 403])('discards stale upload responses (%i) without expiring a newer login', async (status) => {
    const { body, fetcher } = await prepareUpload('anonymous-token')
    const expired = vi.fn()
    const unsubscribe = onSessionExpired(expired)
    try {
      const pending = uploadRequest('/api/v1/uploads', body)
      const stale = latestUpload()
      fetcher.mockResolvedValueOnce(json({ user, csrf_token: 'new-login-token' }))
      await authApi.login(user.email, 'passphrase')
      stale.complete({ old: true }, status)
      await expect(pending).rejects.toMatchObject({ status: 403, message: 'Your session changed. Please try again.' })
      expect(expired).not.toHaveBeenCalled()
      expect(await obtainCsrf()).toBe('new-login-token')
      expect(fetcher).toHaveBeenCalledTimes(2)
    } finally { unsubscribe() }
  })

  it('discards a stale fetch 401 without clearing or expiring a newer session', async () => {
    let finishOld: (response: Response) => void = () => {}
    const fetcher = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { finishOld = resolve }))
      .mockResolvedValueOnce(json({ csrf_token: 'new-token' }))
    vi.stubGlobal('fetch', fetcher)
    const expired = vi.fn()
    const unsubscribe = onSessionExpired(expired)
    try {
      const pending = request('/api/v1/workspaces')
      clearCsrf()
      await obtainCsrf()
      finishOld(json({ detail: 'Expired old session.' }, 401))
      await expect(pending).rejects.toMatchObject({ status: 403 })
      expect(expired).not.toHaveBeenCalled()
      expect(await obtainCsrf()).toBe('new-token')
    } finally { unsubscribe() }
  })

  it.each(['network', 'timeout'])('settles %s failures without another transfer', async (failure) => {
    const { body } = await prepareUpload()
    const pending = uploadRequest('/api/v1/uploads', body)
    const xhr = latestUpload()
    if (failure === 'network') xhr.onerror?.()
    else xhr.ontimeout?.()
    await expect(pending).rejects.toMatchObject({ status: 0 })
    expect(TestUpload.instances).toHaveLength(1)
    expect(xhr.onload).toBeNull()
    expect(xhr.upload.onprogress).toBeNull()
  })

  it('cancels a transfer and removes listeners so late completion cannot resolve it', async () => {
    const { body } = await prepareUpload()
    const controller = new AbortController()
    const progress = vi.fn()
    const pending = uploadRequest('/api/v1/uploads', body, { signal: controller.signal, onProgress: progress })
    const xhr = latestUpload()
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(xhr.abort).toHaveBeenCalledOnce()
    xhr.complete({ accepted: true })
    expect(progress).not.toHaveBeenCalled()
    expect(xhr.upload.onprogress).toBeNull()
    await expect(uploadRequest('/api/v1/uploads', body, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(TestUpload.instances).toHaveLength(1)
  })

  it('can cancel waiting for a shared CSRF request without cancelling another caller', async () => {
    let completeCsrf: (response: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { completeCsrf = resolve })))
    const controller = new AbortController()
    const cancelled = obtainCsrf(controller.signal)
    const otherCaller = obtainCsrf()
    controller.abort()
    await expect(cancelled).rejects.toMatchObject({ name: 'AbortError' })
    completeCsrf(json({ csrf_token: 'shared-token' }))
    await expect(otherCaller).resolves.toBe('shared-token')
    expect(await obtainCsrf()).toBe('shared-token')
  })

  it('discards successful JSON and blob bodies that finish after a session change', async () => {
    const response = json({ old: true })
    let completeJson: (value: unknown) => void = () => {}
    const jsonReader = vi.spyOn(response, 'json').mockImplementation(() => new Promise((resolve) => { completeJson = resolve }))
    const blobResponse = new Response('old original')
    let completeBlob: (value: Blob) => void = () => {}
    const blobReader = vi.spyOn(blobResponse, 'blob').mockImplementation(() => new Promise((resolve) => { completeBlob = resolve }))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response).mockResolvedValueOnce(blobResponse))
    const pendingJson = request('/api/v1/documents')
    const pendingBlob = requestBlob('/api/v1/documents/one/download')
    await vi.waitFor(() => { expect(jsonReader).toHaveBeenCalled(); expect(blobReader).toHaveBeenCalled() })
    clearCsrf()
    completeJson({ old: true })
    completeBlob(new Blob(['old original']))
    await expect(pendingJson).rejects.toMatchObject({ status: 403, message: 'Your session changed. Please try again.' })
    await expect(pendingBlob).rejects.toMatchObject({ status: 403, message: 'Your session changed. Please try again.' })
  })

  it('preserves cancellation while reading an HTTP error body', async () => {
    const response = json({ detail: 'private error' }, 503)
    vi.spyOn(response, 'json').mockRejectedValue(new DOMException('Cancelled', 'AbortError'))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
    await expect(requestBlob('/api/v1/documents/one/download')).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('downloads a blob with cookies and passes errors through normal expiry handling', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response('Original bytes', { headers: { 'Content-Type': 'text/plain' } }))
      .mockResolvedValueOnce(json({ detail: 'Not ready.' }, 409))
      .mockResolvedValueOnce(json({ detail: 'Expired.' }, 401))
    vi.stubGlobal('fetch', fetcher)
    const expired = vi.fn()
    const unsubscribe = onSessionExpired(expired)
    try {
      const blob = await requestBlob('/api/v1/documents/one/download')
      expect(await blob.text()).toBe('Original bytes')
      expect((fetcher.mock.calls[0][1] as RequestInit).credentials).toBe('same-origin')
      await expect(requestBlob('/api/v1/documents/one/download')).rejects.toMatchObject({ status: 409 })
      await expect(requestBlob('/api/v1/documents/one/download')).rejects.toMatchObject({ status: 401 })
      expect(expired).toHaveBeenCalledOnce()
    } finally { unsubscribe() }
  })
})
