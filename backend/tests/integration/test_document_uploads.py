"""Real PostgreSQL lifecycle and adversarial upload tests; private roots are disposable."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from conftest import register_and_login
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from agentsphere.models import (
    AuthSession,
    Document,
    DocumentFile,
    DocumentJob,
    DocumentSection,
    JobKind,
    JobState,
    Membership,
    ProcessingStatus,
)
from agentsphere.storage import PrivateStorage, StorageError

pytestmark = pytest.mark.postgres
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_bytes():
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Research</w:t></w:r></w:p></w:body></w:document>",
        )
        archive.writestr("_rels/.rels", "<Relationships/>")
    return output.getvalue()


@pytest.fixture
def upload_client(client_factory, tmp_path):
    return register_and_login(client_factory(private_storage_root=tmp_path / "private"))


def send_file(
    client, filename="notes.txt", payload=b"Research notes.\n", content_type="text/plain", **kwargs
):
    headers = {"Idempotency-Key": str(uuid4())}
    headers.update(kwargs.pop("headers", {}))
    return client.post(
        kwargs.pop("url", client.documents_url) + "/uploads",
        files={"file": (filename, payload, content_type)},
        headers=headers,
        **kwargs,
    )


def uploaded(client, **kwargs):
    response = send_file(client, **kwargs)
    assert response.status_code == 202, response.text
    return response.json()


def private_files(client):
    root = client.app.state.settings.private_storage_root
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def assert_no_uploads(client, db_env):
    with Session(db_env) as session:
        for model in (Document, DocumentFile, DocumentJob, DocumentSection):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert private_files(client) == []


def trust_extraction(db_env, document_id, clock):
    """Trusted fixture only: simulate the later worker's complete validated outcome."""
    with Session(db_env) as session, session.begin():
        attachment = session.scalar(
            select(DocumentFile).where(DocumentFile.document_id == UUID(document_id))
        )
        attachment.processing_status = ProcessingStatus.READY
        attachment.processed_at = clock[0]
        attachment.parser_version = "test-only-v1"
        job = session.scalar(
            select(DocumentJob).where(DocumentJob.document_id == UUID(document_id))
        )
        job.state = JobState.SUCCEEDED
        job.finished_at = clock[0]


def member(owner, client_factory, tmp_path, role):
    client = register_and_login(
        client_factory(private_storage_root=owner.app.state.settings.private_storage_root)
    )
    if role != "outsider":
        response = owner.post(
            f"/api/v1/workspaces/{owner.workspace_id}/members",
            json={"user_id": client.user_id, "role": role},
        )
        assert response.status_code == 201, response.text
    return client


@pytest.mark.parametrize(
    ("filename", "payload", "content_type"),
    [
        ("research.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n", "application/pdf"),
        ("research.docx", docx_bytes(), DOCX_TYPE),
        ("notes.txt", b"Research notes.\n", "text/plain"),
        ("notes.md", b"# Research\n\nEvidence.\n", "text/markdown"),
        ("notes.markdown", b"# Research\n", "text/markdown"),
    ],
    ids=["pdf", "docx", "txt", "md", "markdown"],
)
def test_supported_formats_create_measured_private_objects(
    upload_client, db_env, filename, payload, content_type
):
    body = uploaded(upload_client, filename=filename, payload=payload, content_type=content_type)
    document, attachment = body["document"], body["file"]
    assert body["replayed"] is False
    assert document["filename"] == filename
    assert document["content_type"] == content_type
    assert document["size_bytes"] == len(payload)
    assert document["status"] == "registered"
    assert attachment["document_id"] == document["id"]
    assert attachment["original_filename"] == filename
    assert attachment["sha256"] == hashlib.sha256(payload).hexdigest()
    assert attachment["uploaded_by"] == upload_client.user_id
    assert attachment["processing_status"] == "queued"
    assert attachment["download_available"] is False
    assert attachment["processed_at"] is None and attachment["error_code"] is None
    assert "storage_key" not in attachment and "parser_version" not in attachment
    assert str(upload_client.app.state.settings.private_storage_root) not in str(body)
    location = f"{upload_client.documents_url}/{document['id']}"
    assert upload_client.get(location).json() == document
    assert upload_client.get(f"{location}/file").json() == attachment
    assert upload_client.get(upload_client.documents_url).json()["items"] == [document]
    assert upload_client.get(f"{location}/download").status_code == 409
    with Session(db_env) as session:
        stored = session.get(DocumentFile, UUID(attachment["id"]))
        jobs = session.scalars(select(DocumentJob)).all()
        assert len(jobs) == 1 and jobs[0].kind == JobKind.EXTRACT
        assert jobs[0].state == JobState.QUEUED and jobs[0].attempts == 0
        assert jobs[0].document_id == UUID(document["id"])
        assert jobs[0].storage_key == stored.storage_key
        assert stored.storage_key.startswith(f"objects/{UUID(upload_client.workspace_id).hex}/")
        assert filename not in stored.storage_key
    assert len(private_files(upload_client)) == 1
    assert private_files(upload_client)[0].read_bytes() == payload


def test_actual_ten_million_byte_boundary_is_inclusive(upload_client):
    body = uploaded(upload_client, payload=b"x" * 10_000_000)
    assert body["document"]["size_bytes"] == 10_000_000


@pytest.mark.parametrize("size", [0, 10_000_001])
def test_empty_and_oversized_files_leave_no_records_or_objects(upload_client, db_env, size):
    response = send_file(upload_client, payload=b"x" * size)
    assert response.status_code == (413 if size else 422), response.text
    assert_no_uploads(upload_client, db_env)


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.txt",
        "..\\secret.txt",
        "C:\\secret.txt",
        "notes.txt:secret",
        "CON.txt",
        ".secret.txt",
        "trailing.txt ",
        "notes.exe",
    ],
)
def test_unsafe_or_unsupported_filenames_are_rejected(upload_client, db_env, filename):
    response = send_file(upload_client, filename=filename)
    assert response.status_code in {415, 422}, response.text
    assert_no_uploads(upload_client, db_env)


@pytest.mark.parametrize(
    ("filename", "payload", "content_type"),
    [
        ("forged.pdf", b"This is not a PDF.", "application/pdf"),
        ("forged.docx", b"Not a ZIP archive.", DOCX_TYPE),
        ("binary.txt", b"\x00\x01\x02binary", "text/plain"),
        ("bad.txt", b"\xff\xfe\x80", "text/plain"),
        ("notes.txt", b"Actual text", "application/pdf"),
    ],
)
def test_basic_format_evidence_and_type_conflicts_are_rejected(
    upload_client, db_env, filename, payload, content_type
):
    response = send_file(
        upload_client, filename=filename, payload=payload, content_type=content_type
    )
    assert response.status_code in {415, 422}, response.text
    assert_no_uploads(upload_client, db_env)


def test_generic_mime_is_canonicalized_from_validated_extension(upload_client):
    body = uploaded(upload_client, content_type="application/octet-stream")
    assert body["document"]["content_type"] == "text/plain"


@pytest.mark.parametrize("nonce", [None, "", "not-a-uuid", "../secret"])
def test_upload_requires_valid_idempotency_identifier(upload_client, db_env, nonce):
    headers = {} if nonce is None else {"Idempotency-Key": nonce}
    response = upload_client.post(
        upload_client.documents_url + "/uploads",
        files={"file": ("notes.txt", b"Text", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert_no_uploads(upload_client, db_env)


def test_idempotent_replay_has_one_document_object_and_job(upload_client, db_env):
    headers = {"Idempotency-Key": str(uuid4())}
    first = uploaded(upload_client, headers=headers)
    second = uploaded(upload_client, headers=headers)
    assert first["document"] == second["document"]
    assert first["file"] == second["file"]
    assert second["replayed"] is True
    with Session(db_env) as session:
        for model in (Document, DocumentFile, DocumentJob):
            assert session.scalar(select(func.count()).select_from(model)) == 1
    assert len(private_files(upload_client)) == 1


def test_concurrent_duplicate_requests_do_not_create_orphan_objects(
    upload_client, client_factory, db_env
):
    second = client_factory(
        private_storage_root=upload_client.app.state.settings.private_storage_root
    )
    second.cookies.update(upload_client.cookies)
    second.headers["X-CSRF-Token"] = upload_client.headers["X-CSRF-Token"]
    second.documents_url = upload_client.documents_url
    headers = {"Idempotency-Key": str(uuid4())}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda client: send_file(client, headers=headers), (upload_client, second))
        )
    assert [response.status_code for response in responses] == [202, 202], [
        response.text for response in responses
    ]
    assert responses[0].json()["document"] == responses[1].json()["document"]
    assert sorted(response.json()["replayed"] for response in responses) == [False, True]
    with Session(db_env) as session:
        for model in (Document, DocumentFile, DocumentJob):
            assert session.scalar(select(func.count()).select_from(model)) == 1
    assert len(private_files(upload_client)) == 1


def test_same_idempotency_key_is_scoped_to_workspace(upload_client, db_env):
    created = upload_client.post("/api/v1/workspaces", json={"name": "Second research"})
    assert created.status_code == 201
    other_url = f"/api/v1/workspaces/{created.json()['id']}/documents"
    headers = {"Idempotency-Key": str(uuid4())}
    first = uploaded(upload_client, headers=headers)
    second = uploaded(upload_client, url=other_url, headers=headers)
    assert first["document"]["id"] != second["document"]["id"]
    assert first["file"]["id"] != second["file"]["id"]
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(DocumentJob)) == 2
    assert len(private_files(upload_client)) == 2


@pytest.mark.parametrize("change", [{"payload": b"Changed notes"}, {"filename": "other.txt"}])
def test_idempotency_payload_conflicts_do_not_overwrite(upload_client, db_env, change):
    headers = {"Idempotency-Key": str(uuid4())}
    original = uploaded(upload_client, headers=headers)
    original_bytes = private_files(upload_client)[0].read_bytes()
    response = send_file(upload_client, headers=headers, **change)
    assert response.status_code == 409, response.text
    assert upload_client.get(upload_client.documents_url).json()["items"] == [original["document"]]
    assert len(private_files(upload_client)) == 1
    assert private_files(upload_client)[0].read_bytes() == original_bytes
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(DocumentJob)) == 1


@pytest.mark.parametrize(
    "defect", ["missing", "wrong-field", "two-files", "extra-field", "json", "broken-boundary"]
)
def test_invalid_multipart_is_rejected_without_side_effects(upload_client, db_env, defect):
    options = {"headers": {"Idempotency-Key": str(uuid4())}}
    file = ("notes.txt", b"Research", "text/plain")
    if defect == "missing":
        options["data"] = {"note": "not a file"}
    elif defect == "wrong-field":
        options["files"] = {"document": file}
    elif defect == "two-files":
        options["files"] = [("file", file), ("file", file)]
    elif defect == "extra-field":
        options.update(files={"file": file}, data={"workspace_id": str(uuid4())})
    elif defect == "json":
        options["json"] = {"filename": "notes.txt"}
    else:
        options["content"] = b"--wrong\r\nNot multipart"
        options["headers"]["Content-Type"] = "multipart/form-data; boundary=expected"
    response = upload_client.post(upload_client.documents_url + "/uploads", **options)
    assert response.status_code in {400, 415, 422}, response.text
    assert_no_uploads(upload_client, db_env)


def multipart_bytes(payload):
    return (
        b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="notes.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n" + payload + b"\r\n--test-boundary--\r\n"
    )


def test_oversized_chunked_request_without_declared_length_is_rejected(upload_client, db_env):
    def chunks():
        data = multipart_bytes(b"x" * 10_100_000)
        for offset in range(0, len(data), 64 * 1024):
            yield data[offset : offset + 64 * 1024]

    response = upload_client.post(
        upload_client.documents_url + "/uploads",
        content=chunks(),
        headers={
            "Idempotency-Key": str(uuid4()),
            "Content-Type": "multipart/form-data; boundary=test-boundary",
        },
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413, response.text
    assert_no_uploads(upload_client, db_env)


def test_interrupted_asgi_upload_cleans_owned_temporary_files(upload_client, db_env):
    request = upload_client.build_request(
        "POST",
        upload_client.documents_url + "/uploads",
        headers={
            "Idempotency-Key": str(uuid4()),
            "Content-Type": "multipart/form-data; boundary=test-boundary",
        },
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "scheme": "https",
        "method": "POST",
        "path": request.url.path,
        "raw_path": request.url.raw_path,
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower(), value) for name, value in request.headers.raw],
        "client": ("testclient", 50000),
        "server": ("localhost", 8443),
        "state": {},
    }
    events = []
    chunks = iter(
        [
            {
                "type": "http.request",
                "body": multipart_bytes(b"Some bytes")[:-25],
                "more_body": True,
            },
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(chunks, {"type": "http.disconnect"})

    async def send(message):
        events.append(message)

    upload_client.portal.call(upload_client.app, scope, receive, send)
    response = next(message for message in events if message["type"] == "http.response.start")
    assert response["status"] in {400, 422}
    assert_no_uploads(upload_client, db_env)


@pytest.mark.parametrize(
    "security_failure", ["csrf-missing", "csrf-invalid", "cross-origin", "expired", "anonymous"]
)
def test_upload_security_checks_precede_body_consumption(
    upload_client, db_env, clock, security_failure
):
    if security_failure == "csrf-missing":
        del upload_client.headers["X-CSRF-Token"]
    elif security_failure == "csrf-invalid":
        upload_client.headers["X-CSRF-Token"] = "wrong-token"
    elif security_failure == "expired":
        clock[0] += timedelta(days=2)
    elif security_failure == "anonymous":
        upload_client.cookies.clear()
    headers = {
        "Idempotency-Key": str(uuid4()),
        "Content-Type": "multipart/form-data; boundary=test-boundary",
    }
    if security_failure == "cross-origin":
        headers["Origin"] = "https://evil.example"

    def forbidden_body():
        pytest.fail("Unauthorized upload consumed its request body.")
        yield b""  # pragma: no cover

    response = upload_client.post(
        upload_client.documents_url + "/uploads", content=forbidden_body(), headers=headers
    )
    expected = 401 if security_failure in {"expired", "anonymous"} else 403
    assert response.status_code == expected, response.text
    assert_no_uploads(upload_client, db_env)


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_workspace_upload_permissions(upload_client, client_factory, tmp_path, db_env, role):
    client = member(upload_client, client_factory, tmp_path, role)
    response = send_file(client, url=upload_client.documents_url)
    assert response.status_code == {"editor": 202, "viewer": 403, "outsider": 404}[role], (
        response.text
    )
    if role != "editor":
        assert_no_uploads(client, db_env)


@pytest.mark.parametrize("revocation", ["viewer", "removed", "session"])
def test_authorization_revoked_while_body_arrives_prevents_finalization(
    upload_client, client_factory, tmp_path, db_env, clock, revocation
):
    client = member(upload_client, client_factory, tmp_path, "editor")

    def body():
        # TestClient reads this iterator only when the ASGI app asks for body bytes.
        # This separate transaction also proves auth locks are released during transfer.
        with Session(db_env) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            if revocation == "session":
                session.execute(
                    update(AuthSession)
                    .where(AuthSession.user_id == UUID(client.user_id))
                    .values(revoked_at=clock[0])
                )
            else:
                membership = session.get(
                    Membership, (UUID(upload_client.workspace_id), UUID(client.user_id))
                )
                if revocation == "removed":
                    session.delete(membership)
                else:
                    membership.role = "viewer"
        yield multipart_bytes(b"Research")

    response = client.post(
        upload_client.documents_url + "/uploads",
        content=body(),
        headers={
            "Idempotency-Key": str(uuid4()),
            "Content-Type": "multipart/form-data; boundary=test-boundary",
        },
    )
    assert response.status_code == {"viewer": 403, "removed": 404, "session": 401}[revocation], (
        response.text
    )
    assert_no_uploads(client, db_env)


def test_storage_failure_is_sanitized_and_leaves_no_rows(upload_client, db_env, monkeypatch):
    def fail(*args, **kwargs):
        raise StorageError("Sensitive internal volume path")

    monkeypatch.setattr(PrivateStorage, "put", fail)
    response = send_file(upload_client)
    assert response.status_code == 503, response.text
    assert "Sensitive internal volume path" not in response.text
    assert_no_uploads(upload_client, db_env)


@pytest.mark.parametrize("failure", ["flush", "commit"])
def test_database_failure_compensates_published_private_object(
    upload_client, db_env, monkeypatch, failure
):
    published = [False]
    real_put = PrivateStorage.put

    def track_put(*args, **kwargs):
        result = real_put(*args, **kwargs)
        published[0] = True
        return result

    monkeypatch.setattr(PrivateStorage, "put", track_put)

    def fail_flush(session, context, instances):
        if published[0] and any(isinstance(item, DocumentFile) for item in session.new):
            raise OperationalError("redacted", {}, Exception("database failure"))

    def fail_commit(connection):
        if published[0]:
            raise OperationalError("redacted", {}, Exception("database failure"))

    target, name, hook = (
        (Session, "before_flush", fail_flush)
        if failure == "flush"
        else (db_env, "commit", fail_commit)
    )
    event.listen(target, name, hook)
    try:
        response = send_file(upload_client)
    finally:
        event.remove(target, name, hook)
    assert published[0], "Fault must occur after an actual private object was published."
    assert response.status_code == 503, response.text
    assert "database failure" not in response.text
    assert_no_uploads(upload_client, db_env)


def test_file_backed_metadata_is_immutable_but_archive_and_rename_work(upload_client):
    body = uploaded(upload_client)
    document = body["document"]
    url = f"{upload_client.documents_url}/{document['id']}"
    for change in ({"size_bytes": 1}, {"content_type": "application/pdf"}):
        assert upload_client.patch(url, json=change).status_code == 409
    assert upload_client.patch(url, json={"filename": "forged.pdf"}).status_code in {409, 422}
    assert upload_client.get(url).json() == document
    same_values = {key: document[key] for key in ("size_bytes", "content_type")}
    assert upload_client.patch(url, json=same_values).status_code == 200
    assert (
        upload_client.patch(url, json={"filename": "renamed.txt", "status": "archived"}).status_code
        == 200
    )
    assert upload_client.patch(url, json={"status": "registered"}).status_code == 200
    assert upload_client.get(url + "/file").json()["original_filename"] == "notes.txt"


def test_delete_hides_metadata_and_durably_retains_cleanup_identifiers(upload_client, db_env):
    body = uploaded(upload_client)
    document_id = UUID(body["document"]["id"])
    url = f"{upload_client.documents_url}/{document_id}"
    with Session(db_env) as session, session.begin():
        key = session.get(DocumentFile, UUID(body["file"]["id"])).storage_key
        session.add(DocumentSection(document_id=document_id, ordinal=0, text="Test text"))
    assert upload_client.delete(url).status_code == 204
    for suffix in ("", "/file", "/download"):
        assert upload_client.get(url + suffix).status_code == 404
    assert upload_client.get(upload_client.documents_url).json()["items"] == []
    assert upload_client.delete(url).status_code == 404
    with Session(db_env) as session:
        assert session.get(Document, document_id) is None
        assert session.scalar(select(func.count()).select_from(DocumentFile)) == 0
        assert session.scalar(select(func.count()).select_from(DocumentSection)) == 0
        extraction = session.scalar(select(DocumentJob).where(DocumentJob.kind == JobKind.EXTRACT))
        cleanup = session.scalar(
            select(DocumentJob).where(DocumentJob.kind == JobKind.DELETE_FILES)
        )
        assert extraction.state == JobState.CANCELLED and extraction.finished_at is not None
        assert extraction.document_id is None
        assert cleanup.state == JobState.QUEUED and cleanup.storage_key == key
        assert cleanup.document_id is None and cleanup.attempts == 0
    # Scheduling deletion is different from physical cleanup by the later worker.
    assert len(private_files(upload_client)) == 1
    assert private_files(upload_client)[0].read_bytes() == b"Research notes.\n"


def test_deletion_cancels_running_job_and_clears_its_lease(upload_client, db_env, clock):
    body = uploaded(upload_client)
    with Session(db_env) as session, session.begin():
        job = session.scalar(select(DocumentJob))
        job.state = JobState.RUNNING
        job.started_at = clock[0]
        job.attempts = 1
        job.lease_token = uuid4()
        job.lease_expires_at = clock[0] + timedelta(minutes=2)
    url = f"{upload_client.documents_url}/{body['document']['id']}"
    assert upload_client.delete(url).status_code == 204
    with Session(db_env) as session:
        job = session.scalar(select(DocumentJob).where(DocumentJob.kind == JobKind.EXTRACT))
        assert job.state == JobState.CANCELLED
        assert job.lease_token is None and job.lease_expires_at is None
        assert job.finished_at is not None and job.document_id is None
        assert session.scalar(select(func.count()).select_from(DocumentJob)) == 2


def test_metadata_only_records_still_edit_and_delete_without_file_jobs(
    upload_client, db_env, metadata
):
    response = upload_client.post(upload_client.documents_url, json=metadata)
    assert response.status_code == 201
    url = f"{upload_client.documents_url}/{response.json()['id']}"
    assert upload_client.get(url + "/file").status_code == 404
    assert upload_client.get(url + "/download").status_code == 404
    assert upload_client.patch(url, json={"size_bytes": 17}).status_code == 200
    assert upload_client.delete(url).status_code == 204
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(DocumentJob)) == 0
    assert private_files(upload_client) == []


@pytest.mark.parametrize("state", ["queued", "processing", "failed", "incomplete-ready"])
def test_unvalidated_or_incomplete_processing_never_allows_download(
    upload_client, db_env, clock, state
):
    body = uploaded(upload_client)
    with Session(db_env) as session, session.begin():
        attachment = session.get(DocumentFile, UUID(body["file"]["id"]))
        attachment.processing_status = (
            ProcessingStatus.READY if state == "incomplete-ready" else ProcessingStatus(state)
        )
        if state == "incomplete-ready":
            attachment.processed_at = clock[0]
            attachment.parser_version = "test-only-v1"
            # An uncompleted extraction job cannot attest validation.
        if state == "failed":
            attachment.error_code = "invalid_content"
    url = f"{upload_client.documents_url}/{body['document']['id']}"
    assert upload_client.get(url + "/download").status_code == 409
    assert upload_client.get(url + "/file").json()["download_available"] is False


@pytest.mark.parametrize("role", ["owner", "editor", "viewer", "outsider"])
def test_validated_download_permissions_and_security_headers(
    upload_client, client_factory, tmp_path, db_env, clock, role
):
    body = uploaded(upload_client)
    trust_extraction(db_env, body["document"]["id"], clock)
    client = (
        upload_client if role == "owner" else member(upload_client, client_factory, tmp_path, role)
    )
    # Both application instances address the same private root in production.
    client.app.state.settings.private_storage_root = (
        upload_client.app.state.settings.private_storage_root
    )
    url = f"{upload_client.documents_url}/{body['document']['id']}"
    response = client.get(url + "/download")
    if role == "outsider":
        assert response.status_code == 404
        return
    assert response.status_code == 200, response.text
    assert response.content == b"Research notes.\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "notes.txt" in response.headers["content-disposition"]
    assert client.get(url + "/file").json()["download_available"] is True


@pytest.mark.parametrize("suffix", ["/file", "/download"])
def test_file_ids_cannot_cross_accessible_workspace(
    upload_client, client_factory, tmp_path, suffix
):
    body = uploaded(upload_client)
    outsider = member(upload_client, client_factory, tmp_path, "outsider")
    assert (
        outsider.get(f"{outsider.documents_url}/{body['document']['id']}{suffix}").status_code
        == 404
    )
    assert (
        outsider.get(f"{upload_client.documents_url}/{body['document']['id']}{suffix}").status_code
        == 404
    )


def test_viewer_cannot_update_or_delete_uploaded_document(upload_client, client_factory, tmp_path):
    body = uploaded(upload_client)
    viewer = member(upload_client, client_factory, tmp_path, "viewer")
    url = f"{upload_client.documents_url}/{body['document']['id']}"
    assert viewer.get(url).status_code == 200
    assert viewer.get(url + "/file").status_code == 200
    assert viewer.patch(url, json={"status": "archived"}).status_code == 403
    assert viewer.delete(url).status_code == 403
    assert upload_client.get(url).status_code == 200
