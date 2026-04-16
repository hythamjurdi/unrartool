"""
extractor.py – RAR discovery, integrity check, and extraction.
Progress is estimated by monitoring the size of extracted files vs the
archive's declared uncompressed size (avoids fragile stdout parsing).
"""

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import AsyncIterator


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def is_first_rar_part(path: Path) -> bool:
    """Return True only for the first part of a RAR set (or a standalone RAR)."""
    name = path.name.lower()
    # New-style multipart: .partN.rar  — keep only part 1
    m = re.match(r'^.+\.part(\d+)\.rar$', name)
    if m:
        return int(m.group(1)) == 1
    # Old-style .rar / .r00 / .r01 …  — .rar is always the first part
    return name.endswith('.rar')


def find_rar_sets(folder_path: str) -> list[str]:
    """Recursively find the first part of every RAR set under folder_path."""
    root = Path(folder_path)
    return sorted(str(p) for p in root.rglob("*.rar") if is_first_rar_part(p))


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------

async def check_parts_complete(rar_path: str) -> tuple[bool, str]:
    """
    Run `unrar l` to verify all volumes are present.
    Returns (ok, error_message).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "unrar", "l", rar_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = out.decode("utf-8", errors="replace")

        if "Cannot find volume" in output or "cannot find" in output.lower():
            m = re.search(r"Cannot find volume (.+)", output, re.IGNORECASE)
            missing = m.group(1).strip() if m else "unknown volume"
            return False, f"Missing part: {missing}"

        if proc.returncode != 0 and "All OK" not in output:
            return False, output.strip()[:300]

        return True, ""
    except FileNotFoundError:
        return False, "unrar binary not found"
    except Exception as e:
        return False, str(e)


async def get_declared_size(rar_path: str) -> int:
    """
    Ask unrar for the total unpacked size of the archive.
    Returns bytes, or 0 on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "unrar", "l", rar_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        output = out.decode("utf-8", errors="replace")

        # Summary line looks like: "     3  1,234,567,890    ...  3 files"
        # Grab all bare numbers followed by whitespace and pick the biggest one.
        candidates = [
            int(s.replace(",", ""))
            for s in re.findall(r"[\d,]{4,}", output)
        ]
        return max(candidates) if candidates else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Size helper (used for progress)
# ---------------------------------------------------------------------------

def folder_size(path: str) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += folder_size(entry.path)
            except OSError:
                pass
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

async def extract(
    rar_path: str,
    dest_path: str,
    password: str | None = None,
    total_size: int = 0,
) -> AsyncIterator[tuple[float, int | None, str]]:
    """
    Async generator that yields (progress %, eta_seconds, status_line).
    Yields progress=-1 on failure with status_line containing the error.
    Yields progress=100 on success.
    """
    cmd = ["unrar", "x", "-y", "-o+"]
    cmd.append(f"-p{password}" if password else "-p-")
    cmd.extend([rar_path, dest_path.rstrip("/") + "/"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        yield -1.0, None, "unrar binary not found in PATH"
        return

    start = time.monotonic()
    stop_flag = asyncio.Event()

    async def _monitor() -> AsyncIterator[tuple[float, int | None]]:
        """Polls extracted file size every second for progress."""
        if total_size <= 0:
            return
        while not stop_flag.is_set():
            current = await asyncio.to_thread(folder_size, dest_path)
            pct = min(99.0, current / total_size * 100)
            elapsed = time.monotonic() - start
            eta = int(elapsed / pct * (100 - pct)) if pct > 0.5 else None
            yield pct, eta
            await asyncio.sleep(1)

    # Run extraction and monitor concurrently
    monitor_task = asyncio.create_task(_run_monitor(_monitor(), stop_flag))

    try:
        stdout_data, stderr_data = await proc.communicate()
    finally:
        stop_flag.set()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    if proc.returncode == 0:
        yield 100.0, 0, "Extraction complete"
    else:
        err = (stderr_data or stdout_data).decode("utf-8", errors="replace").strip()
        yield -1.0, None, err[:500] or f"unrar exited with code {proc.returncode}"


async def _run_monitor(gen, stop_flag):
    """Drain the monitor async generator (it yields internally but we don't
    need the values here — callers will query DB/WS via the queue manager)."""
    async for _ in gen:
        pass


# ---------------------------------------------------------------------------
# Post-action helpers
# ---------------------------------------------------------------------------

def rar_part_paths(first_part: str) -> list[str]:
    """Return all file paths that belong to this RAR set."""
    p = Path(first_part)
    name = p.name.lower()
    parent = p.parent
    parts = []

    if re.match(r'^.+\.part\d+\.rar$', name):
        stem = re.sub(r'\.part\d+\.rar$', '', name)
        parts = [str(f) for f in parent.iterdir()
                 if re.match(rf'^{re.escape(stem)}\.part\d+\.rar$', f.name.lower())]
    else:
        stem = p.stem
        parts = [str(p)]
        # old-style .r00 .r01 …
        parts += [str(f) for f in parent.iterdir()
                  if re.match(rf'^{re.escape(stem)}\.r\d+$', f.name.lower())]

    return parts


def delete_rar_parts(first_part: str):
    for path in rar_part_paths(first_part):
        try:
            os.remove(path)
        except OSError:
            pass


def trash_rar_parts(first_part: str, trash_folder: str):
    Path(trash_folder).mkdir(parents=True, exist_ok=True)
    for path in rar_part_paths(first_part):
        try:
            shutil.move(path, trash_folder)
        except OSError:
            pass
