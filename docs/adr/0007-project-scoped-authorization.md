# ADR-0007: Project-scoped authorization with token-role ceiling

**Context.** Audit finding A-11: roles were global, so any viewer could read every project's
evidence and any operator could approve patches on any project.

**Decision.**
* `project_members(project_id, actor_ref, role)`. The registrant becomes the project's admin member.
* A token's role is a **ceiling**. On a project, a non-admin's effective role is
  `min(token role, membership role)`; with no membership there is no access at all.
* Global admins and the platform's SYSTEM actor are unrestricted.
* Membership management requires an admin token (so, today, only global admins manage members;
  a delegated "project admin" for operator-token holders is intentionally not supported yet).
* The audit log spans all projects and is admin-only. Skills are shared across projects and remain
  globally visible; skill activation stays a globally-scoped, approval-gated action.
* The single authorization boundary for API and CLI is `Platform` / `RepairWorkflow`
  (`require_read`, `visible_*`, `decide_approval`, ...). The low-level `ApprovalService.decide`
  is internal and does not know about projects.

**Consequences.** Denials return 403 "no access to this project" (project ids are random, so this
reveals little); every denial is audited with the project id.
