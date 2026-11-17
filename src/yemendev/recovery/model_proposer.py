"""Model-backed repair proposer (requires a configured model; optional)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from yemendev.core.contracts import new_id, sha256_bytes
from yemendev.diagnostics.engine import DiagnosticReport
from yemendev.learning.skills import is_test_path
from yemendev.mosaic.adapters import ModelAdapter, ModelRequest, structured
from yemendev.recovery.model import FilePatch, PlanSource, RepairPlan

SYSTEM = (
    "You are a repair assistant. Return ONLY JSON matching: "
    '{"rationale": str, "files": [{"path": str, "new_content": str}]}. '
    "Only modify non-test source files that were shown to you. Never modify tests."
)
MAX_CONTEXT_FILES = 8
MAX_FILE_CHARS = 20_000


class _ModelFile(BaseModel):
    path: str
    new_content: str = Field(max_length=200_000)


class _ModelPlan(BaseModel):
    rationale: str = Field(max_length=4000)
    files: list[_ModelFile] = Field(min_length=1, max_length=10)


def propose_with_model(adapter: ModelAdapter, diag: DiagnosticReport, root: Path) -> RepairPlan:
    paths: list[str] = []
    for f in diag.failures:
        for loc in f.locations:
            p = loc.path
            if p.startswith(str(root)):
                p = Path(p).relative_to(root).as_posix()
            if not is_test_path(p) and p not in paths and (root / p).is_file():
                paths.append(p)
    paths = paths[:MAX_CONTEXT_FILES]
    shown = {p: (root / p).read_bytes() for p in paths}
    ctx = "\n\n".join(f"--- {p}\n{d.decode('utf-8', 'replace')[:MAX_FILE_CHARS]}" for p, d in shown.items())
    prompt = f"Diagnostic summary:\n{diag.model_dump_json(indent=1)[:12000]}\n\nSource files:\n{ctx}"
    out, resp = structured(adapter, ModelRequest(system=SYSTEM, prompt=prompt, max_tokens=4096), _ModelPlan)
    patches = []
    for mf in out.files:
        if mf.path not in shown:  # model may only edit files it was shown
            continue
        patches.append(FilePatch(path=mf.path, original_sha256=sha256_bytes(shown[mf.path]),
                                 new_content=mf.new_content))
    if not patches:
        raise ValueError("model proposed no edits to files it was shown")
    return RepairPlan(id=new_id("pln"), description="Model-proposed repair (untrusted until verified)",
                      source=PlanSource.MODEL, source_ref=f"{resp.adapter}:{resp.model}", patches=patches,
                      rationale=out.rationale)
