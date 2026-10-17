import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router'
import { beforeEach, expect, it, vi } from 'vitest'
import App from '../../App'
import { clearCsrf } from '../../api/client'
import type { Workspace } from '../../api/client'

const user = { id: 'user-id', email: 'person@example.com', display_name: 'Person', created_at: '2026-10-17T00:00:00Z' }
const research: Workspace = { id: 'research-id', name: 'Research', role: 'owner', owner_user_id: user.id, created_at: user.created_at }
const shared: Workspace = { ...research, id: 'shared-id', name: 'Shared project', role: 'viewer', owner_user_id: 'other-user' }
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}
function page(items: Workspace[], has_more = false, offset = 0) {
  return json({ items, limit: 20, offset, has_more })
}
function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}{location.search}</output>
}
function mount(route = '/') {
  render(<MemoryRouter initialEntries={[route]}><App /><LocationProbe /></MemoryRouter>)
}
function backend(workspaces: Workspace[] = [research, shared], override?: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetcher = vi.fn((path: string, init?: RequestInit) => {
    const custom = override?.(path, init)
    if (custom) return Promise.resolve(custom)
    if (path === '/api/v1/auth/me') return Promise.resolve(json(user))
    if (path === '/api/v1/auth/csrf') return Promise.resolve(json({ csrf_token: 'session-token' }))
    if (path.startsWith('/api/v1/workspaces?')) return Promise.resolve(page(workspaces))
    const match = workspaces.find((workspace) => path === `/api/v1/workspaces/${workspace.id}`)
    if (match) return Promise.resolve(json(match))
    throw new Error(`Unexpected test request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

beforeEach(() => { clearCsrf(); vi.unstubAllGlobals() })

it('shows loading while the workspace list is pending', async () => {
  let complete: (value: Response) => void = () => {}
  backend([], (path) => path.startsWith('/api/v1/workspaces?') ? new Promise<Response>((resolve) => { complete = resolve }) : undefined)
  mount()
  expect(await screen.findByText('Loading workspaces…')).toBeInTheDocument()
  await act(async () => { complete(page([])) })
  expect(await screen.findByText('No workspaces yet')).toBeInTheDocument()
})

it.each([
  ['owner', 'Ownership & access'], ['editor', 'Editor permissions'], ['viewer', 'Viewer permissions'],
] as const)('displays real profile data and %s navigation', async (role, label) => {
  backend([{ ...research, role }])
  mount()
  expect(await screen.findByRole('link', { name: label })).toBeInTheDocument()
  expect(screen.getAllByText(user.email).length).toBeGreaterThan(0)
  expect(screen.getByRole('button', { name: 'Select Research' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getAllByText(role).length).toBeGreaterThan(0)
  expect(screen.queryByRole('link', { name: 'Documents' })).not.toBeInTheDocument()
})

it('changes selection and uses the selected workspace role and URL', async () => {
  backend()
  mount()
  await screen.findByRole('link', { name: 'Ownership & access' })
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Active workspace' }), shared.id)
  expect(await screen.findByRole('link', { name: 'Viewer permissions' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Ownership & access' })).not.toBeInTheDocument()
  expect(screen.getByTestId('location')).toHaveTextContent(`workspace=${shared.id}`)
})

it('restores a bookmarked selection even when it is outside the loaded page', async () => {
  const fetcher = backend([research], (path) => path === `/api/v1/workspaces/${shared.id}` ? json(shared) : undefined)
  mount(`/?workspace=${shared.id}`)
  expect(await screen.findByRole('link', { name: 'Viewer permissions' })).toBeInTheDocument()
  expect(fetcher.mock.calls.some(([path]) => path === `/api/v1/workspaces/${shared.id}`)).toBe(true)
  expect(screen.getByRole('combobox', { name: 'Active workspace' })).toHaveValue(shared.id)
})

it('rejects inaccessible bookmarked workspaces without showing old role controls', async () => {
  backend([research], (path) => path.endsWith('/private-id') ? json({ detail: 'Workspace not found.' }, 404) : undefined)
  mount('/?workspace=private-id')
  expect(await screen.findByText('Workspace unavailable')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Ownership & access' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Select Research' }))
  expect(await screen.findByRole('link', { name: 'Ownership & access' })).toBeInTheDocument()
})

it('creates a workspace as a viewer and selects the returned owner workspace', async () => {
  const created: Workspace = { ...research, id: 'new-id', name: 'Product research' }
  let didCreate = false
  const fetcher = backend([shared], (path, init) => {
    if (path === '/api/v1/workspaces' && init?.method === 'POST') { didCreate = true; return json(created, 201) }
    if (didCreate && path.startsWith('/api/v1/workspaces?')) return page([created, shared])
    if (path === `/api/v1/workspaces/${created.id}`) return json(created)
    return undefined
  })
  mount()
  await screen.findByRole('link', { name: 'Viewer permissions' })
  await userEvent.click(screen.getByRole('button', { name: 'New workspace' }))
  await userEvent.type(screen.getByRole('textbox', { name: 'Workspace name' }), '  Product research  ')
  await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  expect(await screen.findByText('Workspace “Product research” created. You’re its owner.')).toBeInTheDocument()
  expect(await screen.findByRole('link', { name: 'Ownership & access' })).toBeInTheDocument()
  const call = fetcher.mock.calls.find(([path, init]) => path === '/api/v1/workspaces' && init?.method === 'POST')
  expect(call?.[1]?.body).toBe(JSON.stringify({ name: 'Product research' }))
  expect(new Headers(call?.[1]?.headers).get('X-CSRF-Token')).toBe('session-token')
})

it('validates workspace name before sending a request', async () => {
  const fetcher = backend([])
  mount()
  await screen.findByText('No workspaces yet')
  await userEvent.click(screen.getByRole('button', { name: 'New workspace' }))
  await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  expect(await screen.findByText('Enter a workspace name.')).toBeInTheDocument()
  expect(fetcher.mock.calls.some(([path]) => path === '/api/v1/workspaces')).toBe(false)
})

it('shows a list error and recovers through explicit retry', async () => {
  let fail = true
  backend([research], (path) => path.startsWith('/api/v1/workspaces?') && fail ? json({}, 503) : undefined)
  mount()
  expect(await screen.findByText('The service is temporarily unavailable. Please try again shortly.')).toBeInTheDocument()
  fail = false
  await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByRole('button', { name: 'Select Research' })).toBeInTheDocument()
})

it('clears the dashboard and returns to login on an expired session', async () => {
  backend([research], (path) => path.startsWith('/api/v1/workspaces?') ? json({}, 401) : undefined)
  mount()
  expect(await screen.findByRole('heading', { name: 'Sign in to your account' })).toBeInTheDocument()
  expect(screen.getByText('Your session has expired. Sign in again to continue.')).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Your workspaces' })).not.toBeInTheDocument()
})

it('loads the next workspace page without duplicating existing cards', async () => {
  const first = Array.from({ length: 20 }, (_, index) => ({ ...research, id: `space-${index}`, name: `Space ${index}` }))
  const fetcher = backend(first, (path) => {
    if (path.endsWith('offset=0')) return page(first, true)
    if (path.endsWith('offset=20')) return page([shared], false, 20)
    return undefined
  })
  mount()
  await screen.findByRole('button', { name: 'Load more workspaces' })
  await userEvent.click(screen.getByRole('button', { name: 'Load more workspaces' }))
  expect(await screen.findByRole('button', { name: 'Select Shared project' })).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: /^Select / })).toHaveLength(21)
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('offset=20'))).toBe(true)
})

it('can expand and close mobile navigation without navigating away', async () => {
  backend([])
  mount()
  await screen.findByText('No workspaces yet')
  const menu = screen.getByRole('button', { name: 'Menu' })
  await userEvent.click(menu)
  expect(screen.getByRole('button', { name: 'Close menu' })).toHaveAttribute('aria-expanded', 'true')
  await userEvent.click(screen.getByRole('link', { name: 'Workspaces' }))
  expect(screen.getByRole('button', { name: 'Menu' })).toHaveAttribute('aria-expanded', 'false')
})

it('never applies a late selection response over a newer workspace', async () => {
  let complete: (value: Response) => void = () => {}
  backend([research, shared], (path) => path === `/api/v1/workspaces/${shared.id}` ? new Promise<Response>((resolve) => { complete = resolve }) : undefined)
  mount()
  await screen.findByRole('link', { name: 'Ownership & access' })
  await userEvent.click(screen.getByRole('button', { name: 'Select Shared project' }))
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Ownership & access' })).not.toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: 'Select Research' }))
  await screen.findByRole('link', { name: 'Ownership & access' })
  await act(async () => { complete(json(shared)) })
  expect(screen.queryByRole('link', { name: 'Viewer permissions' })).not.toBeInTheDocument()
})

it('keeps a failed creation editable and never announces false success', async () => {
  backend([], (path, init) => path === '/api/v1/workspaces' && init?.method === 'POST' ? json({}, 503) : undefined)
  mount()
  await screen.findByText('No workspaces yet')
  await userEvent.click(screen.getByRole('button', { name: 'New workspace' }))
  await userEvent.type(screen.getByRole('textbox', { name: 'Workspace name' }), 'Research')
  await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The service is temporarily unavailable.')
  expect(screen.getByRole('textbox', { name: 'Workspace name' })).toHaveValue('Research')
  expect(screen.queryByText(/created. You’re its owner/)).not.toBeInTheDocument()
})

it('removes selected permissions after a refreshed access check returns 404', async () => {
  let removed = false
  backend([research], (path) => removed && path === `/api/v1/workspaces/${research.id}` ? json({}, 404) : undefined)
  mount()
  await screen.findByRole('link', { name: 'Ownership & access' })
  removed = true
  await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(await screen.findByText('Workspace unavailable')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Ownership & access' })).not.toBeInTheDocument()
})
