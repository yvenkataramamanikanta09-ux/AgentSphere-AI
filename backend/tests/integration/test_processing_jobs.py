"""Real PostgreSQL leases, atomic publication, deletion races, and cleanup fencing."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from threading import Barrier, Event, current_thread
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from agentsphere.document_lifecycle import queue_file_deletion
from agentsphere.extraction import ExtractionResult, Section
from agentsphere.models import (
    Document,
    DocumentFile,
    DocumentJob,
    DocumentSection,
    DocumentStatus,
    JobKind,
    JobState,
    ProcessingStatus,
    User,
    Workspace,
)
from agentsphere.storage import PrivateStorage, StorageError, new_storage_key
from agentsphere.worker_jobs import DurableJobQueue

pytestmark = pytest.mark.postgres


@pytest.fixture
def jobs(db_env, tmp_path):
    factory = sessionmaker(db_env, expire_on_commit=False)
    storage = PrivateStorage(tmp_path / "private")
    storage.initialize()
    with factory() as db, db.begin():
        owner = User(
            email=f"{uuid4().hex}@example.invalid", password_hash="unused", display_name="Owner"
        )
        db.add(owner)
        db.flush()
        workspace = Workspace(name="Processing", owner_user_id=owner.id)
        db.add(workspace)
        db.flush()
        owner_id, workspace_id = owner.id, workspace.id

    def add(*, kind=JobKind.EXTRACT, original=True, status=DocumentStatus.REGISTERED):
        file_id = uuid4()
        key = new_storage_key(workspace_id, file_id)
        stored = storage.put(key, BytesIO(b"Research notes.\n"), max_bytes=1000)
        with factory() as db, db.begin():
            document = Document(
                workspace_id=workspace_id,
                filename="notes.txt",
                content_type="text/plain",
                size_bytes=stored.size_bytes,
                status=status,
            )
            db.add(document)
            db.flush()
            if original:
                db.add(
                    DocumentFile(
                        id=file_id,
                        document_id=document.id,
                        storage_key=key,
                        original_filename="notes.txt",
                        sha256=stored.sha256,
                        uploaded_by=owner_id,
                    )
                )
            job = DocumentJob(document_id=document.id, storage_key=key, kind=kind)
            db.add(job)
            db.flush()
            return SimpleNamespace(document_id=document.id, file_id=file_id, job_id=job.id, key=key)

    return SimpleNamespace(
        factory=factory,
        storage=storage,
        workspace_id=workspace_id,
        add=add,
        queue=DurableJobQueue(factory),
    )


def result(*texts):
    return ExtractionResult(
        sections=[
            Section(text=value, line_start=index + 1, line_end=index + 1)
            for index, value in enumerate(texts)
        ],
        parser_version="test-parser-v1",
    )


def expire(factory, claim):
    with factory() as db, db.begin():
        db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == claim.id)
            .values(
                started_at=func.clock_timestamp() - text("interval '5 minutes'"),
                lease_expires_at=func.clock_timestamp() - text("interval '1 second'"),
            )
        )


def make_available(factory, job_id):
    with factory() as db, db.begin():
        db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == job_id)
            .values(available_at=func.clock_timestamp() - text("interval '1 second'"))
        )


def remove_document(jobs, record):
    with jobs.factory() as db, db.begin():
        assert db.get(Workspace, jobs.workspace_id, with_for_update=True) is not None
        document = db.get(Document, record.document_id, with_for_update=True)
        queue_file_deletion(db, document, db.scalar(select(func.clock_timestamp())))
        db.delete(document)


def test_claims_skip_locked_rows_and_concurrent_workers_get_distinct_jobs(jobs):
    records = [jobs.add() for _ in range(3)]
    with jobs.factory() as blocker, blocker.begin():
        blocker.get(DocumentJob, records[0].job_id, with_for_update=True)
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=3)
            return jobs.queue.claim()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            claims = [future.result(timeout=5) for future in futures]
        assert {item.id for item in claims} == {item.job_id for item in records[1:]}
        assert len({item.lease_token for item in claims}) == 2
        assert all(item.attempts == 1 for item in claims)
        assert jobs.queue.claim() is None
    last = jobs.queue.claim()
    assert last.id == records[0].job_id
    with jobs.factory() as db:
        job = db.get(DocumentJob, last.id)
        timestamp = db.scalar(select(func.clock_timestamp()))
        remaining = (job.lease_expires_at - timestamp).total_seconds()
        assert 110 < remaining <= 120


@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_renew_cannot_revive_lease_that_expires_behind_unchanged_job_lock(jobs, db_env, release):
    record = jobs.add()
    queue = DurableJobQueue(jobs.factory, lease_seconds=2)
    claim = queue.claim()
    started = Event()
    backend_pid = []

    def observe_query(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("renew-barrier") and "document_jobs" in statement:
            backend_pid[:] = [connection.connection.driver_connection.info.backend_pid]
            started.set()

    event.listen(db_env, "before_cursor_execute", observe_query)
    try:
        with jobs.factory() as blocker:
            job = blocker.get(DocumentJob, claim.id, with_for_update=True)
            expires_at = job.lease_expires_at
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="renew-barrier") as pool:
                future = pool.submit(queue.renew, claim)
                try:
                    assert started.wait(3)
                    deadline = monotonic() + 3
                    with db_env.connect() as observer:
                        while monotonic() < deadline:
                            assert not future.done(), "Renewal crossed the held job lock."
                            if observer.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                    "WHERE pid = :pid AND NOT granted)"
                                ),
                                {"pid": backend_pid[0]},
                            ):
                                break
                            Event().wait(0.01)
                        else:
                            pytest.fail("Renewal never waited for the held job lock.")
                        assert observer.scalar(select(func.clock_timestamp())) < expires_at
                        while observer.scalar(select(func.clock_timestamp())) <= expires_at:
                            assert monotonic() < deadline, "Lease did not expire during lock wait."
                            assert not future.done()
                            Event().wait(0.01)
                    # Release without changing the row, so UPDATE cannot rely on
                    # a new tuple version to recheck its pre-lock time predicate.
                    getattr(blocker, release)()
                finally:
                    blocker.rollback()
                assert not future.result(timeout=5)
    finally:
        event.remove(db_env, "before_cursor_execute", observe_query)
    with jobs.factory() as db:
        job = db.get(DocumentJob, record.job_id)
        assert job.state == JobState.RUNNING and job.lease_expires_at == expires_at
        assert job.lease_token == claim.lease_token and job.attempts == claim.attempts
    assert not queue.renew(claim)
    fresh = queue.claim()
    assert fresh.id == claim.id and fresh.lease_token != claim.lease_token
    assert fresh.attempts == claim.attempts + 1


def test_expired_fence_cannot_begin_renew_fail_or_publish_after_reclaim(jobs):
    record = jobs.add(status=DocumentStatus.ARCHIVED)
    old = jobs.queue.claim()
    snapshot = jobs.queue.begin(old)
    assert snapshot.document_id == record.document_id
    assert snapshot.workspace_id == jobs.workspace_id
    assert snapshot.key == record.key
    assert snapshot.size_bytes == len(b"Research notes.\n")
    assert len(snapshot.sha256) == 64 and snapshot.content_type == "text/plain"
    assert jobs.queue.renew(old)
    expire(jobs.factory, old)
    assert jobs.queue.begin(old) is None
    assert not jobs.queue.renew(old)
    assert not jobs.queue.fail(old, "parser_timeout")
    assert not jobs.queue.succeed(old, result("Stale result"))
    fresh = jobs.queue.claim()
    assert fresh.id == old.id and fresh.lease_token != old.lease_token
    assert fresh.attempts == 2
    assert jobs.queue.begin(fresh) is not None
    assert jobs.queue.succeed(fresh, result("First", "Second"))
    assert not jobs.queue.succeed(old, result("Overwrite"))
    assert not jobs.queue.fail(old, "parser_timeout")
    assert not jobs.queue.renew(fresh)
    with jobs.factory() as db:
        sections = db.scalars(select(DocumentSection).order_by(DocumentSection.ordinal)).all()
        assert [(item.ordinal, item.text, item.line_start) for item in sections] == [
            (0, "First", 1),
            (1, "Second", 2),
        ]
        assert db.get(Document, record.document_id).status == DocumentStatus.ARCHIVED
        attachment = db.get(DocumentFile, record.file_id)
        assert attachment.processing_status == ProcessingStatus.READY
        assert attachment.processed_at is not None and attachment.error_code is None
        assert attachment.parser_version == "test-parser-v1"
        job = db.get(DocumentJob, record.job_id)
        assert job.state == JobState.SUCCEEDED and job.finished_at is not None
        assert job.lease_token is None and job.lease_expires_at is None


def test_retry_backoff_is_bounded_and_last_failure_is_terminal(jobs):
    record = jobs.add()
    queue = DurableJobQueue(jobs.factory, retry_base_seconds=40)
    for attempt, expected_delay in ((1, 40), (2, 60), (3, None)):
        claim = queue.claim()
        assert claim.attempts == attempt
        assert queue.begin(claim) is not None
        assert queue.fail(claim, "parser_timeout")
        with jobs.factory() as db:
            job = db.get(DocumentJob, record.job_id)
            attachment = db.get(DocumentFile, record.file_id)
            assert job.lease_token is None and job.lease_expires_at is None
            if expected_delay is not None:
                assert job.state == JobState.QUEUED
                delay = (job.available_at - job.updated_at).total_seconds()
                assert expected_delay - 1 < delay <= expected_delay
                assert attachment.processing_status == ProcessingStatus.QUEUED
                assert queue.claim() is None
            else:
                assert job.state == JobState.FAILED and job.finished_at is not None
                assert attachment.processing_status == ProcessingStatus.FAILED
                assert attachment.error_code == "parser_timeout"
        make_available(jobs.factory, record.job_id)
    assert queue.claim() is None


def test_stale_last_attempt_is_failed_in_document_lock_order_without_increment(jobs):
    record = jobs.add()
    previous = None
    for attempt in range(1, 4):
        claim = jobs.queue.claim()
        assert claim.attempts == attempt
        if previous is not None:
            assert claim.lease_token != previous.lease_token
        assert jobs.queue.begin(claim) is not None
        expire(jobs.factory, claim)
        previous = claim
    exhausted = jobs.queue.claim()
    assert exhausted.exhausted and exhausted.attempts == 3
    assert exhausted.lease_token == previous.lease_token
    assert jobs.queue.begin(exhausted) is None and not jobs.queue.renew(exhausted)
    assert jobs.queue.fail(exhausted, "attempts_exhausted", retryable=True)
    assert not jobs.queue.fail(exhausted, "attempts_exhausted", retryable=False)
    with jobs.factory() as db:
        job = db.get(DocumentJob, record.job_id)
        assert job.attempts == 3 and job.state == JobState.FAILED
        assert db.get(DocumentFile, record.file_id).processing_status == ProcessingStatus.FAILED
    assert jobs.queue.claim() is None


def test_invalid_parser_result_is_terminal_and_existing_sections_are_not_discarded(jobs):
    record = jobs.add()
    with jobs.factory() as db, db.begin():
        db.add(DocumentSection(document_id=record.document_id, ordinal=0, text="Prior result"))
    claim = jobs.queue.claim()
    assert jobs.queue.begin(claim) is not None
    malformed = ExtractionResult.model_construct(
        sections=[Section.model_construct(text="", page_number=0)], parser_version="test"
    )
    assert not jobs.queue.succeed(claim, malformed)
    with jobs.factory() as db:
        job = db.get(DocumentJob, record.job_id)
        attachment = db.get(DocumentFile, record.file_id)
        assert job.state == JobState.FAILED and job.attempts == 1
        assert attachment.processing_status == ProcessingStatus.FAILED
        assert attachment.error_code == "invalid_extraction_result"
        assert db.scalars(select(DocumentSection.text)).all() == ["Prior result"]


def test_publication_database_failure_rolls_back_sections_file_and_job(jobs, db_env):
    record = jobs.add()
    with jobs.factory() as db, db.begin():
        db.add(DocumentSection(document_id=record.document_id, ordinal=0, text="Prior result"))
    claim = jobs.queue.claim()
    assert jobs.queue.begin(claim) is not None

    def reject_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("insert into document_sections"):
            raise OperationalError("redacted", {}, Exception("injected database failure"))

    event.listen(db_env, "after_cursor_execute", reject_insert)
    try:
        with pytest.raises(OperationalError):
            jobs.queue.succeed(claim, result("Replacement", "Another section"))
    finally:
        event.remove(db_env, "after_cursor_execute", reject_insert)
    with jobs.factory() as db:
        assert db.scalars(select(DocumentSection.text)).all() == ["Prior result"]
        assert db.get(DocumentJob, record.job_id).state == JobState.RUNNING
        attachment = db.get(DocumentFile, record.file_id)
        assert attachment.processing_status == ProcessingStatus.PROCESSING
        assert attachment.processed_at is None and attachment.parser_version is None
    assert jobs.queue.succeed(claim, result("Replacement"))


def test_publication_rechecks_expiry_at_final_database_write(jobs, db_env):
    record = jobs.add()
    claim = jobs.queue.claim()
    assert jobs.queue.begin(claim) is not None

    def expire_during_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("insert into document_sections"):
            connection.execute(
                update(DocumentJob)
                .where(DocumentJob.id == claim.id)
                .values(
                    started_at=func.clock_timestamp() - text("interval '5 minutes'"),
                    lease_expires_at=func.clock_timestamp() - text("interval '1 second'"),
                )
            )

    event.listen(db_env, "after_cursor_execute", expire_during_insert)
    try:
        assert not jobs.queue.succeed(claim, result("Must roll back"))
    finally:
        event.remove(db_env, "after_cursor_execute", expire_during_insert)
    with jobs.factory() as db:
        assert db.scalar(select(func.count()).select_from(DocumentSection)) == 0
        assert db.get(DocumentFile, record.file_id).processing_status == ProcessingStatus.PROCESSING
        assert db.get(DocumentJob, record.job_id).state == JobState.RUNNING


def test_begin_rechecks_expiry_and_rolls_back_processing_status(jobs, db_env):
    record = jobs.add()
    claim = jobs.queue.claim()

    def expire_during_file_update(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("update document_files"):
            connection.execute(
                update(DocumentJob)
                .where(DocumentJob.id == claim.id)
                .values(
                    started_at=func.clock_timestamp() - text("interval '5 minutes'"),
                    lease_expires_at=func.clock_timestamp() - text("interval '1 second'"),
                )
            )

    event.listen(db_env, "after_cursor_execute", expire_during_file_update)
    try:
        assert jobs.queue.begin(claim) is None
    finally:
        event.remove(db_env, "after_cursor_execute", expire_during_file_update)
    with jobs.factory() as db:
        assert db.get(DocumentFile, record.file_id).processing_status == ProcessingStatus.QUEUED
        assert db.get(DocumentJob, record.job_id).state == JobState.RUNNING


def test_deleted_document_cannot_be_published_and_cleanup_keeps_both_tombstones(jobs):
    record = jobs.add()
    claim = jobs.queue.claim()
    assert jobs.queue.begin(claim) is not None
    remove_document(jobs, record)
    assert jobs.queue.begin(claim) is None
    assert not jobs.queue.succeed(claim, result("Deleted"))
    assert not jobs.queue.fail(claim, "parser_timeout")
    assert not jobs.queue.renew(claim)
    cleanup = jobs.queue.claim()
    assert cleanup.kind == JobKind.DELETE_FILES and cleanup.document_id is None
    assert jobs.queue.cleanup(cleanup, jobs.storage)
    assert not jobs.storage.object_path(record.key).exists()
    with jobs.factory() as db:
        assert db.get(Document, record.document_id) is None
        assert db.get(DocumentFile, record.file_id) is None
        assert db.scalar(select(func.count()).select_from(DocumentSection)) == 0
        history = db.scalars(select(DocumentJob).order_by(DocumentJob.created_at)).all()
        assert {job.state for job in history} == {JobState.CANCELLED, JobState.SUCCEEDED}
        assert len(history) == 2 and all(job.document_id is None for job in history)
    assert jobs.queue.claim() is None


def test_delete_during_parser_work_prevents_late_publication(jobs):
    record = jobs.add()
    claim = jobs.queue.claim()
    parsing = Event()
    finish_parse = Event()

    def parse_and_publish():
        assert jobs.queue.begin(claim) is not None
        parsing.set()
        assert finish_parse.wait(5)
        return jobs.queue.succeed(claim, result("Late parser result"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(parse_and_publish)
        try:
            assert parsing.wait(3)
            remove_document(jobs, record)
        finally:
            finish_parse.set()
        assert not future.result(timeout=5)
    with jobs.factory() as db:
        assert db.scalar(select(func.count()).select_from(DocumentSection)) == 0
        assert db.get(DocumentJob, record.job_id).state == JobState.CANCELLED


def test_file_identity_prevents_result_or_failure_from_affecting_a_replacement(jobs):
    record = jobs.add()
    claim = jobs.queue.claim()
    assert jobs.queue.begin(claim) is not None
    new_key = new_storage_key(jobs.workspace_id)
    with jobs.factory() as db, db.begin():
        db.get(DocumentFile, record.file_id).storage_key = new_key
    assert jobs.queue.begin(claim) is None
    assert not jobs.queue.succeed(claim, result("Wrong original"))
    assert not jobs.queue.fail(claim, "parser_timeout", retryable=False)
    with jobs.factory() as db:
        assert db.get(DocumentFile, record.file_id).storage_key == new_key
        assert db.scalar(select(func.count()).select_from(DocumentSection)) == 0
        assert db.get(DocumentJob, record.job_id).state == JobState.CANCELLED


@pytest.mark.parametrize("missing", ["document", "file", "exhausted_document"])
def test_detached_input_retires_only_owned_extraction_fence(jobs, missing):
    record = jobs.add(status=DocumentStatus.ARCHIVED)
    if missing == "exhausted_document":
        with jobs.factory() as db, db.begin():
            db.get(DocumentJob, record.job_id).max_attempts = 1
    claim = jobs.queue.claim()
    assert jobs.queue.begin(claim) is not None
    with jobs.factory() as db, db.begin():
        if missing == "file":
            db.execute(delete(DocumentFile).where(DocumentFile.id == record.file_id))
        else:
            # A direct FK SET NULL deletion has not used the API's cancellation.
            db.execute(delete(Document).where(Document.id == record.document_id))
    if missing == "exhausted_document":
        expire(jobs.factory, claim)
        claim = jobs.queue.claim()
        assert claim.exhausted and claim.document_id is None
        assert jobs.queue.fail(claim, "attempts_exhausted", retryable=False)
    else:
        assert jobs.queue.begin(claim) is None
    assert not jobs.queue.succeed(claim, result("No resurrection"))
    with jobs.factory() as db:
        job = db.get(DocumentJob, record.job_id)
        assert job.state == JobState.CANCELLED
        assert job.lease_token is None and job.lease_expires_at is None
        assert job.finished_at is not None and job.error_code == "input_unavailable"
        assert db.scalar(select(func.count()).select_from(DocumentSection)) == 0
        if missing == "file":
            assert db.get(Document, record.document_id).status == DocumentStatus.ARCHIVED
        else:
            assert db.get(Document, record.document_id) is None
    assert jobs.queue.claim() is None


def test_missing_workspace_cleanup_keeps_bytes_and_becomes_terminal_after_retries(jobs):
    record = jobs.add(kind=JobKind.DELETE_FILES, original=False)
    with jobs.factory() as db, db.begin():
        db.execute(delete(Document).where(Document.id == record.document_id))
        db.execute(delete(Workspace).where(Workspace.id == jobs.workspace_id))
    for attempt in range(1, 4):
        claim = jobs.queue.claim()
        assert claim.attempts == attempt and claim.document_id is None
        assert not jobs.queue.cleanup(claim, jobs.storage)
        assert jobs.queue.fail(claim, "cleanup_deferred")
        make_available(jobs.factory, claim.id)
    assert jobs.queue.claim() is None
    assert jobs.storage.object_path(record.key).exists()
    with jobs.factory() as db:
        job = db.get(DocumentJob, record.job_id)
        assert job.state == JobState.FAILED and job.attempts == 3
        assert job.finished_at is not None and job.error_code == "cleanup_deferred"


def test_queue_explicitly_uses_read_committed_with_repeatable_read_engine(jobs, db_env):
    record = jobs.add()
    engine = db_env.execution_options(isolation_level="REPEATABLE READ")
    queue = DurableJobQueue(sessionmaker(engine))
    isolation = []

    def observe_isolation(connection, cursor, statement, parameters, context, executemany):
        if statement == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED":
            isolation.append(connection.exec_driver_sql("SHOW transaction_isolation").scalar())

    event.listen(engine, "after_cursor_execute", observe_isolation)
    try:
        claim = queue.claim()
        assert claim.id == record.job_id
        assert queue.begin(claim) is not None
        assert queue.renew(claim)
        assert queue.succeed(claim, result("Fresh snapshot"))
    finally:
        event.remove(engine, "after_cursor_execute", observe_isolation)
    assert isolation == ["read committed"] * 4


def test_cleanup_refuses_any_live_file_reference_and_wrong_or_expired_fence(jobs):
    record = jobs.add(kind=JobKind.DELETE_FILES)
    claim = jobs.queue.claim()
    assert not jobs.queue.cleanup(claim, jobs.storage)
    assert jobs.storage.object_path(record.key).exists()
    with jobs.factory() as db, db.begin():
        db.execute(delete(DocumentFile).where(DocumentFile.id == record.file_id))
    assert not jobs.queue.cleanup(replace(claim, lease_token=uuid4()), jobs.storage)
    expire(jobs.factory, claim)
    assert not jobs.queue.cleanup(claim, jobs.storage)
    assert jobs.storage.object_path(record.key).exists()
    reclaimed = jobs.queue.claim()
    assert reclaimed.lease_token != claim.lease_token
    assert jobs.queue.cleanup(reclaimed, jobs.storage)
    with jobs.factory() as db:
        assert db.get(DocumentJob, record.job_id).state == JobState.SUCCEEDED


def test_cleanup_waits_for_upload_workspace_barrier_before_checking_reference(jobs, db_env):
    record = jobs.add(kind=JobKind.DELETE_FILES, original=False)
    claim = jobs.queue.claim()
    started = Event()
    calls = []
    backend_pid = []
    queue = DurableJobQueue(
        sessionmaker(db_env.execution_options(isolation_level="REPEATABLE READ"))
    )

    def observe_query(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("cleanup-barrier") and "FROM workspaces" in statement:
            backend_pid[:] = [connection.connection.driver_connection.info.backend_pid]
            started.set()

    class ObservedStorage:
        def delete(self, key):
            calls.append(key)
            return jobs.storage.delete(key)

    def cleanup():
        return queue.cleanup(claim, ObservedStorage())

    event.listen(db_env, "before_cursor_execute", observe_query)
    with jobs.factory() as pending:
        pending.get(Workspace, jobs.workspace_id, with_for_update=True)
        document = pending.get(Document, record.document_id)
        owner_id = pending.get(Workspace, jobs.workspace_id).owner_user_id
        pending.add(
            DocumentFile(
                id=record.file_id,
                document_id=record.document_id,
                storage_key=record.key,
                original_filename=document.filename,
                sha256="a" * 64,
                uploaded_by=owner_id,
            )
        )
        pending.flush()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleanup-barrier") as pool:
            future = pool.submit(cleanup)
            try:
                assert started.wait(3)
                deadline = monotonic() + 2
                with db_env.connect() as observer:
                    while monotonic() < deadline:
                        assert not future.done(), "Cleanup crossed the upload workspace barrier."
                        if observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE pid = :pid AND NOT granted)"
                            ),
                            {"pid": backend_pid[0]},
                        ):
                            break
                        Event().wait(0.01)
                    else:
                        pytest.fail("Cleanup never waited for the upload workspace lock.")
                assert calls == []
                pending.commit()
            finally:
                pending.rollback()
                event.remove(db_env, "before_cursor_execute", observe_query)
            assert not future.result(timeout=5)
    assert calls == [] and jobs.storage.object_path(record.key).exists()


def test_cleanup_storage_failure_can_retry_and_missing_original_is_success(jobs):
    record = jobs.add(kind=JobKind.DELETE_FILES, original=False)
    claim = jobs.queue.claim()

    class UnavailableStorage:
        def delete(self, key):
            raise StorageError("Injected storage failure")

    with pytest.raises(StorageError):
        jobs.queue.cleanup(claim, UnavailableStorage())
    assert jobs.queue.fail(claim, "storage_unavailable")
    make_available(jobs.factory, claim.id)
    reclaimed = jobs.queue.claim()
    jobs.storage.delete(record.key)
    assert jobs.queue.cleanup(reclaimed, jobs.storage)
    assert not jobs.queue.cleanup(reclaimed, jobs.storage)
    with jobs.factory() as db:
        job = db.get(DocumentJob, record.job_id)
        assert job.state == JobState.SUCCEEDED and job.attempts == 2


def test_cleanup_commit_failure_can_repeat_physical_delete_safely(jobs):
    record = jobs.add(kind=JobKind.DELETE_FILES, original=False)
    claim = jobs.queue.claim()

    class CommitFailureSession(Session):
        pass

    failing_queue = DurableJobQueue(
        sessionmaker(jobs.factory.kw["bind"], class_=CommitFailureSession)
    )

    def reject_commit(db):
        raise OperationalError("redacted", {}, Exception("Injected COMMIT failure"))

    event.listen(CommitFailureSession, "before_commit", reject_commit)
    try:
        with pytest.raises(OperationalError):
            failing_queue.cleanup(claim, jobs.storage)
    finally:
        event.remove(CommitFailureSession, "before_commit", reject_commit)
    assert not jobs.storage.object_path(record.key).exists()
    with jobs.factory() as db:
        assert db.get(DocumentJob, record.job_id).state == JobState.RUNNING
    assert jobs.queue.cleanup(claim, jobs.storage)
    with jobs.factory() as db:
        assert db.get(DocumentJob, record.job_id).state == JobState.SUCCEEDED
