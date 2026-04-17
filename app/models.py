from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, Float
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class WatchedFolder(Base):
    __tablename__ = "watched_folders"

    id               = Column(Integer, primary_key=True, index=True)
    path             = Column(String, unique=True, nullable=False)
    enabled          = Column(Boolean, default=True)
    password         = Column(String, nullable=True)
    post_action      = Column(String, default="keep")   # keep | delete | trash
    marked_extracted = Column(Boolean, default=False)
    created_at       = Column(DateTime, default=datetime.utcnow)
    last_scanned     = Column(DateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id              = Column(Integer, primary_key=True, index=True)
    folder_path     = Column(String, nullable=False)
    rar_file        = Column(String, nullable=False)   # first-part path
    status          = Column(String, default="pending")  # pending|running|completed|failed|skipped|cancelled
    progress        = Column(Float, default=0.0)
    eta_seconds     = Column(Integer, nullable=True)
    error_message   = Column(Text, nullable=True)
    post_action     = Column(String, default="keep")
    password        = Column(String, nullable=True)
    source          = Column(String, default="manual")  # manual|watch|scheduled
    files_extracted = Column(Text, nullable=True)       # JSON list of extracted filenames
    created_at      = Column(DateTime, default=datetime.utcnow)
    started_at      = Column(DateTime, nullable=True)
    completed_at    = Column(DateTime, nullable=True)


class LogEntry(Base):
    __tablename__ = "log_entries"

    id        = Column(Integer, primary_key=True, index=True)
    level     = Column(String, nullable=False)   # INFO|WARNING|ERROR
    message   = Column(Text, nullable=False)
    job_id    = Column(Integer, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class WebhookSource(Base):
    """
    Stores per-source webhook configuration.
    The API key is NEVER stored plaintext — only its SHA256 hash.
    key_suffix stores the last 4 chars of the original key for UI display only.
    """
    __tablename__ = "webhook_sources"

    id          = Column(Integer, primary_key=True, index=True)
    source      = Column(String, unique=True, nullable=False)  # sonarr|radarr|lidarr|readarr
    enabled     = Column(Boolean, default=False)
    key_hash    = Column(String, nullable=True)   # SHA256 hex digest
    key_suffix  = Column(String, nullable=True)   # last 4 chars of plaintext key (display only)
    hit_count   = Column(Integer, default=0)
    last_hit    = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class Exclusion(Base):
    """
    Tracks paths (folders or specific RAR files) that should never be
    auto-queued — either set manually by the user or automatically after
    a successful extraction.
    """
    __tablename__ = "exclusions"

    id         = Column(Integer, primary_key=True, index=True)
    path       = Column(String, unique=True, nullable=False)  # folder OR rar_file path
    reason     = Column(String, default="manual")             # manual | auto_extracted
    created_at = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key   = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
