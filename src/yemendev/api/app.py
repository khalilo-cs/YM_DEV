"""Versioned HTTP API (/api/v1).

* Auth: ``Authorization: Bearer ydv_...`` (tokens hashed at rest). Only /health is public.
* Every mutating route goes through the same Guard/PolicyEngine as the CLI.
* Errors use one envelope: ``{"error": {"code": ..., "message": ...}}``. Internal
  exceptions are logged server-side and returned as a generic 500 (no stack traces).
* Bind to 127.0.0.1 by default (see cli ``serve``). There is no TLS termination here.
"""

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from yemendev import __version__
from yemendev.core.contracts import ErrorCode, InvalidInputError, YemenDevError
from yemendev.governance.actors import Actor
from yemendev.governance.approvals import ApprovalStatus
from yemendev.governance.policy import Action
from yemendev.learning.skills import SkillSpec, SkillStatus
from yemendev.memory.model import MemoryCategory
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import RepairPlan
from yemendev.twin.graph import DependencyGraph

log = logging.getLogger("yemendev.api")
MAX_BODY_BYTES = 5_000_000

STATUS_FOR = {
    ErrorCode.NOT_FOUND: 404, ErrorCode.INVALID_INPUT: 422, ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.APPROVAL_REQUIRED: 403, ErrorCode.CONFLICT: 409, ErrorCode.PRECONDITION_FAILED: 412,
    ErrorCode.EXECUTION_FAILED: 500, ErrorCode.UNAVAILABLE: 503, ErrorCode.INTEGRITY_ERROR: 500,
    ErrorCode.INTERNAL: 500,
}


class BodySizeLimit:
    """ASGI middleware enforcing a request-body limit for *both* Content-Length and chunked bodies.

    Regression: a header-only check let ``Transfer-Encoding: chunked`` bodies of any size through.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for k, v in scope.get("headers", []):
            if k == b"content-length" and (not v.isdigit() or int(v) > self.max_bytes):
                await _send_413(send)
                return
        # Read the body up front (bounded by max_bytes), then replay it to the app.
        chunks: list[bytes] = []
        seen = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    await _send_413(send)
                    return
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _send_413(send: Any) -> None:
    body = b'{"error":{"code":"invalid_input","message":"request body too large"}}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class RegisterProject(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=4096)


class CreateTask(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="repair", pattern="^repair$")
    plan: RepairPlan | None = None


class Decide(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=2000)


class ProposeSkill(BaseModel):
    spec: SkillSpec
    skill_id: str | None = None


def create_app(platform: Platform) -> FastAPI:
    app = FastAPI(title="YEMENDEV AI", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  redoc_url=None)
    wf = RepairWorkflow(platform)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)

    @app.exception_handler(YemenDevError)
    async def _app_error(_: Request, exc: YemenDevError) -> JSONResponse:
        return _err(STATUS_FOR.get(exc.code, 500), exc.code.value, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()][:20]
        return _err(422, "invalid_input", f"request validation failed: {', '.join(fields)}")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return _err(500, "internal", "internal error")

    class _AuthError(YemenDevError):
        pass

    def actor(authorization: Annotated[str | None, Header()] = None) -> Actor:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _AuthError("unauthenticated")
        a = platform.tokens.authenticate(authorization[7:].strip())
        if a is None:
            raise _AuthError("unauthenticated")
        return a

    @app.exception_handler(_AuthError)
    async def _auth(_: Request, __: _AuthError) -> JSONResponse:
        r = _err(401, "unauthenticated", "missing or invalid bearer token")
        r.headers["WWW-Authenticate"] = "Bearer"
        return r

    Who = Annotated[Actor, Depends(actor)]

    def read(a: Actor, target: str) -> None:
        platform.guard.require(a, Action.READ, target)

    # ---- system --------------------------------------------------------------------
    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "db": platform.db.integrity_check()}

    @app.get("/api/v1/system/status")
    def system_status(a: Who) -> dict[str, Any]:
        read(a, "system")
        return platform.status()

    # ---- projects ------------------------------------------------------------------
    @app.post("/api/v1/projects", status_code=201)
    def register(body: RegisterProject, a: Who) -> dict[str, Any]:
        return platform.register_project(a, body.name, body.path).model_dump()

    @app.get("/api/v1/projects")
    def projects(a: Who) -> list[dict[str, Any]]:
        read(a, "projects")
        return [p.model_dump() for p in platform.projects()]

    @app.get("/api/v1/projects/{project_id}")
    def project(project_id: str, a: Who) -> dict[str, Any]:
        read(a, project_id)
        return platform.project(project_id).model_dump()

    @app.post("/api/v1/projects/{project_id}/scan")
    def scan(project_id: str, a: Who) -> dict[str, Any]:
        sid, snap = platform.scan_project(a, project_id)
        return {"scan_id": sid, "tree_hash": snap.tree_hash, "files": len(snap.files), "modules": len(snap.modules),
                "languages": snap.languages, "findings": [f.model_dump() for f in snap.findings],
                "sensitive_files": snap.sensitive_files, "skipped": snap.skipped}

    @app.get("/api/v1/projects/{project_id}/graph")
    def graph(project_id: str, a: Who) -> dict[str, Any]:
        read(a, project_id)
        sid, snap = platform.latest_scan(project_id)
        return {"scan_id": sid, **DependencyGraph.from_snapshot(snap).report().model_dump()}

    @app.get("/api/v1/projects/{project_id}/memory")
    def memory(project_id: str, a: Who, category: MemoryCategory | None = None,
               include_inactive: bool = False, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[Any]:
        read(a, project_id)
        platform.project(project_id)
        return [r.model_dump() for r in platform.memory.query(project_id, category=category,
                                                               include_inactive=include_inactive, limit=limit)]

    # ---- tasks ---------------------------------------------------------------------
    @app.post("/api/v1/tasks", status_code=201)
    def create_task(body: CreateTask, a: Who) -> dict[str, Any]:
        return wf.create(a, body.project_id, human_plan=body.plan).model_dump()

    @app.get("/api/v1/tasks")
    def tasks(a: Who, project_id: str | None = None) -> list[Any]:
        read(a, "tasks")
        return [t.model_dump() for t in wf.tasks.list(project_id=project_id)]

    @app.get("/api/v1/tasks/{task_id}")
    def task(task_id: str, a: Who) -> dict[str, Any]:
        read(a, task_id)
        t = wf.tasks.get(task_id)
        return {**t.model_dump(), "steps": [{k: v for k, v in s.model_dump().items() if k != "output"}
                                            for s in wf.tasks.steps(task_id)]}

    @app.get("/api/v1/tasks/{task_id}/steps/{name}")
    def task_step(task_id: str, name: str, a: Who) -> dict[str, Any]:
        read(a, task_id)
        s = wf.tasks.step(task_id, name)
        if s is None:
            raise InvalidInputError("step has not run")
        return s.model_dump()

    @app.post("/api/v1/tasks/{task_id}/advance")
    def advance(task_id: str, a: Who) -> dict[str, Any]:
        r = wf.advance(a, task_id)
        return {"task": r.task.model_dump(), "ran_steps": r.ran_steps, "message": r.message}

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str, a: Who) -> dict[str, Any]:
        return wf.cancel(a, task_id).model_dump()

    # ---- approvals -----------------------------------------------------------------
    @app.get("/api/v1/approvals")
    def approvals(a: Who, status: ApprovalStatus | None = None) -> list[Any]:
        read(a, "approvals")
        return [x.model_dump() for x in platform.approvals.list(status=status)]

    @app.get("/api/v1/approvals/{approval_id}")
    def approval(approval_id: str, a: Who) -> dict[str, Any]:
        read(a, approval_id)
        return platform.approvals.get(approval_id).model_dump()

    @app.post("/api/v1/approvals/{approval_id}")
    def decide(approval_id: str, body: Decide, a: Who) -> dict[str, Any]:
        r = platform.approvals.decide(approval_id, a, body.approve, body.reason)
        platform.audit.record(actor=a.ref, action=Action.DECIDE_APPROVAL.value, target=approval_id,
                              outcome=r.status.value, details={"reason": body.reason})
        return r.model_dump()

    # ---- skills --------------------------------------------------------------------
    @app.get("/api/v1/skills")
    def skills(a: Who, status: SkillStatus | None = None) -> list[Any]:
        read(a, "skills")
        return [s.model_dump() for s in platform.skills.list(status)]

    @app.post("/api/v1/skills", status_code=201)
    def propose_skill(body: ProposeSkill, a: Who) -> dict[str, Any]:
        platform.guard.require(a, Action.PROPOSE_SKILL, body.skill_id or "new")
        s = platform.skills.propose(body.spec, a.ref, skill_id=body.skill_id)
        platform.audit.record(actor=a.ref, action=Action.PROPOSE_SKILL.value, target=s.id, outcome="ok")
        return s.model_dump()

    @app.get("/api/v1/skills/{skill_id}")
    def skill(skill_id: str, a: Who) -> dict[str, Any]:
        read(a, skill_id)
        s = platform.skills.get(skill_id)
        return {**s.model_dump(), "validations": [v.model_dump() for v in platform.skills.validations(skill_id)],
                "field_stats": platform.skills.field_stats(skill_id, s.version)}

    @app.post("/api/v1/skills/{skill_id}/validate")
    def validate_skill(skill_id: str, a: Who) -> dict[str, Any]:
        platform.guard.require(a, Action.VALIDATE_SKILL, skill_id)
        vr = platform.skills.validate(skill_id)
        platform.audit.record(actor=a.ref, action=Action.VALIDATE_SKILL.value, target=skill_id,
                              outcome="passed" if vr.passed else "failed")
        return vr.model_dump()

    @app.post("/api/v1/skills/{skill_id}/activation-requests", status_code=201)
    def request_activation(skill_id: str, a: Who) -> dict[str, Any]:
        s = platform.skills.get(skill_id)
        return platform.request_skill_activation(a, skill_id, s.version).model_dump()

    @app.post("/api/v1/skills/activate/{approval_id}")
    def activate(approval_id: str, a: Who) -> dict[str, Any]:
        return platform.activate_skill(a, approval_id).model_dump()

    # ---- evidence & audit ----------------------------------------------------------
    @app.get("/api/v1/evidence")
    def evidence(a: Who, task_id: str | None = None, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> list[Any]:
        read(a, "evidence")
        return [e.model_dump() for e in platform.evidence.list(task_id=task_id, limit=limit)]

    @app.get("/api/v1/evidence/{evidence_id}")
    def evidence_one(evidence_id: str, a: Who) -> dict[str, Any]:
        read(a, evidence_id)
        return platform.evidence.get(evidence_id).model_dump()

    @app.get("/api/v1/audit")
    def audit(a: Who, after_seq: Annotated[int, Query(ge=0)] = 0,
              limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> list[Any]:
        read(a, "audit")
        return [e.model_dump() for e in platform.audit.list(limit=limit, after_seq=after_seq)]

    @app.get("/api/v1/audit/verify")
    def audit_verify(a: Who) -> dict[str, Any]:
        read(a, "audit")
        return platform.audit.verify().model_dump()

    @app.get("/api/v1/models")
    def models(a: Who) -> list[Any]:
        read(a, "models")
        return platform.models.status()

    return app
