from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from pathlib import Path
from .config import config

Path(config.CONFIG_PATH).mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{config.CONFIG_PATH}/rarunpacker.db"
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _run_migrations():
    """
    Safe incremental migrations for SQLite.
    SQLAlchemy's create_all() only creates missing tables — it never
    alters existing ones. Any new columns added to a model must be
    applied here manually. ALTER TABLE IF fails silently if the column
    already exists, so this is safe to run on every startup.
    """
    migrations = [
        # v1.2.0 — webhook source credentials
        "ALTER TABLE webhook_sources ADD COLUMN app_url VARCHAR",
        "ALTER TABLE webhook_sources ADD COLUMN arr_api_key VARCHAR",
        # future migrations go here
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                # Column already exists — safe to ignore
                pass


def init_db():
    from .models import Base
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_session():
    """Standalone session for use inside services."""
    return SessionLocal()
