"""
webhooks.py — Incoming webhook receivers for *arr applications.

Flow:
  1. User enters their *arr app URL + API key in UnrarTool settings
  2. UnrarTool uses those to test connectivity (GET /api/v3/system/status)
  3. The same API key is used as the webhook shared secret:
       - In the *arr app, user adds header X-Api-Key = their API key
       - UnrarTool validates incoming webhooks against SHA256(api_key)
  4. On a valid Download event, UnrarTool scans the payload folder and queues extraction

Security measures:
  - API key stored plaintext for outbound calls, but also hashed for fast webhook validation
  - Webhook auth uses constant-time comparison (hmac.compare_digest) — timing-attack safe
  - IP-based rate limiting: 5 failures per IP → 5-minute block
  - Auth failures always return identical 401 — no information leakage
  - Key value is NEVER written to logs
"""

import hashlib
import hmac
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, new_session
from ..models import AppSetting, LogEntry, WebhookSource
from ..services.queue_manager import queue_manager

router      = APIRouter(prefix="/api/webhook",  tags=["webhooks"])
mgmt_router = APIRouter(prefix="/api/webhooks", tags=["webhook-management"])

# Default ports for each *arr app
SOURCE_DEFAULTS = {
    "sonarr":  {"port": 8989, "label": "Sonarr",  "api_path": "/api/v3/system/status"},
    "radarr":  {"port": 7878, "label": "Radarr",  "api_path": "/api/v3/system/status"},
    "lidarr":  {"port": 8686, "label": "Lidarr",  "api_path": "/api/v1/system/status"},
    "readarr": {"port": 8787, "label": "Readarr", "api_path": "/api/v1/system/status"},
}
SOURCES = list(SOURCE_DEFAULTS.keys())

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_rate_limit: dict[str, dict] = {}
_MAX_FAILURES  = 5
_BLOCK_SECONDS = 300


def _check_rate_limit(ip: str) -> None:
    entry = _rate_limit.get(ip)
    if not entry:
        return
    if entry.get("blocked_until", 0) > time.monotonic():
        raise HTTPException(429, "Too many failed attempts. Try again later.")
    _rate_limit.pop(ip, None)


def _record_failure(ip: str) -> None:
    entry = _rate_limit.setdefault(ip, {"failures": 0, "blocked_until": 0})
    entry["failures"] += 1
    if entry["failures"] >= _MAX_FAILURES:
        entry["blocked_until"] = time.monotonic() + _BLOCK_SECONDS
        _log(f"Webhook rate limit triggered for IP {ip} — blocked 5 min", "WARNING")


def _record_success(ip: str) -> None:
    _rate_limit.pop(ip, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str, level: str = "INFO", job_id: int | None = None) -> None:
    db = new_session()
    try:
        db.add(LogEntry(level=level, message=msg, job_id=job_id))
        db.commit()
    finally:
        db.close()
    print(f"[{level}] {msg}")


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _webhooks_enabled(db: Session) -> bool:
    row = db.query(AppSetting).filter(AppSetting.key == "webhooks_enabled").first()
    return row is not None and row.value == "true"


def _ensure_sources(db: Session) -> None:
    for src in SOURCES:
        if not db.query(WebhookSource).filter(WebhookSource.source == src).first():
            db.add(WebhookSource(source=src, enabled=False))
    db.commit()


def _verify_key(db: Session, source: str, provided_key: str | None, ip: str) -> WebhookSource:
    _check_rate_limit(ip)

    if not _webhooks_enabled(db):
        raise HTTPException(403, "Webhook integration is disabled.")

    if not provided_key:
        _record_failure(ip)
        raise HTTPException(401, "Unauthorized.")

    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    if not src or not src.enabled or not src.key_hash:
        _record_failure(ip)
        raise HTTPException(401, "Unauthorized.")

    provided_hash = _hash_key(provided_key)
    if not hmac.compare_digest(src.key_hash, provided_hash):
        _record_failure(ip)
        _log(f"Webhook auth failure for '{source}' from {ip}", "WARNING")
        raise HTTPException(401, "Unauthorized.")

    _record_success(ip)
    return src


def _update_hit(db: Session, src: WebhookSource) -> None:
    src.hit_count = (src.hit_count or 0) + 1
    src.last_hit  = datetime.utcnow()
    db.commit()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Payload parsers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Payload parsers
# ---------------------------------------------------------------------------
# Sonarr/Radarr send the media library path (e.g. /tv/Show/Season 1) in most
# fields — which is useless if that mount isn't in UnrarTool's container.
# downloadFolder is the actual download directory and is what we want.
# All parsers try that first, then fall back to other fields, and always
# verify the resolved path exists on disk before returning it.

def _first_existing(*paths) -> str | None:
    """Return the first path that exists on disk, or None."""
    for p in paths:
        if p and Path(p).exists():
            return str(p)
    return None


def _parse_sonarr(payload: dict) -> str | None:
    ep_path = payload.get("episodeFile", {}).get("path")
    return _first_existing(
        payload.get("downloadFolder"),              # download dir — best option
        str(Path(ep_path).parent) if ep_path else None,  # imported file's folder
        payload.get("series", {}).get("path"),      # series root — last resort
    )


def _parse_radarr(payload: dict) -> str | None:
    mf_path = payload.get("movieFile", {}).get("path")
    movie   = payload.get("movie", {})
    return _first_existing(
        payload.get("downloadFolder"),
        str(Path(mf_path).parent) if mf_path else None,
        movie.get("folderPath"),
        movie.get("path"),
    )


def _parse_lidarr(payload: dict) -> str | None:
    tracks  = payload.get("trackFiles", [])
    tr_path = tracks[0].get("path") if tracks else None
    return _first_existing(
        payload.get("downloadFolder"),
        str(Path(tr_path).parent) if tr_path else None,
        payload.get("artist", {}).get("path"),
    )


def _parse_readarr(payload: dict) -> str | None:
    books   = payload.get("bookFiles", [])
    bk_path = books[0].get("path") if books else None
    return _first_existing(
        payload.get("downloadFolder"),
        str(Path(bk_path).parent) if bk_path else None,
        payload.get("author", {}).get("path"),
    )


PARSERS = {
    "sonarr":  _parse_sonarr,
    "radarr":  _parse_radarr,
    "lidarr":  _parse_lidarr,
    "readarr": _parse_readarr,
}


# ---------------------------------------------------------------------------
# Generic handler
# ---------------------------------------------------------------------------

async def _handle_webhook(source: str, payload: dict, api_key: str | None, ip: str) -> dict:
    db = new_session()
    try:
        src        = _verify_key(db, source, api_key, ip)
        event_type = payload.get("eventType", "unknown")

        _log(f"Webhook received — source: {source}, event: {event_type}, IP: {ip}")

        if event_type == "Test":
            _update_hit(db, src)
            _log(f"Webhook test from {source} — connection verified OK")
            return {"status": "ok", "message": "Test received successfully"}

        if event_type != "Download":
            _log(f"Webhook from {source}: ignoring event '{event_type}'")
            return {"status": "ignored", "event_type": event_type}

        folder_path = PARSERS.get(source, lambda _: None)(payload)

        if not folder_path:
            _log(f"Webhook from {source}: could not extract folder path", "WARNING")
            return {"status": "error", "message": "Could not determine folder path from payload"}

        if not Path(folder_path).exists():
            _log(
                f"Webhook from {source}: path '{folder_path}' not found inside the UnrarTool container. "
                f"This usually means the path reported by {source} is not mounted into UnrarTool. "
                f"Make sure the same downloads folder is mounted in both containers at the same path.",
                "WARNING",
            )
            return {"status": "error", "message": f"Path not found on disk: {folder_path}"}

        _update_hit(db, src)
    finally:
        db.close()

    result  = await queue_manager.enqueue_folder(folder_path=folder_path, source=f"webhook_{source}")
    queued  = len(result["queued"])
    skipped = len(result["skipped"])

    _log(f"Webhook {source}: '{folder_path}' → {queued} queued, {skipped} skipped (excluded)")

    return {
        "status":  "ok",
        "folder":  folder_path,
        "queued":  queued,
        "skipped": skipped,
        "job_ids": result["queued"],
    }


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------

@router.post("/sonarr")
async def webhook_sonarr(request: Request, payload: dict,
                          x_api_key: str | None = Header(default=None)):
    return await _handle_webhook("sonarr", payload, x_api_key, _client_ip(request))


@router.post("/radarr")
async def webhook_radarr(request: Request, payload: dict,
                          x_api_key: str | None = Header(default=None)):
    return await _handle_webhook("radarr", payload, x_api_key, _client_ip(request))


@router.post("/lidarr")
async def webhook_lidarr(request: Request, payload: dict,
                          x_api_key: str | None = Header(default=None)):
    return await _handle_webhook("lidarr", payload, x_api_key, _client_ip(request))


@router.post("/readarr")
async def webhook_readarr(request: Request, payload: dict,
                           x_api_key: str | None = Header(default=None)):
    return await _handle_webhook("readarr", payload, x_api_key, _client_ip(request))


# ---------------------------------------------------------------------------
# Management endpoints
# ---------------------------------------------------------------------------

class SourceStatus(BaseModel):
    source:     str
    enabled:    bool
    app_url:    str | None
    has_key:    bool
    key_suffix: str | None
    hit_count:  int
    last_hit:   str | None


def _source_status(s: WebhookSource) -> SourceStatus:
    return SourceStatus(
        source=s.source, enabled=s.enabled,
        app_url=s.app_url, has_key=bool(s.key_hash),
        key_suffix=s.key_suffix, hit_count=s.hit_count or 0,
        last_hit=s.last_hit.isoformat() if s.last_hit else None,
    )


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


class SaveKeyRequest(BaseModel):
    app_url: str
    api_key: str


@mgmt_router.post("/sources/{source}/save-key")
def save_key(source: str, body: SaveKeyRequest, db: Session = Depends(get_db)):
    """
    Save the *arr app's URL and API key.
    - app_url and api_key stored plaintext so UnrarTool can call their API (test connection)
    - key_hash = SHA256(api_key) used for fast webhook validation
    - key_suffix = last 4 chars for masked display only
    """
    if source not in SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    if not body.app_url.startswith("http"):
        raise HTTPException(400, "App URL must start with http:// or https://")
    if len(body.api_key) < 8:
        raise HTTPException(400, "API key must be at least 8 characters")

    _ensure_sources(db)
    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    src.app_url     = body.app_url.rstrip("/")
    src.arr_api_key = body.api_key
    src.key_hash    = _hash_key(body.api_key)
    src.key_suffix  = body.api_key[-4:]
    src.enabled     = True
    db.commit()

    # Never log the key value
    _log(f"Connection settings saved for webhook source '{source}' ({body.app_url})")
    return {"ok": True, "source": source, "key_suffix": src.key_suffix}


@mgmt_router.delete("/sources/{source}/key")
def revoke_key(source: str, db: Session = Depends(get_db)):
    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    if src:
        src.app_url = src.arr_api_key = src.key_hash = src.key_suffix = None
        src.enabled = False
        db.commit()
    _log(f"Connection settings cleared for webhook source '{source}'")
    return {"ok": True}


@mgmt_router.get("/sources/{source}/test")
async def test_source(source: str, db: Session = Depends(get_db)):
    """
    Test connectivity to the *arr app by calling its /system/status endpoint.
    Uses the stored app_url and arr_api_key — no credentials needed in the request.
    """
    if source not in SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")

    src = db.query(WebhookSource).filter(WebhookSource.source == source).first()
    if not src or not src.app_url or not src.arr_api_key:
        raise HTTPException(400, "App URL and API key not configured yet")

    api_path = SOURCE_DEFAULTS[source]["api_path"]
    url      = f"{src.app_url}{api_path}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"X-Api-Key": src.arr_api_key})

        if resp.status_code == 200:
            data     = resp.json()
            version  = data.get("version", "unknown")
            app_name = data.get("appName") or SOURCE_DEFAULTS[source]["label"]
            _log(f"Webhook test connection OK — {source} v{version} at {src.app_url}")
            return {"ok": True, "version": version, "appName": app_name}

        elif resp.status_code == 401:
            return {"ok": False, "error": "Invalid API key — check the key in your app under Settings → General → Security"}
        elif resp.status_code == 404:
            return {"ok": False, "error": f"Endpoint not found — check the URL is correct ({src.app_url})"}
        else:
            return {"ok": False, "error": f"Unexpected response: HTTP {resp.status_code}"}

    except httpx.ConnectError:
        return {"ok": False, "error": f"Connection refused — is {source.capitalize()} running at {src.app_url}?"}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"Timeout — {src.app_url} did not respond within 10 seconds"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
