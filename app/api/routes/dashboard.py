"""Dashboard — a self-contained vanilla-JS single page served by FastAPI.

No build step, no external CDNs: the page calls the same-origin API to run scans
and browse candidates (with thesis + analytics), proposals, and paper trades.
Read-only research surface; proposal execution still passes the guard.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

_HTML = (Path(__file__).resolve().parents[2] / "web" / "dashboard.html").read_text(
    encoding="utf-8"
)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return _HTML


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    return _HTML
