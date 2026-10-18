"""Synchronous routes run in FastAPI's thread pool, not the async event loop."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentsphere.config import Settings, get_settings
from agentsphere.document_lifecycle import queue_file_deletion, storage_schema_available
from agentsphere.models import Document, DocumentFile, DocumentStatus
from agentsphere.permissions import Access, Editor
from agentsphere.schemas.documents import (
    DocumentMetadata,
    DocumentPage,
    DocumentRead,
    DocumentUpdate,
)
from agentsphere.security import DatabaseSession, now

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/documents",
    tags=["Document storage"],
    responses={503: {"description": "Database not configured or unavailable."}},
)
Configuration = Annotated[Settings, Depends(get_settings)]


def check_size(document: DocumentMetadata, settings: Settings) -> None:
    if document.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Declared file size exceeds the {settings.max_upload_bytes}-byte limit.",
        )


def find_document(
    session: Session, document_id: UUID, workspace_id: UUID, *, lock: bool = False
) -> Document:
    query = select(Document).where(
        Document.id == document_id, Document.workspace_id == workspace_id
    )
    document = session.scalar(query.with_for_update() if lock else query)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return document


@router.post(
    "",
    response_model=DocumentRead,
    status_code=201,
    responses={413: {"description": "Size limit exceeded."}},
)
def create_document(
    metadata: DocumentMetadata,
    access: Editor,
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: Configuration,
) -> DocumentRead:
    """Register metadata only. A new UUID is generated even for duplicate filenames."""
    check_size(metadata, settings)
    document = Document(workspace_id=access.workspace.id, **metadata.model_dump())
    session.add(document)
    session.flush()
    session.refresh(document)
    result = DocumentRead.model_validate(document)
    response.headers["Location"] = str(
        request.url_for(
            "get_document", workspace_id=str(access.workspace.id), document_id=str(document.id)
        )
    )
    return result


@router.get("", response_model=DocumentPage)
def list_documents(
    access: Access,
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[DocumentStatus | None, Query()] = None,
) -> DocumentPage:
    """Newest first, with UUID as a stable tie breaker; includes archived rows by default."""
    query = (
        select(Document)
        .where(Document.workspace_id == access.workspace.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    if status is not None:
        query = query.where(Document.status == status)
    rows = session.scalars(query.offset(offset).limit(limit + 1)).all()
    return DocumentPage(
        items=[DocumentRead.model_validate(row) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


@router.get(
    "/{document_id}", response_model=DocumentRead, responses={404: {"description": "Not found."}}
)
def get_document(document_id: UUID, session: DatabaseSession, access: Access) -> DocumentRead:
    return DocumentRead.model_validate(find_document(session, document_id, access.workspace.id))


@router.patch(
    "/{document_id}",
    response_model=DocumentRead,
    responses={404: {"description": "Not found."}, 413: {"description": "Size limit exceeded."}},
)
def update_document(
    document_id: UUID,
    patch: DocumentUpdate,
    access: Editor,
    session: DatabaseSession,
    settings: Configuration,
) -> DocumentRead:
    """Update selected fields, validating the complete resulting metadata together."""
    document = find_document(session, document_id, access.workspace.id, lock=True)
    changes = patch.model_dump(exclude_unset=True)
    if storage_schema_available(session):
        attached = session.scalar(
            select(DocumentFile.id).where(DocumentFile.document_id == document.id)
        )
        if attached is not None and any(
            field in changes and changes[field] != getattr(document, field)
            for field in ("size_bytes", "content_type")
        ):
            raise HTTPException(409, "Uploaded file size and content type are server controlled.")
    current = {
        "filename": document.filename,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
    }
    current.update({key: value for key, value in changes.items() if key != "status"})
    try:
        metadata = DocumentMetadata.model_validate(current)
    except ValidationError as error:
        detail = [
            {"loc": ["body", *item["loc"]], "msg": item["msg"], "type": item["type"]}
            for item in error.errors(include_context=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from None
    check_size(metadata, settings)
    for key, value in metadata.model_dump().items():
        setattr(document, key, value)
    if "status" in changes:
        document.status = changes["status"]
    session.flush()
    session.refresh(document)
    return DocumentRead.model_validate(document)


@router.delete("/{document_id}", status_code=204, responses={404: {"description": "Not found."}})
def delete_document(
    document_id: UUID, request: Request, session: DatabaseSession, access: Editor
) -> Response:
    """Remove visibility now; retain durable cleanup work for a later worker."""
    document = find_document(session, document_id, access.workspace.id, lock=True)
    queue_file_deletion(session, document, now(request))
    session.delete(document)
    return Response(status_code=204)
