# Code map

This map names stable ownership boundaries and canonical entry points. It is
not a file inventory. Follow the links and then search current symbols rather
than trusting historical line numbers.

| Area | Canonical ownership and entry points |
|---|---|
| CLI dispatch | [`haikode/__main__.py`](../../haikode/__main__.py) delegates to [`haikode/main.py`](../../haikode/main.py). `main()` selects subcommands, one-shot mode, TUI, JSON, or REPL. |
| Composition root | [`haikode/runtime.py`](../../haikode/runtime.py) merges global/project/agent configuration and constructs providers, permissions, tools, LSP, MCP, and the `Agent`. Do not assemble a parallel engine in a front end. |
| Agent loop and message contract | [`haikode/agent.py`](../../haikode/agent.py) owns provider rounds, native tool calls, steering, reasoning replay, and the raw/provider history split. Provider-neutral dataclasses live in [`haikode/schema.py`](../../haikode/schema.py). |
| Turn and persistence lifecycle | [`haikode/turn.py`](../../haikode/turn.py) is shared by interactive front ends and owns session adoption, persistence, undo coordination, checkpoints, and one-turn results. [`haikode/session.py`](../../haikode/session.py) owns SQLite schema, migrations, snapshots, transcript export, manual compaction, and context checkpoints. |
| Context and accounting | [`haikode/context.py`](../../haikode/context.py) owns instruction discovery, token estimation, compaction planning, summaries, and derived provider history. [`haikode/usage.py`](../../haikode/usage.py) owns measured usage and context display. |
| Configuration and trust | [`haikode/config.py`](../../haikode/config.py) owns user configuration, secure writes, and keystore access. [`haikode/projectconfig.py`](../../haikode/projectconfig.py) owns project discovery, trust boundaries, instruction files, tool narrowing, and project-local config. |
| Commands, agents, skills, memory | [`haikode/commands.py`](../../haikode/commands.py), [`haikode/agents.py`](../../haikode/agents.py), [`haikode/skills.py`](../../haikode/skills.py), and [`haikode/memory.py`](../../haikode/memory.py) own their respective discovery and contracts. Their project inputs are untrusted until the project layer says otherwise. |
| Permissions and tools | [`haikode/permission.py`](../../haikode/permission.py) owns policy decisions. [`haikode/tool/__init__.py`](../../haikode/tool/__init__.py) is the registry entry point; individual tools remain under [`haikode/tool/`](../../haikode/tool/). Filesystem and shell hardening must remain centralized rather than copied into front ends. |
| Providers and transport | [`haikode/providers/base.py`](../../haikode/providers/base.py) defines provider/error semantics. Wire adapters are in [`haikode/providers/`](../../haikode/providers/); [`haikode/net.py`](../../haikode/net.py) owns HTTP/SSE, retry, timeout, TLS, and Haiku errno behavior. Model discovery and custom profiles live in [`haikode/models.py`](../../haikode/models.py). |
| Authentication and secrets | [`haikode/auth.py`](../../haikode/auth.py) dispatches login, [`haikode/oauth.py`](../../haikode/oauth.py) owns device flows and token persistence, and [`haikode/redact.py`](../../haikode/redact.py) owns output/environment redaction. Native key storage is implemented by [`tools/hai-keystore/`](../../tools/hai-keystore/). |
| TUI and REPL | [`haikode/tui.py`](../../haikode/tui.py) owns curses rendering and interaction. [`haikode/repl.py`](../../haikode/repl.py) owns plain and JSON front ends. Both use the same `TurnController` and `Agent`. |
| Native desktop boundary | [`desktop/src/app/HaiApplication.cpp`](../../desktop/src/app/HaiApplication.cpp) owns BeAPI launch/window creation; [`desktop/src/ui/HaiWindow.cpp`](../../desktop/src/ui/HaiWindow.cpp) owns the main UI; [`desktop/src/domain/AppController.cpp`](../../desktop/src/domain/AppController.cpp) owns worker process lifecycle and NDJSON framing; [`haikode/desktop_worker.py`](../../haikode/desktop_worker.py) adapts that protocol to the shared Python engine. Settings cross the boundary through [`desktop/src/domain/ConfigBridge.cpp`](../../desktop/src/domain/ConfigBridge.cpp) and [`haikode/configtool.py`](../../haikode/configtool.py). |
| MCP and LSP | [`haikode/mcp.py`](../../haikode/mcp.py) and [`haikode/lsp.py`](../../haikode/lsp.py) own optional external process protocols. They must degrade without making either dependency mandatory. |
| Packaging and deployment | [`scripts/build-hpkg.sh`](../../scripts/build-hpkg.sh) is the HPKG builder; [`scripts/install-on-haiku.sh`](../../scripts/install-on-haiku.sh) is explicitly a developer/non-packaged install; [`scripts/deploy-to-haiku.sh`](../../scripts/deploy-to-haiku.sh) updates a guarded Haiku checkout. |
| QA authorities | [`scripts/ci_baseline.py`](../../scripts/ci_baseline.py) enforces the known wiring baseline. [`tests/`](../../tests/) owns executable regression contracts, [`tests/render_tui.py`](../../tests/render_tui.py) owns terminal rendering checks, and [`benchmarks/`](../../benchmarks/) owns deterministic fixtures and performance probes. |

## High-coupling seams

Avoid parallel edits without explicit ownership across these groups:

- `agent.py`, `context.py`, `turn.py`, and `session.py`: transcript,
  compaction, checkpoint, and persistence invariants cross all four.
- `net.py` plus provider adapters: retries and replay safety depend on where a
  request becomes committed.
- `projectconfig.py`, `runtime.py`, `permission.py`, `agents.py`, and the tool
  registry: every layer may only narrow untrusted capability.
- `desktop_worker.py`, `AppController.cpp`, `Messages.h`, and `HaiWindow.cpp`:
  they form one versioned event contract despite living in two languages.
- `config.py`, `oauth.py`, `redact.py`, and `hai-keystore`: credential changes
  need end-to-end secret and migration review.
- Prompt files and prompt assembly: wording changes can alter tool behavior and
  enforcement expectations.

Database schema changes are additive migrations in `session.py` and require
round-trip, concurrency, failure, and downgrade/rollback analysis. Never edit a
live SQLite database to infer the migration contract.
