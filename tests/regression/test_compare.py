"""Comparative benchmark (stage 20): arms run real code on shared fixtures; nothing is estimated."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from integration.test_mosaic import stub_factory  # noqa: F401 - pytest fixture

from yemendev.benchmarks.compare import SCENARIOS, run_compare
from yemendev.benchmarks.fixtures import renamed_api_project
from yemendev.core.config import ModelSettings
from yemendev.core.contracts import InvalidInputError

NO_MODEL = ModelSettings()


def by(rep):
    return {(r.arm, r.scenario): r for r in rep.results}


def test_safety_gates_prevent_regression_and_data_loss_that_apply_then_test_causes():
    rep = run_compare(seed=7, arms="AE", only=["reject_regressing_plan", "compound_crash_plus_human_edit"],
                      model=NO_MODEL)
    r = by(rep)
    assert r["A", "reject_regressing_plan"].regression and not r["A", "reject_regressing_plan"].correct
    assert r["A", "compound_crash_plus_human_edit"].data_loss  # the human's edit was overwritten
    for sc in ("reject_regressing_plan", "compound_crash_plus_human_edit"):
        e = r["E", sc]
        assert e.correct and not e.regression and not e.data_loss, e
    assert r["E", "reject_regressing_plan"].outcome == "no_action"
    assert rep.summary["E"]["evidence_records"] > 0 and rep.summary["A"]["evidence_records"] == 0


def test_ablations_isolate_skills_and_gates():
    rep = run_compare(seed=7, arms="CDE", only=["skill_transfer", "reject_regressing_plan"], model=NO_MODEL)
    r = by(rep)
    assert not r["C", "skill_transfer"].correct    # memory without skills: second project not repaired
    assert r["D", "skill_transfer"].correct        # the learned skill does transfer ...
    assert r["E", "skill_transfer"].correct
    assert r["D", "reject_regressing_plan"].regression  # ... but without the gates a bad plan goes through
    assert r["E", "reject_regressing_plan"].correct and r["C", "reject_regressing_plan"].correct


def test_b_is_reported_not_run_without_a_model_never_estimated():
    rep = run_compare(seed=7, arms="AB", only=["healthy_no_action"], model=NO_MODEL)
    assert "B" in rep.not_run and "not estimated" in rep.not_run["B"]
    assert {x.arm for x in rep.results} == {"A"} and "B" not in rep.summary
    assert rep.environment["model"] is None


def test_b_arm_runs_against_a_configured_model(stub_factory):  # noqa: F811
    """Harness mechanics only: the 'model' is a protocol stub that returns a correct fix. This says nothing
    about the quality of any real model."""
    seed = 7
    with tempfile.TemporaryDirectory() as d:
        meta = renamed_api_project(Path(d) / "p", seed * 1000 + SCENARIOS.index("repair_basic"))

    def reply(req):
        prompt = req["body"]["messages"][1]["content"]
        m = re.search(r"--- (\S+)\n(.*)$", prompt, re.DOTALL)
        path, text = m.group(1), m.group(2)
        fixed = text.replace(f"core.{meta['old']}", f"core.{meta['new']}")
        return json.dumps({"rationale": "renamed API", "files": [{"path": path, "new_content": fixed}]})

    s = stub_factory(reply)
    rep = run_compare(seed=seed, arms="B", only=["repair_basic", "healthy_no_action"],
                      model=ModelSettings(base_url=s.url, model="stub"))
    r = by(rep)
    assert r["B", "repair_basic"].correct and r["B", "repair_basic"].outcome == "applied", r["B", "repair_basic"]
    assert r["B", "healthy_no_action"].outcome == "nothing_applied" and r["B", "healthy_no_action"].correct
    assert len(s.requests) == 1  # the healthy project never reached the model
    assert rep.environment["model"].startswith("stub@") and not rep.not_run


@pytest.mark.parametrize("kw", [{"arms": "Z"}, {"arms": ""}, {"only": ["nope"]}])
def test_unknown_inputs_are_rejected(kw):
    with pytest.raises(InvalidInputError):
        run_compare(model=NO_MODEL, **kw)
