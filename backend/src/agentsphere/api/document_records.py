"""Synchronous routes run in FastAPI's thread pool, not the async event loop."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentsphere.config import Settings, get_settings
from agentsphere.database import get_session
from agentsphere.models import Document, DocumentStatus
from agentsphere.schemas.documents import (
    DocumentMetadata,
    DocumentPage,
    DocumentRead,
    DocumentUpdate,
)

router = APIRouter(
    prefix="/api/v1/documents",
    tags=["Document storage"],
    responses={503: {"description": "Database not configured or unavailable."}},
)
DatabaseSession = Annotated[Session, Depends(get_session)]
Configuration = Annotated[Settings, Depends(get_settings)]


def check_size(document: DocumentMetadata, settings: Settings) -> None:
    if document.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Declared file size exceeds the {settings.max_upload_bytes}-byte limit.",
        )


def find_document(session: Session, document_id: UUID, *, lock: bool = False) -> Document:
    document = session.get(Document, document_id, with_for_update=lock)
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
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: Configuration,
) -> DocumentRead:
    """Register metadata only. A new UUID is generated even for duplicate filenames."""
    check_size(metadata, settings)
    with session.begin():
        document = Document(**metadata.model_dump())
        session.add(document)
        session.flush()
        session.refresh(document)
        result = DocumentRead.model_validate(document)
    response.headers["Location"] = str(
        request.url_for("get_document", document_id=str(document.id))
    )
    return result


@router.get("", response_model=DocumentPage)
def list_documents(
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[DocumentStatus | None, Query()] = None,
) -> DocumentPage:
    """Newest first, with UUID as a stable tie breaker; includes archived rows by default."""
    query = select(Document).order_by(Document.created_at.desc(), Document.id.desc())
    if status is not None:
        query = query.where(Document.status == status)
    with session.begin():
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
def get_document(document_id: UUID, session: DatabaseSession) -> DocumentRead:
    with session.begin():
        return DocumentRead.model_validate(find_document(session, document_id))


@router.patch(
    "/{document_id}",
    response_model=DocumentRead,
    responses={404: {"description": "Not found."}, 413: {"description": "Size limit exceeded."}},
)
def update_document(
    document_id: UUID,
    patch: DocumentUpdate,
    session: DatabaseSession,
    settings: Configuration,
) -> DocumentRead:
    """Update selected fields, validating the complete resulting metadata together."""
    with session.begin():
        document = find_document(session, document_id, lock=True)
        changes = patch.model_dump(exclude_unset=True)
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
def delete_document(document_id: UUID, session: DatabaseSession) -> Response:
    """Permanently delete this metadata row; no document file exists in Module 2."""
    with session.begin():
        session.delete(find_document(session, document_id, lock=True))
    return Response(status_code=204)
