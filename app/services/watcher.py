"""
watcher.py – watchdog-based filesystem monitor.
When a .rar file appears in a watched folder, waits for the file to stabilise
(size unchanged for STABILISE_SECS) then enqueues the RAR set.
"""

import asyncio
import time
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from ..database import new_session
from ..models import WatchedFolder
from .extractor import is_first_rar_part
from .queue_manager import queue_manager

STABILISE_SECS = 15   # seconds of unchanged file size before treating as done


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._loop = loop
        self._pending: dict[str, asyncio.TimerHandle] = {}

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if is_first_rar_part(path):
            self._schedule(str(path))

    def on_moved(self, event: FileSystemEvent):
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if is_first_rar_part(path):
            self._schedule(str(path))

    def _schedule(self, rar_path: str):
        # Cancel existing timer for this path (re-detected)
        handle = self._pending.pop(rar_path, None)
        if handle:
            handle.cancel()
        handle = self._loop.call_later(STABILISE_SECS, self._fire, rar_path)
        self._pending[rar_path] = handle

    def _fire(self, rar_path: str):
        self._pending.pop(rar_path, None)
        asyncio.run_coroutine_threadsafe(self._enqueue(rar_path), self._loop)

    @staticmethod
    async def _enqueue(rar_path: str):
        from ..models import Exclusion
        db = new_session()
        try:
            folder = str(Path(rar_path).parent)

            # Check exclusion table first — avoids hitting the queue at all
            paths_to_check = [rar_path, folder]
            p = Path(rar_path).parent
            while p != p.parent:
                paths_to_check.append(str(p))
                p = p.parent
            if db.query(Exclusion).filter(Exclusion.path.in_(paths_to_check)).first():
                print(f"[INFO] Watcher skipped excluded path: {rar_path}")
                return

            # Find matching watch folder config
            wf = (
                db.query(WatchedFolder)
                .filter(WatchedFolder.enabled == True, WatchedFolder.marked_extracted == False)  # noqa: E712
                .all()
            )
            matched: Optional[WatchedFolder] = None
            for w in wf:
                try:
                    Path(folder).relative_to(w.path)
                    if matched is None or len(w.path) > len(matched.path):
                        matched = w
                except ValueError:
                    pass
            if matched is None:
                return

            await queue_manager.enqueue(
                rar_file=rar_path,
                post_action=matched.post_action,
                password=matched.password,
                source="watch",
            )
        finally:
            db.close()


class FolderWatcher:
    def __init__(self):
        self._observer: Optional[Observer] = None
        self._handler: Optional[_Handler] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._handler = _Handler(loop)
        self._observer = Observer()

        db = new_session()
        try:
            folders = db.query(WatchedFolder).filter(WatchedFolder.enabled == True).all()  # noqa: E712
            for wf in folders:
                self._watch(wf.path)
        finally:
            db.close()

        self._observer.start()

    async def stop(self):
        if self._observer:
            self._observer.stop()
            self._observer.join()

    def _watch(self, path: str):
        try:
            self._observer.schedule(self._handler, path, recursive=True)
        except Exception as e:
            print(f"[WARNING] Cannot watch {path}: {e}")

    def add_path(self, path: str):
        if self._observer:
            self._watch(path)

    def remove_path(self, path: str):
        """watchdog doesn't support unscheduling by path cleanly; restart observer."""
        if self._observer:
            self._observer.unschedule_all()
            db = new_session()
            try:
                folders = db.query(WatchedFolder).filter(WatchedFolder.enabled == True).all()  # noqa: E712
                for wf in folders:
                    if wf.path != path:
                        self._watch(wf.path)
            finally:
                db.close()


folder_watcher = FolderWatcher()
