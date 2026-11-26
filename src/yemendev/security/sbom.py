"""Software Bill of Materials in CycloneDX 1.5 JSON (directive: "Software Bill of Materials").

* **Project SBOM** - from the latest scan of a registered project. Exact versions (and npm integrity
  hashes) come from lockfiles when present: ``package-lock.json`` (v1-v3), ``uv.lock``, ``poetry.lock``,
  pinned ``==`` requirements. Without a lockfile, the declared dependency is listed with its version
  range only. A ``compositions`` entry per ecosystem states honestly whether that part is complete
  (from a lockfile) or incomplete (declared ranges / direct dependencies only).
* **Platform SBOM** - the Python distributions installed in the platform's own environment and, when
  this is a source checkout, the web UI's locked npm packages.

Files are read only if the scanner listed them (inside the project root, not symlinks), never executed.
"""

from __future__ import annotations

import base64
import json
import re
import tomllib
import uuid
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from yemendev import __version__
from yemendev.core.contracts import iso
from yemendev.twin.model import ProjectSnapshot

SPEC_VERSION = "1.5"
_EXACT_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*===?\s*([A-Za-z0-9.+!_-]+)\s*(?:;.*)?$")


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def purl(ecosystem: str, name: str, version: str | None) -> str:
    if ecosystem == "npm":
        ns, _, base = name.rpartition("/")
        path = f"{quote(ns, safe='')}/{quote(base, safe='')}" if ns else quote(name, safe="")
        return f"pkg:npm/{path}" + (f"@{quote(version, safe='')}" if version else "")
    return f"pkg:pypi/{_norm_py(name)}" + (f"@{quote(version, safe='')}" if version else "")


def _component(ecosystem: str, name: str, version: str | None, *, scope: str = "required", source: str,
               hashes: list[dict[str, str]] | None = None, props: dict[str, str] | None = None,
               licenses: list[str] | None = None) -> dict[str, Any]:
    ref = purl(ecosystem, name, version)
    c: dict[str, Any] = {"type": "library", "bom-ref": ref, "name": name, "purl": ref, "scope": scope}
    if version:
        c["version"] = version
    if hashes:
        c["hashes"] = hashes
    if licenses:
        c["licenses"] = [_license(lic) for lic in licenses]
    c["properties"] = [{"name": "yemendev:source", "value": source},
                       *({"name": f"yemendev:{k}", "value": v} for k, v in (props or {}).items())]
    return c


_SPDX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")
_SPDX_EXPR = re.compile(r"^[A-Za-z0-9.+()\s-]+$")


def _license(text: str) -> dict[str, Any]:
    """SPDX id ("MIT"), SPDX expression ("(MIT OR Apache-2.0)") or, failing both, a plain name ("MIT License")."""
    t = text.strip()
    if _SPDX_ID.match(t):
        return {"license": {"id": t}}
    if _SPDX_EXPR.match(t) and re.search(r"\s(AND|OR|WITH)\s", t):
        return {"expression": t}
    return {"license": {"name": t[:200]}}


def _integrity_hash(integrity: str | None) -> list[dict[str, str]]:
    """npm 'sha512-<base64>' -> CycloneDX hash."""
    out = []
    for part in (integrity or "").split():
        alg, _, b64 = part.partition("-")
        names = {"sha512": "SHA-512", "sha384": "SHA-384", "sha256": "SHA-256", "sha1": "SHA-1"}
        if alg in names:
            try:
                out.append({"alg": names[alg], "content": base64.b64decode(b64).hex()})
            except ValueError:
                continue
    return out


def _read(root: Path, rel: str, listed: set[str]) -> str | None:
    p = root / rel
    if rel not in listed or p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
        return None
    try:
        return p.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _npm_lock(rel: str, text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    pkgs = data.get("packages")
    if isinstance(pkgs, dict):  # lockfileVersion 2 / 3
        for key, meta in sorted(pkgs.items()):
            if not key or "node_modules/" not in key or not isinstance(meta, dict) or meta.get("link"):
                continue
            name = meta.get("name") or key.rsplit("node_modules/", 1)[1]
            lic = meta.get("license")
            out.append(_component("npm", name, meta.get("version"), source=rel,
                                  scope="optional" if meta.get("dev") or meta.get("optional") else "required",
                                  hashes=_integrity_hash(meta.get("integrity")),
                                  props={"dev": "true"} if meta.get("dev") else None,
                                  licenses=[lic] if isinstance(lic, str) else None))
        return out

    def walk(deps: Any) -> None:  # lockfileVersion 1
        for name, meta in sorted((deps or {}).items()):
            if isinstance(meta, dict):
                out.append(_component("npm", name, meta.get("version"), source=rel,
                                      scope="optional" if meta.get("dev") else "required",
                                      hashes=_integrity_hash(meta.get("integrity"))))
                walk(meta.get("dependencies"))
    walk(data.get("dependencies"))
    return out


def _toml_lock(rel: str, text: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out = []
    for pkg in data.get("package", []) or []:
        if isinstance(pkg, dict) and isinstance(pkg.get("name"), str):
            src = pkg.get("source") or {}
            if isinstance(src, dict) and (src.get("editable") or src.get("virtual")):
                continue  # the project itself / local path packages
            out.append(_component("pypi", pkg["name"], pkg.get("version"), source=rel,
                                  scope="optional" if pkg.get("category") == "dev" else "required"))
    return out


def project_sbom(snap: ProjectSnapshot, project_name: str) -> dict[str, Any]:
    root = Path(snap.root)
    listed = {f.path for f in snap.files}
    components: dict[str, dict[str, Any]] = {}
    complete: dict[str, bool] = {"npm": False, "pypi": False}

    for rel in sorted(listed):
        name = PurePosixPath(rel).name
        if name == "package-lock.json" and (text := _read(root, rel, listed)) is not None:
            found = _npm_lock(rel, text)
            complete["npm"] = complete["npm"] or bool(found)
            for c in found:
                components.setdefault(c["bom-ref"], c)
        elif name in ("uv.lock", "poetry.lock") and (text := _read(root, rel, listed)) is not None:
            found = _toml_lock(rel, text)
            complete["pypi"] = complete["pypi"] or bool(found)
            for c in found:
                components.setdefault(c["bom-ref"], c)

    # Declared dependencies not covered by a lockfile: exact pins give a version, ranges do not.
    locked_names = {(c["purl"].split("/")[0], c["name"].lower()) for c in components.values()}
    for d in snap.declared_dependencies:
        eco = d.ecosystem
        if (f"pkg:{eco}", d.name.lower()) in locked_names or (eco == "npm" and complete["npm"]) or \
                (eco == "pypi" and complete["pypi"]):
            continue
        version, rng = None, d.spec
        if eco == "pypi" and (m := _EXACT_PIN.match(d.spec)):
            version = m.group(2)
        elif eco == "npm":
            rng = d.spec.rpartition("@")[2]
            version = rng.lstrip("=v") if re.fullmatch(r"=?v?\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", rng) else None
        c = _component(eco, d.name, version, source=d.source_file,
                       scope="optional" if d.group not in ("main",) else "required",
                       props={"declared": rng, "group": d.group})
        components.setdefault(c["bom-ref"], c)

    ecosystems = sorted({c["purl"].split(":", 1)[1].split("/", 1)[0] for c in components.values()})
    compositions = [{"aggregate": "complete" if complete.get(e) else "incomplete",
                     "assemblies": sorted(r for r, c in components.items() if c["purl"].startswith(f"pkg:{e}/")),
                     "description": ("all packages from the lockfile" if complete.get(e) else
                                     "declared direct dependencies only; transitive packages and exact versions "
                                     "are unknown without a lockfile")} for e in ecosystems]
    direct = {(f"pkg:{d.ecosystem}", d.name.lower() if d.ecosystem == "npm" else _norm_py(d.name))
              for d in snap.declared_dependencies}
    depends_on = sorted(r for r, c in components.items()
                        if (c["purl"].split("/")[0], c["name"].lower() if c["purl"].startswith("pkg:npm")
                            else _norm_py(c["name"])) in direct)
    return _document(
        component={"type": "application", "bom-ref": "project", "name": project_name,
                   "properties": [{"name": "yemendev:tree_hash", "value": snap.tree_hash}]},
        components=sorted(components.values(), key=lambda c: c["bom-ref"]), compositions=compositions,
        depends_on=depends_on)


def platform_sbom(web_lock: Path | None = None) -> dict[str, Any]:
    comps: dict[str, dict[str, Any]] = {}
    for dist in metadata.distributions():
        md = dist.metadata
        name = md["Name"] if "Name" in md else None
        if not name:
            continue
        lic = (md["License-Expression"] if "License-Expression" in md else None) or next(
            (c.rsplit(" :: ", 1)[1] for c in md.get_all("Classifier") or []
             if c.startswith("License :: OSI Approved :: ")), None)
        c = _component("pypi", name, dist.version, source="installed environment",
                       licenses=[lic] if lic and len(lic) < 100 else None)
        comps.setdefault(c["bom-ref"], c)
    compositions = [{"aggregate": "complete", "assemblies": sorted(comps),
                     "description": "every distribution installed in the platform's Python environment"}]
    if web_lock is not None and web_lock.is_file():
        web = _npm_lock("apps/web/package-lock.json", web_lock.read_text("utf-8"))
        for c in web:
            comps.setdefault(c["bom-ref"], c)
        compositions.append({"aggregate": "complete", "assemblies": sorted(c["bom-ref"] for c in web),
                             "description": "web UI packages from apps/web/package-lock.json (build-time; the "
                                            "served UI is static files)"})
    reqs = [r for r in (metadata.requires("yemendev") or []) if "extra ==" not in r]
    direct = {_norm_py(m.group(0)) for r in reqs if (m := re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r))}
    depends_on = sorted(r for r, c in comps.items() if r.startswith("pkg:pypi/") and _norm_py(c["name"]) in direct)
    return _document(component={"type": "application", "bom-ref": "yemendev", "name": "yemendev",
                                "version": __version__},
                     components=sorted(comps.values(), key=lambda c: c["bom-ref"]), compositions=compositions,
                     depends_on=depends_on)


def _document(component: dict[str, Any], components: list[dict[str, Any]], compositions: list[dict[str, Any]],
              depends_on: list[str]) -> dict[str, Any]:
    """``dependencies`` lists only the *direct* dependencies of the root component; the graph between
    third-party packages is not modelled (CycloneDX ``dependsOn`` means direct dependencies)."""
    return {
        "bomFormat": "CycloneDX", "specVersion": SPEC_VERSION, "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {"timestamp": iso(), "component": component,
                     "tools": {"components": [{"type": "application", "name": "yemendev", "version": __version__}]}},
        "components": components,
        "dependencies": [{"ref": component["bom-ref"], "dependsOn": depends_on}],
        "compositions": compositions,
    }
