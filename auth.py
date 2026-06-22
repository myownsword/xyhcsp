import hashlib
import secrets
from datetime import datetime, date
from functools import wraps
from typing import Optional

from fastapi import Depends, HTTPException, status, Request, Cookie
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from database import get_db
import models


SESSIONS = {}


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def create_session(user_id: int) -> str:
    session_id = secrets.token_hex(32)
    SESSIONS[session_id] = {
        "user_id": user_id,
        "created_at": datetime.now().isoformat()
    }
    return session_id


def get_session(session_id: Optional[str] = Cookie(default=None)):
    if not session_id or session_id not in SESSIONS:
        return None
    return SESSIONS[session_id]


def get_current_user(
    session=Depends(get_session),
    db: Session = Depends(get_db)
) -> models.User:
    if not session:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )
    user = db.query(models.User).filter(models.User.id == session["user_id"]).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )
    return user


def require_roles(*roles):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request: Request = kwargs.get("request")
            current_user: models.User = kwargs.get("current_user")
            if current_user and current_user.role not in roles:
                raise HTTPException(status_code=403, detail="无权限访问此页面")
            return await func(*args, **kwargs)
        return wrapper
    return decorator


def clear_session(session_id: str):
    if session_id in SESSIONS:
        del SESSIONS[session_id]
