"""A cancelled upload cannot delete bytes while finalization is still committing."""

import asyncio
from threading import Event
from uuid import UUID, uuid4

import pytest
from conftest import register_and_login
from fastapi import Request, Response
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from agentsphere.api import document_files
from agentsphere.models import Document, DocumentFile, DocumentJob, JobKind

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("cancellations", [1, 3])
def test_cancelled_finalization_joins_worker_preserves_committed_file_and_supports_replay(
    client_factory, db_env, tmp_path, monkeypatch, cancellations
):
    client = register_and_login(client_factory(private_storage_root=tmp_path / "private"))
    nonce = uuid4()
    payload = b"Cancellation must not delete a successfully committed original.\n"
    prepared = client.build_request(
        "POST",
        client.documents_url + "/uploads",
        headers={"Idempotency-Key": str(nonce)},
        files={"file": ("notes.txt", payload, "text/plain")},
    )
    body = prepared.read()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "scheme": "https",
        "method": "POST",
        "path": prepared.url.path,
        "raw_path": prepared.url.raw_path,
        "query_string": b"",
        "root_path": "",
        # ASGI headers are lowercase; HTTPX preserves supplied header casing.
        "headers": [(name.lower(), value) for name, value in prepared.headers.raw],
        "client": ("testclient", 50000),
        "server": ("localhost", 8443),
        "state": {},
        "app": client.app,
    }
    entered, release, completed = Event(), Event(), Event()
    finalize = document_files.finalize_upload

    def block_commit(connection):
        # This connection sees its own newly flushed attachment before commit.
        # Other transactions cannot see it until cancellation has been joined.
        if connection.scalar(select(DocumentFile.id)) is not None and not release.is_set():
            entered.set()
            assert release.wait(5), "Test did not release the pending database commit."

    def blocked_finalize(*args, **kwargs):
        try:
            return finalize(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(document_files, "finalize_upload", blocked_finalize)
    root = client.app.state.settings.private_storage_root

    async def scenario():
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        request = Request(scope, receive)
        task = asyncio.create_task(
            document_files.upload_document(UUID(client.workspace_id), request, Response(), nonce)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5), "Upload did not reach finalization."
            assert len(list((root / "tmp").iterdir())) == 1
            assert len(list((root / "objects").rglob("original"))) == 1
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "Upload abandoned its active database commit."
                assert not completed.is_set()
                assert len(list((root / "tmp").iterdir())) == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert completed.is_set()
        finally:
            release.set()
            await asyncio.to_thread(completed.wait, 5)
            await asyncio.gather(task, return_exceptions=True)

    event.listen(db_env, "commit", block_commit)
    try:
        asyncio.run(scenario())
    finally:
        event.remove(db_env, "commit", block_commit)
    assert not list((root / "tmp").iterdir())
    with Session(db_env) as session:
        document = session.scalar(select(Document))
        attachment = session.scalar(select(DocumentFile))
        job = session.scalar(select(DocumentJob))
        assert document is not None and attachment is not None and job is not None
        assert attachment.document_id == document.id
        assert job.document_id == document.id and job.kind == JobKind.EXTRACT
        assert (root / attachment.storage_key).read_bytes() == payload
    replay = client.post(
        client.documents_url + "/uploads",
        headers={"Idempotency-Key": str(nonce)},
        files={"file": ("notes.txt", payload, "text/plain")},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["replayed"] is True
    assert len(list((root / "objects").rglob("original"))) == 1
    assert not list((root / "tmp").iterdir())
