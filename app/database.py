"""
Database Configuration and Session Management for InvoicePilot AI.
Supports both SQLite (local development/testing) and PostgreSQL (production).
Includes connection pooling, auto-reconnection pre-ping, and legacy postgres:// normalization.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

# Normalize legacy postgres:// URI schema if provided by PaaS providers (e.g. Render, Railway, Heroku)
raw_db_url = settings.DATABASE_URL
if raw_db_url.startswith("postgres://"):
    database_url = raw_db_url.replace("postgres://", "postgresql://", 1)
else:
    database_url = raw_db_url

# Configure engine arguments based on database engine
engine_kwargs = {"echo": False}

if database_url.startswith("sqlite"):
    # SQLite multithreading support for FastAPI
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Production PostgreSQL / MySQL connection pooling & resilience
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20
    engine_kwargs["pool_pre_ping"] = True  # Verifies connection liveness before execution
    engine_kwargs["pool_recycle"] = 3600    # Recycle stale pooled connections every hour

engine = create_engine(database_url, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency for obtaining a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


