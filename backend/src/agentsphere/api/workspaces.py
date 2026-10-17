from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import or_, select

from agentsphere.models import Membership, User, Workspace
from agentsphere.permissions import Access, Owner
from agentsphere.schemas.auth import (
    MembershipChange,
    MembershipCreate,
    MembershipRead,
    WorkspaceCreate,
    WorkspaceRead,
)
from agentsphere.security import CurrentUser, DatabaseSession

router = APIRouter(prefix="/api/v1/workspaces", tags=["Workspaces"])


def read_workspace(workspace, role):
    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        owner_user_id=workspace.owner_user_id,
        created_at=workspace.created_at,
        role=role,
    )


@router.post("", response_model=WorkspaceRead, status_code=201)
def create_workspace(data: WorkspaceCreate, user: CurrentUser, db: DatabaseSession):
    workspace = Workspace(name=data.name, owner_user_id=user.id)
    db.add(workspace)
    db.flush()
    return read_workspace(workspace, "owner")


@router.get("")
def list_workspaces(
    user: CurrentUser,
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = (
        select(Workspace, Membership.role)
        .outerjoin(
            Membership, (Membership.workspace_id == Workspace.id) & (Membership.user_id == user.id)
        )
        .where(or_(Workspace.owner_user_id == user.id, Membership.user_id == user.id))
        .order_by(Workspace.created_at.desc(), Workspace.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    rows = db.execute(query).all()
    return {
        "items": [
            read_workspace(w, "owner" if w.owner_user_id == user.id else role)
            for w, role in rows[:limit]
        ],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(access: Access):
    return read_workspace(access.workspace, access.role)


@router.get("/{workspace_id}/members")
def list_members(
    access: Owner,
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows = db.scalars(
        select(Membership)
        .where(Membership.workspace_id == access.workspace.id)
        .order_by(Membership.user_id)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {
        "owner_user_id": access.workspace.owner_user_id,
        "items": [MembershipRead.model_validate(row) for row in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
    }


@router.post("/{workspace_id}/members", response_model=MembershipRead, status_code=201)
def add_member(data: MembershipCreate, access: Owner, db: DatabaseSession):
    if data.user_id == access.workspace.owner_user_id:
        raise HTTPException(409, "Ownership cannot be changed through memberships.")
    # Do not lock another user's row while holding a workspace lock.
    user = db.get(User, data.user_id)
    if user is None or not user.is_active:
        raise HTTPException(404, "User not found.")
    if db.get(Membership, (access.workspace.id, data.user_id)):
        raise HTTPException(409, "Membership already exists.")
    row = Membership(workspace_id=access.workspace.id, **data.model_dump())
    db.add(row)
    db.flush()
    return MembershipRead.model_validate(row)


def member_or_404(db, workspace_id, user_id):
    row = db.get(Membership, (workspace_id, user_id))
    if row is None:
        raise HTTPException(404, "Membership not found.")
    return row


@router.patch("/{workspace_id}/members/{user_id}", response_model=MembershipRead)
def change_member(user_id: UUID, data: MembershipChange, access: Owner, db: DatabaseSession):
    row = member_or_404(db, access.workspace.id, user_id)
    row.role = data.role
    db.flush()
    return MembershipRead.model_validate(row)


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(user_id: UUID, access: Owner, db: DatabaseSession):
    db.delete(member_or_404(db, access.workspace.id, user_id))
    return Response(status_code=204)
