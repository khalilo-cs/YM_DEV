"""Command-line interface. Uses the same service layer and token auth as the HTTP API.

Authentication: every command except ``init`` and ``health`` needs a token in
``YEMENDEV_TOKEN`` (or ``--token``). ``init`` creates the first admin token.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from yemendev.core.config import Settings
from yemendev.core.contracts import YemenDevError
from yemendev.governance.actors import Actor, Role
from yemendev.governance.policy import Action
from yemendev.orion.queue import QueueOptions
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import RepairPlan
from yemendev.twin.graph import DependencyGraph


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _platform(args: argparse.Namespace) -> Platform:
    return Platform(Settings.from_env(Path(args.data_dir) if args.data_dir else None))


def _actor(p: Platform, args: argparse.Namespace) -> Actor:
    tok = args.token or os.environ.get("YEMENDEV_TOKEN")
    if not tok:
        raise SystemExit("error: set YEMENDEV_TOKEN (run `yemendev init` to create the first admin token)")
    a = p.tokens.authenticate(tok)
    if a is None:
        raise SystemExit("error: invalid or revoked token")
    return a


def cmd_init(args: argparse.Namespace) -> int:
    p = _platform(args)
    with p.db.read() as conn:
        n = conn.execute("SELECT COUNT(*) FROM api_tokens WHERE revoked_at IS NULL").fetchone()[0]
    if n and not args.force:
        print("already initialised (tokens exist). Use `token issue` with an admin token, or --force.")
        return 1
    actor_id = args.actor or getpass.getuser()
    tid, tok = p.tokens.issue(actor_id, Role.ADMIN, ttl_seconds=p.settings.token_ttl_days * 86400)
    p.audit.record(actor=f"human:{actor_id}", action="token.issue", target=tid, outcome="ok",
                   details={"role": "admin", "via": "init"})
    print(f"data dir: {p.settings.data_dir}")
    print(f"admin token for '{actor_id}' (shown once, store it securely):\n{tok}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    p = _platform(args)
    _print({"db": p.db.integrity_check(), "schema_version": p.db.schema_version()})
    return 0


def cmd_token_issue(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    p.guard.require(a, Action.ISSUE_TOKEN, args.actor_id)
    tid, tok = p.tokens.issue(args.actor_id, Role(args.role), ttl_seconds=args.ttl_days * 86400)
    p.audit.record(actor=a.ref, action=Action.ISSUE_TOKEN.value, target=tid, outcome="ok",
                   details={"for": args.actor_id, "role": args.role, "ttl_days": args.ttl_days})
    print(f"token for '{args.actor_id}' ({args.role}), shown once:\n{tok}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    p.guard.require(a, Action.READ, "system")
    _print(p.status())
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    if args.pcmd == "add":
        _print(p.register_project(a, args.name, str(Path(args.path).absolute())).model_dump())
    elif args.pcmd == "list":
        _print([x.model_dump() for x in p.visible_projects(a)])
    elif args.pcmd == "scan":
        sid, snap = p.scan_project(a, args.project_id)
        _print({"scan_id": sid, "files": len(snap.files), "modules": len(snap.modules), "languages": snap.languages,
                "findings": [f.model_dump() for f in snap.findings], "sensitive_files": snap.sensitive_files})
    elif args.pcmd == "graph":
        p.require_read(a, args.project_id)
        _, snap = p.latest_scan(args.project_id)
        _print(DependencyGraph.from_snapshot(snap).report().model_dump())
    elif args.pcmd == "grant":
        _print(p.grant_member(a, args.project_id, args.actor_ref, Role(args.role)).model_dump())
    elif args.pcmd == "revoke":
        p.revoke_member(a, args.project_id, args.actor_ref)
        print("revoked")
    elif args.pcmd == "whatif":
        from yemendev.simulation.counterfactual import DependencyRemoved, FileCorrupted, ModuleUnavailable, Scenario

        faults = ([ModuleUnavailable(module=m) for m in args.module or []]
                  + [DependencyRemoved(import_name=d) for d in args.dependency or []]
                  + [FileCorrupted(path=c) for c in args.corrupt or []])
        if not faults:
            raise SystemExit("error: give at least one --module, --dependency or --corrupt")
        _print(p.counterfactual(a, args.project_id, Scenario(name=args.name, faults=faults)).model_dump())
    elif args.pcmd == "immune":
        _print(p.immune_check(a, args.project_id).model_dump())
    elif args.pcmd == "debt":
        _print(p.continuity_debt(a, args.project_id).model_dump())
    elif args.pcmd == "risks":
        _print(p.assess_risk(a, args.project_id).model_dump())
    elif args.pcmd == "impact":
        _print(p.impact(a, args.project_id, args.module))
    elif args.pcmd == "vulns":
        _print(p.vulnerabilities(a, args.project_id).model_dump())
    elif args.pcmd == "sbom":
        _write_or_print(p.project_sbom(a, args.project_id), args.out)
    elif args.pcmd == "members":
        _print([m.model_dump() for m in p.list_members(a, args.project_id)])
    return 0


def _queue_opts(args: argparse.Namespace) -> QueueOptions:
    return QueueOptions(priority=args.priority, delay_seconds=args.delay, max_attempts=args.max_attempts,
                        timeout_seconds=args.timeout, depends_on=args.after or [])


def _write_or_print(doc: Any, out: str | None) -> None:
    if out:
        Path(out).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        print(f"written: {out}", file=sys.stderr)
    else:
        _print(doc)


def cmd_advisories(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    if args.acmd == "import":
        _print(p.import_advisories(a, args.path).model_dump())
    else:
        _print(p.advisory_status(a))
    return 0


def cmd_vulns(args: argparse.Namespace) -> int:
    p = _platform(args)
    _print(p.platform_vulnerabilities(_actor(p, args)).model_dump())
    return 0


def cmd_sbom(args: argparse.Namespace) -> int:
    p = _platform(args)
    _write_or_print(p.platform_sbom(_actor(p, args)), args.out)
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    wf = RepairWorkflow(p)
    if args.tcmd == "repair":
        plan = RepairPlan(**json.loads(Path(args.plan).read_text())) if args.plan else None
        t = wf.create(a, args.project_id, human_plan=plan)
        if args.queue:
            e = wf.enqueue(a, t.id, _queue_opts(args))
            _print({"task_id": t.id, "queued": e.model_dump(),
                    "note": "a worker (`yemendev serve` or `yemendev worker`) runs it in the background"})
            return 0
        r = wf.advance(a, t.id)
        _print({"task_id": t.id, "status": r.task.status.value, "ran": r.ran_steps, "message": r.message})
    elif args.tcmd == "enqueue":
        _print(wf.enqueue(a, args.task_id, _queue_opts(args)).model_dump())
    elif args.tcmd == "trace":
        _print(wf.trace(a, args.task_id))
    elif args.tcmd == "advance":
        r = wf.advance(a, args.task_id)
        _print({"task_id": args.task_id, "status": r.task.status.value, "ran": r.ran_steps, "message": r.message})
    elif args.tcmd == "show":
        t = wf.get_task(a, args.task_id)
        _print({**t.model_dump(), "steps": [{"name": s.name, "status": s.status.value, "attempt": s.attempt}
                                            for s in wf.tasks.steps(args.task_id)]})
    elif args.tcmd == "list":
        _print([{"id": t.id, "project_id": t.project_id, "status": t.status.value, "step": t.current_step}
                for t in wf.visible_tasks(a)])
    elif args.tcmd == "cancel":
        _print(wf.cancel(a, args.task_id).model_dump())
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    wf = RepairWorkflow(p)
    p.guard.require(a, Action.READ, "tasks")
    _print([e.model_dump() for e in wf.queue.list() if p.guard.can_read(a, e.project_id)])
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Run queued tasks in the background (each run acts with the rights of the human who queued it)."""
    import signal
    import threading

    from yemendev.orion.scheduler import Scheduler

    p = _platform(args)
    a = _actor(p, args)
    if not a.has_role(Role.ADMIN):
        raise SystemExit("error: starting workers requires an admin token")
    sched = Scheduler(p)
    if args.once:
        _print([o.model_dump() for o in sched.drain()])
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    sched.start(args.workers)
    print(f"ORION: {args.workers if args.workers is not None else p.settings.orion.workers} worker(s) running; "
          "Ctrl-C to stop", file=sys.stderr)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    sched.stop()
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    if args.acmd == "list":
        _print([x.model_dump() for x in p.visible_approvals(a)])
    else:
        _print(p.decide_approval(a, args.approval_id, args.acmd == "approve", args.reason or "").model_dump())
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    if args.scmd == "list":
        p.guard.require(a, Action.READ, "skills")
        _print([{"id": s.id, "version": s.version, "status": s.status.value, "name": s.spec.name}
                for s in p.skills.list()])
    elif args.scmd == "validate":
        p.guard.require(a, Action.VALIDATE_SKILL, args.skill_id)
        _print(p.skills.validate(args.skill_id).model_dump())
    elif args.scmd == "request-activation":
        s = p.skills.get(args.skill_id)
        _print(p.request_skill_activation(a, s.id, s.version).model_dump())
    elif args.scmd == "activate":
        _print(p.activate_skill(a, args.approval_id).model_dump())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    p.guard.require(a, Action.READ_AUDIT, "audit")
    v = p.audit.verify(expected_head=args.expected_head)
    _print(v.model_dump())
    return 0 if v.valid else 2


def cmd_bench(args: argparse.Namespace) -> int:
    from yemendev.benchmarks.resilience import run_benchmark

    rep = run_benchmark(seed=args.seed, full=args.full, only=args.only or None, backend=args.backend)
    out = Path(args.out) if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rep.model_dump_json(indent=2))
    for r in rep.results:
        print(f"{'PASS' if r.correct else 'FAIL'}  {r.scenario:55s} expected={r.expected_status:12s} "
              f"actual={r.actual_status:12s} {r.seconds_to_terminal:6.2f}s")
    _print(rep.summary)
    if out:
        print(f"report written to {out}")
    return 0 if rep.summary["correct"] == rep.summary["scenarios"] else 3


def cmd_sandbox(args: argparse.Namespace) -> int:
    from yemendev.sandbox.docker_backend import isolation_report
    from yemendev.sandbox.image import build_sandbox_image

    settings = Settings.from_env(Path(args.data_dir) if args.data_dir else None)
    if args.scmd == "status":
        s = settings.sandbox
        rep = isolation_report(s)
        _print({**rep, "docker_image": s.docker_image, "privilege_separation": s.sandbox_identity() is not None})
        for w in rep["warnings"]:  # type: ignore[attr-defined]
            print(f"WARNING: {w}", file=sys.stderr)
        return 0
    if args.project:  # per-project dependency image: authenticated, admin-only, audited
        p = _platform(args)
        a = _actor(p, args)
        img = p.build_project_image(a, args.project, wheels_dir=Path(args.wheels) if args.wheels else None)
        _print(img.model_dump())
        return 0
    res = build_sandbox_image(settings.sandbox, base_image=args.base, pytest_version=args.pytest,
                              wheels_dir=Path(args.wheels) if args.wheels else None)
    _print(res.model_dump())
    return 0


def cmd_continuity(args: argparse.Namespace) -> int:
    from yemendev.continuity.signing import fingerprint, public_hex

    p = _platform(args)
    a = _actor(p, args)
    sub = args.ccmd
    if sub == "passport-export":
        sp = p.export_passport(a, args.project_id)
        Path(args.out).write_text(sp.model_dump_json(indent=2))
        print(f"passport written to {args.out} (signer {sp.signature.signer_fingerprint})")
    elif sub == "passport-verify":
        result, _ = p.verify_passport(a, Path(args.file).read_text())
        _print({**result.model_dump(), "accepted": result.accepted})
        return 0 if result.accepted else 2
    elif sub == "passport-import":
        _print(p.import_passport(a, args.project_id, Path(args.file).read_text()).model_dump())
    elif sub == "sync-export":
        bundle, job = p.sync_export(a, args.project_id)
        Path(args.out).write_text(bundle.model_dump_json(indent=2))
        _print(job.model_dump())
    elif sub == "sync-import":
        _print(p.sync_import(a, args.project_id, Path(args.file).read_text()).model_dump())
    elif sub == "sync-status":
        _print(p.sync_status(a))
    elif sub == "sync-conflicts":
        _print([c.model_dump() for c in p.sync_conflicts(a, args.project_id, "open")])
    elif sub == "sync-resolve":
        _print(p.resolve_sync_conflict(a, args.conflict_id, args.resolution).model_dump())
    elif sub == "keys-show":
        pub = public_hex(p.signing_key)
        _print({"public_key": pub, "fingerprint": fingerprint(pub)})
    elif sub == "keys-trust":
        print(f"trusted {p.trust_key(a, args.public_key)}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if args.host not in loopback and not args.allow_remote:
        raise SystemExit("error: refusing to bind a non-loopback address without --allow-remote "
                         "(there is no TLS; put a TLS reverse proxy in front)")
    import uvicorn

    from yemendev.api.app import create_app
    from yemendev.orion.scheduler import Scheduler

    p = _platform(args)
    n = p.settings.orion.workers if args.workers is None else args.workers
    sched = Scheduler(p)

    def workers() -> dict[str, Any]:
        return {"count": n, "running": sched.running}

    if n:
        sched.start(n)
    try:
        uvicorn.run(create_app(p, workers=workers), host=args.host, port=args.port, log_level="info")
    finally:
        sched.stop()  # runs in progress are handed back to the queue without using an attempt
    return 0


def _queue_args(x: argparse.ArgumentParser) -> None:
    x.add_argument("--priority", type=int, default=0, help="-10..10, higher runs first")
    x.add_argument("--delay", type=int, default=0, help="seconds to wait before running")
    x.add_argument("--max-attempts", type=int, default=None, help="attempts for transient failures")
    x.add_argument("--timeout", type=int, default=None, help="wall-clock seconds per run")
    x.add_argument("--after", action="append", metavar="TASK_ID", help="run only after this task (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="yemendev", description="YEMENDEV AI - digital resilience platform (early core)")
    ap.add_argument("--data-dir", help="platform data directory (default: $YEMENDEV_DATA_DIR or ./.yemendev)")
    ap.add_argument("--token", help="API token (default: $YEMENDEV_TOKEN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create data dir, migrate DB, issue first admin token")
    s.add_argument("--actor")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    s = sub.add_parser("token", help="manage tokens")
    ts = s.add_subparsers(dest="tok", required=True)
    t = ts.add_parser("issue")
    t.add_argument("actor_id")
    t.add_argument("--role", choices=[r.value for r in Role], default="operator")
    t.add_argument("--ttl-days", type=int, default=90, help="token lifetime in days (1-366)")
    t.set_defaults(fn=cmd_token_issue)

    s = sub.add_parser("project")
    ps = s.add_subparsers(dest="pcmd", required=True)
    x = ps.add_parser("add")
    x.add_argument("name")
    x.add_argument("path")
    ps.add_parser("list")
    x = ps.add_parser("sbom", help="CycloneDX 1.5 software bill of materials of a project (latest scan)")
    x.add_argument("project_id")
    x.add_argument("--out", help="write to this file instead of stdout")
    ps.add_parser("vulns", help="known vulnerabilities of the project's dependencies (offline OSV database)") \
        .add_argument("project_id")
    for name in ("scan", "graph", "members", "risks", "immune", "debt"):
        ps.add_parser(name).add_argument("project_id")
    x = ps.add_parser("whatif", help="inject faults into a sandbox copy and run the tests (counterfactual)")
    x.add_argument("project_id")
    x.add_argument("--module", action="append", help="make this internal module unavailable (repeatable)")
    x.add_argument("--dependency", action="append", help="make this import name fail (repeatable)")
    x.add_argument("--corrupt", action="append", help="truncate this non-test file (repeatable)")
    x.add_argument("--name", default="what-if")
    x = ps.add_parser("impact", help="modules affected if MODULE fails (static import reachability)")
    x.add_argument("project_id")
    x.add_argument("module")
    x = ps.add_parser("grant", help="grant a human access to a project")
    x.add_argument("project_id")
    x.add_argument("actor_ref", help="e.g. human:bob")
    x.add_argument("--role", choices=[r.value for r in Role], default="operator")
    x = ps.add_parser("revoke")
    x.add_argument("project_id")
    x.add_argument("actor_ref")
    s.set_defaults(fn=cmd_project)

    s = sub.add_parser("task")
    tsub = s.add_subparsers(dest="tcmd", required=True)
    x = tsub.add_parser("repair", help="start a repair task and run it until it needs approval or finishes")
    x.add_argument("project_id")
    x.add_argument("--plan", help="JSON file with a human-authored RepairPlan")
    x.add_argument("--queue", action="store_true", help="run it in the background (ORION queue) instead of now")
    _queue_args(x)
    _queue_args(tsub.add_parser("enqueue", help="queue an existing task for background execution"))
    for name in ("advance", "show", "cancel", "trace", "enqueue"):
        (tsub.choices[name] if name in tsub.choices else tsub.add_parser(name)).add_argument("task_id")
    tsub.add_parser("list")
    s.set_defaults(fn=cmd_task)

    sub.add_parser("queue", help="background task queue (ORION)").set_defaults(fn=cmd_queue)
    s = sub.add_parser("advisories", help="offline vulnerability database (OSV exports)")
    acs = s.add_subparsers(dest="acmd", required=True)
    acs.add_parser("import", help="import an OSV export: all.zip, a directory of *.json, or one file (admin)") \
        .add_argument("path")
    acs.add_parser("status")
    s.set_defaults(fn=cmd_advisories)
    sub.add_parser("vulns", help="known vulnerabilities of the platform's own dependencies").set_defaults(fn=cmd_vulns)
    x = sub.add_parser("sbom", help="CycloneDX 1.5 software bill of materials of the platform itself")
    x.add_argument("--out", help="write to this file instead of stdout")
    x.set_defaults(fn=cmd_sbom)
    x = sub.add_parser("worker", help="run queued tasks in the background (requires an admin token)")
    x.add_argument("--workers", type=int, default=None, help="number of workers (default: $YEMENDEV_WORKERS or 1)")
    x.add_argument("--once", action="store_true", help="run what is runnable now, then exit")
    x.set_defaults(fn=cmd_worker)

    s = sub.add_parser("approvals")
    asub = s.add_subparsers(dest="acmd", required=True)
    asub.add_parser("list")
    for name in ("approve", "deny"):
        x = asub.add_parser(name)
        x.add_argument("approval_id")
        x.add_argument("--reason")
    s.set_defaults(fn=cmd_approvals)

    s = sub.add_parser("skills")
    ssub = s.add_subparsers(dest="scmd", required=True)
    ssub.add_parser("list")
    for name in ("validate", "request-activation"):
        ssub.add_parser(name).add_argument("skill_id")
    ssub.add_parser("activate").add_argument("approval_id")
    s.set_defaults(fn=cmd_skills)

    s = sub.add_parser("audit")
    s.add_argument("--expected-head")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("bench", help="run the resilience benchmark (real sandbox, real tests, injected failures)")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--backend", choices=["process", "docker", "auto"], default="process")
    s.add_argument("--full", action="store_true", help="all 9 crash points (default: 3)")
    s.add_argument("--only", nargs="*", help="scenario names to run")
    s.add_argument("--out", help="write JSON report here")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("sandbox", help="sandbox backend status / build the container image")
    sbs = s.add_subparsers(dest="scmd", required=True)
    sbs.add_parser("status")
    x = sbs.add_parser("build-image", help="build the docker sandbox image (no network inside the build)")
    x.add_argument("--base", default="python:3.13-slim", help="local base image (never pulled implicitly)")
    x.add_argument("--pytest", default="9.2.0")
    x.add_argument("--wheels", help="directory of pre-downloaded wheels (fully offline)")
    x.add_argument("--project", help="build the dependency image for this project id (needs an admin token)")
    s.set_defaults(fn=cmd_sandbox)

    s = sub.add_parser("continuity", help="passports, offline sync bundles and trusted keys")
    cs = s.add_subparsers(dest="ccmd", required=True)
    x = cs.add_parser("passport-export")
    x.add_argument("project_id")
    x.add_argument("--out", required=True)
    cs.add_parser("passport-verify").add_argument("file")
    x = cs.add_parser("passport-import")
    x.add_argument("project_id")
    x.add_argument("file")
    x = cs.add_parser("sync-export")
    x.add_argument("project_id")
    x.add_argument("--out", required=True)
    x = cs.add_parser("sync-import")
    x.add_argument("project_id")
    x.add_argument("file")
    cs.add_parser("sync-status")
    cs.add_parser("sync-conflicts").add_argument("project_id")
    x = cs.add_parser("sync-resolve")
    x.add_argument("conflict_id")
    x.add_argument("resolution", choices=["keep_local", "take_remote"])
    cs.add_parser("keys-show")
    cs.add_parser("keys-trust").add_argument("public_key")
    s.set_defaults(fn=cmd_continuity)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-remote", action="store_true")
    s.add_argument("--workers", type=int, default=None,
                   help="background workers in this process (default: $YEMENDEV_WORKERS or 1; 0 = none)")
    s.set_defaults(fn=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except YemenDevError as exc:
        print(f"error ({exc.code.value}): {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
