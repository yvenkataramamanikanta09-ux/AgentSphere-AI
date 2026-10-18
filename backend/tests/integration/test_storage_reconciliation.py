"""Reconciliation runs only inside pytest's disposable PostgreSQL schema and roots."""

import io
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from agentsphere.models import (
    Document,
    DocumentFile,
    DocumentJob,
    JobKind,
    JobState,
    Workspace,
)
from agentsphere.reconciliation import reconcile
from agentsphere.storage import PrivateStorage, new_storage_key

pytestmark = pytest.mark.postgres


@pytest.fixture
def storage(tmp_path):
    instance = PrivateStorage(tmp_path / "private")
    instance.initialize()
    return instance


@pytest.fixture
def factory(db_env):
    return sessionmaker(db_env, expire_on_commit=False)


@pytest.fixture
def instant():
    return datetime.now(UTC)


def original(storage, workspace_id, instant, age=200_000):
    key = new_storage_key(UUID(workspace_id))
    result = storage.put(key, io.BytesIO(b"private original"), max_bytes=16)
    path = storage.object_path(key)
    timestamp = instant.timestamp() - age
    os.utime(path, (timestamp, timestamp))
    return result


def temporary(storage, instant, age=200_000):
    path = storage.root / "tmp" / f"{uuid4().hex}.part"
    path.write_bytes(b"abandoned temporary bytes")
    timestamp = instant.timestamp() - age
    os.utime(path, (timestamp, timestamp))
    return path


def test_old_unreferenced_original_queues_durable_cleanup_without_deleting_bytes(
    db_client, factory, storage, instant
):
    stored = original(storage, db_client.workspace_id, instant)
    result = reconcile(factory, storage, now=instant)
    assert result.complete and result.error_code is None
    assert result.objects_queued == 1 and result.temporaries_removed == 0
    assert storage.object_path(stored.key).read_bytes() == b"private original"
    with factory() as db:
        job = db.scalar(select(DocumentJob))
        assert job.kind == JobKind.DELETE_FILES and job.state == JobState.QUEUED
        assert job.document_id is None and job.storage_key == stored.key
    again = reconcile(factory, storage, now=instant)
    assert again.objects_queued == 0 and again.referenced_retained == 1


def test_old_scratch_removed_only_after_healthy_database_check(factory, storage, instant):
    scratch = temporary(storage, instant)
    result = reconcile(factory, storage, now=instant)
    assert result.complete and result.temporaries_removed == 1
    assert not scratch.exists()


def test_recent_original_and_temporary_are_retained(db_client, factory, storage, instant):
    stored = original(storage, db_client.workspace_id, instant, age=3600)
    scratch = temporary(storage, instant, age=3599)
    result = reconcile(factory, storage, now=instant)
    assert result.complete and result.young_retained == 2
    assert result.objects_queued == result.temporaries_removed == 0
    assert scratch.exists() and storage.object_path(stored.key).exists()


def test_future_modified_times_are_retained(db_client, factory, storage, instant):
    stored = original(storage, db_client.workspace_id, instant, age=-1)
    result = reconcile(factory, storage, now=instant, min_age_seconds=3600)
    assert result.young_retained == 1 and result.objects_queued == 0
    assert storage.object_path(stored.key).exists()


def test_attachment_reference_prevents_orphan_cleanup(db_client, factory, storage, instant):
    stored = original(storage, db_client.workspace_id, instant)
    with factory() as db, db.begin():
        document = Document(
            workspace_id=UUID(db_client.workspace_id),
            filename="notes.txt",
            content_type="text/plain",
            size_bytes=stored.size_bytes,
        )
        db.add(document)
        db.flush()
        db.add(
            DocumentFile(
                document_id=document.id,
                storage_key=stored.key,
                original_filename="notes.txt",
                sha256=stored.sha256,
                uploaded_by=UUID(db_client.user_id),
            )
        )
    result = reconcile(factory, storage, now=instant)
    assert result.complete and result.referenced_retained == 1
    assert result.objects_queued == 0
    assert storage.object_path(stored.key).exists()
    with factory() as db:
        assert db.scalar(select(DocumentJob)) is None


@pytest.mark.parametrize("kind", [JobKind.EXTRACT, JobKind.DELETE_FILES])
def test_any_active_job_prevents_additional_cleanup(db_client, factory, storage, instant, kind):
    stored = original(storage, db_client.workspace_id, instant)
    with factory() as db, db.begin():
        db.add(DocumentJob(storage_key=stored.key, kind=kind))
    result = reconcile(factory, storage, now=instant)
    assert result.complete and result.referenced_retained == 1
    assert result.objects_queued == 0


@pytest.mark.parametrize("state", [JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED])
def test_terminal_job_tombstones_survive_reconciliation(
    db_client, factory, storage, instant, state
):
    stored = original(storage, db_client.workspace_id, instant)
    tombstone_id = uuid4()
    with factory() as db, db.begin():
        db.add(
            DocumentJob(
                id=tombstone_id,
                storage_key=stored.key,
                kind=JobKind.DELETE_FILES,
                state=state,
                finished_at=instant - timedelta(days=3),
            )
        )
    result = reconcile(factory, storage, now=instant)
    assert result.complete and result.objects_queued == 1
    with factory() as db:
        assert db.get(DocumentJob, tombstone_id).state == state
        assert len(db.scalars(select(DocumentJob)).all()) == 2


def test_unknown_workspace_is_retained_without_assuming_a_safe_lock(factory, storage, instant):
    stored = original(storage, str(uuid4()), instant)
    result = reconcile(factory, storage, now=instant)
    assert result.deferred == 1 and result.objects_queued == 0
    assert storage.object_path(stored.key).exists()


def test_database_failure_retains_old_objects_and_parts(storage, instant):
    stored = original(storage, str(uuid4()), instant)
    scratch = temporary(storage, instant)

    def unavailable():
        raise OperationalError("redacted", {}, Exception("secret database address"))

    result = reconcile(unavailable, storage, now=instant)
    assert not result.complete and result.error_code == "database_unavailable"
    assert result.objects_queued == result.temporaries_removed == 0
    assert scratch.exists() and storage.object_path(stored.key).exists()
    assert "secret database address" not in str(result)


def test_repeatable_read_database_is_not_used_for_cleanup(db_env, storage, instant):
    scratch = temporary(storage, instant)
    factory = sessionmaker(db_env.execution_options(isolation_level="REPEATABLE READ"))
    result = reconcile(factory, storage, now=instant)
    assert result.error_code == "database_unavailable" and not result.complete
    assert scratch.exists()


def test_active_upload_fence_defers_before_any_database_or_inventory_access(storage, instant):
    with storage.activity(), storage.temporary() as handle:
        handle.write(b"upload still in progress")
        handle.flush()

        def forbidden():
            pytest.fail("An active upload must prevent even querying cleanup candidates.")

        result = reconcile(forbidden, storage, now=instant)
        assert result.error_code == "storage_busy" and not result.complete
        handle.seek(0)
        assert handle.read() == b"upload still in progress"


def test_budget_exhaustion_prevents_all_removals_and_job_creation(
    db_client, factory, storage, instant
):
    stored = original(storage, db_client.workspace_id, instant)
    scratch = temporary(storage, instant)
    result = reconcile(factory, storage, now=instant, max_entries=1)
    assert result.error_code == "inventory_budget_exhausted" and not result.complete
    assert result.scanned == 1
    assert result.objects_queued == result.temporaries_removed == 0
    assert scratch.exists() and storage.object_path(stored.key).exists()
    with factory() as db:
        assert db.scalar(select(DocumentJob)) is None


def test_unexpected_inventory_leaf_fails_closed_without_removing_any_parts(
    factory, storage, instant
):
    scratch = temporary(storage, instant)
    unexpected = storage.root / "tmp" / "unmanaged"
    unexpected.write_bytes(b"untouched")
    result = reconcile(factory, storage, now=instant)
    assert result.error_code == "storage_unavailable" and not result.complete
    assert scratch.exists() and unexpected.read_bytes() == b"untouched"


def test_changed_temporary_after_inventory_is_not_deleted(factory, storage, instant, monkeypatch):
    scratch = temporary(storage, instant)
    verify = storage.verify_entry

    def changed(entry):
        scratch.write_bytes(b"newly active data")
        return verify(entry)

    monkeypatch.setattr(storage, "verify_entry", changed)
    result = reconcile(factory, storage, now=instant)
    assert result.error_code == "storage_unavailable" and not result.complete
    assert scratch.read_bytes() == b"newly active data"


def test_workspace_lock_timeout_defers_without_queueing(db_client, factory, storage, instant):
    stored = original(storage, db_client.workspace_id, instant)
    with factory() as owner, owner.begin():
        owner.scalar(
            select(Workspace.id)
            .where(Workspace.id == UUID(db_client.workspace_id))
            .with_for_update()
        )
        result = reconcile(factory, storage, now=instant)
    assert result.error_code == "database_unavailable" and not result.complete
    assert result.objects_queued == 0
    assert storage.object_path(stored.key).exists()
    with factory() as db:
        assert db.scalar(select(DocumentJob)) is None


@pytest.mark.parametrize("age", [0, 3599, True, 3600.0])
def test_reconciliation_refuses_unsafe_grace_settings(factory, storage, age):
    with pytest.raises(ValueError):
        reconcile(factory, storage, min_age_seconds=age)


def test_reconciliation_refuses_naive_clock(factory, storage):
    with pytest.raises(ValueError, match="timezone"):
        reconcile(factory, storage, now=datetime(2026, 10, 18))
