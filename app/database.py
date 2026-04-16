from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pathlib import Path
from .config import config

Path(config.CONFIG_PATH).mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{config.CONFIG_PATH}/rarunpacker.db"
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    from .models import Base
    Base.metadata.create_all(bind=engine)


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
