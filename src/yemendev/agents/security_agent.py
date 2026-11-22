"""Security agent: static review of a proposed change *before* it is simulated or shown for approval.

It inspects only the lines a plan ADDS (and which files it touches). Findings are tripwires, not
proof: pattern matching can be evaded by obfuscation, so a clean review is never treated as a
guarantee - the sandbox, simulation, human approval and verification still apply.

Verdict: any HIGH finding -> BLOCK; any MEDIUM -> CAUTION; otherwise ALLOW.
"""

from __future__ import annotations

import difflib
import re
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel

from yemendev.recovery.model import RepairPlan
from yemendev.security.redaction import redact_text


class Verdict(StrEnum):
    ALLOW = "allow"
    CAUTION = "caution"
    BLOCK = "block"


class SecurityFinding(BaseModel):
    rule: str
    severity: str  # high | medium
    path: str
    line: int | None
    detail: str


class PlanReview(BaseModel):
    plan_id: str
    verdict: Verdict
    findings: list[SecurityFinding]
    note: str = "static pattern review of added lines; a clean result is not a guarantee"


_RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    ("dynamic-code-execution", "high", re.compile(r"\b(eval|exec|compile|__import__)\s*\("),
     "adds dynamic code execution"),
    ("process-execution", "high",
     re.compile(r"\b(subprocess\b|os\.(system|popen|exec\w*|spawn\w*)\s*\(|pty\.spawn|commands\.getoutput)"),
     "adds operating-system command execution"),
    ("unsafe-deserialization", "high", re.compile(r"\b(pickle|marshal|shelve|dill)\.(loads?|Unpickler)\b"),
     "adds unsafe deserialization"),
    ("network-access", "medium",
     re.compile(r"\b(socket\.|import socket|urllib\.request|http\.client|requests\.|httpx\.|ftplib|smtplib|"
                r"paramiko)"), "adds network access"),
    ("yaml-unsafe-load", "medium", re.compile(r"yaml\.load\((?![^)]*SafeLoader)"), "yaml.load without SafeLoader"),
    ("file-deletion", "medium", re.compile(r"\b(shutil\.rmtree|os\.(remove|unlink|rmdir)|\.unlink\()"),
     "adds file deletion"),
    ("permission-change", "medium", re.compile(r"\bos\.(chmod|chown)\s*\(|setuid|setgid"), "changes permissions"),
]
_SENSITIVE_FILES = re.compile(r"(^|/)(setup\.py|setup\.cfg|pyproject\.toml|requirements[^/]*\.txt|Dockerfile|"
                              r"Makefile|\.pre-commit-config\.yaml|MANIFEST\.in)$|(^|/)\.github/|(^|/)\.gitlab-ci")


def review_plan(plan: RepairPlan, originals: dict[str, str]) -> PlanReview:
    findings: list[SecurityFinding] = []
    for p in plan.patches:
        if _SENSITIVE_FILES.search(p.path):
            findings.append(SecurityFinding(rule="build-or-ci-file", severity="medium", path=p.path, line=None,
                                            detail="modifies build, packaging, dependency or CI configuration"))
        old = originals.get(p.path, "").splitlines()
        new = p.new_content.splitlines()
        added: list[tuple[int, str]] = []
        removed = 0
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
            if op in ("replace", "insert"):
                added.extend((j + 1, new[j]) for j in range(j1, j2))
            if op in ("replace", "delete"):
                removed += i2 - i1
        for lineno, text in added:
            code = text.split("#", 1)[0]
            for rule, sev, pat, detail in _RULES:
                if pat.search(code):
                    findings.append(SecurityFinding(rule=rule, severity=sev, path=p.path, line=lineno, detail=detail))
            if redact_text(text) != text:
                findings.append(SecurityFinding(rule="hardcoded-secret", severity="high", path=p.path, line=lineno,
                                                detail="adds what looks like a credential or secret"))
        if old and removed > 20 and removed > len(old) // 2:
            findings.append(SecurityFinding(rule="large-deletion", severity="medium", path=p.path, line=None,
                                            detail=f"removes {removed} of {len(old)} lines"))
        if PurePosixPath(p.path).suffix in (".so", ".pyd", ".dll", ".exe"):
            findings.append(SecurityFinding(rule="binary-file", severity="high", path=p.path, line=None,
                                            detail="writes a native binary"))
    verdict = Verdict.BLOCK if any(f.severity == "high" for f in findings) else (
        Verdict.CAUTION if findings else Verdict.ALLOW)
    return PlanReview(plan_id=plan.id, verdict=verdict, findings=findings)
