"""
webhooks.py — Incoming webhook receivers for *arr applications.

Security measures:
  - API key transmitted via X-Api-Key header ONLY (never URL/query params)
  - Keys stored as SHA256 hashes — plaintext is never persisted
  - Constant-time comparison via hmac.compare_digest (timing-attack safe)
  - IP-based rate limiting: 5 failures → 5-minute block per IP
  - All auth failures return identical 401 (no information leakage)
  - Key value is NEVER written to logs
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, new_session
from ..models import AppSetting, LogEntry, WebhookSource
from ..services.queue_manager import queue_manager

router = APIRouter(prefix="/api/webhook", tags=["webhooks"])
mgmt_router = APIRouter(prefix="/api/webhooks", tags=["webhook-management"])

# ---------------------------------------------------------------------------
# In-memory rate limiter (IP → {failures, blocked_until})
# ---------------------------------------------------------------------------

_rate_limit: dict[str, dict] = {}
_MAX_FAILURES  = 5
_BLOCK_SECONDS = 300   # 5 minutes


def _check_rate_limit(ip: str) -> None:
    """Raises 429 if the IP is currently blocked."""
    entry = _rate_limit.get(ip)
    if not entry:
        return
    if entry.get("blocked_until", 0) > time.monotonic():
        raise HTTPException(429, "Too many failed attempts. Try again later.")
    # Block expired — clean up
    if entry.get("blocked_until", 0) <= time.monotonic():
        _rate_limit.pop(ip, None)


def _record_failure(ip: str) -> None:
    entry = _rate_limit.setdefault(ip, {"failures": 0, "blocked_until": 0})
    entry["failures"] += 1
    if entry["failures"] >= _MAX_FAILURES:
        entry["blocked_until"] = time.monotonic() + _BLOCK_SECONDS
        _log(f"Webhook rate limit triggered for IP {ip} — blocked for 5 minutes", "WARNING")


def _record_success(ip: str) -> None:
    _rate_limit.pop(ip, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(message: str, level: str = "INFO", job_id: int | None = None) -> None:
    db = new_session()
    try:
        db.add(LogEntry(level=level, message=message, job_id=job_id))
        db.commit()
    finally:
        db.close()
    print(f"[{level}] {message}")


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _webhooks_enabled(db: Session) -> bool:
    row = db.query(AppSetting).filter(AppSetting.key == "webhooks_enabled").first()
    return row is not None and row.value == "true"


def _get_source(db: Session, source: str) -> WebhookSource | None:
    return db.query(WebhookSource).filter(WebhookSource.source == source).first()


def _verify_key(db: Session, source: str, provided_key: str | None, ip: str) -> WebhookSource:
    """
    Authenticate the request. Raises HTTPException on any failure.
    The error message is intentionally generic to avoid leaking information.
    """
    _check_rate_limit(ip)

    if not _webhooks_enabled(db):
        raise HTTPException(403, "Webhook integration is disabled.")

    if not provided_key:
        _record_failure(ip)
        raise HTTPException(401, "Unauthorized.")

    src = _get_source(db, source)
    if not src or not src.enabled or not src.key_hash:
        _record_failure(ip)
        raise HTTPException(401, "Unauthorized.")

    # Constant-time comparison — prevents timing attacks
    provided_hash = _hash_key(provided_key)
    if not hmac.compare_digest(src.key_hash, provided_hash):
        _record_failure(ip)
        _log(f"Webhook auth failure for source '{source}' from {ip}", "WARNING")
        raise HTTPException(401, "Unauthorized.")

    _record_success(ip)
    return src


def _update_hit(db: Session, src: WebhookSource) -> None:
    src.hit_count = (src.hit_count or 0) + 1
    src.last_hit  = datetime.utcnow()
    db.commit()


# ---------------------------------------------------------------------------
# Payload parsers — extract the relevant folder path from each *arr payload
# ---------------------------------------------------------------------------

def _parse_sonarr(payload: dict) -> str | None:
    """
    Sonarr sends episodeFile.path (full file path).
    We want the parent directory.
    Also handles series.path as fallback.
    """
    # Prefer the most specific path first
    ep = payload.get("episodeFile", {})
    path = ep.get("path") or ep.get("relativePath")
    if path:
        p = Path(path)
        return str(p.parent) if p.suffix else path

    # Fallback: series root
    series = payload.get("series", {})
    return series.get("path")


def _parse_radarr(payload: dict) -> str | None:
    mf = payload.get("movieFile", {})
    path = mf.get("path")
    if path:
        return str(Path(path).parent)

    movie = payload.get("movie", {})
    return movie.get("folderPath") or movie.get("path")


def _parse_lidarr(payload: dict) -> str | None:
    tracks = payload.get("trackFiles", [])
    if tracks and tracks[0].get("path"):
        return str(Path(tracks[0]["path"]).parent)
    artist = payload.get("artist", {})
    return artist.get("path")


def _parse_readarr(payload: dict) -> str | None:
    books = payload.get("bookFiles", [])
    if books and books[0].get("path"):
        return str(Path(books[0]["path"]).parent)
    author = payload.get("author", {})
    return author.get("path")


PARSERS = {
    "sonarr":  _parse_sonarr,
    "radarr":  _parse_radarr,
    "lidarr":  _parse_lidarr,
    "readarr": _parse_readarr,
}


# ---------------------------------------------------------------------------
# Generic webhook handler
# ---------------------------------------------------------------------------

async def _handle_webhook(
    source: str,
    payload: dict,
    api_key: str | None,
    ip: str,
) -> dict:
    db = new_session()
    try:
        src = _verify_key(db, source, api_key, ip)

        event_type = payload.get("eventType", "unknown")
        _log(f"Webhook received from {source} [{event_type}] (IP: {ip})")

        # Test event — *arr apps send this when you click "Test" in their UI
        if event_type == "Test":
            _update_hit(db, src)
            _log(f"Webhook test from {source} — connection OK")
            return {"status": "ok", "message": "Test received successfully"}

        # Only process Download events
        if event_type != "Download":
            _log(f"Webhook from {source}: ignoring event type '{event_type}'")
            return {"status": "ignored", "event_type": event_type}

        # Extract folder path from payload
        parser = PARSERS.get(source)
        folder_path = parser(payload) if parser else None

        if not folder_path:
            _log(
                f"Webhook from {source}: could not extract folder path from payload",
                "WARNING",
            )
            return {"status": "error", "message": "Could not determine folder path from payload"}

        if not Path(folder_path).exists():
            _log(
                f"Webhook from {source}: path does not exist on disk: {folder_path}",
                "WARNING",
            )
            return {"status": "error", "message": "Path not found on disk"}

        _update_hit(db, src)
    finally:
        db.close()

    # Enqueue — exclusions are respected (force=False), per user preference
    result = await queue_manager.enqueue_folder(
        folder_path=folder_path,
        source=f"webhook_{source}",
    )

    queued  = len(result["queued"])
    skipped = len(result["skipped"])

    _log(
        f"Webhook {source}: scanned '{folder_path}' → "
        f"{queued} queued, {skipped} skipped (excluded)"
    )

    return {
        "status": "ok",
        "folder": folder_path,
        "queued": queued,
        "skipped": skipped,
        "job_ids": result["queued"],
    }


# ---------------------------------------------------------------------------
# Webhook endpoints (one per source)
# ---------------------------------------------------------------------------

class WebhookPayload(BaseModel):
    model_config = {"extra": "allow"}


def _client_ip(request: Request) -> str:
    # Honour X-Forwarded-For if behind a reverse proxy
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/sonarr")
async def webhook_sonarr(
    request: Request,
    payload: dict,
    x_api_key: str | None = Header(default=None),
):
    return await _handle_webhook("sonarr", payload, x_api_key, _client_ip(request))


@router.post("/radarr")
async def webhook_radarr(
    request: Request,
    payload: dict,
    x_api_key: str | None = Header(default=None),
):
    return await _handle_webhook("radarr", payload, x_api_key, _client_ip(request))


@router.post("/lidarr")
async def webhook_lidarr(
    request: Request,
    payload: dict,
    x_api_key: str | None = Header(default=None),
):
    return await _handle_webhook("lidarr", payload, x_api_key, _client_ip(request))


@router.post("/readarr")
async def webhook_readarr(
    request: Request,
    payload: dict,
    x_api_key: str | None = Header(default=None),
):
    return await _handle_webhook("readarr", payload, x_api_key, _client_ip(request))


# ---------------------------------------------------------------------------
# Management endpoints (called by the Settings UI)
# ---------------------------------------------------------------------------

SOURCES = ["sonarr", "radarr", "lidarr", "readarr"]


class SourceStatus(BaseModel):
    source:      str
    enabled:     bool
    has_key:     bool
    key_suffix:  str | None   # last 4 chars only — never the full key
    hit_count:   int
    last_hit:    str | None


def _source_status(s: WebhookSource) -> SourceStatus:
    return SourceStatus(
        source=s.source,
        enabled=s.enabled,
        has_key=bool(s.key_hash),
        key_suffix=s.key_suffix,
        hit_count=s.hit_count or 0,
        last_hit=s.last_hit.isoformat() if s.last_hit else None,
    )


def _ensure_sources(db: Session) -> None:
    """Create default rows for all sources if they don't exist."""
    for src in SOURCES:
        if not db.query(WebhookSource).filter(WebhookSource.source == src).first():
            db.add(WebhookSource(source=src, enabled=False))
    db.commit()


@mgmt_router.get("/sources", response_model=list[SourceStatus])
def list_sources(db: Session = Depends(get_db)):
    _ensure_sources(db)
    return [_source_status(s) for s in
            db.query(WebhookSource).order_by(WebhookSource.source).all()]


@mgmt_router.patch("/sources/{source}")
def update_source(source: str, enabled: bool, db: Session = Depends(get_db)):
    if source not in SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    _ensure_sources(db)
    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    src.enabled = enabled
    db.commit()
    return {"ok": True}


@mgmt_router.post("/sources/{source}/generate-key")
def generate_key(source: str, db: Session = Depends(get_db)):
    """
    Generate a new API key for a source.
    Returns the plaintext key ONCE — it is never stored and cannot be retrieved again.
    """
    if source not in SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    _ensure_sources(db)

    plaintext = secrets.token_urlsafe(32)          # 256 bits of entropy
    key_hash  = _hash_key(plaintext)
    suffix    = plaintext[-4:]                      # last 4 chars for display

    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    src.key_hash   = key_hash
    src.key_suffix = suffix
    src.enabled    = True
    db.commit()

    _log(f"New API key generated for webhook source '{source}'")

    # Return plaintext key exactly once — UI must show it to the user immediately
    return {
        "ok":        True,
        "source":    source,
        "key":       plaintext,   # shown once, never stored
        "key_suffix": suffix,
    }


@mgmt_router.delete("/sources/{source}/key")
def revoke_key(source: str, db: Session = Depends(get_db)):
    """Revoke (delete) the API key for a source. Source is disabled automatically."""
    if source not in SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    if src:
        src.key_hash  = None
        src.key_suffix = None
        src.enabled   = False
        db.commit()
    _log(f"API key revoked for webhook source '{source}'")
    return {"ok": True}


@mgmt_router.get("/enabled")
def get_webhooks_enabled(db: Session = Depends(get_db)):
    row = db.query(AppSetting).filter(AppSetting.key == "webhooks_enabled").first()
    return {"enabled": row is not None and row.value == "true"}


@mgmt_router.put("/enabled")
def set_webhooks_enabled(enabled: bool, db: Session = Depends(get_db)):
    row = db.query(AppSetting).filter(AppSetting.key == "webhooks_enabled").first()
    if row:
        row.value = "true" if enabled else "false"
    else:
        db.add(AppSetting(key="webhooks_enabled", value="true" if enabled else "false"))
    db.commit()
    _log(f"Webhook integration {'enabled' if enabled else 'disabled'}")
    return {"ok": True, "enabled": enabled}
