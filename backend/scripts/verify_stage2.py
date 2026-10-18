"""Run Stage 2 checks in a newly created, disposable local PostgreSQL database.

Uses the existing ignored local cluster configuration without displaying passwords.
Never migrates the development database or an existing test database. Only the
random database created by this process is dropped, including after test failure.
Pass pytest arguments after this script, for example: verify_stage2.py -q.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from agentsphere.config import Settings

BACKEND = Path(__file__).resolve().parents[1]


def main() -> int:
    settings = Settings()
    if settings.database_url is None or settings.test_database_url is None:
        raise SystemExit("Configure separate local DATABASE_URL and TEST_DATABASE_URL first.")
    development = make_url(settings.database_url.get_secret_value())
    template = make_url(settings.test_database_url.get_secret_value())
    for url in (development, template):
        if (
            url.drivername != "postgresql+psycopg"
            or url.host not in {"localhost", "127.0.0.1", "::1"}
            or url.query
            or not url.database
            or not url.username
        ):
            raise SystemExit("This verifier requires explicit, query-free local PostgreSQL URLs.")
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_test", template.database)
        or template.database == development.database
    ):
        raise SystemExit("The test URL must name a separate database ending in _test.")
    config_path = BACKEND / ".local" / "postgres-config.json"
    if not config_path.is_file():
        raise SystemExit("The ignored local PostgreSQL cluster configuration is required.")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if template.port != config["port"] or development.port != config["port"]:
        raise SystemExit("Database ports must match the configured isolated local cluster.")

    database = "agentsphere_stage2_" + uuid4().hex + "_test"
    test_url = template.set(database=database).render_as_string(hide_password=False)
    created = False
    with psycopg.connect(
        host=template.host,
        port=template.port,
        dbname="postgres",
        user="agentsphere_admin",
        password=config["admin_password"],
        connect_timeout=3,
        autocommit=True,
    ) as admin:
        try:
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                    sql.Identifier(database), sql.Identifier(template.username)
                )
            )
            created = True
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database)
                )
            )
            print(f"Created disposable database: {database}", flush=True)
            with TemporaryDirectory(prefix="agentsphere_stage2_") as temporary:
                environment = os.environ.copy()
                environment["PRIVATE_STORAGE_ROOT"] = str(Path(temporary) / "private")
                environment["DATABASE_URL"] = test_url
                environment["TEST_DATABASE_URL"] = test_url
                for arguments in (("alembic", "upgrade", "0003"), ("alembic", "check")):
                    result = subprocess.run(
                        [sys.executable, "-m", *arguments],
                        cwd=BACKEND,
                        env=environment,
                        check=False,
                    )
                    if result.returncode:
                        return result.returncode
                # Integration fixtures require a different development URL as a
                # safety check. They connect only to TEST_DATABASE_URL and create
                # random schemas within the disposable database above.
                environment["DATABASE_URL"] = settings.database_url.get_secret_value()
                return subprocess.run(
                    [sys.executable, "-m", "pytest", *(sys.argv[1:] or ["-q"])],
                    cwd=BACKEND,
                    env=environment,
                    check=False,
                ).returncode
        finally:
            if created:
                # Both identifier and creation flag belong to this invocation;
                # no user-supplied or existing database can reach this statement.
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
                )
                print("Removed this run's disposable database.", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except psycopg.Error:
        raise SystemExit(
            "Local PostgreSQL verification failed; credentials were not printed."
        ) from None
