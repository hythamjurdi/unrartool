from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import LogEntry

router = APIRouter(prefix="/api/logs", tags=["logs"])


class LogOut(BaseModel):
    id: int
    level: str
    message: str
    job_id: Optional[int]
    timestamp: str


@router.get("", response_model=list[LogOut])
def get_logs(
    level: Optional[str] = None,
    job_id: Optional[int] = None,
    limit: int = Query(default=500, le=2000),
    db: Session = Depends(get_db),
):
    q = db.query(LogEntry).order_by(LogEntry.timestamp.desc())
    if level:
        q = q.filter(LogEntry.level == level.upper())
    if job_id is not None:
        q = q.filter(LogEntry.job_id == job_id)
    rows = q.limit(limit).all()
    return [
        LogOut(
            id=r.id,
            level=r.level,
            message=r.message,
            job_id=r.job_id,
            timestamp=r.timestamp.isoformat(),
        )
        for r in rows
    ]


@router.delete("")
def clear_logs(db: Session = Depends(get_db)):
    db.query(LogEntry).delete()
    db.commit()
    return {"ok": True}
