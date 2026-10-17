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

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD'].includes(method)) {
    if (!csrfToken) throw new ApiError(403, 'Security token unavailable. Refresh the page and try again.')
    headers.set('X-CSRF-Token', csrfToken)
  }

  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError(0, 'Cannot reach the server. Check your connection and try again.')
  }

  if (!response.ok) {
    let detail: unknown
    try { detail = (await response.json()).detail } catch { /* No JSON body. */ }
    const fields: Record<string, string> = {}
    if (Array.isArray(detail)) {
      for (const issue of detail as ValidationIssue[]) {
        const field = issue.loc?.find((part) => typeof part === 'string' && part !== 'body')
        if (typeof field === 'string') fields[field] = issue.msg ?? 'Invalid value.'
      }
    }
    if (response.status === 401 || response.status === 403) clearCsrf()
    if (response.status === 401 && path !== '/api/v1/auth/login') {
      sessionExpiryListeners.forEach((listener) => listener())
    }
    throw new ApiError(response.status, messageForStatus(response.status), fields, response.headers.get('Retry-After') ?? undefined)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export function clearCsrf(): void {
  csrfToken = null
  csrfRequest = null
  csrfGeneration += 1
}

export function obtainCsrf(): Promise<string> {
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
  return csrfRequest
}

export const authApi = {
  me: () => request<User>('/api/v1/auth/me'),
  async login(email: string, password: string): Promise<User> {
    await obtainCsrf()
    const result = await request<LoginResult>('/api/v1/auth/login', {
      method: 'POST', body: JSON.stringify({ email, password }),
    })
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
    await request<void>('/api/v1/auth/logout', { method: 'POST' })
    clearCsrf()
  },
}
