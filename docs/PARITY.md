# haikode vs opencode — verified parity

**Last parity audit: 2026-08-12**, against the opencode checkout at
`scratchpad/opencode-src` (`packages/opencode`, `packages/tui`). The current
2026-08-15 haikode baseline is 2459 tests: 4 skips and **exactly 4
deliberate failures**, all in `tests/test_wiring_audit.py`, which pin the
remaining dead code described below.

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
| Agentic loop with native tool calling | `session/processor.ts` | **Yes** | `haikode/agent.py`; accepts several calls in one model response, executes them deterministically in emitted order, and writes real `tool`-role messages |
| Streaming text | yes | **Yes** | `providers/base.py` SSE; TUI streams per chunk |
| Streaming reasoning / thinking | yes | **Yes** | Anthropic `thinking_delta`; six key spellings for OpenAI-compat (`providers/base.py:REASONING_KEYS`) |
| Step limit / agentic cap | yes | **Yes** | unlimited by default; an explicit `max_steps` reserves a tool-free final handoff, emits a continuable `limit` event, and resets on the next user message |
| Abort mid-run | yes | **Yes** | `Agent.abort()`, `esc` / `ctrl+c` in the TUI, `KeyboardInterrupt` in the REPL; an interrupt also discards queued prompts *and* pending steering (`tui._drop_queued`) |
| Retry / backoff on provider errors | `session/retry.ts` | **Partial** | `net.py` retries transport errors with one bounded ladder; ChatGPT no longer multiplies an exhausted transport ladder through its SSE-event retry loop, and the measured terminal Anthropic 429 stops after one request. There is still no per-model retry policy or `dialog-retry-action` equivalent |
| Context-overflow handling | `session/overflow.ts` | **Partial** | a provider-reported pre-output overflow forces one anchored compaction and exactly one main-request retry; a failure after any streamed text/reasoning/tool delta is never replayed. The compaction model/effort is still the active model rather than a separately configurable compaction agent |
| Automatic compaction | `session/compaction.ts` | **Yes** | `Agent` keeps a lossless raw transcript and a separate provider-facing history. A successful summary is latched across tool rounds and stored as a validated SQLite context checkpoint, so a fresh desktop worker reuses it; failed summaries remain transient. The budget uses the per-model **input** window and a live provider-calibrated estimate that includes tool schemas. `/compact` and `/compact undo` remain for durable manual control |
| Prompt queueing while a run is in flight | yes (`session_queued_prompts`) | **Yes** | prompts typed mid-run land in a pinned band above the prompt (`build_pinned_queue_lines`); `ctrl+x q` (`<leader>q`) opens an edit/drop dialog; a steered message (`/steer`, `agent.steer`/`pending_steering`) reaches the model at its next step and is cleared on interrupt |
| Sub-agents (`task` tool) | yes | **Yes** | `tool/task.py` → nested `Agent`, with a `subagent_type` argument; `general` and `explore` built-ins plus custom subagents |
| Cost accounting in currency | yes | **No** | `/cost` prints token totals (`usage.summary_line`); `usage.estimate_cost()` still has **no caller** and there is still no price table |

## 2. Providers and auth

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| OpenAI-compatible dialect | yes | **Yes** | `providers/openai_compat.py`; asks for `stream_options: {"include_usage": true}` and backs off per endpoint when it is rejected (openai_compat.py:164, 229) |
| Anthropic dialect | yes | **Yes** | `providers/anthropic.py` |
| Google Gemini dialect | yes | **Yes** | `runtime.build_provider()` dispatches `dialect: "gemini"` (runtime.py:249) to `providers/gemini.py`; reachable via `haikode provider add` (there is still no built-in Gemini profile in `config.DEFAULT_CONFIG`) |
| ChatGPT subscription (device OAuth) | plugin | **Yes** | `providers/subscription.py`, `oauth.py` |
| SuperGrok subscription (RFC 8628) | plugin | **Yes** | same |
| Models.dev catalogue | yes | **Partial** | still no pricing or capability flags, but per-model **context windows** now come from endpoint metadata (`ModelCatalog.context_for`, models.py:221; Ollama `/api/show` `num_ctx`, configtool.py:130–206) and feed the compaction budget (runtime.py:426) |
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
| `write` | yes | **Yes** | atomic replace preserves ownership, timestamps and BFS attributes (`haiku.copy_attributes`, tool/files.py:184) |
| `edit` | yes | **Yes** | exact string replacement |
| `apply_patch` | yes | **Yes** | `tool/apply_patch.py`, registered in `tool/__init__.py` |
| `glob` / `grep` / `list` | yes | **Yes** | pure Python; no ripgrep dependency |
| `bash` | yes | **Yes** | permission-guarded, timeout, output cap |
| `webfetch` | yes | **Yes** | text/markdown extraction |
| `todowrite` | yes | **Yes** | rendered as a live checklist; conditional system guidance tells the model to use it for multi-step work, not simple tasks |
| `task` (subagent) | yes | **Yes** | |
| `question` | yes | **Yes** | both main front-ends now fill `metadata["answers"]`: a numbered prompt in the REPL (`repl.py:terminal_asker`) and a choice modal in the TUI (tui.py:5340). The desktop asker still only approves/rejects, so a question there returns "Unanswered" |
| `skill` | yes | **Yes** | `tool/skill.py`, registered; the catalogue is advertised in the system prompt so the model knows what to load (§9) |
| `memory_write` / `memory_read` | — | **Yes (haikode only)** | selective durable-memory guidance is present even for an empty store; later sessions load the index; `/memory` exposes editable files |
| `lsp` / diagnostics tool | yes | **Partial** | no standalone `lsp` tool, but diagnostics are appended to every edit/write/patch result via `ctx.lsp` (tool/diagnostics.py) — see §9 |
| `websearch` | yes | **No** | |
| `code-mode` | yes | **No** | |
| `external-directory` | yes | **No** | |
| Output truncation | `truncate.ts` | **Yes** | per-tool caps |

## 4. Permissions

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| `allow` / `ask` / `deny` per key | yes | **Yes** | `permission.py`; MCP proxy tools sit behind the `mcp` key |
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
| `general` search subagent | yes | **Yes** | plus `explore`, a read-only locator (agents.py:210), named by the plan prompt and reachable through `task`'s `subagent_type` |
| Markdown agents with frontmatter | `config/agent.ts` | **Yes** | `.haikode/agent/*.md`, project + global, `agents/` accepted |
| `agents` block in project config | yes | **Yes** | merged on top of files |
| Agent restricts tool list | yes | **Yes** | `resolve_tools()` with a tool→permission-key map |
| Agent restricts permissions | yes | **Yes** | |
| Plan mode read-only in both dimensions | yes | **Yes** | tools filtered *and* write keys denied; `apply_patch` correctly dropped via its `edit` key |
| Plan enter/exit reminder injection | yes | **Yes** | `enter_plan_text()` / `exit_plan_text()`; the plan agent's tool list also carries `plan_exit` (agents.py:57, tool/plan.py) — the model ends plan mode by asking approval, and approval switches to build |
| Built-ins resist being loosened by a repo file | yes | **Yes** | `AgentDef.locked` / `_reassert_locks` |
| Per-model prompt variants | `session/system.ts` | **Yes** | `haikode/prompts/` carries per-model texts (anthropic, beast, codex, gemini, gpt, kimi, meta, trinity, …); still no `copilot-gpt-5.txt` or `plan-reminder-anthropic.txt` |
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
| `mcp` block honoured | yes | **Yes** | read by `runtime.build_agent()` (runtime.py:388–395), which builds the `MCPManager` from it (§9) |
| `theme`, `username` honoured | yes | **No** | accepted and validated, never read; `theme` is pinned as the config audit's deliberate failure (`ProjectConfigKeysThatGoNowhere`) |
| Custom `shell` honoured | yes | **Yes** | `runtime.build_agent()` assigns the effective setting to `agent.ctx.shell`, which the bash tool consumes |
| Managed / enterprise config | yes | **n/a** | |

## 7. Commands

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Built-in slash commands | yes | **Yes** | 40 builtins in `repl.py:_builtins()`, now including `/mcp`, `/steer`, `/cost`, `/fork` |
| Custom markdown commands | `command/index.ts` | **Yes** | `.haikode/command/*.md`, project + global |
| `$ARGUMENTS`, `$1`…`$9` | yes | **Yes** | |
| Inline `` !`shell` `` | yes | **Yes** | 10 s timeout |
| `@file` mentions | yes | **Yes** | `expand_mentions()` |
| Custom command `agent:` / `model:` frontmatter | yes | **No** | still parsed into `CustomCommand.agent` / `.model` and then **never read** — `CommandRegistry.dispatch()` (commands.py:424) returns only the rendered prompt |
| Command palette (`ctrl+p`) | `command-palette.tsx` | **Yes** | `CommandBridge` (main.py:600) exposes the registry to the TUI, which registers every builtin (minus 12 shadowed duplicates) and every custom command (`tui._register_slash_commands`). The old `__self__` probe that left the palette empty is gone |
| Tab completion of command names | yes | **Yes** | |
| `/init` scaffolding | yes | **Yes** | `turn.prepare_init()` writes `haikode.json`, then has the model write `AGENTS.md`; shared by REPL and TUI |

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
| Command palette | yes | **Yes** | 14 UI actions plus every slash and custom command (see §7). The curated `palette.DEFAULT_COMMANDS` table is still unused, so 9 of its ids (`session.undo`, `session.export`, `session.rename`, `auth.login`, `auth.logout`, `permission.list`, `provider.default`, `todo.list`, `tool.list`) exist only as `/`-commands, not as named palette actions — pinned as two deliberate audit failures |
| Queued-prompt band + dialog | yes | **Yes** | pinned band between plan and prompt; `ctrl+x q` edits/drops; steered items labelled "taken at the next step" |
| Model dialog, favourites, recents | `dialog-model.tsx` | **Yes** | `ctrl+x m`; `ctrl+f` favourite, `ctrl+a` to providers |
| Provider dialog + add-provider form | `dialog-provider.tsx` | **Yes** | |
| Session dialog with full-text search | `dialog-session-list.tsx` | **Partial** | search, rename, delete and resume all work, and the "current session" marker is now real (`_current_session_id()` reads the shared TurnController). It still lists **all** directories; `app_toggle_session_directory_filter` remains unavailable |
| Agent dialog | `dialog-agent.tsx` | **Yes** | `ctrl+x a` |
| Status dialog | `dialog-status.tsx` | **Yes** | `ctrl+x s` |
| Help / keybinding dialog | via palette | **Yes** | every definition has a focused dispatch path; unavailable curses-port features are labelled instead of silently swallowing a configured chord |
| MCP dialog | `dialog-mcp.tsx` | **Partial** | the `mcp_list` binding and the palette both dispatch `/mcp` (tui.py:4681) — a textual server/tool report (`skills.mcp_report`), not opencode's interactive dialog |
| Theme dialog / themes | `dialog-theme-list.tsx` | **No** | 3 semantic colours only |
| Skill dialog | `dialog-skill.tsx` | **No** | `prompt_skills` is in `UNAVAILABLE_BINDINGS` |
| Variant dialog | `dialog-variant.tsx` | **Partial** | effort cycles with `ctrl+t` and is set with `/effort`; no list dialog |
| Workspace / worktree / stash dialogs | several | **n/a / No** | |
| Session timeline, fork, tag, move | several | **Partial** | forking exists as `/fork`, `--fork` and `haikode sessions fork` (whole session, §10), just not from a TUI dialog; timeline, tag and move are absent |
| External editor (`ctrl+x e`) | yes | **No** | `editor_open` is in `UNAVAILABLE_BINDINGS`: consumed and reported, not implemented |
| Sidebar / file context toggles | yes | **No** | |
| Mouse support | yes | **Yes** | scroll wheel in the transcript during and after a turn; a single Up/Down press still browses prompt history |
| ASCII fallback for non-UTF-8 terminals | no | **Yes (haikode only)** | `Glyphs.detect()`, for serial and `TERM=vt100` |
| Session persistence from the TUI | yes | **Yes** | every turn runs through the shared `TurnController.run_turn()` (tui.py:2270, 2712); the wiring audit asserts a TUI turn writes a session row (`TUIUsesTheTurnController`) — see §10 |

## 9. Integrations

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| **MCP client** | `src/mcp` | **Yes** | `runtime.build_agent()` constructs `MCPManager` when the config has an `mcp` block (runtime.py:390) and merges its proxy tools into the agent behind the `mcp` permission key (`agent.attach_mcp`, agent.py:497). A connecting or dead server degrades to an honest `mcp_<name>_status` stand-in tool (mcp.py:858) instead of vanishing; servers die with the process (atexit). `/mcp` lists servers in the REPL and TUI |
| **LSP client** | `src/lsp` | **Yes** | `runtime.build_agent()` assigns `agent.ctx.lsp = LSPManager.from_config()` (runtime.py:382); edit/write/patch append diagnostics through `tool/diagnostics.py`; servers spawn lazily per language and shut down atexit (lsp.py:953); `lsp: false` opts out |
| Plugins | `src/plugin` | **No** | |
| Skills | `src/skill` | **Yes** | `haikode/skills.py` scans `{skill,skills}/**/SKILL.md`, project + global, as opencode does; the catalogue reaches the system prompt (`agent._skills_block`, agent.py:718), the `skill` tool loads one on demand, and SKILL.md warnings surface (wiring audit `SkillsAreWired`) |
| Share links / hosted sessions | `src/share` | **n/a** | serverless by design |
| Formatters | `src/format` | **No** | |
| Git snapshots | `src/snapshot` | **n/a** | replaced by per-file snapshots in SQLite, because a Haiku install cannot assume git |
| IDE / ACP / editor extensions | `src/ide`, `src/acp` | **No** | |
| GitHub integration, PR/issue commands | `cli/cmd/github.ts` | **No** | |
| HTTP server + SDK + web UI | `src/server`, `packages/web` | **n/a** | the whole point is that there is no server |
| Desktop application | Electron/Tauri (`packages/desktop`) | **Yes, natively** | pure BeAPI C++ + NDJSON worker; `B_MULTIPLE_LAUNCH` gives each window an independent session and provider/model/effort route, while every worker runs the same agent loop through the same TurnController |
| Haiku desktop integration (notifications, BFS attributes, Tracker, alerts) | — | **Partial** | `haiku.copy_attributes()` now preserves BFS attributes across every atomic file replace (tool/files.py:184). The rest of `haikode/haiku.py` (494 lines) — notifications, Tracker, native alerts, attributes on exported transcripts — is still **never called** |

## 10. Sessions

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Persistent session store | yes | **Yes** | SQLite, `session.py` (1649 lines) |
| Auto title from the first message | yes | **Yes** | |
| List / resume / rename / delete | yes | **Yes** | |
| Archive | yes | **Partial** | `/archive` works; `unarchive()` still has no caller and no UI |
| Full-text search over message bodies | yes | **Yes** | `SessionStore.search()`; the `session_history` tool (session.py:1545, registered) also lets the *model* list and read past sessions |
| Revert / undo file changes | `session/revert.ts` + git snapshots | **Yes** | checkpoint + per-file original text (`NULL` = did not exist); `/undo` restores and deletes created files, and fails closed while persistence is broken (`turn.undo_available`) |
| Redo | yes (`messages_redo`) | **No** | binding is reported unavailable; no handler |
| Manual compaction | yes | **Yes** | `/compact` |
| Undo a compaction | yes | **Yes** | `/compact undo` (and `/compact restore`) calls `Session.restore_compaction()`, reloads the agent transcript and invalidates the automatic context checkpoint |
| Per-session token totals and stats | yes | **Yes** | `Session.stats()` (which folds `token_totals()`, `files_touched()` and `compactions()`) feeds `haikode sessions show` (main.py:358) and every JSON export; the live UI counters still come from `UsageTracker` |
| Export transcript | `cli/cmd/export.ts` | **Yes** | `/export` and `haikode sessions export` → markdown / text / json |
| Fork a session from a message | yes | **Partial** | `/fork`, `--fork` and `haikode sessions fork` copy the whole session so it can be branched; opencode's fork-from-a-*message* does not exist |
| **Sessions from every front-end** | yes | **Yes** | `turn.py:TurnController` owns open-session → checkpoint → run → persist for all three front-ends (repl.py:451, tui.py:2712, desktop_worker.py:441). The wiring audit forbids any front-end calling `agent.run()` directly (`OnlyTurnOwnsTheLifecycle`) and asserts a TUI turn writes a session row |

## 11. CLI

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Interactive TUI by default | yes | **Yes** | |
| One-shot `run` | `cli/cmd/run.ts` | **Yes** | `haikode "prompt"`, `haikode run …` when the words would collide with a sub-command |
| `--continue` / `--session` | yes | **Yes** | |
| `--agent`, `--model`, `--provider` | yes | **Yes** | |
| Auto-approve flag | yes | **Yes** | `--yes` |
| `doctor` / environment report | partial | **Yes (better)** | SSL, curses, sqlite3, config path, keystore, tools, every provider's auth, project config, instruction files, prompt variant, agents, memory, and all collected warnings |
| `models` / `providers` sub-commands | yes | **Yes** | `haikode provider …`, `haikode models [PROVIDER] [--json --refresh]` (main.py:448), `haikode agent [NAME]`, `haikode sessions list/show/export/import/delete/rename/fork` |
| `serve`, `web`, `attach`, `acp`, `stats`, `upgrade`, `import` | yes | **Partial / n/a** | `import` now exists (`haikode import`, `haikode sessions import`) and per-session stats come from `sessions show`; `serve`/`web`/`attach`/`acp` stay n/a, `upgrade` is No |
| `agent create` generator | `cli/cmd/agent.ts` | **No** | `haikode agent` lists and describes agents (main.py:482); there is still no generator |

## 12. Platform

| Feature | opencode | haikode | Evidence |
|---|---|---|---|
| Runs on Haiku at all | **No** | **Yes** | that is the project |
| Zero runtime dependencies | no (Bun) | **Yes** | stdlib only, Python 3.10 |
| `.hpkg` package | no | **Yes** | `scripts/build-hpkg.sh`; release packages are built and `package list`-verified on physical x86_64 and x86_gcc2 machines |
| Deskbar entry, MIME signature | no | **Yes** | via the package |
| OS keyring for secrets | no | **Yes** | BKeyStore |

---

## Dead code inventory

The four fully dead modules of the last audit — `mcp.py`, `lsp.py`,
`providers/gemini.py`, `haiku.py` — are gone from this table as modules:
the first three are wired (§9, §2) and `haiku.py` has its first production
caller. What remains dead, verified by grep and pinned by the audit's four
deliberate failures:

| File | Lines | What is still lost |
|---|---|---|
| `haikode/haiku.py` | 494 | everything except `copy_attributes()`: notifications after a long run, Tracker integration, native alerts, BFS attributes on exported transcripts |

Plus these individually dead entry points inside otherwise-live modules:

| Symbol | Consequence |
|---|---|
| `palette.build_default_palette` / `DEFAULT_COMMANDS` / `resolve_handler` | the TUI's own palette now carries every slash command, but this curated table is still never consulted, and 9 of its ids have no named palette action (pinned: `PaletteDefaultCommandSetIsUsed`, 2 failures) |
| `palette.move_to` / `page_count` / `selected_positions` / `unregister` / `select_list` | dead widget helpers |
| `models.probe` | no "test this endpoint" action |
| `ModelCatalog.cycle_favourite` | `model_cycle_favorite` remains in `UNAVAILABLE_BINDINGS` |
| `usage.estimate_cost` | no cost in currency; no price table anywhere |
| `keybind.bindings_for` / `help_rows` | the help dialog builds its own rows |
| `Session.unarchive` | archiving is one-way |
| `Session.set_tokens` | dead setter |
| `Session.needs_compaction` | vestigial wrapper — the live decision is `context.needs_compaction()`, taken on every request |
| `CustomCommand.agent` / `.model` | command frontmatter silently ignored |
| project-config `theme`, `username` | validated but not consumed (`theme` is pinned by `ProjectConfigKeysThatGoNowhere`) |

The four deliberate failures in `tests/test_wiring_audit.py` are exactly this
inventory's guard: `NoDeadPublicFunctions` (the symbol list above),
`PaletteDefaultCommandSetIsUsed` ×2, and
`ProjectConfigKeysThatGoNowhere.test_theme_is_consumed`. When one of them
starts passing, delete its row here. `VERIFICATION.md` is the platform evidence
record; this file remains the parity inventory.

---

## Ranked: what is still missing

Ordered by *user-visible harm per unit of work*, not by size. The former top
four — TUI session persistence, the empty command palette, unconnected MCP and
unconnected LSP — are fixed and verified above.

1. **No price table.** Context windows now come from the endpoints, but cost
   reporting is token-only: `usage.estimate_cost()` is written and dead, and
   there is no models.dev-equivalent pricing data to feed it.

2. **Shell mode and attachments.** opencode's prompt accepts a `!` prefix for
   a shell command and image/file attachments; haikode's prompt has neither
   (no front-end handles a leading `!`, nothing builds an image content part).

3. **Themes.** `theme` is accepted in config and never read; there is no theme
   dialog and only 3 semantic colours. Pinned as a deliberate audit failure so
   it cannot be quietly forgotten. `username` remains in the same
   validated-but-unread state; `shell` is wired to the bash tool.

4. **Missing TUI features with existing keybind names:** external editor
   (`ctrl+x e`), message copy (`<leader>y`) and redo (`<leader>r`). They are
   reported as unavailable rather than silently ignored, but they do not work.

5. **Session list is not scoped to the project.** It shows every directory's
   sessions, where opencode defaults to the current one with a toggle
   (`app_toggle_session_directory_filter`, currently unavailable).

6. **Custom command `agent:` / `model:` frontmatter is ignored.** Documented in
   the file format, parsed, dropped by `dispatch()`.

7. **Archiving is one-way.** `unarchive()` still has no caller. Manual
   compaction, by contrast, can now be reversed with `/compact undo`.

8. **The desktop asker cannot answer the `question` tool.** REPL and TUI now
   fill `metadata["answers"]`; the desktop NDJSON protocol still only
   approves or rejects, so a question there degrades to "Unanswered".

9. **The curated palette table has diverged.** The TUI palette is full now,
   but `palette.DEFAULT_COMMANDS` — the opencode-shaped command map — is
   still unused, and 9 declared ids have no named palette action. Hygiene,
   pinned by two audit failures.

10. **The Haiku integration module is mostly unused.** BFS attributes now
    survive file edits, but notifications after a long run, attributes on
    exported transcripts, Tracker and native alerts — the things that make
    this feel like a Haiku application rather than a port — are written and
    never called.

11. **Fork-from-a-message and the session timeline.** `/fork` copies a whole
    session; opencode can branch from any message and show a timeline.

12. **Websearch, code-mode, external-directory, formatters, plugins.**
    Genuinely absent; none of them is load-bearing for "replace opencode on
    Haiku".

Deliberately **not** on this list, because they are out of scope for a
serverless, Haiku-native tool: the HTTP server and SDK, the web UI, hosted
session sharing, IDE/ACP bridges, GitHub automation, worktrees and workspaces,
and git-based snapshots (replaced by the SQLite per-file snapshots that `/undo`
already uses).
