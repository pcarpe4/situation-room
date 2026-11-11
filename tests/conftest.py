"""Tests run on a real Postgres (embedded, started once per run) and the app's fixed clock.

Each test gets its own database copied from a migrated template, so tests
cannot see each other's rows.
"""

from __future__ import annotations

import re
import tempfile
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient

from situation_room import config as config_mod
from situation_room import domain
from situation_room.cli import alembic_config
from situation_room.clock import FixedClock
from situation_room.db import make_engine, make_sessionmaker
from situation_room.models import Person, Role
from situation_room.web.app import create_app

TZ = ZoneInfo("America/Chicago")
# A Wednesday, mid-morning.
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)
TEMPLATE_DB = "sr_template"


@pytest.fixture(scope="session")
def pg():
    import pgserver
    server = pgserver.get_server(tempfile.mkdtemp(prefix="srpg"), cleanup_mode="stop")
    yield server
    server.cleanup()


def _socket_dir(server) -> str:
    return parse_qs(urlsplit(server.get_uri()).query)["host"][0]


def _url(server, name: str) -> str:
    return f"postgresql+psycopg://postgres@/{name}?host={_socket_dir(server)}"


@pytest.fixture(scope="session")
def template_db(pg):
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        conn.execute(f"drop database if exists {TEMPLATE_DB}")
        conn.execute(f"create database {TEMPLATE_DB}")
    command.upgrade(alembic_config(_url(pg, TEMPLATE_DB)), "head")
    return TEMPLATE_DB


@pytest.fixture
def db_url(pg, template_db):
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        conn.execute(f"create database {name} template {template_db}")
    yield _url(pg, name)
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        conn.execute(f"drop database if exists {name} with (force)")


@pytest.fixture
def engine(db_url):
    eng = make_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def sm(engine):
    return make_sessionmaker(engine)


@pytest.fixture
def db(sm):
    with sm() as session:
        yield session


@pytest.fixture
def clock():
    return FixedClock(TZ, NOW)


def make_env(db_url: str, **overrides: str) -> dict[str, str]:
    env = {
        "SR_AUTH_MODE": "dev",
        "SR_ENVIRONMENT": "development",
        "SR_DATABASE_URL": db_url,
        "SR_SECRET_KEY": "k" * 40,
        "SR_PUBLIC_URL": "http://testserver",
        "SR_TIMEZONE": "America/Chicago",
    }
    env.update(overrides)
    return env


@pytest.fixture
def cfg(db_url):
    return config_mod.load(make_env(db_url))


@pytest.fixture
def app(cfg, clock, engine):
    return create_app(cfg, clock=clock, engine=engine)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


class People:
    def __init__(self, db, clock):
        def add(name: str, role: Role) -> Person:
            login = name.lower().replace(" ", ".")
            return domain.add_person(db, clock, None, display_name=name, email=f"{login}@example.com",
                                     login_id=login, role=role)
        self.triage = add("Tara Triage", Role.TRIAGE)
        self.lead = add("Leo Lead", Role.LEAD)
        self.ann = add("Ann Manager", Role.MANAGER)
        self.bob = add("Bob Manager", Role.MANAGER)
        db.commit()


@pytest.fixture
def people(db, clock):
    return People(db, clock)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf token on the page"
    return match.group(1)


def sign_in(client: TestClient, person: Person) -> str:
    """Sign in through the dev page. Returns the session's csrf token."""
    token = csrf_from(client.get("/signin").text)
    response = client.post("/signin/dev", data={"person_id": person.id, "next": "/", "csrf_token": token},
                           follow_redirects=False)
    assert response.status_code == 303
    return csrf_from(client.get("/signin").text)
