"""Offline dependency vulnerability scanning (stage 18c). Advisory ids here are synthetic (TEST-...)."""

import json
import zipfile

import pytest
from end_to_end.conftest import ALICE
from helpers import write

from yemendev.core.contracts import PermissionDeniedError
from yemendev.governance.actors import Actor, ActorKind, Role
from yemendev.security.vulns import import_advisories, is_affected, scan_components, semver_key


def adv(adv_id, eco, name, *, versions=None, events=None, modified="2026-01-01T00:00:00Z", **extra):
    affected = {"package": {"ecosystem": eco, "name": name}}
    if versions is not None:
        affected["versions"] = versions
    if events is not None:
        affected["ranges"] = [{"type": "SEMVER" if eco == "npm" else "ECOSYSTEM", "events": events}]
    return {"id": adv_id, "modified": modified, "summary": f"{name} issue", "aliases": ["CVE-0000-0001"],
            "affected": [affected], "references": [{"type": "WEB", "url": "https://example.invalid/adv"}], **extra}


def test_semver_precedence_follows_the_spec():
    order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
             "1.0.0-rc.1", "1.0.0", "2.0.0", "2.1.0", "2.1.1"]
    assert sorted(order, key=semver_key) == order
    assert semver_key("1.2.3+build.5") == semver_key("1.2.3")
    with pytest.raises(ValueError):
        semver_key("1.2")


@pytest.mark.parametrize(("version", "events", "eco", "expected"), [
    ("2.30.0", [{"introduced": "0"}, {"fixed": "2.31.0"}], "pypi", True),
    ("2.31.0", [{"introduced": "0"}, {"fixed": "2.31.0"}], "pypi", False),
    ("2.31.0rc1", [{"introduced": "0"}, {"fixed": "2.31.0"}], "pypi", True),  # PEP 440: rc before release
    ("1.2.0", [{"introduced": "1.0.0"}, {"last_affected": "1.2.0"}], "npm", True),
    ("1.2.1", [{"introduced": "1.0.0"}, {"last_affected": "1.2.0"}], "npm", False),
    ("0.9.0", [{"introduced": "1.0.0"}, {"fixed": "1.5.0"}], "npm", False),
    ("1.6.0", [{"introduced": "1.0.0"}, {"fixed": "1.5.0"}, {"introduced": "2.0.0"}, {"fixed": "2.3.0"}], "npm", False),
    ("2.1.0", [{"introduced": "1.0.0"}, {"fixed": "1.5.0"}, {"introduced": "2.0.0"}, {"fixed": "2.3.0"}], "npm", True),
    ("9.0.0", [{"introduced": "3.0.0"}], "npm", True),  # no fix yet
    ("1.0.0", [{"introduced": "0"}, {"limit": "1.0.0"}], "npm", False),
    ("2.0.0-beta.1", [{"introduced": "0"}, {"fixed": "2.0.0"}], "npm", True),
])
def test_osv_range_evaluation(version, events, eco, expected):
    aff = {"ranges": [{"type": "SEMVER", "events": events}]}
    assert is_affected(version, aff, eco) is expected


def test_explicit_versions_still_match_when_a_range_is_malformed():
    aff = {"versions": ["1.0.3"], "ranges": [{"type": "SEMVER", "events": [{"introduced": "not-a-version"}]}]}
    assert is_affected("1.0.3", aff, "npm") and not is_affected("1.0.4", aff, "npm")


@pytest.fixture
def osv_zip(tmp_path):
    path = tmp_path / "all.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("TEST-PYPI-0001.json", json.dumps(adv("TEST-PYPI-0001", "PyPI", "Requests",
                                                          events=[{"introduced": "0"}, {"fixed": "2.31.0"}],
                                                          database_specific={"severity": "moderate"})))
        zf.writestr("TEST-NPM-0001.json", json.dumps(adv("TEST-NPM-0001", "npm", "lodash",
                                                         events=[{"introduced": "0"}, {"fixed": "4.17.21"}])))
        zf.writestr("TEST-NPM-0002.json", json.dumps(adv("TEST-NPM-0002", "npm", "react", versions=["0.0.1"])))
        zf.writestr("TEST-GO-0001.json", json.dumps(adv("TEST-GO-0001", "Go", "example.com/x", versions=["1"])))
        zf.writestr("../escape.json", json.dumps(adv("TEST-PYPI-0002", "PyPI", "idna", versions=["3.6"])))
        zf.writestr("broken.json", "{not json")
        big = adv("TEST-PYPI-BIG", "PyPI", "oversized", versions=["1.0"])  # valid, but over the per-entry limit
        zf.writestr("huge.json", json.dumps(big)[:-1] + " " * 2_100_000 + "}")
    return path


def test_import_reads_zip_in_memory_and_reports_what_it_skipped(platform, osv_zip, tmp_path):
    s = import_advisories(platform.db, osv_zip, "human:alice")
    assert (s.records_read, s.rows_written, s.skipped) == (5, 4, 2)  # Go is read but not stored; 2 unusable
    assert not (tmp_path.parent / "escape.json").exists()  # nothing is ever extracted to disk
    with platform.db.read() as conn:
        rows = sorted(tuple(r) for r in conn.execute("SELECT id, ecosystem, package, severity FROM advisories"))
    assert "oversized" not in {r[2] for r in rows}  # the size limit, not JSON validity, rejected it
    assert rows == [("TEST-NPM-0001", "npm", "lodash", None), ("TEST-NPM-0002", "npm", "react", None),
                    ("TEST-PYPI-0001", "pypi", "requests", "MODERATE"), ("TEST-PYPI-0002", "pypi", "idna", None)]


def test_newer_records_win_and_withdrawn_advisories_are_removed(platform, tmp_path):
    d = tmp_path / "osv"
    write(d, "a.json", json.dumps(adv("TEST-X", "npm", "pkg", versions=["1.0.0"], modified="2026-02-01T00:00:00Z")))
    import_advisories(platform.db, d, "human:alice")
    write(d, "a.json", json.dumps(adv("TEST-X", "npm", "pkg", versions=["9.9.9"], modified="2025-01-01T00:00:00Z")))
    assert import_advisories(platform.db, d, "human:alice").rows_written == 0  # an older copy never overwrites
    write(d, "a.json", json.dumps(adv("TEST-X", "npm", "pkg", versions=["1.0.0"], withdrawn="2026-03-01T00:00:00Z",
                                      modified="2026-03-01T00:00:00Z")))
    assert import_advisories(platform.db, d, "human:alice").withdrawn_removed == 1


def test_scan_reports_affected_components_and_what_it_could_not_check(platform, osv_zip, tmp_path):
    import_advisories(platform.db, osv_zip, "human:alice")
    comps = [{"purl": "pkg:pypi/requests@2.25.0", "name": "requests", "version": "2.25.0"},
             {"purl": "pkg:pypi/requests@2.31.0", "name": "requests", "version": "2.31.0"},
             {"purl": "pkg:npm/lodash@4.17.20", "name": "lodash", "version": "4.17.20"},
             {"purl": "pkg:npm/lodash", "name": "lodash"},
             {"purl": "pkg:npm/weird@latest", "name": "weird", "version": "latest"}]
    r = scan_components(platform.db, comps, "p")
    assert [(f.component, f.advisory_id, f.fixed_in) for f in r.findings] == [
        ("pkg:npm/lodash@4.17.20", "TEST-NPM-0001", ["4.17.21"]),
        ("pkg:pypi/requests@2.25.0", "TEST-PYPI-0001", ["2.31.0"])]
    assert r.checked == 3
    assert {u.component: u.reason.split(" ")[0] for u in r.not_checkable} == {
        "pkg:npm/lodash": "no", "pkg:npm/weird@latest": "version"}
    assert r.database["advisories"] == {"npm": 2, "pypi": 2}


def test_with_no_database_nothing_is_claimed_safe(platform):
    r = scan_components(platform.db, [{"purl": "pkg:npm/a@1.0.0", "name": "a", "version": "1.0.0"}], "p")
    assert r.findings == [] and "no advisories imported" in r.database["note"]


def test_only_admins_import_advisories(platform, osv_zip):
    op = Actor(id="olga", kind=ActorKind.HUMAN, role=Role.OPERATOR)
    with pytest.raises(PermissionDeniedError):
        platform.import_advisories(op, str(osv_zip))
    s = platform.import_advisories(ALICE, str(osv_zip))
    assert any(e.action == "advisories.import" and e.target == s.import_id for e in platform.audit.list(limit=50))


def test_project_scan_and_immune_system_use_the_database(platform, osv_zip, tmp_path):
    root = tmp_path / "web"
    write(root, "package.json", '{"dependencies": {"lodash": "^4.17.0"}}')
    write(root, "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/lodash": {"version": "4.17.20"}}}))
    write(root, "src/a.js", 'const _ = require("lodash");\n')
    pid = platform.register_project(ALICE, "web", str(root)).id
    platform.scan_project(ALICE, pid)
    platform.import_advisories(ALICE, str(osv_zip))
    r = platform.vulnerabilities(ALICE, pid)
    assert [f.advisory_id for f in r.findings] == ["TEST-NPM-0001"]
    assert any(e.action == "dependencies.vulnerability_scan" for e in platform.evidence.list())
    imm = [f for f in platform.immune_check(ALICE, pid).findings if f.rule == "vulnerable-dependency"]
    assert len(imm) == 1 and imm[0].category == "security" and "4.17.21" in imm[0].recommended_action


def test_duplicate_publications_are_reported_once_and_git_fixes_are_not_versions(platform, tmp_path):
    d = tmp_path / "osv"
    ghsa = adv("GHSA-test-0001", "PyPI", "requests", events=[{"introduced": "0"}, {"fixed": "2.31.0"}])
    ghsa["aliases"] = ["CVE-2099-0001", "PYSEC-TEST-1"]
    pysec = adv("PYSEC-TEST-1", "PyPI", "requests", events=[{"introduced": "0"}, {"fixed": "2.31.0"}])
    pysec["aliases"] = ["CVE-2099-0001", "GHSA-test-0001"]
    pysec["affected"][0]["ranges"].append({"type": "GIT", "repo": "https://example.invalid/r",
                                           "events": [{"introduced": "0"}, {"fixed": "74ea7cf7a6a27a4e"}]})
    other = adv("GHSA-test-0002", "PyPI", "requests", events=[{"introduced": "0"}, {"fixed": "2.32.0"}])
    other["aliases"] = ["CVE-2099-0002"]
    for name, rec in (("a", ghsa), ("b", pysec), ("c", other)):
        write(d, f"{name}.json", json.dumps(rec))
    import_advisories(platform.db, d, "human:alice")
    r = scan_components(platform.db, [{"purl": "pkg:pypi/requests@2.25.0", "name": "requests", "version": "2.25.0"}],
                        "p")
    assert [(f.advisory_id, f.aliases, f.fixed_in) for f in r.findings] == [
        ("GHSA-test-0001", ["CVE-2099-0001", "PYSEC-TEST-1"], ["2.31.0"]),
        ("GHSA-test-0002", ["CVE-2099-0002"], ["2.32.0"])]
