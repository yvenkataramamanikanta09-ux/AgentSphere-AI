/** The browser owns the HttpOnly cookie; this module only holds the CSRF token in memory. */
export interface User {
  id: string
  email: string
  display_name: string
  created_at: string
}

export interface Workspace {
  id: string
  name: string
  owner_user_id: string
  created_at: string
  role: 'owner' | 'editor' | 'viewer'
}

export interface RegistrationResult {
  user: User
  workspace: Workspace
}

export interface LoginResult {
  user: User
  csrf_token: string
  expires_at: string
}

interface ValidationIssue {
  loc?: Array<string | number>
  msg?: string
}

let csrfToken: string | null = null
let csrfRequest: Promise<string> | null = null
let csrfGeneration = 0
const sessionExpiryListeners = new Set<() => void>()

export function onSessionExpired(listener: () => void): () => void {
  sessionExpiryListeners.add(listener)
  return () => { sessionExpiryListeners.delete(listener) }
}

export class ApiError extends Error {
  readonly status: number
  readonly fieldErrors: Record<string, string>
  readonly retryAfter?: string

  constructor(
    status: number,
    message: string,
    fieldErrors: Record<string, string> = {},
    retryAfter?: string,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
    this.retryAfter = retryAfter
  }
}

function messageForStatus(status: number): string {
  switch (status) {
    case 401: return 'Email or password is incorrect, or your session has expired.'
    case 403: return 'Your session or security token has expired. Refresh the page and try again.'
    case 409: return 'This account already exists. Try signing in.'
    case 413: return 'This request is too large.'
    case 422: return 'Check the highlighted fields and try again.'
    case 429: return 'Too many attempts. Please wait before trying again.'
    case 503: return 'The service is temporarily unavailable. Please try again shortly.'
    default: return 'Something went wrong. Please try again.'
  }
}

const connectionMessage = 'Cannot reach the server. Check your connection and try again.'

function abortError(): DOMException {
  return new DOMException('The request was cancelled.', 'AbortError')
}

function isAbortError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'
}

function assertCurrentSession(generation: number): void {
  if (generation !== csrfGeneration) throw new ApiError(403, 'Your session changed. Please try again.')
}

function sameOriginUrl(path: string): URL {
  let url: URL
  try { url = new URL(path, window.location.href) }
  catch { throw new ApiError(0, 'API requests must use this site’s origin.') }
  if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new ApiError(0, 'API requests must use this site’s origin.')
  }
  return url
}

function requestContext(path: string, init: RequestInit) {
  const url = sameOriginUrl(path)
  if (init.signal?.aborted) throw abortError()
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  // The browser must generate the multipart boundary as well as its header.
  if (init.body instanceof FormData) headers.delete('Content-Type')
  else if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD'].includes(method)) {
    if (!csrfToken) throw new ApiError(403, 'Security token unavailable. Refresh the page and try again.')
    headers.set('X-CSRF-Token', csrfToken)
  }
  return { headers, generation: csrfGeneration, pathname: url.pathname }
}

function responseError(status: number, detail: unknown, retryAfter: string | null, pathname: string, generation: number): ApiError {
  assertCurrentSession(generation)
  const fields: Record<string, string> = {}
  if (Array.isArray(detail)) {
    for (const issue of detail as ValidationIssue[]) {
      if (!issue || typeof issue !== 'object') continue
      const field = Array.isArray(issue.loc)
        ? issue.loc.find((part) => typeof part === 'string' && part !== 'body')
        : undefined
      if (typeof field === 'string') fields[field] = typeof issue.msg === 'string' ? issue.msg : 'Invalid value.'
    }
  }
  if (status === 401 || status === 403) clearCsrf()
  if (status === 401 && pathname !== '/api/v1/auth/login') {
    sessionExpiryListeners.forEach((listener) => listener())
  }
  return new ApiError(status, status === 0 ? connectionMessage : messageForStatus(status), fields, retryAfter ?? undefined)
}

async function fetchResponse(path: string, init: RequestInit) {
  const context = requestContext(path, init)

  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: context.headers,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
    })
  } catch (error) {
    if (isAbortError(error)) throw error
    assertCurrentSession(context.generation)
    throw new ApiError(0, connectionMessage)
  }

  assertCurrentSession(context.generation)
  if (init.signal?.aborted) throw abortError()
  if (!response.ok) {
    let detail: unknown
    try { detail = (await response.json()).detail }
    catch (error) {
      if (isAbortError(error)) throw error
      // Other failures can be an empty or non-JSON error body.
    }
    if (init.signal?.aborted) throw abortError()
    throw responseError(response.status, detail, response.headers.get('Retry-After'), context.pathname, context.generation)
  }
  return { response, generation: context.generation }
}

async function readResponse<T>(response: Response, generation: number, read: () => Promise<T>): Promise<T> {
  try {
    const result = await read()
    assertCurrentSession(generation)
    return result
  } catch (error) {
    if (isAbortError(error)) throw error
    assertCurrentSession(generation)
    if (error instanceof ApiError) throw error
    throw new ApiError(response.status, 'The server returned an incomplete or invalid response. Please try again.')
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { response, generation } = await fetchResponse(path, init)
  assertCurrentSession(generation)
  if (response.status === 204) return undefined as T
  return readResponse(response, generation, () => response.json() as Promise<T>)
}

export async function requestBlob(path: string, init: RequestInit = {}): Promise<Blob> {
  const { response, generation } = await fetchResponse(path, init)
  return readResponse(response, generation, () => response.blob())
}

export interface UploadRequestOptions {
  headers?: HeadersInit
  signal?: AbortSignal
  onProgress?: (percent: number | null) => void
  timeoutMs?: number
}

/** One transfer only. The caller owns the upload intent and its idempotency key. */
export function uploadRequest<T>(path: string, body: FormData, options: UploadRequestOptions = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let context: ReturnType<typeof requestContext>
    try { context = requestContext(path, { method: 'POST', body, headers: options.headers, signal: options.signal }) }
    catch (error) { reject(error); return }

    let xhr: XMLHttpRequest
    try { xhr = new XMLHttpRequest() }
    catch { reject(new ApiError(0, connectionMessage)); return }
    let settled = false
    const cleanup = () => {
      options.signal?.removeEventListener('abort', cancel)
      xhr.onload = xhr.onerror = xhr.ontimeout = xhr.onabort = null
      xhr.upload.onprogress = null
    }
    const fail = (error: unknown) => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    }
    const cancel = () => {
      fail(abortError())
      xhr.abort()
    }
    const connectionFailure = (message: string) => {
      try { assertCurrentSession(context.generation); fail(new ApiError(0, message)) }
      catch (error) { fail(error) }
    }
    xhr.onload = () => {
      if (settled) return
      try {
        assertCurrentSession(context.generation)
        if (xhr.responseURL) sameOriginUrl(xhr.responseURL)
        let data: unknown
        try { data = JSON.parse(xhr.responseText) } catch { /* Errors may have no JSON body. */ }
        if (xhr.status < 200 || xhr.status >= 300) {
          const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : undefined
          throw responseError(xhr.status, detail, xhr.getResponseHeader('Retry-After'), context.pathname, context.generation)
        }
        if (data === undefined && xhr.status !== 204) {
          throw new ApiError(xhr.status, 'The server returned an incomplete or invalid response. Please try again.')
        }
        settled = true
        cleanup()
        resolve(data as T)
      } catch (error) { fail(error) }
    }
    xhr.onerror = () => connectionFailure(connectionMessage)
    xhr.ontimeout = () => connectionFailure('The request timed out. Please try again.')
    xhr.onabort = () => fail(abortError())
    xhr.upload.onprogress = (event) => {
      if (settled) return
      try { assertCurrentSession(context.generation) }
      catch (error) { fail(error); xhr.abort(); return }
      options.onProgress?.(event.lengthComputable && event.total > 0
        ? Math.min(100, Math.max(0, Math.round(event.loaded / event.total * 100)))
        : null)
    }
    options.signal?.addEventListener('abort', cancel, { once: true })
    try {
      xhr.open('POST', path, true)
      // Same-origin is enforced above; only the browser handles the HttpOnly cookie.
      xhr.withCredentials = true
      xhr.timeout = options.timeoutMs ?? 180_000
      context.headers.forEach((value, name) => xhr.setRequestHeader(name, value))
      if (options.signal?.aborted) cancel()
      else xhr.send(body)
    } catch { fail(new ApiError(0, connectionMessage)) }
  })
}

export function clearCsrf(): void {
  csrfToken = null
  csrfRequest = null
  csrfGeneration += 1
}

export function obtainCsrf(signal?: AbortSignal): Promise<string> {
  if (signal?.aborted) return Promise.reject(abortError())
  if (csrfToken) return Promise.resolve(csrfToken)
  if (!csrfRequest) {
    const generation = csrfGeneration
    const pending = request<{ csrf_token: string }>('/api/v1/auth/csrf')
      .then((data) => {
        if (generation !== csrfGeneration) throw new ApiError(403, 'Your session changed. Please try again.')
        csrfToken = data.csrf_token
        return data.csrf_token
      })
      .finally(() => { if (csrfRequest === pending) csrfRequest = null })
    csrfRequest = pending
  }
  const pending = csrfRequest
  if (!signal) return pending
  // Cancelling one upload must not cancel a CSRF request shared by other callers.
  return new Promise<string>((resolve, reject) => {
    const cancel = () => { signal.removeEventListener('abort', cancel); reject(abortError()) }
    signal.addEventListener('abort', cancel, { once: true })
    pending.then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel))
    if (signal.aborted) cancel()
  })
}

export const authApi = {
  me: (signal?: AbortSignal) => request<User>('/api/v1/auth/me', { signal }),
  async login(email: string, password: string): Promise<User> {
    await obtainCsrf()
    const generation = csrfGeneration
    const result = await request<LoginResult>('/api/v1/auth/login', {
      method: 'POST', body: JSON.stringify({ email, password }),
    })
    assertCurrentSession(generation)
    clearCsrf()
    csrfToken = result.csrf_token // The anonymous token is invalid after login.
    return result.user
  },
  async register(email: string, password: string, display_name: string): Promise<RegistrationResult> {
    await obtainCsrf()
    return request<RegistrationResult>('/api/v1/auth/register', {
      method: 'POST', body: JSON.stringify({ email, password, display_name }),
    })
  },
  async logout(): Promise<void> {
    await obtainCsrf()
    const generation = csrfGeneration
    await request<void>('/api/v1/auth/logout', { method: 'POST' })
    assertCurrentSession(generation)
    clearCsrf()
  },
}
