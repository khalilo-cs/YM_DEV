# ADR-0012: Signed offline sync bundles and continuity passports

**Context.** Directive 5.10/6.3/12: synchronize eligible knowledge when connectivity returns without ever
overwriting important data silently, and export/import a portable system description with schema
validation and cryptographic integrity.

**Decision.** File-based transport (works with no network, e.g. removable media). Bundles and passports
are canonical-JSON bodies signed with an installation Ed25519 key (`cryptography` library; key in
`keys/ed25519.pem`, 0600). Verification distinguishes *integrity* (signature valid) from *origin*
(signer key explicitly trusted by an admin). Imported knowledge is unverified; any disagreement with
active local knowledge becomes a human-resolved conflict. Approvals, audit, tokens and evidence are
never synchronized. Exports are recorded as "exported_not_delivered".

**Consequences.** New runtime dependency `cryptography`. No CRDT or automatic merge (the directive warns
CRDTs do not preserve business invariants). Key distribution is manual.
