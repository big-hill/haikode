---
canonical_reference: origin/main
---

# Project context

This is the stable, agent-agnostic contract for haikode. Read it before making
changes. It defines product invariants and authority boundaries, not current
task status or a history of the project.

## Purpose

haikode is an AI coding agent that runs natively on Haiku OS and talks directly
to model providers over HTTPS. It is a from-scratch, MIT-licensed
reimplementation of opencode behavior, not a wrapper around an opencode server.
The product has three front ends over one engine:

- a curses TUI;
- a plain REPL and scriptable JSON mode;
- a native C++/BeAPI desktop application using the Python engine through an
  NDJSON worker process.

The [README](../../README.md) is the user-facing product and installation guide.
[PARITY.md](../PARITY.md) records measured gaps from opencode.

## Scope and explicit boundaries

- Python runtime code is Python 3.10 standard library only. Do not add pip,
  vendored Python dependencies, Node, Bun, or a mandatory service process.
- The native desktop UI uses Haiku's BeAPI. It is not a web view, Electron app,
  Qt shell, or a second agent engine.
- The program talks directly to configured providers. A localhost server, SSH
  tunnel, or second computer is not part of the product runtime.
- There is no central production server. Runtime claims refer to an actually
  installed process or package on physical Haiku hardware.
- Full opencode parity is a direction, not an assumption. Only behavior marked
  reachable and verified in [PARITY.md](../PARITY.md) exists for users.
- Provider subscriptions and API-key products are separate contracts. Support
  for one never implies support for the other.

## Product and architecture invariants

1. **One engine across front ends.** Provider routing, tools, permissions,
   sessions, undo, compaction, project trust, and agent definitions are owned by
   the Python engine. A front end may adapt presentation and transport, but must
   not invent a divergent lifecycle.
2. **The raw transcript is lossless authority.** Persistence, resume, undo, and
   audit operate on the raw message history. A compacted provider view is
   derived state and may never replace or truncate that authority.
3. **Project boundaries fail closed.** Paths are canonicalized, untrusted
   project configuration cannot silently widen permissions, and mutations are
   previewed and permission-checked before execution.
4. **Provider details stay at adapters.** Provider wire formats, error payloads,
   reasoning signatures, and authentication schemes must not leak into the
   stored provider-neutral message model or front-end contracts.
5. **Haiku is an acceptance platform, not a compile target on paper.** Terminal,
   filesystem, SQLite, keyring, native UI, packaging, and lifecycle changes
   require proportionate checks on physical Haiku before they are called done.
6. **Native desktop windows are independent.** The application permits multiple
   simultaneous windows. Each owns its conversation and process-local
   provider/model/reasoning route; Settings supplies defaults for future
   windows, not shared mutable routing for live ones.
7. **User state outlives package replacement.** Session history, provider
   configuration, OAuth state, and keys live outside the installed package.
   Install, upgrade, and ordinary uninstall paths must not purge them.
8. **`hai-keystore` is a compatibility identity.** Do not rename the helper.
   Haiku keyring approval is tied to its signature and binary path.
9. **OAuth must not destabilize Haiku.** Do not launch a browser automatically
   on Haiku. Present the authorization URL for the user to open elsewhere.
10. **Never impersonate a first-party client.** Do not spoof identity, user
    agents, or prompts to bypass provider policy. Provider policy is
    time-sensitive: check current official documentation and live responses
    before changing an authentication claim.
11. **Network exposure is opt-in and narrow.** Internal helpers bind to loopback,
    never all interfaces. Secrets, host addresses, personal paths, and machine
    configuration do not belong in Git.
12. **Prompt text is executable behavior.** Changes under `haikode/prompts/` or
    guidance assembled by `haikode/prompt.py` need review and regression tests
    like code changes.
13. **Repository language is English.** Product code, committed documentation,
    prompts, and project instructions stay in English. A maintainer's personal
    language, tone, or identity belongs in untracked global instructions.
14. **Model availability is endpoint evidence.** Refreshable provider catalogues
    come from the configured API, not a public marketing/search page or a
    hard-coded wish list. Preserve an endpoint's empty/error result and never
    silently substitute another model when the requested one is unavailable.

Accepted architectural reasons are recorded as separate ADRs under
[decisions/](decisions/README.md). Changing an invariant requires an explicit
decision that describes migration, compatibility, and reversal conditions.

## Authority model

Different questions have different authorities:

| Question | Authority |
|---|---|
| What code is implemented? | Git, with `origin/main` as the canonical reference branch. A local commit is not published merely because it exists. |
| What is running or installed? | The observed process, package metadata, filesystem, and behavior on the target Haiku machine. There is no central production server. |
| What passed QA? | Executable tests and validators, especially `scripts/ci_baseline.py`, plus physical-Haiku evidence for platform behavior. `VERIFICATION.md` is an evidence ledger, not a substitute for rerunning a drifting check. |
| Why was a durable choice made? | Accepted ADRs in `docs/project/decisions/`. |
| What must remain true across tasks? | This file. |
| What is happening right now? | Git and live systems first; `NOW.md` only when its mechanical validity checks pass. |
| What should be built next? | Current issue/task scope. GitHub issues may coordinate work but do not prove implementation. |

Agent memory, conversation history, old handoffs, generated reports, and model
claims are never authoritative project status. They may suggest what to verify.

## Required startup sequence

1. Start in the permanent project checkout, not an abandoned task worktree.
2. Run `./scripts/project-preflight` to fetch and validate the reference.
3. Read this file.
4. Read [CODEMAP.md](CODEMAP.md).
5. Use [NOW.md](NOW.md) only if preflight says it is valid; otherwise ignore it.
6. Read [WORKFLOW.md](WORKFLOW.md) before changing files or touching Haiku.
7. Use [INDEX.md](INDEX.md) to load only documentation relevant to the task.
8. Create a separate task branch/worktree before functional changes.

The goal is correct routing, not loading all historical specifications and
reviews into a model context.

## Progressive context loading

- For product behavior and install questions, start with the README.
- For code ownership, use CODEMAP and then open only the named modules.
- For parity claims, read PARITY and the executable wiring tests.
- For QA or release claims, read VERIFICATION, the benchmark guide, and current
  scripts before relying on recorded dates or counts.
- For a proposed or disputed architecture change, read the relevant ADR and
  only then its linked investigation or review material.
- Treat line numbers in archived reviews as historical. Re-find symbols in the
  current tree before using them.

## External operational context

Machine aliases, addresses, credentials, private runtime notes, and local
release artifacts belong outside Git. A maintainer may expose the location of
that material through `HAIKODE_OPS_CONTEXT`. Consult it only for hardware,
deployment, incident, or release work. Never copy its secrets or machine
identifiers into the repository, and never load it automatically for ordinary
code tasks.

Live provider policy is another external context. Use current primary vendor
documentation and measured responses; do not freeze a dated conversation claim
into this contract.

## Context maintenance

Before finishing a task, decide whether it changed an invariant, ownership
boundary, workflow, operational contract, documentation route, or durable
architectural reason. Update the owning document in the same change when it
did. Do not duplicate facts across documents merely for convenience.

- Rewrite `NOW.md`; never append history to it.
- Only the landing/main-maintainer step writes `NOW.md` after results land.
- Remove completed transient items from `NOW.md`.
- Put a non-trivial durable decision in a new ADR. Normally do not rewrite old
  accepted ADRs; supersede them.
- Prefer executable checks over copied counts, hashes, branch positions, or
  runtime status.
- Mark historical material clearly instead of silently deleting it.

If this file or the preflight validator cannot be read, stop before changes.
