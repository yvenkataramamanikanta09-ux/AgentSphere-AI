from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from agentsphere.config import Settings, get_settings
from agentsphere.schemas.documents import DocumentMetadata, DocumentValidationResponse

router = APIRouter(prefix="/api/v1/documents", tags=["Documents"])


@router.post(
    "/validate",
    response_model=DocumentValidationResponse,
    responses={413: {"description": "Declared size exceeds the configured byte limit."}},
)
async def validate_document(
    document: DocumentMetadata,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentValidationResponse:
    """Validate metadata only. No file is uploaded, inspected, or saved."""
    # Schema rules are fixed; this policy limit comes from runtime configuration.
    if document.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Declared file size exceeds the {settings.max_upload_bytes}-byte limit.",
        )
    return DocumentValidationResponse(document=document, max_upload_bytes=settings.max_upload_bytes)
