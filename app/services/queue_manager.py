"""
queue_manager.py – Manages the extraction job queue.
Supports concurrency limiting, cancellation, retry, WS progress broadcast,
and a full exclusion system (manual + automatic after extraction).

Auto-defer: when unrar reports the file is not yet a valid archive
(still being written by the downloader), the job is automatically
retried after DEFER_RETRY_SECONDS rather than marked as failed.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..database import new_session
from ..models import Job, AppSetting, LogEntry, Exclusion
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
# Constants
# ---------------------------------------------------------------------------

# Phrases in unrar output that mean "file exists but isn't ready yet"
# (still being written by the downloader, not a real corruption/error)
_STILL_DOWNLOADING_PHRASES = (
    "is not rar archive",
    "no files to extract",
    "bad archive",
    "unexpected end of archive",
)

# How long to wait before retrying a deferred job
DEFER_RETRY_SECONDS = 300   # 5 minutes

# Maximum number of auto-defers before giving up with a real failure
MAX_DEFER_RETRIES = 6       # 6 × 5 min = 30 min max wait


# ---------------------------------------------------------------------------
# File tracking helpers
# ---------------------------------------------------------------------------

def _snapshot_folder(folder: str) -> set[str]:
    """Return a set of all file paths currently in folder (recursive)."""
    result = set()
    try:
        for root, _, files in os.walk(folder):
            for f in files:
                result.add(os.path.join(root, f))
    except OSError:
        pass
    return result


def _new_files(before: set[str], after: set[str], rar_parts: set[str]) -> list[str]:
    """
    Return files that appeared after extraction, excluding RAR parts themselves.
    These are the files UnrarTool wrote — the ones we want to track for clean-up.
    """
    return sorted(p for p in (after - before) if p not in rar_parts)


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


def _is_excluded(db, rar_file: str) -> bool:
    """
    True if this RAR path OR any of its ancestor folders is in the exclusions table.
    This means marking a parent folder as done will protect all children.
    """
    paths_to_check = [rar_file]
    p = Path(rar_file).parent
    while True:
        paths_to_check.append(str(p))
        if p == p.parent:
            break
        p = p.parent

    return (
        db.query(Exclusion)
        .filter(Exclusion.path.in_(paths_to_check))
        .first()
    ) is not None


def _add_exclusion(db, path: str, reason: str = "auto_extracted"):
    """Insert exclusion if it doesn't exist yet."""
    if not db.query(Exclusion).filter(Exclusion.path == path).first():
        db.add(Exclusion(path=path, reason=reason))
        db.commit()


# ---------------------------------------------------------------------------
# Queue Manager
# ---------------------------------------------------------------------------

class QueueManager:
    def __init__(self):
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._active_procs: dict[int, asyncio.Task] = {}

    def _get_semaphore(self) -> asyncio.Semaphore:
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
        force: bool = False,
    ) -> Optional[int]:
        """
        Queue one RAR set.
        - Returns Job.id on success.
        - Returns None if skipped (already excluded, already active, or already completed).
        - force=True bypasses exclusion (lets user explicitly re-extract).
        """
        folder = str(Path(rar_file).parent)

        db = new_session()
        try:
            # 1. Exclusion guard (auto/manual) — skipped when forced
            if not force and _is_excluded(db, rar_file):
                _log(f"Skipped (excluded): {rar_file}")
                return None

            # 2. Deduplicate active jobs
            active = (
                db.query(Job)
                .filter(Job.rar_file == rar_file, Job.status.in_(["pending", "running"]))
                .first()
            )
            if active:
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
        force: bool = False,
    ) -> dict:
        """Scan folder recursively and queue all non-excluded RAR sets."""
        rar_sets = await asyncio.to_thread(find_rar_sets, folder_path)
        queued_ids, skipped = [], []

        for rar in rar_sets:
            jid = await self.enqueue(rar, post_action, password, source, force=force)
            if jid:
                queued_ids.append(jid)
            else:
                skipped.append(rar)

        return {"queued": queued_ids, "skipped": skipped}

    # ------------------------------------------------------------------
    # Exclusion management
    # ------------------------------------------------------------------

    async def mark_done(self, path: str) -> bool:
        """
        Manually exclude a path (folder or specific RAR).
        The watcher and scheduler will permanently skip it.
        Can be undone with unmark_done().
        """
        db = new_session()
        try:
            _add_exclusion(db, path, reason="manual")
        finally:
            db.close()
        await ws_manager.broadcast({"type": "exclusion_added", "path": path, "reason": "manual"})
        _log(f"Marked as done (excluded): {path}")
        return True

    async def unmark_done(self, path: str) -> bool:
        """Remove an exclusion so the path will be processed again."""
        db = new_session()
        try:
            row = db.query(Exclusion).filter(Exclusion.path == path).first()
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()
        await ws_manager.broadcast({"type": "exclusion_removed", "path": path})
        _log(f"Exclusion cleared: {path}")
        return True

    def get_exclusions(self) -> list[dict]:
        db = new_session()
        try:
            rows = db.query(Exclusion).order_by(Exclusion.created_at.desc()).all()
            return [
                {
                    "id": r.id,
                    "path": r.path,
                    "reason": r.reason,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Job control
    # ------------------------------------------------------------------

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

        task = self._active_procs.get(job_id)
        if task:
            task.cancel()

        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "cancelled"})
        _log(f"Job #{job_id} cancelled", job_id=job_id)
        return True

    async def retry(self, job_id: int, force: bool = False) -> bool:
        """
        Re-queue a failed/cancelled job.
        force=True also clears the exclusion for that RAR so it can run.
        """
        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job or job.status not in ("failed", "cancelled", "skipped"):
                return False
            if force:
                excl = db.query(Exclusion).filter(Exclusion.path == job.rar_file).first()
                if excl:
                    db.delete(excl)
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
        _log(f"Job #{job_id} retried{' (forced)' if force else ''}", job_id=job_id)
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
            rar_file    = job.rar_file
            dest_path   = str(Path(rar_file).parent)
            password    = job.password
            post_action = job.post_action
            retry_count = job.retry_count or 0
        finally:
            db.close()

        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "running", "progress": 0})
        _log(f"Job #{job_id} started: {rar_file}", job_id=job_id)

        # Pre-flight: check all parts present
        ok, err = await check_parts_complete(rar_file)
        if not ok:
            # "Cannot find volume" means the downloader hasn't finished yet
            if "missing part" in err.lower() or "cannot find volume" in err.lower():
                await self._defer(job_id, err, retry_count)
            else:
                await self._fail(job_id, f"Incomplete archive — {err}")
            return

        # Snapshot folder BEFORE extraction so we know exactly what we wrote
        from .extractor import rar_part_paths as _rpp
        folder_snapshot = await asyncio.to_thread(_snapshot_folder, dest_path)
        rar_part_set    = set(await asyncio.to_thread(_rpp, rar_file))

        total_size = await get_declared_size(rar_file)

        try:
            async for pct, eta, status_line in extract(rar_file, dest_path, password, total_size):
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
                    # Check if this is a "still downloading" error vs a real failure
                    if any(p in status_line.lower() for p in _STILL_DOWNLOADING_PHRASES):
                        await self._defer(job_id, status_line, retry_count)
                    else:
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

        await self._complete(job_id, rar_file, post_action, folder_snapshot, rar_part_set)

    async def _defer(self, job_id: int, reason: str, retry_count: int):
        """
        The file exists but isn't ready yet (still being downloaded).
        Reset to pending and retry after DEFER_RETRY_SECONDS.
        After MAX_DEFER_RETRIES attempts, give up and mark as failed.
        """
        if retry_count >= MAX_DEFER_RETRIES:
            await self._fail(
                job_id,
                f"File not ready after {MAX_DEFER_RETRIES} retries ({DEFER_RETRY_SECONDS}s apart). "
                f"Last error: {reason}"
            )
            return

        new_count = retry_count + 1
        wait_min  = DEFER_RETRY_SECONDS // 60

        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status        = "pending"
                job.progress      = 0.0
                job.eta_seconds   = None
                job.error_message = None
                job.started_at    = None
                job.retry_count   = new_count
                db.commit()
        finally:
            db.close()

        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "pending"})
        _log(
            f"Job #{job_id} deferred (attempt {new_count}/{MAX_DEFER_RETRIES}) — "
            f"file not ready yet, retrying in {wait_min} min. Reason: {reason[:120]}",
            job_id=job_id,
        )

        # Sleep without holding the semaphore so other jobs can run
        await asyncio.sleep(DEFER_RETRY_SECONDS)

        # Re-queue only if not cancelled in the meantime
        db2 = new_session()
        try:
            j = db2.query(Job).filter(Job.id == job_id).first()
            if not j or j.status == "cancelled":
                return
        finally:
            db2.close()

        task = asyncio.create_task(self._process(job_id))
        self._active_procs[job_id] = task
        task.add_done_callback(lambda t: self._active_procs.pop(job_id, None))

    async def _fail(self, job_id: int, error: str):
        db = new_session()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status        = "failed"
                job.error_message = error
                job.completed_at  = datetime.utcnow()
                db.commit()
        finally:
            db.close()
        await ws_manager.broadcast({"type": "job_update", "job_id": job_id, "status": "failed", "error": error})
        _log(f"Job #{job_id} failed: {error}", level="ERROR", job_id=job_id)

    async def _complete(
        self,
        job_id: int,
        rar_file: str,
        action: str,
        folder_snapshot: set[str],
        rar_part_set: set[str],
    ):
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

        # ── TRACK EXTRACTED FILES ───────────────────────────────────────────
        # Diff folder snapshot to find exactly what unrar wrote.
        # Exclude RAR parts so we never accidentally track them.
        dest_path       = str(Path(rar_file).parent)
        after_snapshot  = await asyncio.to_thread(_snapshot_folder, dest_path)
        extracted_files = _new_files(folder_snapshot, after_snapshot, rar_part_set)

        # ── AUTO-EXCLUDE ────────────────────────────────────────────────────
        db = new_session()
        try:
            _add_exclusion(db, rar_file, reason="auto_extracted")

            folder   = str(Path(rar_file).parent)
            all_rars = await asyncio.to_thread(find_rar_sets, folder)
            if all_rars and all(
                db.query(Exclusion).filter(Exclusion.path == r).first() is not None
                for r in all_rars
            ):
                _add_exclusion(db, folder, reason="auto_extracted")
                _log(f"All RARs in '{folder}' extracted — folder auto-excluded", job_id=job_id)

            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status          = "completed"
                job.progress        = 100.0
                job.eta_seconds     = 0
                job.completed_at    = datetime.utcnow()
                job.files_extracted = json.dumps(extracted_files)
            db.commit()
        finally:
            db.close()

        _log(
            f"Job #{job_id} completed — {len(extracted_files)} file(s) extracted and tracked",
            job_id=job_id,
        )
        await ws_manager.broadcast({
            "type": "job_update",
            "job_id": job_id,
            "status": "completed",
            "progress": 100,
        })


queue_manager = QueueManager()
