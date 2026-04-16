"""
exclusions.py – API for viewing, adding, and removing path exclusions.

Exclusions prevent the scheduler and filesystem watcher from re-queueing
a RAR set that's already been extracted (or that the user wants to skip).
They are set:
  • Automatically after every successful extraction (reason: auto_extracted)
  • Manually by the user via this API (reason: manual)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.queue_manager import queue_manager

router = APIRouter(prefix="/api/exclusions", tags=["exclusions"])


class ExclusionIn(BaseModel):
    path: str


class ExclusionOut(BaseModel):
    id: int
    path: str
    reason: str
    created_at: str | None


@router.get("", response_model=list[ExclusionOut])
def list_exclusions():
    return queue_manager.get_exclusions()


@router.post("", response_model=dict)
async def add_exclusion(body: ExclusionIn):
    """Manually mark a folder or RAR path as done — it will be skipped by automation."""
    await queue_manager.mark_done(body.path)
    return {"ok": True, "path": body.path, "reason": "manual"}


@router.delete("/by-path")
async def remove_exclusion_by_path(path: str):
    """Remove an exclusion so the path will be processed again."""
    await queue_manager.unmark_done(path)
    return {"ok": True}


@router.delete("/{exclusion_id}")
async def remove_exclusion(exclusion_id: int):
    """Remove exclusion by ID."""
    from ..database import new_session
    from ..models import Exclusion

    db = new_session()
    try:
        row = db.query(Exclusion).filter(Exclusion.id == exclusion_id).first()
        if not row:
            raise HTTPException(404, "Exclusion not found")
        path = row.path
        db.delete(row)
        db.commit()
    finally:
        db.close()

    await queue_manager.unmark_done.__func__(queue_manager, path)  # fire WS event
    return {"ok": True}
