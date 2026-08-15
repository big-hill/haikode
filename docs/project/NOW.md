---
last_reconciled: 2026-08-15T23:08:40+02:00
verified_sha: 374bada197e9f601649d51c60dac2b02eb8a1f8e
reference_branch: origin/main
valid_until: 2026-08-18T23:08:40+02:00
---

# Current handoff

This file is transient and non-authoritative. Ignore it unless
`./scripts/project-preflight` says it is valid.

- GitHub release `v0.1.2` points to `374bada` and is the live latest release.
  Its `0.1.2-119` x86_64 and x86_gcc2 assets passed dual-architecture native
  builds, full baselines, package inspection, local checksums and byte-for-byte
  post-upload verification.
- The installed x86_64 `0.1.1-118` package sees v0.1.2 and selects the correct
  live asset and digest. The next test is a human-run `/update` in the CLI,
  followed by one close/reopen, installed-package verification, preserved
  sessions/providers and a simple real prompt.
- Do not infer that the manual upgrade passed from the published release or
  automated checks. Record the observed result in `VERIFICATION.md`, then
  rewrite this handoff. None of this status survives the validity window.

One writer only: rewrite this file from the landing/main-maintainer step after
results land. Do not append completed work or move durable knowledge here.
