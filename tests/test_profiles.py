"""Local profiles: the stand-in for a directory. People create their own profile on
first open, sign in with a password, and Triage manages everyone from the roster."""

from __future__ import annotations

import re
import uuid

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from situation_room import config as config_mod
from situation_room import domain, passwords
from situation_room.cli import alembic_config
from situation_room.db import make_engine
from situation_room.domain import DomainError
from situation_room.models import AuditLog, Person, Role

from .conftest import _url, csrf_from, make_env, sign_in

PASSWORD = "correct horse battery"


def create_profile(client: TestClient, name: str, email: str, password: str = PASSWORD, **extra: str):
    token = csrf_from(client.get("/welcome").text)
    data = {"csrf_token": token, "display_name": name, "email": email, "password": password,
            "password_again": password, "next": "/"}
    data.update(extra)
    return client.post("/welcome", data=data, follow_redirects=False)


def password_sign_in(client: TestClient, email: str, password: str = PASSWORD):
    token = csrf_from(client.get("/signin").text)
    return client.post("/signin", data={"csrf_token": token, "email": email, "password": password, "next": "/"},
                       follow_redirects=False)


def person(db, email: str) -> Person:
    db.expire_all()
    return db.scalar(select(Person).where(Person.email == email))


@pytest.fixture
def other_client(local_app):
    """A second browser."""
    with TestClient(local_app) as c:
        yield c


# First open ---------------------------------------------------------------------------

def test_a_first_visit_is_sent_to_create_a_profile(local_client):
    r = local_client.get("/board", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome?next=/board"
    page = local_client.get("/welcome?next=/board")
    assert page.status_code == 200 and "Create your profile" in page.text


def test_a_browser_that_has_signed_in_before_is_sent_to_sign_in(local_client):
    create_profile(local_client, "Pat Admin", "pat@corp.test")
    token = csrf_from(local_client.get("/board").text)
    local_client.post("/signout", data={"csrf_token": token})
    r = local_client.get("/board", follow_redirects=False)
    assert r.headers["location"] == "/signin?next=/board"


def test_the_first_profile_is_the_admin_and_later_ones_are_managers(db, local_client, other_client):
    assert "this profile will be the admin" in local_client.get("/welcome").text
    r = create_profile(local_client, "Pat Admin", "Pat@Corp.test")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert person(db, "pat@corp.test").role == Role.TRIAGE
    assert local_client.get("/roster").status_code == 200

    assert "this profile will be the admin" not in other_client.get("/welcome").text
    create_profile(other_client, "Mia Manager", "mia@corp.test")
    mia = person(db, "mia@corp.test")
    assert mia.role == Role.MANAGER and mia.login_id == "mia@corp.test"
    assert other_client.get("/", follow_redirects=False).headers["location"] == "/mine"
    assert other_client.get("/roster").status_code == 403


def test_a_password_is_stored_hashed(db, local_client):
    create_profile(local_client, "Pat Admin", "pat@corp.test")
    stored = person(db, "pat@corp.test").password_hash
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert passwords.verify_password(PASSWORD, stored) and not passwords.verify_password("something else", stored)


@pytest.mark.parametrize(("change", "message"), [
    ({"password": "short"}, "at least 10 characters"),
    ({"password_again": "a different one"}, "do not match"),
    ({"display_name": "   "}, "Name is required"),
    ({"email": "not-an-address"}, "mailbox address"),
])
def test_a_refused_profile_keeps_what_was_typed(db, local_client, change, message):
    token = csrf_from(local_client.get("/welcome").text)
    data = {"csrf_token": token, "display_name": "Pat Admin", "email": "pat@corp.test", "password": PASSWORD,
            "password_again": PASSWORD}
    if "password" in change:
        data["password_again"] = change["password"]
    data.update(change)
    r = local_client.post("/welcome", data=data)
    assert r.status_code == 400 and message in r.text
    if "display_name" not in change:
        assert 'value="Pat Admin"' in r.text
    assert PASSWORD not in r.text
    assert db.scalar(select(func.count()).select_from(Person)) == 0


def test_one_email_gets_one_profile(db, local_client, other_client):
    create_profile(local_client, "Pat Admin", "pat@corp.test")
    r = create_profile(other_client, "Someone Else", "PAT@corp.test", password="another password")
    assert r.status_code == 409 and "already a profile for that email" in r.text
    assert db.scalar(select(func.count()).select_from(Person)) == 1
    assert password_sign_in(other_client, "pat@corp.test", "another password").status_code == 401


# Signing in ---------------------------------------------------------------------------

def test_sign_in_with_email_and_password(local_client, other_client):
    create_profile(local_client, "Pat Admin", "pat@corp.test")
    wrong = password_sign_in(other_client, "pat@corp.test", "not the password")
    assert wrong.status_code == 401 and "do not match" in wrong.text and 'value="pat@corp.test"' in wrong.text
    assert password_sign_in(other_client, "nobody@corp.test").status_code == 401
    assert other_client.get("/board", follow_redirects=False).status_code == 303

    r = password_sign_in(other_client, " PAT@corp.test ")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert other_client.get("/board").status_code == 200


def test_repeated_wrong_passwords_make_that_email_wait(local_client, other_client):
    create_profile(local_client, "Pat Admin", "pat@corp.test")
    for _ in range(8):
        assert password_sign_in(other_client, "pat@corp.test", "guess").status_code == 401
    r = password_sign_in(other_client, "pat@corp.test")
    assert r.status_code == 429 and "Too many tries" in r.text


def test_the_no_password_picker_is_off_outside_dev_mode(db, clock, local_client, people):
    html = local_client.get("/signin").text
    assert "Development sign-in" not in html and people.triage.display_name not in html
    token = csrf_from(html)
    r = local_client.post("/signin/dev", data={"csrf_token": token, "person_id": people.triage.id})
    assert r.status_code == 404 and "SIGNIN_MODE" in r.text
    # A roster entry with no password cannot be signed into by guessing an empty one.
    assert password_sign_in(local_client, people.triage.email, "").status_code == 401


def test_local_mode_runs_in_production():
    cfg = config_mod.load(make_env("postgresql://x@/db", SR_AUTH_MODE="local", SR_ENVIRONMENT="production",
                                   SR_PUBLIC_URL="https://board.example.com"))
    assert cfg.has_profiles and not cfg.is_dev_signin


# Triage manages everyone ---------------------------------------------------------------

@pytest.fixture
def admin(db, local_client):
    """A signed-in Triage browser. Returns its csrf token."""
    create_profile(local_client, "Pat Admin", "pat@corp.test")
    return csrf_from(local_client.get("/roster").text)


@pytest.fixture
def mia(db, admin, other_client):
    create_profile(other_client, "Mia Manager", "mia@corp.test")
    return person(db, "mia@corp.test")


def test_the_roster_lists_everyone_with_a_manage_link(local_client, admin, mia):
    html = local_client.get("/roster").text
    assert "Mia Manager" in html and f'href="/roster/{mia.id}"' in html and "Sign-in value" not in html
    assert local_client.get(f"/roster/{mia.id}").status_code == 200
    assert local_client.get("/roster/99999").status_code == 404


def test_triage_changes_a_role_name_and_mailbox(db, local_client, other_client, admin, mia):
    r = local_client.post(f"/roster/{mia.id}", data={"csrf_token": admin, "display_name": "Mia Lead",
                                                      "email": "Mia.Lead@corp.test", "role": "lead"},
                          follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/roster/{mia.id}"
    changed = person(db, "mia.lead@corp.test")
    assert (changed.display_name, changed.role, changed.login_id) == ("Mia Lead", Role.LEAD, "mia.lead@corp.test")
    # Her open session picks the new role up, and she now signs in with the new address.
    assert other_client.get("/firedrills/new").status_code == 200
    assert password_sign_in(other_client, "mia.lead@corp.test").status_code == 303


def test_a_mailbox_cannot_be_changed_to_one_in_use(local_client, admin, mia):
    r = local_client.post(f"/roster/{mia.id}", data={"csrf_token": admin, "display_name": "Mia Manager",
                                                      "email": "pat@corp.test", "role": "manager"})
    assert r.status_code == 409 and "already has that email" in r.text


def test_the_last_admin_cannot_be_demoted_deactivated_or_removed(db, local_client, admin, mia):
    pat = person(db, "pat@corp.test")
    r = local_client.post(f"/roster/{pat.id}", data={"csrf_token": admin, "display_name": "Pat Admin",
                                                      "email": "pat@corp.test", "role": "manager"})
    assert r.status_code == 409 and "last active Triage" in r.text
    r = local_client.post(f"/roster/{pat.id}/deactivate", data={"csrf_token": admin})
    assert r.status_code == 409 and "last active Triage" in r.text
    r = local_client.post(f"/roster/{pat.id}/remove", data={"csrf_token": admin})
    assert r.status_code == 409 and "your own profile" in r.text
    assert person(db, "pat@corp.test").role == Role.TRIAGE and person(db, "pat@corp.test").active


def test_deactivating_signs_someone_out_and_reactivating_lets_them_back(db, local_client, other_client, admin, mia):
    assert other_client.get("/mine").status_code == 200
    local_client.post(f"/roster/{mia.id}/deactivate", data={"csrf_token": admin})
    assert other_client.get("/mine").status_code == 403
    assert password_sign_in(other_client, "mia@corp.test").status_code == 401

    local_client.post(f"/roster/{mia.id}/reactivate", data={"csrf_token": admin})
    assert password_sign_in(other_client, "mia@corp.test").status_code == 303
    assert other_client.get("/mine").status_code == 200


def test_a_signup_with_no_history_can_be_removed(db, local_client, other_client, admin, mia):
    r = local_client.post(f"/roster/{mia.id}/remove", data={"csrf_token": admin}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/roster"
    assert person(db, "mia@corp.test") is None
    assert other_client.get("/mine", follow_redirects=False).status_code == 403
    # The address is free again.
    assert create_profile(other_client, "Mia Manager", "mia@corp.test").status_code == 303


def test_someone_with_history_is_not_removed(db, clock, local_client, admin, mia):
    pat = person(db, "pat@corp.test")
    domain.create_firedrill(db, clock, pat, title="Rotate passwords", short_description="Rotate them.", due_date=None,
                            requested_by=None, source=None, ticket_text="", owner_ids=[mia.id])
    db.commit()
    r = local_client.post(f"/roster/{mia.id}/remove", data={"csrf_token": admin})
    assert r.status_code == 409 and "Deactivate them instead" in r.text
    assert person(db, "mia@corp.test") is not None


def test_only_triage_manages_people(db, local_client, other_client, admin, mia):
    pat = person(db, "pat@corp.test")
    token = csrf_from(other_client.get("/mine").text)
    assert other_client.get(f"/roster/{pat.id}").status_code == 403
    for action, data in (("", {"display_name": "Mia", "email": "mia@corp.test", "role": "triage"}),
                         ("/deactivate", {}), ("/reactivate", {}), ("/remove", {}), ("/password-link", {})):
        target = mia.id if action == "" else pat.id
        r = other_client.post(f"/roster/{target}{action}", data={"csrf_token": token, **data})
        assert r.status_code == 403 and "NOT_ALLOWED" in r.text, action
    assert person(db, "mia@corp.test").role == Role.MANAGER and person(db, "pat@corp.test").active


# Password links -------------------------------------------------------------------------

def link_from(html: str) -> str:
    match = re.search(r'value="http://testserver(/password/[^"]+)"', html)
    assert match, "no password link on the page"
    return match.group(1)


def test_a_password_link_replaces_a_forgotten_password_once(db, local_client, other_client, local_app, admin, mia):
    page = local_client.post(f"/roster/{mia.id}/password-link", data={"csrf_token": admin})
    assert page.status_code == 200 and "mailto:mia@corp.test" in page.text
    link = link_from(page.text)
    assert other_client.get("/mine").status_code == 200  # the old password and session still work

    with TestClient(local_app) as fresh:
        form = fresh.get(link)
        assert form.status_code == 200 and "Mia Manager" in form.text
        token = csrf_from(form.text)
        short = fresh.post(link, data={"csrf_token": token, "password": "short", "password_again": "short"})
        assert short.status_code == 400 and "at least 10" in short.text
        r = fresh.post(link, data={"csrf_token": token, "password": "a brand new password",
                                   "password_again": "a brand new password"}, follow_redirects=False)
        assert r.status_code == 303 and fresh.get("/mine").status_code == 200

        # Used once: the link is spent, the old password is gone, and the old session is signed out.
        assert fresh.get(link).status_code == 410
        assert other_client.get("/mine", follow_redirects=False).status_code == 303
        assert password_sign_in(other_client, "mia@corp.test").status_code == 401
        assert password_sign_in(other_client, "mia@corp.test", "a brand new password").status_code == 303
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "person.password_link")) == 1


def test_someone_added_by_triage_gets_in_with_a_password_link(db, local_client, other_client, admin):
    r = local_client.post("/roster", data={"csrf_token": admin, "display_name": "Lee Lead", "email": "lee@corp.test",
                                           "role": "lead"}, follow_redirects=False)
    lee = person(db, "lee@corp.test")
    assert r.headers["location"] == f"/roster/{lee.id}" and lee.password_hash is None and lee.login_id == "lee@corp.test"
    assert "No password yet" in local_client.get(f"/roster/{lee.id}").text
    # Nobody can take the entry over by signing up with its address.
    assert create_profile(other_client, "Not Lee", "lee@corp.test").status_code == 409

    link = link_from(local_client.post(f"/roster/{lee.id}/password-link", data={"csrf_token": admin}).text)
    token = csrf_from(other_client.get(link).text)
    other_client.post(link, data={"csrf_token": token, "password": PASSWORD, "password_again": PASSWORD})
    assert other_client.get("/firedrills/new").status_code == 200


def test_a_bad_or_tampered_link_is_refused(local_client, admin, mia, local_cfg):
    assert local_client.get("/password/not-a-token").status_code == 410
    other_key = passwords.link_token("x" * 40, mia.id, mia.password_hash)
    assert local_client.get(f"/password/{other_key}").status_code == 410
    good = passwords.link_token(local_cfg.secret_key, mia.id, mia.password_hash)
    assert local_client.get(f"/password/{good}").status_code == 200


def test_no_link_for_someone_inactive(db, local_client, admin, mia, local_cfg):
    link = passwords.link_token(local_cfg.secret_key, mia.id, mia.password_hash)
    local_client.post(f"/roster/{mia.id}/deactivate", data={"csrf_token": admin})
    r = local_client.post(f"/roster/{mia.id}/password-link", data={"csrf_token": admin})
    assert r.status_code == 409 and "Reactivate them first" in r.text
    assert local_client.get(f"/password/{link}").status_code == 410


# Domain rules ---------------------------------------------------------------------------

def test_registering_never_needs_an_actor_but_managing_does(db, clock, people):
    late = domain.register_profile(db, clock, display_name="Late Comer", email="late@corp.test", password=PASSWORD)
    assert late.role == Role.MANAGER  # the fixture already has a Triage person
    with pytest.raises(DomainError) as err:
        domain.update_person(db, clock, late, people.ann.id, display_name="Ann", email=people.ann.email, role="triage")
    assert err.value.code == "NOT_ALLOWED"


def test_a_new_admin_is_made_when_the_only_one_is_inactive(db, clock, people):
    people.triage.active = False
    db.commit()
    assert domain.register_profile(db, clock, display_name="New Admin", email="new@corp.test", password=PASSWORD).role == Role.TRIAGE


def test_dev_mode_still_offers_the_picker_and_profiles(client, people):
    html = client.get("/signin").text
    assert "Development sign-in" in html and 'action="/signin"' in html
    sign_in(client, people.ann)
    assert client.get("/mine").status_code == 200


# The upgrade from a board that had sample data ---------------------------------------------

def test_the_upgrade_removes_sample_data_and_keeps_real_work(pg):
    name = "m_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        conn.execute(f"create database {name}")
    url = _url(pg, name)
    engine = make_engine(url)
    try:
        command.upgrade(alembic_config(url), "0001")
        with engine.begin() as c:
            def add_person(email, role):
                return c.execute(text(
                    "insert into person (display_name, email, login_id, role, active, created_at) "
                    "values (:e, :e, :e, :r, true, now()) returning id"), {"e": email, "r": role}).scalar_one()

            def add_firedrill(title, creator, owner):
                f = c.execute(text(
                    "insert into firedrill (public_id, title, short_description, state, ticket_refs, auto_ask, "
                    "created_by_id, created_at) values (gen_random_uuid(), :t, '', 'open', '', true, :c, now()) "
                    "returning id"), {"t": title, "c": creator}).scalar_one()
                a = c.execute(text(
                    "insert into assignment (public_id, firedrill_id, owner_id, status, created_at) "
                    "values (gen_random_uuid(), :f, :o, 'new', now()) returning id"), {"f": f, "o": owner}).scalar_one()
                u = c.execute(text(
                    "insert into update (assignment_id, author_id, status, text, source, created_at) "
                    "values (:a, :o, 'on_track', 'x', 'form', now()) returning id"), {"a": a, "o": owner}).scalar_one()
                c.execute(text(
                    "insert into update_request (assignment_id, asked_by_id, origin, requested_at, answered_by_update_id) "
                    "values (:a, :c, 'manual', now(), :u)"), {"a": a, "c": creator, "u": u})
                c.execute(text(
                    "insert into audit_log (actor_id, action, object_type, object_id, detail, at) "
                    "values (:c, 'firedrill.create', 'firedrill', (select public_id::text from firedrill where id = :f), "
                    "'{}', now())"), {"c": creator, "f": f})
                return f

            sam = add_person("sam.rivera@example.com", "triage")
            robin = add_person("robin.ortiz@example.com", "lead")
            alex = add_person("alex.kim@example.com", "manager")
            jordan = add_person("jordan.lee@example.com", "manager")
            real = add_person("real.person@corp.test", "manager")
            c.execute(text("insert into usage_event (person_id, day, kind) values (:p, current_date, 'board_view')"), {"p": sam})
            c.execute(text("insert into audit_log (actor_id, action, object_type, object_id, detail, at) "
                           "values (:s, 'person.add', 'person', :r, '{}', now())"), {"s": sam, "r": str(real)})
            add_firedrill("Rotate service account passwords", robin, jordan)
            add_firedrill("Functional tests for every application", robin, alex)
            # Real work entered while signed in as a sample person, for a sample owner and a real one.
            add_firedrill("Something real", robin, real)
            add_firedrill("Rotate service account passwords", real, alex)

        command.upgrade(alembic_config(url), "head")
        with engine.connect() as c:
            titles = sorted(c.execute(text("select title from firedrill")).scalars())
            assert titles == ["Rotate service account passwords", "Something real"]
            left = dict(c.execute(text("select email, active from person")).all())
            # Sam and Jordan are gone. Robin and Alex still have real work pointing at them: kept, switched off.
            assert left == {"real.person@corp.test": True, "robin.ortiz@example.com": False, "alex.kim@example.com": False}
            assert c.execute(text("select count(*) from assignment")).scalar_one() == 2
            assert c.execute(text("select count(*) from update")).scalar_one() == 2
            assert c.execute(text("select count(*) from update_request")).scalar_one() == 2
            assert c.execute(text("select count(*) from usage_event")).scalar_one() == 0
            kept_audit = c.execute(text("select action, actor_id from audit_log order by id")).all()
            assert [(a, actor is None) for a, actor in kept_audit] == [
                ("person.add", True), ("firedrill.create", False), ("firedrill.create", False)]
            assert c.execute(text("select count(*) from person where password_hash is not null")).scalar_one() == 0
    finally:
        engine.dispose()
        with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
            conn.execute(f"drop database if exists {name} with (force)")
