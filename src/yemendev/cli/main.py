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
    elif args.pcmd == "members":
        _print([m.model_dump() for m in p.list_members(a, args.project_id)])
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    p = _platform(args)
    a = _actor(p, args)
    wf = RepairWorkflow(p)
    if args.tcmd == "repair":
        plan = RepairPlan(**json.loads(Path(args.plan).read_text())) if args.plan else None
        t = wf.create(a, args.project_id, human_plan=plan)
        r = wf.advance(a, t.id)
        _print({"task_id": t.id, "status": r.task.status.value, "ran": r.ran_steps, "message": r.message})
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

    rep = run_benchmark(seed=args.seed, full=args.full, only=args.only or None)
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


def cmd_serve(args: argparse.Namespace) -> int:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if args.host not in loopback and not args.allow_remote:
        raise SystemExit("error: refusing to bind a non-loopback address without --allow-remote "
                         "(there is no TLS; put a TLS reverse proxy in front)")
    import uvicorn

    from yemendev.api.app import create_app

    uvicorn.run(create_app(_platform(args)), host=args.host, port=args.port, log_level="info")
    return 0


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
    for name in ("scan", "graph", "members"):
        ps.add_parser(name).add_argument("project_id")
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
    for name in ("advance", "show", "cancel"):
        tsub.add_parser(name).add_argument("task_id")
    tsub.add_parser("list")
    s.set_defaults(fn=cmd_task)

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
    s.add_argument("--full", action="store_true", help="all 9 crash points (default: 3)")
    s.add_argument("--only", nargs="*", help="scenario names to run")
    s.add_argument("--out", help="write JSON report here")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-remote", action="store_true")
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
