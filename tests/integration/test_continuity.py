"""Directive 5.10 / 6.3: signed passports and offline sync bundles between two real installations."""

import json

import pytest
from end_to_end.conftest import ALICE, BOB
from helpers import make_passing_project, write

from yemendev.core.config import SandboxSettings, Settings
from yemendev.core.contracts import PermissionDeniedError
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.memory.model import MemoryCategory, MemoryStatus, MemoryWrite
from yemendev.platform import Platform


@pytest.fixture
def two(tmp_path):
    a = Platform(Settings(data_dir=tmp_path / "A", sandbox=SandboxSettings(backend="process")))
    b = Platform(Settings(data_dir=tmp_path / "B", sandbox=SandboxSettings(backend="process")))
    root = make_passing_project(tmp_path / "src")
    write(root, ".env", "API_KEY=sk-livesecret0123456789abcdef\n")
    pa = a.register_project(ALICE, "inventory", str(root))
    a.scan_project(ALICE, pa.id)
    rb = make_passing_project(tmp_path / "copy")
    pb = b.register_project(ALICE, "inventory", str(rb))
    b.scan_project(ALICE, pb.id)
    return a, pa, b, pb


def test_passport_is_signed_complete_and_secret_free(two):
    a, pa, b, _ = two
    sp = a.export_passport(ALICE, pa.id)
    raw = sp.model_dump_json()
    assert "livesecret" not in raw and sp.body.security["sensitive_file_names"] == [".env"]
    assert sp.body.security["secrets_included"] is False
    for section in ("identity", "architecture", "dependencies", "compatibility", "data_requirements", "recovery",
                    "security", "operational_constraints", "verification", "evidence"):
        assert getattr(sp.body, section) is not None
    assert "unknown" in sp.body.data_requirements["status"]  # not analysed -> stated, not invented
    # on the issuing installation the signer is trusted (own key)
    own, _ = a.verify_passport(ALICE, raw)
    assert own.accepted and own.signer_trusted
    # elsewhere: integrity proven, origin not trusted until the key is trusted
    other, _ = b.verify_passport(ALICE, raw)
    assert other.accepted and not other.signer_trusted and "UNTRUSTED" in other.problems[0]


def test_tampered_or_malformed_passports_are_rejected(two):
    a, pa, b, pb = two
    body = json.loads(a.export_passport(ALICE, pa.id).model_dump_json())
    body["body"]["identity"]["name"] = "evil"
    tampered, _ = b.verify_passport(ALICE, json.dumps(body))
    assert not tampered.accepted and not tampered.signature_valid and not tampered.body_hash_matches
    bad, sp = b.verify_passport(ALICE, '{"not": "a passport"}')
    assert not bad.schema_valid and sp is None
    with pytest.raises(PermissionDeniedError):
        b.import_passport(ALICE, pb.id, json.dumps(body))


def test_passport_import_requires_trusted_signer_and_stays_unverified(two):
    a, pa, b, pb = two
    raw = a.export_passport(ALICE, pa.id).model_dump_json()
    with pytest.raises(PermissionDeniedError, match="not trusted"):
        b.import_passport(ALICE, pb.id, raw)
    from yemendev.continuity.signing import public_hex
    b.trust_key(ALICE, public_hex(a.signing_key))
    assert b.import_passport(ALICE, pb.id, raw).signer_trusted
    [rec] = b.memory.query(pb.id, category=MemoryCategory.PROJECT_IDENTITY)
    assert rec.status is MemoryStatus.UNVERIFIED and rec.source.startswith("passport:")


def test_sync_roundtrip_conflicts_idempotency_and_resolution(two):
    a, pa, b, pb = two
    from yemendev.continuity.signing import public_hex
    a.memory.write(MemoryWrite(project_id=pa.id, category=MemoryCategory.CONSTRAINT, subject="deploy",
                               key="window", value="fridays only", verified=True, source="human:alice",
                               evidence_ids=["evd_a"]))
    b.memory.write(MemoryWrite(project_id=pb.id, category=MemoryCategory.CONSTRAINT, subject="deploy",
                               key="window", value="never on fridays", verified=True, source="human:bob",
                               evidence_ids=["evd_b"]))
    bundle, job = a.sync_export(ALICE, pa.id)
    assert job.status == "exported_not_delivered"  # never claims remote success
    raw = bundle.model_dump_json()
    with pytest.raises(PermissionDeniedError, match="not trusted"):
        b.sync_import(ALICE, pb.id, raw)
    assert b.sync_status(ALICE)["jobs"] == []  # a rejected bundle creates no job
    b.trust_key(ALICE, public_hex(a.signing_key))
    j = b.sync_import(ALICE, pb.id, raw)
    assert j.status == "completed" and j.conflicts == 1 and j.records_applied >= 1
    # imported knowledge is unverified; the conflicting verified local value is untouched
    window = b.memory.history(pb.id, "deploy", "window")
    assert [w.value for w in window if w.status is MemoryStatus.VERIFIED] == ["never on fridays"]
    imported = [r for r in b.memory.query(pb.id, limit=5000) if r.source.startswith("sync:")]
    assert imported and all(r.status is MemoryStatus.UNVERIFIED for r in imported)
    # idempotent: same bundle again -> same job, nothing new
    assert b.sync_import(ALICE, pb.id, raw).id == j.id
    [c] = b.sync_conflicts(ALICE, pb.id, "open")
    assert (c.local_value, c.remote_value) == ("never on fridays", "fridays only")
    agent = Actor(id="planning-agent", kind=ActorKind.AGENT, role=Role.ADMIN)
    with pytest.raises(PermissionDeniedError):
        b.resolve_sync_conflict(agent, c.id, "take_remote")
    b.grant_member(ALICE, pb.id, BOB.ref, Role.OPERATOR)
    assert b.resolve_sync_conflict(BOB, c.id, "take_remote").status == "resolved"
    current = [w for w in b.memory.history(pb.id, "deploy", "window") if w.status is MemoryStatus.VERIFIED]
    assert [w.value for w in current] == ["fridays only"] and current[0].evidence_ids


def test_tampered_bundle_rejected(two):
    a, pa, b, pb = two
    from yemendev.continuity.signing import public_hex
    b.trust_key(ALICE, public_hex(a.signing_key))
    body = json.loads(a.sync_export(ALICE, pa.id)[0].model_dump_json())
    body["body"]["memory"][0]["value"] = "injected"
    with pytest.raises(PermissionDeniedError, match="invalid signature"):
        b.sync_import(ALICE, pb.id, json.dumps(body))


def test_only_admins_trust_keys_and_api_routes(two):
    a, pa, b, pb = two
    from fastapi.testclient import TestClient

    from yemendev.api.app import create_app
    with pytest.raises(PermissionDeniedError):
        b.trust_key(BOB, "a" * 64)
    _, tok = b.tokens.issue("alice", Role.ADMIN)
    c = TestClient(create_app(b), raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {tok}"}
    st = c.get("/api/v1/synchronization/status", headers=h).json()
    assert st["transport"].startswith("signed file bundles")
    passport = c.get(f"/api/v1/projects/{pb.id}/passport", headers=h).json()
    assert c.post("/api/v1/passports/verify", json=passport, headers=h).json()["accepted"]
    assert c.post("/api/v1/keys/trusted", json={"public_key": "zz" * 32}, headers=h).status_code == 422
