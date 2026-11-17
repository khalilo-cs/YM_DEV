"""Secret redaction for anything written to logs, audit, evidence or API responses.

This is a defence-in-depth filter, not a guarantee: callers must still avoid passing
secrets around. Patterns cover common credential shapes; key names that look
sensitive are redacted regardless of value.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_TOKENS = {
    "password", "passwd", "pwd", "passphrase", "secret", "token", "apikey", "privatekey", "credential",
    "credentials", "authorization", "cookie", "accesskey", "sessionid", "bearer",
}
_KEY_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def is_sensitive_key(key: str) -> bool:
    """Token-based match: ``api_key``/``apiKey``/``X-Auth-Token`` match; ``passed``/``max_tokens`` do not."""
    tokens = [t.lower() for t in _KEY_SPLIT.split(key) if t]
    if any(t in _SENSITIVE_TOKENS for t in tokens):
        return True
    return any(a + b in _SENSITIVE_TOKENS for a, b in zip(tokens, tokens[1:], strict=False))


_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),  # AWS access key id
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), REDACTED),  # GitHub tokens
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), REDACTED),  # OpenAI/Anthropic-style keys
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), REDACTED),  # Slack
    (re.compile(r"\bydv_[A-Za-z0-9_\-]{20,}"), REDACTED),  # this platform's API tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{16,}=*"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@/]+@"), r"\1" + REDACTED + "@"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)['\"]?[^\s'\"]{4,}['\"]?"),
     r"\1\2" + REDACTED),
]


def redact_text(text: str) -> str:
    out = text
    for pat, repl in _VALUE_PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure."""
    if _depth > 32:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive_key(k) and isinstance(v, (str, bytes)):
                result[k] = REDACTED
            else:
                result[k] = redact(v, _depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value
