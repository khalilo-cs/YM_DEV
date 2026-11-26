"""Digital twin data model.

Every edge carries a ``certainty``:
* observed  - read directly from source (e.g. an ``import`` statement in the AST);
* inferred  - derived by a heuristic (e.g. import name -> distribution name guess);
* unknown   - referenced but could not be resolved.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Certainty(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class EdgeKind(StrEnum):
    INTERNAL_IMPORT = "internal_import"
    STDLIB_IMPORT = "stdlib_import"
    EXTERNAL_IMPORT = "external_import"


class FileInfo(BaseModel):
    path: str
    language: str
    size: int
    sha256: str
    parsed: bool = False
    parse_error: str | None = None


class ModuleInfo(BaseModel):
    name: str          # Python: dotted module name; JS/TS: project-relative file path
    path: str
    is_package: bool
    is_test: bool
    language: str = "python"


class Edge(BaseModel):
    source: str
    target: str
    kind: EdgeKind
    certainty: Certainty
    line: int | None = None
    type_only: bool = False  # TypeScript `import type`: erased at runtime


class DeclaredDependency(BaseModel):
    name: str          # normalized distribution name
    spec: str          # raw requirement string
    source_file: str
    group: str = "main"
    ecosystem: str = "pypi"  # pypi | npm


class Finding(BaseModel):
    kind: str
    severity: str = Field(pattern="^(info|low|medium|high)$")
    message: str
    path: str | None = None
    certainty: Certainty = Certainty.OBSERVED


class ProjectSnapshot(BaseModel):
    root: str
    tree_hash: str
    files: list[FileInfo]
    languages: dict[str, int]
    modules: list[ModuleInfo]
    edges: list[Edge]
    declared_dependencies: list[DeclaredDependency]
    findings: list[Finding]
    skipped: list[str]
    sensitive_files: list[str]
    test_framework: str | None
