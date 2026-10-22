# AgentSphere AI backend — Module 5 and Module 6 Stage 1

FastAPI provides cookie-based authentication, workspace permissions, document
metadata CRUD, private multipart uploads, processing status, extracted sections,
and authorized original downloads. A separate Python worker uses PostgreSQL jobs
for bounded PDF/DOCX/TXT/Markdown extraction and durable file cleanup.

Start with the [Module 5 handover](../docs/MODULE5_STAGE5.md) for architecture,
security boundaries, verification results, recovery, and the migration checklist.
The [Module 3 guide](../docs/MODULE3.md) explains authentication and permissions;
the [Module 2 guide](../docs/MODULE2.md) explains SQLAlchemy and migrations.

[Module 6 Stage 1](../docs/MODULE6_STAGE1.md) adds PostgreSQL search schema and
validated data/configuration contracts only. Code migration head is now **0004**.
No tokenizer, embedding model, Qdrant service, vector worker, semantic-search
route, or search interface is installed or started by this foundation. Keep
`SEARCH_ENABLED=false` and `SEARCH_INDEXING_ENABLED=false`.

## Existing local development environment

The existing development database is **0002**. Keep it there until backup and
migration receive separate approval. Metadata, accounts, and workspaces work at
0002; file APIs fail closed and the worker refuses to start. Do not run `init`,
`alembic upgrade`, or a worker against that database as a setup shortcut.

From this `backend` directory, using the existing Poetry environment:

```powershell
.\.venv\Scripts\python.exe scripts/local_postgres.py start
$env:APP_ORIGIN = 'http://127.0.0.1:5173'
.\.venv\Scripts\python.exe -m uvicorn agentsphere.main:create_app --factory --host 127.0.0.1 --port 8001 --no-proxy-headers
```

The existing ignored `.env` supplies local credentials and explicitly permits
loopback HTTP. Vite must proxy `/api` to port 8001; see the
[frontend instructions](../frontend/README.md). Use the exact `127.0.0.1` hostname.
The configured origin, Host checks, HttpOnly cookies, and CSRF checks still apply.
Use the frontend at [http://127.0.0.1:5173/](http://127.0.0.1:5173/).
The health endpoint is `/health/live`. Swagger is at
[http://127.0.0.1:8001/docs](http://127.0.0.1:8001/docs), but direct authentication
requests there do not match the frontend origin in this configuration.

## Complete file workflow without changing development data

Install dependencies with `poetry install --with lint` when setting up a fresh checkout.
The existing `.venv\Scripts\python.exe` is used below. These drivers require the
existing local cluster and its ignored configuration; they create their own
databases, apply code head **0004 only there**, and allocate temporary private
storage. Explicit legacy migration tests also cover populated 0002 and 0003
schemas. These drivers never upgrade the normal development database.

```powershell
# Complete backend suite, including clean/populated migration tests:
.\.venv\Scripts\python.exe scripts/verify_stage2.py -q
# Real HTTP API and separate worker, all four formats and failure recovery:
.\.venv\Scripts\python.exe scripts/verify_processing.py
# From ../frontend, real browser + API + worker workflow:
npm run test:e2e
# To watch the disposable application while tests run:
npm run test:e2e -- --headed --workers=1
```

The browser harness uses ports 8002/5174 and removes only its own resources.
Generated test passwords, fixtures, logs, and reports stay local and ignored.
Do not publish reports containing generated action labels. An externally killed
harness can leave disposable resources; investigate their exact ownership before
cleanup. Do not issue wildcard database or storage deletion commands.

## Worker, only for an approved 0003 or 0004 environment

After a separate backup/migration approval, API and worker must use the same
0003 or 0004 database and absolute private storage root. The Module 6 compatibility
changes accept both known revisions at worker startup and in the reconciliation
health check; extraction and file-cleanup algorithms are unchanged. It does not
create search sources or consume vector jobs. Never mount that root as static
content. See `.env.example` for bounded extraction settings and secure defaults.

```powershell
.\.venv\Scripts\python.exe -m agentsphere.worker
# One available job attempt; exit success does not mean every document is ready:
.\.venv\Scripts\python.exe -m agentsphere.worker --once
# Conservative maintenance; queues original cleanup, then exits:
.\.venv\Scripts\python.exe -m agentsphere.worker --reconcile
```

Stop API/worker with Ctrl+C. Expired leases allow another worker to recover after
a crash. Reconciliation retains uncertain or referenced objects. It does not
purge upload idempotency tombstones. Parsing is not antivirus scanning or a full
OS security sandbox. Production needs HTTPS, private filesystem permissions,
backups, process supervision, and request/concurrency/disk controls.

## Static checks

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
poetry check --lock
.\.venv\Scripts\python.exe -m alembic heads
```

Use the disposable drivers for `alembic check` and integration pytest rather than
pointing ad hoc migration/test commands at development. Do not commit `.env`,
`.local`, environments, originals, database files, or generated credentials.
