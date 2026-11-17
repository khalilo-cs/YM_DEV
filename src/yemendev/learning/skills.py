"""Verified Skill Engine.

A skill is a versioned, declarative, testable capability. The only executable kind
implemented is ``token_rename``: replace a dotted identifier with another at word
boundaries. It is deliberately restricted - no user regex (no ReDoS), no code
execution - so validating a skill means running its own test cases deterministically.

Lifecycle: PROPOSED -> VALIDATING -> VERIFIED -> ACTIVE -> DEPRECATED, plus REJECTED
when validation fails. Rules (enforced here, tested in tests/unit/test_skills.py):
* only a VERIFIED skill whose latest validation passed *for the current content hash*
  can be activated, and activation needs a bound human approval (handled by caller);
* any change to a skill's spec creates a new version that starts at PROPOSED;
* field outcomes (repairs verified/failed) adjust confidence; a skill whose field
  failures exceed successes is automatically deprecated.
"""

from __future__ import annotations

import builtins
import json
import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field, field_validator

from yemendev.core.contracts import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PreconditionFailedError,
    digest,
    iso,
    new_id,
)
from yemendev.storage.db import Database

_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class SkillStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATING = "validating"
    VERIFIED = "verified"
    REJECTED = "rejected"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class SkillKind(StrEnum):
    TOKEN_RENAME = "token_rename"  # noqa: S105 - skill kind, not a secret


class TokenRenameParams(BaseModel):
    old: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)

    @field_validator("old", "new")
    @classmethod
    def _dotted(cls, v: str) -> str:
        if not _DOTTED.match(v):
            raise ValueError("must be a dotted Python identifier")
        return v


class Preconditions(BaseModel):
    hypothesis_category: str
    signals: dict[str, str] = Field(default_factory=dict)


class SkillTestCase(BaseModel):
    name: str
    input: str = Field(max_length=200_000)
    expected: str = Field(max_length=200_000)


class SkillSpec(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000)
    kind: SkillKind = SkillKind.TOKEN_RENAME
    params: TokenRenameParams
    languages: list[str] = Field(default_factory=lambda: ["python"])
    frameworks: list[str] = Field(default_factory=list)
    python_versions: str = ">=3.8"
    preconditions: Preconditions
    permissions: list[str] = Field(default_factory=lambda: ["project.apply_patch"])
    expected_effects: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    test_cases: list[SkillTestCase] = Field(min_length=1, max_length=50)
    dependencies: list[str] = Field(default_factory=list)
    rollback_strategy: str = "checkpoint_restore"
    origin: str = Field(default="human", max_length=200)

    def content_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class Skill(BaseModel):
    id: str
    version: int
    status: SkillStatus
    spec: SkillSpec
    content_hash: str
    created_at: str
    created_by: str
    updated_at: str


class ValidationResult(BaseModel):
    id: str
    skill_id: str
    skill_version: int
    passed: bool
    kind: str
    results: list[dict[str, Any]]
    content_hash: str
    created_at: str


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
            or "tests" in p.parts or "test" in p.parts)


def apply_token_rename(params: TokenRenameParams, text: str) -> tuple[str, int]:
    pattern = re.compile(r"(?<![\w.])" + re.escape(params.old) + r"(?!\w)")
    return pattern.subn(params.new, text)


def python_version_ok(spec: str, version: tuple[int, int]) -> bool:
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        m = re.fullmatch(r"(>=|<=|<|>|==)\s*(\d+)\.(\d+)", part)
        if not m:
            return False
        op, want = m.group(1), (int(m.group(2)), int(m.group(3)))
        ok = {">=": version >= want, "<=": version <= want, "<": version < want, ">": version > want,
              "==": version == want}[op]
        if not ok:
            return False
    return True


class SkillEngine:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- persistence -------------------------------------------------------------
    def _row(self, r: Any) -> Skill:
        return Skill(id=r["id"], version=r["version"], status=SkillStatus(r["status"]),
                     spec=SkillSpec(**json.loads(r["spec_json"])), content_hash=r["content_hash"],
                     created_at=r["created_at"], created_by=r["created_by"], updated_at=r["updated_at"])

    def get(self, skill_id: str, version: int | None = None) -> Skill:
        with self._db.read() as conn:
            if version is None:
                r = conn.execute("SELECT * FROM skills WHERE id = ? ORDER BY version DESC LIMIT 1",
                                 (skill_id,)).fetchone()
            else:
                r = conn.execute("SELECT * FROM skills WHERE id = ? AND version = ?", (skill_id, version)).fetchone()
        if r is None:
            raise NotFoundError("skill not found")
        return self._row(r)

    def list(self, status: SkillStatus | None = None) -> builtins.list[Skill]:
        with self._db.read() as conn:
            if status:
                rows = conn.execute("SELECT * FROM skills WHERE status = ? ORDER BY id, version", (status.value,))
            else:
                rows = conn.execute("SELECT * FROM skills ORDER BY id, version")
            return [self._row(r) for r in rows.fetchall()]

    def _set_status(self, s: Skill, new: SkillStatus, allowed_from: tuple[SkillStatus, ...]) -> Skill:
        with self._db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE skills SET status = ?, updated_at = ? WHERE id = ? AND version = ? AND status IN "  # noqa: S608
                f"({','.join('?' * len(allowed_from))})",
                (new.value, iso(), s.id, s.version, *[a.value for a in allowed_from]),
            )
            if cur.rowcount != 1:
                raise ConflictError(f"skill {s.id} v{s.version} is not in an allowed state for -> {new.value}")
        return self.get(s.id, s.version)

    # ---- lifecycle ---------------------------------------------------------------
    def propose(self, spec: SkillSpec, created_by: str, skill_id: str | None = None) -> Skill:
        """Create a new skill, or a new version of an existing skill (starts at PROPOSED)."""
        if not any(tc.input != tc.expected for tc in spec.test_cases):
            raise InvalidInputError("at least one test case must demonstrate a change")
        now = iso()
        with self._db.transaction() as conn:
            if skill_id:
                r = conn.execute("SELECT MAX(version) AS v FROM skills WHERE id = ?",
                                 (skill_id,)).fetchone()
                if r["v"] is None:
                    raise NotFoundError("skill not found")
                last = conn.execute("SELECT content_hash FROM skills WHERE id = ? AND version = ?",
                                    (skill_id, r["v"])).fetchone()
                if last["content_hash"] == spec.content_hash():
                    raise ConflictError("spec is identical to the latest version")
                version = int(r["v"]) + 1
            else:
                skill_id, version = new_id("skl"), 1
            conn.execute(
                "INSERT INTO skills(id, version, status, spec_json, content_hash, created_at, created_by, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (skill_id, version, SkillStatus.PROPOSED.value, spec.model_dump_json(), spec.content_hash(), now,
                 created_by, now),
            )
        return self.get(skill_id, version)

    def validate(self, skill_id: str, version: int | None = None) -> ValidationResult:
        s = self.get(skill_id, version)
        if s.status not in (SkillStatus.PROPOSED, SkillStatus.REJECTED, SkillStatus.VERIFIED, SkillStatus.ACTIVE):
            raise ConflictError(f"cannot validate a {s.status.value} skill")
        prior = s.status
        if prior in (SkillStatus.PROPOSED, SkillStatus.REJECTED):
            s = self._set_status(s, SkillStatus.VALIDATING, (SkillStatus.PROPOSED, SkillStatus.REJECTED))
        results: builtins.list[dict[str, Any]] = []
        integrity_ok = s.spec.content_hash() == s.content_hash
        if not integrity_ok:
            results.append({"case": "integrity", "passed": False, "detail": "stored spec does not match content hash"})
        for tc in s.spec.test_cases:
            try:
                out, n = apply_token_rename(s.spec.params, tc.input)
                ok = out == tc.expected
                detail = f"{n} replacement(s)" if ok else "output differs from expected"
            except Exception as exc:  # noqa: BLE001 - any failure of a skill is a validation failure
                ok, detail = False, f"error: {exc.__class__.__name__}"
            results.append({"case": tc.name, "passed": ok, "detail": detail})
        passed = integrity_ok and all(r["passed"] for r in results)
        vr = self._record_validation(s, passed, "validation" if prior not in (SkillStatus.ACTIVE,
                                                                               SkillStatus.VERIFIED) else "regression",
                                     results)
        if prior in (SkillStatus.PROPOSED, SkillStatus.REJECTED):
            self._set_status(s, SkillStatus.VERIFIED if passed else SkillStatus.REJECTED, (SkillStatus.VALIDATING,))
        elif not passed:
            # regression failure of a verified/active skill: take it out of service
            self._set_status(s, SkillStatus.DEPRECATED, (SkillStatus.VERIFIED, SkillStatus.ACTIVE))
        return vr

    def _record_validation(
        self, s: Skill, passed: bool, kind: str, results: builtins.list[dict[str, Any]]
    ) -> ValidationResult:
        vr = ValidationResult(id=new_id("svr"), skill_id=s.id, skill_version=s.version, passed=passed, kind=kind,
                              results=results, content_hash=s.content_hash, created_at=iso())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO skill_validation_runs(id, skill_id, skill_version, created_at, passed, results_json,"
                " content_hash) VALUES (?,?,?,?,?,?,?)",
                (vr.id, s.id, s.version, vr.created_at, int(passed), json.dumps({"kind": kind, "results": results}),
                 s.content_hash),
            )
        return vr

    def validations(self, skill_id: str, version: int | None = None) -> builtins.list[ValidationResult]:
        q = "SELECT * FROM skill_validation_runs WHERE skill_id = ?"
        args: builtins.list[Any] = [skill_id]
        if version is not None:
            q += " AND skill_version = ?"
            args.append(version)
        with self._db.read() as conn:
            rows = conn.execute(q + " ORDER BY created_at", args).fetchall()
        out = []
        for r in rows:
            body = json.loads(r["results_json"])
            out.append(ValidationResult(id=r["id"], skill_id=r["skill_id"], skill_version=r["skill_version"],
                                        passed=bool(r["passed"]), kind=body["kind"], results=body["results"],
                                        content_hash=r["content_hash"], created_at=r["created_at"]))
        return out

    def activation_digest(self, s: Skill) -> str:
        return digest({"skill": s.id, "version": s.version, "content_hash": s.content_hash})

    def check_activatable(self, skill_id: str, version: int) -> Skill:
        s = self.get(skill_id, version)
        if s.status is not SkillStatus.VERIFIED:
            raise PreconditionFailedError(f"only verified skills can be activated (status: {s.status.value})")
        vals = [v for v in self.validations(skill_id, version) if v.kind in ("validation", "regression")]
        if not vals or not vals[-1].passed or vals[-1].content_hash != s.content_hash:
            raise PreconditionFailedError("latest validation did not pass for the current skill content")
        return s

    def activate(self, skill_id: str, version: int) -> Skill:
        """Caller must have consumed a bound approval (Action.ACTIVATE_SKILL) first."""
        s = self.check_activatable(skill_id, version)
        # one active version per skill id
        with self._db.transaction() as conn:
            conn.execute("UPDATE skills SET status = ?, updated_at = ? WHERE id = ? AND status = ? AND version != ?",
                         (SkillStatus.DEPRECATED.value, iso(), skill_id, SkillStatus.ACTIVE.value, version))
        return self._set_status(s, SkillStatus.ACTIVE, (SkillStatus.VERIFIED,))

    def deprecate(self, skill_id: str, version: int) -> Skill:
        s = self.get(skill_id, version)
        return self._set_status(s, SkillStatus.DEPRECATED, tuple(x for x in SkillStatus if x is not
                                                                  SkillStatus.DEPRECATED))

    def record_field_outcome(self, skill_id: str, version: int, success: bool, evidence_id: str) -> Skill:
        s = self.get(skill_id, version)
        self._record_validation(s, success, "field", [{"case": "field_use", "passed": success,
                                                       "evidence_id": evidence_id}])
        stats = self.field_stats(skill_id, version)
        if s.status is SkillStatus.ACTIVE and stats["failures"] > stats["successes"]:
            return self._set_status(s, SkillStatus.DEPRECATED, (SkillStatus.ACTIVE,))
        return self.get(skill_id, version)

    def field_stats(self, skill_id: str, version: int) -> dict[str, float]:
        f = [v for v in self.validations(skill_id, version) if v.kind == "field"]
        succ = sum(1 for v in f if v.passed)
        fail = len(f) - succ
        # Laplace-smoothed success rate; with no field data, confidence stays at 0.5
        return {"successes": succ, "failures": fail, "confidence": round((succ + 1) / (succ + fail + 2), 3)}

    # ---- matching / compatibility -----------------------------------------------
    @staticmethod
    def matches(s: Skill, hypothesis_category: str, signals: dict[str, str]) -> bool:
        pre = s.spec.preconditions
        return pre.hypothesis_category == hypothesis_category and all(
            signals.get(k) == v for k, v in pre.signals.items())

    @staticmethod
    def compatible(s: Skill, languages: dict[str, int], python_version: tuple[int, int]) -> tuple[bool, str]:
        if "python" in s.spec.languages and not languages.get("python"):
            return False, "project has no Python files"
        if not python_version_ok(s.spec.python_versions, python_version):
            return False, f"python {python_version} outside {s.spec.python_versions}"
        return True, "compatible"
