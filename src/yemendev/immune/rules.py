"""Pure detection rules for the immune system."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from yemendev.immune.model import ImmuneFinding

# Files whose code runs automatically (test start, interpreter start) - persistence/injection vectors.
AUTO_EXEC = re.compile(r"(^|/)(conftest\.py|sitecustomize\.py|usercustomize\.py)$|\.pth$")
BUILD_CI = re.compile(r"(^|/)(setup\.py|setup\.cfg|pyproject\.toml|requirements[^/]*\.txt|Dockerfile|Makefile|"
                      r"tox\.ini|noxfile\.py)$|(^|/)\.github/|(^|/)\.gitlab-ci")
BINARY = {".so", ".pyd", ".dll", ".exe", ".dylib", ".bin"}

# Small offline reference list of widely used PyPI distributions, for typosquatting checks.
POPULAR = sorted({
    "requests", "urllib3", "numpy", "pandas", "django", "flask", "fastapi", "pydantic", "sqlalchemy", "boto3",
    "botocore", "setuptools", "pytest", "pyyaml", "six", "python-dateutil", "certifi", "idna", "charset-normalizer",
    "cryptography", "pillow", "scipy", "matplotlib", "scikit-learn", "tensorflow", "torch", "jinja2", "click",
    "attrs", "packaging", "pip", "wheel", "colorama", "rich", "httpx", "aiohttp", "uvicorn", "gunicorn", "celery",
    "redis", "psycopg2", "pymongo", "beautifulsoup4", "lxml", "selenium", "paramiko", "pyjwt", "openpyxl",
    "protobuf", "grpcio", "markupsafe", "werkzeug", "tqdm", "toml", "tomli", "filelock", "platformdirs", "virtualenv",
    "docutils", "sphinx", "pygments", "pyparsing", "decorator", "simplejson", "ujson", "orjson", "msgpack",
    "jsonschema", "typing-extensions", "mypy", "black", "ruff", "flake8", "isort", "coverage", "mock", "tox",
    "nose", "twisted", "tornado", "sanic", "starlette", "marshmallow", "arrow", "pendulum", "pytz", "tzdata",
})


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def typosquat_candidates(names: list[str]) -> list[ImmuneFinding]:
    out = []
    for n in names:
        if n in POPULAR or len(n) < 4:
            continue
        near = [p for p in POPULAR if abs(len(p) - len(n)) <= 2 and levenshtein(n, p) <= (1 if len(n) < 7 else 2)]
        if near:
            out.append(ImmuneFinding(
                rule="possible-typosquatting", category="security", severity="medium", certainty="inferred",
                subject=n, detail=f"dependency name is very close to well-known package(s): {', '.join(near)}",
                recommended_action="confirm the intended package name before installing or building an image"))
    return out


def classify_changed_file(path: str, authorized: bool) -> ImmuneFinding:
    """A file whose hash differs from the baseline scan."""
    if authorized:
        return ImmuneFinding(rule="authorized-change", category="operational", severity="info", certainty="observed",
                             subject=path, detail="matches content applied by an approved platform repair",
                             recommended_action="none")
    if AUTO_EXEC.search(path):
        return ImmuneFinding(rule="auto-executing-file-changed", category="security", severity="high",
                             certainty="observed", subject=path,
                             detail="file that runs automatically (test start / interpreter start) changed outside "
                                    "the platform", recommended_action="review the change before running any tests")
    if BUILD_CI.search(path):
        return ImmuneFinding(rule="build-or-ci-changed", category="security", severity="medium", certainty="observed",
                             subject=path, detail="build, dependency or CI configuration changed outside the platform",
                             recommended_action="review the change; rescan to make it the new baseline")
    return ImmuneFinding(rule="unrecorded-change", category="operational", severity="low", certainty="observed",
                         subject=path, detail="source changed outside the platform since the baseline scan (drift)",
                         recommended_action="rescan to update the baseline and memory")


def classify_new_file(path: str) -> ImmuneFinding:
    suffix = PurePosixPath(path).suffix.lower()
    if AUTO_EXEC.search(path):
        return ImmuneFinding(rule="auto-executing-file-added", category="security", severity="high",
                             certainty="observed", subject=path,
                             detail="new file that runs code automatically", recommended_action="review before testing")
    if suffix in BINARY:
        return ImmuneFinding(rule="binary-added", category="security", severity="medium", certainty="observed",
                             subject=path, detail="new native binary in the source tree",
                             recommended_action="verify its origin")
    return ImmuneFinding(rule="file-added", category="operational", severity="info", certainty="observed",
                         subject=path, detail="new file since the baseline scan", recommended_action="rescan")


def denial_bursts(denials: list[tuple[str, str | None]], threshold: int = 5) -> list[ImmuneFinding]:
    """denials: (actor, project_id) of recent 'denied' audit events. Repeated denials suggest probing."""
    by_actor: dict[str, list[str | None]] = {}
    for actor, pid in denials:
        by_actor.setdefault(actor, []).append(pid)
    out = []
    for actor, pids in sorted(by_actor.items()):
        if len(pids) >= threshold:
            projects = len({p for p in pids if p})
            out.append(ImmuneFinding(
                rule="repeated-permission-denials", category="security", severity="medium", certainty="observed",
                subject=actor, detail=f"{len(pids)} denied actions across {projects} project(s) in the window",
                recommended_action="check whether this actor's token is compromised or misconfigured; "
                                   "revoke it if needed"))
        else:
            out.append(ImmuneFinding(
                rule="permission-denied", category="operational", severity="info", certainty="observed",
                subject=actor, detail=f"{len(pids)} denied action(s) (below the probing threshold of {threshold})",
                recommended_action="none, unless unexpected"))
    return out
