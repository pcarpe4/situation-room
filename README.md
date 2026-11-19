# Situation Room

A firedrill board: every open firedrill, its owners, each owner's status and latest update, and the asks that keep them moving.

Stack: Python 3.12, FastAPI, server-rendered Jinja templates (no JavaScript), SQLAlchemy 2, Alembic, Postgres.

## Run it locally

You need [uv](https://docs.astral.sh/uv/). No Docker and no installed Postgres: an embedded Postgres runs from `.data/`.

```sh
uv sync
uv run sr dev
```

Open http://localhost:8000 and create a profile. `sr dev` starts the database, applies migrations and serves with reload. The board starts empty: there is no sample data. Use `--port` to change the port.

```sh
uv run pytest        # tests run on a real Postgres too
```

CI runs the same `uv run pytest` on every pull request and on pushes to `main` (`.github/workflows/tests.yml`).

## People and sign-in

Until a directory (AD through OIDC or a sign-in proxy) is connected, the app keeps its own profiles. Set `SR_AUTH_MODE=local`.

- **First open.** Someone who has never signed in from their browser lands on "Create your profile": name, work email and a password. After that they get the normal sign-in page.
- **The admin.** The first profile created while there is no active Triage person becomes Triage, the role that manages everyone else. Open the app yourself straight after deploying. To name the admin up front instead, set `SR_FIRST_TRIAGE_EMAIL` before `sr migrate`, then run `sr password-link <email>` and open the link it prints.
- **Everyone else** joins as a Manager, the role that can do the least. Triage raises a role from the Roster page.
- **Managing users (Triage, Roster page).** Change a name, mailbox or role; deactivate and reactivate; remove a sign-up that has no history; add someone before they arrive; make a one-time password link for someone who forgot theirs or was added by hand.
- **Locked out.** `sr password-link <email>` prints a password link from the command line, for when no Triage person can sign in.

Passwords are hashed with scrypt. Emails are not verified, so treat this as a stopgap for a trusted internal network, not a replacement for the directory. `SR_AUTH_MODE=dev` adds a "pick who to be" list with no password, for development only.

## The one rule

Everything is an event on an assignment. A firedrill has one assignment per owner. Updates, asks and waits belong to an assignment. Only `situation_room/domain.py` changes firedrills, assignments, updates and asks. Pages, and later the jobs, call it and never write those tables themselves.

## Layout

| Path | What it holds |
|---|---|
| `src/situation_room/domain.py` | Every state change, with row locks (firedrill first, then assignment) and the rules |
| `src/situation_room/policy.py` | Who may do what, deny by default |
| `src/situation_room/board.py` | What the board shows, in a fixed number of queries |
| `src/situation_room/dates.py` | Stale, waiting, overdue and due-this-week arithmetic |
| `src/situation_room/models.py` | Tables |
| `src/situation_room/migrations/` | Alembic migrations |
| `src/situation_room/web/` | Routes, templates, styles, sessions and request-forgery checks |
| `src/situation_room/passwords.py` | Password hashing and one-time password links for local profiles |
| `src/situation_room/cli.py` | The `sr` command |

## Commands

| Command | Does |
|---|---|
| `sr dev` | Local run with embedded Postgres |
| `sr migrate` | Apply migrations; adds the first Triage person when `SR_FIRST_TRIAGE_*` is set |
| `sr password-link EMAIL` | Print a one-time link that person uses to choose a password |
| `sr serve` | Run from `SR_*` configuration |
| `sr check-config` | Check configuration and name the first problem |
| `sr db start\|stop\|url` | The local embedded Postgres |
| `sr makemigration -m "..."` | Autogenerate a migration from the models |

## Configuration

Read from the environment at start. A missing or invalid key stops the app with its name, never its value.

| Key | Required | Default | Notes |
|---|---|---|---|
| `SR_AUTH_MODE` | yes | | `local`, `dev`, `oidc` or `proxy`. `local` (profiles with a password) and `dev` are built so far |
| `SR_ENVIRONMENT` | yes | | `development` or `production`. `dev` sign-in refuses to start in production |
| `SR_DATABASE_URL` | yes | | `postgresql://...` |
| `SR_SECRET_KEY` | yes | | At least 32 characters. Signs the session cookie |
| `SR_PUBLIC_URL` | yes | | The app's address, used in every emailed link. https in production |
| `SR_TIMEZONE` | yes | | IANA name, for example `America/Chicago`. Decides day boundaries |
| `SR_STALE_AFTER` | no | `3` | Working days without an update before an assignment is stale |
| `SR_CLOCK_OVERRIDE` | no | | ISO date and time to shift the app's clock. Refused outside `dev` |
| `SR_FIRST_TRIAGE_LOGIN`, `SR_FIRST_TRIAGE_EMAIL`, `SR_FIRST_TRIAGE_NAME` | no | | First Triage person, created by `sr migrate`. The login defaults to the email |

`sr dev` fills in development values for all of these.

## What works now

- Local profiles: create your own on first open, sign in with email and password; roles: Lead, Manager, Triage
- Roster: edit people and roles, add, deactivate (refused while they own active work, or for the last Triage person), reactivate, remove, password links
- New firedrill by hand, with owners, due date, source and ticket numbers
- Board: counts that filter (open, stale, asked with no reply, waiting, due this week, overdue), stale first then by status then due date, one line per owner
- My assignments, the landing page for managers
- Request update: records the ask and opens a prefilled email with four status links; "Sent it?" Yes or Cancel ask
- Update form from a status link or the Update button; a repeated Send makes one update; an update answers every open ask; a Lead can post for an owner
- Firedrill page with the full update history
- Waiting until a date, set from the update page: the line shows the date and reason, does not go stale, and the wait answers open asks
- Light, dark or system theme from the header, remembered per browser
- Security headers with no inline script, request-forgery tokens on every form, an outage page (503) when the database is down

Not built yet: OIDC and proxy sign-in, paste intake with drafted fields, Needs you, Seen by, close and reopen, reassigning owners, the Health page and the mail features.
