"""Stored benchmark reports, for the research & benchmark centre (directive sections 13 and 15).

Two read-only sources, both plain JSON written by ``yemendev bench``:

* ``published`` - reports committed with the source tree under ``benchmarks/results`` (when present);
* ``local`` - reports produced on this installation, under ``<data dir>/benchmarks``.

Names are validated against a strict pattern and resolved inside their directory, so a request can never read
any other file. Reports are returned exactly as stored: nothing is recomputed, merged or "smoothed".
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from yemendev.core.config import Settings
from yemendev.core.contracts import InvalidInputError, NotFoundError

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.json$")
MAX_BYTES = 5 * 1024 * 1024
_PUBLISHED = Path(__file__).resolve().parents[3] / "benchmarks" / "results"


def sources(settings: Settings) -> dict[str, Path]:
    out = {"local": settings.data_dir / "benchmarks"}
    if _PUBLISHED.is_dir():
        out["published"] = _PUBLISHED
    return out


def save_report(report: BaseModel, settings: Settings, out: Path | None = None) -> Path:
    if out is None:
        kind = "compare" if getattr(report, "benchmark", "") == "yemendev-compare" else "run"
        out = settings.data_dir / "benchmarks" / f"{kind}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2))
    return out


def _path(settings: Settings, source: str, name: str) -> Path:
    if not NAME.match(name):
        raise InvalidInputError("invalid report name")
    base = sources(settings).get(source)
    if base is None:
        raise NotFoundError(f"unknown source {source!r}")
    path = base / name
    if path.is_symlink() or path.resolve().parent != base.resolve() or not path.is_file():
        raise NotFoundError("report not found")
    return path


def _load(path: Path) -> dict[str, Any] | None:
    if path.stat().st_size > MAX_BYTES:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("results"), list) else None


def list_reports(settings: Settings) -> list[dict[str, Any]]:
    """One summary row per readable report, newest first. Unreadable files are listed as such, not hidden."""
    rows: list[dict[str, Any]] = []
    for source, base in sources(settings).items():
        if not base.is_dir():
            continue
        for f in base.iterdir():
            if f.is_symlink() or not (f.is_file() and NAME.match(f.name)):
                continue
            data = _load(f)
            if data is None:
                rows.append({"source": source, "name": f.name, "readable": False})
                continue
            env = data.get("environment") or {}
            rows.append({
                "source": source, "name": f.name, "readable": True,
                "kind": "compare" if data.get("benchmark") == "yemendev-compare" else "resilience",
                "benchmark_version": data.get("benchmark_version"), "platform_version": data.get("platform_version"),
                "started_at": data.get("started_at"), "git_commit": data.get("git_commit"),
                "sandbox_backend": env.get("sandbox_backend"), "seed": data.get("seed"),
                "results": len(data["results"]), "summary": data.get("summary"),
                "not_run": data.get("not_run"),
            })
    return sorted(rows, key=lambda r: (r.get("started_at") or "", r["name"]), reverse=True)


def load_report(settings: Settings, source: str, name: str) -> dict[str, Any]:
    data = _load(_path(settings, source, name))
    if data is None:
        raise InvalidInputError("report is not a readable benchmark report")
    return {"source": source, "name": name, "report": data}
