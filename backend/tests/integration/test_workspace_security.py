from uuid import uuid4

import pytest
from conftest import register_and_login

pytestmark = pytest.mark.postgres


def add_member(owner, member, role):
    return owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/members",
        json={"user_id": member.user_id, "role": role},
    )


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
@pytest.mark.parametrize("operation", ["list", "get", "create", "patch", "delete"])
def test_document_permission_matrix(db_client, client_factory, metadata, role, operation):
    member = register_and_login(client_factory())
    if role != "outsider":
        assert add_member(db_client, member, role).status_code == 201
    created = db_client.post(db_client.documents_url, json=metadata)
    assert created.status_code == 201
    location = created.headers["location"]
    calls = {
        "list": lambda: member.get(db_client.documents_url),
        "get": lambda: member.get(location),
        "create": lambda: member.post(db_client.documents_url, json=metadata),
        "patch": lambda: member.patch(location, json={"status": "archived"}),
        "delete": lambda: member.delete(location),
    }
    if role == "outsider":
        expected = 404
    elif role == "viewer" and operation in {"create", "patch", "delete"}:
        expected = 403
    else:
        expected = {"list": 200, "get": 200, "create": 201, "patch": 200, "delete": 204}[operation]
    assert calls[operation]().status_code == expected


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_document_id_cannot_cross_workspace(db_client, client_factory, metadata, method):
    other = register_and_login(client_factory())
    record = db_client.post(db_client.documents_url, json=metadata).json()
    kwargs = {"json": {"status": "archived"}} if method == "patch" else {}
    # Even a valid accessible workspace cannot be combined with another workspace's UUID.
    assert (
        getattr(other, method)(f"{other.documents_url}/{record['id']}", **kwargs).status_code == 404
    )
    assert other.get(other.documents_url).json()["items"] == []
    assert db_client.get(f"{db_client.documents_url}/{record['id']}").status_code == 200


def test_membership_management_and_immediate_revocation(db_client, client_factory, metadata):
    editor = register_and_login(client_factory())
    viewer = register_and_login(client_factory())
    path = f"/api/v1/workspaces/{db_client.workspace_id}/members"
    assert add_member(db_client, editor, "editor").status_code == 201
    assert add_member(db_client, viewer, "viewer").status_code == 201
    assert add_member(db_client, editor, "editor").status_code == 409
    for client in (editor, viewer):
        assert client.get(path).status_code == 403
        assert (
            client.post(path, json={"user_id": str(uuid4()), "role": "editor"}).status_code == 403
        )
        assert client.patch(f"{path}/{viewer.user_id}", json={"role": "editor"}).status_code == 403
        assert client.delete(f"{path}/{viewer.user_id}").status_code == 403
    assert (
        db_client.post(path, json={"user_id": db_client.user_id, "role": "viewer"}).status_code
        == 409
    )
    assert db_client.post(path, json={"user_id": str(uuid4()), "role": "viewer"}).status_code == 404
    assert db_client.patch(f"{path}/{viewer.user_id}", json={"role": "owner"}).status_code == 422
    assert db_client.patch(f"{path}/{viewer.user_id}", json={"role": "editor"}).status_code == 200
    assert viewer.post(db_client.documents_url, json=metadata).status_code == 201
    assert db_client.patch(f"{path}/{viewer.user_id}", json={"role": "viewer"}).status_code == 200
    assert viewer.post(db_client.documents_url, json=metadata).status_code == 403
    assert db_client.delete(f"{path}/{editor.user_id}").status_code == 204
    assert editor.get(db_client.documents_url).status_code == 404
    assert db_client.get(path).json()["owner_user_id"] == db_client.user_id


def test_workspace_list_and_owner_assignment(db_client, client_factory):
    other = register_and_login(client_factory())
    assert add_member(db_client, other, "viewer").status_code == 201
    created = db_client.post("/api/v1/workspaces", json={"name": "Research"})
    assert created.status_code == 201 and created.json()["owner_user_id"] == db_client.user_id
    assert (
        db_client.post(
            "/api/v1/workspaces", json={"name": "Bad", "owner_user_id": other.user_id}
        ).status_code
        == 422
    )
    visible = other.get("/api/v1/workspaces").json()["items"]
    assert {w["id"] for w in visible} == {other.workspace_id, db_client.workspace_id}
    assert other.get(f"/api/v1/workspaces/{created.json()['id']}").status_code == 404
    page = other.get("/api/v1/workspaces?limit=1").json()
    assert len(page["items"]) == 1 and page["has_more"]


def test_documents_reject_workspace_reassignment_and_missing_csrf(db_client, metadata):
    record = db_client.post(db_client.documents_url, json=metadata).json()
    path = f"{db_client.documents_url}/{record['id']}"
    assert db_client.patch(path, json={"workspace_id": str(uuid4())}).status_code == 422
    del db_client.headers["X-CSRF-Token"]
    assert db_client.post(db_client.documents_url, json=metadata).status_code == 403
    assert db_client.patch(path, json={"status": "archived"}).status_code == 403
    assert db_client.delete(path).status_code == 403
    assert db_client.get(path).status_code == 200


@pytest.mark.parametrize("method", ["get", "post", "patch", "delete"])
def test_old_unscoped_crud_is_not_an_auth_bypass(client_factory, method, metadata):
    client = client_factory()
    path = "/api/v1/documents" + (f"/{uuid4()}" if method in {"patch", "delete"} else "")
    kwargs = {"json": metadata} if method in {"post", "patch"} else {}
    assert getattr(client, method)(path, **kwargs).status_code == 404


def test_unauthenticated_workspace_access(client_factory):
    client = client_factory()
    assert client.get("/api/v1/workspaces").status_code == 401
    assert client.get(f"/api/v1/workspaces/{uuid4()}/documents").status_code == 401
