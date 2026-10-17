"""Authorization checks share the request transaction with protected operations."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException

from agentsphere.models import Membership, Workspace
from agentsphere.security import CurrentUser, DatabaseSession


@dataclass
class WorkspaceAccess:
    workspace: Workspace
    role: str


def workspace_access(workspace_id: UUID, user: CurrentUser, db: DatabaseSession) -> WorkspaceAccess:
    # Lock workspace before membership, also used by member management. This
    # serializes member removal with document writes for this small application.
    workspace = db.get(Workspace, workspace_id, with_for_update=True)
    if workspace is None:
        raise HTTPException(404, "Workspace not found.")
    if workspace.owner_user_id == user.id:
        return WorkspaceAccess(workspace, "owner")
    member = db.get(Membership, (workspace_id, user.id))
    if member is None:
        raise HTTPException(404, "Workspace not found.")
    return WorkspaceAccess(workspace, member.role)


Access = Annotated[WorkspaceAccess, Depends(workspace_access)]


def require_editor(access: Access) -> WorkspaceAccess:
    if access.role not in {"owner", "editor"}:
        raise HTTPException(403, "Editor permission required.")
    return access


def require_owner(access: Access) -> WorkspaceAccess:
    if access.role != "owner":
        raise HTTPException(403, "Owner permission required.")
    return access


Editor = Annotated[WorkspaceAccess, Depends(require_editor)]
Owner = Annotated[WorkspaceAccess, Depends(require_owner)]
