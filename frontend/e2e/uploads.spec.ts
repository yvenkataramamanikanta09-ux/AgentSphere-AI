import { execFileSync, spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { basename } from 'node:path'
import AxeBuilder from '@axe-core/playwright'
import { test, expect } from '@playwright/test'
import type { Page, Request } from '@playwright/test'

if (process.env.E2E_ISOLATED !== '1') {
  throw new Error('Use npm run test:e2e so uploads use disposable private storage and PostgreSQL.')
}

interface Fixture {
  users: Record<string, { id: string; email: string; display_name: string }>
  workspace_id: string
}
interface Upload {
  document: { id: string; filename: string; workspace_id: string }
  file: { processing_status: string; download_available: boolean }
  replayed: boolean
}
interface Snapshot {
  document_count: number
  file_count: number
  section_count: number
  extract_job_count: number
  private_original_exists: boolean
  cleanup_state: string | null
}

const origin = 'http://127.0.0.1:5174'
const fixture = JSON.parse(process.env.E2E_FIXTURE ?? '{}') as Fixture
const files = JSON.parse(process.env.E2E_UPLOAD_FIXTURES ?? '{}') as Record<string, string>
const password = process.env.E2E_PASSWORD!
const collection = `/api/v1/workspaces/${fixture.workspace_id}/documents`
const uploads = `${collection}/uploads`
const mime: Record<string, string> = {
  txt: 'text/plain', md: 'text/markdown', pdf: 'application/pdf', malformed: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}

function control(action: 'pause' | 'resume') {
  execFileSync(process.env.E2E_PYTHON!, ['scripts/run_e2e.py', '--worker-control', action], { stdio: 'pipe' })
}

function inspect(id: string): Snapshot {
  return JSON.parse(execFileSync(process.env.E2E_PYTHON!, ['scripts/run_e2e.py', '--inspect-upload', id], {
    encoding: 'utf8', stdio: 'pipe',
  })) as Snapshot
}

async function login(page: Page, role = 'owner', shared = true) {
  await page.goto('/login')
  await page.getByLabel('Email address', { exact: true }).fill(fixture.users[role].email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('heading', { name: `Welcome, ${fixture.users[role].display_name}.` })).toBeVisible()
  const picker = page.getByRole('combobox', { name: 'Active workspace' })
  await expect(picker).not.toHaveValue('')
  if (shared) {
    await picker.selectOption(fixture.workspace_id)
    await expect(picker).toHaveValue(fixture.workspace_id)
  }
  await expect(page.getByRole('button', { name: 'Refresh records', exact: true })).toBeEnabled()
}

async function choose(page: Page, kind: string, name = `${randomUUID()}-${basename(files[kind])}`) {
  await page.getByRole('button', { name: 'Upload document', exact: true }).click()
  await page.getByLabel('Choose document', { exact: true }).setInputFiles({
    name, mimeType: mime[kind], buffer: readFileSync(files[kind]),
  })
  return name
}

async function submit(page: Page, endpoint = uploads): Promise<Upload> {
  const accepted = page.waitForResponse((response) => response.url().endsWith(endpoint)
    && response.request().method() === 'POST')
  await page.getByRole('button', { name: /^(Upload file|Retry same upload)$/ }).click()
  const response = await accepted
  expect(response.status()).toBe(202)
  return await response.json() as Upload
}

async function ready(page: Page, body: Upload) {
  await expect(page.getByRole('heading', { name: body.document.filename, exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'File processing', exact: true })).toContainText('Ready', { timeout: 20_000 })
  await expect(page.getByRole('button', { name: 'Preview extracted text', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Download original', exact: true })).toBeEnabled()
}

async function downloadEquals(page: Page, kind: string, name: string) {
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download original', exact: true }).click()
  const download = await downloaded
  expect(download.suggestedFilename()).toBe(name)
  const path = await download.path()
  expect(path).not.toBeNull()
  expect(readFileSync(path!).equals(readFileSync(files[kind]))).toBe(true)
}

for (const [kind, evidence] of [
  ['txt', 'Synthetic source paragraph 01.'],
  ['md', 'A generated Markdown paragraph.'],
  ['pdf', 'Synthetic PDF evidence'],
  ['docx', 'A generated DOCX paragraph.'],
]) {
  test(`real ${kind.toUpperCase()} UI upload is processed, previewed and downloaded byte-for-byte`, async ({ page }) => {
    await login(page)
    const name = await choose(page, kind)
    const body = await submit(page)
    await ready(page, body)
    if (kind === 'txt') {
      const refresh = page.getByRole('button', { name: 'Refresh file status', exact: true })
      await refresh.focus()
      await refresh.press('Enter')
      await expect(refresh).toHaveAttribute('aria-disabled', 'false')
      await expect(refresh).toBeFocused()
    }
    await page.getByRole('button', { name: 'Preview extracted text', exact: true }).click()
    await expect(page.getByText(evidence, { exact: true })).toBeVisible()
    const sections = await page.request.get(`${collection}/${body.document.id}/sections?limit=1&offset=0`)
    expect(sections.status()).toBe(200)
    const first = (await sections.json()).items[0]
    if (kind === 'pdf') expect(first.page_number).toBe(1)
    else {
      expect(first.page_number).toBeNull()
      if (kind === 'docx' || kind === 'md') expect(first.heading).toBeTruthy()
      else expect(first.line_start).toBe(1)
    }
    if (kind === 'txt') {
      const next = page.getByRole('button', { name: 'Next sections', exact: true })
      const previous = page.getByRole('button', { name: 'Previous sections', exact: true })
      await next.focus()
      await next.press('Enter')
      await expect(page.getByText('Synthetic source paragraph 01.', { exact: true })).toHaveCount(0)
      await expect(page.getByText('Synthetic source paragraph 21.', { exact: true })).toBeVisible()
      await expect(next).toBeFocused()
      await expect(page.getByRole('status').filter({ hasText: /^Page 2$/ })).toBeVisible()
      await expect(previous).toBeEnabled()
      await page.getByRole('button', { name: 'Next sections', exact: true }).click()
      await expect(page.getByText('Synthetic source paragraph 41.', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Next sections', exact: true }).click()
      await expect(page.getByText('Synthetic source paragraph 65.', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Next sections', exact: true })).toBeDisabled()
      await previous.focus()
      await previous.press('Enter')
      await expect(page.getByText('Synthetic source paragraph 41.', { exact: true })).toBeVisible()
      await expect(previous).toBeFocused()
      await expect(page.getByRole('status').filter({ hasText: /^Page 3$/ })).toBeVisible()
      await page.getByRole('button', { name: 'Previous sections', exact: true }).click()
      await expect(page.getByText('Synthetic source paragraph 21.', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Previous sections', exact: true }).click()
      await expect(page.getByText(evidence, { exact: true })).toBeVisible()
    }
    if (kind === 'md') {
      await expect(page.getByText('<script>window.uploadPreviewExecuted=true</script>', { exact: true })).toBeVisible()
      expect(await page.evaluate(() => (window as Window & { uploadPreviewExecuted?: boolean }).uploadPreviewExecuted)).toBeUndefined()
      await expect(page.locator('img[src="https://example.invalid/never-fetch.png"]')).toHaveCount(0)
    }
    await downloadEquals(page, kind, name)
    expect(inspect(body.document.id)).toMatchObject({
      document_count: 1, file_count: 1, extract_job_count: 1, private_original_exists: true,
    })
  })
}

test('editor can upload; viewer can preview/download but UI and real API forbid writing across roles and workspaces', async ({ page, browser }) => {
  await login(page, 'editor')
  const name = await choose(page, 'txt')
  const body = await submit(page)
  await ready(page, body)
  const viewerContext = await browser.newContext({ baseURL: origin })
  try {
    const viewer = await viewerContext.newPage()
    await login(viewer, 'viewer')
    await expect(viewer.getByRole('button', { name: 'Upload document', exact: true })).toHaveCount(0)
    await viewer.getByRole('button', { name: `View ${name}`, exact: true }).click()
    await ready(viewer, body)
    await viewer.getByRole('button', { name: 'Preview extracted text', exact: true }).click()
    await expect(viewer.getByText('Synthetic source paragraph 01.', { exact: true })).toBeVisible()
    await downloadEquals(viewer, 'txt', name)
    await expect(viewer.getByRole('button', { name: 'Edit metadata', exact: true })).toHaveCount(0)
    const csrf = await (await viewer.request.get('/api/v1/auth/csrf')).json()
    const denied = await viewer.request.post(uploads, {
      headers: { Origin: origin, 'X-CSRF-Token': csrf.csrf_token, 'Idempotency-Key': randomUUID() },
      multipart: { file: { name: 'viewer-denied.txt', mimeType: mime.txt, buffer: Buffer.from('Generated viewer denial.') } },
    })
    expect(denied.status()).toBe(403)
    await viewer.getByRole('combobox', { name: 'Active workspace' }).selectOption({ label: 'Personal workspace' })
    await expect(viewer.getByRole('combobox', { name: 'Active workspace' })).not.toHaveValue(fixture.workspace_id)
    const ownWorkspace = await viewer.getByRole('combobox', { name: 'Active workspace' }).inputValue()
    for (const suffix of ['/file', '/sections', '/download']) {
      expect((await viewer.request.get(`/api/v1/workspaces/${ownWorkspace}/documents/${body.document.id}${suffix}`)).status()).toBe(404)
    }
    await expect(viewer.getByRole('region', { name: 'File processing', exact: true })).toHaveCount(0)
  } finally { await viewerContext.close() }
})

test('archived originals remain readable; deletion cleans the private original and retains an idempotency tombstone', async ({ page }) => {
  await login(page)
  let nonce = ''
  page.on('request', (request) => { if (isUpload(request)) nonce = request.headers()['idempotency-key'] })
  const name = await choose(page, 'txt')
  const body = await submit(page)
  await ready(page, body)
  await page.getByRole('button', { name: 'Archive record', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Restore record', exact: true })).toBeVisible()
  await downloadEquals(page, 'txt', name)
  await page.getByRole('button', { name: 'Delete record', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm deletion', exact: true }).click()
  await expect(page.getByText(`“${name}” deleted.`, { exact: true })).toBeVisible()
  await expect.poll(() => inspect(body.document.id), { timeout: 20_000 }).toMatchObject({
    document_count: 0, file_count: 0, section_count: 0,
    private_original_exists: false, cleanup_state: 'succeeded',
  })
  expect((await page.request.get(`${collection}/${body.document.id}/download`)).status()).toBe(404)
  const csrf = await (await page.request.get('/api/v1/auth/csrf')).json()
  const replay = await page.request.post(uploads, {
    headers: { Origin: origin, 'X-CSRF-Token': csrf.csrf_token, 'Idempotency-Key': nonce },
    multipart: { file: { name, mimeType: mime.txt, buffer: readFileSync(files.txt) } },
  })
  expect(replay.status()).toBe(409)
  expect((await replay.json()).detail).toContain('deleted')
})

test('malformed PDF is accepted into the queue then fails real processing with preview and original blocked', async ({ page }) => {
  await login(page)
  await choose(page, 'malformed')
  const body = await submit(page)
  const processing = page.getByRole('region', { name: 'File processing', exact: true })
  await expect(processing).toContainText('Processing failed', { timeout: 20_000 })
  await expect(page.getByRole('button', { name: 'Preview extracted text', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Download original', exact: true })).toBeDisabled()
  const metadata = await (await page.request.get(`${collection}/${body.document.id}/file`)).json()
  expect(metadata).toMatchObject({ processing_status: 'failed', download_available: false, error_code: 'invalid_pdf' })
  expect((await page.request.get(`${collection}/${body.document.id}/sections`)).status()).toBe(409)
  expect((await page.request.get(`${collection}/${body.document.id}/download`)).status()).toBe(409)
})

test('worker unavailability leaves accepted uploads queued until the separate worker resumes', async ({ page }) => {
  await login(page)
  control('pause')
  try {
    await choose(page, 'txt')
    const body = await submit(page)
    await expect(page.getByRole('region', { name: 'File processing', exact: true })).toContainText('Queued')
    expect((await (await page.request.get(`${collection}/${body.document.id}/file`)).json()).processing_status).toBe('queued')
    expect((await page.request.get(`${collection}/${body.document.id}/download`)).status()).toBe(409)
    control('resume')
    await ready(page, body)
  } finally { control('resume') }
})

function isUpload(request: Request) {
  return request.url().endsWith(uploads) && request.method() === 'POST'
}

test('a real CSRF rejection waits for deliberate retry and preserves the upload UUID', async ({ page }) => {
  await login(page)
  const nonces: string[] = []
  let bootstraps = 0
  page.on('request', (request) => { if (request.url().endsWith('/api/v1/auth/csrf')) bootstraps++ })
  await page.route(`**${uploads}`, async (route) => {
    nonces.push(route.request().headers()['idempotency-key'])
    const headers = { ...route.request().headers() }
    if (nonces.length === 1) headers['x-csrf-token'] = 'deliberately-invalid-upload-token'
    await route.continue({ headers })
  })
  const name = await choose(page, 'txt')
  await page.getByRole('button', { name: 'Upload file', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText(/Action denied/)
  expect(nonces).toHaveLength(1)
  await expect(page.getByLabel('Choose document', { exact: true })).toHaveValue(new RegExp(`${name}$`))
  const body = await submit(page)
  expect(nonces).toHaveLength(2)
  expect(nonces[0]).toMatch(/^[0-9a-f-]{36}$/)
  expect(nonces[1]).toBe(nonces[0])
  expect(bootstraps).toBe(1)
  await ready(page, body)
  expect(inspect(body.document.id)).toMatchObject({ document_count: 1, file_count: 1, extract_job_count: 1 })
})

for (const lostResponse of [false, true]) {
  test(`${lostResponse ? 'a lost accepted response' : 'an interrupted upload'} retries explicitly with the same UUID and creates one real record`, async ({ page }) => {
    await login(page)
    const nonces: string[] = []
    let acceptedBeforeLoss: Upload | undefined
    await page.route(`**${uploads}`, async (route) => {
      nonces.push(route.request().headers()['idempotency-key'])
      if (nonces.length === 1) {
        if (lostResponse) {
          const accepted = await route.fetch()
          expect(accepted.status()).toBe(202)
          acceptedBeforeLoss = await accepted.json() as Upload
        }
        await route.abort('failed')
      } else await route.continue()
    })
    const name = await choose(page, 'txt')
    await page.getByRole('button', { name: 'Upload file', exact: true }).click()
    await expect(page.getByRole('alert')).toHaveText(/Connection lost/)
    expect(nonces).toHaveLength(1)
    await expect(page.getByLabel('Choose document', { exact: true })).toHaveValue(new RegExp(`${name}$`))
    const body = await submit(page)
    expect(nonces).toHaveLength(2)
    expect(nonces[1]).toBe(nonces[0])
    expect(body.replayed).toBe(lostResponse)
    if (lostResponse) expect(body.document.id).toBe(acceptedBeforeLoss?.document.id)
    await ready(page, body)
    expect(inspect(body.document.id)).toMatchObject({ document_count: 1, file_count: 1, extract_job_count: 1 })
  })
}

test('expiry during upload rejects the real request and clears the protected upload draft', async ({ page }) => {
  await login(page, 'expiry', false)
  const workspace = await page.getByRole('combobox', { name: 'Active workspace' }).inputValue()
  const endpoint = `/api/v1/workspaces/${workspace}/documents/uploads`
  await choose(page, 'txt')
  execFileSync(process.env.E2E_PYTHON!, ['scripts/run_e2e.py', '--expire-user', fixture.users.expiry.id], { stdio: 'pipe' })
  const denied = page.waitForResponse((response) => response.url().endsWith(endpoint) && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Upload file', exact: true }).click()
  expect((await denied).status()).toBe(401)
  await expect(page.getByRole('heading', { name: 'Sign in to your account' })).toBeVisible()
  await expect(page.getByLabel('Choose document', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'File processing', exact: true })).toHaveCount(0)
})

test('test-only storage and worker controls reject an unmarked database or private root before access', () => {
  for (const overrides of [
    { E2E_ISOLATED: '0' },
    { DATABASE_URL: process.env.DATABASE_URL!.replace(/agentsphere_e2e_[0-9a-f]{32}/, 'agentsphere') },
    { PRIVATE_STORAGE_ROOT: process.cwd() },
  ]) {
    const rejected = spawnSync(process.env.E2E_PYTHON!, ['scripts/run_e2e.py', '--worker-control', 'pause'], {
      env: { ...process.env, ...overrides }, encoding: 'utf8',
    })
    expect(rejected.status).toBe(1)
    expect(rejected.stderr).toMatch(/requires|refuses/)
  }
})

for (const [width, height] of [[1440, 1000], [768, 1024], [320, 740]]) {
  test(`upload and real extracted preview are accessible without overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await login(page)
    await choose(page, 'pdf')
    await expect(page.getByLabel('Choose document', { exact: true })).toBeFocused()
    const analyze = async () => {
      const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
      expect(result.violations.map(({ id, nodes }) => ({ id, targets: nodes.map((node) => node.target) }))).toEqual([])
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    }
    await analyze()
    const body = await submit(page)
    await ready(page, body)
    await page.getByRole('button', { name: 'Preview extracted text', exact: true }).click()
    await expect(page.getByText('Synthetic PDF evidence', { exact: true })).toBeVisible()
    await analyze()
    await page.screenshot({ path: `.local/e2e/upload-${width}.png`, fullPage: true })
  })
}
