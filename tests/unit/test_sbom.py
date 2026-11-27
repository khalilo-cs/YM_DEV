"""CycloneDX SBOM (stage 18b)."""

import base64
import hashlib
import json
import re

from helpers import write

from yemendev.security.sbom import platform_sbom, project_sbom, purl
from yemendev.twin.scanner import ProjectScanner

INTEGRITY = "sha512-" + base64.b64encode(hashlib.sha512(b"react-18.3.1").digest()).decode()


def check_document(doc):
    """Structural checks of the CycloneDX 1.5 document (no offline copy of the official JSON schema)."""
    assert doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.5" and doc["version"] == 1
    assert re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", doc["serialNumber"])
    refs = [c["bom-ref"] for c in doc["components"]]
    assert len(refs) == len(set(refs))
    for c in doc["components"]:
        assert c["type"] == "library" and c["purl"] == c["bom-ref"] and c["purl"].startswith(("pkg:npm/", "pkg:pypi/"))
        assert c["scope"] in ("required", "optional")
        for h in c.get("hashes", []):
            assert h["alg"] in ("SHA-512", "SHA-384", "SHA-256", "SHA-1") and re.fullmatch(r"[0-9a-f]+", h["content"])
        for lic in c.get("licenses", []):
            assert ("expression" in lic) ^ ("license" in lic)
            if "license" in lic:
                assert ("id" in lic["license"]) ^ ("name" in lic["license"])
    known = set(refs)
    assert all(r in known for comp in doc["compositions"] for r in comp["assemblies"])
    assert all(r in known for d in doc["dependencies"] for r in d["dependsOn"])
    assert doc["dependencies"][0]["ref"] == doc["metadata"]["component"]["bom-ref"]


def test_purl_encoding():
    assert purl("npm", "@scope/pkg", "1.0.0") == "pkg:npm/%40scope/pkg@1.0.0"
    assert purl("npm", "react", None) == "pkg:npm/react"
    assert purl("pypi", "Typing_Extensions", "4.12.2") == "pkg:pypi/typing-extensions@4.12.2"


def test_npm_lockfile_gives_exact_versions_hashes_and_only_direct_dependencies(tmp_path):
    root = tmp_path / "web"
    write(root, "package.json", '{"dependencies": {"react": "^18.3.1"}, "devDependencies": {"@scope/tool": "^1.0.0"}}')
    write(root, "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "web"},
        "node_modules/react": {"version": "18.3.1", "integrity": INTEGRITY, "license": "MIT"},
        "node_modules/loose-envify": {"version": "1.4.0", "license": "MIT"},  # transitive
        "node_modules/@scope/tool": {"version": "1.0.0", "dev": True, "license": "(MIT OR Apache-2.0)"},
        "packages/local": {"version": "0.0.0"},  # workspace package, not a dependency
    }}))
    doc = project_sbom(ProjectScanner().scan(root), "web")
    check_document(doc)
    comps = {c["name"]: c for c in doc["components"]}
    assert set(comps) == {"react", "loose-envify", "@scope/tool"}
    assert comps["react"]["version"] == "18.3.1"
    assert comps["react"]["hashes"] == [{"alg": "SHA-512", "content": hashlib.sha512(b"react-18.3.1").hexdigest()}]
    assert comps["@scope/tool"]["scope"] == "optional"
    assert comps["@scope/tool"]["licenses"] == [{"expression": "(MIT OR Apache-2.0)"}]
    assert doc["dependencies"][0]["dependsOn"] == ["pkg:npm/%40scope/tool@1.0.0", "pkg:npm/react@18.3.1"]
    assert [c["aggregate"] for c in doc["compositions"]] == ["complete"]


def test_without_a_lockfile_the_sbom_says_it_is_incomplete(tmp_path):
    root = tmp_path / "py"
    write(root, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["requests>=2", "rich==13.7.1"]\n')
    doc = project_sbom(ProjectScanner().scan(root), "py")
    check_document(doc)
    comps = {c["name"]: c for c in doc["components"]}
    assert "version" not in comps["requests"]  # a range is not a version: not invented
    assert {"name": "yemendev:declared", "value": "requests>=2"} in comps["requests"]["properties"]
    assert comps["rich"]["version"] == "13.7.1"
    [comp] = doc["compositions"]
    assert comp["aggregate"] == "incomplete" and "lockfile" in comp["description"]


def test_uv_lock_makes_python_part_complete(tmp_path):
    root = tmp_path / "py"
    write(root, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["requests>=2"]\n')
    reg = 'source = { registry = "https://pypi.org" }\n'
    write(root, "uv.lock", 'version = 1\n[[package]]\nname = "x"\nversion = "0.1.0"\nsource = { editable = "." }\n'
                           '[[package]]\nname = "requests"\nversion = "2.32.3"\n' + reg
                           + '[[package]]\nname = "idna"\nversion = "3.10"\n' + reg)
    doc = project_sbom(ProjectScanner().scan(root), "py")
    check_document(doc)
    assert sorted((c["name"], c["version"]) for c in doc["components"]) == [("idna", "3.10"), ("requests", "2.32.3")]
    assert doc["dependencies"][0]["dependsOn"] == ["pkg:pypi/requests@2.32.3"]
    assert doc["compositions"][0]["aggregate"] == "complete"


def test_platform_sbom_lists_the_installed_environment(tmp_path):
    doc = platform_sbom()
    check_document(doc)
    names = {c["name"].lower() for c in doc["components"]}
    assert {"fastapi", "pydantic", "cryptography", "tree-sitter"} <= names
    direct = {r.split("/")[1].split("@")[0] for r in doc["dependencies"][0]["dependsOn"]}
    assert {"fastapi", "pydantic", "uvicorn", "cryptography", "tree-sitter"} <= direct
    assert "ruff" not in direct and "mypy" not in direct  # dev extra, not runtime dependencies
