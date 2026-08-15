---
last_reconciled: 2026-08-15T14:02:32+02:00
verified_sha: 0016d1c1563043d5d96953dfb21383473f11098a
reference_branch: origin/main
valid_until: 2026-08-18T14:02:32+02:00
---

# Current handoff

This file is transient and non-authoritative. Ignore it unless
`./scripts/project-preflight` says it is valid.

- Local `main` contains the unpushed v0.1.1 release-candidate commit `442945f`
  on top of the verified reference SHA.
- The x86_64 HPKG was built and structurally verified. Independent GUI
  installation of that package has not been recorded in Git.
- The x86_gcc2 package, v0.1.1 tag, GitHub push, and GitHub release remain
  pending. Recheck Git, target availability, artifacts, and live runtime before
  continuing; none of this status survives the validity window.

One writer only: rewrite this file from the landing/main-maintainer step after
results land. Do not append completed work or move durable knowledge here.
