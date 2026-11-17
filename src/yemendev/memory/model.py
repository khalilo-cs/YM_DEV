"""Living Memory data model."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MemoryCategory(StrEnum):
    PROJECT_IDENTITY = "project_identity"
    PROJECT_STRUCTURE = "project_structure"
    TECHNICAL_DECISION = "technical_decision"
    REQUIREMENT = "requirement"
    CONSTRAINT = "constraint"
    DEPENDENCY = "dependency"
    VERIFIED_FACT = "verified_fact"
    OBSERVATION = "observation"
    EXPERIENCE = "experience"
    FAILED_EXPERIMENT = "failed_experiment"
    CANDIDATE_SKILL = "candidate_skill"
    VALIDATED_SKILL = "validated_skill"
    DEPRECATED_KNOWLEDGE = "deprecated_knowledge"


class MemoryStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    STALE = "stale"              # a validity condition no longer holds
    CONTRADICTED = "contradicted"  # conflicts with a verified record; not trusted
    SUPERSEDED = "superseded"    # replaced by a newer verified version
    DEPRECATED = "deprecated"


ACTIVE_STATUSES = (MemoryStatus.UNVERIFIED, MemoryStatus.VERIFIED)


class ValidityCondition(BaseModel):
    """A machine-checkable condition under which a memory remains valid."""

    kind: str = Field(pattern="^(file_sha256|file_absent)$")
    path: str
    sha256: str | None = None


class MemoryRecord(BaseModel):
    id: str
    project_id: str | None
    category: MemoryCategory
    subject: str
    key: str
    value: Any
    status: MemoryStatus
    confidence: float = Field(ge=0.0, le=1.0)
    source: str
    evidence_ids: list[str] = Field(default_factory=list)
    validity: list[ValidityCondition] = Field(default_factory=list)
    version: int = 1
    supersedes: str | None = None
    contradicts: list[str] = Field(default_factory=list)
    created_at: str
    validated_at: str | None = None
    revalidate_after: str | None = None


class MemoryWrite(BaseModel):
    project_id: str | None
    category: MemoryCategory
    subject: str = Field(min_length=1, max_length=500)
    key: str = Field(min_length=1, max_length=200)
    value: Any
    verified: bool = False
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: str = Field(min_length=1, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list)
    validity: list[ValidityCondition] = Field(default_factory=list)
    revalidate_after: str | None = None


class WriteOutcome(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"          # same value already present; re-validated
    SUPERSEDED_PREVIOUS = "superseded_previous"
    CONTRADICTION_RECORDED = "contradiction_recorded"


class WriteResult(BaseModel):
    outcome: WriteOutcome
    record: MemoryRecord
    conflicting_ids: list[str] = Field(default_factory=list)
