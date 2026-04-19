"""
watcher.py – watchdog-based filesystem monitor.
When a .rar file appears in a watched folder, waits for the file to stabilise
(size unchanged across two checks STABILISE_SECS apart) then enqueues the RAR set.
The double-check catches cases where the downloader pauses briefly mid-write.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from ..database import new_session
from ..models import WatchedFolder
from .extractor import is_first_rar_part
from .queue_manager import queue_manager

# Wait this long after the last filesystem event before doing the first size check
STABILISE_INITIAL_SECS = 30
# Then wait this long and check size again — if unchanged, the file is done
STABILISE_CONFIRM_SECS = 15


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
        # Reset timer on every filesystem event for this path
        handle = self._pending.pop(rar_path, None)
        if handle:
            handle.cancel()
        handle = self._loop.call_later(STABILISE_INITIAL_SECS, self._fire, rar_path)
        self._pending[rar_path] = handle

    def _fire(self, rar_path: str):
        self._pending.pop(rar_path, None)
        asyncio.run_coroutine_threadsafe(self._check_and_enqueue(rar_path), self._loop)

    @staticmethod
    async def _check_and_enqueue(rar_path: str):
        """
        Double-check the file size is stable before enqueuing.
        Wait STABILISE_INITIAL_SECS (handled by call_later above), then
        check size, wait STABILISE_CONFIRM_SECS, check again.
        Only enqueue if the size is identical — avoids firing mid-download.
        """
        try:
            size1 = os.path.getsize(rar_path)
        except OSError:
            return  # File disappeared — download was cancelled
        await asyncio.sleep(STABILISE_CONFIRM_SECS)
        try:
            size2 = os.path.getsize(rar_path)
        except OSError:
            return
        if size1 != size2:
            # Still growing — reschedule and wait again
            asyncio.get_event_loop().call_later(
                STABILISE_INITIAL_SECS,
                lambda: asyncio.run_coroutine_threadsafe(
                    _Handler._check_and_enqueue(rar_path),
                    asyncio.get_event_loop()
                )
            )
            return
        await _Handler._enqueue(rar_path)

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
