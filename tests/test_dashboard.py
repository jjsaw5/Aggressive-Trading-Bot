"""Dashboard is served and self-contained (no external resources)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_serves_html() -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Aggressive Trading Bot" in r.text
    assert "<table" in r.text or "Run a scan" in r.text


def test_dashboard_route_also_serves() -> None:
    assert client.get("/dashboard").status_code == 200


def test_dashboard_is_self_contained() -> None:
    html = client.get("/").text
    # No external scripts/styles/fonts — CSP-safe, offline-capable.
    assert "src=\"http" not in html
    assert "href=\"http" not in html
    assert "cdn" not in html.lower()
