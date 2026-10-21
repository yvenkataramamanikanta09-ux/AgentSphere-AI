"""Authorized multipart intake and retrieval gated by durable worker results."""

import asyncio
import hashlib
import logging
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from agentsphere.api.document_records import find_document
from agentsphere.database import get_session
from agentsphere.document_lifecycle import (
    cleanup_job_id,
    download_allowed,
    read_attachment,
    require_storage_schema,
    upload_document_id,
)
from agentsphere.models import (
    Document,
    DocumentFile,
    DocumentJob,
    DocumentSection,
    JobKind,
    JobState,
    Workspace,
)
from agentsphere.permissions import Access, require_editor, workspace_access
from agentsphere.schemas.documents import (
    DocumentMetadata,
    DocumentRead,
    FileRead,
    SectionPage,
    SectionRead,
    UploadRead,
)
from agentsphere.security import DatabaseSession, current_user
from agentsphere.storage import PrivateStorage, StorageBusy, StorageError, new_storage_key
from agentsphere.uploads import finish_thread_call, receive_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/documents", tags=["Document files"])
transaction = contextmanager(get_session)


@asynccontextmanager
async def upload_activity(storage):
    """Yield while briefly waiting for another process's initialization fence."""
    with ExitStack() as stack:
        deadline = time.monotonic() + 2
        while True:
            try:
                stack.enter_context(storage.activity())
            except StorageBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(remaining, 0.01))
            else:
                break
        # Only acquisition is retried, never multipart reads, publication or errors
        # raised by the protected body. Enter and exit stay on the same thread.
        yield


def authorize_upload(request, workspace_id, nonce):
    with transaction(request) as db:
        user = current_user(request, db, request.headers.get("x-csrf-token"))
        require_editor(workspace_access(workspace_id, user, db))
        require_storage_schema(db)
        document_id = upload_document_id(user.id, workspace_id, nonce)
        if db.get(DocumentJob, cleanup_job_id(document_id)) is not None:
            raise HTTPException(409, "This upload was deleted. Use a new idempotency key.")
        replay_filename = db.scalar(
            select(DocumentFile.original_filename)
            .join(Document, Document.id == DocumentFile.document_id)
            .where(
                Document.id == document_id,
                Document.workspace_id == workspace_id,
                DocumentFile.uploaded_by == user.id,
            )
        )
        return user.id, document_id, replay_filename


def get_storage(request):
    with request.app.state.private_storage_lock:
        if request.app.state.private_storage is None:
            store = PrivateStorage(request.app.state.settings.private_storage_root)
            store.initialize()
            request.app.state.private_storage = store
        return request.app.state.private_storage


def finalize_upload(request, workspace_id, user_id, document_id, stored, metadata, file_id):
    with transaction(request) as db:
        # Same cookie, CSRF, user and current membership must still be valid.
        user = current_user(request, db, request.headers.get("x-csrf-token"))
        access = require_editor(workspace_access(workspace_id, user, db))
        if user.id != user_id:
            raise HTTPException(401, "Authentication changed during upload.")
        require_storage_schema(db)
        if db.get(DocumentJob, cleanup_job_id(document_id)) is not None:
            raise HTTPException(409, "This upload was deleted. Use a new idempotency key.")
        existing = db.get(Document, document_id)
        if existing is not None:
            attachment = db.scalar(
                select(DocumentFile).where(DocumentFile.document_id == document_id)
            )
            if (
                existing.workspace_id != access.workspace.id
                or attachment is None
                or attachment.uploaded_by != user.id
                or attachment.original_filename != metadata.filename
                or attachment.sha256 != stored.sha256
                or existing.size_bytes != stored.size_bytes
                or existing.content_type != metadata.content_type
            ):
                raise HTTPException(409, "Idempotency key was already used for different content.")
            return UploadRead(
                document=DocumentRead.model_validate(existing),
                file=read_attachment(db, attachment),
                replayed=True,
            )
        # A compatibility allowance can only replay an existing attachment,
        # never create a new record after an unexpected change in database state.
        try:
            metadata = DocumentMetadata.model_validate(metadata.model_dump())
        except ValidationError:
            raise HTTPException(422, "Invalid upload filename.") from None
        document = Document(id=document_id, workspace_id=workspace_id, **metadata.model_dump())
        db.add(document)
        db.flush()
        attachment = DocumentFile(
            id=file_id,
            document_id=document_id,
            storage_key=stored.key,
            original_filename=metadata.filename,
            sha256=stored.sha256,
            uploaded_by=user.id,
        )
        db.add(attachment)
        db.add(DocumentJob(document_id=document_id, storage_key=stored.key, kind=JobKind.EXTRACT))
        db.flush()
        db.refresh(document)
        db.refresh(attachment)
        return UploadRead(
            document=DocumentRead.model_validate(document), file=read_attachment(db, attachment)
        )


def compensate(request, storage, key, workspace_id):
    """Do not erase a committed original after an ambiguous commit failure.

    If DB state cannot be established, retain the private orphan for later
    reconciliation. Blindly deleting could corrupt a successful upload.
    """
    try:
        with transaction(request) as db:
            # A failed client COMMIT does not prove that the server rolled back.
            # Finalization holds this workspace lock until its outcome resolves.
            # Wait for that outcome, then use a fresh READ COMMITTED snapshot.
            db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            workspace = db.get(Workspace, workspace_id, with_for_update=True)
            if workspace is None:
                logger.warning("Upload compensation deferred (workspace unavailable)")
                return
            if db.scalar(select(DocumentFile.id).where(DocumentFile.storage_key == key)):
                return
            try:
                storage.delete(key)
            except StorageError:
                active = db.scalar(
                    select(DocumentJob.id).where(
                        DocumentJob.storage_key == key,
                        DocumentJob.kind == JobKind.DELETE_FILES,
                        DocumentJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
                    )
                )
                if active is None:
                    db.add(DocumentJob(storage_key=key, kind=JobKind.DELETE_FILES))
    except (HTTPException, SQLAlchemyError, StorageError) as error:
        logger.warning("Upload compensation deferred (%s)", type(error).__name__)


def publish_and_finalize(
    request, workspace_id, user_id, document_id, storage, key, incoming, file_id
):
    """Keep publication, DB commit and compensation in one joined worker call."""
    stored = None
    result = None
    try:
        stored = storage.put(
            key, incoming.file, max_bytes=request.app.state.settings.max_upload_bytes
        )
        result = finalize_upload(
            request, workspace_id, user_id, document_id, stored, incoming.metadata, file_id
        )
        return result
    finally:
        if stored is not None and (result is None or result.replayed):
            compensate(request, storage, key, workspace_id)


@router.post(
    "/uploads",
    response_model=UploadRead,
    status_code=202,
    openapi_extra={
        "parameters": [
            {
                "name": "X-CSRF-Token",
                "in": "header",
                "description": "Current authenticated CSRF token (required for acceptance).",
                "schema": {"type": "string"},
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                        "additionalProperties": False,
                    }
                }
            },
        },
    },
)
async def upload_document(
    workspace_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    user_id, document_id, replay_filename = await run_in_threadpool(
        authorize_upload, request, workspace_id, idempotency_key
    )
    slots = request.app.state.upload_slots
    try:
        async with asyncio.timeout(0.05):
            await slots.acquire()
    except TimeoutError:
        raise HTTPException(
            503, "Upload capacity is busy. Try again later.", headers={"Retry-After": "1"}
        ) from None
    try:
        storage = await run_in_threadpool(get_storage, request)
        file_id = uuid4()
        key = new_storage_key(workspace_id, file_id)
        settings = request.app.state.settings
        # Reconciliation takes an exclusive fence. Keep this shared fence until
        # all private writes, final commits and compensation have finished.
        async with upload_activity(storage):
            async with receive_upload(
                request,
                storage,
                max_bytes=settings.max_upload_bytes,
                timeout_seconds=settings.upload_timeout_seconds,
                replay_filename=replay_filename,
            ) as incoming:
                result = await finish_thread_call(
                    publish_and_finalize,
                    request,
                    workspace_id,
                    user_id,
                    document_id,
                    storage,
                    key,
                    incoming,
                    file_id,
                )
        response.headers["Location"] = str(
            request.url_for(
                "get_document", workspace_id=str(workspace_id), document_id=str(document_id)
            )
        )
        return result
    except StorageError:
        raise HTTPException(
            503, "Private file storage is unavailable. Please try again later."
        ) from None
    finally:
        slots.release()


@router.get("/{document_id}/file", response_model=FileRead)
def file_metadata(document_id: UUID, session: DatabaseSession, access: Access):
    document = find_document(session, document_id, access.workspace.id)
    require_storage_schema(session)
    attachment = session.scalar(select(DocumentFile).where(DocumentFile.document_id == document.id))
    if attachment is None:
        raise HTTPException(404, "Document has no attached file.")
    return read_attachment(session, attachment)


@router.get("/{document_id}/sections", response_model=SectionPage)
def extracted_sections(
    document_id: UUID,
    session: DatabaseSession,
    access: Access,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    document = find_document(session, document_id, access.workspace.id)
    require_storage_schema(session)
    attachment = session.scalar(select(DocumentFile).where(DocumentFile.document_id == document.id))
    if attachment is None:
        raise HTTPException(404, "Document has no attached file.")
    if not download_allowed(session, attachment):
        raise HTTPException(409, "Sections are unavailable until processing succeeds.")
    rows = session.scalars(
        select(DocumentSection)
        .where(DocumentSection.document_id == document.id)
        .order_by(DocumentSection.ordinal)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return SectionPage(
        document_id=document.id,
        items=[SectionRead.model_validate(row) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


class ValidatedDownload(Response):
    """Open after authorization commits; verify bounded bytes and always close."""

    def __init__(self, storage, key, size, digest, filename, content_type):
        super().__init__()
        self.storage, self.key, self.size, self.digest = storage, key, size, digest
        self.filename, self.content_type = filename, content_type

    def verified_content(self):
        # The configured upload bound caps memory. No open descriptor crosses a
        # thread/async boundary, including cancellation while verification runs.
        with self.storage.open(self.key) as handle:
            size, digest = 0, hashlib.sha256()
            pieces = []
            while chunk := handle.read(64 * 1024):
                size += len(chunk)
                if size > self.size:
                    raise StorageError("Stored object integrity check failed.")
                digest.update(chunk)
                pieces.append(chunk)
            if size != self.size or digest.hexdigest() != self.digest:
                raise StorageError("Stored object integrity check failed.")
            return b"".join(pieces)

    async def __call__(self, scope, receive, send):
        try:
            content = await run_in_threadpool(self.verified_content)
        except (OSError, StorageError):
            return await JSONResponse(
                {"detail": "Private file is unavailable."},
                status_code=503,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )(scope, receive, send)

        await Response(
            content,
            media_type=self.content_type,
            headers={
                "Content-Length": str(self.size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(self.filename, safe=""),
            },
        )(scope, receive, send)


@router.get("/{document_id}/download", response_class=Response)
def download_document(
    document_id: UUID, request: Request, session: DatabaseSession, access: Access
):
    document = find_document(session, document_id, access.workspace.id)
    require_storage_schema(session)
    attachment = session.scalar(select(DocumentFile).where(DocumentFile.document_id == document.id))
    if attachment is None:
        raise HTTPException(404, "Document has no attached file.")
    if not download_allowed(session, attachment):
        raise HTTPException(409, "File is not available until validation and processing succeed.")
    try:
        storage = get_storage(request)
    except StorageError:
        raise HTTPException(503, "Private file storage is unavailable.") from None
    return ValidatedDownload(
        storage,
        attachment.storage_key,
        document.size_bytes,
        attachment.sha256,
        document.filename,
        document.content_type,
    )
