# haikode completion and verification matrix

Last updated: 2026-08-07. This file records executable evidence, not
intended behavior. Every PASS below names the machine class it was
demonstrated on; nothing is claimed from reading the source alone.

Hardware the evidence comes from: a stock-named x86_64 desktop
(hrev57937), a 32-bit x86_gcc2 Acer Aspire One (R1/beta6, hrev59866), and
a MacBook5,1 (R1/beta6 development, hrev59917) run by an independent
reviewer.

| Requirement | Current evidence | Status |
|---|---|---|
| Standalone CLI on Haiku | Installed and used daily on all three machines; no Node, Bun, tunnel or external server. Deployment is git push to a bare repo on the box. | PASS |
| Curses TUI + REPL engine | Driven end-to-end through a real pty on Haiku (`tests/render_tui.py`); slash commands, dialogs, queueing, steering and farewell flow covered by the suite. | PASS |
| Provider protocols | ChatGPT device OAuth + Responses SSE, SuperGrok RFC 8628 + bearer chat, OpenAI-compatible SSE, Gemini dialect, keyless zen. Real device logins completed in the field; live sessions run daily on subscription accounts. | PASS |
| Secrets | BKeyStore helper (`hai-keystore`), hidden input, mode-0600 fallback, redaction layer with its own canary tests. | PASS |
| Sessions, undo, compaction | SQLite store with file snapshots; automatic compaction keeps the raw transcript and checkpoints its latched provider view across desktop workers; `/compact undo` restores a manual fold. WAL guard incidents and checkpoint reuse have regression tests. | PASS |
| HPKG packaging | Built and installed on x86_gcc2 (architecture field verified against HaikuPorts convention) and x86_64; in-place upgrades get a commit-count package revision after a field report of silent same-version no-ops. | PASS |
| 32-bit x86_gcc2 | Full suite at the documented baseline, native builds under `setarch x86`, live provider turn — all on the Aspire One. | PASS |
| MCP + LSP | Configured MCP servers join the tool set behind the `mcp` permission key; `ctx.lsp` provides diagnostics after edit/write. Covered by the suite; exercised with local servers. | PASS |
| Skills | `SKILL.md` discovery (global + project), catalogue in the system prompt, on-demand loading via the `skill` tool, `/skills` report. Worked example ships in `docs/examples/skills/`. | PASS |
| Subagents, cross-provider | Agent definitions and per-call `model` may pin any configured provider's model; the sub-agent runs on its own client or fails loudly. | PASS |
| Native desktop app | Builds, installs and runs its worker on Haiku; convergence on the current agent engine is tracked, not claimed. | PARTIAL |

## Automated checks

```sh
python3 -m unittest discover -s tests -b
```

The 2026-08-07 Mac run executed **2427 tests**. It fails **exactly four** on
purpose — the wiring-audit backlog documented in the README — and skips four.
A fifth failure is a real regression. `scripts/ci_baseline.py` verifies the
failure identities, not only the count.

The no-network performance probes are also executable evidence:

```sh
HAI_DISABLE_KEYSTORE=1 python3 benchmarks/performance_audit.py --pretty
```

They cover provider-round counts, time to first SSE event, retry attempt ids,
compaction reuse, cross-process checkpoint restoration, SQLite writes/backups,
MCP startup budget and desktop worker startup. They intentionally do not claim
live-provider or TLS latency.

## Acceptance shape

Acceptance runs happen over a single non-multiplexed SSH connection per
machine and leave no processes behind (`ps` verified). The independent
review on hrev59917 additionally rebuilt the package, compared installed
trees against the checkout by hash, and audited the full git history for
personal identifiers (zero findings).
