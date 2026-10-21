"""Database-side lifecycle, durable worker jobs and validated-file visibility."""

from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text

from agentsphere.models import DocumentFile, DocumentJob, JobKind, JobState, ProcessingStatus
from agentsphere.schemas.documents import FileRead

UPLOAD_NAMESPACE = UUID("ed09a1d0-a861-41a3-b97e-e722aa6a2118")
CLEANUP_NAMESPACE = UUID("7a3ea056-a73d-426c-b015-c5e06081bc27")


def upload_document_id(user_id, workspace_id, nonce):
    # A server-computed scoped UUID makes retries durable without changing 0003.
    # This identifier is not a credential; every lookup still requires authorization.
    return uuid5(UPLOAD_NAMESPACE, f"{user_id}:{workspace_id}:{nonce}")


def cleanup_job_id(document_id):
    return uuid5(CLEANUP_NAMESPACE, str(document_id))


def storage_schema_available(db):
    # Preserve metadata-only CRUD on the still-unmigrated development database.
    # Never run DDL or silently cascade file rows from a partially migrated schema.
    count = db.scalar(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name IN "
            "('document_files', 'document_sections', 'document_jobs')"
        )
    )
    if count not in (0, 3):
        raise HTTPException(503, "Document storage schema is incomplete.")
    return count == 3


def require_storage_schema(db):
    if not storage_schema_available(db):
        raise HTTPException(503, "Document storage requires the reviewed 0003 migration.")


def download_allowed(db, attachment):
    if (
        attachment.processing_status != ProcessingStatus.READY
        or attachment.processed_at is None
        or not attachment.parser_version
        or attachment.error_code is not None
    ):
        return False
    return (
        db.scalar(
            select(DocumentJob.id)
            .where(
                DocumentJob.document_id == attachment.document_id,
                DocumentJob.storage_key == attachment.storage_key,
                DocumentJob.kind == JobKind.EXTRACT,
                DocumentJob.state == JobState.SUCCEEDED,
            )
            .limit(1)
        )
        is not None
    )


def read_attachment(db, attachment):
    result = FileRead.model_validate(attachment)
    result.download_available = download_allowed(db, attachment)
    return result


def queue_file_deletion(db, document, timestamp):
    if not storage_schema_available(db):
        return
    attachment = db.scalar(
        select(DocumentFile).where(DocumentFile.document_id == document.id).with_for_update()
    )
    if attachment is None:
        return
    active = db.scalars(
        select(DocumentJob)
        .where(
            DocumentJob.document_id == document.id,
            DocumentJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        .with_for_update()
    ).all()
    for job in active:
        if job.kind == JobKind.EXTRACT:
            job.state = JobState.CANCELLED
            job.finished_at = timestamp
            job.lease_token = None
            job.lease_expires_at = None
    # Keep the independent key after document/file CASCADE and job SET NULL.
    existing = next((job for job in active if job.kind == JobKind.DELETE_FILES), None)
    if existing is None:
        db.add(
            DocumentJob(
                id=cleanup_job_id(document.id),
                document_id=document.id,
                storage_key=attachment.storage_key,
                kind=JobKind.DELETE_FILES,
            )
        )
    db.flush()
