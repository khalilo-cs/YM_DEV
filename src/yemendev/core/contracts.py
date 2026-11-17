"""Shared primitive contracts: identifiers, time, hashing, errors.

Everything here is dependency-free so every subsystem can import it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def new_id(prefix: str) -> str:
    """Return a stable, globally unique, prefixed identifier (e.g. ``task_3f2a...``)."""
    return f"{prefix}_{uuid.uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601 with fixed microsecond precision, so SQL string comparison is chronological."""
    return (dt or utcnow()).astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashing and integrity digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """SHA-256 of the canonical JSON form of ``value``."""
    return sha256_text(canonical_json(value))


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    CONFLICT = "conflict"
    PRECONDITION_FAILED = "precondition_failed"
    EXECUTION_FAILED = "execution_failed"
    UNAVAILABLE = "unavailable"
    INTEGRITY_ERROR = "integrity_error"
    INTERNAL = "internal"


class YemenDevError(Exception):
    """Base error. ``message`` must be safe to show to API clients (no secrets, no internals)."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(YemenDevError):
    code = ErrorCode.NOT_FOUND


class InvalidInputError(YemenDevError):
    code = ErrorCode.INVALID_INPUT


class PermissionDeniedError(YemenDevError):
    code = ErrorCode.PERMISSION_DENIED


class ApprovalRequiredError(YemenDevError):
    code = ErrorCode.APPROVAL_REQUIRED


class ConflictError(YemenDevError):
    code = ErrorCode.CONFLICT


class PreconditionFailedError(YemenDevError):
    code = ErrorCode.PRECONDITION_FAILED


class ExecutionFailedError(YemenDevError):
    code = ErrorCode.EXECUTION_FAILED


class UnavailableError(YemenDevError):
    code = ErrorCode.UNAVAILABLE


class IntegrityError(YemenDevError):
    code = ErrorCode.INTEGRITY_ERROR
