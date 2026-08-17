---
last_reconciled: 2026-08-17T00:55:00+02:00
verified_sha: 76b85706c5eccf0b49712b8d65ed327008f2e1fa
reference_branch: origin/main
valid_until: 2026-08-20T00:55:00+02:00
---

# Current handoff

This file is transient and non-authoritative. Ignore it unless
`./scripts/project-preflight` says it is valid.

- Local `main` is 10 commits ahead of `origin/main` (76b8570) with the
  session-store work: picker error surfacing, `browse()` fast reads,
  `-shm`-only recovery, and the WAL departure on Haiku (accepted ADR
  `20260816-0312`). Physical-Haiku evidence including the operator's
  two-TUI test is in `VERIFICATION.md`. The chain is unpublished; pushing
  needs the maintainer's go-ahead.
- The x86_64 machine runs installed `0.1.2-119` with its live store already
  converted out of WAL by the test tree at `/boot/home/haikode-test`.
  Starting the *installed* 0.1.2 alone would silently flip the store back
  to WAL (documented downgrade hazard), so the next release should follow
  soon; until then the operator tests via the test tree.
- The v0.1.2 user-driven `/update` flow itself was never directly observed;
  the machine was simply found running `0.1.2-119` with sessions intact.
  Treat that checkbox as observed-state, not a witnessed flow.
- Third-party MCP interop (Pippo) is verified and recorded; the operator
  has Pippo installed and running on the x86_64 machine with haikode
  configured against it.

One writer only: rewrite this file from the landing/main-maintainer step after
results land. Do not append completed work or move durable knowledge here.
