"""Versioned HTTP API (/api/v1).

* Auth: ``Authorization: Bearer ydv_...`` (tokens hashed at rest). Only /health is public.
* Every mutating route goes through the same Guard/PolicyEngine as the CLI.
* Errors use one envelope: ``{"error": {"code": ..., "message": ...}}``. Internal
  exceptions are logged server-side and returned as a generic 500 (no stack traces).
* Bind to 127.0.0.1 by default (see cli ``serve``). There is no TLS termination here.
* The web UI (apps/web, built with ``npm run build``) is served as static files under /app with a
  strict Content-Security-Policy: same-origin scripts, styles, fonts and API calls only - no CDN,
  no inline script, no framing. It authenticates with the same bearer tokens as any API client.
"""

import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from yemendev import __version__
from yemendev.agents.registry import AGENTS
from yemendev.core.contracts import ErrorCode, InvalidInputError, YemenDevError
from yemendev.governance.actors import Actor, Role
from yemendev.governance.approvals import ApprovalStatus
from yemendev.governance.policy import Action
from yemendev.learning.skills import SkillSpec, SkillStatus
from yemendev.memory.model import MemoryCategory
from yemendev.observability.logs import request_id_var
from yemendev.observability.metrics import HTTP_DURATION, HTTP_REQUESTS, REGISTRY
from yemendev.observability.tracing import extract, inject, tracer
from yemendev.orion.queue import QueueOptions
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import RepairPlan
from yemendev.security.ratelimit import TokenBucketLimiter
from yemendev.simulation.counterfactual import Scenario
from yemendev.twin.graph import DependencyGraph

log = logging.getLogger("yemendev.api")
access_log = logging.getLogger("yemendev.access")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
MAX_BODY_BYTES = 5_000_000
DEFAULT_WEB_DIR = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"
# The UI needs nothing but its own origin. connect-src 'self' also means a compromised page cannot
# exfiltrate the token to another host via fetch/XHR/WebSocket.
UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; "
          "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
API_CSP = "default-src 'none'; frame-ancestors 'none'"

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


def _too_many(wait: float, message: str) -> JSONResponse:
    r = _err(429, "rate_limited", message)
    r.headers["Retry-After"] = str(max(1, math.ceil(wait)))
    return r


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class RegisterProject(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=4096)


class CreateTask(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="repair", pattern="^repair$")
    plan: RepairPlan | None = None
    queue: QueueOptions | None = Field(default=None, description="run in the background via the ORION queue")


class Decide(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=2000)


class ProposeSkill(BaseModel):
    spec: SkillSpec
    skill_id: str | None = None


class ResolveConflict(BaseModel):
    resolution: str = Field(pattern="^(keep_local|take_remote)$")


class TrustKey(BaseModel):
    public_key: str = Field(min_length=64, max_length=64)


class GrantMember(BaseModel):
    actor_ref: str = Field(min_length=3, max_length=140, description="e.g. 'human:bob'")
    role: Role


def create_app(platform: Platform, clock: Callable[[], float] = time.monotonic,
               workers: Callable[[], dict[str, Any]] | None = None) -> FastAPI:
    """``workers`` reports the background workers running in this process (``yemendev serve``)."""
    app = FastAPI(title="YEMENDEV AI", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  redoc_url=None)
    wf = RepairWorkflow(platform)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        resp = await call_next(request)
        path = request.url.path
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # Content-hashed build assets may be cached; everything else (API data, index.html) may not.
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if path.startswith("/app/assets/")
                                         else "no-store")
        if path == "/app" or path.startswith("/app/"):
            resp.headers["Content-Security-Policy"] = UI_CSP
        elif not path.startswith("/api/docs"):  # Swagger UI (developer aid) loads its own assets
            resp.headers["Content-Security-Policy"] = API_CSP
        return resp

    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)

    st = platform.settings
    limiter = TokenBucketLimiter(st.rate_limit_per_minute, st.rate_limit_burst, clock=clock)
    auth_failures = TokenBucketLimiter(st.auth_failures_per_minute, st.auth_failures_per_minute, clock=clock)

    # Registered last => outermost: throttled requests are rejected before their body is read.
    @app.middleware("http")
    async def rate_limit(request: Request, call_next: Any) -> Any:
        client = request.client.host if request.client else "unknown"
        ok, wait = auth_failures.peek(client)
        if not ok:
            return _too_many(wait, "too many failed authentication attempts")
        auth = request.headers.get("authorization", "")
        key = "cred:" + hashlib.sha256(auth.encode()).hexdigest() if auth else "ip:" + client
        ok, wait = limiter.allow(key)
        if not ok:
            return _too_many(wait, "rate limit exceeded")
        resp = await call_next(request)
        if resp.status_code == 401:
            auth_failures.allow(client)
        return resp

    # Registered last => outermost: every request (also rate-limited ones) gets an id, a trace span, an access
    # log line and metrics. Labels use the route *template*, never raw paths or ids.
    @app.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status, route = 500, "unmatched"
        try:
            with tracer.start_as_current_span(f"{request.method} {request.url.path}", context=extract(
                    dict(request.headers)), kind=SpanKind.SERVER) as span:
                resp = await call_next(request)
                status = resp.status_code
                r = request.scope.get("route")
                route = getattr(r, "path", None) or ("/app/*" if request.url.path.startswith("/app") else "unmatched")
                span.update_name(f"{request.method} {route}")
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                resp.headers["X-Request-ID"] = rid
                for k, v in inject().items():  # W3C traceparent for correlation by the caller
                    resp.headers[k] = v
                return resp
        finally:
            dur = time.perf_counter() - start
            HTTP_REQUESTS.inc(request.method, route, str(status))
            HTTP_DURATION.observe(dur, request.method, route)
            access_log.info("request", extra={"method": request.method, "route": route, "status": status,
                                              "duration_ms": round(dur * 1000, 2),
                                              "client": request.client.host if request.client else None})
            request_id_var.reset(token)

    def _state_gauges() -> None:
        def q(sql: str) -> list[tuple[tuple[str, ...], float]]:
            with platform.db.read() as conn:
                return [((str(k),), float(v)) for k, v in conn.execute(sql)]
        REGISTRY.gauge("yemendev_queue_entries", "ORION queue entries by state", ("state",),
                       lambda: q("SELECT state, COUNT(*) FROM task_queue GROUP BY state"))
        REGISTRY.gauge("yemendev_tasks", "Tasks by status", ("status",),
                       lambda: q("SELECT status, COUNT(*) FROM tasks GROUP BY status"))
        REGISTRY.gauge("yemendev_approvals", "Approval requests by status", ("status",),
                       lambda: q("SELECT status, COUNT(*) FROM approval_requests GROUP BY status"))
        REGISTRY.gauge("yemendev_advisories", "Imported vulnerability advisories by ecosystem", ("ecosystem",),
                       lambda: q("SELECT ecosystem, COUNT(*) FROM advisories GROUP BY ecosystem"))
    _state_gauges()

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
        """Global (non-project) read: system status, skills, models."""
        platform.guard.require(a, Action.READ, target)

    def read_audit(a: Actor) -> None:
        platform.guard.require(a, Action.READ_AUDIT, "audit")

    # ---- system --------------------------------------------------------------------
    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "db": platform.db.integrity_check()}

    @app.get("/api/v1/metrics", response_class=PlainTextResponse)
    def metrics(a: Who) -> PlainTextResponse:
        read(a, "system")
        return PlainTextResponse(REGISTRY.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

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
        return [p.model_dump() for p in platform.visible_projects(a)]

    @app.get("/api/v1/projects/{project_id}")
    def project(project_id: str, a: Who) -> dict[str, Any]:
        return platform.require_read(a, project_id).model_dump()

    @app.get("/api/v1/projects/{project_id}/members")
    def members(project_id: str, a: Who) -> list[Any]:
        return [m.model_dump() for m in platform.list_members(a, project_id)]

    @app.post("/api/v1/projects/{project_id}/members", status_code=201)
    def grant(project_id: str, body: GrantMember, a: Who) -> dict[str, Any]:
        return platform.grant_member(a, project_id, body.actor_ref, body.role).model_dump()

    @app.delete("/api/v1/projects/{project_id}/members/{actor_ref}", status_code=204)
    def revoke(project_id: str, actor_ref: str, a: Who) -> None:
        platform.revoke_member(a, project_id, actor_ref)

    @app.post("/api/v1/projects/{project_id}/scan")
    def scan(project_id: str, a: Who) -> dict[str, Any]:
        sid, snap = platform.scan_project(a, project_id)
        return {"scan_id": sid, "tree_hash": snap.tree_hash, "files": len(snap.files), "modules": len(snap.modules),
                "languages": snap.languages, "findings": [f.model_dump() for f in snap.findings],
                "sensitive_files": snap.sensitive_files, "skipped": snap.skipped}

    @app.post("/api/v1/projects/{project_id}/sandbox-image")
    def project_image(project_id: str, a: Who) -> dict[str, Any]:
        return dict(platform.build_project_image(a, project_id).model_dump())

    @app.get("/api/v1/projects/{project_id}/risks")
    def risks(project_id: str, a: Who) -> dict[str, Any]:
        return dict(platform.assess_risk(a, project_id).model_dump())

    @app.get("/api/v1/projects/{project_id}/immune")
    def immune(project_id: str, a: Who) -> dict[str, Any]:
        return dict(platform.immune_check(a, project_id).model_dump())

    @app.get("/api/v1/projects/{project_id}/continuity-debt")
    def continuity_debt(project_id: str, a: Who) -> dict[str, Any]:
        return dict(platform.continuity_debt(a, project_id).model_dump())

    @app.post("/api/v1/projects/{project_id}/counterfactual")
    def counterfactual(project_id: str, body: Scenario, a: Who) -> dict[str, Any]:
        return dict(platform.counterfactual(a, project_id, body).model_dump())

    # ---- continuity: passports & synchronization ---------------------------------------------
    @app.get("/api/v1/projects/{project_id}/passport")
    def passport(project_id: str, a: Who) -> dict[str, Any]:
        return dict(platform.export_passport(a, project_id).model_dump())

    @app.post("/api/v1/passports/verify")
    def passport_verify(body: dict[str, Any], a: Who) -> dict[str, Any]:
        result, _ = platform.verify_passport(a, json.dumps(body))
        return {**result.model_dump(), "accepted": result.accepted}

    @app.post("/api/v1/projects/{project_id}/passport/import")
    def passport_import(project_id: str, body: dict[str, Any], a: Who) -> dict[str, Any]:
        r = platform.import_passport(a, project_id, json.dumps(body))
        return {**r.model_dump(), "accepted": r.accepted}

    @app.post("/api/v1/projects/{project_id}/sync/export")
    def sync_export(project_id: str, a: Who) -> dict[str, Any]:
        bundle, job = platform.sync_export(a, project_id)
        return {"job": job.model_dump(), "bundle": bundle.model_dump()}

    @app.post("/api/v1/projects/{project_id}/sync/import")
    def sync_import(project_id: str, body: dict[str, Any], a: Who) -> dict[str, Any]:
        return dict(platform.sync_import(a, project_id, json.dumps(body)).model_dump())

    @app.get("/api/v1/projects/{project_id}/sync/conflicts")
    def sync_conflicts(project_id: str, a: Who, status: str | None = None) -> list[Any]:
        return [c.model_dump() for c in platform.sync_conflicts(a, project_id, status)]

    @app.post("/api/v1/sync/conflicts/{conflict_id}/resolve")
    def sync_resolve(conflict_id: str, body: ResolveConflict, a: Who) -> dict[str, Any]:
        return dict(platform.resolve_sync_conflict(a, conflict_id, body.resolution).model_dump())

    @app.get("/api/v1/synchronization/status")
    def sync_status(a: Who) -> dict[str, Any]:
        return platform.sync_status(a)

    @app.get("/api/v1/synchronization/jobs")
    def sync_jobs(a: Who) -> list[Any]:
        return list(platform.sync_status(a)["jobs"])

    @app.post("/api/v1/keys/trusted", status_code=201)
    def trust_key(body: TrustKey, a: Who) -> dict[str, Any]:
        return {"fingerprint": platform.trust_key(a, body.public_key)}

    @app.get("/api/v1/keys/self")
    def own_key(a: Who) -> dict[str, Any]:
        from yemendev.continuity.signing import fingerprint, public_hex

        read(a, "keys")
        pub = public_hex(platform.signing_key)
        return {"public_key": pub, "fingerprint": fingerprint(pub), "algorithm": "ed25519"}

    @app.get("/api/v1/projects/{project_id}/impact")
    def impact(project_id: str, a: Who, module: Annotated[str, Query(min_length=1, max_length=500)]) -> dict[str, Any]:
        return platform.impact(a, project_id, module)

    @app.get("/api/v1/projects/{project_id}/sbom")
    def sbom(project_id: str, a: Who) -> dict[str, Any]:
        return platform.project_sbom(a, project_id)

    @app.get("/api/v1/continuity/backups")
    def backups(a: Who) -> list[dict[str, Any]]:
        return platform.list_backups(a)

    @app.post("/api/v1/continuity/backups", status_code=201)
    def create_backup(a: Who) -> dict[str, Any]:
        return platform.create_backup(a)

    @app.post("/api/v1/continuity/backups/{backup_id}/verify")
    def verify_backup(backup_id: str, a: Who) -> dict[str, Any]:
        return platform.verify_backup(a, backup_id)

    @app.get("/api/v1/continuity/readiness")
    def readiness(a: Who) -> dict[str, Any]:
        return platform.recovery_readiness(a)

    @app.get("/api/v1/projects/{project_id}/checkpoints")
    def checkpoints(project_id: str, a: Who) -> list[dict[str, Any]]:
        return platform.list_checkpoints(a, project_id)

    @app.get("/api/v1/projects/{project_id}/vulnerabilities")
    def vulnerabilities(project_id: str, a: Who) -> dict[str, Any]:
        return dict(platform.vulnerabilities(a, project_id).model_dump())

    @app.get("/api/v1/system/vulnerabilities")
    def system_vulnerabilities(a: Who) -> dict[str, Any]:
        return dict(platform.platform_vulnerabilities(a).model_dump())

    @app.get("/api/v1/research/benchmarks")
    def research_benchmarks(a: Who) -> list[dict[str, Any]]:
        return platform.benchmark_reports(a)

    @app.get("/api/v1/research/benchmarks/{source}/{name}")
    def research_benchmark(source: str, name: str, a: Who) -> dict[str, Any]:
        return platform.benchmark_report(a, source, name)

    @app.get("/api/v1/advisories/status")
    def advisories_status(a: Who) -> dict[str, Any]:
        return platform.advisory_status(a)

    @app.get("/api/v1/system/sbom")
    def system_sbom(a: Who) -> dict[str, Any]:
        return platform.platform_sbom(a)

    @app.get("/api/v1/projects/{project_id}/graph")
    def graph(project_id: str, a: Who) -> dict[str, Any]:
        platform.require_read(a, project_id)
        sid, snap = platform.latest_scan(project_id)
        return {"scan_id": sid, **DependencyGraph.from_snapshot(snap).report().model_dump()}

    @app.get("/api/v1/projects/{project_id}/memory")
    def memory(project_id: str, a: Who, category: MemoryCategory | None = None,
               include_inactive: bool = False, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[Any]:
        platform.require_read(a, project_id)
        return [r.model_dump() for r in platform.memory.query(project_id, category=category,
                                                               include_inactive=include_inactive, limit=limit)]

    # ---- tasks ---------------------------------------------------------------------
    @app.post("/api/v1/tasks", status_code=201)
    def create_task(body: CreateTask, a: Who) -> dict[str, Any]:
        t = wf.create(a, body.project_id, human_plan=body.plan)
        queued = wf.enqueue(a, t.id, body.queue).model_dump() if body.queue is not None else None
        return {**t.model_dump(), "queue": queued}

    @app.get("/api/v1/tasks")
    def tasks(a: Who, project_id: str | None = None) -> list[Any]:
        ts = wf.visible_tasks(a, project_id=project_id)
        qs = wf.queue.states_for([t.id for t in ts])
        return [{**t.model_dump(), "queue_state": qs[t.id].state.value if t.id in qs else None} for t in ts]

    @app.post("/api/v1/tasks/{task_id}/enqueue", status_code=201)
    def enqueue(task_id: str, body: QueueOptions, a: Who) -> dict[str, Any]:
        return wf.enqueue(a, task_id, body).model_dump()

    @app.get("/api/v1/tasks/{task_id}/trace")
    def trace(task_id: str, a: Who) -> dict[str, Any]:
        return wf.trace(a, task_id)

    @app.get("/api/v1/queue")
    def queue(a: Who, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
        read(a, "tasks")
        entries = [e.model_dump() for e in wf.queue.list(limit=limit) if platform.guard.can_read(a, e.project_id)]
        o = platform.settings.orion
        return {"entries": entries,
                "limits": {"max_concurrent_runs": o.max_concurrent_runs, "default_timeout_seconds":
                           o.run_timeout_seconds, "default_max_attempts": o.max_attempts,
                           "retry_base_seconds": o.retry_base_seconds, "retry_max_seconds": o.retry_max_seconds},
                "workers_in_this_process": workers() if workers else {"count": 0, "running": False,
                                                                       "note": "no workers in this process"}}

    @app.get("/api/v1/tasks/{task_id}")
    def task(task_id: str, a: Who) -> dict[str, Any]:
        t = wf.get_task(a, task_id)
        return {**t.model_dump(), "steps": [{k: v for k, v in s.model_dump().items() if k != "output"}
                                            for s in wf.tasks.steps(task_id)]}

    @app.get("/api/v1/tasks/{task_id}/steps/{name}")
    def task_step(task_id: str, name: str, a: Who) -> dict[str, Any]:
        wf.get_task(a, task_id)
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
        return [x.model_dump() for x in platform.visible_approvals(a, status=status)]

    @app.get("/api/v1/approvals/{approval_id}")
    def approval(approval_id: str, a: Who) -> dict[str, Any]:
        return platform.get_approval(a, approval_id).model_dump()

    @app.post("/api/v1/approvals/{approval_id}")
    def decide(approval_id: str, body: Decide, a: Who) -> dict[str, Any]:
        return platform.decide_approval(a, approval_id, body.approve, body.reason).model_dump()

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
        return [e.model_dump() for e in platform.visible_evidence(a, task_id=task_id, limit=limit)]

    @app.get("/api/v1/evidence/{evidence_id}")
    def evidence_one(evidence_id: str, a: Who) -> dict[str, Any]:
        return platform.get_evidence(a, evidence_id).model_dump()

    @app.get("/api/v1/audit")
    def audit(a: Who, after_seq: Annotated[int, Query(ge=0)] = 0,
              limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> list[Any]:
        read_audit(a)
        return [e.model_dump() for e in platform.audit.list(limit=limit, after_seq=after_seq)]

    @app.get("/api/v1/audit/verify")
    def audit_verify(a: Who) -> dict[str, Any]:
        read_audit(a)
        return platform.audit.verify().model_dump()

    @app.get("/api/v1/integrity/verify")
    def integrity_verify(a: Who) -> dict[str, Any]:
        read_audit(a)
        return platform.verify_integrity()

    # ---- agents (registry + traceable runs) --------------------------------------------
    @app.get("/api/v1/agents")
    def agents(a: Who) -> list[Any]:
        read(a, "agents")
        return [{"name": s.name, "responsibility": s.responsibility, "actor": s.actor.ref,
                 "allowed_actions": sorted(x.value for x in s.allowed_actions)} for s in AGENTS.values()]

    @app.get("/api/v1/agents/{name}/runs")
    def agent_runs(name: str, a: Who, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> list[Any]:
        read(a, "agents")
        platform.agents.spec(name)
        return [r.model_dump() for r in platform.agents.runs(agent=name, limit=2000)
                if r.project_id and platform.guard.can_read(a, r.project_id)][-limit:]

    @app.get("/api/v1/tasks/{task_id}/agent-runs")
    def task_agent_runs(task_id: str, a: Who) -> list[Any]:
        wf.get_task(a, task_id)
        return [r.model_dump() for r in platform.agents.runs(task_id=task_id)]

    @app.get("/api/v1/models")
    def models(a: Who) -> list[Any]:
        read(a, "models")
        return platform.models.status()

    # ---- web UI (static, public: holds no data; every value it shows comes from the API) ----------
    web_dir = platform.settings.web_dir or DEFAULT_WEB_DIR
    if (web_dir / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/app/")

        app.mount("/app", StaticFiles(directory=web_dir, html=True), name="web")
    else:
        @app.get("/app", include_in_schema=False)
        @app.get("/app/{_path:path}", include_in_schema=False)
        def ui_not_built(_path: str = "") -> JSONResponse:
            return _err(404, "not_found", "web UI is not built: run `npm ci && npm run build` in apps/web")

    return app
