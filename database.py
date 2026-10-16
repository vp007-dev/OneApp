# database.py
import os
import sqlite3
from typing import Generator
from sqlalchemy import event, text, inspect
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, Session, create_engine
from models import VehicleRecord, Device

# Determine Database URL
RAW_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Workaround for safe and free database:
# 1. If DATABASE_URL is not provided, use an optimized local SQLite DB (100% free, zero cost, rock-solid).
# 2. If PostgreSQL is provided via Render/Supabase/Neon/Heroku (which often starts with postgres://),
#    automatically convert postgres:// to postgresql:// so SQLAlchemy doesn't crash.
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "tractorcare.db"))

if not RAW_DATABASE_URL:
    DATABASE_URL = f"sqlite:///{DB_PATH}"
    IS_SQLITE = True
else:
    if RAW_DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = RAW_DATABASE_URL.replace("postgres://", "postgresql://", 1)
    else:
        DATABASE_URL = RAW_DATABASE_URL
    IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Engine configuration with health checks & connection pooling
engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}

if IS_SQLITE:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    # Configure SQLite with WAL mode for safe multi-worker concurrency
    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=10000;")
            cursor.close()
else:
    engine_kwargs["pool_recycle"] = 300
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

engine = create_engine(DATABASE_URL, **engine_kwargs)


def init_db():
    """Create tables and apply non-destructive schema migrations if needed."""
    SQLModel.metadata.create_all(engine)

    # Safe lightweight column migration for existing tables
    try:
        inspector = inspect(engine)
        if "vehiclerecord" in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns("vehiclerecord")]
            with engine.connect() as conn:
                if "owner_phone" not in columns:
                    conn.execute(text("ALTER TABLE vehiclerecord ADD COLUMN owner_phone VARCHAR DEFAULT ''"))
                if "notes" not in columns:
                    conn.execute(text("ALTER TABLE vehiclerecord ADD COLUMN notes VARCHAR DEFAULT ''"))
                if "last_alert_sent" not in columns:
                    conn.execute(text("ALTER TABLE vehiclerecord ADD COLUMN last_alert_sent DATE"))
                if "created_at" not in columns:
                    conn.execute(text("ALTER TABLE vehiclerecord ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
                conn.commit()
    except Exception as e:
        print(f"Schema verification note: {e}")


def get_session() -> Generator[Session, None, None]:
    """FastAPI database session dependency."""
    with Session(engine) as session:
        yield session
