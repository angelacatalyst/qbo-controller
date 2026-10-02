"""
tests/conftest.py
─────────────────
Shared test infrastructure for all Milestone 1A test files.

Provides a single in-memory SQLite database with a StaticPool connection
shared across ALL test files in this directory.  Each file that imported
its own engine and overrode app.dependency_overrides[get_db] separately
caused a conflict: the last-imported file's override shadowed the others.

Resolution: this conftest is loaded by pytest FIRST; it sets the override
once.  Individual test files import _Session from here and do NOT set the
override themselves.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

# ─── Single shared in-memory database ────────────────────────────────────────
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_Session = sessionmaker(bind=_engine)


def _override_get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


# Override ONCE here — test files must NOT repeat this line
app.dependency_overrides[get_db] = _override_get_db
