from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from agentsphere.models import Document

pytestmark = pytest.mark.postgres


def create(client, metadata):
    response = client.post(client.documents_url, json=metadata)
    assert response.status_code == 201, response.text
    return response


def test_crud_lifecycle(db_client, metadata):
    response = create(db_client, metadata)
    record = response.json()
    UUID(record["id"])
    assert record["status"] == "registered"
    assert datetime.fromisoformat(record["created_at"]).utcoffset().total_seconds() == 0
    assert datetime.fromisoformat(record["updated_at"]) >= datetime.fromisoformat(
        record["created_at"]
    )
    location = response.headers["location"]
    assert db_client.get(location).json() == record
    update = db_client.patch(location, json={"filename": "renamed.pdf", "status": "archived"})
    assert update.status_code == 200
    assert update.json()["id"] == record["id"]
    assert update.json()["created_at"] == record["created_at"]
    assert update.json()["filename"] == "renamed.pdf"
    assert update.json()["status"] == "archived"
    assert db_client.patch(location, json={"status": "registered"}).status_code == 200
    deleted = db_client.delete(location)
    assert deleted.status_code == 204 and deleted.content == b""
    assert db_client.get(location).status_code == 404
    assert db_client.delete(location).status_code == 404


def test_listing_pagination_and_status_filter(db_client, metadata):
    URL = db_client.documents_url
    ids = [create(db_client, metadata).json()["id"] for _ in range(3)]
    first = db_client.get(URL, params={"limit": 2}).json()
    assert len(first["items"]) == 2 and first["has_more"] is True
    second = db_client.get(URL, params={"limit": 2, "offset": 2}).json()
    assert len(second["items"]) == 1 and second["has_more"] is False
    assert {row["id"] for row in first["items"] + second["items"]} == set(ids)
    assert db_client.get(URL, params={"limit": 2}).json() == first
    db_client.patch(f"{URL}/{ids[0]}", json={"status": "archived"})
    archived = db_client.get(URL, params={"status": "archived"}).json()
    assert [row["id"] for row in archived["items"]] == [ids[0]]


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "uploaded"}]
)
def test_invalid_list_parameters(db_client, params):
    URL = db_client.documents_url
    assert db_client.get(URL, params=params).status_code == 422


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_missing_and_malformed_ids(db_client, method):
    URL = db_client.documents_url
    kwargs = {"json": {"status": "archived"}} if method == "patch" else {}
    assert getattr(db_client, method)(f"{URL}/{uuid4()}", **kwargs).status_code == 404
    assert getattr(db_client, method)(f"{URL}/not-a-uuid", **kwargs).status_code == 422


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"filename": "../bad.pdf"}, 422),
        ({"content_type": "text/plain"}, 422),
        ({"size_bytes": 0}, 422),
        ({"size_bytes": True}, 422),
        ({"size_bytes": 10_000_001}, 413),
        ({"status": "uploaded"}, 422),
    ],
)
def test_invalid_creation_leaves_no_row(db_client, metadata, change, expected):
    URL = db_client.documents_url
    assert db_client.post(URL, json=metadata | change).status_code == expected
    assert db_client.get(URL).json()["items"] == []


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({}, 422),
        ({"filename": None}, 422),
        ({"filename": "notes.txt"}, 422),
        ({"size_bytes": -1}, 422),
        ({"size_bytes": 10_000_001}, 413),
        ({"status": "uploaded"}, 422),
        ({"id": str(uuid4())}, 422),
        ({"created_at": "2020-01-01"}, 422),
    ],
)
def test_invalid_patch_does_not_change_row(db_client, metadata, patch, expected):
    URL = db_client.documents_url
    before = create(db_client, metadata).json()
    location = f"{URL}/{before['id']}"
    assert db_client.patch(location, json=patch).status_code == expected
    assert db_client.get(location).json() == before


def test_patch_validates_merged_fields_and_normalizes(db_client, metadata):
    URL = db_client.documents_url
    record = create(db_client, metadata).json()
    response = db_client.patch(
        f"{URL}/{record['id']}", json={"filename": "notes.txt", "content_type": " Text/Plain "}
    )
    assert response.status_code == 200
    assert response.json()["content_type"] == "text/plain"
    assert response.json()["size_bytes"] == metadata["size_bytes"]


def test_ten_mb_boundary_and_duplicate_names(db_client, metadata):
    first = create(db_client, metadata | {"size_bytes": 10_000_000}).json()
    second = create(db_client, metadata).json()
    assert first["id"] != second["id"]


def test_persistence_across_app_instances_and_timestamp_update(client_factory, db_env, metadata):
    from conftest import register_and_login

    first = register_and_login(client_factory())
    record = create(first, metadata).json()
    record_id = UUID(record["id"])
    second = client_factory()
    second.cookies.update(first.cookies)
    second.headers["X-CSRF-Token"] = first.headers["X-CSRF-Token"]
    location = f"{first.documents_url}/{record_id}"
    first.close()
    assert second.get(location).json() == record
    changed = second.patch(location, json={"status": "archived"}).json()
    assert datetime.fromisoformat(changed["updated_at"]) > datetime.fromisoformat(
        record["updated_at"]
    )
    assert changed["created_at"] == record["created_at"]
    with Session(db_env) as session:
        assert session.get(Document, record_id).status.value == "archived"
