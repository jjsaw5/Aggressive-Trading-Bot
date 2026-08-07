"""API surface: scans, audit, and human decision tracking.

The safety test at the bottom is the important one — it asserts that no route
this subsystem exposes can place an order.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.session import engine
from app.main import app


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c


def test_the_methodology_endpoint_reports_the_live_rubric(client):
    r = client.get("/multiagent/methodology")
    assert r.status_code == 200
    body = r.json()
    assert body["version"].startswith("ma-")
    assert sum(body["weights"].values()) == 100.0
    assert set(body["allowed_strategies"]) == {
        "long_call", "long_put", "bull_call_spread", "bear_put_spread"
    }
    assert "UNCALIBRATED" in body["note"]


def test_a_scan_runs_persists_and_reads_back(client):
    r = client.post("/multiagent/scans", json={"symbols": ["NVDA", "AMD"], "persist": True})
    assert r.status_code == 200, r.text
    summary = r.json()
    run_id = summary["run_id"]
    assert summary["calibration_status"] == "UNCALIBRATED"
    assert summary["agent_runner"] == "deterministic"

    runs = client.get("/multiagent/runs").json()
    assert any(run["run_id"] == run_id for run in runs)

    recos = client.get(f"/multiagent/runs/{run_id}/recommendations").json()
    assert recos
    for reco in recos:
        assert reco["calibration_status"] == "UNCALIBRATED"
        assert 0.0 <= reco["score"] <= 100.0
        assert 0.0 <= reco["input_coverage"] <= 1.0

    # Every stored recommendation exposes its full audit trail.
    audit = client.get(f"/multiagent/candidates/{recos[0]['candidate_id']}/audit").json()
    assert len(audit["components"]) == 8
    assert any(c["rules"] for c in audit["components"])


def test_an_unknown_run_returns_404(client):
    assert client.get("/multiagent/runs/does-not-exist/recommendations").status_code == 404
    assert client.get("/multiagent/candidates/does-not-exist/audit").status_code == 404


def test_the_text_report_endpoint_renders(client):
    r = client.post("/multiagent/scans/text", json={"symbols": ["NVDA"], "persist": False})
    assert r.status_code == 200
    assert "MULTI-AGENT OPTIONS RESEARCH" in r.json()["report"]
    assert "PLACES NO ORDERS" in r.json()["report"]


def test_the_full_report_endpoint_returns_the_score_breakdown(client):
    r = client.post("/multiagent/scans/report", json={"symbols": ["NVDA"], "persist": False})
    assert r.status_code == 200
    report = r.json()
    assert report["calibration_status"] == "UNCALIBRATED"
    assert "diagnostics" in report
    assert "rejected" in report


def test_a_human_decision_execution_and_result_can_be_recorded(client):
    scan = client.post("/multiagent/scans", json={"symbols": ["NVDA"], "persist": True}).json()
    recos = client.get(f"/multiagent/runs/{scan['run_id']}/recommendations").json()
    candidate_id = recos[0]["candidate_id"]

    decision = client.post(
        "/multiagent/decisions",
        json={
            "run_id": scan["run_id"],
            "candidate_id": candidate_id,
            "action": "entered",
            "notes": "taking it",
        },
    )
    assert decision.status_code == 200
    decision_id = decision.json()["decision_id"]

    execution = client.post(
        "/multiagent/executions",
        json={
            "decision_id": decision_id,
            "candidate_id": candidate_id,
            "contract_description": "NVDA 2026-09-04 126/123.5 bear put spread",
            "quantity": 1,
            "entry_price_per_contract": 97.0,
        },
    )
    assert execution.status_code == 200
    # The endpoint says plainly what it did and did not do.
    assert "does not place orders" in execution.json()["note"]

    result = client.post(
        "/multiagent/results",
        json={
            "execution_id": execution.json()["execution_id"],
            "candidate_id": candidate_id,
            "realized_pnl": 43.0,
            "max_favorable_excursion_bound": 60.0,
        },
    )
    assert result.status_code == 200
    # The excursion caveat travels with the record.
    assert "not achieved prices" in result.json()["excursion_note"]


@pytest.mark.parametrize("action", ["approved", "rejected", "watched", "entered", "skipped"])
def test_every_decision_action_is_accepted(client, action):
    r = client.post(
        "/multiagent/decisions",
        json={"run_id": "r", "candidate_id": "c", "action": action},
    )
    assert r.status_code == 200
    assert r.json()["action"] == action


def test_an_invalid_decision_action_is_rejected(client):
    r = client.post(
        "/multiagent/decisions",
        json={"run_id": "r", "candidate_id": "c", "action": "yolo"},
    )
    assert r.status_code == 422


def _multiagent_paths() -> list[str]:
    """Every mounted path under /multiagent.

    Read from the generated OpenAPI schema rather than `app.routes`: FastAPI
    nests included routers, so the top-level list does not contain them and a
    naive scan would silently find nothing — which would make this safety test
    pass by looking at an empty set.
    """
    return [p for p in app.openapi()["paths"] if p.startswith("/multiagent")]


def test_no_multiagent_route_can_place_an_order():
    """The safety requirement, asserted against the route table."""
    paths = _multiagent_paths()
    assert paths, "the multiagent router is not mounted"

    forbidden = ("order", "execute", "submit", "buy", "sell", "place")
    for path in paths:
        lowered = path.lower()
        # `/multiagent/executions` RECORDS a trade the human already made. It is
        # the only path containing 'execut' and it places nothing — the test
        # below pins that.
        if lowered.endswith("/executions"):
            continue
        assert not any(word in lowered for word in forbidden), (
            f"{path} looks like an order-placement route"
        )


def _executable_source(module) -> str:
    """Module source with docstrings stripped.

    Checking raw text would flag this router's own docstring, which *mentions*
    the execution guard in order to say it does not use it. The check must look
    at what the code does, not at what the prose says.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                node.value.value = ""
    return ast.unparse(tree)


def test_the_execution_endpoint_only_records_and_says_so(client):
    """Named 'executions' but it writes a row, it does not send an order."""
    import inspect

    from app.api.routes import multiagent as module

    code = _executable_source(module)
    # No import of, or call into, the platform's live-order chokepoint.
    assert "execution_guard" not in code
    assert "place_order" not in code
    assert "place_equity_order" not in code
    # And the response says so to whoever calls it.
    assert "does not place orders" in inspect.getsource(module)
