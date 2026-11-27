# ADR-0016: Offline supply-chain security - secrets, SBOM, OSV advisories

**Context.** The directive requires dependency scanning, secret scanning and an SBOM, and the platform
must work offline.

**Decision.**
* **Secret scanning** of file contents with specific credential patterns plus entropy-checked generic
  assignments; findings carry rule, file and line only - the value is never stored.
* **SBOM** in CycloneDX 1.5 JSON from lockfiles (exact versions, npm integrity hashes) or declared
  dependencies; every ecosystem states whether it is complete (lockfile) or incomplete (ranges only);
  `dependsOn` lists direct dependencies only.
* **Vulnerability matching** against OSV exports imported offline (osv.dev `all.zip`, a directory or a
  file; read in memory, never extracted; admin-only CLI import, audited). Matching follows the OSV schema
  with PEP 440 / SemVer 2.0. A version range is reported as *not checkable*, never as safe; duplicate
  publications (GHSA/PYSEC) are merged.

**Consequences.** No network at scan time; results are only as current as the last import, which every
report states. Unpublished vulnerabilities are not detected; CVSS vectors are shown, not scored; a forged
export imported by an admin could hide findings (import is audited with the source's SHA-256).
