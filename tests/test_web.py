import hashlib
import re
from datetime import datetime
from urllib.parse import unquote

import pytest

from sqlalchemy import func, select
from sqlalchemy.engine import create_engine
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from situation_room import domain, mailto, seed
from situation_room.models import Assignment, Firedrill, Update
from situation_room.web import routes
from situation_room.web.app import create_app

from .conftest import csrf_from, sign_in


def make(db, clock, people, title="Rotate passwords", owners=None):
    f = domain.create_firedrill(db, clock, people.lead, title=title, short_description="Rotate them.", due_date=None,
                                requested_by=None, source=None, ticket_text="",
                                owner_ids=[p.id for p in (owners or [people.ann])])
    db.commit()
    return f


def test_signed_out_requests_go_to_signin(client):
    r = client.get("/board", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin?next=/board"


def test_security_headers(client):
    r = client.get("/healthz")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "same-origin"


def test_every_post_route_refuses_a_missing_token(client, people):
    sign_in(client, people.triage)
    post_routes = [r for r in routes.router.routes if isinstance(r, APIRoute) and "POST" in r.methods]
    assert len(post_routes) >= 8
    for route in post_routes:
        path = re.sub(r"\{[^}]+\}", "1", route.path).replace("/a/1/", "/a/00000000-0000-0000-0000-000000000000/")
        r = client.post(path, data={"anything": "x"}, follow_redirects=False)
        assert r.status_code == 403, route.path
        assert "CSRF_TOKEN" in r.text


def test_a_post_from_another_origin_is_refused(client, people):
    token = sign_in(client, people.triage)
    r = client.post("/signout", data={"csrf_token": token}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and "CSRF_ORIGIN" in r.text


def test_managers_land_on_my_assignments_and_leads_on_the_board(client, people):
    sign_in(client, people.ann)
    assert client.get("/", follow_redirects=False).headers["location"] == "/mine"
    client.cookies.clear()
    sign_in(client, people.lead)
    assert client.get("/", follow_redirects=False).headers["location"] == "/board"


def test_text_is_escaped(db, clock, client, people):
    make(db, clock, people, title="<script>alert(1)</script>")
    sign_in(client, people.lead)
    html = client.get("/board").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_create_ask_and_answer_through_the_pages(db, clock, client, people):
    token = sign_in(client, people.lead)
    r = client.post("/firedrills/new", data={
        "csrf_token": token, "title": "Functional tests", "short_description": "Add tests to every pipeline.",
        "due_date": "2026-11-20", "source": "Platform", "requested_by": "", "ticket_text": "RITM0045121",
        "owner_ids": [str(people.ann.id)],
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/f/")

    a = db.scalar(select(Assignment))
    r = client.post(f"/a/{a.public_id}/ask", data={"csrf_token": token, "back": "/board"}, follow_redirects=False)
    assert r.status_code == 303
    board_html = client.get("/board").text
    assert "Did your ask to <strong>Ann Manager</strong> go out?" in board_html and "Open the email" in board_html
    link = re.search(r'href="(mailto:[^"]+)"', board_html).group(1)
    decoded = unquote(link.replace("&amp;", "&"))
    for status in ("on_track", "at_risk", "blocked", "done"):
        assert f"http://testserver/a/{a.public_id}/update?status={status}" in decoded

    client.cookies.clear()
    token = sign_in(client, people.ann)
    form = client.get(f"/a/{a.public_id}/update?status=at_risk").text
    assert "Not sent yet" in form
    assert 'value="at_risk" required checked' in form
    form_token = re.search(r'name="form_token" value="([^"]+)"', form).group(1)
    data = {"csrf_token": token, "form_token": form_token, "status": "at_risk", "text": "Two apps left."}
    sent = client.post(f"/a/{a.public_id}/update", data=data)
    assert sent.status_code == 200 and "Two apps left." in sent.text and "answered 1 open ask" in sent.text
    again = client.post(f"/a/{a.public_id}/update", data=data)
    assert again.status_code == 200
    assert db.scalar(select(func.count()).select_from(Update)) == 1


def test_opening_a_status_link_posts_nothing(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    sign_in(client, people.ann)
    assert client.get(f"/a/{a.public_id}/update?status=done").status_code == 200
    assert db.scalar(select(func.count()).select_from(Update)) == 0


def test_a_manager_opening_someone_elses_link_is_told_who_owns_it(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    sign_in(client, people.bob)
    r = client.get(f"/a/{a.public_id}/update")
    assert r.status_code == 403 and "Ann Manager" in r.text


def test_new_firedrill_errors_keep_what_was_typed(client, people):
    token = sign_in(client, people.lead)
    r = client.post("/firedrills/new", data={"csrf_token": token, "title": "Keep me", "short_description": "And me"})
    assert r.status_code == 400 and "Pick at least one owner" in r.text and 'value="Keep me"' in r.text


def test_roster_is_triage_only(client, people):
    sign_in(client, people.lead)
    assert client.get("/roster").status_code == 403


def test_the_seeded_board_renders_for_every_role(db, clock, client, people):
    assert seed.seed(db, clock)
    db.commit()
    for person in (people.lead, people.ann, people.triage):
        client.cookies.clear()
        sign_in(client, person)
        for path in ("/board", "/mine", "/calendar", "/board?filter=stale", "/board?filter=waiting", "/board?filter=overdue"):
            assert client.get(path).status_code == 200, (person.display_name, path)
    f = db.scalar(select(Firedrill).limit(1))
    assert client.get(f"/f/{f.public_id}").status_code == 200


def test_the_mailto_stays_under_budget_with_a_long_title():
    link = mailto.ask_mailto(public_url="https://board.example.com", to_email="someone@example.com",
                             title="T" * 200, due_date=None, assignment_public_id="0" * 36, asker_name="A Lead")
    assert len(link) <= mailto.BUDGET
    assert "%0A" not in link.split("&body=")[0]


def test_the_database_being_down_shows_the_outage_page(cfg, clock):
    dead = create_engine("postgresql+psycopg://postgres@/nope?host=/nonexistent", hide_parameters=True)
    with TestClient(create_app(cfg, clock=clock, engine=dead)) as c:
        r = c.get("/signin")
        version = _css_version(c)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"
    assert "unavailable" in r.text
    assert f"/static/app.css?v={version}" in r.text


def _css_version(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200
    return hashlib.sha256(css.content).hexdigest()[:12]


def test_pages_link_the_stylesheet_by_a_hash_of_its_content(client):
    # Value: protects=a deploy that changes app.css changes its URL, so browsers never pair new
    # markup (e.g. the hidden board h1) with cached old CSS; fails_when=the link loses its ?v= or
    # the hash stops tracking the served file; why_new=no test covered the stylesheet link;
    # seam=none; no_claim=browser cache headers or a real cache
    html = client.get("/signin").text
    assert f'href="/static/app.css?v={_css_version(client)}"' in html
    assert client.get(f"/static/app.css?v={_css_version(client)}").status_code == 200


def test_theme_defaults_to_system(client):
    html = client.get("/signin").text
    assert "data-theme" not in html.split("<head>")[0]
    assert 'name="theme" value="system" aria-pressed="true"' in html


def test_theme_choice_applies_to_every_page_and_survives_sign_out(client, people):
    token = csrf_from(client.get("/signin").text)
    r = client.post("/theme", data={"theme": "dark", "back": "/signin", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
    assert '<html lang="en" data-theme="dark">' in client.get("/signin").text

    token = sign_in(client, people.lead)
    board = client.get("/board").text
    assert '<html lang="en" data-theme="dark">' in board
    assert 'name="theme" value="dark" aria-pressed="true"' in board
    client.post("/signout", data={"csrf_token": token})
    assert '<html lang="en" data-theme="dark">' in client.get("/signin").text


def test_theme_light_then_back_to_system(client):
    token = csrf_from(client.get("/signin").text)
    client.post("/theme", data={"theme": "light", "back": "/signin", "csrf_token": token})
    assert '<html lang="en" data-theme="light">' in client.get("/signin").text
    client.post("/theme", data={"theme": "system", "back": "/signin", "csrf_token": token})
    html = client.get("/signin").text
    assert "data-theme" not in html.split("<head>")[0]


def test_theme_refuses_odd_values_and_offsite_back_links(client):
    token = csrf_from(client.get("/signin").text)
    r = client.post("/theme", data={"theme": "neon", "back": "//evil.example", "csrf_token": token},
                    follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "data-theme" not in client.get("/signin").text.split("<head>")[0]


def test_a_lead_keeps_sent_it_after_triage_asks_too(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    mine = domain.request_update(db, clock, people.lead, a.public_id)
    domain.request_update(db, clock, people.triage, a.public_id)
    db.commit()
    sign_in(client, people.lead)
    html = client.get("/board").text
    assert "go out?" in html and f'action="/asks/{mine.id}/sent"' in html
    # Value: protects=the cancel button beside 'Sent it?' says it cancels the ask (FINDING-011);
    # fails_when=the label goes back to a bare 'Cancel' or the cancel form drops out; why_new=no test
    # rendered the cancel-ask control at all; seam=none; no_claim=styling of the quiet button
    assert re.search(rf'action="/asks/{mine.id}/cancel">(?:(?!</form>).)*>Cancel ask</button>', html, re.S)


def test_the_board_has_one_page_heading(client, people):
    # Value: protects=the board exposes a single page-level h1 for screen-reader heading navigation;
    # fails_when=the hidden h1 is removed or a second h1 is added; why_new=no test checked board headings;
    # seam=none; no_claim=that the heading is visually hidden (CSS)
    sign_in(client, people.lead)
    html = client.get("/board").text
    assert re.findall(r"<h1\b[^>]*>(.*?)</h1>", html) == ["Nothing needs you right now"]


def test_navigation_follows_policy(client, people):
    sign_in(client, people.ann)
    html = client.get("/mine").text
    assert "/firedrills/new" not in html and 'href="/roster"' not in html
    client.cookies.clear()
    sign_in(client, people.triage)
    html = client.get("/board").text
    assert 'href="/firedrills/new"' in html and 'href="/roster"' in html


def test_the_new_firedrill_page_marks_its_nav_link_current(client, people):
    token = sign_in(client, people.lead)
    current = 'href="/firedrills/new" aria-current="page"'
    assert current in client.get("/firedrills/new").text
    r = client.post("/firedrills/new", data={"csrf_token": token, "title": "Keep me", "short_description": "And me"})
    assert r.status_code == 400 and current in r.text
    # Value: protects=the bad-due-date re-render keeps its error and the current nav mark; fails_when=tab="new"
    # is dropped from the ValueError branch or that branch stops rendering the form; why_new=only the
    # DomainError branch was exercised; seam=none; no_claim=browser date-picker validation
    r = client.post("/firedrills/new", data={"csrf_token": token, "title": "Keep me", "short_description": "And me",
                                             "due_date": "2026-02-30"})
    assert r.status_code == 400 and "Due date is not a valid date." in r.text and current in r.text
    assert 'value="Keep me"' in r.text
    assert current not in client.get("/board").text


def test_the_update_page_offers_a_wait(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    sign_in(client, people.ann)
    html = client.get(f"/a/{a.public_id}/update").text
    assert f'action="/a/{a.public_id}/wait"' in html
    assert 'name="until" type="date" min="2026-11-12" required' in html
    assert 'name="reason"' in html


def _wait_form(client, a):
    html = client.get(f"/a/{a.public_id}/update").text
    token = re.search(r'action="/a/[^"]+/wait".*?name="form_token" value="([^"]+)"', html, re.S).group(1)
    return {"csrf_token": csrf_from(html), "form_token": token}


def test_setting_a_wait_moves_the_line_to_waiting(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    sign_in(client, people.ann)
    data = {**_wait_form(client, a), "until": "2026-11-16", "reason": "Vendor ships the patch Monday"}
    r = client.post(f"/a/{a.public_id}/wait", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/f/{f.public_id}#a-{a.public_id}"

    client.cookies.clear()
    sign_in(client, people.lead)
    page = client.get(f"/f/{f.public_id}").text
    assert "Waiting until Mon Nov 16" in page and "Vendor ships the patch Monday" in page
    assert "Ask anyway" in page
    assert "0 of 1 done · 1 waiting" in client.get("/board").text


def test_a_wait_error_keeps_what_was_typed(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    domain.post_update(db, clock, people.ann, a.public_id, status="on_track", text="Halfway there.", form_token="u1")
    db.commit()
    sign_in(client, people.ann)
    form = _wait_form(client, a)

    r = client.post(f"/a/{a.public_id}/wait", data={**form, "until": "2026-11-11", "reason": "Vendor patch"})
    assert r.status_code == 400 and "The waiting date must be in the future." in r.text
    assert 'value="Vendor patch"' in r.text and 'value="2026-11-11"' in r.text
    assert "Halfway there." in r.text
    assert f'name="form_token" value="{form["form_token"]}"' in r.text

    r = client.post(f"/a/{a.public_id}/wait", data={**form, "until": "2026-11-20", "reason": "  "})
    assert r.status_code == 400 and "Say what you are waiting for." in r.text and 'value="2026-11-20"' in r.text

    r = client.post(f"/a/{a.public_id}/wait", data={**form, "until": "", "reason": "Vendor patch"})
    assert r.status_code == 400 and "Pick a date." in r.text
    assert db.scalar(select(func.count()).select_from(Update)) == 1


@pytest.mark.parametrize("who", ["lead", "triage"])
def test_lead_or_triage_sets_a_wait_for_the_owner_once_and_it_answers_the_ask(who, db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    ask = domain.request_update(db, clock, people.lead, a.public_id)
    db.commit()
    poster = getattr(people, who)
    sign_in(client, poster)
    assert "Posted for Ann Manager" in client.get(f"/a/{a.public_id}/update").text.split('id="wait"')[1]
    data = {**_wait_form(client, a), "until": "2026-11-16", "reason": "Change window Monday"}
    for _ in range(2):
        client.post(f"/a/{a.public_id}/wait", data=data, follow_redirects=False)
    assert db.scalar(select(func.count()).select_from(Update)) == 1
    db.refresh(ask)
    assert ask.answered_by_update_id is not None
    page = client.get(f"/f/{f.public_id}").text
    assert f"posted for, by {poster.display_name}" in page and "Change window Monday" in page


def test_a_done_assignment_offers_no_wait_and_refuses_one(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    sign_in(client, people.ann)
    form = _wait_form(client, a)
    domain.post_update(db, clock, people.ann, a.public_id, status="done", text="Finished.", form_token="d1")
    db.commit()
    assert f'action="/a/{a.public_id}/wait"' not in client.get(f"/a/{a.public_id}/update").text
    r = client.post(f"/a/{a.public_id}/wait", data={**form, "until": "2026-11-16", "reason": "Late"})
    assert r.status_code == 409 and "This assignment is done, so it cannot wait." in r.text


def test_a_stale_line_leaves_the_stale_filter_when_it_waits(db, clock, client, people):
    clock.set(datetime(2026, 11, 2, 10))
    f = make(db, clock, people)
    clock.set(datetime(2026, 11, 11, 10))
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    sign_in(client, people.ann)
    board_html = client.get("/board").text
    assert "needs your update." in board_html and f'href="/a/{a.public_id}/update"' in board_html
    client.post(f"/a/{a.public_id}/wait", data={**_wait_form(client, a), "until": "2026-11-16", "reason": "Vendor"})
    board_html = client.get("/board").text
    assert f'href="/a/{a.public_id}/update"' not in board_html and "1 waiting" in board_html


def test_a_manager_cannot_set_a_wait_on_someone_elses_line(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    token = sign_in(client, people.bob)
    r = client.post(f"/a/{a.public_id}/wait", data={"csrf_token": token, "form_token": "x", "until": "2026-11-16",
                                                      "reason": "Not mine"})
    assert r.status_code == 403
    assert db.scalar(select(func.count()).select_from(Update)) == 0


def test_a_manager_cannot_post_an_update_on_someone_elses_line(db, clock, client, people):
    f = make(db, clock, people)
    a = db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
    token = sign_in(client, people.bob)
    r = client.post(f"/a/{a.public_id}/update", data={"csrf_token": token, "form_token": "x", "status": "on_track",
                                                        "text": "Not mine"})
    assert r.status_code == 403 and "NOT_ALLOWED" in r.text


def test_a_lead_cannot_deactivate_someone(client, people):
    token = sign_in(client, people.lead)
    r = client.post(f"/roster/{people.ann.id}/deactivate", data={"csrf_token": token})
    assert r.status_code == 403 and "NOT_ALLOWED" in r.text
