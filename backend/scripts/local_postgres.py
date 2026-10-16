"""Manage an isolated loopback-only PostgreSQL cluster using installed binaries.

No system services or existing clusters are changed. Generated passwords and data
stay in ignored backend/.local and backend/.env. No database is deleted by this script.
"""

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

BACKEND = Path(__file__).resolve().parents[1]
LOCAL = BACKEND / ".local"
DATA = LOCAL / "postgres"
CONFIG = LOCAL / "postgres-config.json"
ENV_FILE = BACKEND / ".env"


def pg_binary(directory: Path, name: str) -> str:
    path = directory / (name + (".exe" if os.name == "nt" else ""))
    if not path.is_file():
        raise SystemExit(f"PostgreSQL executable not found: {path}")
    return str(path)


def run(directory: Path, name: str, *arguments: str, check: bool = True) -> int:
    return subprocess.run(
        [pg_binary(directory, name), *arguments],
        check=check,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).returncode


def start(directory: Path, port: int) -> None:
    if run(directory, "pg_ctl", "-D", str(DATA), "status", check=False) == 0:
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    run(
        directory,
        "pg_ctl",
        "-D",
        str(DATA),
        "-l",
        str(LOCAL / "postgres.log"),
        "-o",
        f"-h 127.0.0.1 -p {port}",
        "-w",
        "start",
    )


def initialize(directory: Path, port: int) -> None:
    if DATA.exists() or CONFIG.exists() or ENV_FILE.exists():
        raise SystemExit(
            "Existing data/configuration found. Nothing overwritten. Use start instead."
        )
    for name in ("initdb", "pg_ctl"):
        pg_binary(directory, name)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    LOCAL.mkdir(exist_ok=True)
    admin_password = secrets.token_urlsafe(32)
    app_password = secrets.token_urlsafe(32)
    test_password = secrets.token_urlsafe(32)
    password_file = LOCAL / "init-password"
    with password_file.open("x", encoding="utf-8") as file:
        file.write(admin_password + "\n")
    try:
        run(
            directory,
            "initdb",
            "-D",
            str(DATA),
            "-U",
            "agentsphere_admin",
            "--pwfile",
            str(password_file),
            "--auth=scram-sha-256",
            "--encoding=UTF8",
            "--locale=C",
        )
    finally:
        password_file.unlink(missing_ok=True)
    with CONFIG.open("x", encoding="utf-8") as file:
        json.dump({"port": port, "admin_password": admin_password}, file)
    start(directory, port)
    with psycopg.connect(
        host="127.0.0.1",
        port=port,
        dbname="postgres",
        user="agentsphere_admin",
        password=admin_password,
        autocommit=True,
        connect_timeout=5,
    ) as connection:
        for name, password in (("agentsphere", app_password), ("agentsphere_test", test_password)):
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(name), sql.Literal(password))
            )
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier(name)
                )
            )
            connection.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )

    def url(database: str, password: str) -> str:
        return URL.create(
            "postgresql+psycopg",
            username=database,
            password=password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)

    with ENV_FILE.open("x", encoding="utf-8") as file:
        file.write(
            f"MAX_UPLOAD_BYTES=10000000\nDATABASE_URL={url('agentsphere', app_password)}\n"
            f"TEST_DATABASE_URL={url('agentsphere_test', test_password)}\n"
        )
    print(f"Created isolated PostgreSQL on 127.0.0.1:{port}.")
    print("Generated credentials are in ignored .env/.local files; they were not printed.")
    print("Next: poetry run alembic upgrade head")


def main() -> None:
    default_directory = (
        Path(shutil.which("pg_ctl")).parent
        if shutil.which("pg_ctl")
        else Path(r"C:\Program Files\PostgreSQL\18\bin")
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "start", "stop", "status"])
    parser.add_argument("--bin-dir", type=Path, default=default_directory)
    parser.add_argument("--port", type=int, default=55432, help="Only used by init.")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if args.action == "init":
        initialize(args.bin_dir, args.port)
        return
    if not CONFIG.exists():
        parser.error("No local cluster configuration; run init first.")
    port = json.loads(CONFIG.read_text(encoding="utf-8"))["port"]
    if args.action == "start":
        start(args.bin_dir, port)
    elif args.action == "stop":
        run(args.bin_dir, "pg_ctl", "-D", str(DATA), "-m", "fast", "-w", "stop")
    else:
        raise SystemExit(run(args.bin_dir, "pg_ctl", "-D", str(DATA), "status", check=False))


if __name__ == "__main__":
    main()
