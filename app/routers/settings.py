from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AppSetting
from ..services.scheduler import scan_scheduler

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULTS = {
    "scan_interval_minutes": "30",
    "max_concurrent_jobs": "1",
    "default_post_action": "keep",
    "trash_folder": "/config/trash",
    "watch_enabled": "true",
}


class SettingsOut(BaseModel):
    scan_interval_minutes: int
    max_concurrent_jobs: int
    default_post_action: str
    trash_folder: str
    watch_enabled: bool


class SettingsIn(BaseModel):
    scan_interval_minutes: int | None = None
    max_concurrent_jobs: int | None = None
    default_post_action: str | None = None
    trash_folder: str | None = None
    watch_enabled: bool | None = None


def _get(db, key: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else DEFAULTS.get(key, "")


def _set(db, key: str, value: str):
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return SettingsOut(
        scan_interval_minutes=int(_get(db, "scan_interval_minutes")),
        max_concurrent_jobs=int(_get(db, "max_concurrent_jobs")),
        default_post_action=_get(db, "default_post_action"),
        trash_folder=_get(db, "trash_folder"),
        watch_enabled=_get(db, "watch_enabled") == "true",
    )


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsIn, db: Session = Depends(get_db)):
    if body.scan_interval_minutes is not None:
        _set(db, "scan_interval_minutes", str(body.scan_interval_minutes))
        scan_scheduler.reschedule(body.scan_interval_minutes)
    if body.max_concurrent_jobs is not None:
        _set(db, "max_concurrent_jobs", str(body.max_concurrent_jobs))
    if body.default_post_action is not None:
        _set(db, "default_post_action", body.default_post_action)
    if body.trash_folder is not None:
        _set(db, "trash_folder", body.trash_folder)
    if body.watch_enabled is not None:
        _set(db, "watch_enabled", "true" if body.watch_enabled else "false")
    db.commit()
    return get_settings(db)
