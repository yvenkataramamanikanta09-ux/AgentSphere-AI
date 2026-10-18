"""Durable PostgreSQL leases and fenced publication for document workers.

Claiming holds only a job row for a short transaction. Every operation that
touches a document uses the API's workspace, document, file, job lock order.
Job addresses and completed history survive document deletion.
"""

import math
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from agentsphere.extraction import ExtractionResult
from agentsphere.models import (
    Document,
    DocumentFile,
    DocumentJob,
    DocumentSection,
    JobKind,
    JobState,
    ProcessingStatus,
    Workspace,
)
from agentsphere.storage import PrivateStorage, validate_storage_key

SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


@dataclass(frozen=True)
class Claim:
    id: UUID
    document_id: UUID | None
    storage_key: str
    kind: JobKind
    lease_token: UUID
    attempts: int
    exhausted: bool = False


@dataclass(frozen=True)
class InputRecord:
    document_id: UUID
    workspace_id: UUID
    key: str
    size_bytes: int
    sha256: str
    content_type: str


class _LostLease(Exception):
    """Roll back changes if the lease expires during a publication or unlink."""


class DurableJobQueue:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        lease_seconds: float = 120,
        retry_base_seconds: float = 2,
    ):
        for value in (lease_seconds, retry_base_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Worker timing values must be positive numbers.")
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Worker timing values must be positive numbers.")
        self.session_factory = session_factory
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self.session_factory() as db, db.begin():
            # Reference checks after the workspace barrier must see uploads
            # committed while we waited, even if the supplied engine defaults
            # to REPEATABLE READ. Apply before the transaction's first query.
            db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            yield db

    @staticmethod
    def _now(db: Session) -> datetime:
        # clock_timestamp(), unlike now(), advances while row locks are awaited.
        return db.scalar(select(func.clock_timestamp()))

    @staticmethod
    def _claim(job: DocumentJob, *, exhausted: bool = False) -> Claim:
        return Claim(
            id=job.id,
            document_id=job.document_id,
            storage_key=job.storage_key,
            kind=job.kind,
            lease_token=job.lease_token,
            attempts=job.attempts,
            exhausted=exhausted,
        )

    def claim(self) -> Claim | None:
        """Lease one available job without waiting on another worker's job lock."""
        with self._transaction() as db:
            job = db.scalar(
                select(DocumentJob)
                .where(
                    or_(
                        and_(
                            DocumentJob.state == JobState.QUEUED,
                            DocumentJob.available_at <= func.clock_timestamp(),
                            DocumentJob.attempts < DocumentJob.max_attempts,
                        ),
                        and_(
                            DocumentJob.state == JobState.RUNNING,
                            DocumentJob.lease_expires_at <= func.clock_timestamp(),
                        ),
                    )
                )
                .order_by(DocumentJob.available_at, DocumentJob.created_at, DocumentJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            if job.attempts >= job.max_attempts:
                # Do not take document locks while holding this job lock. The
                # caller terminal-fails this expired fence in the normal order.
                return self._claim(job, exhausted=True)
            timestamp = self._now(db)
            job.state = JobState.RUNNING
            job.attempts += 1
            job.lease_token = uuid4()
            job.started_at = timestamp
            job.lease_expires_at = timestamp + timedelta(seconds=self.lease_seconds)
            job.finished_at = None
            job.error_code = None
            job.updated_at = timestamp
            db.flush()
            return self._claim(job)

    @staticmethod
    def _identity(claim: Claim, *, allow_detached: bool = False):
        document_matches = DocumentJob.document_id == claim.document_id
        if allow_detached:
            document_matches = or_(document_matches, DocumentJob.document_id.is_(None))
        return (
            DocumentJob.id == claim.id,
            document_matches,
            DocumentJob.storage_key == claim.storage_key,
            DocumentJob.kind == claim.kind,
            DocumentJob.state == JobState.RUNNING,
            DocumentJob.lease_token == claim.lease_token,
            DocumentJob.attempts == claim.attempts,
        )

    def renew(self, claim: Claim) -> bool:
        """Heartbeat only the still-live fence in an independent short transaction."""
        if claim.exhausted:
            return False
        with self._transaction() as db:
            # UPDATE can evaluate its time predicate before waiting on a row
            # lock, without rechecking it if the blocker leaves no new version.
            # Take the job lock first, then sample the clock under that lock.
            job = db.scalar(select(DocumentJob).where(*self._identity(claim)).with_for_update())
            if not self._live(db, job, claim):
                return False
            return (
                db.scalar(
                    update(DocumentJob)
                    .where(
                        *self._identity(claim),
                        DocumentJob.lease_expires_at > func.clock_timestamp(),
                    )
                    .values(
                        lease_expires_at=func.clock_timestamp()
                        + timedelta(seconds=self.lease_seconds),
                        updated_at=func.clock_timestamp(),
                    )
                    .returning(DocumentJob.id)
                )
                is not None
            )

    def _lock(self, db: Session, claim: Claim):
        try:
            workspace_id = UUID(hex=validate_storage_key(claim.storage_key).split("/")[1])
        except (ValueError, TypeError, AttributeError):
            return None
        workspace = db.get(Workspace, workspace_id, with_for_update=True)
        document = None
        attachment = None
        if workspace is not None and claim.document_id is not None:
            document = db.scalar(
                select(Document)
                .where(
                    Document.id == claim.document_id,
                    Document.workspace_id == workspace_id,
                )
                .with_for_update()
            )
        if document is not None:
            attachment = db.scalar(
                select(DocumentFile)
                .where(DocumentFile.document_id == document.id)
                .with_for_update()
            )
        job = db.get(DocumentJob, claim.id, with_for_update=True)
        return document, attachment, job, workspace is not None

    @staticmethod
    def _matches(job: DocumentJob | None, claim: Claim, *, allow_detached=False) -> bool:
        return (
            job is not None
            and job.id == claim.id
            and (
                job.document_id == claim.document_id or (allow_detached and job.document_id is None)
            )
            and job.storage_key == claim.storage_key
            and job.kind == claim.kind
            and job.state == JobState.RUNNING
            and job.lease_token == claim.lease_token
            and job.attempts == claim.attempts
        )

    def _live(self, db: Session, job: DocumentJob | None, claim: Claim) -> bool:
        return (
            not claim.exhausted
            and self._matches(job, claim)
            and job.lease_expires_at > self._now(db)
        )

    def _cancel_detached(self, db: Session, job: DocumentJob | None, claim: Claim) -> bool:
        """Retire obsolete extraction without touching a missing/replaced input."""
        if claim.kind != JobKind.EXTRACT or not self._matches(job, claim, allow_detached=True):
            return False
        timestamp = self._now(db)
        if claim.exhausted:
            if job.attempts < job.max_attempts or job.lease_expires_at > timestamp:
                return False
        elif job.lease_expires_at <= timestamp:
            return False
        self._finish(
            db,
            claim,
            dict(
                state=JobState.CANCELLED,
                error_code="input_unavailable",
                lease_token=None,
                lease_expires_at=None,
                finished_at=func.clock_timestamp(),
            ),
            exhausted=claim.exhausted,
            allow_detached=True,
        )
        return True

    @staticmethod
    def _attachment_matches(document, attachment, claim: Claim) -> bool:
        return (
            claim.kind == JobKind.EXTRACT
            and document is not None
            and attachment is not None
            and attachment.document_id == claim.document_id
            and attachment.storage_key == claim.storage_key
        )

    def begin(self, claim: Claim) -> InputRecord | None:
        """Read immutable parser input and mark processing under the current fence."""
        try:
            with self._transaction() as db:
                locked = self._lock(db, claim)
                if locked is None:
                    return None
                document, attachment, job, _ = locked
                if not self._attachment_matches(document, attachment, claim):
                    self._cancel_detached(db, job, claim)
                    return None
                if not self._live(db, job, claim):
                    return None
                attachment.processing_status = ProcessingStatus.PROCESSING
                attachment.error_code = None
                attachment.warning_code = None
                attachment.parser_version = None
                attachment.processed_at = None
                self._finish(db, claim, {})
                return InputRecord(
                    document_id=document.id,
                    workspace_id=document.workspace_id,
                    key=attachment.storage_key,
                    size_bytes=document.size_bytes,
                    sha256=attachment.sha256,
                    content_type=document.content_type,
                )
        except _LostLease:
            return None

    def _finish(
        self, db: Session, claim: Claim, values: dict, *, exhausted=False, allow_detached=False
    ) -> None:
        condition = DocumentJob.lease_expires_at > func.clock_timestamp()
        if exhausted:
            condition = and_(
                DocumentJob.lease_expires_at <= func.clock_timestamp(),
                DocumentJob.attempts >= DocumentJob.max_attempts,
            )
        # Flush sections/file first, then check DB time again at the final write.
        # A missed condition raises to roll back every preceding database write.
        db.flush()
        changed = db.scalar(
            update(DocumentJob)
            .where(*self._identity(claim, allow_detached=allow_detached), condition)
            .values(**values, updated_at=func.clock_timestamp())
            .returning(DocumentJob.id)
            .execution_options(synchronize_session=False)
        )
        if changed is None:
            raise _LostLease

    def succeed(self, claim: Claim, result: ExtractionResult) -> bool:
        """Publish sections, READY, and SUCCEEDED as one fenced transaction."""
        try:
            # Revalidate even an instance made with Pydantic model_construct().
            payload = result.model_dump() if isinstance(result, ExtractionResult) else result
            result = ExtractionResult.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            self.fail(claim, "invalid_extraction_result", retryable=False)
            return False
        try:
            with self._transaction() as db:
                locked = self._lock(db, claim)
                if locked is None:
                    return False
                document, attachment, job, _ = locked
                if not self._attachment_matches(document, attachment, claim):
                    self._cancel_detached(db, job, claim)
                    return False
                if not self._live(db, job, claim):
                    return False
                db.execute(
                    delete(DocumentSection).where(DocumentSection.document_id == document.id)
                )
                db.add_all(
                    DocumentSection(
                        document_id=document.id, ordinal=ordinal, **section.model_dump()
                    )
                    for ordinal, section in enumerate(result.sections)
                )
                attachment.processing_status = ProcessingStatus.READY
                attachment.error_code = None
                attachment.warning_code = result.warning_code
                attachment.parser_version = result.parser_version
                attachment.processed_at = self._now(db)
                self._finish(
                    db,
                    claim,
                    dict(
                        state=JobState.SUCCEEDED,
                        error_code=None,
                        lease_token=None,
                        lease_expires_at=None,
                        finished_at=func.clock_timestamp(),
                    ),
                )
            return True
        except _LostLease:
            return False

    def fail(self, claim: Claim, code: str, retryable: bool = True) -> bool:
        """Retry with bounded backoff, or terminal-fail only the matching input."""
        code = code if isinstance(code, str) and SAFE_CODE.fullmatch(code) else "processing_failed"
        try:
            with self._transaction() as db:
                locked = self._lock(db, claim)
                if locked is None:
                    return False
                document, attachment, job, _ = locked
                if claim.kind == JobKind.EXTRACT and not self._attachment_matches(
                    document, attachment, claim
                ):
                    return self._cancel_detached(db, job, claim)
                if not self._matches(job, claim):
                    return False
                timestamp = self._now(db)
                if claim.exhausted:
                    if job.attempts < job.max_attempts or job.lease_expires_at > timestamp:
                        return False
                elif job.lease_expires_at <= timestamp:
                    return False
                retry = retryable and not claim.exhausted and job.attempts < job.max_attempts
                if attachment is not None and claim.kind == JobKind.EXTRACT:
                    attachment.processing_status = (
                        ProcessingStatus.QUEUED if retry else ProcessingStatus.FAILED
                    )
                    attachment.error_code = None if retry else code
                    attachment.warning_code = None
                    attachment.parser_version = None
                    attachment.processed_at = None if retry else timestamp
                values = dict(
                    state=JobState.QUEUED if retry else JobState.FAILED,
                    error_code=code,
                    lease_token=None,
                    lease_expires_at=None,
                    finished_at=None if retry else func.clock_timestamp(),
                )
                if retry:
                    delay = min(60, self.retry_base_seconds * (2 ** min(job.attempts - 1, 63)))
                    values["available_at"] = timestamp + timedelta(seconds=delay)
                self._finish(db, claim, values, exhausted=claim.exhausted)
            return True
        except _LostLease:
            return False

    def cleanup(self, claim: Claim, storage: PrivateStorage) -> bool:
        """Unlink an unreferenced key with its workspace and current fence locked.

        The workspace lock is also the upload finalization/compensation barrier.
        Physical deletion is idempotent: a failed or uncertain COMMIT can safely
        repeat an unlink, and the retained job remains the durable tombstone.
        """
        if claim.kind != JobKind.DELETE_FILES or claim.exhausted:
            return False
        try:
            with self._transaction() as db:
                locked = self._lock(db, claim)
                if locked is None:
                    return False
                _, _, job, workspace_available = locked
                if not workspace_available or not self._live(db, job, claim):
                    return False
                # The workspace barrier prevents a concurrent upload from
                # attaching this immutable key after this reference check.
                if (
                    db.scalar(
                        select(DocumentFile.id).where(DocumentFile.storage_key == claim.storage_key)
                    )
                    is not None
                ):
                    return False
                storage.delete(claim.storage_key)
                self._finish(
                    db,
                    claim,
                    dict(
                        state=JobState.SUCCEEDED,
                        error_code=None,
                        lease_token=None,
                        lease_expires_at=None,
                        finished_at=func.clock_timestamp(),
                    ),
                )
            return True
        except _LostLease:
            return False
