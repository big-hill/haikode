# haikode vs opencode — verified parity

**Last verified: 2026-07-27**, against haikode `0.1.0-m0m1` (1549 tests passing)
and the opencode checkout at
`scratchpad/opencode-src` (`packages/opencode`, `packages/tui`).

This document exists to stop us fooling ourselves. The failure mode it guards
against is real and has already happened here once: a module can be complete,
well-designed and thoroughly unit-tested, and still do **nothing at all**,
because no running code path ever calls it.

## Method

Every row was checked three ways:

1. **Read both implementations.** opencode's file is named in the row where the
   comparison is not obvious.
2. **Grep for call sites in `haikode/`, excluding the module's own file and
   excluding `tests/`.** A test is not a call site. A library that only its own
   test suite calls is not a feature.
3. **Look at the running program** where the feature is user-visible, using
   `tests/render_tui.py` (a pty + ECMA-48 screen reconstructor), and
   `haikode doctor` on the reference Haiku machine (hrev57937).

## Legend

| Mark | Means |
|---|---|
| **Yes** | Implemented and reachable from a running front-end. |
| **Partial** | Reachable, but materially narrower than opencode, or reachable from only some front-ends. |
| **Dead** | Code exists and passes tests, but **nothing outside its own module calls it**. From a user's point of view it does not exist. |
| **No** | Not implemented. |
| **n/a** | Deliberately out of scope for a Haiku-native, stdlib-only, serverless tool. |

---

## 1. Engine

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Agentic loop with native tool calling | `session/processor.ts` | **Yes** | `haikode/agent.py`; parallel calls, real `tool`-role messages |
| Streaming text | yes | **Yes** | `providers/base.py` SSE; TUI streams per chunk |
| Streaming reasoning / thinking | yes | **Yes** | Anthropic `thinking_delta`; six key spellings for OpenAI-compat (`providers/base.py:REASONING_KEYS`) |
| Step limit / agentic cap | yes | **Yes** | unlimited by default; an explicit `max_steps` reserves a tool-free final handoff, emits a continuable `limit` event, and resets on the next user message |
| Abort mid-run | yes | **Yes** | `Agent.abort()`, `esc` / `ctrl+c` in the TUI, `KeyboardInterrupt` in the REPL |
| Retry / backoff on provider errors | `session/retry.ts` | **Partial** | `net.py` retries transport errors; no per-model retry policy, no `dialog-retry-action` equivalent |
| Context-overflow handling | `session/overflow.ts` | **Partial** | `compact_history()` trims at request time; no summarising overflow recovery |
| Automatic compaction | `session/compaction.ts` | **No** | `Session.needs_compaction()` exists and **has no callers**. Compaction is manual (`/compact`) only |
| Prompt queueing while a run is in flight | yes (`session_queued_prompts`) | **No** | keybind name exists, defaults to `<leader>q`, no handler |
| Sub-agents (`task` tool) | yes | **Yes** | `tool/task.py` → nested `Agent`; `general` subagent |
| Cost accounting in currency | yes | **No** | `usage.py` counts tokens; `format_cost` exists but there is no price table |

## 2. Providers and auth

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| OpenAI-compatible dialect | yes | **Yes** | `providers/openai_compat.py` |
| Anthropic dialect | yes | **Yes** | `providers/anthropic.py` |
| Google Gemini dialect | yes | **Dead** | `providers/gemini.py` is complete and tested, but `runtime.build_provider()` only constructs Anthropic / OpenAI-compat / subscription providers, and no `gemini` profile exists in `config.DEFAULT_CONFIG` |
| ChatGPT subscription (device OAuth) | plugin | **Yes** | `providers/subscription.py`, `oauth.py` |
| SuperGrok subscription (RFC 8628) | plugin | **Yes** | same |
| Models.dev catalogue | yes | **No** | `models.py` lists what the endpoint's `/models` returns, with an on-disk cache; no curated metadata, no pricing, no capability flags |
| Custom provider profiles | yes | **Yes** | `haikode provider add`, TUI *Add provider* form |
| Key storage in an OS keyring | no | **Yes (haikode only)** | native `hai-keystore` (BKeyStore); opencode uses `auth.json` |
| `opencode auth login` device flows for many providers | yes | **Partial** | only ChatGPT and SuperGrok |
| Model variants / reasoning effort | `dialog-variant.tsx`, `variant_cycle` | **Partial** | `--effort`, `/effort`, provider config and `ctrl+t` cycle are live; no variant-list dialog |

## 3. Tools

opencode's registry: `apply_patch, bash, edit, glob, grep, invalid, list, lsp,
patch, question, read, skill, task, todo, webfetch, websearch, write,
code-mode, external-directory`.

| Tool | opencode | haikode | Evidence |
|---|---|---|---|
| `read` | yes | **Yes** | offset/limit, line numbers, read-before-write guard |
| `write` | yes | **Yes** | |
| `edit` | yes | **Yes** | exact string replacement |
| `apply_patch` | yes | **Yes** | `tool/apply_patch.py`, registered in `tool/__init__.py` |
| `glob` / `grep` / `list` | yes | **Yes** | pure Python; no ripgrep dependency |
| `bash` | yes | **Yes** | permission-guarded, timeout, output cap |
| `webfetch` | yes | **Yes** | text/markdown extraction |
| `todowrite` | yes | **Yes** | rendered as a live checklist; conditional system guidance tells the model to use it for multi-step work, not simple tasks |
| `task` (subagent) | yes | **Yes** | |
| `question` | yes | **Partial** | registered, but **no front-end fills `metadata["answers"]`** — the TUI, REPL and desktop asker all just approve or reject, so every question returns "Unanswered". Degrades instead of hanging, by design (`tool/question.py`), but it is not the feature opencode has |
| `memory_write` / `memory_read` | — | **Yes (haikode only)** | selective durable-memory guidance is present even for an empty store; later sessions load the index; `/memory` exposes editable files |
| `lsp` / diagnostics tool | yes | **Dead** | see §9 |
| `websearch` | yes | **No** | |
| `skill` | yes | **No** | |
| `code-mode` | yes | **No** | |
| `external-directory` | yes | **No** | |
| Output truncation | `truncate.ts` | **Yes** | per-tool caps |

## 4. Permissions

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| `allow` / `ask` / `deny` per key | yes | **Yes** | `permission.py` |
| Glob patterns, most-specific-first | yes | **Yes** | `fnmatch`, sorted by specificity |
| "Always" grants, session-scoped | yes | **Yes** | `grant_always()` |
| Persist a grant to config | yes | **Yes** | `persist()`; via `SessionConfig.save()` only the session's own additions reach the user's config, never the project file's rules |
| Per-agent permission overrides | `subagent-permissions.ts` | **Yes** | `AgentPermissions`, and grants that would bypass an agent's `deny` are filtered on switch |
| Detecting a project file that *loosens* permissions | no | **Yes (haikode only)** | `ProjectConfig.escalations()`, surfaced in `/status` and `doctor` |
| Interactive permission modal | yes | **Yes** | TUI modal with diff/command preview; REPL prompt; desktop NDJSON round trip |
| Headless deny-by-default | yes | **Yes** | no asker ⇒ every `ask` rejects |

## 5. Agents, prompts and instructions

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Built-in `build` / `plan` agents | yes | **Yes** | `agents.py` `BUILTIN` |
| `general` search subagent | yes | **Yes** | |
| Markdown agents with frontmatter | `config/agent.ts` | **Yes** | `.haikode/agent/*.md`, project + global, `agents/` accepted |
| `agents` block in project config | yes | **Yes** | merged on top of files |
| Agent restricts tool list | yes | **Yes** | `resolve_tools()` with a tool→permission-key map |
| Agent restricts permissions | yes | **Yes** | |
| Plan mode read-only in both dimensions | yes | **Yes** | tools filtered *and* write keys denied; `apply_patch` correctly dropped via its `edit` key |
| Plan enter/exit reminder injection | yes | **Yes** | `enter_plan_text()` / `exit_plan_text()` |
| Built-ins resist being loosened by a repo file | yes | **Yes** | `AgentDef.locked` / `_reassert_locks` |
| Per-model prompt variants | `session/system.ts` | **Yes** | 12 of opencode's 14 prompt texts ported; missing `copilot-gpt-5.txt` and `plan-reminder-anthropic.txt` |
| `# Haiku OS` briefing in every prompt | — | **Yes (haikode only)** | `prompts/haiku.md`, asserted by marker |
| `AGENTS.md` / `CLAUDE.md` chain | `session/instruction.ts` | **Yes** | plus `HAIKODE.md`, global `AGENTS.md`, `~/.claude/CLAUDE.md` |
| `instructions` globs from project config | yes | **Yes** | bounded scan, must resolve inside the project |
| Environment block (cwd, platform, git, tree) | yes | **Yes** | `ContextManager.environment_block()` |
| Agent switch keeps the conversation | yes | **Yes** | `switch_agent()` never touches `.messages` |
| Cycle agents with `tab` / `shift+tab` | yes | **Yes** | both dispatch; an active slash-command token retains Tab completion by focus |

## 6. Project configuration

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Config file discovery up the tree | `config/paths.ts` | **Yes** | global → each ancestor → cwd; nearest wins |
| `.haikode/haikode.json` beats `haikode.json` | yes (`.opencode/`) | **Yes** | |
| Reads `opencode.json` for compatibility | — | **Yes (haikode only)** | native file wins in the same directory |
| `instructions` concatenate, rest deep-merges | yes | **Yes** | |
| Unknown keys warn; real opencode keys ignored silently | partial | **Yes** | `IGNORED_KEYS` keeps an imported `opencode.json` from producing a wall of noise |
| Broken config never blocks startup | yes | **Yes** | reason collected in `.errors`, shown by `/status` and `doctor` |
| `$schema` / JSON-schema publishing | yes | **No** | |
| Variable interpolation (`{env:…}`, `{file:…}`) | `config/variable.ts` | **No** | |
| `mcp` block honoured | yes | **No** | key is accepted and validated, then nothing reads it (§9) |
| `theme`, `username` honoured | yes | **No** | accepted and validated, never read |
| Managed / enterprise config | yes | **n/a** | |

## 7. Commands

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Built-in slash commands | yes | **Yes** | 34 builtins in `repl.py:_builtins()` |
| Custom markdown commands | `command/index.ts` | **Yes** | `.haikode/command/*.md`, project + global |
| `$ARGUMENTS`, `$1`…`$9` | yes | **Yes** | |
| Inline `` !`shell` `` | yes | **Yes** | 10 s timeout |
| `@file` mentions | yes | **Yes** | `expand_mentions()` |
| Custom command `agent:` / `model:` frontmatter | yes | **No** | parsed into `CustomCommand.agent` / `.model` and then **never read** — `CommandRegistry.dispatch()` returns only the rendered prompt |
| Command palette (`ctrl+p`) | `command-palette.tsx` | **Partial** | opens and works, but see §8 — the REPL's 34 builtins and every custom command are **missing from it** |
| Tab completion of command names | yes | **Yes** | |
| `/init` scaffolding | yes | **Yes** | writes `haikode.json`, then has the model write `AGENTS.md` |

## 8. TUI

Verified by rendering the real program with `tests/render_tui.py`.

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Home screen: wordmark, cwd, provider/model, auth, tools, instructions | `routes/home.tsx` | **Yes** | rendered; degrades to one line below 24×60 |
| Streaming transcript with tool cards and diffs | yes | **Yes** | |
| Todo checklist rendering | `todo-item.tsx` | **Yes** | drawn from the *call*, so it survives a failed tool |
| Context meter next to the prompt | `component/prompt/index.tsx` | **Yes** | bar → percent → nothing as width shrinks; coloured by pressure |
| Status line (model, cwd, agent, tokens) | yes | **Yes** | context remains beside the prompt instead of being duplicated |
| Leader key (`ctrl+x`) with pending indicator | yes | **Yes** | |
| Keybinds configurable from config | `config/keybind.ts` | **Yes** | `keybinds` block; unknown names warn |
| Command palette | yes | **Partial** | 14 entries. `TUI._command_registry()` finds the REPL by `getattr(self.on_command, "__self__")`, but `main.py:_start_tui()` passes a **closure**, not a bound method — so the lookup returns `None` and no `/`-command is ever registered. Verified: `ctrl+p` renders "1/14" |
| Model dialog, favourites, recents | `dialog-model.tsx` | **Yes** | `ctrl+x m`; `ctrl+f` favourite, `ctrl+a` to providers |
| Provider dialog + add-provider form | `dialog-provider.tsx` | **Yes** | |
| Session dialog with full-text search | `dialog-session-list.tsx` | **Partial** | search, rename, delete and resume all work. Two gaps: it lists **all** directories (opencode filters to the project, with a toggle), and the "current session" marker is dead for the same `__self__` reason as the palette |
| Agent dialog | `dialog-agent.tsx` | **Yes** | `ctrl+x a` |
| Status dialog | `dialog-status.tsx` | **Yes** | `ctrl+x s` |
| Help / keybinding dialog | via palette | **Yes** | every definition has a focused dispatch path; unavailable curses-port features are labelled instead of silently swallowing a configured chord |
| MCP dialog | `dialog-mcp.tsx` | **No** | |
| Theme dialog / themes | `dialog-theme-list.tsx` | **No** | 3 semantic colours only |
| Skill dialog | `dialog-skill.tsx` | **No** | |
| Variant dialog | `dialog-variant.tsx` | **Partial** | effort cycles with `ctrl+t` and is set with `/effort`; no list dialog |
| Workspace / worktree / stash dialogs | several | **n/a / No** | |
| Session timeline, fork, tag, move | several | **No** | keybind names default to `"none"` |
| External editor (`ctrl+x e`) | yes | **No** | binding listed, no handler |
| Sidebar / file context toggles | yes | **No** | |
| Mouse support | yes | **Yes** | scroll wheel in the transcript |
| ASCII fallback for non-UTF-8 terminals | no | **Yes (haikode only)** | `Glyphs.detect()`, for serial and `TERM=vt100` |
| Session persistence from the TUI | yes | **No** | see §10 — this is the most serious gap in the table |

## 9. Integrations

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| **MCP client** | `src/mcp` | **Dead** | `haikode/mcp.py` is 1126 lines, complete (stdio + HTTP JSON-RPC, bounded startup, schema hardening, `mcp` permission key) and well tested. **Nothing in `haikode/` imports it.** No `MCPManager` is ever constructed, so the `mcp` config block is inert |
| **LSP client** | `src/lsp` | **Dead** | `haikode/lsp.py` is 1115 lines and complete. `tool/files.py` and `tool/apply_patch.py` *do* call `append_diagnostics(ctx, …)`, but that helper reads `getattr(ctx, "lsp", None)` and **`ToolContext.lsp` is never assigned anywhere** (`agent.py:155` constructs `ToolContext(cwd=…, permissions=…)`). Diagnostics are therefore always empty |
| Plugins | `src/plugin` | **No** | |
| Skills | `src/skill` | **No** | |
| Share links / hosted sessions | `src/share` | **n/a** | serverless by design |
| Formatters | `src/format` | **No** | |
| Git snapshots | `src/snapshot` | **n/a** | replaced by per-file snapshots in SQLite, because a Haiku install cannot assume git |
| IDE / ACP / editor extensions | `src/ide`, `src/acp` | **No** | |
| GitHub integration, PR/issue commands | `cli/cmd/github.ts` | **No** | |
| HTTP server + SDK + web UI | `src/server`, `packages/web` | **n/a** | the whole point is that there is no server |
| Desktop application | Electron/Tauri (`packages/desktop`) | **Yes, natively** | pure BeAPI C++ + NDJSON worker; runs the same agent loop |
| Haiku desktop integration (notifications, BFS attributes, Tracker, alerts) | — | **Dead** | `haikode/haiku.py` is 426 lines, complete and tested. **No importers.** No notification is ever raised, no exported transcript gets BFS attributes |

## 10. Sessions

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Persistent session store | yes | **Yes** | SQLite, `session.py` (1167 lines) |
| Auto title from the first message | yes | **Yes** | |
| List / resume / rename / delete | yes | **Yes** | |
| Archive | yes | **Partial** | `/archive` works; `unarchive()` has no caller and no UI |
| Full-text search over message bodies | yes | **Yes** | `SessionStore.search()` |
| Revert / undo file changes | `session/revert.ts` + git snapshots | **Yes** | checkpoint + per-file original text (`NULL` = did not exist); `/undo` restores and deletes created files |
| Redo | yes (`messages_redo`) | **No** | binding exists, no handler |
| Manual compaction | yes | **Yes** | `/compact` |
| Undo a compaction | yes | **Dead** | `Session.restore_compaction()` / `compactions()` have no callers |
| Per-session token totals and stats | yes | **Dead** | `token_totals()` and `stats()` have no callers; the UI counters come from the live `UsageTracker` instead |
| Export transcript | `cli/cmd/export.ts` | **Yes** | `/export` → markdown / text / json |
| Fork a session from a message | yes | **No** | |
| **Sessions from the TUI** | yes | **No** | `haikode/tui.py` calls `agent.run()` on a worker thread and contains **no** `capture_modified`, `session.append` or `checkpoint` call. Only `repl.send()` (REPL, one-shot) and `desktop_worker._persist()` write to the database. So in the TUI: a resumed session is read and then never extended, new conversations are never saved, and `/undo` reports "No session to undo" |

## 11. CLI

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Interactive TUI by default | yes | **Yes** | |
| One-shot `run` | `cli/cmd/run.ts` | **Yes** | `haikode "prompt"` |
| `--continue` / `--session` | yes | **Yes** | |
| `--agent`, `--model`, `--provider` | yes | **Yes** | |
| Auto-approve flag | yes | **Yes** | `--yes` |
| `doctor` / environment report | partial | **Yes (better)** | SSL, curses, sqlite3, config path, keystore, tools, every provider's auth, project config, instruction files, prompt variant, agents, memory, and all collected warnings |
| `models` / `providers` sub-commands | yes | **Partial** | `haikode provider …`; models are listed via `/models` in a session, not from the shell |
| `serve`, `web`, `attach`, `acp`, `stats`, `upgrade`, `import` | yes | **n/a / No** | |
| `agent create` generator | `cli/cmd/agent.ts` | **No** | |

## 12. Platform

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Runs on Haiku at all | **No** | **Yes** | that is the project |
| Zero runtime dependencies | no (Bun) | **Yes** | stdlib only, Python 3.10 |
| `.hpkg` package | no | **Yes** | `scripts/build-hpkg.sh`; a real `haikode-0.1.0-1-x86_64.hpkg` (284 KB) was built and `package list`-verified on hrev57937 |
| Deskbar entry, MIME signature | no | **Yes** | via the package |
| OS keyring for secrets | no | **Yes** | BKeyStore |

---

## Dead code inventory

Modules with **zero call sites** outside their own file and their tests. Each
one is a working, tested library and a feature that does not exist:

| File | Lines | What is lost |
|---|---|---|
| `haikode/mcp.py` | 1126 | all MCP servers |
| `haikode/lsp.py` | 1115 | all diagnostics after an edit |
| `haikode/haiku.py` | 426 | notifications, BFS attributes, Tracker, native alerts |
| `haikode/providers/gemini.py` | 275 | Google Gemini as a provider |

Plus these individually dead entry points inside otherwise-live modules:

| Symbol | Consequence |
|---|---|
| `palette.build_default_palette` / `DEFAULT_COMMANDS` / `resolve_handler` | the TUI builds its own 14-item palette; this curated 20-command table (with `session.undo`, `session.export`, `session.rename`, `auth.login`, `auth.logout`, `permission.list`, `tool.list`, `todo.list`) is never shown |
| `models.probe` | no "test this endpoint" action |
| `ModelCatalog.cycle_favourite` | `model_cycle_favorite` can never be bound usefully |
| `Session.needs_compaction` | no automatic compaction |
| `Session.restore_compaction` / `compactions` | a compaction cannot be undone |
| `Session.token_totals` / `stats` / `files_touched` | no per-session cost or footprint report |
| `Session.unarchive` | archiving is one-way |
| `CustomCommand.agent` / `.model` | command frontmatter silently ignored |

`VERIFICATION.md` at the repository root is **stale** — dated 2026-07-14, it
describes a 20-test suite and says "HPKG packaging is not yet provided". Both
statements are now wrong (1549 tests; the package builds and installs). Treat
this file as the current record.

---

## Ranked: what is still missing

Ordered by *user-visible harm per unit of work*, not by size.

1. **Sessions do not persist from the TUI.** The default front-end silently
   discards every conversation and leaves `/undo` inert — which is exactly the
   safety net that justifies letting an agent edit files on a machine without
   git. The engine, the store and the snapshot logic all exist and are used by
   the REPL; the TUI's `_submit`/`_run_agent` path just never calls them. This
   is the single highest-value fix in the project.

2. **The command palette is empty of commands.** `ctrl+p` is opencode's main
   discovery surface. Ours lists 14 UI actions and none of the 34 slash
   commands or any custom command, because `TUI._command_registry()` probes
   `on_command.__self__` and `main.py` hands it a closure. It is close to a
   one-line fix (pass `repl.handle_command` bound, or expose the registry
   explicitly) and it changes how discoverable the whole tool is.

3. **MCP is not connected.** 1126 tested lines, zero users. MCP is how a coding
   agent reaches anything the built-in tools do not cover, and the config key is
   already parsed and validated. Needs an `MCPManager` built in
   `runtime.build_agent()` and its proxy tools merged into the registry.

4. **LSP diagnostics are not connected.** 1115 tested lines, and the call sites
   in `edit`/`write` already exist — the only missing link is assigning
   `ctx.lsp`. Without it the model never learns that the edit it just made does
   not compile.

5. **Some configured keybinding targets remain unavailable.** The help dialog
   now marks them and the dispatcher reports them, but external editor, themes,
   sidebar, message copy/redo and several hosted opencode features still have
   no curses implementation.

6. **No automatic compaction.** Long sessions hit the context wall and the user
   has to know to type `/compact`. `needs_compaction()` is written and tested;
   it needs a caller in the run path.

7. **The `question` tool never gets an answer.** Registered and advertised to
   the model, but no asker fills `metadata["answers"]`, so every question costs
   a turn and returns "Unanswered". Needs a small choice modal in the TUI and a
   numbered prompt in the REPL.

8. **Gemini is unreachable.** A finished provider that `build_provider()` does
   not construct and that has no default profile. Two small additions.

9. **The Haiku integration module is unused.** Notifications after a long run,
   BFS attributes on exported transcripts, Tracker integration — the things
   that make this feel like a Haiku application rather than a port — are all
   written and never called.

10. **Custom command `agent:` / `model:` frontmatter is ignored.** Documented in
    the file format, parsed, dropped by `dispatch()`.

11. **Session list is not scoped to the project.** It shows every directory's
    sessions, where opencode defaults to the current one with a toggle
    (`app_toggle_session_directory_filter`).

12. **Missing TUI features with existing keybind names:** external editor
    (`ctrl+x e`), message copy (`<leader>y`) and redo (`<leader>r`). They are
    now reported as unavailable rather than silently ignored.

13. **Themes and a variant-list dialog.** `theme` is accepted in config and
    never read. Reasoning effort now has CLI, slash-command and cycle controls,
    but no opencode-style variant picker. Neither blocks work.

14. **Model catalogue metadata.** No models.dev equivalent: no pricing, no
    context-length or capability data, so cost reporting stays token-only and
    the model list is whatever the endpoint returns.

15. **Skills, plugins, websearch, code-mode, formatters, session fork/timeline,
    prompt queueing, redo.** Genuinely absent; none of them is load-bearing for
    "replace opencode on Haiku".

Deliberately **not** on this list, because they are out of scope for a
serverless, Haiku-native tool: the HTTP server and SDK, the web UI, hosted
session sharing, IDE/ACP bridges, GitHub automation, worktrees and workspaces,
and git-based snapshots (replaced by the SQLite per-file snapshots that `/undo`
already uses).
