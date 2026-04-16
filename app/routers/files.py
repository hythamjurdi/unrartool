"""
files.py – filesystem browser API.
Returns directory listings with RAR set info overlaid.
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..config import config
from ..database import new_session
from ..models import Job, WatchedFolder
from ..services.extractor import find_rar_sets

router = APIRouter(prefix="/api/files", tags=["files"])


class BrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float
    rar_count: int = 0          # number of RAR sets (for dirs)
    rar_status: str = "none"    # none | has_rars | completed | in_progress | failed
    is_watched: bool = False
    marked_extracted: bool = False


@router.get("/browse", response_model=list[BrowseEntry])
def browse(path: str = Query(default=None)):
    root = path or config.DATA_PATH

    try:
        entries_raw = list(os.scandir(root))
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {root}")
    except FileNotFoundError:
        raise HTTPException(404, f"Path not found: {root}")

    db = new_session()
    try:
        watch_paths = {w.path for w in db.query(WatchedFolder).all()}
        watch_map = {w.path: w for w in db.query(WatchedFolder).all()}
    finally:
        db.close()

    result = []
    for e in sorted(entries_raw, key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            stat = e.stat(follow_symlinks=False)
        except OSError:
            continue

        entry = BrowseEntry(
            name=e.name,
            path=e.path,
            is_dir=e.is_dir(follow_symlinks=False),
            size=stat.st_size,
            modified=stat.st_mtime,
            is_watched=e.path in watch_paths,
            marked_extracted=watch_map.get(e.path, WatchedFolder()).marked_extracted or False,
        )

        if entry.is_dir:
            # Lightweight RAR detection without full recursive scan
            rar_sets = _quick_rar_count(e.path)
            entry.rar_count = rar_sets
            if rar_sets > 0:
                entry.rar_status = _dir_rar_status(e.path, db)
        else:
            if e.name.lower().endswith(".rar"):
                entry.rar_status = "has_rars"

        result.append(entry)

    return result


def _quick_rar_count(path: str) -> int:
    try:
        rars = find_rar_sets(path)
        return len(rars)
    except Exception:
        return 0


def _dir_rar_status(path: str, db) -> str:
    rar_sets = find_rar_sets(path)
    if not rar_sets:
        return "none"

    statuses = set()
    for rar in rar_sets:
        job = (
            db.query(Job)
            .filter(Job.rar_file == rar)
            .order_by(Job.created_at.desc())
            .first()
        )
        if job:
            statuses.add(job.status)
        else:
            statuses.add("pending")

    if "running" in statuses:
        return "in_progress"
    if "failed" in statuses:
        return "failed"
    if all(s == "completed" for s in statuses):
        return "completed"
    return "has_rars"
