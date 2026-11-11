from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    # hide_parameters keeps bound values (which can hold email text) out of error messages and logs.
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
