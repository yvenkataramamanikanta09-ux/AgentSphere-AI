"""New-input portability checks preserve existing records and durable retries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from conftest import register_and_login
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_document_uploads import private_files, send_file, uploaded

from agentsphere.api import document_files
from agentsphere.document_lifecycle import cleanup_job_id
from agentsphere.models import Document, DocumentFile, DocumentJob, DocumentSection, JobState

pytestmark = pytest.mark.postgres
DEVICE_ALIASES = [
    "CON .txt",
    "COM\u00b9.txt",
    "LPT\u00b2.txt",
    "lpt\u00b3.txt",
    "CONIN$.txt",
    "CONOUT$.txt",
]


@pytest.fixture
def private_client(client_factory, tmp_path):
    return register_and_login(client_factory(private_storage_root=tmp_path / "private"))


@pytest.mark.parametrize("filename", DEVICE_ALIASES)
def test_new_metadata_upload_and_rename_reject_device_aliases(private_client, db_env, filename):
    metadata = {"filename": filename, "content_type": "text/plain", "size_bytes": 10}
    assert private_client.post(private_client.documents_url, json=metadata).status_code == 422
    assert send_file(private_client, filename=filename).status_code == 422
    metadata["filename"] = "ordinary.txt"
    created = private_client.post(private_client.documents_url, json=metadata)
    assert created.status_code == 201, created.text
    url = f"{private_client.documents_url}/{created.json()['id']}"
    assert private_client.patch(url, json={"filename": filename}).status_code == 422
    assert private_client.get(url).json()["filename"] == "ordinary.txt"
    assert private_files(private_client) == []
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(DocumentFile)) == 0


@pytest.mark.parametrize("filename", DEVICE_ALIASES)
def test_legacy_metadata_remains_readable_and_manageable(private_client, db_env, filename):
    # Model the persisted input accepted before the filename policy was tightened.
    with Session(db_env) as session, session.begin():
        document = Document(
            workspace_id=UUID(private_client.workspace_id),
            filename=filename,
            content_type="text/plain",
            size_bytes=10,
        )
        session.add(document)
        session.flush()
        identifier = str(document.id)
    url = f"{private_client.documents_url}/{identifier}"
    assert private_client.get(url).json()["filename"] == filename
    assert (
        private_client.get(private_client.documents_url).json()["items"][0]["filename"] == filename
    )
    for patch in (
        {"status": "archived"},
        {"status": "registered", "filename": filename},
        {"filename": filename, "size_bytes": 11},
    ):
        response = private_client.patch(url, json=patch)
        assert response.status_code == 200, response.text
        assert response.json()["filename"] == filename
    renamed = private_client.patch(url, json={"filename": "portable.txt"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["filename"] == "portable.txt"
    assert private_client.delete(url).status_code == 204
    assert private_client.get(url).status_code == 404


@pytest.mark.parametrize("filename", DEVICE_ALIASES)
def test_legacy_original_exact_key_replay_survives_policy_and_display_rename(
    private_client, db_env, filename
):
    headers = {"Idempotency-Key": str(uuid4())}
    original = uploaded(private_client, headers=headers)
    identifier = UUID(original["document"]["id"])
    # File locations are UUIDs: this fixture changes only historical display metadata.
    with Session(db_env) as session, session.begin():
        session.get(Document, identifier).filename = filename
        attachment = session.scalar(
            select(DocumentFile).where(DocumentFile.document_id == identifier)
        )
        attachment.original_filename = filename
    url = f"{private_client.documents_url}/{identifier}"
    for renamed in (False, True):
        if renamed:
            response = private_client.patch(url, json={"filename": "portable.txt"})
            assert response.status_code == 200, response.text
        replay = send_file(private_client, filename=filename, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["document"]["id"] == str(identifier)
        assert replay.json()["document"]["filename"] == ("portable.txt" if renamed else filename)
        assert replay.json()["file"]["original_filename"] == filename
    assert send_file(private_client, filename=filename).status_code == 422
    # The compatibility allowance never bypasses exact content matching.
    conflict = send_file(private_client, filename=filename, payload=b"Changed\n", headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert len(private_files(private_client)) == 1
    assert private_client.delete(url).status_code == 204
    assert send_file(private_client, filename=filename, headers=headers).status_code == 409
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(DocumentFile)) == 0
        cleanup = session.get(DocumentJob, cleanup_job_id(identifier))
        assert cleanup is not None and cleanup.document_id is None
        assert cleanup.state == JobState.QUEUED


def test_inflight_replay_cannot_resurrect_deleted_upload(
    private_client, client_factory, db_env, monkeypatch
):
    headers = {"Idempotency-Key": str(uuid4())}
    original = uploaded(private_client, headers=headers)
    identifier = UUID(original["document"]["id"])
    url = f"{private_client.documents_url}/{identifier}"
    original_path = private_files(private_client)[0]
    second = client_factory(
        private_storage_root=private_client.app.state.settings.private_storage_root
    )
    second.cookies.update(private_client.cookies)
    second.headers["X-CSRF-Token"] = private_client.headers["X-CSRF-Token"]
    second.documents_url = private_client.documents_url
    waiting, resume = Event(), Event()
    finalize = document_files.finalize_upload

    def delayed_finalization(*args, **kwargs):
        waiting.set()
        if not resume.wait(10):
            raise RuntimeError("The disposable race test did not resume finalization.")
        return finalize(*args, **kwargs)

    monkeypatch.setattr(document_files, "finalize_upload", delayed_finalization)
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(send_file, second, headers=headers)
        try:
            assert waiting.wait(10), "Replay did not reach the intended transaction boundary."
            assert private_client.delete(url).status_code == 204
        finally:
            resume.set()
        response = retry.result(timeout=10)
    assert response.status_code == 409, response.text
    assert private_client.get(url).status_code == 404
    assert private_files(private_client) == [original_path]
    with Session(db_env) as session:
        for model in (Document, DocumentFile, DocumentSection):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        cleanup = session.get(DocumentJob, cleanup_job_id(identifier))
        assert cleanup is not None and cleanup.document_id is None
        assert cleanup.state == JobState.QUEUED
        assert cleanup.storage_key.endswith("/original")
