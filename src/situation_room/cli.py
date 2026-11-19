"""The `sr` command.

    uv run sr dev              local Postgres, migrations, app on :8000
    uv run sr migrate          apply migrations (and add the first Triage person if configured)
    uv run sr password-link EMAIL   print a one-time link that person uses to choose a password
    uv run sr serve            run the app from SR_* configuration
    uv run sr check-config     check SR_* configuration and name the first problem
    uv run sr db start|stop|url
    uv run sr makemigration -m "message"
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine

from . import config as config_mod
from .clock import Clock
from .db import make_sessionmaker

PACKAGE_DIR = Path(__file__).parent
DATA_DIR = Path.cwd() / ".data"
LOCAL_DB = "situation_room"


def fail(code: str, message: str, exit_code: int = 1) -> None:
    print(f"sr: {code}: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


# Local database ---------------------------------------------------------------------

def _pgserver(keep_running: bool):
    try:
        import pgserver
    except ImportError:
        fail("LOCAL_DB_UNAVAILABLE", "the embedded Postgres is a dev dependency; run `uv sync` first")
    DATA_DIR.mkdir(exist_ok=True)
    return pgserver.get_server(DATA_DIR / "pg", cleanup_mode=None if keep_running else "stop")


def local_database_url(keep_running: bool = False) -> str:
    server = _pgserver(keep_running)
    admin = server.get_uri()
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = %s", (LOCAL_DB,)).fetchone()
        if not exists:
            conn.execute(f'create database "{LOCAL_DB}"')
    return server.get_uri(LOCAL_DB)


def _database_url() -> str:
    url = os.environ.get("SR_DATABASE_URL", "").strip()
    if url:
        return config_mod.sqlalchemy_url(url)
    if os.environ.get("SR_ENVIRONMENT") == "production":
        fail("CONFIG_MISSING", "SR_DATABASE_URL is required")
    url = config_mod.sqlalchemy_url(local_database_url())
    os.environ["SR_DATABASE_URL"] = url
    return url


# Commands -----------------------------------------------------------------------------

def alembic_config(url: str) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str) -> None:
    try:
        command.upgrade(alembic_config(url), "head")
    except Exception as exc:  # noqa: BLE001 - report the class, not values that may hold secrets
        fail("MIGRATE_FAILED", f"{type(exc).__name__}; nothing was changed")
    login = os.environ.get("SR_FIRST_TRIAGE_LOGIN", "").strip()
    email = os.environ.get("SR_FIRST_TRIAGE_EMAIL", "").strip()
    if login or email:
        if not email:
            fail("CONFIG_MISSING", "SR_FIRST_TRIAGE_EMAIL must be set when SR_FIRST_TRIAGE_LOGIN is")
        # A local profile signs in with its email, so the sign-in value defaults to it.
        login = login or email
        from . import domain
        tz = _timezone()
        engine = create_engine(url, hide_parameters=True)
        with make_sessionmaker(engine)() as s:
            domain.ensure_first_triage(
                s, Clock(tz), login_id=login, email=email,
                display_name=os.environ.get("SR_FIRST_TRIAGE_NAME", "").strip() or email,
            )
            s.commit()
        engine.dispose()
    print("sr: migrations applied")


def _timezone():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = os.environ.get("SR_TIMEZONE", "").strip()
    if not name:
        fail("CONFIG_MISSING", "SR_TIMEZONE is required")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        fail("CONFIG_INVALID", "SR_TIMEZONE is not a known time zone name")


def password_link(url: str, email: str) -> None:
    """The way back in when nobody can make a link from the roster, such as a locked-out Triage person."""
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        fail(exc.code, f"{exc.key} {exc.problem}", exit_code=2)
    if not cfg.has_profiles:
        fail("SIGNIN_MODE", "password links are only for local profiles (SR_AUTH_MODE=local)")
    from sqlalchemy import select

    from . import passwords
    from .models import Person
    engine = create_engine(url, hide_parameters=True)
    with make_sessionmaker(engine)() as s:
        person = s.scalar(select(Person).where(Person.email == email.strip().lower()))
        if person is None or not person.active:
            fail("NOT_FOUND", "no active person on the roster has that email")
        token = passwords.link_token(cfg.secret_key, person.id, person.password_hash)
    engine.dispose()
    print(f"{cfg.public_url}/password/{token}")
    print(f"sr: works once, for {passwords.LINK_MAX_AGE // 86400} days", file=sys.stderr)


def _local_timezone() -> str:
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def dev(port: int) -> None:
    os.environ.setdefault("SR_AUTH_MODE", "local")
    os.environ.setdefault("SR_ENVIRONMENT", "development")
    os.environ.setdefault("SR_PUBLIC_URL", f"http://localhost:{port}")
    os.environ.setdefault("SR_TIMEZONE", _local_timezone())
    os.environ.setdefault("SR_SECRET_KEY", secrets.token_urlsafe(48))
    url = _database_url()
    migrate(url)
    check_config()
    import uvicorn
    print(f"sr: open {os.environ['SR_PUBLIC_URL']}")
    uvicorn.run("situation_room.web.app:app_from_env", factory=True, host="127.0.0.1", port=port,
                reload=True, reload_dirs=[str(PACKAGE_DIR)],
                # A CSS edit must restart the app so the stylesheet's ?v= hash follows it;
                # templates need no restart, Jinja re-reads them on change.
                reload_includes=["*.css"], log_level="info")


def serve(host: str, port: int) -> None:
    check_config()
    import uvicorn
    uvicorn.run("situation_room.web.app:app_from_env", factory=True, host=host, port=port, proxy_headers=True)


def check_config() -> None:
    try:
        config_mod.load()
    except config_mod.ConfigError as exc:
        fail(exc.code, f"{exc.key} {exc.problem}", exit_code=2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sr", description="Situation Room")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_dev = sub.add_parser("dev", help="run locally with an embedded Postgres")
    p_dev.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="apply database migrations")
    p_link = sub.add_parser("password-link", help="print a one-time link a person uses to choose a password")
    p_link.add_argument("email")
    p_serve = sub.add_parser("serve", help="run the app from SR_* configuration")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("check-config", help="check SR_* configuration")
    p_db = sub.add_parser("db", help="the local embedded Postgres")
    p_db.add_argument("action", choices=["start", "stop", "url"])
    p_mm = sub.add_parser("makemigration", help="autogenerate a migration from the models")
    p_mm.add_argument("-m", "--message", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "dev":
        dev(args.port)
    elif args.cmd == "migrate":
        migrate(_database_url())
    elif args.cmd == "password-link":
        password_link(_database_url(), args.email)
    elif args.cmd == "serve":
        serve(args.host, args.port)
    elif args.cmd == "check-config":
        check_config()
        print("sr: configuration OK")
    elif args.cmd == "db":
        if args.action == "start":
            print(local_database_url(keep_running=True))
        elif args.action == "url":
            print(local_database_url(keep_running=True))
        else:
            _pgserver(keep_running=False).cleanup()
            print("sr: local database stopped")
    elif args.cmd == "makemigration":
        command.revision(alembic_config(_database_url()), message=args.message, autogenerate=True)


if __name__ == "__main__":
    main()
