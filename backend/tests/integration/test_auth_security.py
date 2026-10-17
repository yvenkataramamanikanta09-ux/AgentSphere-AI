from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from conftest import PASSWORD, bootstrap, register_and_login
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentsphere.models import AuthSession, RateLimitBucket, User, Workspace
from agentsphere.security import PASSWORD_HASHER, digest

pytestmark = pytest.mark.postgres
AUTH = "/api/v1/auth"


def signup_data(email="person@example.com"):
    return {"email": email, "password": PASSWORD, "display_name": "Person"}


def test_registration_login_rotation_and_hash_storage(client_factory, db_env):
    client = client_factory()
    bootstrap(client)
    anonymous = client.cookies.get("__Host-agentsphere_session")
    old_csrf = client.headers["X-CSRF-Token"]
    registered = client.post(f"{AUTH}/register", json=signup_data("Person@EXAMPLE.com"))
    assert registered.status_code == 201
    assert registered.json()["user"]["email"] == "person@example.com"
    assert client.get(f"{AUTH}/me").status_code == 401
    assert client.post(f"{AUTH}/register", json=signup_data()).status_code == 409
    logged_in = client.post(
        f"{AUTH}/login", json={"email": "PERSON@example.com", "password": PASSWORD}
    )
    assert logged_in.status_code == 200
    cookie = logged_in.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=43200"):
        assert flag in cookie
    assert "Domain=" not in cookie
    token = client.cookies.get("__Host-agentsphere_session")
    assert token != anonymous and len(token) == 43
    assert logged_in.json()["csrf_token"] != old_csrf
    assert logged_in.headers["cache-control"] == "no-store"
    assert PASSWORD not in logged_in.text and token not in logged_in.text
    with Session(db_env) as db:
        user = db.scalar(select(User))
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD_HASHER.verify(user.password_hash, PASSWORD)
        assert db.scalar(select(func.count()).select_from(Workspace)) == 1
        rows = db.scalars(select(AuthSession)).all()
        assert all(row.token_digest not in {token, anonymous} for row in rows)
        assert db.scalar(
            select(AuthSession).where(AuthSession.token_digest == digest(anonymous))
        ).revoked_at
    # Pre-login CSRF token cannot authorize authenticated writes.
    assert client.post(f"{AUTH}/logout").status_code == 403


@pytest.mark.parametrize("cookie", [None, "bad", "a" * 43])
def test_missing_malformed_forged_sessions(client_factory, cookie):
    client = client_factory()
    if cookie:
        client.cookies.set("__Host-agentsphere_session", cookie)
    assert client.get(f"{AUTH}/me").status_code == 401


@pytest.mark.parametrize("endpoint", ["register", "login"])
@pytest.mark.parametrize("token", ["", "wrong", "different-session"])
def test_csrf_required_before_authentication(client_factory, endpoint, token):
    client = client_factory()
    bootstrap(client)
    if token == "different-session":
        token = bootstrap(client_factory()).json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token
    data = signup_data()
    if endpoint == "login":
        data.pop("display_name")
    assert client.post(f"{AUTH}/{endpoint}", json=data).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "https://localhost:8443.evil.example"},
        {"Referer": "https://evil.example/page"},
        {"Referer": "https://[malformed"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_hostile_origin_rejected(db_client, headers):
    assert db_client.post(f"{AUTH}/logout", headers=headers).status_code == 403
    assert db_client.get(f"{AUTH}/me").status_code == 200


def test_matching_origin_and_referer_accepted(db_client):
    assert (
        db_client.get(f"{AUTH}/me", headers={"Referer": "https://localhost:8443/docs"}).status_code
        == 200
    )
    assert (
        db_client.post(f"{AUTH}/logout", headers={"Origin": "https://localhost:8443"}).status_code
        == 204
    )


@pytest.mark.parametrize("seconds", [1800, 43200])
def test_expiry_boundaries(db_client, clock, seconds):
    clock[0] += timedelta(seconds=seconds)
    assert db_client.get(f"{AUTH}/me").status_code == 401


def test_activity_extends_idle_but_not_absolute(client_factory, clock):
    client = register_and_login(
        client_factory(session_idle_seconds=120, session_absolute_seconds=300)
    )
    for _ in range(2):
        clock[0] += timedelta(seconds=100)
        assert client.get(f"{AUTH}/me").status_code == 200
    clock[0] += timedelta(seconds=100)
    assert client.get(f"{AUTH}/me").status_code == 401


def test_anonymous_expiry_and_bootstrap_rotation(client_factory, clock):
    client = client_factory()
    first = bootstrap(client).json()["csrf_token"]
    clock[0] += timedelta(seconds=600)
    assert client.post(f"{AUTH}/register", json=signup_data()).status_code == 403
    assert bootstrap(client).json()["csrf_token"] != first


def test_logout_and_logout_all_revoke_server_side(db_client, client_factory):
    other = client_factory()
    bootstrap(other)
    response = other.post(
        f"{AUTH}/login", json={"email": db_client.login_email, "password": PASSWORD}
    )
    assert response.status_code == 200
    other.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    stolen = other.cookies.get("__Host-agentsphere_session")
    assert other.post(f"{AUTH}/logout").status_code == 204
    other.cookies.set("__Host-agentsphere_session", stolen)
    assert other.get(f"{AUTH}/me").status_code == 401
    assert db_client.get(f"{AUTH}/me").status_code == 200
    other.cookies.clear()
    bootstrap(other)
    response = other.post(
        f"{AUTH}/login", json={"email": db_client.login_email, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert db_client.post(f"{AUTH}/logout-all").status_code == 204
    assert other.get(f"{AUTH}/me").status_code == 401


def test_inactive_user_and_generic_credentials(db_client, client_factory, db_env):
    with Session(db_env) as db, db.begin():
        db.get(User, UUID(db_client.user_id)).is_active = False
    assert db_client.get(f"{AUTH}/me").status_code == 401
    anon = client_factory()
    bootstrap(anon)
    responses = [
        anon.post(f"{AUTH}/login", json={"email": email, "password": password})
        for email, password in [
            (db_client.login_email, PASSWORD),
            (db_client.login_email, "incorrect"),
            ("absent@example.com", "incorrect"),
        ]
    ]
    assert all(response.status_code == 401 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)


def test_password_rehash_on_login(db_client, client_factory, db_env):
    weak = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1).hash(PASSWORD)
    with Session(db_env) as db, db.begin():
        db.get(User, UUID(db_client.user_id)).password_hash = weak
    anon = client_factory()
    bootstrap(anon)
    assert (
        anon.post(
            f"{AUTH}/login", json={"email": db_client.login_email, "password": PASSWORD}
        ).status_code
        == 200
    )
    with Session(db_env) as db:
        encoded = db.get(User, UUID(db_client.user_id)).password_hash
        assert not PASSWORD_HASHER.check_needs_rehash(encoded)


def test_password_input_not_reflected_and_body_limit(client_factory):
    client = client_factory()
    bootstrap(client)
    sentinel = "private-sentinel-" + "x" * 150
    response = client.post(f"{AUTH}/register", json=signup_data() | {"password": sentinel})
    assert response.status_code == 422 and sentinel not in response.text
    assert client.post(f"{AUTH}/login", content=b"x" * 9000).status_code == 413


def test_rate_limit_shared_across_apps_and_persists_failure(client_factory, clock, db_env):
    clients = [client_factory(login_account_limit=2), client_factory(login_account_limit=2)]
    for client in clients:
        bootstrap(client)
    data = {"email": "unknown@example.com", "password": "incorrect"}
    assert clients[0].post(f"{AUTH}/login", json=data).status_code == 401
    assert (
        clients[1].post(f"{AUTH}/login", json=data | {"email": "UNKNOWN@example.com"}).status_code
        == 401
    )
    response = clients[0].post(f"{AUTH}/login", json=data)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    with Session(db_env) as db:
        bucket = db.scalar(select(RateLimitBucket).where(RateLimitBucket.scope == "login_account"))
        assert bucket.count == 3 and "unknown" not in bucket.identity_digest
    clock[0] += timedelta(seconds=900)
    bootstrap(clients[0])
    assert clients[0].post(f"{AUTH}/login", json=data).status_code == 401


def test_csrf_and_registration_limits(client_factory):
    client = client_factory(csrf_ip_limit=1, registration_ip_limit=1)
    bootstrap(client)
    assert client.get(f"{AUTH}/csrf").status_code == 429
    assert client.post(f"{AUTH}/register", json=signup_data()).status_code == 201
    assert client.post(f"{AUTH}/register", json=signup_data("other@example.com")).status_code == 429


def test_rate_limit_atomic_under_concurrency(client_factory):
    clients = [client_factory(csrf_ip_limit=3) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda c: c.get(f"{AUTH}/csrf").status_code, clients))
    assert codes.count(200) == 3 and codes.count(429) == 5


def test_duplicate_registration_race_is_atomic(client_factory, db_env):
    clients = [client_factory(), client_factory()]
    for client in clients:
        bootstrap(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(
            pool.map(lambda c: c.post(f"{AUTH}/register", json=signup_data()).status_code, clients)
        )
    assert sorted(codes) == [201, 409]
    with Session(db_env) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.scalar(select(func.count()).select_from(Workspace)) == 1


def test_secure_mode_rejects_http_and_wrong_host(client_factory):
    client = client_factory()
    assert client.get("http://localhost:8443/api/v1/auth/csrf").status_code == 403
    assert client.get(f"{AUTH}/csrf", headers={"Host": "evil.example"}).status_code == 403


def test_long_display_email_uses_same_rate_bucket(client_factory):
    client = client_factory(login_account_limit=1)
    bootstrap(client)
    data = {"email": "unknown@example.com", "password": "incorrect"}
    assert client.post(f"{AUTH}/login", json=data).status_code == 401
    alternate = "A" * 300 + " <unknown@example.com>"
    assert client.post(f"{AUTH}/login", json=data | {"email": alternate}).status_code == 429
