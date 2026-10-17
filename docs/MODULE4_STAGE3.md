# Module 4 — Stage 3: Document management

The selected workspace now contains a document metadata library connected to the
existing FastAPI and PostgreSQL APIs. Owners and editors can create, edit,
archive, restore, and delete records. Viewers can list and inspect them. This
stage does not upload files, extract text, run RAG, or introduce AI features.
Backend source, migrations, dependencies, and API contracts are unchanged.

## Concepts and architecture

A document record describes a file; it is not the file itself. The browser sends
JSON containing `filename`, `content_type`, and a declared `size_bytes` value.
FastAPI validates those values, checks workspace membership, and persists the
record. PostgreSQL supplies durable storage, while React displays responses.
No record is announced as saved before a successful server response.

`DocumentLibrary` coordinates forms, selection, confirmation, and notices.
`useDocuments` handles list and detail requests independently, with request
cancellation and keys that prevent an old response appearing in a new workspace.
`documentApi` reuses the shared cookie and CSRF client. The existing dashboard
mounts the library only after resolving the selected workspace. Changing workspace
or role remounts the library, clearing drafts and selected document details.

Returning focus to the window refreshes records and rechecks workspace access.
During a recheck of the same workspace, the form remains mounted so unsaved input
survives; writes pause until the workspace request succeeds. A lost membership
removes the library. The server always enforces the current permission on every
request, even if a role changes while the page is open.

## Existing API contracts

All paths begin with `/api/v1/workspaces/{workspace_id}/documents`.

| Method | Suffix | Request | Success |
| --- | --- | --- | --- |
| GET | collection | `limit`, `offset`, optional `status` | 200 page |
| POST | collection | filename, content_type, size_bytes | 201 record |
| GET | `/{document_id}` | none | 200 record |
| PATCH | `/{document_id}` | changed metadata and/or status | 200 record |
| DELETE | `/{document_id}` | none | 204, no body |

A record contains `id`, `workspace_id`, `filename`, `content_type`, `size_bytes`,
`status`, `created_at`, and `updated_at`. Status is `registered` or `archived`.
Archiving sends `{"status":"archived"}`; restoration sends
`{"status":"registered"}`. Deletion permanently removes the metadata record
and requires an explicit confirmation in the interface.

Example creation JSON:

```json
{
  "filename": "research.pdf",
  "content_type": "application/pdf",
  "size_bytes": 2048
}
```

The list uses ten records per page, with `offset` increments of ten. The backend
returns `{items,limit,offset,has_more}`; Next follows `has_more`, without inventing
a total count. Status choices are All records (parameter omitted), Registered,
and Archived. Changing status resets to page one. Filtering does not change a
record's status. Newly created records can be hidden by an Archived filter;
their separately fetched details remain available.

## Validation, permissions, and failures

React Hook Form and Zod validate required fields, safe filenames, matching MIME
types, and positive whole-byte sizes. Supported extensions are `.pdf`, `.docx`,
`.txt`, `.md`, and `.markdown`. Changing the filename infers its MIME type.
The backend remains authoritative and enforces its configured size limit
(default 10,000,000 bytes). The UI reports a 413 without assuming a fixed limit.
This only validates declared metadata; it cannot verify actual file contents.

| Workspace role | Read and filter | Create/edit/archive/restore/delete |
| --- | --- | --- |
| Owner | Yes | Yes |
| Editor | Yes | Yes |
| Viewer | Yes | No |
| Non-member | No | No |

The UI hides write controls for viewers. This is usability, not an authorization
boundary: FastAPI independently rejects unauthorized writes and conceals records
from non-members and requests using the wrong workspace.

The shared API client sends same-origin HttpOnly cookies and holds CSRF tokens
only in memory. Login rotates the token; authenticated writes attach it in the
existing header. A protected 401 clears the dashboard and returns to login.
On a 403, the token is discarded, the UI reports a permission/security failure,
and workspace access is rechecked. Failed writes are never replayed
automatically. A deliberate retry obtains a fresh token. Server 422 field errors
appear next to inputs; failed saves preserve input. Unavailable records return
a clear 404 message, and list/detail failures offer retry controls.

Desktop layouts put details beside the list. Narrow layouts stack panels and
hide the table size column while keeping exact bytes in the detail panel.

## Files and responsibilities

| File under `frontend/` | Purpose |
| --- | --- |
| `src/api/documents.ts` | Typed workspace-scoped document requests |
| `src/api/documents.test.ts` | URL, payload, cookie, CSRF, and empty DELETE response checks |
| `src/features/documents/DocumentLibrary.tsx` | Library, roles, actions, confirmation, pagination and filtering |
| `src/features/documents/DocumentForm.tsx` | Creation/editing forms and server validation errors |
| `src/features/documents/useDocuments.ts` | Abortable list/detail requests and refresh state |
| `src/features/documents/validation.ts` | Filename, MIME, and byte-size validation |
| `src/features/documents/errors.ts` | Document-specific error explanations |
| `src/features/documents/documents.css` | Responsive library styling |
| `src/features/documents/validation.test.ts` | Supported formats and invalid metadata cases |
| `src/features/documents/DocumentLibrary.test.tsx` | Operations, roles, failures, expiry, and stale-response tests |
| `src/features/workspaces/WorkspaceDashboard.tsx` | Dashboard integration and Documents navigation |
| `src/features/workspaces/useWorkspaces.ts` | Preserve drafts and pause writes during access rechecks |
| `src/features/workspaces/WorkspaceDashboard.test.tsx` | Existing dashboard coverage adapted to the real document contract |
| `scripts/verify_document_api.py` | Disposable integration checks against local FastAPI/PostgreSQL |
| `README.md` | Local startup and frontend check instructions |

This guide is `docs/MODULE4_STAGE3.md`. No new npm dependencies were required.

## Run locally

Use the existing environment. In PowerShell, terminal one:

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\backend
.\.venv\Scripts\python.exe scripts/local_postgres.py start
$env:APP_ORIGIN = 'http://127.0.0.1:5173'
.\.venv\Scripts\python.exe -m uvicorn agentsphere.main:create_app --factory --host 127.0.0.1 --port 8001 --no-proxy-headers
```

In terminal two:

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\frontend
npm ci
$env:BACKEND_TARGET = 'http://127.0.0.1:8001'
npm run dev
```

If these processes are already running, use them instead of starting duplicates.
The existing ignored backend `.env` must configure the local development database
and loopback HTTP cookies. Do not expose development HTTP settings publicly.
Vite preserves the frontend-facing Host header and proxies `/api`, keeping
requests same-origin and compatible with the backend's Origin/Host/CSRF checks.

Open [the frontend](http://127.0.0.1:5173/), register or sign in with your own
account, and select a workspace. Choose Documents, then Add record. Enter the
example metadata above. Select its row to inspect details, Edit metadata, Archive
record, or Delete record. Use Archived to find archived records and Restore record
to restore them. Use a viewer membership to confirm write controls are absent.
Pagination appears once the workspace has more than ten matching records.

## Automated and real-backend verification

From `frontend`:

```powershell
npm run typecheck
npm run lint
npm test
npm run build
..\backend\.venv\Scripts\python.exe -m ruff check --config ..\backend\pyproject.toml scripts/verify_document_api.py
..\backend\.venv\Scripts\python.exe -m ruff format --check --config ..\backend\pyproject.toml scripts/verify_document_api.py
..\backend\.venv\Scripts\python.exe scripts/verify_document_api.py
```

The real API script requires the existing local backend/proxy and local development
PostgreSQL settings. It generates unique disposable users and records, uses real
HTTP requests through Vite, and removes only its fixture records afterward. It
does not reset the database. It refuses a non-development or non-loopback database.
Authorization and expiry tests adjust only its generated accounts.

For optional manual browser testing, `--keep-fixture` retains the disposable
accounts and writes their credentials to ignored `frontend/.local/stage3-fixture.json`.
Never commit or share that file. Delete any additional browser-created test records
before running `scripts/verify_document_api.py --cleanup`. Cleanup refuses unknown
documents in the fixture workspaces, protecting records outside its tracked set.

Verification for this implementation:

- 66 frontend tests passed, 0 failed, across seven test files.
- 169 existing backend tests passed, 0 failed. One Starlette/httpx dependency
  deprecation warning was reported; backend dependencies were not changed.
- TypeScript, frontend lint, production build, and Ruff lint/format checks for
  the new Python verification script passed.
- 53 real HTTP/PostgreSQL checks passed, 0 failed: format creation, CRUD, archive
  and restore, filters, pagination, role denials, isolation, validation, CSRF,
  role downgrade, and session expiry.
- Browser verification exercised creation, editing, archive/filter/restore,
  confirmed deletion, workspace switching, and viewer read-only access.
- Layouts checked at 1440×1000 and 390×844 without horizontal page overflow.
- Disposable verification users, workspaces, and documents were removed afterward.

Controlled HTTP responses exist only in automated frontend tests. The running
application uses FastAPI responses; there are no fabricated document records,
statistics, or AI features.
