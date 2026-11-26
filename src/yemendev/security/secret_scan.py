"""Secret scanning of project file *contents* (directive: "Secret scanning").

Complements the scanner's name-based detection (``.env``, ``*.pem`` ... are never read at all). Every
text file that is read for analysis is also checked against credential patterns.

* A finding names the rule, the file and the line - **never the secret itself** (not even a prefix).
* Pattern-based, so ``certainty=inferred``: false positives and misses are both possible.
* Generic assignments (``password = "..."``) are reported only when the value looks random enough
  (Shannon entropy) and is not an obvious placeholder, to keep noise low.
* A line containing ``yemendev:allow-secret`` is skipped (for deliberate test fixtures); suppressed
  hits are counted so they stay visible.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel

MAX_SCAN_BYTES = 1_000_000
ALLOW_MARKER = "yemendev:allow-secret"

# (rule id, description, pattern, severity). Group "v", when present, is the value to judge.
RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    ("private-key", "private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), "high"),
    ("aws-access-key-id", "AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "high"),
    ("github-token", "GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
     "high"),
    ("gitlab-token", "GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), "high"),
    ("slack-token", "Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "high"),
    ("stripe-live-key", "Stripe live key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}"), "high"),
    ("google-api-key", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high"),
    ("ai-api-key", "AI provider API key (sk-...)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{32,}"), "high"),
    ("yemendev-token", "YEMENDEV API token", re.compile(r"\bydv_[A-Za-z0-9_\-]{30,}"), "high"),
    ("jwt", "JSON Web Token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "medium"),
    ("url-credentials", "credentials embedded in a URL",
     re.compile(r"\b[a-z][a-z0-9+.\-]*://(?P<u>[^\s:/@'\"<>]+):(?P<v>[^\s@/'\"<>]{6,})@"), "medium"),
    ("secret-assignment", "hard-coded password/secret/key",
     re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
                r"\s*[:=]\s*[\"'](?P<v>[^\"'\s]{8,200})[\"']"), "medium"),
]
_PLACEHOLDER = re.compile(r"(?i)(change.?me|example|dummy|placeholder|your|xxx|\*\*\*|<.*>|\$\{.*\}|\{\{.*\}\}|"
                          r"redacted|not.?a.?real|fake|sample|test|todo)")


class SecretHit(BaseModel):
    rule: str
    description: str
    line: int
    severity: str


def entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values()) if s else 0.0


def _judged_out(rule: str, value: str | None) -> bool:
    """Generic rules need a value that looks like a real secret."""
    if value is None or rule not in ("secret-assignment", "url-credentials"):
        return False
    return bool(_PLACEHOLDER.search(value)) or entropy(value) < 3.0


def scan_text(data: bytes) -> tuple[list[SecretHit], int]:
    """Return (hits, suppressed). Binary or oversized content is not scanned."""
    if len(data) > MAX_SCAN_BYTES or b"\x00" in data[:8192]:
        return [], 0
    text = data.decode("utf-8", "replace")
    hits: list[SecretHit] = []
    suppressed = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > 10_000:  # minified bundles etc.: patterns on huge lines are noise and slow
            continue
        for rule, desc, pat, sev in RULES:
            m = pat.search(line)
            if m is None or _judged_out(rule, m.groupdict().get("v")):
                continue
            if ALLOW_MARKER in line:
                suppressed += 1
            else:
                hits.append(SecretHit(rule=rule, description=desc, line=lineno, severity=sev))
            break  # one finding per line is enough
    return hits, suppressed
