"""Exercise the real local API with disposable PostgreSQL users and workspaces.

Run with backend/.venv/Scripts/python.exe from the repository root.
Default behavior removes all records it creates. --keep-fixture retains them
for a browser review; --cleanup removes only that saved fixture afterward.
"""

import argparse
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from agentsphere.config import Settings
from agentsphere.database import build_engine
from agentsphere.models import AuthSession, Document, Membership, User, Workspace
from agentsphere.security import hash_password
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

FIXTURE = Path(__file__).resolve().parents[1] / ".local" / "stage2-fixture.json"
ORIGIN = "http://127.0.0.1:5173"


def cleanup(engine, fixture):
    identities = [UUID(item["id"]) for item in fixture["users"]]
    with Session(engine) as db, db.begin():
        for item in fixture["users"]:
            user = db.get(User, UUID(item["id"]))
            if user and user.email != item["email"]:
                raise RuntimeError("Fixture identity changed; cleanup refused.")
            if not item["email"].startswith(f"module4-stage2-{fixture['run_id']}-"):
                raise RuntimeError("Fixture marker mismatch; cleanup refused.")
        owned = select(Workspace.id).where(Workspace.owner_user_id.in_(identities))
        if db.scalar(select(Document.id).where(Document.workspace_id.in_(owned)).limit(1)):
            raise RuntimeError("Documents exist in fixture workspace; cleanup refused.")
        db.execute(delete(Membership).where(Membership.workspace_id.in_(owned)))
        db.execute(delete(AuthSession).where(AuthSession.user_id.in_(identities)))
        db.execute(delete(Workspace).where(Workspace.owner_user_id.in_(identities)))
        db.execute(delete(User).where(User.id.in_(identities)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-fixture", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("The existing local DATABASE_URL is required.")
    if args.keep_fixture and args.cleanup:
        raise SystemExit("Choose either --keep-fixture or --cleanup.")
    engine = build_engine(settings.database_url.get_secret_value())
    if args.cleanup:
        try:
            cleanup(engine, json.loads(FIXTURE.read_text()))
            FIXTURE.unlink()
            print("Disposable Stage 2 fixture removed; existing accounts/documents preserved.")
        finally:
            engine.dispose()
        return
    if FIXTURE.exists():
        engine.dispose()
        raise SystemExit("A saved fixture already exists. Review and clean it up first.")

    fixture = {"run_id": uuid4().hex, "users": [], "password": secrets.token_urlsafe(24)}
    passed = 0

    def check(name, condition):
        nonlocal passed
        if not condition:
            raise AssertionError(name)
        passed += 1
        print("PASS " + name)

    try:
        # Fixture setup touches only these new UUIDs. All operations under test use HTTP.
        with Session(engine) as db, db.begin():
            for role in ("owner", "editor", "viewer"):
                user = User(
                    email=f"module4-stage2-{fixture['run_id']}-{role}@example.com",
                    password_hash=hash_password(fixture["password"]),
                    display_name=f"Stage Two {role.title()}",
                )
                db.add(user)
                db.flush()
                db.add(Workspace(name="Personal workspace", owner_user_id=user.id))
                fixture["users"].append({"id": str(user.id), "email": user.email, "role": role})

        clients = [httpx.Client(base_url=ORIGIN, timeout=15) for _ in fixture["users"]]
        try:
            for client, identity in zip(clients, fixture["users"], strict=True):
                csrf = client.get("/api/v1/auth/csrf")
                check(f"{identity['role']} CSRF bootstrap", csrf.status_code == 200)
                old = csrf.json()["csrf_token"]
                client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": old})
                logged = client.post(
                    "/api/v1/auth/login",
                    json={"email": identity["email"], "password": fixture["password"]},
                )
                check(f"{identity['role']} login", logged.status_code == 200)
                check(f"{identity['role']} CSRF rotation", logged.json()["csrf_token"] != old)
                client.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
            owner, editor, viewer = clients
            created = owner.post("/api/v1/workspaces", json={"name": "Stage 2 project review"})
            check("workspace creation", created.status_code == 201)
            workspace_id = created.json()["id"]
            check(
                "server assigns ownership",
                created.json()["owner_user_id"] == fixture["users"][0]["id"]
                and created.json()["role"] == "owner",
            )
            for identity in fixture["users"][1:]:
                response = owner.post(
                    f"/api/v1/workspaces/{workspace_id}/members",
                    json={"user_id": identity["id"], "role": identity["role"]},
                )
                check(identity["role"] + " fixture membership", response.status_code == 201)
            for client, identity in zip(clients, fixture["users"], strict=True):
                listing = client.get("/api/v1/workspaces?limit=20&offset=0")
                check(
                    identity["role"] + " accessible list",
                    listing.status_code == 200
                    and any(item["id"] == workspace_id for item in listing.json()["items"]),
                )
                selected = client.get(f"/api/v1/workspaces/{workspace_id}")
                check(
                    identity["role"] + " selected role",
                    selected.status_code == 200 and selected.json()["role"] == identity["role"],
                )
            check(
                "inaccessible UUID rejected",
                viewer.get(f"/api/v1/workspaces/{uuid4()}").status_code == 404,
            )
            check(
                "empty name rejected",
                owner.post("/api/v1/workspaces", json={"name": "   "}).status_code == 422,
            )
            missing = owner.post(
                "/api/v1/workspaces",
                json={"name": "Must not be created"},
                headers={"X-CSRF-Token": ""},
            )
            check("creation requires CSRF", missing.status_code == 403)
            own = viewer.post("/api/v1/workspaces", json={"name": "Viewer personal project"})
            check(
                "viewer can own a separate workspace",
                own.status_code == 201 and own.json()["role"] == "owner",
            )
            check(
                "separate workspace isolation",
                owner.get(f"/api/v1/workspaces/{own.json()['id']}").status_code == 404,
            )
            with Session(engine) as db, db.begin():
                db.execute(
                    update(AuthSession)
                    .where(AuthSession.user_id == UUID(fixture["users"][1]["id"]))
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            check(
                "expired session rejected",
                editor.get("/api/v1/workspaces?limit=20&offset=0").status_code == 401,
            )
            fixture["workspace_id"] = workspace_id
        finally:
            for client in clients:
                client.close()
        if args.keep_fixture:
            FIXTURE.parent.mkdir(exist_ok=True)
            FIXTURE.write_text(json.dumps(fixture), encoding="utf-8")
            print("Browser fixture saved in ignored frontend/.local; credentials not printed.")
        print(f"{passed} live API checks passed, 0 failed.")
    finally:
        if not args.keep_fixture or not FIXTURE.exists():
            cleanup(engine, fixture)
            print("Disposable users, memberships, sessions and workspaces removed.")
        engine.dispose()


if __name__ == "__main__":
    main()
