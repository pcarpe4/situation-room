from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..policy import Action, can
from .security import csrf_token

THEME_COOKIE = "sr_theme"
THEMES = ("light", "dark", "system")
FOCUS_COOKIE = "sr_focus"


def theme_of(request: Request) -> str:
    value = request.cookies.get(THEME_COOKIE)
    return value if value in ("light", "dark") else "system"


def render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    context.setdefault("me", getattr(request.state, "person", None))
    me = context["me"]
    # Templates ask policy, never the role: {% if can('create_firedrill') %}
    context["can"] = lambda action, obj=None: can(me, Action(action), obj)
    context["cfg"] = request.app.state.cfg
    context["csrf_token"] = csrf_token(request)
    context["theme"] = theme_of(request)
    context["focus"] = request.cookies.get(FOCUS_COOKIE) == "on"
    # Where the theme form returns to: this page, unless it was the result of a POST.
    url = request.url
    context["here"] = (url.path + (f"?{url.query}" if url.query else "")) if request.method == "GET" else "/"
    return templates.TemplateResponse(request, name, context, status_code=status_code)
