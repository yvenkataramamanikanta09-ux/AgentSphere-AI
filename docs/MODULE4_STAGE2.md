# Module 4 — Stage 2: Workspace dashboard

Stage 2 replaces the account confirmation with a responsive workspace dashboard.
Every workspace and role displayed by the application comes from FastAPI.
Workspace creation persists a real PostgreSQL record. This stage does not add
document CRUD, membership editing, analytics, or AI features.

## Concepts and data flow

A component renders the interface. A hook coordinates asynchronous data and
selection. The shared API client sends the existing HttpOnly cookie automatically
and attaches an in-memory CSRF token to writes. FastAPI checks authentication and
authorization before accessing PostgreSQL.

`GET /api/v1/workspaces?limit=20&offset=0` returns `{items,limit,offset,has_more}`.
The dashboard loads additional pages only when requested. Each item includes
`id`, `name`, `owner_user_id`, `created_at`, and the current user's `role`.

The active UUID is stored in `?workspace=...`, allowing a selection to survive
reloads and be bookmarked. A separate `GET /api/v1/workspaces/{id}` validates that
selection, including selections outside the first list page. A UUID in a URL is
never treated as permission. A 404 removes selected permission controls and asks
the user to choose another workspace. Cancelled or late responses cannot replace
a newer selection. Refresh and returning focus to the window recheck access.

`POST /api/v1/workspaces` accepts only `{name}`. The form trims surrounding
whitespace and validates 1–100 characters. The server sets ownership. Every
authenticated user, including a viewer of another workspace, can create a new
workspace they own. A successful response selects the new workspace and refreshes
the list. Failed requests retain the input and never announce success.

## Roles and sessions

The sidebar shows ownership, editor, or viewer permission navigation according to
the verified active workspace. The corresponding panel explains that role.
Role labels describe backend permissions; this stage does not expose membership
or document editing controls. The backend remains responsible for enforcement.

A protected 401 clears CSRF state and signals the authentication provider. The
dashboard unmounts, dropping workspace state, and returns to login with an expiry
message. Invalid login credentials do not trigger that global signal. On window
focus, `/auth/me` rechecks the session. Transient connection errors do not silently
sign out the user.

Login replaces the anonymous CSRF token with the server's rotated token. On a
403, the rejected mutation is not replayed automatically. A deliberate retry
obtains a fresh token first. A generation counter prevents a late CSRF bootstrap
from restoring a token invalidated by logout or expiry. Cookies, passwords, and
CSRF tokens are never stored in localStorage or URLs.

## Files

| File | Purpose |
| --- | --- |
| `frontend/src/api/client.ts` | Shared HTTP, CSRF lifecycle, and session-expiry notification |
| `frontend/src/api/workspaces.ts` | Typed workspace list, retrieval, and creation calls |
| `frontend/src/features/auth/AuthContext.tsx` | Session restoration, expiry handling, and focus checks |
| `frontend/src/features/workspaces/useWorkspaces.ts` | Pagination, active selection, loading/error states, request cancellation |
| `frontend/src/features/workspaces/WorkspaceDashboard.tsx` | Sidebar, top navigation, workspace cards, profile, and permission panels |
| `frontend/src/features/workspaces/CreateWorkspaceForm.tsx` | Validated create form with success/error behavior |
| `frontend/src/features/workspaces/dashboard.css` | Desktop columns and mobile navigation/layout |
| `frontend/src/App.tsx` | Connects the protected root route to the dashboard |
| `frontend/src/api/workspaces.test.ts` | API contracts, CSRF retry/rotation, and expiry events |
| `frontend/src/features/workspaces/WorkspaceDashboard.test.tsx` | Component flows integrated with the shared HTTP client |
| `frontend/src/features/auth/AuthPages.test.tsx` | Existing login/restoration tests adapted to the dashboard |
| `frontend/scripts/verify_workspace_api.py` | Reproducible live API/PostgreSQL checks with disposable fixtures |
| `frontend/README.md` | Updated frontend overview and link to this guide |
| `docs/MODULE4_STAGE2.md` | Architecture, file inventory, run instructions, and verification results |

## Run and verify

Use two PowerShell terminals. Existing local database configuration from Modules
2–3 is required. These commands do not rewrite `.env`.

Backend terminal:

```powershell
cd C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\backend
.\.venv\Scripts\python.exe scripts/local_postgres.py start
$env:APP_ORIGIN = 'http://127.0.0.1:5173'
.\.venv\Scripts\python.exe -m uvicorn agentsphere.main:create_app --factory --host 127.0.0.1 --port 8001 --no-proxy-headers
```

Frontend terminal:

```powershell
cd C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\frontend
npm ci
$env:BACKEND_TARGET = 'http://127.0.0.1:8001'
npm run dev
```

Open http://127.0.0.1:5173/ and sign in with your own account. Vite retains the
browser-facing Host on proxied `/api` requests, matching FastAPI's `APP_ORIGIN`.
HTTP is explicitly permitted only for local development; deployed sessions use
the existing HTTPS settings. Use port 8001 to preserve an existing server on 8000.

1. Confirm your profile and accessible workspaces appear.
2. Select a workspace through its card or the top selector; reload to verify the URL selection.
3. Create a named workspace; confirm its role is owner and the list updates.
4. Check the sidebar and permission panel with owner/editor/viewer accounts.
5. At a narrow viewport, open Menu and select a navigation link.
6. Sign out, then confirm the dashboard is no longer accessible.

Frontend checks, run in `frontend`:

```powershell
npm run typecheck
npm run lint
npm test
npm run build
```

Live API checks, with both servers running, from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe frontend/scripts/verify_workspace_api.py
```

The script creates three uniquely identified local PostgreSQL users as fixtures,
then logs in and tests the real HTTP API. It tests owner/editor/viewer responses,
creation, isolation, invalid names, missing CSRF, rotation, and expiry. By default
it removes its own users/workspaces/memberships/sessions in `finally`. It refuses
cleanup if fixture identity changed or documents were added to those workspaces.
No existing accounts or document records are modified.

For a temporary manual browser review, `--keep-fixture` saves generated test
credentials in ignored `frontend/.local/stage2-fixture.json` without printing them.
After the review, sign out and run the same script with `--cleanup`. Do not share
or stage that file. Anonymous bootstrap sessions and rate counters expire under
the existing Module 3 policy.

Component tests use HTTP fixtures only inside tests. The running application has
no mock backend or fallback workspace data. Browser and HTTP checks complement
unit tests; the production build alone does not verify server integration.

Verified on 2026-10-17: **29 frontend tests passed, 0 failed** across four test
files. TypeScript, Oxlint, and the production build passed. Ruff lint and format
checks passed for the Python live-check script. The live FastAPI/PostgreSQL run
passed **25 checks, 0 failed**. The real browser flow verified login, workspace
creation and selection, reload restoration, viewer navigation, mobile menu, and
logout. Desktop (1440 pixels) and mobile (390 pixels) layouts fit without
horizontal overflow. All disposable fixture users and workspaces were removed
after the review; screenshots remain only in ignored `frontend/.local/`.
