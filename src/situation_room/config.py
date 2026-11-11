"""Deploy-time configuration, read from SR_* environment variables and checked at start.

Errors name the key and the problem, never the value, so a bad secret never
lands in a log.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUTH_MODES = ("oidc", "proxy", "dev")
ENVIRONMENTS = ("development", "production")
BUILT_AUTH_MODES = ("dev",)


class ConfigError(Exception):
    """A configuration key is missing or invalid."""

    def __init__(self, code: str, key: str, problem: str):
        self.code = code
        self.key = key
        self.problem = problem
        super().__init__(f"{code}: {key} {problem}")


@dataclass(frozen=True)
class Config:
    auth_mode: str
    environment: str
    database_url: str
    secret_key: str
    public_url: str
    timezone: ZoneInfo
    stale_after: int
    clock_override: datetime | None
    first_triage_login: str | None
    first_triage_email: str | None
    first_triage_name: str | None

    @property
    def is_dev_signin(self) -> bool:
        return self.auth_mode == "dev"

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"


def sqlalchemy_url(url: str) -> str:
    """Use the psycopg 3 driver for a plain postgresql:// address."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    def get(key: str, required: bool = True) -> str | None:
        value = (env.get(key) or "").strip()
        if required and not value:
            raise ConfigError("CONFIG_MISSING", key, "is required")
        return value or None

    auth_mode = get("SR_AUTH_MODE")
    if auth_mode not in AUTH_MODES:
        raise ConfigError("CONFIG_INVALID", "SR_AUTH_MODE", f"must be one of: {', '.join(AUTH_MODES)}")

    environment = get("SR_ENVIRONMENT")
    if environment not in ENVIRONMENTS:
        raise ConfigError("CONFIG_INVALID", "SR_ENVIRONMENT", f"must be one of: {', '.join(ENVIRONMENTS)}")
    if auth_mode == "dev" and environment != "development":
        raise ConfigError(
            "CONFIG_INVALID", "SR_AUTH_MODE",
            "is dev, which is refused unless SR_ENVIRONMENT is development",
        )

    tz_name = get("SR_TIMEZONE")
    try:
        timezone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("CONFIG_INVALID", "SR_TIMEZONE", "is not a known time zone name") from None

    clock_raw = get("SR_CLOCK_OVERRIDE", required=False)
    clock_override = None
    if clock_raw:
        if auth_mode != "dev":
            raise ConfigError(
                "CONFIG_INVALID", "SR_CLOCK_OVERRIDE",
                "is refused outside dev sign-in mode (a wrong clock would purge and ask early)",
            )
        try:
            clock_override = datetime.fromisoformat(clock_raw)
        except ValueError:
            raise ConfigError("CONFIG_INVALID", "SR_CLOCK_OVERRIDE", "is not an ISO 8601 date and time") from None
        if clock_override.tzinfo is None:
            clock_override = clock_override.replace(tzinfo=timezone)

    if auth_mode not in BUILT_AUTH_MODES:
        raise ConfigError(
            "CONFIG_UNSUPPORTED", "SR_AUTH_MODE",
            f"is {auth_mode}, which is not built yet; this version runs with dev sign-in only",
        )

    public_url = get("SR_PUBLIC_URL").rstrip("/")
    parts = urlsplit(public_url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query:
        raise ConfigError("CONFIG_INVALID", "SR_PUBLIC_URL", "must be an http or https address with no path")
    if environment == "production" and parts.scheme != "https":
        raise ConfigError("CONFIG_INVALID", "SR_PUBLIC_URL", "must use https in production")

    secret_key = get("SR_SECRET_KEY")
    if len(secret_key) < 32:
        raise ConfigError("CONFIG_INVALID", "SR_SECRET_KEY", "must be at least 32 characters")

    stale_raw = get("SR_STALE_AFTER", required=False) or "3"
    try:
        stale_after = int(stale_raw)
    except ValueError:
        raise ConfigError("CONFIG_INVALID", "SR_STALE_AFTER", "must be a whole number") from None
    if stale_after < 1:
        raise ConfigError("CONFIG_INVALID", "SR_STALE_AFTER", "must be at least 1")

    return Config(
        auth_mode=auth_mode,
        environment=environment,
        database_url=sqlalchemy_url(get("SR_DATABASE_URL")),
        secret_key=secret_key,
        public_url=public_url,
        timezone=timezone,
        stale_after=stale_after,
        clock_override=clock_override,
        first_triage_login=get("SR_FIRST_TRIAGE_LOGIN", required=False),
        first_triage_email=get("SR_FIRST_TRIAGE_EMAIL", required=False),
        first_triage_name=get("SR_FIRST_TRIAGE_NAME", required=False),
    )
