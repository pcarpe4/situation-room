from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .security import csrf_token


def render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    context.setdefault("me", getattr(request.state, "person", None))
    context["cfg"] = request.app.state.cfg
    context["csrf_token"] = csrf_token(request)
    return templates.TemplateResponse(request, name, context, status_code=status_code)
