"""Verify real document CRUD against local FastAPI and PostgreSQL.

Use backend/.venv/Scripts/python.exe. Default: remove all disposable records.
--keep-fixture saves ignored browser credentials; --cleanup removes that fixture.
"""

import argparse
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

# isort: split
# Keep backend imports separate when linting this script from either project folder.
from agentsphere.config import Settings
from agentsphere.database import build_engine
from agentsphere.models import AuthSession, Document, Membership, User, Workspace
from agentsphere.security import hash_password

ORIGIN = "http://127.0.0.1:5173"
FIXTURE = Path(__file__).resolve().parents[1] / ".local" / "stage3-fixture.json"


def cleanup(engine, fixture):
    identities = [UUID(item["id"]) for item in fixture["users"]]
    records = [UUID(value) for value in fixture["documents"]]
    with Session(engine) as db, db.begin():
        for item in fixture["users"]:
            user = db.get(User, UUID(item["id"]))
            if not item["email"].startswith(f"module4-stage3-{fixture['run_id']}-") or (
                user and user.email != item["email"]
            ):
                raise RuntimeError("Fixture identity changed; cleanup refused.")
        owned = select(Workspace.id).where(Workspace.owner_user_id.in_(identities))
        unknown = db.scalar(
            select(Document.id)
            .where(Document.workspace_id.in_(owned), Document.id.not_in(records))
            .limit(1)
        )
        if unknown:
            raise RuntimeError(
                "Untracked documents remain in fixture workspace. "
                "Remove only your browser test record first."
            )
        db.execute(
            delete(Document).where(Document.workspace_id.in_(owned), Document.id.in_(records))
        )
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
    if settings.database_url is None or settings.environment != "development":
        raise SystemExit("An existing local development database is required.")
    if make_url(settings.database_url.get_secret_value()).host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise SystemExit("Only a loopback PostgreSQL database may be used.")
    if args.keep_fixture and args.cleanup:
        raise SystemExit("Choose either --keep-fixture or --cleanup.")
    engine = build_engine(settings.database_url.get_secret_value())
    if args.cleanup:
        try:
            cleanup(engine, json.loads(FIXTURE.read_text()))
            FIXTURE.unlink()
            print("Disposable Stage 3 users, documents and workspaces removed.")
        finally:
            engine.dispose()
        return
    if FIXTURE.exists():
        engine.dispose()
        raise SystemExit("An existing browser fixture must be cleaned up first.")

    fixture = {
        "run_id": uuid4().hex,
        "users": [],
        "documents": [],
        "password": secrets.token_urlsafe(24),
    }
    passed = 0

    def check(name, condition):
        nonlocal passed
        if not condition:
            raise AssertionError(name)
        passed += 1
        print("PASS " + name)

    try:
        with Session(engine) as db, db.begin():
            for role in ("owner", "editor", "viewer", "outsider"):
                user = User(
                    email=f"module4-stage3-{fixture['run_id']}-{role}@example.com",
                    password_hash=hash_password(fixture["password"]),
                    display_name=f"Stage Three {role.title()}",
                )
                db.add(user)
                db.flush()
                personal = Workspace(name="Personal workspace", owner_user_id=user.id)
                db.add(personal)
                db.flush()
                fixture["users"].append(
                    {
                        "id": str(user.id),
                        "email": user.email,
                        "role": role,
                        "personal_workspace": str(personal.id),
                    }
                )
        clients = [httpx.Client(base_url=ORIGIN, timeout=15) for _ in fixture["users"]]
        try:
            for client, identity in zip(clients, fixture["users"], strict=True):
                csrf = client.get("/api/v1/auth/csrf")
                check(identity["role"] + " bootstrap", csrf.status_code == 200)
                old = csrf.json()["csrf_token"]
                client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": old})
                logged = client.post(
                    "/api/v1/auth/login",
                    json={"email": identity["email"], "password": fixture["password"]},
                )
                check(identity["role"] + " login", logged.status_code == 200)
                check(identity["role"] + " CSRF rotation", logged.json()["csrf_token"] != old)
                client.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
            owner, editor, viewer, outsider = clients
            created = owner.post("/api/v1/workspaces", json={"name": "Document research review"})
            check("workspace creation", created.status_code == 201)
            workspace_id = created.json()["id"]
            fixture["workspace_id"] = workspace_id
            base = f"/api/v1/workspaces/{workspace_id}/documents"
            for identity in fixture["users"][1:3]:
                member = owner.post(
                    f"/api/v1/workspaces/{workspace_id}/members",
                    json={"user_id": identity["id"], "role": identity["role"]},
                )
                check(identity["role"] + " membership", member.status_code == 201)
            formats = [
                ("research.pdf", "application/pdf"),
                (
                    "proposal.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
                ("notes.txt", "text/plain"),
                ("findings.md", "text/markdown"),
            ]
            for filename, content_type in formats:
                response = owner.post(
                    base,
                    json={"filename": filename, "content_type": content_type, "size_bytes": 2048},
                )
                check("create " + filename, response.status_code == 201)
                fixture["documents"].append(response.json()["id"])
            target = base + "/" + fixture["documents"][0]
            for client, role in zip(clients[:3], ("owner", "editor", "viewer"), strict=True):
                check(role + " list", client.get(base).status_code == 200)
                check(role + " detail", client.get(target).status_code == 200)
            changed = editor.patch(
                target, json={"filename": "research-edited.pdf", "size_bytes": 4096}
            )
            check(
                "editor metadata update",
                changed.status_code == 200 and changed.json()["size_bytes"] == 4096,
            )
            check("archive", editor.patch(target, json={"status": "archived"}).status_code == 200)
            archived = owner.get(base, params={"status": "archived"}).json()["items"]
            check(
                "archived filtering",
                len(archived) == 1 and archived[0]["id"] == fixture["documents"][0],
            )
            registered = owner.get(base, params={"status": "registered"}).json()["items"]
            check("registered filtering", len(registered) == 3)
            first = owner.get(base, params={"limit": 1, "offset": 0}).json()
            second = owner.get(base, params={"limit": 1, "offset": 1}).json()
            check("pagination has_more", len(first["items"]) == 1 and first["has_more"])
            check("pagination offset", first["items"][0]["id"] != second["items"][0]["id"])
            check("restore", editor.patch(target, json={"status": "registered"}).status_code == 200)
            metadata = {
                "filename": "must-not-exist.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1,
            }
            check("viewer create denied", viewer.post(base, json=metadata).status_code == 403)
            check(
                "viewer edit denied",
                viewer.patch(target, json={"size_bytes": 2}).status_code == 403,
            )
            check(
                "viewer archive denied",
                viewer.patch(target, json={"status": "archived"}).status_code == 403,
            )
            check("viewer delete denied", viewer.delete(target).status_code == 403)
            check("outsider list denied", outsider.get(base).status_code == 404)
            check("outsider detail denied", outsider.get(target).status_code == 404)
            check(
                "outsider edit denied",
                outsider.patch(target, json={"size_bytes": 2}).status_code == 404,
            )
            check("outsider delete denied", outsider.delete(target).status_code == 404)
            for changes, label, expected in [
                ({"filename": "../bad.pdf"}, "path rejected", 422),
                ({"size_bytes": 0}, "zero size rejected", 422),
                ({"content_type": "text/plain"}, "MIME mismatch rejected", 422),
                ({"size_bytes": settings.max_upload_bytes + 1}, "configured size limit", 413),
            ]:
                check(label, owner.post(base, json=metadata | changes).status_code == expected)
            check(
                "missing CSRF rejected",
                owner.post(base, json=metadata, headers={"X-CSRF-Token": ""}).status_code == 403,
            )
            deleted = base + "/" + fixture["documents"][2]
            check("editor deletion", editor.delete(deleted).status_code == 204)
            check("deleted detail is missing", owner.get(deleted).status_code == 404)
            wrong = (
                f"/api/v1/workspaces/{fixture['users'][0]['personal_workspace']}"
                f"/documents/{fixture['documents'][0]}"
            )
            check("cross-workspace detail isolation", owner.get(wrong).status_code == 404)
            check(
                "cross-workspace edit isolation",
                owner.patch(wrong, json={"size_bytes": 2}).status_code == 404,
            )
            check("cross-workspace delete isolation", owner.delete(wrong).status_code == 404)
            role_change = owner.patch(
                f"/api/v1/workspaces/{workspace_id}/members/{fixture['users'][1]['id']}",
                json={"role": "viewer"},
            )
            check("editor downgrade", role_change.status_code == 200)
            check(
                "downgraded editor write denied",
                editor.patch(target, json={"size_bytes": 2}).status_code == 403,
            )
            with Session(engine) as db, db.begin():
                db.execute(
                    update(AuthSession)
                    .where(AuthSession.user_id == UUID(fixture["users"][2]["id"]))
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            check("expired session rejected", viewer.get(base).status_code == 401)
        finally:
            for client in clients:
                client.close()
        if args.keep_fixture:
            FIXTURE.parent.mkdir(exist_ok=True)
            FIXTURE.write_text(json.dumps(fixture), encoding="utf-8")
            print("Disposable browser credentials saved in ignored frontend/.local; not printed.")
        print(f"{passed} live document checks passed, 0 failed.")
    finally:
        if not args.keep_fixture or not FIXTURE.exists():
            cleanup(engine, fixture)
            print("All tracked disposable document records and accounts removed.")
        engine.dispose()


if __name__ == "__main__":
    main()
