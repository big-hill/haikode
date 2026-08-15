# haikode completion and verification matrix

Last updated: 2026-08-15. This file records executable evidence, not
intended behavior. Every PASS below names the machine class it was
demonstrated on; nothing is claimed from reading the source alone.

Hardware the evidence comes from: a stock-named x86_64 desktop
(hrev57937), a 32-bit x86_gcc2 Acer Aspire One (R1/beta6, hrev59866), and
a MacBook5,1 (R1/beta6 development, hrev59917) run by an independent
reviewer.

## Release boundary

- Published `v0.1.0` at `0016d1c` completed the dual-architecture release gate:
  x86_64 and x86_gcc2 packages, native suites, fixture validation, package
  inspection, and an independent x86_64 HaikuDepot install.
- Published `v0.1.1` at `8fe7009` has matching `0.1.1-109` x86_64 and
  x86_gcc2 packages. The 2440-test baseline, all 13 fixture validations,
  deterministic performance audit, native build, package metadata, signature,
  multiple-launch flag, HVIF icon, links, documentation and clean package
  contents were checked on both physical architectures. A manual `/update`
  from v0.1.0 found v0.1.1, downloaded the matching x86_64 asset, and printed
  the correct installation path. Package activation, state preservation and
  the post-upgrade prompt remain **TO VERIFY**.
- Candidate `76910f0` makes an explicit `/update` verify GitHub's asset digest
  and HPKG name/version, then wait for `pkgman install -y` to complete. Its
  2455-test baseline passed on macOS and physical x86_64 Haiku with only the
  four documented wiring failures; the native app also built there. The final
  candidate downloaded the live GitHub asset, verified it as `haikode
  0.1.1-109`, and cleaned its private temporary copy on x86_64 without
  installation. The full branch at `192c8cd` was then built as
  `haikode-0.1.1-114-x86_64.hpkg` and upgraded the physical system from
  `0.1.0-106`. A fresh process imported the new packaged updater, while the
  configuration and session database hashes remained unchanged. User-driven
  close/reopen plus a real prompt remain **TO VERIFY**. The same updater at
  branch tip `393efe5` then passed the 2455-test baseline, all 13 fixture
  validations and the deterministic performance audit on physical x86_gcc2.
  Its native package built as `haikode-0.1.1-115-x86_gcc2.hpkg` (SHA-256
  `a00f928c2407b0042fd9e10a2fd25574f2cda9506427f04d58eeea6a3ca57a92`);
  metadata, clean contents, 32-bit binaries, app signature and updater code
  were inspected. The live release path also selected, verified, parsed and
  cleaned the published x86_gcc2 asset without installing it.
- Candidate `8aa2da9` restores normal Haiku Terminal wheel scrolling after a
  completed turn without stealing single-arrow prompt history. Exact raw-PTY
  wheel-burst and single-arrow tests passed on physical x86_64 and x86_gcc2
  Haiku. The 2459-test baseline also passed on macOS and physical x86_64 with
  exactly the four documented wiring failures and no others.

Recorded release evidence is a snapshot. Git, current test output, package
metadata, and observed target behavior override it when they differ.

| Requirement | Current evidence | Status |
|---|---|---|
| Standalone CLI on Haiku | Installed and used daily on all three machines; no Node, Bun, tunnel or external server. Deployment is git push to a bare repo on the box. | PASS |
| Curses TUI + REPL engine | Driven end-to-end through a real pty on Haiku (`tests/render_tui.py`); slash commands, dialogs, queueing, steering, farewell flow and after-turn wheel scrolling are covered. Exact raw-PTY wheel-burst and single-arrow checks passed on physical x86_64 and x86_gcc2 Haiku. | PASS |
| Provider protocols | ChatGPT device OAuth + Responses SSE, SuperGrok RFC 8628 + bearer chat, OpenAI-compatible SSE, Gemini dialect, keyless zen. Real device logins completed in the field; live sessions run daily on subscription accounts. | PASS |
| Secrets | BKeyStore helper (`hai-keystore`), hidden input, mode-0600 fallback, redaction layer with its own canary tests. | PASS |
| Sessions, undo, compaction | SQLite store with file snapshots; automatic compaction keeps the raw transcript and checkpoints its latched provider view across desktop workers; `/compact undo` restores a manual fold. WAL guard incidents and checkpoint reuse have regression tests. | PASS |
| HPKG packaging | Published v0.1.1 packages were built on physical x86_gcc2 and x86_64 from commit `8fe7009`; architecture, version `0.1.1-109`, contents, BFS attributes and checksums were verified before and after GitHub upload. Candidate `0.1.1-114` completed an in-place physical x86_64 upgrade with persistent-state hashes unchanged, and candidate `0.1.1-115` was built and inspected on physical x86_gcc2. | PASS structural on both candidate architectures and x86_64 activation; TO VERIFY published-asset GUI upgrade |
| Real Haiku release gate | v0.1.1 passed the 2440-test baseline, all 13 fixture validations, deterministic performance audit and native package build on x86_gcc2 and x86_64. Its physical multi-window behavior was exercised on x86_64 before release. The updater candidate passes its 2455-test baseline, fixtures, performance audit, native package build and live-asset verification on both architectures; x86_64 package activation/state preservation also passed. The scroll candidate passes the 2459-test baseline on x86_64 and exact physical raw-PTY checks on both architectures. User-driven restart and a simple real prompt remain to verify. | PASS automated/structural/download/activation; TO VERIFY restarted GUI |
| MCP + LSP | Configured MCP servers join the tool set behind the `mcp` permission key; `ctx.lsp` provides diagnostics after edit/write. Covered by the suite; exercised with local servers. | PASS |
| Skills | `SKILL.md` discovery (global + project), catalogue in the system prompt, on-demand loading via the `skill` tool, `/skills` report. Worked example ships in `docs/examples/skills/`. | PASS |
| Subagents, cross-provider | Agent definitions and per-call `model` may pin any configured provider's model; the sub-agent runs on its own client or fails loudly. | PASS |
| Native desktop app | Builds, installs and runs its worker on Haiku. Multiple simultaneous windows, independent model selection, visible window cascading and continuous reasoning text were exercised on the physical x86_64 machine. Convergence on the full TUI feature set is tracked, not claimed. | PARTIAL |

## Automated checks

```sh
python3 -m unittest discover -s tests -b
```

The current 2026-08-15 Mac run executed **2460 tests**. It fails **exactly four** on
purpose — the wiring-audit backlog documented in the README — and skips four.
A fifth failure is a real regression. `scripts/ci_baseline.py` verifies the
failure identities, not only the count.

The physical x86_64 scroll-candidate run also executed **2459 tests** with the
same four intentional failures, four skips, no other failures and no errors.
Its exact raw-PTY checks distinguish a three-arrow wheel burst from a single
prompt-history arrow. The same targeted 25-test regression set and exact PTY
checks passed on physical x86_gcc2.

The physical x86_gcc2 updater-candidate run also executed **2455 tests** with
the same four intentional failures, two platform-specific skips, no other
failures and no errors.

The no-network performance probes are also executable evidence:

```sh
HAI_DISABLE_KEYSTORE=1 python3 benchmarks/performance_audit.py --pretty
```

They cover provider-round counts, time to first SSE event, retry attempt ids,
compaction reuse, cross-process checkpoint restoration, SQLite writes/backups,
MCP startup budget and desktop worker startup. They intentionally do not claim
live-provider or TLS latency.

## Acceptance shape

Completed v0.1.0 acceptance runs used one SSH connection per machine and left
no processes behind (`ps` verified). The independent review on hrev59917 also
rebuilt the package, compared installed trees against the checkout by hash,
and audited the full git history for personal identifiers (zero findings).
For v0.1.1, GitHub's downloaded release assets matched the locally verified
packages byte for byte, and the live `releases/latest` response selected the
correct asset for both architectures. The equivalent updater candidate was
activated on x86_64 with unchanged persistent-state hashes, but that does not
replace the pending user-driven close/reopen and real-prompt checks against an
installed release asset.
