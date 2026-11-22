"""Digital Continuity Passport (directive 6.3): a portable, versioned, signed description of a project.

Contains no secrets and no source code. Anything the platform has not analysed is stated as unknown.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from yemendev import __version__
from yemendev.continuity.signing import fingerprint, public_hex, sign, verify
from yemendev.core.contracts import canonical_json, iso, sha256_text

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from yemendev.platform import Platform

SCHEMA_VERSION = "1.0"


class PassportBody(BaseModel):
    schema_version: str = Field(pattern=r"^1\.\d+$")
    generated_at: str
    platform_version: str
    identity: dict[str, Any]
    architecture: dict[str, Any]
    dependencies: list[dict[str, Any]]
    compatibility: dict[str, Any]
    data_requirements: dict[str, Any]
    recovery: dict[str, Any]
    security: dict[str, Any]
    operational_constraints: dict[str, Any]
    verification: dict[str, Any]
    evidence: list[dict[str, str]]
    limitations: list[str]


class Signature(BaseModel):
    algorithm: str = "ed25519"
    public_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    signer_fingerprint: str
    body_sha256: str
    value: str = Field(pattern=r"^[0-9a-f]{128}$")


class SignedPassport(BaseModel):
    body: PassportBody
    signature: Signature


class PassportVerification(BaseModel):
    schema_valid: bool
    body_hash_matches: bool
    signature_valid: bool
    signer_trusted: bool
    signer_fingerprint: str | None
    problems: list[str]

    @property
    def accepted(self) -> bool:
        return self.schema_valid and self.body_hash_matches and self.signature_valid


def build_passport(p: Platform, project_id: str) -> PassportBody:
    from yemendev.twin.graph import DependencyGraph

    proj = p.project(project_id)
    scan_id, snap = p.latest_scan(project_id)
    rep = DependencyGraph.from_snapshot(snap).report()
    with p.db.read() as conn:
        ev = conn.execute("SELECT id, action, outcome, content_hash, created_at FROM evidence_records WHERE target = ?"
                          " ORDER BY created_at DESC, rowid DESC LIMIT 200", (project_id,)).fetchall()
    ev = list(reversed(ev))
    verified = [r["id"] for r in ev if r["action"] == "repair.verify" and r["outcome"] == "passed"]
    rollbacks = [r["id"] for r in ev if r["action"] == "repair.rollback"]
    baselines = [r for r in ev if r["action"] == "tests.baseline"]
    immune = [r for r in ev if r["action"] == "immune.check"]
    sandbox, _ = p.sandbox_for(project_id)
    return PassportBody(
        schema_version=SCHEMA_VERSION, generated_at=iso(), platform_version=__version__,
        identity={"project_id": proj.id, "name": proj.name, "scan_id": scan_id, "tree_hash": snap.tree_hash},
        architecture={"languages": snap.languages, "modules": len(snap.modules),
                      "module_names": [m.name for m in snap.modules][:2000], "internal_edges": len(rep.edges),
                      "cycles": rep.cycles, "critical_modules": rep.critical_modules},
        dependencies=[{"name": d.name, "spec": d.spec, "group": d.group, "source_file": d.source_file,
                       "pinned": any(op in d.spec.split(";")[0] for op in ("==", "<", "~="))}
                      for d in snap.declared_dependencies],
        compatibility={"test_framework": snap.test_framework, "sandbox_backend": sandbox.backend,
                       "sandbox_image": sandbox.docker_image if sandbox.backend != "process" else None,
                       "python_requires": "unknown - not analysed"},
        data_requirements={"status": "unknown - databases and data invariants are not analysed yet"},
        recovery={"test_command": "python -m pytest", "change_policy": "simulate, bound human approval, "
                  "checkpoint, verify, byte-exact rollback", "verified_repairs": verified,
                  "rollbacks": rollbacks},
        security={"secrets_included": False, "sensitive_file_names": snap.sensitive_files,
                  "latest_immune_check": immune[-1]["outcome"] if immune else "never run"},
        operational_constraints={"network_isolation": sandbox.network_isolation,
                                 "timeout_seconds": sandbox.timeout_seconds, "memory_mb": sandbox.max_memory_mb},
        verification={"latest_baseline": baselines[-1]["outcome"] if baselines else "never run",
                      "latest_baseline_evidence": baselines[-1]["id"] if baselines else None},
        evidence=[{"id": r["id"], "action": r["action"], "content_hash": r["content_hash"]} for r in ev[-50:]],
        limitations=["evidence ids refer to the issuing installation; their contents are not included",
                     "describes the project as of the latest scan"])


def sign_passport(body: PassportBody, key: Ed25519PrivateKey) -> SignedPassport:
    msg = canonical_json(body.model_dump(mode="json")).encode()
    pub = public_hex(key)
    return SignedPassport(body=body, signature=Signature(public_key=pub, signer_fingerprint=fingerprint(pub),
                                                         body_sha256=sha256_text(msg.decode()), value=sign(key, msg)))


def verify_passport(raw: str | bytes, trusted: set[str]) -> tuple[PassportVerification, SignedPassport | None]:
    problems: list[str] = []
    try:
        sp = SignedPassport.model_validate(json.loads(raw))
    except (ValueError, TypeError) as exc:
        return PassportVerification(schema_valid=False, body_hash_matches=False, signature_valid=False,
                                    signer_trusted=False, signer_fingerprint=None,
                                    problems=[f"schema validation failed: {str(exc)[:300]}"]), None
    msg = canonical_json(sp.body.model_dump(mode="json")).encode()
    hash_ok = sha256_text(msg.decode()) == sp.signature.body_sha256
    sig_ok = verify(sp.signature.public_key, msg, sp.signature.value)
    trusted_signer = sp.signature.public_key in trusted
    if not hash_ok:
        problems.append("body hash does not match (modified after signing)")
    if not sig_ok:
        problems.append("signature invalid")
    if sig_ok and not trusted_signer:
        problems.append("valid signature from an UNTRUSTED key: integrity proven, origin not")
    if sp.signature.signer_fingerprint != fingerprint(sp.signature.public_key):
        problems.append("signer fingerprint does not match the public key")
        sig_ok = False
    return PassportVerification(schema_valid=True, body_hash_matches=hash_ok, signature_valid=sig_ok,
                                signer_trusted=trusted_signer, signer_fingerprint=sp.signature.signer_fingerprint,
                                problems=problems), sp
