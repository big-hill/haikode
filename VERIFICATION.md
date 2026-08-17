# haikode completion and verification matrix

Last updated: 2026-08-17. This file records executable evidence, not
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
- Published `v0.1.2` at `374bada` has matching `0.1.2-119` x86_64 and
  x86_gcc2 packages. The 2460-test baseline, all 13 fixture validations,
  deterministic performance audit, native build, package metadata, signature,
  multiple-launch flag, app version 0.1.2, 2728-byte HVIF icon, links and clean
  contents passed on both physical architectures. GitHub reports SHA-256
  `575826452d8e80abd61b3fa8ffbed47dbb8ebc500c74acfc257966b4da587697`
  for x86_64 and
  `be6c276c150c65e6f7e91f72ada6fa4254c66baf0b15a5e835e3acc41ebf3fa7`
  for x86_gcc2; fresh post-upload downloads matched the local packages byte
  for byte. The live latest-release check from installed x86_64 `0.1.1-118`
  selected v0.1.2, its matching asset and digest. The user-driven `/update`,
  close/reopen, state-preservation check and real prompt remain **TO VERIFY**.
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
- Candidate `d96a902` (session store out of WAL on Haiku, ADR
  `20260816-0312`, plus picker error surfacing, `browse()` fast reads, and
  `-shm`-only recovery). The 2486-test baseline passed on macOS with exactly
  the four documented wiring failures. On physical x86_64 Haiku:
  store/TUI/turn suites pass on the machine; the user's real 23 MB store
  (36 sessions, 8894 messages) converted out of WAL on first open in 0.08 s
  with `integrity_check` ok, counts unchanged, and a kept `.pre-rollback`
  snapshot; three simultaneous CLI processes then listed sessions with no
  `locking protocol`; and the operator ran two live TUI instances
  side-by-side on the physical display with real prompts on 2026-08-17 —
  the original two-instance failure did not reproduce. Measured basis: one
  resident writer plus a repeated opener over 12 s completed 5 opens under
  WAL against ~159 under a rollback journal; three writers plus three
  readers under DELETE sustained p50 0.013 s commits and p50 0.001 s picker
  reads. Recovery of the wedged store also demonstrated Haiku keeping a dead
  process's byte-range locks (`SHARED` range HELD on the live file, free on
  a byte-identical copy) — recorded in the ADR as an expected
  conversion-refused case until reboot.
- MCP interop against a third-party server: haikode's `RemoteMCPClient`
  connected to Pippo (`codeberg.org/atomozero/Pippo`, native Haiku MCP
  server, JSON-RPC over `127.0.0.1:2607`) with zero code changes — `/mcp`
  reports `pippo connected (38 tools)`, four SAFE tools (`system_info`,
  `list_windows`, `query_fs`, `haiku_docs`) returned real results through
  `MCPProxyTool`, and the operator completed a live model turn using Pippo
  tools with the `mcp` permission flow on 2026-08-17.

Recorded release evidence is a snapshot. Git, current test output, package
metadata, and observed target behavior override it when they differ.

| Requirement | Current evidence | Status |
|---|---|---|
| Standalone CLI on Haiku | Installed and used daily on all three machines; no Node, Bun, tunnel or external server. Deployment is git push to a bare repo on the box. | PASS |
| Curses TUI + REPL engine | Driven end-to-end through a real pty on Haiku (`tests/render_tui.py`); slash commands, dialogs, queueing, steering, farewell flow and after-turn wheel scrolling are covered. Exact raw-PTY wheel-burst and single-arrow checks passed on physical x86_64 and x86_gcc2 Haiku. | PASS |
| Provider protocols | ChatGPT device OAuth + Responses SSE, SuperGrok RFC 8628 + bearer chat, OpenAI-compatible SSE, Gemini dialect, keyless zen. Real device logins completed in the field; live sessions run daily on subscription accounts. | PASS |
| Secrets | BKeyStore helper (`hai-keystore`), hidden input, mode-0600 fallback, redaction layer with its own canary tests. | PASS |
| Sessions, undo, compaction | SQLite store with file snapshots; automatic compaction keeps the raw transcript and checkpoints its latched provider view across desktop workers; `/compact undo` restores a manual fold. WAL guard incidents and checkpoint reuse have regression tests. On Haiku the store leaves WAL (ADR `20260816-0312`); concurrent multi-process access was verified on the physical machine, including a live two-TUI session by the operator. | PASS |
| HPKG packaging | Published v0.1.2 packages were built on physical x86_gcc2 and x86_64 from commit `374bada`; architecture, version `0.1.2-119`, contents, BFS resources, checksums and fresh post-upload bytes were verified on both. Candidate `0.1.1-114` previously completed an in-place physical x86_64 upgrade with persistent-state hashes unchanged. | PASS structural and published bytes on both architectures; TO VERIFY v0.1.2 activation |
| Real Haiku release gate | v0.1.2 passed the 2460-test baseline, all 13 fixture validations, deterministic performance audit, native build and exact raw-PTY wheel/history checks on x86_gcc2 and x86_64. The installed x86_64 updater selects the live release's correct asset and digest. User-driven `/update`, restart and a simple real prompt remain to verify. | PASS automated/structural/download selection; TO VERIFY user-driven activation |
| MCP + LSP | Configured MCP servers join the tool set behind the `mcp` permission key; `ctx.lsp` provides diagnostics after edit/write. Covered by the suite; exercised with local servers and, on physical Haiku, against the third-party Pippo server (38 tools) end to end including a live model turn. | PASS |
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

The physical x86_64 and x86_gcc2 v0.1.2 release runs each discovered **2460
tests** and passed `scripts/ci_baseline.py`: exactly the same four intentional
wiring failures, platform-appropriate skips, no other failures and no errors.
Both also passed all 13 fixture validations, the deterministic performance
audit and exact raw-PTY wheel-burst versus single-arrow checks.

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

For v0.1.2, the release tag resolves to the exact dual-architecture build
commit, GitHub's asset digests match `SHA256SUMS.txt`, fresh downloads match the
locally inspected packages byte for byte, and the installed x86_64 updater
selects the correct live asset. Activation remains a separate user-observed
test.
