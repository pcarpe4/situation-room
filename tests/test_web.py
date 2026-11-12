import re
from urllib.parse import unquote

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
    assert "Open the email" in board_html and "Sent it?" in board_html
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
        for path in ("/board", "/mine", "/board?filter=stale", "/board?filter=waiting", "/board?filter=overdue"):
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
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"
    assert "unavailable" in r.text


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
    assert "Sent it?" in html and f'action="/asks/{mine.id}/sent"' in html


def test_navigation_follows_policy(client, people):
    sign_in(client, people.ann)
    html = client.get("/mine").text
    assert "/firedrills/new" not in html and 'href="/roster"' not in html
    client.cookies.clear()
    sign_in(client, people.triage)
    html = client.get("/board").text
    assert 'href="/firedrills/new"' in html and 'href="/roster"' in html
