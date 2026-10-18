# Module 5 Stage 2 — Secure Upload API and File Lifecycle

Stage 2 accepts private originals and queues durable extraction work. It does **not**
extract text, run workers, perform malware scanning, or provide a React upload interface.
Queued uploads are intentionally unavailable for download.

## Architecture and existing behavior

The existing document metadata routes, response fields, workspace permissions,
session cookies, CSRF rotation, and registered/archived lifecycle remain compatible.
`document_files.processing_status` is independent of `documents.status`.

New code uses the additive Stage 1 migration **0003**; there is no new migration.
It creates a document, its single attachment, and a queued `extract` job in one
PostgreSQL transaction. Storage publication happens first, using an unrelated
server-generated UUID and `PrivateStorage.put`. The persisted size and SHA-256
come from measured bytes, never client metadata.

Authentication, CSRF, editor permission, and schema readiness are checked before
reading any upload bytes. That short transaction releases its locks before body
transfer. A second transaction checks the same session, CSRF token, user, workspace,
and role again before finalization. Existing workspace locks serialize finalization
with membership changes. An upload whose permission/session expires during transfer
is rejected and its unreferenced original is compensated.

Metadata-only CRUD continues to work at revision 0002. New file endpoints return
503 until the separately approved migration is applied. Schema checks are read-only;
application startup never upgrades or creates tables.

## Changed files and their purposes

Paths in this table are relative to `backend/` unless prefixed with `docs/`.

| Files | Purpose |
| --- | --- |
| `src/agentsphere/api/document_files.py` (new) | Upload, attachment metadata, gated original download, commit/compensation coordination |
| `src/agentsphere/document_lifecycle.py` (new) | Readiness/schema checks, scoped retry identities, durable deletion scheduling |
| `src/agentsphere/uploads.py` (new) | Bounded private multipart intake, basic evidence checks, cancellation-safe thread ownership |
| `src/agentsphere/api/document_records.py` | Preserve immutable measured fields and schedule file-aware deletion |
| `src/agentsphere/storage.py` | Owned private temporary-file context |
| `src/agentsphere/config.py`, `.env.example` | Bounded upload slots and receive deadline configuration |
| `src/agentsphere/main.py` | Register file routes and initialize per-app concurrency/storage state |
| `src/agentsphere/schemas/documents.py` | New upload/file responses without changing existing metadata responses |
| `pyproject.toml`, `poetry.lock` | Add and pin the streaming python-multipart parser |
| `scripts/verify_stage2.py` (new) | Create, migrate, check and remove only a new disposable verification database |
| `tests/test_uploads.py` (new) | Parser, format evidence, limits and cancellation tests |
| `tests/test_upload_cancellation.py` (new) | Successful/failing worker join and repeated-cancellation tests |
| `tests/test_private_storage.py`, `tests/test_config.py` | Extend scratch-file safety/failure and resource-control validation coverage |
| `tests/integration/test_document_uploads.py` (new) | Real PostgreSQL API lifecycle, role, isolation and adversarial tests |
| `tests/integration/test_upload_commit_cancellation.py` (new) | Cancellation while the actual PostgreSQL commit is blocked |
| `tests/integration/test_upload_failures.py` (new) | Compensation, integrity failures, capacity and revision 0002 compatibility |
| `docs/MODULE5_STAGE2.md` (new, repository root) | API, verification, migration procedure and known limitations |

No frontend source, existing migrations, or ORM schema definitions changed.

## Configuration

Use the existing ignored `backend/.env`; templates contain no working credentials.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_UPLOAD_BYTES` | `10000000` | Inclusive actual file-byte limit |
| `PRIVATE_STORAGE_ROOT` | Absolute `backend/.local/private_storage` | Private originals and temporary files |
| `UPLOAD_TIMEOUT_SECONDS` | `120` | Body receive/format-check deadline, 1–600 seconds |
| `UPLOAD_CONCURRENCY` | `2` | Concurrent uploads per application process, 1–8 |
| `DATABASE_URL` | Existing PostgreSQL configuration | Application database |
| `TEST_DATABASE_URL` | Separate loopback database ending `_test` | Verification connection template |

Storage is initialized lazily after authorization and schema checks. Do not serve
the storage root through FastAPI static files, Vite, or a reverse proxy. The root
must be absolute and private; Stage 1's UUID key validation, exclusive writes,
checksums, directory/link checks, restrictive permissions, and overwrite protection
remain in use. Multipart temporary handles also live under that private root.

## API contracts

All paths below start with `/api/v1/workspaces/{workspace_id}/documents`.
Authentication uses the existing HttpOnly session cookie. Mutating requests require
the current `X-CSRF-Token` and existing same-origin/Host protections. No new tokens
are stored in browser localStorage or URLs.

### `POST /uploads` → 202 Accepted

Required headers:

- `Idempotency-Key`: a fresh UUID for a new upload intent; retain it for retries.
- `X-CSRF-Token`: current authenticated token.
- `Content-Type: multipart/form-data; boundary=...`, normally generated by the client.

Body: exactly one file part named `file`; no additional file/form fields. Supply
its original filename and matching MIME type. Do not send a JSON metadata object
or declared size. Supported extensions are `.pdf`, `.docx`, `.txt`, `.md`, and
`.markdown`. Missing or `application/octet-stream` MIME is accepted when extension
and basic evidence agree; Markdown also permits `text/plain`. Canonical MIME types
are persisted.

Response shape:

```json
{
  "document": {
    "id": "<server-derived UUID>",
    "workspace_id": "<authorized workspace UUID>",
    "filename": "notes.txt",
    "content_type": "text/plain",
    "size_bytes": 15,
    "status": "registered",
    "created_at": "<timestamp>",
    "updated_at": "<timestamp>"
  },
  "file": {
    "id": "<server-generated UUID>",
    "document_id": "<document UUID>",
    "original_filename": "notes.txt",
    "sha256": "<64 hexadecimal characters>",
    "uploaded_by": "<user UUID>",
    "uploaded_at": "<timestamp>",
    "processing_status": "queued",
    "processed_at": null,
    "error_code": null,
    "warning_code": null,
    "download_available": false
  },
  "replayed": false
}
```

The `Location` header points at existing document metadata. The same user,
workspace, and nonce with the same original filename, canonical type, measured size,
and checksum returns the same record with `replayed: true`; no second extraction
job remains. Changed content with that nonce returns 409. Concurrent retries are
serialized by the existing authorization locks. A deleted upload's retained cleanup
job prevents reuse of its nonce; use a new UUID for a new document. Cleanup-job
retention must preserve these tombstones in a later worker/retention design.

### `GET /{document_id}/file` → 200

Returns the `file` object above for workspace owners/editors/viewers, without the
storage key, private path, parser internals, or raw exception messages. Missing
attachments return 404. Existing metadata detail/list JSON is unchanged.

### `GET /{document_id}/download` → 200 only after trusted processing

Owners/editors/viewers may retrieve an original only when its processing status is
`ready`, `processed_at` and `parser_version` exist, no processing error exists,
and a matching extraction job succeeded. Otherwise return 409. Stage 2 cannot
produce that state: a later approved worker must perform actual content validation
and processing. Positive download tests explicitly seed a trusted processing result
inside disposable fixtures; they do not represent a running extraction feature.

Before sending bytes, the server verifies the stored original's measured size and
SHA-256. Downloads use attachment disposition with a UTF-8 encoded filename,
`Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. Bytes are bounded
by the accepted file size and buffered for verification; no open descriptor crosses
the worker-thread boundary. A missing, changed, or unsafe private object returns 503.
An already-authorized in-flight response may finish after a subsequent revocation.

### Existing `PATCH /{document_id}` and `DELETE /{document_id}`

Owners/editors can rename, archive, and restore attachments using existing metadata
validation. Changing measured `size_bytes` or canonical `content_type` returns 409;
unchanged values remain accepted so the existing React metadata form is compatible.
Metadata-only records retain their old editing behavior.

Deletion atomically removes API visibility, cancels queued/running extraction work,
clears its leases, and queues durable `delete_files` work. Cascading file/section
deletion and job `document_id SET NULL` retain the independent private `storage_key`.
The original is **not physically deleted** by this request. The later worker will
execute cleanup. Viewers cannot upload, edit, or delete. Cross-workspace requests
return 404 without exposing whether a target document exists.

## Bounds and basic format evidence

- Count actual streamed file bytes, accepting exactly 10,000,000 by default.
- Bound the entire multipart body to the file limit plus 65,536 bytes, even with
  absent or misleading Content-Length. Bound headers, parts, filename, and boundary.
- Reject additional fields/files, duplicate/unsupported multipart headers,
  malformed or incomplete framing, encoded request bodies, empty files, and paths.
- Accept only safe existing metadata filenames. Never use one as a storage path.
- PDF: signature and ending-marker evidence only.
- DOCX: ZIP/package entry evidence; reject unsafe paths, encryption, obvious macro/
  embedded binary entries, duplicates, excessive entry counts, and expansion ratios.
  XML is not extracted or interpreted here.
- TXT/Markdown: strict UTF-8, reject unsupported controls and obvious binary signatures.

These are intake checks, **not full structural validation or a malware guarantee**.
No queued bytes are publicly retrievable. Total upload slots are per process;
distributed throttling, workspace quotas, antivirus/CDR, and edge proxy limits are
later deployment concerns. A busy process returns 503 with `Retry-After: 1`.
The multipart adapter extends Starlette parser callbacks; rerun the adversarial
intake tests whenever FastAPI, Starlette, or python-multipart is upgraded.

## Error handling and filesystem/database boundaries

| Status | Typical cause |
| --- | --- |
| 400 | Invalid multipart framing/parameters, interrupted body |
| 401 | Missing, expired, revoked, or changed authenticated session |
| 403 | CSRF/origin failure or viewer write attempt |
| 404 | Inaccessible workspace/document or missing attachment |
| 408 | Upload deadline exceeded |
| 409 | Reused nonce with changed content, deleted nonce, immutable metadata, unavailable download |
| 413 | Actual file/body byte limit exceeded |
| 415 | Unsupported or mismatching format/MIME/encoding |
| 422 | Invalid filename/UUID, empty file, failed basic evidence |
| 503 | Schema not ready, upload capacity, storage failure, database failure |

Temporary handles are owned until parsing and joined file operations finish,
including cancellation. Publication, commit, and compensation run as one joined
thread operation so cancellation cannot close a live source or race a committing
transaction. Native/repeated cancellation waits for side effects before unwinding.
A cancelled client may retry the nonce if the commit succeeded.

On database/authorization failure after publication, compensation checks for a
committed attachment before deleting any bytes. It first acquires the workspace
row lock held by finalization, then checks the attachment using a fresh READ
COMMITTED snapshot. This waits for an uncertain server-side commit to finish;
an immediate negative lookup alone would not prove that the upload rolled back.
The lock wait is bounded to two seconds. If the workspace or database state cannot
be established, including a lock timeout, compensation retains the private object
and logs a sanitized deferral warning instead of risking a committed original.
A failed deletion queues cleanup when PostgreSQL is available.

PostgreSQL and local files cannot form one atomic transaction: process death,
disk/DB outage, or machine failure can leave private orphan objects or stale
private temporary files requiring future reconciliation. A later reconciliation
worker must compare storage inventory with committed attachment/job references,
allow for active uploads before removing stale objects, and handle referenced
objects that become unavailable. Not every outage can leave a durable cleanup job.
Stage 2 does not claim crash-proof physical cleanup or file/database atomicity.

## Verification (isolated from development data)

From the repository's `backend` directory, using the existing Poetry environment:

```powershell
& .\.venv\Scripts\python.exe scripts/local_postgres.py start
& .\.venv\Scripts\python.exe scripts/verify_stage2.py -q
& .\.venv\Scripts\python.exe -m ruff check src tests scripts
& .\.venv\Scripts\python.exe -m ruff format --check src tests scripts
```

`verify_stage2.py` requires the existing ignored local cluster configuration. It
validates loopback URLs, creates a randomly named **new database**, upgrades only
that database to 0003, runs `alembic check`, and runs pytest with isolated random
schemas and a disposable private root. It drops only its own generated database in
`finally`. No existing database is upgraded, emptied, or reset. An externally killed
verifier may leave its named disposable database for separately reviewed cleanup.

Migration tests cover both a clean schema and populated Module 4 data. Upload tests
cover formats/bounds, invalid multipart/path/ZIP input, session/CSRF failures,
cross-workspace roles, mid-upload revocation, retries/concurrency, DB/storage
failures and compensation, downloads, immutable fields, and durable deletion.
Dedicated cancellation tests coordinate real threads and transactions.

From `frontend`:

```powershell
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
```

Existing Playwright flows use their own disposable PostgreSQL database. No React
upload controls or invented AI features have been added.

Completed verification:

| Check | Result |
| --- | --- |
| Complete backend pytest suite, disposable PostgreSQL upgraded to 0003 | **491 passed, 0 failed, 0 skipped** |
| Private storage tests (included above) | 62 passed |
| Multipart/format/cancellation tests (included above) | 89 passed |
| Upload lifecycle API/PostgreSQL cases (included above) | 68 passed |
| Dedicated thread-call/commit cancellation tests (included above) | 8 passed |
| Additional lifecycle failure/0002 compatibility tests (included above) | 12 passed |
| Existing frontend Vitest suite | **66 passed, 0 failed, 0 skipped** |
| Existing real HTTP metadata/auth/workspace regression checks | **53 passed, 0 failed** |
| Existing Chromium Playwright workflows, separate disposable database | **17 passed, 0 failed, 0 skipped** |
| Backend Ruff lint / formatting | Passed; 45 Python files formatted |
| Poetry lock validation | Passed |
| Frontend TypeScript / lint / production build | Passed |
| Clean/populated migration regression tests and Alembic schema consistency | Passed in disposable databases/schemas |
| Development database read-only verification | Still 0002; all seven recorded table count/hash snapshots unchanged |
| Secrets/artifact review | No configured real credentials, private keys, DB data, environments or generated artifacts among changed files |

Final commit review added a compensation barrier for uncertain PostgreSQL commits
and three real database regressions: wait/preserve after commit, clean after rollback,
and retain/log on lock timeout. The previous 488-test suite therefore became 491.
The complete backend suite, frontend tests, browser/HTTP checks, lint, formatting,
TypeScript, build, dependency lock and disposable migration consistency checks were
rerun successfully after this change. No development migration was performed.

The latest complete backend run took 189.74 seconds. One existing Starlette/httpx test
client deprecation warning remains. Playwright also reports Node's harmless
`NO_COLOR`/`FORCE_COLOR` precedence notice. Git may report normal Windows LF/CRLF
normalization notices; `git diff --check` passes. An initial browser setup attempt
encountered a stopped/interrupted PostgreSQL server; normal startup/recovery
completed and the subsequent full browser/HTTP run passed. No reset, reinitialization,
development migration, or development-data cleanup was performed.

## Development migration and backup procedure — separate approval required

**Do not run this procedure as part of Stage 2 verification.** The existing
development database remains at 0002. Obtain approval for backup and the development
upgrade first. Stop the development API and other database writers for a stable
snapshot; keep PostgreSQL running. Confirm its database name, revision 0002, and
record counts using a read-only connection.

After approval, from `backend`, this exact backup command reads the existing
connection configuration without printing credentials or placing them in command
arguments. It writes only inside ignored `.local/backups` and refuses an existing
output file. PostgreSQL 18 binaries are assumed for this workstation.

```powershell
@'
import hashlib, os, subprocess
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy.engine import make_url
from agentsphere.config import Settings

settings = Settings()
url = make_url(settings.database_url.get_secret_value())
if (url.drivername != 'postgresql+psycopg' or url.query
    or url.host not in {'127.0.0.1', 'localhost', '::1'}
    or url.database != 'agentsphere'):
    raise SystemExit('Unexpected development destination; backup refused.')
directory = Path('.local/backups')
directory.mkdir(parents=True, exist_ok=True)
archive = directory / ('agentsphere_0002_' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.dump')
environment = os.environ.copy()
environment.update(PGHOST=url.host, PGPORT=str(url.port or 5432),
                   PGDATABASE=url.database, PGUSER=url.username,
                   PGPASSWORD=url.password or '')
binary = Path(r'C:\Program Files\PostgreSQL\18\bin')
with archive.open('xb') as output:
    subprocess.run([str(binary / 'pg_dump.exe'), '--format=custom', '--no-password'],
                   stdout=output, env=environment, check=True)
subprocess.run([str(binary / 'pg_restore.exe'), '--list', str(archive)],
               stdout=subprocess.DEVNULL, check=True)
print('Backup:', archive.resolve())
print('SHA-256:', hashlib.sha256(archive.read_bytes()).hexdigest())
'@ | & .\.venv\Scripts\python.exe -
```

Stop if backup or archive listing fails; retain and identify an incomplete file,
never treat it as a valid backup. A list check is not a restore drill: restore the
archive into a separately named disposable database and compare counts before
approving a production-style migration. Do not restore over development.

After a verified backup and separate migration approval:

```powershell
& .\.venv\Scripts\python.exe -m alembic current
& .\.venv\Scripts\python.exe -m alembic upgrade 0003
& .\.venv\Scripts\python.exe -m alembic current
& .\.venv\Scripts\python.exe -m alembic check
```

Require 0003 and no schema differences; compare existing table counts/snapshots,
then restart the API. The migration is additive; do not automatically downgrade or
restore if a check fails. Stage 1 guards downgrade when new tables contain data.
No worker exists yet, so accepted originals remain queued and private.

## Remaining limitations

Full PDF/DOCX structural validation, extraction, retry execution, lease renewal,
physical cleanup, reconciliation, disk quotas, and React upload/progress UI are
not implemented. Validation does not guarantee that an original is harmless.
Downloads deliberately remain blocked for all normally created Stage 2 uploads.
Cancellation joins active I/O for safe ownership; a blocked OS operation may finish
after the receive deadline instead of being forcibly killed. Download verification
buffers up to the accepted file size per request; deployment quotas remain future work.
Production HTTPS and deployment/storage hardening still require a separate review.
