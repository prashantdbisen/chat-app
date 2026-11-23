"""
routers/users.py - Find people to message.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connections import manager
from app.database import get_db
from app.models import User
from app.security import get_current_user

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/search")
def search_users(
    q: str = Query("", max_length=20),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = select(User).where(User.id != user.id)
    if q.strip():
        query = query.where(User.username.contains(q.strip().lower()))
    people = db.scalars(query.order_by(User.username).limit(20))
    return [{"id": p.id, "username": p.display_name, "online": manager.is_online(p.id)} for p in people]
