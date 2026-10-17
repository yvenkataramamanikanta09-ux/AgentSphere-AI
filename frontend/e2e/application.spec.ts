import { randomUUID } from 'node:crypto'
import { execFileSync, spawnSync } from 'node:child_process'
import AxeBuilder from '@axe-core/playwright'
import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

interface Fixture { users: Record<string, { id: string; email: string; display_name: string }>; workspace_id: string }
const fixture = JSON.parse(process.env.E2E_FIXTURE ?? '{}') as Fixture
const password = process.env.E2E_PASSWORD!
const collection = `/api/v1/workspaces/${fixture.workspace_id}/documents`

async function login(page: Page, role = 'owner') {
  await page.goto('/login')
  await page.getByLabel('Email address', { exact: true }).fill(fixture.users[role].email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: `Welcome, ${fixture.users[role].display_name}.` })).toBeVisible()
  if (role !== 'expiry') {
    const picker = page.getByRole('combobox', { name: 'Active workspace' })
    await expect(picker).not.toHaveValue('')
    await picker.selectOption(fixture.workspace_id)
    await expect(picker).toHaveValue(fixture.workspace_id)
    await expect(page.getByText('Metadata for Browser research. Files are not uploaded.', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Refresh records', exact: true })).toBeEnabled()
  }
}

async function addRecord(page: Page, filename: string, size = '2048') {
  await page.getByRole('button', { name: 'Add record', exact: true }).click()
  await page.getByLabel('Filename', { exact: true }).fill(filename)
  await page.getByLabel('Declared size (bytes)').fill(size)
  await page.getByRole('button', { name: 'Create record', exact: true }).click()
}

async function accessibility(page: Page) {
  const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  expect(result.violations.map(({ id, nodes }) => ({ id, targets: nodes.map((node) => node.target) }))).toEqual([])
}

test('register, validate, login, restore session, create/select workspace and logout', async ({ page, context }) => {
  const email = `journey-${randomUUID()}@example.com`
  await page.goto('/register')
  await page.getByRole('button', { name: 'Create account', exact: true }).click()
  await expect(page.getByLabel('Full name')).toBeFocused()
  await expect(page.getByText('Enter your name.', { exact: true })).toBeVisible()
  await page.getByLabel('Full name').fill('Journey Researcher')
  await page.getByLabel('Email address', { exact: true }).fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Create account', exact: true }).click()
  await expect(page.getByText('Your account is ready. Sign in to continue.')).toBeVisible()
  await page.getByLabel('Email address', { exact: true }).fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Welcome, Journey Researcher.' })).toBeVisible()
  await page.getByRole('button', { name: 'New workspace', exact: true }).click()
  await expect(page.getByLabel('Workspace name')).toBeFocused()
  await page.getByLabel('Workspace name').fill('Journey workspace')
  await page.getByRole('button', { name: 'Create workspace', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Journey workspace' })).toBeVisible()
  await expect(page.getByRole('combobox', { name: 'Active workspace' })).not.toHaveValue('')
  const selected = await page.getByRole('combobox', { name: 'Active workspace' }).inputValue()
  await page.reload()
  await expect(page.getByRole('combobox', { name: 'Active workspace' })).toHaveValue(selected)
  const cookies = await context.cookies()
  const session = cookies.find((cookie) => cookie.name === 'agentsphere_dev_session')
  expect(session?.httpOnly).toBe(true)
  expect(session?.sameSite).toBe('Lax')
  expect(await page.evaluate(() => ({ ...localStorage }))).toEqual({})
  await page.getByRole('combobox', { name: 'Active workspace' }).selectOption({ label: 'Personal workspace' })
  await expect(page.getByRole('heading', { name: 'No document records yet' })).toBeVisible()
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Sign in to your account' })).toBeVisible()
  expect((await page.request.get('/api/v1/auth/me')).status()).toBe(401)
})

for (const role of ['owner', 'editor', 'viewer']) {
  test(`${role} permissions are enforced by UI and real API`, async ({ page }) => {
    await login(page, role)
    await page.getByRole('button', { name: 'View seed-11.pdf', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'seed-11.pdf', exact: true })).toBeVisible()
    if (role === 'viewer') {
      await expect(page.getByRole('button', { name: 'Add record', exact: true })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Edit metadata', exact: true })).toHaveCount(0)
      const csrf = await (await page.request.get('/api/v1/auth/csrf')).json()
      const rejected = await page.request.post(collection, {
        headers: { Origin: 'http://127.0.0.1:5174', 'X-CSRF-Token': csrf.csrf_token },
        data: { filename: 'forbidden.pdf', content_type: 'application/pdf', size_bytes: 1 },
      })
      expect(rejected.status()).toBe(403)
    } else {
      const filename = `${role}-${randomUUID()}.pdf`
      await addRecord(page, filename)
      await expect(page.getByText(`“${filename}” created.`, { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Edit metadata', exact: true }).click()
      await page.getByLabel('Declared size (bytes)').fill('4096')
      await page.getByRole('button', { name: 'Save changes', exact: true }).click()
      await expect(page.getByText(`“${filename}” updated.`, { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Archive record', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Restore record', exact: true })).toBeVisible()
      await page.getByLabel('Status', { exact: true }).selectOption('archived')
      await expect(page.getByRole('button', { name: `View ${filename}`, exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Restore record', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Archive record', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Delete record', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Keep record', exact: true })).toBeFocused()
      await page.keyboard.press('Escape')
      await expect(page.getByRole('button', { name: 'Delete record', exact: true })).toBeFocused()
      await page.getByRole('button', { name: 'Delete record', exact: true }).click()
      await page.getByRole('button', { name: 'Confirm deletion', exact: true }).click()
      await expect(page.getByText(`“${filename}” deleted.`, { exact: true })).toBeVisible()
    }
  })
}

test('real pagination, status filtering, and wrong-workspace isolation', async ({ page }) => {
  await login(page)
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
  await page.getByLabel('Status', { exact: true }).selectOption('archived')
  await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'View seed-00.pdf', exact: true })).toBeVisible()
  const records = await (await page.request.get(collection)).json()
  await page.getByRole('combobox', { name: 'Active workspace' }).selectOption({ label: 'Personal workspace' })
  await expect(page.getByRole('heading', { name: 'No document records yet' })).toBeVisible()
  const ownWorkspace = await page.getByRole('combobox', { name: 'Active workspace' }).inputValue()
  expect((await page.request.get(`/api/v1/workspaces/${ownWorkspace}/documents/${records.items[0].id}`)).status()).toBe(404)
})

test('invalid metadata is rejected locally and server size errors retain the draft', async ({ page }) => {
  await login(page)
  await addRecord(page, '../bad.pdf', '0')
  await expect(page.getByLabel('Filename', { exact: true })).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByLabel('Declared size (bytes)')).toHaveAttribute('aria-invalid', 'true')
  await page.getByLabel('Filename', { exact: true }).fill('too-large.pdf')
  await page.getByLabel('Declared size (bytes)').fill('10000001')
  await page.getByRole('button', { name: 'Create record', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText(/configured limit/)
  await expect(page.getByLabel('Filename', { exact: true })).toHaveValue('too-large.pdf')
})

test('real CSRF rejection never replays a write; deliberate retry obtains a token', async ({ page }) => {
  await login(page)
  let writes = 0
  let bootstraps = 0
  page.on('request', (request) => { if (request.url().endsWith('/api/v1/auth/csrf')) bootstraps++ })
  await page.route(`**${collection}`, async (route) => {
    if (route.request().method() === 'POST') {
      writes++
      const headers = { ...route.request().headers() }
      if (writes === 1) headers['x-csrf-token'] = 'deliberately-invalid-test-token'
      await route.continue({ headers })
    } else await route.continue()
  })
  const filename = `csrf-${randomUUID()}.pdf`
  await addRecord(page, filename)
  await expect(page.getByRole('alert')).toHaveText(/Action denied/)
  expect(writes).toBe(1)
  await page.getByRole('button', { name: 'Create record', exact: true }).click()
  await expect(page.getByText(`“${filename}” created.`, { exact: true })).toBeVisible()
  expect(writes).toBe(2)
  expect(bootstraps).toBe(1)
})

test('expired server session clears protected UI and returns to login', async ({ page }) => {
  const unsafe = new URL(process.env.DATABASE_URL!)
  unsafe.searchParams.set('dbname', 'agentsphere')
  for (const mode of ['setup', 'expiry']) {
    const connection = new URL(unsafe)
    if (mode === 'setup') connection.pathname = '/agentsphere_test'
    const result = spawnSync(process.env.E2E_PYTHON!, ['scripts/run_e2e.py', ...(mode === 'expiry' ? ['--expire-user', fixture.users.expiry.id] : [])], {
      env: { ...process.env, [mode === 'setup' ? 'TEST_DATABASE_URL' : 'DATABASE_URL']: connection.toString() },
      encoding: 'utf8',
    })
    expect(result.status).toBe(1)
    expect(result.stderr).toContain('without query parameters')
  }
  await login(page, 'expiry')
  await expect(page.getByRole('heading', { name: 'Document library' })).toBeVisible()
  execFileSync(process.env.E2E_PYTHON!, ['scripts/run_e2e.py', '--expire-user', fixture.users.expiry.id], { stdio: 'pipe' })
  await page.getByRole('button', { name: 'Refresh records', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Sign in to your account' })).toBeVisible()
  await expect(page.getByText('Your session has expired. Sign in again to continue.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Document library' })).toHaveCount(0)
})

test('transport failures retain input and recover through explicit retry', async ({ page }) => {
  await login(page)
  await page.route(`**${collection}?*`, (route) => route.abort())
  await page.getByRole('button', { name: 'Refresh records', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText(/Cannot reach the server/)
  await page.unroute(`**${collection}?*`)
  await page.getByRole('button', { name: 'Retry documents', exact: true }).click()
  await expect(page.getByRole('button', { name: 'View seed-11.pdf', exact: true })).toBeVisible()
  await page.route(`**${collection}`, (route) => route.request().method() === 'POST' ? route.abort() : route.continue())
  await addRecord(page, 'offline.pdf')
  await expect(page.getByRole('alert')).toHaveText(/Cannot reach the server/)
  await expect(page.getByLabel('Filename', { exact: true })).toHaveValue('offline.pdf')
  await page.unroute(`**${collection}`)
  await page.getByRole('button', { name: 'Create record', exact: true }).click()
  await expect(page.getByText('“offline.pdf” created.', { exact: true })).toBeVisible()
})

test('keyboard navigation, form cancellation, and detail focus', async ({ page }) => {
  await login(page)
  await page.keyboard.press('Control+Home')
  await page.getByRole('link', { name: 'Skip to content', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('main')).toBeFocused()
  await page.getByRole('button', { name: 'New workspace', exact: true }).click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByRole('button', { name: 'New workspace', exact: true })).toBeFocused()
  await page.getByRole('button', { name: 'Add record', exact: true }).click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Add record', exact: true })).toBeFocused()
  await page.getByRole('button', { name: 'View seed-11.pdf', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Record details', exact: true })).toBeFocused()
})

test('incorrect login is rejected and a deliberate retry succeeds', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Email address', { exact: true }).fill(fixture.users.owner.email)
  await page.getByLabel('Password', { exact: true }).fill('Deliberately incorrect password')
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText(/Email or password is incorrect/)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Welcome, Browser Owner.' })).toBeVisible()
})

test('login and registration have no detectable WCAG A/AA violations', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Sign in to your account' })).toBeVisible()
  await accessibility(page)
  await page.getByRole('link', { name: 'Create an account', exact: true }).click()
  await accessibility(page)
})

for (const [width, height] of [[1440, 1000], [1024, 768], [768, 1024], [390, 844], [320, 740]]) {
  test(`responsive dashboard and forms at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await login(page, `layout-${width}`)
    await expect(page.getByRole('combobox', { name: 'Active workspace' })).toHaveAccessibleName('Active workspace')
    if (width <= 800) {
      await page.getByRole('button', { name: 'Menu', exact: true }).click()
      await page.getByRole('link', { name: 'Documents', exact: true }).click()
      await expect(page.locator('#document-library')).toBeFocused()
    }
    await page.getByRole('button', { name: 'View seed-11.pdf', exact: true }).click()
    await accessibility(page)
    await page.screenshot({ path: `.local/e2e/dashboard-${width}.png`, fullPage: true })
    if (width === 1440) await page.screenshot({ path: '.local/e2e/desktop-preview.png' })
    await page.getByRole('button', { name: 'Edit metadata', exact: true }).click()
    await accessibility(page)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: `.local/e2e/viewport-${width}.png`, fullPage: true })
  })
}
