from end_to_end.conftest import ALICE
from fastapi.testclient import TestClient
from helpers import make_passing_project, make_renamed_api_project

from yemendev.governance.actors import Role


def test_fresh_project_gets_no_fabricated_predictions(platform, tmp_path):
    proj = platform.register_project(ALICE, "ok", str(make_passing_project(tmp_path / "ok")))
    platform.scan_project(ALICE, proj.id)
    r = platform.assess_risk(ALICE, proj.id)
    assert r.predictions == [] and r.anomalies == []
    assert {"test failure prediction", "repair reliability"} <= {x.analysis for x in r.insufficient_evidence}


def test_history_produces_predictions_root_causes_and_api(platform, wf, tmp_path):
    proj = platform.register_project(ALICE, "c", str(make_renamed_api_project(tmp_path / "p")))
    for _ in range(2):  # two tasks x two baseline runs = 4 recorded runs (no plan -> no_action)
        t = wf.create(ALICE, proj.id)
        wf.advance(ALICE, t.id)
    ev = [e for e in platform.evidence.list(task_id=t.id) if e.action == "diagnostics.root_causes"]
    assert ev and ev[0].body.details["candidates"][0]["certainty"] == "observed"
    assert all("duration_seconds" in r for e in platform.evidence.list(task_id=t.id)
               if e.action == "tests.baseline" for r in e.body.tests["runs"])
    r = platform.assess_risk(ALICE, proj.id)
    [p] = [p for p in r.predictions if p.subject == "tests.test_api::test_total"]
    assert p.kind == "persistent_failure" and abs(p.probability - 5 / 6) < 1e-3
    assert any(s.subject == "calc.mathutils" for s in r.risk_scores)
    assert any(e.action == "risk.assessment" for e in platform.evidence.list(limit=1000))

    _, tok = platform.tokens.issue("vic", Role.VIEWER)
    from yemendev.api.app import create_app
    c = TestClient(create_app(platform), raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get(f"/api/v1/projects/{proj.id}/risks", headers=h).status_code == 403  # not a member
    platform.grant_member(ALICE, proj.id, "human:vic", Role.VIEWER)
    body = c.get(f"/api/v1/projects/{proj.id}/risks", headers=h).json()
    assert body["predictions"] and body["limitations"]
    imp = c.get(f"/api/v1/projects/{proj.id}/impact?module=calc.mathutils", headers=h).json()
    assert [(x["module"], x["is_test"]) for x in imp["impacted"]] == [("calc.api", False), ("tests.test_api", True)]
    assert c.get(f"/api/v1/projects/{proj.id}/impact?module=nope", headers=h).status_code == 404
