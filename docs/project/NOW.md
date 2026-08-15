---
last_reconciled: 2026-08-15T15:39:08+02:00
verified_sha: 8fe7009709de68c53ce5f8a2ff53f7258e43f112
reference_branch: origin/main
valid_until: 2026-08-18T15:39:08+02:00
---

# Current handoff

This file is transient and non-authoritative. Ignore it unless
`./scripts/project-preflight` says it is valid.

- GitHub release `v0.1.1` points to `8fe7009` and is the live latest release.
  Its `0.1.1-109` x86_64 and x86_gcc2 assets passed native builds, package
  inspection, local checksums and byte-for-byte post-upload verification.
- The next test is a human-run `/update` from the installed v0.1.0 package on
  the physical x86_64 target, followed by `Open with -> HaikuDepot`, Deskbar
  launch, preserved sessions/providers and a simple real prompt.
- Do not infer that the manual upgrade passed from the published release or
  automated checks. Record the observed result in `VERIFICATION.md`, then
  rewrite this handoff. None of this status survives the validity window.

One writer only: rewrite this file from the landing/main-maintainer step after
results land. Do not append completed work or move durable knowledge here.
