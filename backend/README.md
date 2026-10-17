# AgentSphere backend — Module 3

FastAPI stores **document metadata** in PostgreSQL with accounts, server-side
sessions, CSRF protection, and owner/editor/viewer workspace permissions.
There are no file uploads, RAG, LLM calls, or agents in this module.

Start with [the Module 3 guide](../docs/MODULE3.md) for configuration, authentication,
Swagger examples, migrations, and security tests. The
[Module 2 tutorial](../docs/MODULE2.md) explains the database foundation. The original
[Module 1 guide](../docs/MODULE1.md) remains available.

Run these commands in this `backend` directory using Python 3.12+ and Poetry 2:

```powershell
poetry install
# One-time setup using an installed PostgreSQL 18 distribution:
poetry run python scripts/local_postgres.py init
poetry run python scripts/configure_local_auth.py
poetry run alembic upgrade head
poetry run pytest -q
poetry run uvicorn agentsphere.main:create_app --factory --reload --host 127.0.0.1 --port 8000 --no-proxy-headers
```

The local setup creates separate development/test databases on port **55432**,
generates passwords, and writes ignored `.env` / `.local` files. It refuses to
overwrite existing configuration or data. It does not change the PostgreSQL
Windows service on port 5432. Use `start` instead of `init` on subsequent runs.

Open [Swagger UI](http://127.0.0.1:8000/docs). Stop Uvicorn with Ctrl+C and the
isolated database with `poetry run python scripts/local_postgres.py stop`.

If Poetry is not on PATH, activate its existing executable or substitute
`.\.venv\Scripts\python.exe -m pytest`, `-m alembic`, and `-m uvicorn` for the
respective `poetry run` commands after dependencies have been installed.

The HTTP quick start is explicitly limited to loopback development; HTTPS and
Secure cookies are the default for other environments. Never commit
`.env` or `.local`, and do not use the development credentials in a deployed app.
