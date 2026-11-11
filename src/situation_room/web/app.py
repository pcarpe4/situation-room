"""Builds the web app. `app_from_env` is the entry point uvicorn uses."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware

from .. import config as config_mod
from ..board import STATUS_LABELS
from ..clock import Clock
from ..config import Config
from ..db import make_engine, make_sessionmaker
from ..domain import DomainError
from ..models import Status
from . import routes
from .render import render
from .security import Refused, SignInRequired, csrf_protect

HERE = Path(__file__).parent
SESSION_MAX_AGE = 12 * 60 * 60

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


def create_app(cfg: Config, *, clock: Clock | None = None, engine: Engine | None = None) -> FastAPI:
    clock = clock or Clock(cfg.timezone, cfg.clock_override)
    engine = engine or make_engine(cfg.database_url)

    app = FastAPI(title="Situation Room", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(csrf_protect)])
    app.state.cfg = cfg
    app.state.clock = clock
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.templates = _templates(cfg, clock)

    app.add_middleware(
        SessionMiddleware, secret_key=cfg.secret_key, session_cookie="sr_session",
        max_age=SESSION_MAX_AGE, same_site="lax", https_only=cfg.public_url.startswith("https://"),
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if cfg.public_url.startswith("https://"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(SignInRequired)
    async def _signin(request: Request, exc: SignInRequired):
        return RedirectResponse(f"/signin?next={quote(exc.next_path, safe='/')}", status_code=303)

    @app.exception_handler(Refused)
    async def _refused(request: Request, exc: Refused):
        return render(request, "error.html", status_code=exc.http_status, code=exc.code, message=exc.message)

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return render(request, "error.html", status_code=exc.http_status, code=exc.code, message=exc.message)

    @app.exception_handler(OperationalError)
    async def _outage(request: Request, exc: OperationalError):
        response = render(request, "outage.html", status_code=503, code="DB_UNAVAILABLE")
        response.headers["Retry-After"] = "30"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(routes.router)
    return app


def _templates(cfg: Config, clock: Clock) -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env

    def day(value: date | None) -> str:
        if value is None:
            return ""
        text = f"{value.strftime('%a %b')} {value.day}"
        return text if value.year == clock.today().year else f"{text}, {value.year}"

    def ago(value: datetime | None) -> str:
        if value is None:
            return ""
        seconds = (clock.now() - value).total_seconds()
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)} min ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)} h ago"
        days = int(seconds // 86400)
        if days < 7:
            return "1 day ago" if days == 1 else f"{days} days ago"
        return day(clock.local(value).date())

    def exact(value: datetime | None) -> str:
        if value is None:
            return ""
        local = clock.local(value)
        return f"{day(local.date())} {local.strftime('%H:%M')}"

    env.filters["day"] = day
    env.filters["ago"] = ago
    env.filters["exact"] = exact
    env.globals["status_label"] = lambda s: STATUS_LABELS[Status(s)]
    env.globals["Status"] = Status
    env.globals["today"] = clock.today
    return templates


def app_from_env() -> FastAPI:
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        print(f"situation-room: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    return create_app(cfg)
