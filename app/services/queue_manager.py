"""
queue_manager.py – Manages the extraction job queue.
Supports concurrency limiting, cancellation, retry, and WS progress broadcast.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..database import new_session
from ..models import Job, AppSetting, LogEntry
from ..ws_manager import ws_manager
from .extractor import (
    check_parts_complete,
    delete_rar_parts,
    extract,
    find_rar_sets,
    get_declared_size,
    trash_rar_parts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "folder_path": job.folder_path,
        "rar_file": job.rar_file,
        "status": job.status,
        "progress": job.progress,
        "eta_seconds": job.eta_seconds,
        "error_message": job.error_message,
        "post_action": job.post_action,
        "source": job.source,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _log(message: str, level: str = "INFO", job_id: Optional[int] = None):
    db = new_session()
    try:
        db.add(LogEntry(level=level, message=message, job_id=job_id))
        db.commit()
    finally:
        db.close()
    print(f"[{level}] {message}")


def _get_setting(db, key: str, default: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value is not None else default


# ---------------------------------------------------------------------------
# Queue Manager
# ---------------------------------------------------------------------------

class QueueManager:
    def __init__(self):
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._active_procs: dict[int, asyncio.Task] = {}   # job_id -> task

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazily create semaphore once event loop is running."""
        if self._semaphore is None:
            db = new_session()
            try:
                limit = int(_get_setting(db, "max_concurrent_jobs", "1"))
            finally:
                db.close()
            self._semaphore = asyncio.Semaphore(limit)
        return self._semaphore

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        rar_file: str,
        post_action: Optional[str] = None,
        password: Optional[str] = None,
        source: str = "manual",
    ) -> Optional[int]:
        """
        Add one RAR set to the queue.
        Returns the new Job.id, or None if a duplicate is already pending/running.
        """
        folder = str(Path(rar_file).parent)

        db = new_session()
        try:
            # Deduplicate
            existing = (
                db.query(Job)
                .filter(Job.rar_file == rar_file, Job.status.in_(["pending", "running"]))
                .first()
            )
            if existing:
                return None

            if post_action is None:
                post_action = _get_setting(db, "default_post_action", "keep")

            job = Job(
                folder_path=folder,
                rar_file=rar_file,
                post_action=post_action,
                password=password,
                source=source,
                status="pending",
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            jd = _job_dict(job)
        finally:
            db.close()

        await ws_manager.broadcast({"type": "new_job", "job": jd})
        _log(f"Job #{job_id} queued: {rar_file}", job_id=job_id)

        task = asyncio.create_task(self._process(job_id))
        self._active_procs[job_id] = task
        task.add_done_callback(lambda t: self._active_procs.pop(job_id, None))

        return job_id

    async def enqueue_folder(
        self,
        folder_path: str,
        post_action: Optional[str] = None,
        password: Optional[str] = None,
        source: str = "manual",
    ) -> list[int]:
        """Scan a folder and enqueue all RAR sets found."""
        rar_sets = await asyncio.to_thread(find_rar_sets, folder_path)
        ids = []
        for rar in rar_sets:
            jid = await self.enqueue(rar, post_action, password, source)
            if jid:
                ids.append(jid)
        return ids

    async def cancel(self, job_id: int) -> bool:
        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job or job.status not in ("pending", "running"):
                return False
            job.status = "cancelled"
            job.completed_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()

        # Cancel asyncio task
        task = self._active_procs.get(job_id)
        if task:
            task.cancel()

        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "cancelled"})
        _log(f"Job #{job_id} cancelled", job_id=job_id)
        return True

    async def retry(self, job_id: int) -> bool:
        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job or job.status not in ("failed", "cancelled", "skipped"):
                return False
            job.status = "pending"
            job.progress = 0.0
            job.eta_seconds = None
            job.error_message = None
            job.started_at = None
            job.completed_at = None
            db.commit()
            job_id = job.id
        finally:
            db.close()

        task = asyncio.create_task(self._process(job_id))
        self._active_procs[job_id] = task
        task.add_done_callback(lambda t: self._active_procs.pop(job_id, None))
        _log(f"Job #{job_id} retried", job_id=job_id)
        return True

    # ------------------------------------------------------------------
    # Internal processing
    # ------------------------------------------------------------------

    async def _process(self, job_id: int):
        async with self._get_semaphore():
            await self._run(job_id)

    async def _run(self, job_id: int):
        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job or job.status == "cancelled":
                return
            job.status = "running"
            job.started_at = datetime.utcnow()
            db.commit()
            rar_file = job.rar_file
            dest_path = str(Path(rar_file).parent)
            password = job.password
            post_action = job.post_action
        finally:
            db.close()

        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "running", "progress": 0})
        _log(f"Job #{job_id} started: {rar_file}", job_id=job_id)

        # Pre-flight: check all parts present
        ok, err = await check_parts_complete(rar_file)
        if not ok:
            await self._fail(job_id, f"Incomplete archive — {err}")
            return

        # Get declared size for progress estimation
        total_size = await get_declared_size(rar_file)

        # Extract
        try:
            async for pct, eta, status_line in extract(rar_file, dest_path, password, total_size):
                # Check for cancellation
                db2 = new_session()
                try:
                    j = db2.query(Job).filter(Job.id == job_id).first()
                    if j and j.status == "cancelled":
                        return
                    if j:
                        j.progress = pct if pct >= 0 else j.progress
                        j.eta_seconds = eta
                        db2.commit()
                finally:
                    db2.close()

                if pct == -1.0:
                    await self._fail(job_id, status_line)
                    return

                await ws_manager.broadcast({
                    "type": "job_progress",
                    "job_id": job_id,
                    "progress": pct,
                    "eta": eta,
                })

        except asyncio.CancelledError:
            return
        except Exception as e:
            await self._fail(job_id, str(e))
            return

        # Post-action
        await self._post_action(job_id, rar_file, post_action)

    async def _fail(self, job_id: int, error: str):
        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = "failed"
                job.error_message = error
                job.completed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "failed", "error": error})
        _log(f"Job #{job_id} failed: {error}", level="ERROR", job_id=job_id)

    async def _post_action(self, job_id: int, rar_file: str, action: str):
        db = new_session()
        try:
            trash = _get_setting(db, "trash_folder", "/config/trash")
        finally:
            db.close()

        if action == "delete":
            await asyncio.to_thread(delete_rar_parts, rar_file)
            _log(f"Job #{job_id}: RAR parts deleted", job_id=job_id)
        elif action == "trash":
            await asyncio.to_thread(trash_rar_parts, rar_file, trash)
            _log(f"Job #{job_id}: RAR parts moved to trash ({trash})", job_id=job_id)

        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = "completed"
                job.progress = 100.0
                job.eta_seconds = 0
                job.completed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "completed", "progress": 100})
        _log(f"Job #{job_id} completed successfully", job_id=job_id)


queue_manager = QueueManager()
