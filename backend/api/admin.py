"""Administrator-only account management. Public registration is disabled."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session as DBSession, select

from backend.accounts import AccountConflict, change_user, create_user
from backend.auth import current_admin, public_user
from backend.storage.database import get_db
from backend.storage.models import User


router = APIRouter(prefix="/api/admin/users", tags=["admin"])


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatch(BaseModel):
    is_active: bool | None = None
    password: str | None = None


@router.get("")
def list_users(db: DBSession = Depends(get_db), _admin: User = Depends(current_admin)):
    return [public_user(row) for row in db.exec(select(User).order_by(User.username)).all()]


@router.post("", status_code=201)
def create_account(body: UserCreate, db: DBSession = Depends(get_db), _admin: User = Depends(current_admin)):
    try:
        return public_user(create_user(db, body.username, body.password, body.is_admin))
    except AccountConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/{user_id}")
def update_account(user_id: str, body: UserPatch, db: DBSession = Depends(get_db), admin: User = Depends(current_admin)):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    try:
        return public_user(change_user(db, target, admin, is_active=body.is_active, password=body.password))
    except AccountConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
