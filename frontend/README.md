# AgentSphere AI frontend — Module 4, Stage 3

React + TypeScript authentication UI connected to the real FastAPI Module 3 API.
The app contains registration, login, session restoration, logout, and the
workspace dashboard and workspace-scoped document metadata library. See
[the Stage 3 guide](../docs/MODULE4_STAGE3.md) for document operations, permissions,
tests, and real API verification. The [Stage 2 guide](../docs/MODULE4_STAGE2.md)
explains workspace selection and creation. Actual file uploads are not included.

## Local development

Use PowerShell in the repository. Start the existing local PostgreSQL instance
first. The backend's `.env` already supplies local database credentials and a
generated rate-limit secret; never copy them into this frontend.

Terminal 1, in `backend`:

```powershell
.\.venv\Scripts\python.exe scripts/local_postgres.py start
$env:APP_ORIGIN = 'http://127.0.0.1:5173'
.\.venv\Scripts\python.exe -m uvicorn agentsphere.main:create_app --factory --host 127.0.0.1 --port 8001 --no-proxy-headers
```

Port 8001 keeps any existing backend on 8000 untouched. The temporary
PowerShell environment variable overrides `.env` for this process only.
The backend must see the frontend origin because its Host and Origin checks use
`APP_ORIGIN`. Use the exact `127.0.0.1` hostname on both ports.

Terminal 2, in `frontend`:

```powershell
npm ci
$env:BACKEND_TARGET = 'http://127.0.0.1:8001'
npm run dev
```

Open **http://127.0.0.1:5173/**. Vite proxies `/api` to
`http://127.0.0.1:8001` while retaining the browser-facing Host header. The
browser makes same-origin requests, so it stores the backend's HttpOnly cookie
for `127.0.0.1` and sends it automatically. There is no CORS override. Direct
authentication requests on port 8001 are rejected while `APP_ORIGIN` is 5173;
open the frontend on port 5173 for this setup.

When port 8000 is free, it is the proxy's default target; start FastAPI there
and omit `BACKEND_TARGET`.

## Security flow

On first write, `GET /api/v1/auth/csrf` creates an anonymous cookie and returns
a CSRF token. Registration creates a user and workspace but leaves the visitor
signed out. Login rotates the cookie and CSRF token; the client replaces its
in-memory token. On reload, `GET /api/v1/auth/me` restores the user, and a CSRF
request restores that session's token. Logout revokes the server-side session.
The frontend never reads the HttpOnly cookie or stores it or the CSRF token in
localStorage. For production, use the backend's HTTPS settings and serve the
frontend and `/api` under one HTTPS origin.

## Checks

```powershell
npm run typecheck
npm run lint
npm test
npm run build
```

Unit tests use controlled HTTP responses to check client behavior; the application
does not use mock data. The documented local run connects to FastAPI and PostgreSQL.
