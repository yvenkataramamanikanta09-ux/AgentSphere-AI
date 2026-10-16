# AgentSphere AI — Module 1

A small FastAPI backend that validates declared document metadata. This module
provides two endpoints and automated tests. It does not upload files, inspect file
contents, persist data, authenticate users, or call AI services.

## Prerequisites

- Python 3.12–3.14 (development verification uses Python 3.12).
- Poetry 2.x.
- Internet access for the initial dependency installation. No paid API or key is needed.

## Install on Windows (PowerShell)

Open PowerShell in the `agentsphere-ai` project folder. If Poetry is already on
your PATH, skip this bootstrap block. Otherwise install it in an isolated tools
environment (the same approach used for this workspace):

```powershell
py -3.12 -m venv .tools\poetry
& .\.tools\poetry\Scripts\python.exe -m pip install "poetry>=2.0,<3.0"
Set-Alias poetry (Resolve-Path .\.tools\poetry\Scripts\poetry.exe).Path
```

The alias lasts only for this PowerShell session. In a new session, run the
`Set-Alias` command again from the project root. The tools environment is separate
from the application's `backend\.venv` environment.

```powershell
Set-Location backend
poetry env use "C:\Program Files\Python312\python.exe"
poetry install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
poetry run uvicorn agentsphere.main:create_app --factory --reload
```

Replace the interpreter path if your Python installation is elsewhere; `py -0p`
lists installed interpreters. `poetry install` uses the included dependency
lockfile to reproduce the resolved dependency versions.

Stop the development server with **Ctrl+C**.

## Install on macOS/Linux

With Python 3.12 and Poetry 2.x installed, run from the project root:

```bash
cd backend
poetry env use python3.12
poetry install
test -f .env || cp .env.example .env
poetry run uvicorn agentsphere.main:create_app --factory --reload
```

The project uses only Python dependencies, but these shell instructions are not
verified on macOS/Linux in this Windows workspace.

## Try the application

- Interactive API documentation: <http://127.0.0.1:8000/docs>
- Liveness endpoint: <http://127.0.0.1:8000/health/live>
- OpenAPI schema: <http://127.0.0.1:8000/openapi.json>

From a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live

$payload = @{
    filename = "proposal.pdf"
    content_type = "application/pdf"
    size_bytes = 1024
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri http://127.0.0.1:8000/api/v1/documents/validate `
    -ContentType "application/json" `
    -Body $payload
```

Successful liveness response (`200 OK`):

```json
{"status": "ok"}
```

Successful metadata response (`200 OK`):

```json
{
  "valid": true,
  "document": {
    "filename": "proposal.pdf",
    "content_type": "application/pdf",
    "size_bytes": 1024
  },
  "max_upload_bytes": 10000000
}
```

## Validation contract

All three fields are required. Unknown fields are rejected.

| Field | Rules |
| --- | --- |
| `filename` | String of 1–255 characters; filename only, no path; supported extension |
| `content_type` | String matching the extension; outer whitespace removed and case normalized |
| `size_bytes` | Strictly a positive JSON integer; strings, floats, booleans, and null are rejected |

| Format | Extensions (case insensitive) | Required declared content type |
| --- | --- | --- |
| PDF | `.pdf` | `application/pdf` |
| DOCX | `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` |
| TXT | `.txt` | `text/plain` |
| Markdown | `.md`, `.markdown` | `text/markdown` |

Filenames must not have surrounding whitespace, leading/trailing dots, control
or other nonprintable characters, or any of `< > : " / \ | ? *`. Windows device
names such as `CON.pdf` and `LPT1.txt` are rejected. Ordinary spaces inside a name
and printable Unicode characters are accepted. Filenames are preserved, not renamed.

This is an intentionally narrow content-type contract: MIME parameters such as
`; charset=utf-8` are not accepted. A declared MIME type is not file-content verification.

**Size convention:** 10 MB means **10,000,000 bytes** here (decimal MB, not MiB).
Exactly that size is accepted; 10,000,001 bytes is rejected by default.

- `422 Unprocessable Entity`: malformed JSON, missing/extra fields, invalid name,
  unsupported extension, MIME mismatch, or an invalid/nonpositive size.
- `413 Content Too Large`: structurally valid metadata declares a size above the
  configured limit.

Schema validation happens before the size policy check. A request with both an
invalid filename and an oversized declaration therefore returns `422`.

For an oversized declaration the response is:

```json
{"detail": "Declared file size exceeds the 10000000-byte limit."}
```

The limit applies to the **declared file size**, not the HTTP request body. Actual
upload limits and byte inspection belong to a later module.

## Configuration

`backend/.env.example` documents `MAX_UPLOAD_BYTES=10000000`. Copy it to
`backend/.env` to customize the positive integer limit. Process environment
variables override `.env`; the application also works with the default when no
`.env` exists. Invalid limits fail application startup rather than silently
disabling validation. Restart the server after a configuration change.

The `.env` path is resolved from the source file, so it does not depend on the
current working directory. Local `.env` files are ignored by Git.

## Run tests and code checks

From `backend`, with Poetry available:

```powershell
poetry run pytest -q
poetry check --lock
```

Optional lint and formatting tools are kept outside the default installation:

```powershell
poetry install --with lint
poetry run ruff check .
poetry run ruff format --check .
```

Tests use FastAPI's in-process HTTP client, so you do not need to start Uvicorn.
They use explicit configuration and temporary files, not your personal `.env`.
No test makes an external API call.

Coverage includes all formats, case normalization, Unicode filenames, invalid
names and paths, MIME mismatches, missing fields, malformed JSON, strict integer
validation, exact size boundaries, a custom limit, environment precedence,
invalid configuration, and the OpenAPI contract.

### Verification in this workspace

Verified on Windows with Python 3.12:

- `poetry install`: completed, including installation of the application package.
- `poetry run pytest -q`: **61 passed**, one upstream deprecation warning.
- `poetry check --lock`: passed.
- `python -m pip check` inside the application environment: no broken requirements.
- Live Uvicorn HTTP smoke check: health `200`, valid metadata `200`, oversized
  metadata `413`. The temporary server was stopped after verification.

Starlette 1.8.0 emits a deprecation warning when its test client uses HTTPX 0.28.1.
The tests pass; the warning is not suppressed. This is a test-client dependency
notice, not an API validation failure.

Ruff checks were **not run**: the optional package download stalled on both
installation attempts. The default install does not need Ruff. Install the
`lint` group later to run those checks.

The project has its own Git repository because the enclosing home-directory
repository contains unrelated changes and ignores all new files. No files were
staged, committed, or pushed during Module 1 implementation.

## Purpose of each file

| File | Purpose |
| --- | --- |
| `README.md` | Setup, API contract, commands, and learning notes |
| `.gitignore` | Excludes secrets, virtual environments, caches, and generated artifacts |
| `backend/.env.example` | Shareable configuration example with no credentials |
| `backend/pyproject.toml` | Package metadata, dependency ranges, pytest and Ruff settings |
| `backend/poetry.lock` | Exact resolved dependency versions and hashes; keep in version control |
| `backend/poetry.toml` | Keeps the application virtual environment inside `backend/.venv` |
| `backend/src/agentsphere/__init__.py` | Marks the application package |
| `backend/src/agentsphere/main.py` | Creates the FastAPI application and attaches routers/settings |
| `backend/src/agentsphere/config.py` | Loads and validates environment settings; exposes a dependency |
| `backend/src/agentsphere/api/__init__.py` | Marks the routes package |
| `backend/src/agentsphere/api/health.py` | Implements the liveness endpoint |
| `backend/src/agentsphere/api/documents.py` | Applies the configured size limit and returns validated metadata |
| `backend/src/agentsphere/schemas/__init__.py` | Marks the schema package |
| `backend/src/agentsphere/schemas/documents.py` | Pydantic request/response models and metadata validation |
| `backend/tests/conftest.py` | Reusable isolated HTTP client and request fixtures |
| `backend/tests/test_health.py` | Verifies the liveness response |
| `backend/tests/test_documents.py` | Verifies the document API's successful and unsuccessful requests |
| `backend/tests/test_config.py` | Verifies configuration defaults, overrides, and invalid settings |

`.tools/poetry` and `backend/.venv` are generated local environments, not source files.

## Understand the implementation

1. Uvicorn calls `create_app` because we pass `--factory`.
2. The factory creates validated settings and registers both routers.
3. FastAPI receives JSON and asks Pydantic to construct `DocumentMetadata`.
4. Field validators check the filename; a model validator compares MIME type
   with extension. Invalid requests produce `422` before the endpoint runs.
5. `Depends(get_settings)` provides this application's size policy to the route.
6. The route applies that policy and returns a typed response or `413`.

Fixed schema rules live in Pydantic. The configurable size limit lives in the
route because it is application policy. `create_app(settings)` lets tests supply
a small limit without changing your real configuration. These short async routes
do not block on file or network I/O; `async` itself does not make computation faster.

Before moving to Module 2, try these exercises:

- Predict the response to `size_bytes: true`, then verify it in `/docs`.
- Explain why a `.pdf` extension and `text/plain` declaration disagree.
- Set `MAX_UPLOAD_BYTES=100`, restart, and check sizes 100 and 101.
- Write a new test for an invalid filename and explain which layer rejects it.

Implementation references: [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/),
[Pydantic validators](https://docs.pydantic.dev/latest/concepts/validators/), and
[Poetry basic usage](https://python-poetry.org/docs/basic-usage/).
