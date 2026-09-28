"""Account mutations shared by the local bootstrap CLI and admin API."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlmodel import Session as DBSession, select

from backend.auth import hash_password, normalize_username, revoke_user_logins
from backend.storage.models import User


class AccountConflict(ValueError):
    pass


def create_user(db: DBSession, username: str, password: str, is_admin: bool = False) -> User:
    canonical = normalize_username(username)
    if db.exec(select(User.id).where(User.username == canonical)).first():
        raise AccountConflict("Username already exists")
    first_admin = is_admin and db.exec(select(User.id).where(User.is_admin == True)).first() is None  # noqa: E712
    user = User(username=canonical, password_hash=hash_password(password), is_admin=is_admin)
    try:
        db.add(user)
        db.flush()
        if first_admin:
            # Existing recordings, transcripts and glossaries hang off Session.
            # Assigning only the parent preserves every related row in place.
            db.execute(text("UPDATE session SET user_id = :owner WHERE user_id IS NULL"), {"owner": user.id})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccountConflict("Username already exists") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return user


def change_user(
    db: DBSession, target: User, actor: User | None = None,
    *, is_active: bool | None = None, password: str | None = None,
) -> User:
    if is_active is False and target.is_active and target.is_admin:
        if actor and target.id == actor.id:
            raise AccountConflict("You cannot disable your own administrator account")
        other_admin = db.exec(select(User.id).where(
            User.id != target.id, User.is_admin == True, User.is_active == True,  # noqa: E712
        )).first()
        if other_admin is None:
            raise AccountConflict("Cannot disable the last active administrator")
    if is_active is not None:
        target.is_active = is_active
    if password is not None:
        target.password_hash = hash_password(password)
    if is_active is False or password is not None:
        revoke_user_logins(db, target.id)
    db.add(target)
    db.commit()
    db.refresh(target)
    return target
