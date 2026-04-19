"""
cleanup.py — API for the Clean Up extracted files feature.

Only touches files that UnrarTool itself extracted (tracked in jobs.files_extracted).
Never deletes RAR files, never deletes anything not in the tracking list.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import new_session
from ..models import Job
from ..ws_manager import ws_manager

router = APIRouter(prefix="/api/cleanup", tags=["cleanup"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CleanupFile(BaseModel):
    path:      str
    filename:  str
    folder:    str
    size:      int          # bytes
    job_id:    int
    exists:    bool         # False if already deleted


class DeleteRequest(BaseModel):
    paths: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[CleanupFile])
def list_cleanup_files():
    """
    Return all files that UnrarTool has extracted across all completed jobs,
    filtered to only those that still exist on disk.
    Only files stored in jobs.files_extracted are ever returned —
    we never guess or scan arbitrary folders.
    """
    db = new_session()
    try:
        jobs = (
            db.query(Job)
            .filter(Job.status == "completed", Job.files_extracted.isnot(None))
            .order_by(Job.completed_at.desc())
            .all()
        )

        result = []
        for job in jobs:
            try:
                files = json.loads(job.files_extracted or "[]")
            except (json.JSONDecodeError, TypeError):
                continue

            for path in files:
                p = Path(path)
                result.append(CleanupFile(
                    path=path,
                    filename=p.name,
                    folder=str(p.parent),
                    size=_file_size(path),
                    job_id=job.id,
                    exists=p.exists(),
                ))

        # Only return files that still exist
        return [f for f in result if f.exists]
    finally:
        db.close()


@router.post("/delete")
async def delete_files(req: DeleteRequest):
    """
    Delete the specified file paths.
    Only paths that appear in jobs.files_extracted are allowed —
    any path not in our tracking list is rejected with 403.
    """
    if not req.paths:
        return {"deleted": [], "failed": []}

    # Build the full set of tracked paths for validation
    db = new_session()
    try:
        jobs = (
            db.query(Job)
            .filter(Job.status == "completed", Job.files_extracted.isnot(None))
            .all()
        )
        tracked: set[str] = set()
        for job in jobs:
            try:
                tracked.update(json.loads(job.files_extracted or "[]"))
            except (json.JSONDecodeError, TypeError):
                pass
    finally:
        db.close()

    deleted, failed, rejected = [], [], []

    for path in req.paths:
        # Safety check — only delete tracked files
        if path not in tracked:
            rejected.append(path)
            continue

        try:
            if os.path.exists(path):
                os.remove(path)
                deleted.append(path)
            else:
                deleted.append(path)   # already gone — count as success
        except OSError as e:
            failed.append({"path": path, "error": str(e)})

    # Broadcast so UI can update live
    await ws_manager.broadcast({
        "type": "cleanup_progress",
        "deleted": deleted,
        "failed": [f["path"] for f in failed],
    })

    return {
        "deleted":  deleted,
        "failed":   failed,
        "rejected": rejected,
    }
