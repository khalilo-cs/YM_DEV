"""MOSAIC adapter tests against a local *protocol stub* (not a real model).

These verify request/response handling, schema validation and safety rules. They do NOT
verify that any real model produces good repairs.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from helpers import make_renamed_api_project

from yemendev.core.config import ModelSettings, SandboxSettings
from yemendev.core.contracts import InvalidInputError, UnavailableError
from yemendev.diagnostics.engine import diagnose
from yemendev.mosaic.adapters import ModelRegistry, ModelRequest, OpenAICompatibleAdapter
from yemendev.recovery.model import PlanSource
from yemendev.recovery.model_proposer import propose_with_model
from yemendev.sandbox.testrunner import run_pytest
from yemendev.sandbox.workspace import Workspace


class Stub:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stub.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "body": json.loads(body)})
                content = stub.reply(stub.requests[-1]) if callable(stub.reply) else stub.reply
                out = json.dumps({"choices": [{"message": {"content": content}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def stub_factory():
    made = []

    def make(reply):
        s = Stub(reply)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_unconfigured_is_unavailable_not_faked():
    a = OpenAICompatibleAdapter(ModelSettings())
    assert not a.availability().available
    with pytest.raises(UnavailableError):
        a.complete(ModelRequest(system="s", prompt="p"))
    assert ModelRegistry([a]).first_available() is None


def test_missing_api_key_env_is_unavailable(monkeypatch):
    monkeypatch.delenv("YDV_TEST_KEY", raising=False)
    a = OpenAICompatibleAdapter(ModelSettings(base_url="http://127.0.0.1:1", model="m", api_key_env="YDV_TEST_KEY"))
    assert "YDV_TEST_KEY" in a.availability().reason


def test_unreachable_endpoint_is_unavailable():
    a = OpenAICompatibleAdapter(ModelSettings(base_url="http://127.0.0.1:1", model="m", timeout_seconds=2))
    with pytest.raises(UnavailableError):
        a.complete(ModelRequest(system="s", prompt="p"))


def test_key_sent_from_env_and_request_shape(stub_factory, monkeypatch):
    monkeypatch.setenv("YDV_TEST_KEY", "sk-test-123")
    s = stub_factory("hello")
    a = OpenAICompatibleAdapter(ModelSettings(base_url=s.url, model="local-model", api_key_env="YDV_TEST_KEY"))
    r = a.complete(ModelRequest(system="sys", prompt="hi"))
    assert r.text == "hello" and r.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    req = s.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-test-123"
    assert req["body"]["model"] == "local-model" and req["body"]["messages"][0]["role"] == "system"


def _diag_and_root(tmp_path):
    root = make_renamed_api_project(tmp_path / "p")
    runs = []
    for _ in range(2):
        with Workspace(root, SandboxSettings()) as ws:
            runs.append(run_pytest(ws))
    return diagnose(runs), root


def test_model_plan_is_validated_and_marked_untrusted(stub_factory, tmp_path):
    diag, root = _diag_and_root(tmp_path)
    fixed = (root / "calc/api.py").read_text().replace("add_numbers", "add")
    s = stub_factory("```json\n" + json.dumps({"rationale": "renamed API", "files": [
        {"path": "calc/api.py", "new_content": fixed},
        {"path": "tests/test_api.py", "new_content": "def test_total(): pass\n"},  # not shown -> dropped
        {"path": "../../etc/passwd", "new_content": "x"},                          # not shown -> dropped
    ]}) + "\n```")
    plan = propose_with_model(OpenAICompatibleAdapter(ModelSettings(base_url=s.url, model="m")), diag, root)
    assert plan.source is PlanSource.MODEL and "untrusted" in plan.description
    assert [p.path for p in plan.patches] == ["calc/api.py"]
    prompt = s.requests[0]["body"]["messages"][1]["content"]
    assert "calc/api.py" in prompt and "def test_total" not in prompt  # tests are never sent for editing


@pytest.mark.parametrize("reply", ["not json at all", json.dumps({"rationale": "x"}),
                                   json.dumps({"rationale": "x", "files": []})])
def test_invalid_model_output_rejected(stub_factory, tmp_path, reply):
    diag, root = _diag_and_root(tmp_path)
    s = stub_factory(reply)
    with pytest.raises(InvalidInputError):
        propose_with_model(OpenAICompatibleAdapter(ModelSettings(base_url=s.url, model="m")), diag, root)


def test_workflow_uses_model_plan_but_still_requires_simulation_and_approval(stub_factory, tmp_path, settings):
    from end_to_end.conftest import ALICE, BOB

    from yemendev.orion.tasks import TaskStatus
    from yemendev.orion.workflow import RepairWorkflow
    from yemendev.platform import Platform

    root = make_renamed_api_project(tmp_path / "proj")
    wrong = (root / "calc/api.py").read_text().replace("mathutils.add_numbers", "mathutils.mul")
    s = stub_factory(json.dumps({"rationale": "plausible but wrong", "files": [
        {"path": "calc/api.py", "new_content": wrong}]}))
    settings.model = ModelSettings(base_url=s.url, model="m")
    p = Platform(settings)
    wf = RepairWorkflow(p)
    proj = p.register_project(ALICE, "c", str(root))
    r = wf.advance(ALICE, wf.create(BOB, proj.id).id)
    # A wrong model patch is caught by simulation; nothing is applied and no approval is even requested.
    assert r.task.status is TaskStatus.NO_ACTION and "simulation" in r.message
    assert p.approvals.for_task(r.task.id) == []
    assert "add_numbers" in (root / "calc/api.py").read_text()
