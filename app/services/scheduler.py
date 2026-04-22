"""
scheduler.py – periodically scans all enabled watch folders and queues
any new RAR sets that aren't already done / marked-extracted.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..database import new_session
from ..models import AppSetting, Job, WatchedFolder
from .extractor import find_rar_sets
from .queue_manager import queue_manager


def _get_interval(db) -> int:
    row = db.query(AppSetting).filter(AppSetting.key == "scan_interval_minutes").first()
    return int(row.value) if row and row.value else 30


async def _scan_all():
    db = new_session()
    try:
        folders = (
            db.query(WatchedFolder)
            .filter(
                WatchedFolder.enabled == True,           # noqa: E712
                WatchedFolder.marked_extracted == False, # noqa: E712
            )
            .all()
        )
        folder_data = [
            (wf.path, wf.post_action, wf.password)
            for wf in folders
        ]

        # Update last_scanned
        for wf in folders:
            wf.last_scanned = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    for path, post_action, password in folder_data:
        rar_sets = await asyncio.to_thread(find_rar_sets, path)
        for rar in rar_sets:
            # Skip if already completed OR if there is an active/pending job
            # (watcher may have already queued it, possibly deferred while download finishes)
            db2 = new_session()
            try:
                existing = (
                    db2.query(Job)
                    .filter(
                        Job.rar_file == rar,
                        Job.status.in_(["completed", "pending", "running"]),
                    )
                    .first()
                )
                if existing:
                    continue
            finally:
                db2.close()

            await queue_manager.enqueue(rar, post_action, password, source="scheduled")


class ScanScheduler:
    def __init__(self):
        self._scheduler = AsyncIOScheduler()
        self._job = None

    async def start(self):
        db = new_session()
        try:
            interval = _get_interval(db)
        finally:
            db.close()

        self._job = self._scheduler.add_job(
            _scan_all,
            "interval",
            minutes=interval,
            id="scan_all",
        )
        self._scheduler.start()

    async def stop(self):
        self._scheduler.shutdown(wait=False)

    def reschedule(self, interval_minutes: int):
        if self._job:
            self._job.reschedule("interval", minutes=interval_minutes)

    async def run_now(self):
        await _scan_all()


scan_scheduler = ScanScheduler()
