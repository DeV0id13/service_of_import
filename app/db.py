from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

READINESS_TIMEOUT_SECONDS = 3
settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
readiness_engine = create_engine(
    settings.database_url,
    poolclass=NullPool,
    connect_args={
        "connect_timeout": READINESS_TIMEOUT_SECONDS,
        "options": f"-c statement_timeout={READINESS_TIMEOUT_SECONDS * 1_000}",
    },
)

SessionFactory = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


@contextmanager
def transaction() -> Iterator[Session]:
    """Provide a Session with one explicit transaction boundary."""

    with SessionFactory() as session, session.begin():
        yield session
