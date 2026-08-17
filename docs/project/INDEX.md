# Documentation routes

This is a routing table, not another project summary. Load only the source that
owns the question.

| Area | Canonical source | Status |
|---|---|---|
| Stable project contract and authority | [CONTEXT.md](CONTEXT.md) | current |
| Code ownership and entry points | [CODEMAP.md](CODEMAP.md) | current |
| Development, QA, worktrees, deploy, release, rollback | [WORKFLOW.md](WORKFLOW.md) | current |
| Short-lived active handoff | `NOW.md` | local and untracked; absent in a fresh clone, use only after preflight |
| Architectural decisions and ADR format | [decisions/README.md](decisions/README.md) | current |
| User guide, install, providers, CLI, commands | [README.md](../../README.md) | current |
| Contribution and security workflow | [CONTRIBUTING.md](../../CONTRIBUTING.md) | current |
| Executed QA evidence | [VERIFICATION.md](../../VERIFICATION.md) | current evidence; reverify drifting claims |
| Implemented parity and reachable gaps | [docs/PARITY.md](../PARITY.md) | current audit |
| Benchmark mechanics and limits | [benchmarks/README.md](../../benchmarks/README.md) | current |
| 32-bit Haiku acceptance runbook | [docs/x86-32bit.md](../x86-32bit.md) | current runbook |
| Native desktop implementation notes | [desktop/NATIVE_UI_NOTES.md](../../desktop/NATIVE_UI_NOTES.md) | current notes; code wins |
| Persistent connection proposal | [persistent-connections.md](../specs/persistent-connections.md) | proposed, not implemented |
| Persistent connection reviews | [review 1](../specs/persistent-connections-review.md), [review 2](../specs/persistent-connections-review-2.md) | investigation |
| Latched compaction design evidence | [brief](../specs/latched-compaction-brief.md), [review](../specs/latched-compaction-review.md) | historical investigation; implemented decision has an ADR |
| v0.1.0 security audit | [release-security-audit.md](../release-security-audit.md) | historical release evidence |
| Original desktop specification | [DESKTOP_SPEC.md](../../DESKTOP_SPEC.md), [duplicate archive](../../specs/DESKTOP_SPEC.md), [early clean proposal](../../specs/hai_desktop_spec_clean.md) | superseded; do not use as current architecture |

External machine and incident notes are intentionally absent. Consult the
maintainer-provided `HAIKODE_OPS_CONTEXT` only for tasks that need them.
