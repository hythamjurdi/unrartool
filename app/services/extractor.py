"""
extractor.py – RAR discovery, integrity check, and real-time extraction.

Progress is driven by parsing unrar's own stdout output.
unrar prints lines like:
    Extracting  Movie.S01E01.mkv                                        3%
using \\r (carriage return) to overwrite the same terminal line.
We split the raw byte stream on both \\r and \\n, then regex-match the
trailing percentage on each segment — giving us smooth, real-time updates
directly from the tool itself rather than guessing from folder sizes.
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
    """Return True only for the first part of a RAR set (or standalone RAR)."""
    name = path.name.lower()
    m = re.match(r'^.+\.part(\d+)\.rar$', name)
    if m:
        return int(m.group(1)) == 1
    return name.endswith('.rar')


def find_rar_sets(folder_path: str) -> list[str]:
    """Recursively find the first part of every RAR set under folder_path."""
    root = Path(folder_path)
    return sorted(str(p) for p in root.rglob("*.rar") if is_first_rar_part(p))


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

async def check_parts_complete(rar_path: str) -> tuple[bool, str]:
    """Run `unrar l` to verify all volumes are present. Returns (ok, error)."""
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
    """Return total unpacked size in bytes from `unrar l`, or 0 on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "unrar", "l", rar_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        output = out.decode("utf-8", errors="replace")
        candidates = [int(s.replace(",", "")) for s in re.findall(r"[\d,]{4,}", output)]
        return max(candidates) if candidates else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Real-time stdout reader
# ---------------------------------------------------------------------------

# Matches a percentage at the end of a unrar progress line, e.g.:
#   "Extracting  Movie.mkv                                              37%"
_PCT_RE = re.compile(r'\b(\d{1,3})%')


async def _read_progress(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue,
    stderr_buf: list[bytes],
    is_stderr: bool = False,
) -> None:
    """
    Read raw bytes from stdout (or stderr), split on both \\r and \\n,
    parse percentage values, and push them onto queue.
    Also buffers everything for post-mortem error reporting.
    """
    buf = b""
    while True:
        chunk = await stream.read(512)
        if not chunk:
            break
        stderr_buf.append(chunk)
        if is_stderr:
            continue                    # stderr: buffer only, don't parse %

        buf += chunk
        # Split on \r or \n — unrar uses \r to overwrite the progress line
        segments = re.split(b'[\r\n]', buf)
        # The last element may be incomplete — keep it in the buffer
        buf = segments.pop()

        for seg in segments:
            text = seg.decode("utf-8", errors="replace")
            m = _PCT_RE.search(text)
            if m:
                pct = int(m.group(1))
                await queue.put(("progress", float(pct)))

    # Flush any remaining buffer
    if buf:
        text = buf.decode("utf-8", errors="replace")
        m = _PCT_RE.search(text)
        if m:
            await queue.put(("progress", float(int(m.group(1)))))

    await queue.put(("eof", None))


# ---------------------------------------------------------------------------
# Extraction generator
# ---------------------------------------------------------------------------

async def extract(
    rar_path: str,
    dest_path: str,
    password: str | None = None,
    total_size: int = 0,             # kept for API compat, no longer used
) -> AsyncIterator[tuple[float, int | None, str]]:
    """
    Async generator that yields (progress_pct, eta_seconds, status_line).
    Yields progress=-1 on failure (status_line contains the error message).
    Yields progress=100 on success.

    Progress comes directly from unrar's stdout percentage output, giving
    smooth, accurate real-time updates for both single files and large
    multi-part archives.
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

    progress_queue: asyncio.Queue = asyncio.Queue()
    stdout_buf: list[bytes] = []
    stderr_buf: list[bytes] = []

    # Two background tasks — one parses progress from stdout, one drains stderr
    reader_tasks = [
        asyncio.create_task(
            _read_progress(proc.stdout, progress_queue, stdout_buf, is_stderr=False)
        ),
        asyncio.create_task(
            _read_progress(proc.stderr, progress_queue, stderr_buf, is_stderr=True)
        ),
    ]

    start        = time.monotonic()
    last_pct     = 0.0
    eof_count    = 0                # we expect 2 EOFs (stdout + stderr)
    done         = False

    while not done:
        try:
            kind, value = await asyncio.wait_for(
                progress_queue.get(), timeout=0.25
            )
        except asyncio.TimeoutError:
            # No new data yet — re-yield the last known percentage so the UI
            # keeps the bar alive (important for large files with sparse output)
            elapsed = time.monotonic() - start
            eta = None
            if last_pct > 0.5:
                eta = int(elapsed / last_pct * (100.0 - last_pct))
            yield last_pct, eta, "extracting"
            continue

        if kind == "eof":
            eof_count += 1
            if eof_count >= 2:
                done = True
            continue

        if kind == "progress":
            pct = value
            # Only move forward — never let the bar go backwards
            if pct > last_pct:
                last_pct = pct
            elapsed = time.monotonic() - start
            eta = None
            if last_pct > 0.5:
                eta = int(elapsed / last_pct * (100.0 - last_pct))
            yield last_pct, eta, "extracting"

    # Wait for reader tasks to clean up
    await asyncio.gather(*reader_tasks, return_exceptions=True)

    # Wait for the process itself
    try:
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()

    if proc.returncode == 0:
        yield 100.0, 0, "Extraction complete"
    else:
        stderr_text = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()
        stdout_text = b"".join(stdout_buf).decode("utf-8", errors="replace").strip()
        err = stderr_text or stdout_text or f"unrar exited with code {proc.returncode}"
        yield -1.0, None, err[:500]


# ---------------------------------------------------------------------------
# Post-action helpers
# ---------------------------------------------------------------------------

def rar_part_paths(first_part: str) -> list[str]:
    """Return every file path that belongs to this RAR set."""
    p = Path(first_part)
    name = p.name.lower()
    parent = p.parent

    if re.match(r'^.+\.part\d+\.rar$', name):
        stem = re.sub(r'\.part\d+\.rar$', '', name)
        return [str(f) for f in parent.iterdir()
                if re.match(rf'^{re.escape(stem)}\.part\d+\.rar$', f.name.lower())]
    else:
        stem = p.stem
        parts = [str(p)]
        parts += [str(f) for f in parent.iterdir()
                  if re.match(rf'^{re.escape(stem)}\.r\d+$', f.name.lower())]
        return parts


def delete_rar_parts(first_part: str) -> None:
    for path in rar_part_paths(first_part):
        try:
            os.remove(path)
        except OSError:
            pass


def trash_rar_parts(first_part: str, trash_folder: str) -> None:
    Path(trash_folder).mkdir(parents=True, exist_ok=True)
    for path in rar_part_paths(first_part):
        try:
            shutil.move(path, trash_folder)
        except OSError:
            pass
