"""Run real browser tests in a disposable local PostgreSQL database.

Uses the existing ignored local cluster configuration, never the development DB.
Only the database created by this run is dropped. Ignored HTML reports may include
generated disposable credentials in browser action labels.
"""

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

# isort: split
from agentsphere.config import BACKEND_DIR, Settings
from agentsphere.database import build_engine
from agentsphere.models import AuthSession, Document, Membership, User, Workspace
from agentsphere.security import hash_password

FRONTEND = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:5174"


def require_local_url(url):
    # SQLAlchemy applies query parameters after URL fields; dbname/host overrides
    # would otherwise bypass the disposable database and loopback checks.
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.query:
        raise SystemExit("E2E database URLs must be loopback URLs without query parameters.")


def wait_ready(process, url):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("E2E server exited; inspect ignored .local/e2e logs.")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise RuntimeError("E2E server did not become ready.")


def expire_user(user_id):
    """Test helper: only a marked account in this runner's disposable database."""
    url = make_url(os.environ["DATABASE_URL"])
    require_local_url(url)
    if not re.fullmatch(r"agentsphere_e2e_[0-9a-f]{32}", url.database or ""):
        raise SystemExit("Expiry helper refuses databases outside the E2E fixture.")
    engine = build_engine(url.render_as_string(hide_password=False))
    try:
        with Session(engine) as db, db.begin():
            user = db.get(User, UUID(user_id))
            if user is None or not user.email.startswith("stage4-expiry-"):
                raise SystemExit("Expiry helper refuses unmarked users.")
            db.execute(
                update(AuthSession)
                .where(AuthSession.user_id == user.id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    finally:
        engine.dispose()


def seed(url, password):
    engine = build_engine(url)
    result = {"users": {}}
    try:
        with Session(engine) as db, db.begin():
            users = {}
            roles = ("owner", "editor", "viewer", "expiry") + tuple(
                f"layout-{width}" for width in (1440, 1024, 768, 390, 320)
            )
            for role in roles:
                user = User(
                    email=f"stage4-{role}-{uuid4().hex}@example.com",
                    display_name=f"Browser {role.title()}",
                    password_hash=hash_password(password),
                )
                db.add(user)
                db.flush()
                users[role] = user
                personal = Workspace(name="Personal workspace", owner_user_id=user.id)
                db.add(personal)
                db.flush()
                result["users"][role] = {
                    "id": str(user.id),
                    "email": user.email,
                    "display_name": user.display_name,
                }
            workspace = Workspace(name="Browser research", owner_user_id=users["owner"].id)
            db.add(workspace)
            db.flush()
            result["workspace_id"] = str(workspace.id)
            for role in ("editor", "viewer", *roles[4:]):
                db.add(
                    Membership(
                        workspace_id=workspace.id,
                        user_id=users[role].id,
                        role="viewer" if role == "viewer" else "editor",
                    )
                )
            for index in range(12):
                db.add(
                    Document(
                        workspace_id=workspace.id,
                        filename=f"seed-{index:02}.pdf",
                        content_type="application/pdf",
                        size_bytes=2048,
                        status="archived" if index == 0 else "registered",
                        created_at=datetime.now(UTC) + timedelta(seconds=index),
                    )
                )
        return result
    finally:
        engine.dispose()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--expire-user":
        expire_user(sys.argv[2])
        return 0
    settings = Settings()
    if settings.environment != "development" or settings.test_database_url is None:
        raise SystemExit("Configure the existing local development TEST_DATABASE_URL first.")
    test_url = make_url(settings.test_database_url.get_secret_value())
    require_local_url(test_url)
    if test_url.host not in {"localhost", "127.0.0.1", "::1"} or not (
        test_url.database or ""
    ).endswith("_test"):
        raise SystemExit("Only a loopback database ending in _test is supported.")
    cluster = json.loads((BACKEND_DIR / ".local/postgres-config.json").read_text())
    if cluster["port"] != test_url.port:
        raise SystemExit("Local cluster and test database ports differ; nothing changed.")
    for port in (8002, 5174):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    node = shutil.which("node")
    cli = FRONTEND / "node_modules/@playwright/test/cli.js"
    if not node or not cli.is_file():
        raise SystemExit("Run npm ci and npx playwright install chromium first.")
    name = "agentsphere_e2e_" + uuid4().hex
    url = test_url.set(database=name).render_as_string(hide_password=False)
    env = os.environ.copy()
    installed_chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if "E2E_BROWSER_CHANNEL" not in env and installed_chrome.is_file():
        env["E2E_BROWSER_CHANNEL"] = "chrome"
    env.update(
        DATABASE_URL=url,
        APP_ORIGIN=ORIGIN,
        ALLOW_INSECURE_LOCAL_HTTP="true",
        ENVIRONMENT="development",
        MAX_UPLOAD_BYTES="10000000",
        AUTH_RATE_LIMIT_KEY=secrets.token_urlsafe(48),
        E2E_PASSWORD=secrets.token_urlsafe(30),
        E2E_PYTHON=sys.executable,
        E2E_ISOLATED="1",
        BACKEND_TARGET="http://127.0.0.1:8002",
    )
    admin = dict(
        host=test_url.host,
        port=test_url.port,
        user="agentsphere_admin",
        password=cluster["admin_password"],
        dbname="postgres",
        autocommit=True,
        connect_timeout=3,
    )
    created = False
    servers = []
    logs = []
    try:
        with psycopg.connect(**admin) as db:
            db.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                    sql.Identifier(name), sql.Identifier(test_url.username)
                )
            )
            created = True
        print("Created a unique disposable E2E database; development records are untouched.")
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env=env,
            check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "alembic", "check"], cwd=BACKEND_DIR, env=env, check=True
        )
        env["E2E_FIXTURE"] = json.dumps(seed(url, env["E2E_PASSWORD"]))
        log_dir = FRONTEND / ".local/e2e"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "backend.log").open("w", encoding="utf-8")
        logs.append(log)
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentsphere.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8002",
                "--no-proxy-headers",
            ],
            cwd=BACKEND_DIR,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        servers.append(server)
        wait_ready(server, "http://127.0.0.1:8002/health/live")
        frontend_log = (log_dir / "frontend.log").open("w", encoding="utf-8")
        logs.append(frontend_log)
        vite = subprocess.Popen(
            [
                node,
                str(FRONTEND / "node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                "5174",
                "--strictPort",
            ],
            cwd=FRONTEND,
            env=env,
            stdout=frontend_log,
            stderr=frontend_log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        servers.append(vite)
        wait_ready(vite, ORIGIN)
        subprocess.run(
            [sys.executable, "scripts/verify_document_api.py"],
            cwd=FRONTEND,
            env=env | {"VERIFICATION_ORIGIN": ORIGIN},
            check=True,
        )
        return subprocess.run(
            [node, str(cli), "test", *sys.argv[1:]], cwd=FRONTEND, env=env
        ).returncode
    finally:
        for server in reversed(servers):
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        for log in logs:
            log.close()
        if created:
            # name is generated above, never taken from user input or a saved fixture.
            with psycopg.connect(**admin) as db:
                db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            print("Removed only this run's disposable database.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (psycopg.Error, subprocess.CalledProcessError):
        # Driver exception text can contain connection details; keep it out of reports.
        raise SystemExit(
            "Local E2E setup failed; check PostgreSQL and the documented prerequisites."
        ) from None
