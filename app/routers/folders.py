from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import WatchedFolder
from ..services.queue_manager import queue_manager
from ..services.scheduler import scan_scheduler
from ..services.watcher import folder_watcher

router = APIRouter(prefix="/api/folders", tags=["folders"])


class FolderIn(BaseModel):
    path: str
    enabled: bool = True
    password: Optional[str] = None
    post_action: str = "keep"


class FolderUpdate(BaseModel):
    enabled: Optional[bool] = None
    password: Optional[str] = None
    post_action: Optional[str] = None
    marked_extracted: Optional[bool] = None


class FolderOut(BaseModel):
    id: int
    path: str
    enabled: bool
    password: Optional[str]
    post_action: str
    marked_extracted: bool
    created_at: Optional[str]
    last_scanned: Optional[str]


def _out(w: WatchedFolder) -> FolderOut:
    return FolderOut(
        id=w.id,
        path=w.path,
        enabled=w.enabled,
        password=w.password,
        post_action=w.post_action,
        marked_extracted=w.marked_extracted,
        created_at=w.created_at.isoformat() if w.created_at else None,
        last_scanned=w.last_scanned.isoformat() if w.last_scanned else None,
    )


@router.get("", response_model=list[FolderOut])
def list_folders(db: Session = Depends(get_db)):
    return [_out(w) for w in db.query(WatchedFolder).order_by(WatchedFolder.created_at).all()]


@router.post("", response_model=FolderOut)
def add_folder(body: FolderIn, db: Session = Depends(get_db)):
    from pathlib import Path
    if not Path(body.path).is_dir():
        raise HTTPException(400, f"Path does not exist or is not a directory: {body.path}")
    existing = db.query(WatchedFolder).filter(WatchedFolder.path == body.path).first()
    if existing:
        raise HTTPException(409, "Folder already watched")
    wf = WatchedFolder(**body.model_dump())
    db.add(wf)
    db.commit()
    db.refresh(wf)
    folder_watcher.add_path(body.path)
    return _out(wf)


@router.patch("/{folder_id}", response_model=FolderOut)
def update_folder(folder_id: int, body: FolderUpdate, db: Session = Depends(get_db)):
    wf = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not wf:
        raise HTTPException(404, "Watch folder not found")
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(wf, field, val)
    db.commit()
    db.refresh(wf)
    return _out(wf)


@router.delete("/{folder_id}")
def remove_folder(folder_id: int, db: Session = Depends(get_db)):
    wf = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not wf:
        raise HTTPException(404, "Watch folder not found")
    path = wf.path
    db.delete(wf)
    db.commit()
    folder_watcher.remove_path(path)
    return {"ok": True}


@router.post("/{folder_id}/scan")
async def scan_folder(folder_id: int, db: Session = Depends(get_db)):
    wf = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not wf:
        raise HTTPException(404, "Watch folder not found")
    if wf.marked_extracted:
        return {"ok": True, "queued": 0, "skipped_reason": "marked_extracted"}
    ids = await queue_manager.enqueue_folder(wf.path, wf.post_action, wf.password, "manual")
    wf.last_scanned = datetime.utcnow()
    db.commit()
    return {"ok": True, "queued": len(ids), "job_ids": ids}


@router.post("/{folder_id}/mark-extracted")
def mark_extracted(folder_id: int, extracted: bool = True, db: Session = Depends(get_db)):
    wf = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not wf:
        raise HTTPException(404, "Watch folder not found")
    wf.marked_extracted = extracted
    db.commit()
    return {"ok": True, "marked_extracted": extracted}


@router.post("/scan-all")
async def scan_all():
    await scan_scheduler.run_now()
    return {"ok": True}
