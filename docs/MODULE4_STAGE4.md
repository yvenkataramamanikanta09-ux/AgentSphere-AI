# Module 4 — Stage 4: Frontend testing, accessibility, and polish

Stage 4 tests the real React, Vite, FastAPI, Alembic, and PostgreSQL stack. It adds
Playwright browser tests and axe accessibility scans, improves keyboard focus and
text contrast, and preserves the existing backend APIs and security settings.
There are no uploads, RAG features, AI agents, or new application endpoints.

## Concepts and boundaries

Component tests use controlled HTTP responses to exercise individual frontend
states. End-to-end tests use a real browser and real server responses, including
cookie authentication and PostgreSQL commits. Neither kind of test replaces the
backend's own security tests. Route interception in two browser scenarios only
simulates a network failure or corrupts a CSRF request header; it does not supply
fabricated successful backend responses.

Playwright tests operate through labels, roles, and visible text. Axe checks the
rendered authentication pages, dashboard, details, and edit form against its
WCAG 2.0/2.1 A and AA rules. Automated scans cannot prove full accessibility;
manual screen-reader and usability assessment remain necessary. See the
[official Playwright accessibility guidance](https://playwright.dev/docs/accessibility-testing).

## Improvements made from review and browser findings

- Authentication pages now have a focusable main H1, route-specific page titles,
  and heading focus after navigation. The promotional heading is an H2.
- The skip link has a focusable main destination. Mobile navigation focuses its
  first link when opened, the selected destination after navigation, and the
  Menu button when dismissed with Escape.
- Cancelling workspace or document creation returns focus to the opener. Opening
  details focuses its heading; closing or deleting details focuses the filter.
- Inline deletion confirmation initially focuses Keep record. Escape cancels it
  and restores focus to Delete record. It is an inline group, not a modal dialog.
- Workspace cards separate their descriptive content from the selection button.
  Document MIME descriptions sit outside their filename buttons. Accessible names
  now include the visible action text, supporting screen readers and voice input.
- Secondary text, hints, table headings, action text, and focus indicators have
  stronger contrast. The active workspace label remains accessible at small widths.
- Existing loading, empty, validation, permission, and error states were retained
  and exercised. Failed writes keep input and never announce success.

Initial browser runs exposed contrast and accessible-name violations, a mobile
navigation focus issue, and a test account hitting the existing login limit.
These were resolved; separate layout-test accounts avoid the limit without raising
or disabling it. There are no remaining failures or skipped tests in the final run.

## Run the application for manual review

From PowerShell, terminal one:

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\backend
.\.venv\Scripts\python.exe scripts/local_postgres.py start
$env:APP_ORIGIN = 'http://127.0.0.1:5173'
.\.venv\Scripts\python.exe -m uvicorn agentsphere.main:create_app --factory --host 127.0.0.1 --port 8001 --no-proxy-headers
```

Terminal two:

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\frontend
npm ci
$env:BACKEND_TARGET = 'http://127.0.0.1:8001'
npm run dev
```

Use existing processes if these ports are already occupied by this application.
The existing ignored backend `.env` supplies database credentials and loopback
development settings. Do not put secrets in `VITE_*` variables. The Vite proxy
preserves the browser-facing Host header; requests remain same-origin and comply
with the backend's Origin, Host, cookie, and CSRF checks.

Open [AgentSphere AI](http://127.0.0.1:5173/) using that exact hostname.

1. Choose Create an account. Enter your name, email, and a unique password of at
   least 15 characters; confirm it. Registration creates your personal workspace
   and returns you to sign-in, without automatically authenticating you.
2. Sign in. Select a workspace through the top selector or its Select button.
   Refresh the page to verify the session and `?workspace=...` selection restore.
3. Choose New workspace, enter a name, and create it. You become its owner and
   the dashboard selects the returned workspace.
4. Choose Documents. Add a record such as `research.pdf`, `application/pdf`,
   and `2048` bytes. These are declared metadata; no file is uploaded or inspected.
5. Select the record to view timestamps, status, size, content type, and IDs.
   Edit the filename or size, then save. Filename changes infer the MIME type.
6. Archive the record, choose Archived in the filter, then restore it. All records
   includes both statuses. Page controls show ten matching rows at a time.
7. Choose Delete record. Keep record or Escape cancels the confirmation; Confirm
   deletion permanently removes metadata. Only test records should be used when
   exploring this action.
8. With existing editor and viewer memberships, verify that editors can write
   and viewers only read. Membership editing remains an API capability; no new
   membership UI is included in this stage.
9. Use Tab and Shift+Tab, the skip link, Enter, and Escape to review keyboard flow.
   At narrow widths, open Menu to navigate. Errors appear beside fields and in
   announced messages; controls show loading states during pending operations.
10. Sign out. The server revokes the session and returns you to sign-in. An expired
    session also clears the protected interface and asks you to sign in again.

## Automated checks

From `frontend`:

```powershell
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
```

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check . ..\frontend\scripts\run_e2e.py ..\frontend\scripts\verify_document_api.py
.\.venv\Scripts\python.exe -m ruff format --check . ..\frontend\scripts\run_e2e.py ..\frontend\scripts\verify_document_api.py
```

The E2E runner uses the existing local cluster created by `local_postgres.py`.
It requires the ignored `backend/.local/postgres-config.json`, a loopback
`TEST_DATABASE_URL` ending in `_test`, the backend Poetry environment, Node, and
free ports **8002 and 5174**. It does not initialize or reset an existing cluster.
An incompatible configuration or occupied test port stops setup safely.
Database URLs containing query parameters are rejected before connections or
database creation, so driver options cannot override the database or host.

The runner uses the saved local administrator credential to create a unique
`agentsphere_e2e_<uuid>` database owned by the existing test role. It applies
Alembic migrations and checks consistency, seeds disposable identities, starts
the unmodified FastAPI application and Vite, runs the 53 live document API checks,
and then executes Playwright. Finally, it terminates only its own servers and
drops only the database it created. Development data and settings are untouched.
Backend regression tests use their existing separate test schemas.

Installed Google Chrome at its standard Windows path is selected automatically.
If unavailable, install bundled Chromium once with:

```powershell
npx playwright install chromium
```

An explicit browser channel can be selected for a run, for example:

```powershell
$env:E2E_BROWSER_CHANNEL = 'chrome'
npm run test:e2e
Remove-Item Env:E2E_BROWSER_CHANNEL
```

To use installed Playwright Chromium explicitly, use `chromium` as the channel
after installation. This implementation was verified with installed Chrome,
which uses Chromium. Firefox and WebKit were not run.

Run a focused subset with `npm run test:e2e -- --grep responsive`. Setup, migrations,
the 53 live API checks, and cleanup still run. Configuration deliberately rejects
calling Playwright directly outside the isolated runner.

Reports, screenshots, and server logs stay in ignored `frontend/.local/e2e/`.
The HTML report is `.local/e2e/report/index.html`; dashboard screenshots cover
all five viewport sizes. The report reflects the most recent invocation, including
a filtered run. No storage-state files, network traces, videos, or credential
fixtures are saved. Credentials and CSRF/session secrets are not put in
localStorage. HTML reports can retain generated disposable passwords in browser
action labels even with traces disabled. Reports are therefore sensitive local
artifacts, excluded from Git. Their generated accounts are removed with the
disposable database; no development account credentials are used in these tests.

The existing standalone document verification script retains its default origin
of port 5173. The E2E runner sets `VERIFICATION_ORIGIN` to port 5174 for its child
process; the script accepts only loopback origins without paths or credentials.

## Results and remaining limitations

| Check | Final result |
| --- | --- |
| Frontend component/client/validation tests | 66 passed, 0 failed, 0 skipped |
| Backend tests | 169 passed, 0 failed, 0 skipped |
| Real FastAPI/PostgreSQL checks | 53 passed, 0 failed |
| Playwright browser scenarios | 17 passed, 0 failed, 0 skipped |
| Final focused keyboard/responsive rerun | 6 passed, 0 failed, 0 skipped |
| TypeScript, frontend lint, production build | Passed |
| Ruff lint and formatting | Passed |
| Alembic in the disposable database | Head applied; no schema differences |

The browser scenarios cover registration and field validation, incorrect login,
login/logout, session restoration, workspace creation/selection, metadata CRUD,
archive/restore, pagination, filtering, owner/editor/viewer restrictions,
cross-workspace isolation, server size rejection, CSRF rejection/manual retry,
server session expiry, network failure recovery, keyboard focus, and accessibility.
Setup and expiry regression assertions also reject database URL query overrides.
Viewport checks passed at 1440×1000, 1024×768, 768×1024, 390×844, and 320×740,
with no horizontal page overflow or detected axe violations in the scanned states.

Backend tests report one existing Starlette/httpx deprecation warning. Playwright
may report an environment color-output warning; neither warning skips tests.
Manual screen-reader testing, Firefox/WebKit, real mobile hardware, and production
HTTPS/reverse-proxy deployment remain outside this local verification. Tests use
the Vite development server; the production bundle was built but not served in
the browser suite. A forcefully terminated runner may leave its disposable
database behind; inspect its generated name before any manual cleanup.

## Changed files

| File | Purpose |
| --- | --- |
| `frontend/package.json`, `frontend/package-lock.json` | Dev-only Playwright/axe dependencies and E2E command |
| `frontend/playwright.config.ts` | Browser runner, timeouts, isolated-run guard and ignored reports |
| `frontend/e2e/application.spec.ts` | Real browser workflows and accessibility checks |
| `frontend/scripts/run-e2e.mjs` | Portable launcher using the existing backend interpreter |
| `frontend/scripts/run_e2e.py` | Disposable database, migrations, seeds, servers, API checks and cleanup |
| `frontend/scripts/verify_document_api.py` | Loopback-only configurable verification origin |
| `frontend/.gitignore` | Exclude additional browser report directories |
| `frontend/tsconfig.node.json`, `frontend/vite.config.ts` | Typecheck E2E files and separate Vitest discovery |
| `frontend/src/App.tsx` | Route titles and main-heading focus |
| `frontend/src/features/auth/AuthLayout.tsx` | Promotional heading semantics |
| `frontend/src/features/auth/LoginPage.tsx`, `frontend/src/features/auth/RegisterPage.tsx` | Focusable main H1 headings |
| `frontend/src/features/workspaces/WorkspaceDashboard.tsx` | Focus management and accessible workspace selection buttons |
| `frontend/src/features/workspaces/dashboard.css` | Accessible labels, contrast and card action styling |
| `frontend/src/features/documents/DocumentLibrary.tsx` | Detail/confirmation focus and accessible document buttons |
| `frontend/src/features/documents/documents.css` | Readable metadata descriptions and contrast |
| `frontend/src/styles.css` | Authentication text contrast, heading styles and focus indicator |
| `frontend/README.md`, `docs/MODULE4_STAGE4.md` | Startup, workflow, test architecture and results |

No backend source, migrations, production dependencies, or API contracts changed.
