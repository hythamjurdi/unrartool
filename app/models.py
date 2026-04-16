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


class AppSetting(Base):
    __tablename__ = "app_settings"

    key   = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
