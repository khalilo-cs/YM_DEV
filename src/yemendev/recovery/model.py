"""Repair plan contracts (see ADR-0006)."""

from __future__ import annotations

import difflib
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator

from yemendev.core.contracts import digest, sha256_text

PROTECTED_PREFIXES = (".git/", ".yemendev/", ".hg/", ".svn/")
MAX_PATCH_BYTES = 2_000_000


class FilePatch(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    original_sha256: str | None = Field(description="sha256 of the file being replaced; None = create new file")
    new_content: str = Field(max_length=MAX_PATCH_BYTES)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        if "\\" in v or "\x00" in v:
            raise ValueError("invalid characters in path")
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or str(p) != v or v.startswith("./"):
            raise ValueError("patch path must be a normalized relative path inside the project")
        if any((v + "/").startswith(pref) or v.startswith(pref) for pref in PROTECTED_PREFIXES):
            raise ValueError("patch path targets a protected location")
        return v

    @property
    def new_sha256(self) -> str:
        return sha256_text(self.new_content)


class PlanSource(StrEnum):
    SKILL = "skill"
    MODEL = "model"
    HUMAN = "human"


class RepairPlan(BaseModel):
    id: str
    description: str = Field(max_length=2000)
    source: PlanSource
    source_ref: str = Field(max_length=200)
    hypothesis_id: str | None = None
    patches: list[FilePatch] = Field(min_length=1, max_length=50)
    rationale: str = Field(default="", max_length=4000)
    # Text that looks like instructions found in what the model was shown (possible prompt injection).
    untrusted_input_signals: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("patches")
    @classmethod
    def _unique_paths(cls, v: list[FilePatch]) -> list[FilePatch]:
        paths = [p.path for p in v]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate patch paths")
        return v

    def operation_digest(self) -> str:
        """Binds an approval to exactly these file changes."""
        return digest({"plan": self.id, "patches": [[p.path, p.original_sha256, p.new_sha256] for p in self.patches]})

    def unified_diff(self, originals: dict[str, str]) -> str:
        chunks: list[str] = []
        for p in self.patches:
            old = originals.get(p.path, "")
            chunks.extend(difflib.unified_diff(old.splitlines(keepends=True), p.new_content.splitlines(keepends=True),
                                               fromfile=f"a/{p.path}", tofile=f"b/{p.path}"))
        return "".join(chunks)


class TestComparison(BaseModel):
    fixed: list[str]
    regressed: list[str]
    still_failing: list[str]
    newly_added: list[str]
    removed: list[str]
    baseline_failing: int
    after_failing: int

    @property
    def acceptable(self) -> bool:
        """Accept only if something was fixed and nothing that passed now fails or disappeared."""
        return bool(self.fixed) and not self.regressed and not self.removed
