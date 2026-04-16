from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Job
from ..services.queue_manager import queue_manager

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class EnqueueRequest(BaseModel):
    path: str                           # folder OR specific rar file
    post_action: Optional[str] = None  # keep | delete | trash
    password: Optional[str] = None


class JobOut(BaseModel):
    id: int
    folder_path: str
    rar_file: str
    status: str
    progress: float
    eta_seconds: Optional[int]
    error_message: Optional[str]
    post_action: str
    source: str
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]


def _out(j: Job) -> JobOut:
    return JobOut(
        id=j.id,
        folder_path=j.folder_path,
        rar_file=j.rar_file,
        status=j.status,
        progress=j.progress,
        eta_seconds=j.eta_seconds,
        error_message=j.error_message,
        post_action=j.post_action,
        source=j.source,
        created_at=j.created_at.isoformat() if j.created_at else None,
        started_at=j.started_at.isoformat() if j.started_at else None,
        completed_at=j.completed_at.isoformat() if j.completed_at else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[JobOut])
def list_jobs(
    status: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    q = db.query(Job).order_by(Job.created_at.desc())
    if status:
        statuses = status.split(",")
        q = q.filter(Job.status.in_(statuses))
    return [_out(j) for j in q.limit(limit).all()]


@router.post("", response_model=list[int])
async def enqueue(req: EnqueueRequest):
    from pathlib import Path
    from ..services.extractor import is_first_rar_part

    p = Path(req.path)
    if not p.exists():
        raise HTTPException(404, f"Path not found: {req.path}")

    if p.is_file():
        if not is_first_rar_part(p):
            raise HTTPException(400, "File is not a first-part RAR")
        jid = await queue_manager.enqueue(str(p), req.post_action, req.password, "manual")
        return [jid] if jid else []
    else:
        ids = await queue_manager.enqueue_folder(str(p), req.post_action, req.password, "manual")
        return ids


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.query(Job).filter(Job.id == job_id).first()
    if not j:
        raise HTTPException(404, "Job not found")
    return _out(j)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    ok = await queue_manager.cancel(job_id)
    if not ok:
        raise HTTPException(400, "Job cannot be cancelled (wrong state)")
    return {"ok": True}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    ok = await queue_manager.retry(job_id)
    if not ok:
        raise HTTPException(400, "Job cannot be retried (wrong state)")
    return {"ok": True}


@router.delete("/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    j = db.query(Job).filter(Job.id == job_id).first()
    if not j:
        raise HTTPException(404, "Job not found")
    if j.status in ("pending", "running"):
        raise HTTPException(400, "Cannot delete an active job; cancel it first")
    db.delete(j)
    db.commit()
    return {"ok": True}


@router.delete("")
def clear_history(status: str = "completed,failed,cancelled,skipped", db: Session = Depends(get_db)):
    statuses = status.split(",")
    db.query(Job).filter(Job.status.in_(statuses)).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}
