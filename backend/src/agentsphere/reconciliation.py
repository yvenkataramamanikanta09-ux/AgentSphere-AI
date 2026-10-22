"""Conservative maintenance: queue old private orphans, retain durable job history."""

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from agentsphere.document_lifecycle import require_storage_schema
from agentsphere.models import DocumentFile, DocumentJob, JobKind, JobState, Workspace
from agentsphere.storage import PrivateStorage, StorageBusy, StorageError


@dataclass
class ReconcileStats:
    scanned: int = 0
    objects_queued: int = 0
    temporaries_removed: int = 0
    referenced_retained: int = 0
    young_retained: int = 0
    deferred: int = 0
    complete: bool = True
    error_code: str | None = None


def _healthy(db):
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    if db.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise ValueError("Reconciliation requires READ COMMITTED isolation.")
    require_storage_schema(db)
    # 0004 is additive; the original-file reconciliation algorithm is unchanged.
    if db.scalar(text("SELECT version_num FROM alembic_version")) not in {"0003", "0004"}:
        raise ValueError("Reconciliation requires schema revision 0003 or 0004.")


def reconcile(
    session_factory,
    storage: PrivateStorage,
    *,
    min_age_seconds: int = 86_400,
    max_entries: int = 10_000,
    now: datetime | None = None,
) -> ReconcileStats:
    """Hold an exclusive process fence before any inventory or database decision.

    Physical originals are never removed here. A healthy database and locked
    existing workspace are required before a durable cleanup job is queued.
    Old scratch files need the same fence and healthy database before removal.
    Terminal jobs, including idempotency tombstones, are never purged.
    """
    if type(min_age_seconds) is not int or min_age_seconds < 3600:
        raise ValueError("Reconciliation grace must be at least 3600 seconds.")
    if type(max_entries) is not int or max_entries < 1:
        raise ValueError("Reconciliation requires a positive entry budget.")
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Reconciliation clock must be timezone aware.")
    result = ReconcileStats()
    try:
        with storage.activity(exclusive=True):
            with session_factory() as db, db.begin():
                _healthy(db)
            inventory = storage.inventory(max_entries=max_entries)
            result.scanned = inventory.scanned
            if not inventory.complete:
                result.complete = False
                result.error_code = "inventory_budget_exhausted"
                return result
            for entry in inventory.entries:
                if timestamp.timestamp() - entry.modified_at < min_age_seconds:
                    result.young_retained += 1
                    continue
                with session_factory() as db, db.begin():
                    _healthy(db)
                    if entry.kind == "temporary":
                        storage.remove_temporary(entry)
                        result.temporaries_removed += 1
                        continue
                    workspace = db.scalar(
                        select(Workspace.id)
                        .where(Workspace.id == entry.workspace_id)
                        .with_for_update()
                    )
                    if workspace is None:
                        result.deferred += 1
                        continue
                    referenced = db.scalar(
                        select(DocumentFile.id).where(DocumentFile.storage_key == entry.storage_key)
                    )
                    active_job = db.scalar(
                        select(DocumentJob.id).where(
                            DocumentJob.storage_key == entry.storage_key,
                            DocumentJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
                        )
                    )
                    if referenced is not None or active_job is not None:
                        result.referenced_retained += 1
                        continue
                    storage.verify_entry(entry)
                    db.add(DocumentJob(storage_key=entry.storage_key, kind=JobKind.DELETE_FILES))
                    db.flush()
                result.objects_queued += 1
            return result
    except StorageBusy:
        result.complete = False
        result.error_code = "storage_busy"
    except StorageError:
        result.complete = False
        result.error_code = "storage_unavailable"
    except (SQLAlchemyError, HTTPException, ValueError):
        result.complete = False
        result.error_code = "database_unavailable"
    return result
