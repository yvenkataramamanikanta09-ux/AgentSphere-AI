# Module 5 Stage 4 — React document upload interface

This stage connects the React library to the existing secure upload, file-status,
section, and download APIs. It adds no backend endpoints, schema changes,
parser features, external services, or production dependencies. It uses the
existing backend worker unchanged.
Implementation starts from `develop` at
`3c4369abf8ee3b2cbc6d2f4b571db266b0d2466e`.

## Features and boundaries

Owners and editors can choose **Upload document** for PDF, DOCX, TXT, `.md`, or
`.markdown`. The accessible chooser checks safe filenames, supported formats,
nonempty files, and the inclusive 10,000,000-byte ceiling before sending. These
checks improve feedback; backend byte measurement, format checks, authorization,
and extraction remain authoritative. **Add record** remains a separate,
metadata-only workflow for all existing Module 4 operations.

Transfer progress measures actual XHR upload events, including multipart overhead.
100% means transfer completion, not durable server receipt or successful parsing.
The UI waits for the upload response before showing **Upload received**. The
selected record's file panel then distinguishes **Queued**, **Processing**,
**Ready**, and **Processing failed**, independently of registered/archived status.
Queued means a file is stored and awaits a worker; it does not promise when a
worker will become available. Failure messages use a fixed safe vocabulary and
never render parser traces, storage paths, or raw database errors.

Ready files may expose **Preview extracted text** and **Download original** only
when the server reports availability. Preview displays text as React text nodes,
not HTML, Markdown rendering, embedded images, scripts, or executable links.
Section pagination uses limit 20 and the returned offset/has_more. It displays
actual PDF page numbers, DOCX headings, and line ranges when supplied; missing
locations are not fabricated. Empty extraction reports that no text was found;
there is no OCR. Download uses the authenticated Blob response, a safe filename,
and a temporary object URL that is revoked after use and on unmount.

Viewers can inspect authorized records, ready text, and available originals;
upload/edit/archive/restore/delete controls remain restricted to owners/editors.
File-backed edit forms allow a filename change within the same format and keep
measured size and canonical content type immutable. Metadata-only editing retains
the existing behavior. Deleting a document removes API visibility and schedules
durable physical cleanup; the UI does not claim disk cleanup has already finished.

## API integration and request lifecycle

| Method and workspace-scoped path | Use |
| --- | --- |
| `POST /api/v1/workspaces/{workspace_id}/documents/uploads` | One multipart file, CSRF header, UUID Idempotency-Key |
| `GET /api/v1/workspaces/{workspace_id}/documents/{document_id}/file` | Processing status and authoritative download availability |
| `GET /api/v1/workspaces/{workspace_id}/documents/{document_id}/sections?limit=20&offset=0` | Authorized, successfully published text only |
| `GET /api/v1/workspaces/{workspace_id}/documents/{document_id}/download` | Authorized original Blob after server validation |
| Existing document CRUD paths | Metadata-only creation plus editing, lifecycle status, deletion, filtering, pagination |

The existing shared client owns CSRF state in memory and the browser owns the
HttpOnly session cookie. No session credentials or CSRF tokens enter localStorage.
The same-origin guard applies to JSON, Blob, and XHR requests. Multipart uses
FormData and browser-generated Content-Type/boundaries; its canonical part MIME
comes from the supported extension, with backend content validation unchanged.
Expired sessions notify the existing authentication provider. CSRF or permission
failures clear stale tokens, request access refresh where appropriate, and require
an explicit user retry. Login rotates the token. A generation check prevents
responses begun under a previous client session from being published afterward.

Each accepted chooser selection creates a `crypto.randomUUID()` intent paired
with the exact File object. Network failure, timeout, a lost response, cancellation,
or a CSRF rejection retains this pair. **Retry same upload** submits that pair;
the server can recover a previously committed result without duplicate records.
There is no automatic upload replay. Choosing a file again starts a new intent.
Closing/reloading the form or switching workspace discards its in-memory intent;
check the library before retrying as a new selection after uncertain receipt.
Aborting a browser request cannot undo a server commit. A temporary network/service
failure while rechecking the same workspace preserves the open form and UUID and
pauses writes until access is verified. A confirmed access loss removes the form;
an actual workspace switch also clears it. Obsolete session restoration requests
are aborted on cleanup, including React StrictMode's effect replay, so they cannot
invalidate a later restoration or produce spurious login errors.

File polling schedules the next request three seconds after the previous response,
only while queued or processing. Errors stop automatic polling until manual
**Refresh file status**. Ready/failed states, document removal, workspace switch,
logout, session expiry, and unmount stop polling and cancel outstanding reads.
Late responses cannot publish into a new document/workspace/session. A rejected
preview/download invalidates availability and requires a status refresh before
the file action can be attempted again. Server authorization is always decisive.

The UI uses labeled controls, progress/status announcements, visible focus,
focus restoration after form cancellation, and wrapped source text at narrow
widths. File-status refresh and section-pagination controls remain mounted during
loading, retain keyboard focus, and guard repeated activation with aria-disabled.
Each loaded section page is announced through a polite status region. Async
responses do not move focus after the user chooses another control. Existing
metadata workflows remain accessible. Screen-reader assessment with assistive
technology is still required.

## Safe local verification and startup

The existing development database stays at **0002**, with no migration or storage
initialization in this stage. File APIs require **0003**. Starting the normal
development frontend against 0002 preserves metadata features and produces a safe
file-service-unavailable message for upload/file requests.

Use a **new disposable environment** for the working upload demonstration. In
PowerShell, with the existing backend Poetry environment and local PostgreSQL
cluster already installed:

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\backend
.\.venv\Scripts\python.exe scripts/local_postgres.py start
Set-Location ..\frontend
npm ci
npm run test:e2e
```

For a visible browser run:

```powershell
npm run test:e2e -- --headed --workers=1
```

The harness reserves API port **8002** and frontend port **5174**, creates a new
randomly named local PostgreSQL database, applies 0003 and checks schema drift,
and creates a new temporary private root. FastAPI and a separate real worker
share exactly that database and storage root. The runner seeds disposable users,
generates synthetic documents at runtime, runs 53 existing API checks, then runs
Playwright against **http://127.0.0.1:5174/**. It stops only its own processes and
removes only its own database/temporary storage on exit. Normal services on
8000/8001/5173 remain separate. These temporary services are not a persistent demo.

Test helpers for worker pause/resume, expiry, and upload inspection require the
per-run isolation marker, generated database name, and matching temporary storage
path. They are CLI helpers, not application endpoints. The worker supervisor
confirms the worker has stopped before queued-state checks and resumes afterward.
The harness fails if its dedicated ports are already occupied; do not terminate
unrelated servers to free them. On Windows the real base interpreter loads the
existing virtual environment's trusted site directory, avoiding launcher parent
processes that would outlive test teardown.

Installed Chrome is selected when available; otherwise run
`npx playwright install chromium`. Backend credentials come from the existing
ignored local cluster configuration. Reports and logs remain ignored under
`frontend/.local/e2e/`; action labels may contain generated disposable test
passwords, so keep reports private and out of Git. Forced termination may leave
disposable resources requiring separately reviewed cleanup.

Standalone backend regression and worker verification, each with its own new
disposable database and private directories:

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\backend
.\.venv\Scripts\python.exe scripts/verify_stage2.py -q
.\.venv\Scripts\python.exe scripts/verify_processing.py
.\.venv\Scripts\python.exe -m pytest ..\frontend\scripts\test_e2e_runner.py -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts ..\frontend\scripts\run_e2e.py ..\frontend\scripts\test_e2e_runner.py
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts ..\frontend\scripts\run_e2e.py ..\frontend\scripts\test_e2e_runner.py
C:\Users\yvenk\Desktop\agentsphere-ai\.tools\poetry\Scripts\poetry.exe check --lock
.\.venv\Scripts\python.exe -m alembic heads
Set-Location ..\frontend
npm test
npm run typecheck
npm run lint
npm run build
```

Do not run `alembic upgrade` against the existing development configuration.
The separately approved backup, restore verification, and 0002→0003 procedure is
documented in [Module 5 Stage 2](MODULE5_STAGE2.md); worker/storage startup and
security assumptions are in [Stage 3](MODULE5_STAGE3.md). No development migration
is authorized or needed for the disposable checks above.

## User workflow in the isolated browser environment

1. Sign in with a disposable test account or register a new test account, then
   select/create a workspace. Existing owner/editor/viewer permissions apply.
2. Choose **Upload document**, select a supported file, inspect its size/name,
   and choose **Upload file**. Observe transfer progress and server receipt.
3. Inspect the file panel as queued becomes processing and ready. If no worker
   is available, queued remains truthful. Refresh manually after a service error.
4. Choose **Preview extracted text**, inspect actual source references, and use
   **Next sections**/**Previous sections** for long results. HTML-looking source
   remains inert text.
5. Choose **Download original** after availability is reported; the downloaded
   bytes are the original file. Owners/editors can rename, archive/restore, and
   confirm deletion. Viewers see read actions only.
6. Use **Add record** to confirm the separate metadata-only path remains intact.
   A metadata-only record reports that no file was uploaded.
7. After uncertain receipt, keep the form open and choose **Retry same upload**.
   Never assume that cancelling a transfer deleted a received file.

## Verification results

Tests use synthetic fixtures only; no actual customer documents or development
records are copied.

| Verification | Result |
| --- | --- |
| Complete frontend Vitest suite | 136 passed, 0 failed, 0 skipped after commit-preparation fixes |
| Complete backend pytest suite, new disposable 0003 database | 732 passed, 0 failed, 0 skipped |
| Isolated E2E runner mailbox pytest regressions | 4 passed, 0 failed, 0 skipped on Windows |
| Separate real API/PostgreSQL/worker verification | 79 passed, 0 failed |
| Existing live auth/workspace/document API checks | 53 passed, 0 failed |
| Playwright real application tests | 33 passed, 0 failed, 0 skipped; 17 existing and 16 new |
| TypeScript, frontend lint, production build | Passed; frontend lint has no warnings |
| Ruff, backend plus changed Python runner/tests | Passed; 61 files formatted |
| Poetry lock validation | Passed |
| Alembic head / disposable schema check | 0003 / no model-schema drift |

The backend suite includes clean/populated migration, storage safety, extraction,
lease/race/recovery, authorization, and idempotency regressions. The worker check
uses separate processes to verify all four formats, source references, downloads,
retry/recovery, durable cleanup, and conservative reconciliation.

Initial browser verification found an obsolete StrictMode restoration invalidating
a later session check. Aborting restoration on effect cleanup and a new StrictMode
regression address this race. Review also found that a temporary workspace refresh
failure could discard an uncertain upload intent; a regression now verifies the
exact File/UUID survives while writes are paused. Two initial network-error
assertions expected the old generic message instead of the new safe retry message,
and a cross-workspace test read the old selection before its authorized response
arrived; those test expectations now reflect the real UI/selection contract.
Two reruns exposed a Windows control-file replacement race during isolated worker
startup. Private diagnostics identified an open reader handle blocking atomic
replacement; a two-second retry budget now handles only Windows sharing/access
errors and preserves atomic publication. Four focused Python tests verify normal
publication, unrelated failures, actual Windows handle contention, and timeout
cleanup. They use pytest temporary directories and no database or private storage.
The final full Playwright run passed all 33 tests after these fixes. Real upload
checks cover PDF/DOCX/TXT/Markdown, exact original bytes, source/section pagination,
viewer/editor/owner boundaries, wrong-workspace access, archive/deletion and
physical cleanup/tombstones, malformed-PDF failure, worker pause/resume, CSRF,
lost/interrupted-response recovery with the same UUID, and expired sessions.
Upload forms and real extracted previews passed axe WCAG A/AA checks and overflow
checks at 1440, 768, and 320 pixels; existing layouts also cover 1024 and 390.
Desktop and mobile synthetic-data screenshots were visually reviewed and remain
ignored. The separate four mailbox regression tests passed on Windows.

Read-only final comparison confirmed development revision 0002 and unchanged
counts/SHA-256 values for all seven baseline tables. Its private storage root
remains absent and was never initialized. All tested databases and private roots
were new disposable resources. Changed-file review detected no configured real
credentials, private keys, uploads, databases, local environments, or generated
artifacts. No backend source or migration changes were required.

Commit preparation first verified all 28 file SHA-256 hashes against the saved
implementation snapshot. Review then fixed two keyboard-focus transitions: status
refresh remounted its focused trigger, and section pagination removed its focused
pager during loading. Two delayed-response regressions verify stable focus and no
asynchronous focus theft; the real TXT browser test now exercises these controls
with the keyboard. Safe processing-error mapping was also hardened to reject
inherited object-property names as unknown codes. All three added adversarial
error-code regressions failed before that fix and passed afterward. These five
new cases increase the frontend total from 131 to 136. The changes remain within
the original 28-file inventory. Backend code, dependencies and migrations are
unchanged, so the saved 732 backend and 79 worker/API checks still apply. Before
staging, the complete revised frontend suite passed 136 tests; the fresh browser
run passed all 33 tests and 53 live API checks, using a new 0003 database and
private storage root shared by API and worker. TypeScript, frontend lint/build,
the four Python harness tests, scoped Ruff/formatting, Poetry lock validation and
disposable Alembic consistency also passed. There were no final test failures or
skips. Read-only checks confirmed development stayed at 0002 with all seven
table snapshots unchanged and private storage still uninitialized.

One existing Starlette/httpx TestClient deprecation is reported by pytest.
An additional broad Ruff scan from the backend working directory reports a
pre-existing **I001** import-order issue at
`frontend/scripts/verify_workspace_api.py:8`; repo-root invocation passes because
import classification differs with the working directory. That unrelated file
was left unchanged. Backend Python and the changed runner/tests pass both
lint and formatting. Windows LF/CRLF normalization notices and Playwright's
NO_COLOR/FORCE_COLOR notice do not indicate failed checks.

## Changed-file inventory

| File | Purpose |
| --- | --- |
| `frontend/src/api/client.ts` | Shared multipart XHR, Blob reads, same-origin and session-generation protection |
| `frontend/src/api/client.test.ts` | CSRF, cancellation, progress, error, and stale-response tests |
| `frontend/src/api/documentFormats.ts` | Canonical supported extension/MIME mapping |
| `frontend/src/api/documentFiles.ts` | Real upload/status/sections/download contracts |
| `frontend/src/api/documentFiles.test.ts` | API URL, multipart, MIME, header, and download tests |
| `frontend/src/features/auth/AuthContext.tsx` | Abort obsolete session restoration and clear superseded login feedback |
| `frontend/src/features/auth/AuthPages.test.tsx` | React StrictMode session-restoration/login regression |
| `frontend/src/features/documents/DocumentUpload.tsx` | Accessible single-file upload intent, validation, progress, explicit retry |
| `frontend/src/features/documents/DocumentFilePanel.tsx` | Nonoverlapping polling, readiness gates, preview pagination, Blob download |
| `frontend/src/features/documents/fileValidation.ts` | Filename/format/size checks and safe download names |
| `frontend/src/features/documents/fileErrors.ts` | Fixed safe request/processing messages |
| `frontend/src/features/documents/files.css` | Responsive upload, status, preview, and file-action presentation |
| `frontend/src/features/documents/DocumentFiles.test.tsx` | Upload/status/preview/download lifecycle and security tests |
| `frontend/src/features/documents/fileValidation.test.ts` | Inclusive size ceiling, filename/format checks, safe errors |
| `frontend/src/features/documents/DocumentLibrary.tsx` | Separate upload and metadata entry points, selected-upload integration |
| `frontend/src/features/documents/DocumentLibrary.test.tsx` | Role controls, upload selection/focus, immutable measured fields |
| `frontend/src/features/documents/DocumentForm.tsx` | Safe file-backed rename without editable measured metadata |
| `frontend/src/features/documents/documents.css` | Responsive creation actions and read-only field presentation |
| `frontend/src/features/documents/errors.ts` | Safe conflict/fallback messages for existing record operations |
| `frontend/src/features/workspaces/useWorkspaces.ts` | Preserve the same upload intent during temporary access-check failures; pause writes |
| `frontend/src/features/workspaces/WorkspaceDashboard.tsx` | Explicit access-check failure and retry while retaining the form |
| `frontend/src/features/workspaces/WorkspaceDashboard.test.tsx` | Exact File/UUID retention through outage and access recovery |
| `frontend/e2e/application.spec.ts` | Existing workflow caption reflects real files and records |
| `frontend/e2e/uploads.spec.ts` | Actual upload/worker/roles/preview/download/failure browser tests |
| `frontend/scripts/run_e2e.py` | New disposable database/storage, generated fixtures, real worker supervision |
| `frontend/scripts/test_e2e_runner.py` | Atomic mailbox publication and bounded Windows sharing-race regressions |
| `frontend/README.md` | Current startup, same-origin security, disposable upload workflow |
| `docs/MODULE5_STAGE4.md` | This implementation and verification guide |

No dependency or lockfile changes are required. No backend source files or
migrations are changed. The review contains **28 changed files: 15 modified and
13 new**, prepared for staging on `develop` at the starting commit. The complete
inventory above matches the reviewed Git file list.

## Remaining limitations

One file per form; no batch uploads, transfer resume, persistent upload intent,
automatic retry, or user-triggered failed-job retry endpoint. Files/keys are held
only in memory. Queued work depends on a running separate worker. The UI exposes
server failures safely but cannot repair inaccessible storage or a stopped worker.

No OCR, antivirus/CDR, embeddings, RAG, vector database, AI chat, or agents. Server
structural validation is not malware scanning. Existing parser, storage, and
operating-system limits remain those documented in Stage 3. Blob downloads are
bounded by the existing 10 MB upload limit but buffer the response in browser
memory. Preview pages contain up to 20 sections and render plain text.

Automated browser verification targets local Chromium/Chrome on Windows.
Non-Chromium browsers, manual screen-reader assessment, production HTTPS cookie
deployment, and production infrastructure require separate verification.
The original development database remains 0002; production/development migration
and data backup require separate approval. Commit preparation stages only the
reviewed 28 files; no commit, push, main-branch modification, or Stage 5 work is
included.
