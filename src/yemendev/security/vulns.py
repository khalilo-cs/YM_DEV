"""Dependency vulnerability scanning against an offline OSV database (directive: "Dependency scanning").

Offline-first: advisories are imported from an OSV export (the ``all.zip`` files osv.dev publishes per
ecosystem, a directory of ``*.json`` files, or a single file) that can be carried on removable media.
Nothing is fetched at scan time.

Matching follows the OSV schema: a version is affected if it is listed in ``versions`` or falls inside an
``ECOSYSTEM``/``SEMVER`` range (``introduced`` / ``fixed`` / ``last_affected`` / ``limit`` events). PyPI
versions are compared with PEP 440 (``packaging``), npm versions with Semantic Versioning 2.0.0.

Honesty rules: a component without an exact version is reported as *not checkable*, never as safe; a
version that cannot be parsed is reported the same way; withdrawn advisories are ignored; every report
says which database import it used and how old the newest advisory in it is.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel

from yemendev.core.contracts import InvalidInputError, canonical_json, iso, new_id
from yemendev.storage.db import Database

ECOSYSTEMS = {"PyPI": "pypi", "npm": "npm"}
MAX_ENTRY_BYTES = 2_000_000
MAX_ZIP_ENTRIES = 1_000_000
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def norm_package(ecosystem: str, name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "pypi" else name.lower()


# ---- version ordering ------------------------------------------------------------------------------
def semver_key(v: str) -> tuple[Any, ...]:
    m = _SEMVER.match(v.strip())
    if m is None:
        raise ValueError(f"not a semantic version: {v!r}")
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key: tuple[Any, ...] = (1,)  # a release sorts after all of its pre-releases
    else:
        pre_key = (0, *((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return int(major), int(minor), int(patch), pre_key


def version_key(ecosystem: str) -> Callable[[str], Any]:
    if ecosystem == "npm":
        return semver_key

    def pep440(v: str) -> Version:
        try:
            return Version(v)
        except InvalidVersion as exc:
            raise ValueError(str(exc)) from exc
    return pep440


def _in_range(key: Any, events: list[dict[str, str]], vkey: Callable[[str], Any]) -> bool:
    """OSV range evaluation: sorted events open (introduced) and close (fixed / limit exclusive,
    last_affected inclusive) affected intervals."""
    def ev_key(ev: dict[str, str]) -> tuple[int, Any]:
        v = next(iter(ev.values()))
        return (0, None) if v == "0" else (1, vkey(v))

    intervals: list[tuple[Any, Any, bool]] = []
    start: Any = None
    opened = False
    for ev in sorted(events, key=ev_key):
        kind, value = next(iter(ev.items()))
        if kind == "introduced":
            start, opened = (None if value == "0" else vkey(value)), True
        elif kind in ("fixed", "limit", "last_affected") and opened:
            intervals.append((start, vkey(value), kind == "last_affected"))
            opened = False
    if opened:
        intervals.append((start, None, False))
    for lo, hi, inclusive in intervals:
        if (lo is None or key >= lo) and (hi is None or (key <= hi if inclusive else key < hi)):
            return True
    return False


def is_affected(version: str, affected: dict[str, Any], ecosystem: str) -> bool:
    """Raises ValueError if ``version`` cannot be parsed for the ecosystem."""
    vkey = version_key(ecosystem)
    key = vkey(version)
    if version in (affected.get("versions") or []):
        return True
    for r in affected.get("ranges") or []:
        if r.get("type") in ("ECOSYSTEM", "SEMVER") and isinstance(r.get("events"), list):
            try:
                if _in_range(key, r["events"], vkey):
                    return True
            except (ValueError, StopIteration):
                continue  # a malformed range in one advisory must not hide the others
    return False


# ---- import ----------------------------------------------------------------------------------------
class ImportSummary(BaseModel):
    import_id: str
    source: str
    source_sha256: str
    records_read: int
    rows_written: int
    withdrawn_removed: int
    skipped: int
    newest_modified: str | None


def _iter_records(path: Path) -> Iterator[bytes | None]:
    """Raw JSON documents from a zip (read in memory, never extracted), a directory or a file.
    Yields None for an entry that was skipped (too large)."""
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist() if i.filename.endswith(".json") and not i.is_dir()]
            if len(infos) > MAX_ZIP_ENTRIES:
                raise InvalidInputError("archive has too many entries")
            for info in infos:
                if info.file_size > MAX_ENTRY_BYTES:  # also bounds zip-bomb expansion per entry
                    yield None
                    continue
                with zf.open(info) as fh:
                    yield fh.read(MAX_ENTRY_BYTES + 1)
    elif path.is_dir():
        for f in sorted(path.rglob("*.json")):
            if f.is_symlink() or not f.is_file() or f.stat().st_size > MAX_ENTRY_BYTES:
                yield None
                continue
            yield f.read_bytes()
    elif path.is_file():
        yield path.read_bytes()[: MAX_ENTRY_BYTES + 1]
    else:
        raise InvalidInputError("advisory source not found")


def _file_sha256(path: Path) -> str:
    if path.is_dir():
        return "directory"
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _severity(rec: dict[str, Any], affected: dict[str, Any]) -> str | None:
    for src in (affected.get("database_specific"), affected.get("ecosystem_specific"), rec.get("database_specific")):
        if isinstance(src, dict) and isinstance(src.get("severity"), str):
            return str(src["severity"]).upper()[:20]
    sev = rec.get("severity")
    if isinstance(sev, list) and sev and isinstance(sev[0], dict) and isinstance(sev[0].get("score"), str):
        return str(sev[0]["score"])[:200]  # CVSS vector as published; no score is computed here
    return None


def import_advisories(db: Database, path: Path, actor_ref: str) -> ImportSummary:
    import_id, read, written, removed, skipped = new_id("adv"), 0, 0, 0, 0
    newest: str | None = None
    rows: list[tuple[Any, ...]] = []
    withdrawn: list[str] = []
    for raw in _iter_records(path):
        if raw is None or len(raw) > MAX_ENTRY_BYTES:
            skipped += 1
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not isinstance(rec.get("affected"), list):
            skipped += 1
            continue
        read += 1
        modified = str(rec.get("modified") or "")
        newest = max(newest or modified, modified)
        if rec.get("withdrawn"):
            withdrawn.append(rec["id"])
            continue
        for aff in rec["affected"]:
            pkg = aff.get("package") if isinstance(aff, dict) else None
            if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
                continue
            eco = ECOSYSTEMS.get(str(pkg.get("ecosystem", "")))
            if eco is None:
                continue
            refs = [r.get("url") for r in rec.get("references") or [] if isinstance(r, dict) and r.get("url")][:5]
            aliases = [a for a in rec.get("aliases") or [] if isinstance(a, str)][:20]
            text = str(rec.get("summary") or rec.get("details") or "")[:500]
            rows.append((rec["id"], eco, norm_package(eco, pkg["name"]), modified, text, canonical_json(aliases),
                         _severity(rec, aff), canonical_json({k: aff.get(k) for k in ("versions", "ranges")}),
                         canonical_json(refs), import_id))
    with db.transaction() as conn:
        for adv_id in withdrawn:
            removed += conn.execute("DELETE FROM advisories WHERE id = ?", (adv_id,)).rowcount
        for row in rows:
            cur = conn.execute(
                "INSERT INTO advisories(id, ecosystem, package, modified, summary, aliases_json, severity,"
                " affected_json, references_json, import_id) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id, ecosystem, package) DO UPDATE SET modified = excluded.modified,"
                " summary = excluded.summary, aliases_json = excluded.aliases_json, severity = excluded.severity,"
                " affected_json = excluded.affected_json, references_json = excluded.references_json,"
                " import_id = excluded.import_id WHERE excluded.modified >= advisories.modified", row)
            written += cur.rowcount
        summary = ImportSummary(import_id=import_id, source=path.name, source_sha256=_file_sha256(path),
                                records_read=read, rows_written=written, withdrawn_removed=removed,
                                skipped=skipped, newest_modified=newest)
        conn.execute("INSERT INTO advisory_imports(id, imported_at, imported_by, source, source_sha256,"
                     " newest_modified, records_read, rows_written, withdrawn_removed, skipped)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (import_id, iso(), actor_ref, summary.source, summary.source_sha256, newest, read, written,
                      removed, skipped))
    return summary


def database_status(db: Database) -> dict[str, Any]:
    with db.read() as conn:
        counts = {r[0]: r[1] for r in conn.execute("SELECT ecosystem, COUNT(*) FROM advisories GROUP BY ecosystem")}
        newest = conn.execute("SELECT MAX(modified) FROM advisories").fetchone()[0]
        last = conn.execute("SELECT * FROM advisory_imports ORDER BY imported_at DESC LIMIT 1").fetchone()
    age_days = None
    if newest:
        try:
            age_days = round((datetime.now(UTC) - datetime.fromisoformat(newest.replace("Z", "+00:00")))
                             .total_seconds() / 86400, 1)
        except ValueError:
            age_days = None
    return {"advisories": counts, "newest_advisory_modified": newest, "newest_advisory_age_days": age_days,
            "last_import": dict(last) if last else None,
            "note": "results are only as current as the last import" if counts else
                    "no advisories imported: vulnerability matching is not possible yet"}


# ---- matching --------------------------------------------------------------------------------------
class VulnFinding(BaseModel):
    component: str
    ecosystem: str
    package: str
    version: str
    advisory_id: str
    aliases: list[str]
    summary: str
    severity: str | None
    fixed_in: list[str]
    references: list[str]


class Uncheckable(BaseModel):
    component: str
    reason: str


class VulnReport(BaseModel):
    subject: str
    checked: int
    findings: list[VulnFinding]
    not_checkable: list[Uncheckable]
    database: dict[str, Any]
    limitations: list[str]


def _fixed_versions(affected: dict[str, Any]) -> list[str]:
    """Fixed *versions* only (GIT ranges carry commit hashes, which are not installable versions)."""
    return sorted({ev["fixed"] for r in affected.get("ranges") or [] if r.get("type") in ("ECOSYSTEM", "SEMVER")
                   for ev in r.get("events") or [] if isinstance(ev, dict) and isinstance(ev.get("fixed"), str)})


def _merge_aliases(findings: list[VulnFinding]) -> list[VulnFinding]:
    """The same vulnerability is often published twice (e.g. GHSA-... and PYSEC-..., each listing the other
    as an alias). Report it once per component, keeping every id."""
    out: list[VulnFinding] = []
    for f in sorted(findings, key=lambda f: (f.component, f.advisory_id)):
        ids = {f.advisory_id, *f.aliases}
        same = next((o for o in out if o.component == f.component and ids & {o.advisory_id, *o.aliases}), None)
        if same is None:
            out.append(f.model_copy(update={"aliases": sorted(set(f.aliases) - {f.advisory_id})}))
            continue
        same.aliases = sorted(({*same.aliases, *ids}) - {same.advisory_id})
        same.fixed_in = sorted(set(same.fixed_in) | set(f.fixed_in))
        same.severity = same.severity or f.severity
        same.references = (same.references + [r for r in f.references if r not in same.references])[:8]
    return out


def scan_components(db: Database, components: list[dict[str, Any]], subject: str) -> VulnReport:
    findings: list[VulnFinding] = []
    uncheckable: list[Uncheckable] = []
    checked = 0
    with db.read() as conn:
        for c in components:
            purl: str = c["purl"]
            eco = purl.split(":", 1)[1].split("/", 1)[0]
            if eco not in ("pypi", "npm"):
                continue
            version = c.get("version")
            if not version:
                uncheckable.append(Uncheckable(component=purl, reason="no exact version (declared range only; "
                                                                      "add a lockfile or pin the version)"))
                continue
            try:
                version_key(eco)(version)
            except ValueError:
                uncheckable.append(Uncheckable(component=purl, reason=f"version {version!r} cannot be parsed"))
                continue
            checked += 1
            for r in conn.execute("SELECT * FROM advisories WHERE ecosystem = ? AND package = ?",
                                  (eco, norm_package(eco, c["name"]))):
                aff = json.loads(r["affected_json"])
                if is_affected(version, aff, eco):
                    findings.append(VulnFinding(
                        component=purl, ecosystem=eco, package=c["name"], version=version, advisory_id=r["id"],
                        aliases=json.loads(r["aliases_json"]), summary=r["summary"], severity=r["severity"],
                        fixed_in=_fixed_versions(aff), references=json.loads(r["references_json"])))
    status = database_status(db)
    return VulnReport(
        subject=subject, checked=checked,
        findings=sorted(_merge_aliases(findings), key=lambda f: (f.package, f.advisory_id)),
        not_checkable=uncheckable, database=status,
        limitations=["matches published advisories only (OSV); unknown vulnerabilities are not detected",
                     "only components with an exact version are checked",
                     f"database newest advisory: {status['newest_advisory_modified'] or 'none imported'}"])
