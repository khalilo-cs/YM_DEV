"""Extended resilience scenarios (stage 20): directive section 15 categories missing from the first harness.

Every scenario drives the real platform (real sandbox, real tests, real files and database). A scenario
returns (expected, actual, message, platform, task_id, notes) like the original ones and records extra
measurements in ``ctx.metrics``. ``ctx.metrics["required_evidence"]`` overrides the evidence completeness
check for scenarios that are not a single repair task.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from yemendev.benchmarks.fixtures import (
    legacy_project,
    memory_hungry_project,
    multilingual_project,
    renamed_api_project,
)
from yemendev.benchmarks.resilience import project_id_for
from yemendev.core.config import Settings
from yemendev.core.contracts import PermissionDeniedError, YemenDevError, new_id, sha256_bytes
from yemendev.core.faults import FaultInjector
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.learning.skills import SkillStatus
from yemendev.memory.model import MemoryCategory, MemoryStatus, MemoryWrite
from yemendev.orion.tasks import TaskStatus
from yemendev.orion.workflow import RepairWorkflow
from yemendev.platform import Platform
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan
from yemendev.sandbox.suites import run_tests
from yemendev.sandbox.testrunner import RunStatus
from yemendev.sandbox.workspace import Workspace


@dataclass
class Verdict:
    """A non-task outcome (``.value`` like TaskStatus)."""

    value: str


def rename_plan(root: Any, rel: str, old: str, new: str, description: str = "rename the called API") -> RepairPlan:
    f = root / rel
    return RepairPlan(id=new_id("pln"), description=description, source=PlanSource.HUMAN, source_ref="bench",
                      patches=[FilePatch(path=rel, original_sha256=sha256_bytes(f.read_bytes()),
                                         new_content=f.read_text().replace(f"core.{old}", f"core.{new}"))])


def tests_pass(p: Platform, root: Any) -> bool:
    with Workspace(root, p.settings.sandbox, base_dir=p.settings.workspaces_dir) as ws:
        return run_tests(ws).status is RunStatus.ALL_PASSED


def learn_and_activate(p: Platform, ctx: Any, task_id: str) -> str | None:
    """Validate the candidate skill a verified repair produced and activate it with a second human."""
    from yemendev.benchmarks.resilience import APPROVER, OPERATOR

    step = RepairWorkflow(p).tasks.step(task_id, "learn")
    cand = (step.output or {}).get("candidate_skill") if step else None
    if not cand or not p.skills.validate(cand).passed:
        return None
    req = p.request_skill_activation(OPERATOR, cand, p.skills.get(cand).version)
    p.decide_approval(APPROVER, req.id, approve=True, reason="benchmark harness")
    return cand if p.activate_skill(OPERATOR, req.id).status is SkillStatus.ACTIVE else None


# ---- scenarios -------------------------------------------------------------------------------------
def sc_skill_transfer(ctx: Any) -> Any:
    """Learn from one verified repair, then fix a *different* application that uses the same library and
    received the same upstream change - without any human plan or model. (Skills match on the module and
    attribute named in the diagnosis, so they do not generalise across different module names.)"""
    from yemendev.benchmarks.resilience import _run_to_end

    st1, _, p, t1 = _run_to_end(ctx, rename_plan(ctx.root, ctx.meta["service"], ctx.meta["old"], ctx.meta["new"]))
    skill = learn_and_activate(p, ctx, t1) if st1 is TaskStatus.SUCCEEDED else None
    second = ctx.root.parent / "second"
    meta2 = renamed_api_project(second, ctx.seed + 1, names=(ctx.meta["old"], ctx.meta["new"]),
                                package=ctx.meta["package"])
    st2, msg2, p2, t2 = _run_to_end(ctx.with_root(second, meta2), None, platform=p)
    plans = RepairWorkflow(p2).tasks.step(t2, "propose")
    used_skill = bool(plans and plans.output and [x["source"] for x in plans.output.get("plans", [])] == ["skill"])
    notes = [] if skill else ["no skill could be activated from the first repair"]
    if not used_skill:
        notes.append("second project was not repaired from the learned skill")
    ctx.metrics.update({"skill_transfer_success": st2 is TaskStatus.SUCCEEDED and used_skill,
                        "skill_field_stats": p2.skills.field_stats(skill, 1) if skill else None})
    return "succeeded", st2, msg2, p2, t2, notes


def sc_long_horizon(ctx: Any, iterations: int = 6) -> Any:
    """The same project through many upstream API changes, each repaired in turn; nothing may regress."""
    from yemendev.benchmarks.resilience import _run_to_end

    core = ctx.root / f"{ctx.meta['package']}/core.py"
    current_old, current_new = ctx.meta["old"], ctx.meta["new"]
    p: Platform | None = None
    outcomes, regressions, durations = [], 0, []
    tid = ""
    import time
    for i in range(iterations):
        if i:  # upstream renames the function again: the call sites break
            current_old, current_new = current_new, f"{current_new}_v{i + 1}"
            core.write_text(core.read_text().replace(f"def {current_old}(", f"def {current_new}("))
        t0 = time.monotonic()
        st, msg, p, tid = _run_to_end(ctx, rename_plan(ctx.root, ctx.meta["service"], current_old, current_new),
                                      platform=p)
        durations.append(round(time.monotonic() - t0, 2))
        outcomes.append(st.value)
        if not tests_pass(p, ctx.root):
            regressions += 1
    assert p is not None
    pid = project_id_for(p, ctx.root) or ""
    experiences = [r for r in p.memory.query(pid, category=MemoryCategory.EXPERIENCE, limit=1000)
                   if r.status is MemoryStatus.VERIFIED]
    ok = outcomes.count("succeeded")
    ctx.metrics.update({"iterations": iterations, "iterations_succeeded": ok, "regressions": regressions,
                        "knowledge_retention": round(len(experiences) / ok, 3) if ok else None,
                        "seconds_per_iteration": durations})
    notes = [] if ok == iterations and not regressions else [
        f"{ok}/{iterations} succeeded, {regressions} regression(s)"]
    return "succeeded", TaskStatus(outcomes[-1]), msg, p, tid, notes


def sc_distributed_sync(ctx: Any) -> Any:
    """Knowledge moves between two installations as a signed offline bundle; tampering is rejected; a
    disagreement becomes a conflict instead of overwriting verified local knowledge."""
    from yemendev.benchmarks.resilience import OPERATOR, _run_to_end
    from yemendev.continuity.signing import public_hex
    from yemendev.continuity.sync import _record_hash

    st, msg, a, tid = _run_to_end(ctx, rename_plan(ctx.root, ctx.meta["service"], ctx.meta["old"], ctx.meta["new"]))
    pa = a.project(project_id_for(a, ctx.root) or "")
    b = Platform(Settings(data_dir=ctx.root.parent / "peer", workspaces_root=ctx.settings.workspaces_root,
                          sandbox=ctx.settings.sandbox), FaultInjector())
    copy = ctx.root.parent / "peer-copy"
    renamed_api_project(copy, ctx.seed, names=(ctx.meta["old"], ctx.meta["new"]))
    pb = b.register_project(OPERATOR, pa.name, str(copy))
    b.scan_project(OPERATOR, pb.id)
    for plat, pid, value in ((a, pa.id, "fridays only"), (b, pb.id, "never on fridays")):
        plat.memory.write(MemoryWrite(project_id=pid, category=MemoryCategory.CONSTRAINT, subject="deploy",
                                      key="window", value=value, verified=True, source=OPERATOR.ref,
                                      evidence_ids=["evd_bench"]))
    bundle, _ = a.sync_export(OPERATOR, pa.id)
    raw = bundle.model_dump_json()
    notes: list[str] = []
    tampered = json.loads(raw)
    rec = tampered["body"]["memory"][0]  # a capable attacker also recomputes the per-record hash, so only
    rec["value"] = "injected"            # the signature can reveal the change
    rec["record_hash"] = _record_hash(rec["category"], rec["subject"], rec["key"], rec["value"])
    b.trust_key(OPERATOR, public_hex(a.signing_key))
    try:
        b.sync_import(OPERATOR, pb.id, json.dumps(tampered))
        notes.append("tampered bundle was accepted")
        tamper_rejected = False
    except (PermissionDeniedError, YemenDevError):
        tamper_rejected = True
    job = b.sync_import(OPERATOR, pb.id, raw)
    kept = [w.value for w in b.memory.history(pb.id, "deploy", "window") if w.status is MemoryStatus.VERIFIED]
    if kept != ["never on fridays"]:
        notes.append("verified local knowledge was overwritten")
    if job.conflicts < 1:
        notes.append("disagreement not reported as a conflict")
    imported = [r for r in b.memory.query(pb.id, limit=5000) if r.source.startswith("sync:")]
    ctx.metrics.update({"records_applied": job.records_applied, "conflicts": job.conflicts,
                        "tamper_rejected": tamper_rejected, "imported_unverified": all(
                            r.status is MemoryStatus.UNVERIFIED for r in imported)})
    return "completed", Verdict(job.status), msg, a, tid, notes


def sc_data_integrity(ctx: Any) -> Any:
    """Tampering with stored evidence or the audit log is detected; an untouched store raises no alarm."""
    from yemendev.benchmarks.resilience import _run_to_end

    st, msg, p, tid = _run_to_end(ctx, rename_plan(ctx.root, ctx.meta["service"], ctx.meta["old"], ctx.meta["new"]))
    notes = []
    clean = p.verify_integrity()["valid"]
    if not clean:
        notes.append("false alarm on an untouched store")
    ev = p.evidence.list(task_id=tid, limit=1)[0]
    with p.db.transaction() as conn:
        conn.execute("UPDATE evidence_records SET outcome = 'tampered' WHERE id = ?", (ev.id,))
    caught_evidence = not p.verify_integrity()["valid"]
    with p.db.transaction() as conn:
        conn.execute("UPDATE evidence_records SET outcome = ? WHERE id = ?", (ev.outcome, ev.id))
        conn.execute("DELETE FROM audit_events WHERE seq = (SELECT MAX(seq) - 3 FROM audit_events)")
    caught_audit = not p.verify_integrity()["valid"]
    if not caught_evidence:
        notes.append("modified evidence not detected")
    if not caught_audit:
        notes.append("deleted audit event not detected")
    ctx.metrics.update({"tampering_detected": int(caught_evidence) + int(caught_audit), "tampering_attempts": 2,
                        "false_alarms": int(not clean)})
    ctx.metrics["skip_audit_valid"] = True  # the audit log was damaged on purpose
    return "detected", Verdict("detected" if caught_evidence and caught_audit else "missed"), msg, p, tid, notes


def sc_knowledge_staleness(ctx: Any) -> Any:
    """Facts learned from files become stale when those files change - and only those."""
    from yemendev.benchmarks.resilience import OPERATOR

    p = ctx.platform()
    pid = p.register_project(OPERATOR, "bench", str(ctx.root)).id
    p.scan_project(OPERATOR, pid)
    changed = ctx.meta["extra"]
    (ctx.root / changed).write_text("VERSION = 99\n")
    stats = p.memory.revalidate(pid, ctx.root)
    stale = [r for r in p.memory.query(pid, include_inactive=True, limit=5000) if r.status is MemoryStatus.STALE]
    stale_paths = {c.path for r in stale for c in r.validity if c.path}
    notes = []
    if changed not in stale_paths:
        notes.append("knowledge about the changed file was not marked stale")
    false_stale = sorted(stale_paths - {changed})
    if false_stale:
        notes.append(f"unchanged files marked stale: {false_stale}")
    ctx.metrics.update({"marked_stale": stats["marked_stale"], "false_stale": len(false_stale),
                        "required_evidence": ["project.scan"]})
    return "detected", Verdict("detected" if not notes else "missed"), "revalidated", p, "", notes


def sc_low_memory(ctx: Any) -> Any:
    """A test needing more memory than the sandbox allows: the limit holds, the platform stays healthy,
    the project is untouched and the task ends in a controlled state."""
    from yemendev.benchmarks.resilience import _run_to_end

    ctx.settings.sandbox = ctx.settings.sandbox.model_copy(update={"max_memory_mb": 256})
    meta = memory_hungry_project(ctx.root.parent / "hungry", ctx.seed)
    c2 = ctx.with_root(ctx.root.parent / "hungry", meta)
    before = {f: f.read_bytes() for f in c2.root.rglob("*.py")}
    st, msg, p, tid = _run_to_end(c2, None)
    notes = [] if {f: f.read_bytes() for f in c2.root.rglob("*.py")} == before else ["project modified"]
    base = [e for e in p.evidence.list(task_id=tid, limit=100) if e.action == "tests.baseline"]
    limited = bool(base) and base[0].outcome != "all_passed"
    if not limited:
        notes.append("the memory limit did not stop the test")
    ctx.metrics.update({"sandbox_memory_mb": 256, "limit_enforced": limited,
                        "required_evidence": ["project.scan", "tests.baseline"]})
    return "no_action", st, msg, p, tid, notes


def sc_legacy(ctx: Any) -> Any:
    """Understand and repair an old-style project (setup.py, requirements.txt, relative imports, unittest)."""
    from yemendev.benchmarks.resilience import _run_to_end

    meta = legacy_project(ctx.root.parent / "legacy", ctx.seed)
    c2 = ctx.with_root(ctx.root.parent / "legacy", meta)
    st, msg, p, tid = _run_to_end(c2, rename_plan(c2.root, meta["service"], meta["old"], meta["new"]))
    snap = p.latest_scan(project_id_for(p, c2.root) or "")[1]
    notes = []
    internal = {(e.source, e.target) for e in snap.edges if e.kind.value == "internal_import"}
    if (f"{meta['package']}.ledger", f"{meta['package']}.core") not in internal:
        notes.append("relative import not understood")
    if not any(d.source_file == "requirements.txt" for d in snap.declared_dependencies):
        notes.append("requirements.txt not read")
    if meta["old"] in (c2.root / meta["service"]).read_text():
        notes.append("fix not on disk")
    ctx.metrics.update({"modules": len(snap.modules), "internal_edges": len(internal)})
    return "succeeded", st, msg, p, tid, notes


def sc_multilingual(ctx: Any) -> Any:
    """Arabic identifiers, docstrings, test names and documentation survive scan, repair, evidence and memory."""
    from yemendev.benchmarks.resilience import _run_to_end

    meta = multilingual_project(ctx.root.parent / "arabic", ctx.seed)
    c2 = ctx.with_root(ctx.root.parent / "arabic", meta)
    plan = rename_plan(c2.root, meta["service"], meta["old"], meta["new"], description="إعادة تسمية الدالة المستدعاة")
    st, msg, p, tid = _run_to_end(c2, plan)
    notes = []
    text = (c2.root / meta["service"]).read_text()
    if meta["new"] not in text or "مجموع قائمة من الأعداد" not in text:
        notes.append("Arabic source text damaged")
    base = [e for e in p.evidence.list(task_id=tid, limit=100) if e.action == "tests.baseline"]
    failing = json.dumps(base[0].body.tests if base else {}, ensure_ascii=False)
    if "test_المجموع_الصحيح" not in failing:
        notes.append("Arabic test name lost in evidence")
    pid = project_id_for(p, c2.root) or ""
    exp = p.memory.query(pid, category=MemoryCategory.EXPERIENCE, limit=10)
    if not any("إعادة تسمية" in json.dumps(r.value, ensure_ascii=False) for r in exp):
        notes.append("Arabic plan description lost in memory")
    ctx.metrics["languages"] = p.latest_scan(pid)[1].languages
    return "succeeded", st, msg, p, tid, notes


def sc_unauthorized(ctx: Any) -> Any:
    """Operations outside an actor's rights are refused and recorded; nothing changes."""
    from yemendev.benchmarks.resilience import OPERATOR, _run_to_end

    p = ctx.platform()
    pid = p.register_project(OPERATOR, "bench", str(ctx.root)).id
    wf = RepairWorkflow(p)
    t = wf.create(OPERATOR, pid, human_plan=rename_plan(ctx.root, ctx.meta["service"], ctx.meta["old"],
                                                        ctx.meta["new"]))
    st, _, p, tid = _run_to_end(ctx, None, platform=p, task_id=t.id, approve=False)
    apr = next(a for a in p.approvals.for_task(tid) if a.status.value == "pending")
    agent = Actor(id="planning-agent", kind=ActorKind.AGENT, role=Role.ADMIN)
    outsider = Actor(id="mallory", kind=ActorKind.HUMAN, role=Role.OPERATOR)
    viewer = Actor(id="vic", kind=ActorKind.HUMAN, role=Role.VIEWER)
    attempts: list[Callable[[], object]] = [
        lambda: p.decide_approval(agent, apr.id, approve=True, reason="x"),     # agents never decide
        lambda: p.decide_approval(OPERATOR, apr.id, approve=True, reason="x"),  # no self-approval
        lambda: wf.advance(outsider, tid),                                      # not a project member
        lambda: p.register_project(viewer, "x", str(ctx.root)),                 # role too low
        lambda: p.create_backup(outsider),                                      # admin only
    ]
    blocked = 0
    for attempt in attempts:
        try:
            attempt()
        except (PermissionDeniedError, YemenDevError):
            blocked += 1
    notes = [] if blocked == len(attempts) else [f"{len(attempts) - blocked} unauthorized operation(s) succeeded"]
    if (ctx.root / ctx.meta["service"]).read_text().count(ctx.meta["old"]) == 0:
        notes.append("project changed without a valid approval")
    ctx.metrics.update({"unauthorized_attempts": len(attempts), "blocked": blocked,
                        "required_evidence": ["project.scan", "tests.baseline", "repair.simulate"]})
    return "blocked", Verdict("blocked" if not notes else "allowed"), "", p, tid, notes


EXTENDED = [
    ("skill_transfer", "skill_transfer", sc_skill_transfer),
    ("long_horizon_6_changes", "long_horizon", sc_long_horizon),
    ("distributed_sync", "distributed_synchronization", sc_distributed_sync),
    ("tamper_detection", "data_integrity", sc_data_integrity),
    ("knowledge_staleness", "knowledge_staleness", sc_knowledge_staleness),
    ("sandbox_memory_limit", "low_memory", sc_low_memory),
    ("legacy_project_repair", "legacy_understanding", sc_legacy),
    ("arabic_project_repair", "multilingual", sc_multilingual),
    ("unauthorized_operations", "security", sc_unauthorized),
]
