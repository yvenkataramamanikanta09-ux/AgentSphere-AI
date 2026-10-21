import { render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router'
import App from '../../App'
import { clearCsrf } from '../../api/client'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

it('shows the login form after an unauthenticated session check', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'Authentication required.' }, 401)))
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: 'Sign in to your account' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /^Sign in/ }))
  expect(await screen.findByText('Enter a valid email address.')).toBeInTheDocument()
  expect(screen.getByText('Enter your password.')).toBeInTheDocument()
})

it('rejects mismatched registration passwords before calling the API', async () => {
  const fetcher = vi.fn().mockResolvedValue(json({ detail: 'Authentication required.' }, 401))
  vi.stubGlobal('fetch', fetcher)
  render(<MemoryRouter initialEntries={['/register']}><App /></MemoryRouter>)
  await screen.findByRole('heading', { name: 'Create your account' })
  await userEvent.type(screen.getByRole('textbox', { name: 'Full name' }), 'Researcher')
  await userEvent.type(screen.getByRole('textbox', { name: 'Email address' }), 'person@example.com')
  await userEvent.type(screen.getByLabelText('Password', { exact: true }), 'A long example passphrase')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'A different passphrase')
  await userEvent.click(screen.getByRole('button', { name: /^Create account/ }))
  expect(await screen.findByText('Passwords do not match.')).toBeInTheDocument()
  expect(fetcher).toHaveBeenCalledTimes(1) // Only /auth/me; no registration call.
})

it('submits login and replaces the anonymous CSRF token', async () => {
  const user = { id: 'user-id', email: 'person@example.com', display_name: 'Person', created_at: '2026-10-17T00:00:00Z' }
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ detail: 'Authentication required.' }, 401))
    .mockResolvedValueOnce(json({ csrf_token: 'anonymous-token' }))
    .mockResolvedValueOnce(json({ user, csrf_token: 'rotated-token', expires_at: '2026-10-17T12:00:00Z' }))
    .mockResolvedValueOnce(json({ items: [], limit: 20, offset: 0, has_more: false }))
  vi.stubGlobal('fetch', fetcher)
  render(<MemoryRouter initialEntries={['/login']}><App /></MemoryRouter>)
  await screen.findByRole('heading', { name: 'Sign in to your account' })
  await userEvent.type(screen.getByRole('textbox', { name: 'Email address' }), user.email)
  await userEvent.type(screen.getByLabelText('Password'), 'A long example passphrase')
  await userEvent.click(screen.getByRole('button', { name: /^Sign in/ }))
  expect(await screen.findByRole('heading', { name: 'Welcome, Person.' })).toBeInTheDocument()
  await screen.findByText('No workspaces yet')
  expect(fetcher).toHaveBeenCalledTimes(4)
})

it('restores an authenticated session and its CSRF token on reload', async () => {
  const user = { id: 'user-id', email: 'person@example.com', display_name: 'Person', created_at: '2026-10-17T00:00:00Z' }
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json(user))
    .mockResolvedValueOnce(json({ csrf_token: 'existing-session-token' }))
    .mockResolvedValueOnce(json({ items: [], limit: 20, offset: 0, has_more: false }))
  vi.stubGlobal('fetch', fetcher)
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: 'Welcome, Person.' })).toBeInTheDocument()
  await screen.findByText('No workspaces yet')
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/v1/auth/me', '/api/v1/auth/csrf', '/api/v1/workspaces?limit=20&offset=0'])
})

it('returns to sign-in after registration because registration does not log in', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ detail: 'Authentication required.' }, 401))
    .mockResolvedValueOnce(json({ csrf_token: 'anonymous-token' }))
    .mockResolvedValueOnce(json({ user: { id: 'user-id' }, workspace: { id: 'workspace-id' } }, 201))
  vi.stubGlobal('fetch', fetcher)
  render(<MemoryRouter initialEntries={['/register']}><App /></MemoryRouter>)
  await screen.findByRole('heading', { name: 'Create your account' })
  await userEvent.type(screen.getByRole('textbox', { name: 'Full name' }), 'Researcher')
  await userEvent.type(screen.getByRole('textbox', { name: 'Email address' }), 'person@example.com')
  await userEvent.type(screen.getByLabelText('Password', { exact: true }), 'A long example passphrase')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'A long example passphrase')
  await userEvent.click(screen.getByRole('button', { name: /^Create account/ }))
  expect(await screen.findByText('Your account is ready. Sign in to continue.')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Sign in to your account' })).toBeInTheDocument()
  expect(fetcher).toHaveBeenCalledTimes(3)
})

it('aborts obsolete StrictMode restoration without creating a session-change login error', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path === '/api/v1/auth/csrf') return json({ csrf_token: 'anonymous-token' })
    return json({ detail: 'Authentication required.' }, 401)
  })
  vi.stubGlobal('fetch', fetcher)
  render(<StrictMode><MemoryRouter initialEntries={['/login']}><App /></MemoryRouter></StrictMode>)
  await userEvent.type(await screen.findByLabelText('Email address'), 'person@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'An incorrect passphrase')
  await userEvent.click(screen.getByRole('button', { name: /^Sign in/ }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Email or password is incorrect')
  expect(screen.queryByText('Your session changed. Please try again.')).not.toBeInTheDocument()
  const restoration = fetcher.mock.calls.filter(([path]) => path === '/api/v1/auth/me')
  expect(restoration).toHaveLength(2)
})
