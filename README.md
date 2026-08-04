# haikode — an AI coding agent that runs on Haiku

```
⠀⠀⠀⠀⠀⠀⠀⠀⣀⣠⣤⣤⣄⣀⣀⡀⠀⠀⠀
⠀⠀⠀⠀⠀⢀⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣶⠶   ▄         ▀ ▄            ▄
⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠃⠀   █▀▀▄  ▀▀█ █ █ ▄▀ █▀▀█ █▀▀█ █▀▀█
⠀⠀⠀⢀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⠀   █  █ █▀▀█ █ █▀▄  █  █ █  █ █▀▀▀
⢀⣠⠞⠋⠉⠛⠻⠿⣿⣿⣿⠿⠟⠋⠀⠀⠀⠀⠀   ▀  ▀ ▀▀▀▀ ▀ ▀ ▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀
⠞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
```

`haikode` is a coding agent that runs **natively on Haiku** and talks **directly
to cloud providers** over HTTPS. There is no `opencode serve`, no SSH tunnel, no
Node, no Bun and no second computer in the loop. The agent loop, the provider
clients, OAuth refresh, the tools, permissions, sessions, memory and all three
front-ends are **Python 3 standard library only**, because Haiku has no usable
pip ecosystem.

It is a from-scratch reimplementation of [opencode](https://opencode.ai)
(MIT), ported feature by feature rather than wrapped. See
[docs/PARITY.md](docs/PARITY.md) for an honest, verified account of what is
ported, what is partial and what is missing.

Three front-ends over one engine:

* a **curses TUI** — the default on an interactive terminal
* a **plain REPL** — the automatic fallback, and `--no-tui`
* a **native BeAPI desktop app** — `haikode-desktop`

```
haikode                       # TUI in the current directory
haikode "why does this fail"  # one-shot, prints and exits
haikode -C ~/src/myproject    # start somewhere else
haikode doctor                # what is configured and what is broken
```

---

## The test suite fails 5 tests on purpose

Before you conclude the project is broken: `python3 -m unittest discover -s
tests -t .` reports **5 failures, all in `tests/test_wiring_audit.py`**, and
that is the expected result. Any other failure is a real one.

Those 5 are executable bug reports. Each names a subsystem that exists, is
tested in isolation and is reachable from nothing — MCP, LSP, the skill
catalogue, automatic compaction and a few dead config keys. They are kept
failing rather than deleted so the gap is visible in every run instead of
living in a stale TODO. Fixing one should make its test pass without the test
being weakened.

The count is the same on macOS and on Haiku. See
[issue #4](https://github.com/big-hill/haikode/issues/4) for the list.

---

## Requirements

| Need | How |
|---|---|
| Haiku R1/beta5 or newer | hrev57937 is the reference machine |
| Python 3.10+ | `pkgman install python3` |
| TLS root certificates | `pkgman install ca_root_certificates` |
| `sqlite3` module | ships with Haiku's python3; without it sessions are disabled |
| `curses` module | ships with Haiku's python3; without it you get the REPL |

Nothing else. No pip install, ever.

## Install

### The package (preferred)

The Haiku-native way. Build the `.hpkg` on the Haiku machine, then install it
with the package manager so it can also be cleanly removed:

```sh
cd /boot/home/haikode
scripts/build-hpkg.sh                                  # needs haiku_devel
pkgman install build/haikode-0.1.0-1-$(uname -m).hpkg
```

That installs into `/boot/system`:

```
apps/haikode/haikode                        native BeAPI desktop app
bin/haikode                                 CLI launcher
bin/hai                     -> haikode      compatibility name
bin/haikode-desktop         -> ../apps/…    desktop app on $PATH
bin/hai-desktop             -> ../apps/…    compatibility name
bin/hai-keystore                            BKeyStore helper
lib/python3.10/vendor-packages/haikode/     the Python package
data/deskbar/menu/Applications/haikode      Deskbar entry
```

`vendor-packages` is already on the stock `python3.10` search path, so the
packaged launcher needs no `PYTHONPATH`. Remove it again with
`pkgman uninstall haikode`.

### Developer install (a source checkout that runs)

```sh
cd /boot/home/haikode
sh scripts/install-on-haiku.sh
```

This installs unmanaged copies under `/boot/home/config/non-packaged/`
(`bin/haikode`, `bin/hai`, `bin/hai-keystore`, `apps/haikode/haikode`) and
points the launcher at the checkout, so the tree you edit is the tree that
runs. `pkgman` knows nothing about these files. If you later install the
package, delete the non-packaged copies first or they will shadow
`/boot/system/bin`.

### No install at all

```sh
PYTHONPATH=$HOME/haikode python3 -m haikode
```

## First run

```sh
haikode doctor        # SSL, curses, sqlite3, config path, keystore, tools,
                      # every provider's auth status, the project config,
                      # instruction files, the prompt variant, agents, memory
haikode login zen     # or ollama, anthropic, openai, chatgpt, …
haikode               # start the TUI
```

`doctor` is the first thing to run on a fresh machine and the first thing to
paste into a bug report. Real output from the reference Haiku box:

```
haikode doctor
✓ SSL OK
✓ curses available (TUI enabled)
✓ sqlite3 available (sessions enabled)
✓ Config: /boot/home/config/settings/haikode/config.json
✓ Keystore helper: /boot/home/config/non-packaged/bin/hai-keystore
✓ Tools: apply_patch, bash, edit, glob, grep, list, memory_read, memory_write,
         question, read, task, todowrite, webfetch, write
* ollama       key: keystore
  zen          key: config
  …
✓ Prompt variant: default (for glm-5.2)
✓ Agents: build*, plan | subagents: general
✓ Memory: 0 saved (/boot/home/.haikode/memory)
```

### Where things live

| What | Haiku | Elsewhere |
|---|---|---|
| Config | `~/config/settings/haikode/config.json` | `~/.config/haikode/config.json` |
| OAuth tokens | `~/config/settings/haikode/oauth.json` (mode 0600) | same, under `~/.config` |
| Sessions | `~/config/settings/haikode/sessions.db` | same |
| User memory | `~/config/settings/haikode/memory/` | same |
| Global instructions | `~/config/settings/haikode/AGENTS.md` | same |
| Project memory | `<project>/.haikode/memory/` | same |

A pre-rename `.../hai/config.json` is **copied** (not moved) into the new
location on first run, so an older build keeps working; the migration prints
one notice on stderr.

---

## Providers and authentication

| Profile | Authentication | Endpoint |
|---|---|---|
| `ollama` *(default)* | Ollama Cloud API key | `https://ollama.com/v1` |
| `zen` | none — public free tier | OpenCode Zen |
| `ollama-local` | none | local / LAN / Tailscale Ollama `/v1` |
| `chatgpt` | ChatGPT subscription, device OAuth | ChatGPT Codex backend |
| `supergrok` | SuperGrok subscription, device OAuth | xAI API |
| `xai` | xAI API key | xAI API billing |
| `anthropic` | Anthropic API key | Anthropic API billing |
| `openai` | OpenAI API key | OpenAI API billing |

`zen` needs no personal key at all, which makes it the cheapest way to smoke
test a fresh install: `haikode -p zen "hello"`.

API-key profiles and subscription profiles are deliberately separate: an OpenAI
API key is not a ChatGPT subscription, and an xAI API key is not a SuperGrok
subscription. Access remains subject to each provider's terms. The ChatGPT and
SuperGrok device flows follow the same public OAuth contracts opencode's
built-in plugins use, reimplemented in stdlib Python. Tokens never leave the
machine.

### Keys

Lookup order for an API key:

1. **Haiku keystore** — the native `hai-keystore` helper (`BKeyStore` /
   `BPasswordKey`, the same mechanism WebPositive uses)
2. **Config file** — `config.json`, mode 0600
3. **Environment variable** — `OLLAMA_API_KEY`, `ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`, `XAI_API_KEY`

```sh
haikode login <provider>     # hidden input, validated live, stored in the best place
```

The helper binary is still called **`hai-keystore`**, on purpose. Haiku grants
keyring access per app signature *and* binary path, so renaming it would
re-trigger the "Application keyring access" dialog on the machine's physical
screen and orphan every key already stored under the approved binary. Only the
identifier namespace was renamed (`hai:<provider>` → `haikode:<provider>`), and
that migration happens transparently on first read without deleting the old
entry. See `tools/hai-keystore/README.md`.

### Custom endpoints

Anything that speaks the OpenAI or Anthropic HTTP dialect — including a gateway
you run yourself — is a profile, no JSON editing required:

```sh
haikode provider list
haikode provider add studio-ollama \
    --base-url http://ollama.tailnet:11434/v1 --model qwen3 --no-key
haikode provider default studio-ollama
haikode provider remove studio-ollama
```

`--dialect anthropic` switches the wire format. `provider add` is also
available from the TUI as **Add provider** (`ctrl+p` → *Add provider*).

`--no-key` is only needed to force the issue. Left out, the address decides:
loopback, a private LAN range, Tailscale's `100.64/10` and `*.local` are
treated as key-free, because Ollama, LM Studio and llama.cpp all serve without
authentication and a profile that demands a key for them reports itself
unusable and tells you to run a `/login` that does not exist. In the **Add
provider** dialog the *Needs key* row is a three-way toggle whose default,
*auto*, is the same rule.

---

## The command line

```
haikode [options] [prompt…]
haikode run [options] PROMPT…      the same thing, spelled out

  -p, --provider NAME      provider profile for this run
  -m, --model  P/M         PROVIDER/MODEL, or a bare model id for this provider
  -a, --agent  NAME        start in this agent (build, plan, or a custom one)
  -C, --directory DIR      working directory
  -c, --continue           resume the most recent session for this directory
  -s, --session ID         resume a session by id (a unique prefix will do)
      --fork               continue in a copy, leaving the original untouched
                           (needs --continue or --session)
      --title TEXT         name the session instead of deriving one from the prompt
      --json               one JSON object per line instead of text (see below)
      --print-logs         print configuration warnings to stderr
      --no-tui             use the plain REPL instead of the curses TUI
      --yes                auto-approve every tool permission (scripted runs)
  -h, --help

Sub-commands:
  haikode doctor [DIR]
  haikode login <provider>
  haikode provider [list | add | remove | default]
  haikode session  [list | show | export | import | delete | rename | fork]
  haikode models   [PROVIDER] [--json] [--refresh]
  haikode agent    [NAME] [--json]
  haikode export   [ID] [-f markdown|text|json] [-o FILE]
  haikode import   FILE
```

With a positional prompt haikode runs once, prints, and exits — that is the
scripting mode. Without one it starts the TUI, or the REPL when stdin/stdout is
not a terminal, when `--no-tui` is given, or when curses is unavailable.

A bare first word is only a sub-command when it matches one exactly, so
`haikode "export the parser"` is still a prompt. Use `haikode run …` when the
two would otherwise collide.

### Exit codes

A script has to be able to tell "the model answered" from "the provider
refused", so each distinct failure has its own code.

| Code | Meaning |
|---|---|
| `0` | the turn finished and the model answered |
| `1` | the provider or the agent failed (auth, rate limit, a dead endpoint) |
| `2` | bad arguments, or an id/name that does not exist |
| `3` | a tool call was refused by the permission layer |
| `4` | the agent hit its step limit without finishing |
| `130` | interrupted (Ctrl-C), the shell's convention for SIGINT |

A run of several prompts exits with the **worst** code any of them earned, so a
failure in the middle of a pipe is never hidden by a success after it.

```sh
haikode --yes "run the tests and fix what fails" || echo "exit $?"
```

### Stdin

With a prompt, stdin is appended to it when stdin is not a terminal:

```sh
git diff | haikode "review this change"
haikode "explain this file" < haikode/turn.py
```

Without a prompt, haikode reads **one prompt per line** — that is how a caller
sends several prompts (and slash commands) down one pipe. Everything works
without a tty; the only difference is that nothing can prompt, so a permission
that is not pre-granted by config or `--yes` is reported and refused rather
than asked about.

### `--json` — the event stream

`--json` writes [JSON Lines](https://jsonlines.org): exactly one JSON object
per line, flushed as it happens, so a reader can consume it with
`for line in proc.stdout`. It implies `--no-tui`.

Every event carries three fields:

| Field | Type | |
|---|---|---|
| `type` | string | the event kind, from the table below |
| `time` | float | unix seconds, when the event was emitted |
| `session` | string | the durable session id — `""` until a session is opened |

and then, per kind:

| `type` | Extra fields |
|---|---|
| `run` | `prompt`, `provider`, `model`, `agent`, `cwd` — a turn is starting |
| `notice` | `text` — a startup message (resumed, forked, "session not saved") |
| `attach` | `paths[]` — `@`-mentions expanded into the prompt |
| `memory` | `text` — the line was a `#` quick capture; the model never ran |
| `text` | `text` — one streamed chunk of the answer |
| `reasoning` | `text` — one streamed chunk of reasoning |
| `tool` | `name`, `args{}` — the model called a tool |
| `tool_result` | `name`, `title`, `output`, `metadata{}` |
| `tool_denied` | `name`, `reason` — the permission layer refused it |
| `tool_error` | `name`, `error` — the tool raised |
| `permission` | `key`, `title`, `patterns[]`, `decision` — asked, and how it was answered |
| `limit` | `steps` — the step limit was reached |
| `error` | `source` (`provider` or `turn`), `message`, `kind`, `retryable` |
| `usage` | `input`, `output`, `reasoning`, `cache_read`, `cache_write`, `total`, `cost` |
| `command` | `command`, `text` — what a slash command printed |
| `done` | `text`, `interrupted`, `error`, `denied[]`, `limited`, `persisted`, `exit` |

`done` is always the last event of a turn, and `done.exit` is the code that
turn alone earned. A provider failure appears twice on purpose: once as
`error.source == "provider"` (structured, with `kind` and `retryable` from the
provider) and once as `error.source == "turn"`, which is what ended the run.

```sh
haikode --json --yes "add a docstring to turn.py" \
  | while read -r line; do
      python3 -c 'import json,sys; e=json.loads(sys.argv[1]); print(e["type"])' "$line"
    done
```

### Inspecting and scripting sessions

```sh
haikode session list --json          # id, title, message_count, updated, …
haikode session show <id>            # header, tool counts, files touched, tokens
haikode session export <id> -f markdown -o notes.md
haikode session rename <id> New title
haikode session delete <id>
haikode session fork <id>            # branch a conversation without running it

haikode export <id> > session.json   # defaults to the newest session here
haikode import session.json          # rebuilds it as a new session

haikode models ollama                # provider/model, one per line
haikode agent plan                   # description, mode, resolved tool list
```

Session ids are time-prefixed, so a unique **prefix** is accepted anywhere an
id is — but the eight characters the TUI shows are the same for every session
opened this decade, so take the full id from `haikode session list`. An
ambiguous prefix is refused rather than guessed at.

---

## Slash commands

Every command below works in **both** the TUI and the REPL. In the TUI some of
them open a dialog instead of printing (marked ◆); the rest print into the
transcript.

### Session

| Command | Does |
|---|---|
| `/new`, `/clear` | start a fresh conversation |
| `/sessions [query]` ◆ | list or full-text search saved sessions |
| `/resume <id>` ◆ | resume a session by id |
| `/fork [id]` | continue in a copy, leaving the original untouched |
| `/rename <title>` | rename the current session |
| `/archive` | archive the current session |
| `/export [path] [markdown\|text\|json]` | render or write the transcript |
| `/compact [keep]` | fold old messages into a summary |
| `/undo` | revert the file changes made by the last run |
| `/todos` | show the agent's current task list |
| `/farewell [on\|off]` | exit with a model-written haiku; `on` makes every exit do it |

### Model and provider

| Command | Does |
|---|---|
| `/model` ◆ | show the active model, favourites and recents |
| `/model <provider/model>` | switch model |
| `/model fav [id]` | toggle a favourite |
| `/models [provider]` | list what the providers actually offer |
| `/provider` ◆ | switch provider |
| `/provider add\|remove\|default …` | manage profiles |
| `/login [provider]`, `/logout [provider]` | credentials |
| `/keys` | credential status per provider |
| `/effort [level\|next]` | inspect or change reasoning effort for this live session |

### Agents

| Command | Does |
|---|---|
| `/agent` ◆ | list agents and subagents |
| `/agent <name>` | switch agent |
| `/plan` | switch to the read-only `plan` agent |
| `/build` | leave plan mode |

### Memory

| Command | Does |
|---|---|
| `/memory [query]` | list or search saved memories |
| `/remember <text>` | save a memory |
| `/forget <name>` | delete one |
| `#<text>` | shorthand: a line starting with `#` is a memory, not a message. `#` saves it for this project, `##` saves it globally, and an explicit `#user: …` / `#project: …` overrides both. `###` is left alone — you are writing markdown. |

### Inspection

| Command | Does |
|---|---|
| `/status` ◆ | setup, auth, tools, tokens and context usage |
| `/config` | effective settings and which file each came from |
| `/reload` | apply external config-file edits without losing the conversation |
| `/context` | context-window usage breakdown |
| `/usage`, `/cost` | token usage for this session |
| `/tools` | tools available to the agent |
| `/mcp` | MCP servers: connection state, tools, warnings |
| `/permissions` | what tools may do without asking |
| `/reasoning` | show or hide model reasoning blocks |
| `/help` | this list, plus custom commands |
| `/init` | write `haikode.json`, then have the model write `AGENTS.md` |
| `/exit`, `/quit` | leave |

TUI-only additions: `/commands` (or `/palette`) opens the command palette,
`/keybinds` opens the keybinding list, `/expand` toggles folded tool output,
`/redraw` repaints the screen.

`@path` anywhere in a prompt inlines that file, so the model sees it without
spending a tool call. `Tab` completes an active slash command; elsewhere it
cycles agents.

---

## Keybindings

The names, defaults and descriptions are ported from opencode
(`packages/tui/src/config/keybind.ts`), so muscle memory carries over. The
**leader** is `ctrl+x`: press it, then the next key. The footer shows when the
leader is armed.

### What the main screen answers to today

| Keys | Command |
|---|---|
| `ctrl+p` | command palette |
| `ctrl+x ?`, `f1` | keybinding help |
| `ctrl+x s` | status |
| `ctrl+x n` | new session |
| `ctrl+x l` | session list |
| `ctrl+x m` | model list |
| `ctrl+a` | provider list |
| `ctrl+x a` | agent list |
| `tab` / `shift+tab` | next / previous agent |
| `ctrl+x c` | compact the session |
| `f2` / `shift+f2` | next / previous recently used model |
| `ctrl+t` | cycle the active model's reasoning effort |
| `pageup`, `ctrl+alt+b` | scroll transcript up a page |
| `pagedown`, `ctrl+alt+f` | scroll transcript down a page |
| `ctrl+alt+u` / `ctrl+alt+d` | scroll half a page |
| `ctrl+x q` | queued prompts — edit or drop what is waiting |
| `ctrl+c`, `ctrl+d` | quit |

`ctrl+x` followed by an unbound key is swallowed rather than typed into the
prompt. **`ctrl+a` opens the provider list, not line-start** — that is
opencode's ranking, and `Home` still moves to the start of the line.

One deliberate divergence: opencode gives `ctrl+x q` to *both* "quit" and
"queued prompts", and its own priority order hands it to quit — so the chord
documented as reaching your queue ends the session instead. Here it opens the
queue, and quitting is `ctrl+c` or `ctrl+d`.

### Inside a dialog

| Keys | Does |
|---|---|
| type | filter |
| `up`/`down`, `ctrl+p`/`ctrl+n` | move |
| `pageup`/`pagedown` | page |
| `home`/`end` | first / last |
| `return` | select |
| `escape` | close |
| `ctrl+u` / `ctrl+w` | clear / delete word in the filter |
| `ctrl+f` | toggle favourite (model dialog) |
| `ctrl+a` | jump to providers (model dialog) |
| `ctrl+r` / `ctrl+d` | rename / delete (session dialog) |

A dialog never arms the leader, so `ctrl+x` inside the model picker cannot
swallow your next key.

### At the prompt

| Keys | Does |
|---|---|
| `return` | send |
| `alt+return` | newline |
| `escape` | interrupt a run; else clear the input; else re-follow the tail |
| `ctrl+c` | interrupt a run |
| `ctrl+d` | quit on an empty prompt |
| `tab` | complete a slash command; otherwise cycle agent |
| `up`/`down` | history, or move the cursor in a multi-line prompt |
| `home`/`end` | start / end of buffer |
| `ctrl+e` | end of line |
| `ctrl+k` / `ctrl+u` | delete to end / start of line |
| `ctrl+w` | delete word backwards |
| `ctrl+r` | rename the current session |
| `ctrl+o` | expand folded tool output |
| `ctrl+l` | redraw |

### Rebinding

Add a `keybinds` object to `config.json`. Names are opencode's; values are a
comma-separated list of chords, and `"none"` (or `false`) unbinds:

```json
{
  "keybinds": {
    "leader": "ctrl+g",
    "model_list": "<leader>m,f3",
    "model_provider_list": "none"
  }
}
```

Unbinding hands the chord back to the prompt — that is how you get `ctrl+a` to
mean line-start again. Unknown names and unusable chords become warnings shown
in `/status`, never a crash.

> `ctrl+x ?` shows every configured binding. Implemented bindings dispatch in
> the focused screen or widget; a binding for a feature this curses port does
> not have is explicitly marked unavailable instead of silently doing nothing.

---

## Setting up a project

### `haikode.json`

Per-project configuration, layered weakest-first: the global config, then every
ancestor directory from the project root down to the working directory, so the
file nearest you wins. Within one directory `.haikode/haikode.json` beats a
plain `haikode.json`, and a native `haikode.json` beats an `opencode.json` that
happens to be lying around — an existing opencode project works without being
converted first.

```json
{
  "model": "anthropic/claude-sonnet-5",
  "instructions": ["docs/conventions.md", "docs/*.md"],
  "permission": {
    "bash": { "git status": "allow", "git diff*": "allow", "*": "ask" },
    "webfetch": "deny"
  },
  "tools": { "webfetch": false },
  "max_steps": 60,
  "default_agent": "plan",
  "agents": { "review": { "description": "…", "tools": ["read", "grep"] } },
  "context": 200000
}
```

Understood keys: `model`, `provider`, `providers`, `instructions`, `agents`,
`commands`, `permission`, `tools`, `mcp`, `shell`, `max_steps`, `context`,
`default_agent`, `theme`, `username`. `instructions` arrays **concatenate**
across layers; everything else deep-merges. JSON has no comments, so keys
starting with `_` are ignored, and `/init` writes the documentation into
`_comment`.

Runs are unlimited by default. Setting `max_steps` is an optional safety cap,
not a normal completion mechanism: the final step is tool-free and asks the
model for a concise handoff, after which sending `continue` starts a fresh
budget. Configuration is snapshotted into a running session; use `/reload` to
re-read global and project files while retaining the conversation, or restart.
A malformed reload leaves the live snapshot untouched.

Because a project config arrives with a checked-out repository, it is treated
as untrusted input:

* instruction paths must resolve **inside** the project, so a cloned repo
  cannot paste `../../../.ssh/id_rsa` into your system prompt;
* instruction globs are bounded in both files scanned and files kept;
* any project rule that *loosens* permissions relative to the defaults and your
  own global config is reported by `/status` and refusable.

A syntax error never stops the agent: the file is skipped and the reason shows
up in `/status` and `doctor`.

`/init` scaffolds one, then asks the model to write `AGENTS.md`.

> `theme`, `username` and `mcp` are accepted and validated but not yet acted on.

### `AGENTS.md`

Instructions the model reads on every turn. haikode looks for `AGENTS.md`,
`CLAUDE.md` or `HAIKODE.md` in the project (walking up to the project root),
plus a global `AGENTS.md` in the config directory and `~/.claude/CLAUDE.md`.
Files named by `instructions` in `haikode.json` are appended. The whole chain is
budgeted, so a large docs tree cannot eat the context window.

Alongside it the system prompt carries an environment block — working
directory, platform, git branch, project tree — and the prompt variant is
chosen from the model family the way opencode does it (`anthropic`, `gpt`,
`codex`, `gemini`, `kimi`, `beast`, `meta`, `trinity`, `default`), always with a
`# Haiku OS` briefing appended so the model knows it is not on Linux.

### `.haikode/command/` — custom commands

A markdown file per command, in the project or in the global config directory:

```markdown
---
description: Review the working tree
---
Review these changes and list the three riskiest ones.

Branch: !`git rev-parse --abbrev-ref HEAD`

Diff:
!`git diff --stat`

Focus on: $ARGUMENTS
```

Saved as `.haikode/command/review.md`, that becomes `/review something`.
`$ARGUMENTS`, `$1`…`$9` and inline `` !`shell` `` are substituted before the
prompt is sent. A custom command shadows a built-in of the same name.
(`commands/` is accepted as a directory name too.) The `agent:` and `model:`
frontmatter keys are parsed but not yet applied — a custom command runs on
whatever agent and model are active.

### `.haikode/agent/` — custom agents

Same shape, one file per agent:

```markdown
---
description: Reviews code without touching it
mode: primary
tools: [read, grep, glob, list]
permission:
  bash: deny
model: anthropic/claude-sonnet-5
steps: 25
---
You are a code reviewer. Report findings; never edit.
```

`mode` is `primary` (selectable), `subagent` (reachable through the `task`
tool) or `all`. `tools` restricts what the model can see; `permission` tightens
what it may do; `disable: true` removes an agent entirely. Project files win
over global ones, and an `agents` block in `haikode.json` merges on top of both.
(`agents/` is accepted as a directory name too.)

---

## Agents and plan mode

Four built-ins:

| Agent | Mode | What it is |
|---|---|---|
| `build` | primary, default | full tool set |
| `plan` | primary | **read-only** |
| `general` | subagent | search and research, reached via the `task` tool |
| `explore` | subagent | read-only locator: finds files and symbols, never runs or edits |

Switch with `/agent <name>`, `ctrl+x a`, or `-a` on the command line. Switching
swaps the prompt, the tool list, the permissions, the step budget and the
agent's own model — and never touches the conversation. The `task` tool takes
a `subagent_type`, so the model picks the right subagent for the job.

A custom agent (`.haikode/agent/<name>.md`, or an `agents` block in config)
may pin its own model with `model: <provider>/<id>` — any configured provider,
no vendor is special. A subagent pinned that way runs on its own client for
that provider, built through the same profile machinery (auth, dialect, stall
budget) as the session's. That is how you get genuinely independent review:

```markdown
---
name: second-opinion
description: Adversarial reviewer on a different vendor's model
mode: subagent
model: ollama/qwen3-coder:480b
---
You review the parent agent's conclusion adversarially. Attack it;
do not extend it. Return findings only.
```

The `task` tool also takes a per-call `model` ("provider/id", or a bare id
on the current provider), so the orchestrating agent can pick a model for
one delegation. Precedence is call > definition > inherit — the same order
Claude Code resolves it in.

If the pinned provider is not configured, the task call fails with a clear
error — never a silent fallback to the parent's model, because a "second
opinion" secretly rendered by the same model is worse than no opinion.

Plan mode is read-only in **both** dimensions at once, because hiding a tool
only discourages a model while a permission deny actually stops it: `plan` sees
only `read`, `grep`, `glob`, `list`, `task`, `todowrite`, `question` and
`plan_exit` (`apply_patch` goes too — its permission key is `edit`), and every
write key is denied underneath. Session-scoped "always" grants are filtered on
the way in, so a `bash` grant you made in build mode cannot walk around plan's
deny. Entering and leaving plan mode injects the same synthetic reminder
opencode uses.

A planning turn ends one of two ways: the model asks you something with the
`question` tool (both front ends render the options and feed the answer back),
or it presents the finished plan and calls `plan_exit` — approve, and it is
switched to `build` and starts implementing; decline, and it stays in plan
mode and refines. `plan_exit` is offered only to agents whose tool list names
it, so `build` is never handed a plan to approve.

## Permissions

Every side-effecting tool call is checked against a per-key policy of
`allow` / `ask` / `deny`.

| Default | Keys |
|---|---|
| `allow` | `read`, `list`, `glob`, `grep`, `todowrite`, `task` |
| `ask` | `edit`, `write`, `bash`, `webfetch`, `memory_write`, `question`, `external_directory`, anything unknown |

`external_directory` is the separate question a tool asks before it touches
anything outside the working directory, so approving a *command* never doubles
as approval to run it on the rest of the disk.

Rules live under `permission` in the config, either as a decision or as a
glob → decision map. Rules are matched **in order and the last match wins**
(opencode's `findLast`), so a catch-all belongs first and the exceptions after
it — anything written before a `*` is dead:

```json
{ "permission": { "bash": { "*": "ask", "git status": "allow", "rm *": "deny" } } }
```

Answering **always** grants a pattern for the rest of the session and can be
persisted. When a project config supplied the merged view, persisting writes
only the rules *you added this session*, onto your own config — a checked-in
repository's permission block is never copied into your global config behind
your back. `--yes` auto-approves everything that is not explicitly denied — a
`deny` rule still stops the call — and is for scripted runs only.

## Tools

| Tool | Purpose |
|---|---|
| `read` | read a file, with line numbers and offset/limit |
| `write` | create or overwrite a file |
| `edit` | exact string replacement |
| `apply_patch` | multi-file patch in one call |
| `glob` | find files by pattern |
| `grep` | search file contents |
| `list` | list a directory tree |
| `bash` | run a shell command |
| `webfetch` | fetch a URL as text or markdown |
| `todowrite` | maintain the task list for a multi-step job |
| `task` | delegate to a subagent |
| `memory_write` / `memory_read` | save and recall durable notes |
| `question` | ask the user a multiple-choice question |
| `skill` | load a named instruction set on demand |
| `plan_exit` | submit a finished plan for approval (plan mode only) |

Calls use the provider's own tool-call protocol (OpenAI function calling,
Anthropic tool use), not prompt-parsed pseudo-syntax, so the model can call
several tools in one turn and results return as real `tool` messages.

Both front ends implement the `question` answer contract as a modal; in a
non-interactive run it degrades to "Unanswered" rather than hanging.

## Skills

A skill is a named instruction set the model loads when the task calls for
it — the same `SKILL.md` shape opencode and Claude Code use: a directory
per skill, frontmatter for identity, markdown for the instructions.

    <global config dir>/skill/<name>/SKILL.md
    <project>/.haikode/skill/<name>/SKILL.md      (wins on a name clash)

```markdown
---
name: release-checklist
description: How releases are cut and verified in this repo
when to use: Whenever asked to tag, package or publish a release
---

The instructions themselves, as long as they need to be. Files shipped
beside SKILL.md (scripts/, reference/) are listed to the model together
with the skill's base directory, so "run scripts/check.sh" resolves.
```

Only the name and a one-line summary reach the system prompt; the body
arrives when the model calls the `skill` tool. That split is the point:
twenty skills cost twenty lines of context, not twenty documents. `/skills`
lists everything found — and everything skipped, with the reason — and
loading is gated by the `skill` permission key, so an agent overlay can
deny a skill outright. A worked example ships in
[docs/examples/skills/](docs/examples/skills/).

## Memory

opencode leans entirely on `AGENTS.md`, a file *you* maintain. haikode adds a
second layer the *agent* maintains: short markdown notes it writes when it
learns something durable and reads back on every later run.

```
~/config/settings/haikode/memory/     facts about you, in every project
<project>/.haikode/memory/            facts about this codebase
```

One markdown file per memory plus a generated `MEMORY.md` index — deliberately
not a database, because a memory you cannot open, edit and delete in a text
editor is a memory you cannot trust.

```
/remember the test suite runs with python3 -m unittest discover -s tests
# this project has no git repository, so there is no undo outside /undo
## I prefer Norwegian in commit messages
/memory sqlite
/forget test-suite-command
```

The model reaches the same store through `memory_write` / `memory_read`. It is
instructed to save durable user preferences and corrections, project decisions,
and non-obvious reusable gotchas—not secrets, guesses, current-task progress,
or a note after every turn. `/memory` always prints both editable directories,
even before the first note exists. Memories ride in the system prompt under a
fixed budget: descriptions first, oldest dropped when the budget runs out.

## The haiku

The project is named for an operating system that is named for a poem, and
it behaves accordingly.

**At startup**, one poem from the built-in collection appears under the
wordmark: thirty-one in all — Matsuo Bashō, Yosa Buson and Kobayashi Issa in
this project's own renderings (their originals are centuries free; published
English translations are not, which is why these are ours), alongside
originals signed *botfred, 2026*. Every poem is attributed on screen. On a
small terminal the poem is the first thing to yield — it never costs you a
line of facts.

**At exit** there are two doors. `/exit`, `ctrl+c` and EOF leave instantly,
with one more poem from the collection on the way out. `/farewell` is the
ceremonial exit: the model writes a haiku about the very session it is
leaving — composed at that moment, from the whole conversation, signed with
its own name:

```
  a quiet last diff
  the parser breathes easily
  rest now, terminal
        — gpt-5.6-terra

resume this session:  haikode -s ses_0019fc3c8ebfab7d3e6
```

Typing `/farewell` is the consent — and `/farewell on` extends it: every
plain exit then composes too (persisted as `farewell_on_exit`, default off;
`/farewell off` reverts, and `/farewell` alone still works either way). It
runs from the prompt, so no turn is in flight and the stream has the pipe to
itself; a quit that doubles as an interrupt stays instant regardless. If
composition fails, the collection covers the goodbye. The only background
call is the session's 3-5 word display title for the terminal tab and the
session list, composed once after the first successful turn, in interactive
sessions only.

The resume line always prints the full session id: the ids are
time-prefixed, so every id from the same era shares its first eight
characters, and a shortened form would only ever be ambiguous.

## Sessions and undo

Conversations are stored in SQLite at
`~/config/settings/haikode/sessions.db`: full history, tool calls, token
totals, titles (auto-generated from the first message), archive flags and
full-text search across message bodies.

Before each run the session takes a **checkpoint** and records the original text
of every file a tool touches — `NULL` meaning "this file did not exist".
`/undo` restores those originals, deletes files that were newly created, and
drops the messages after the checkpoint. Haiku installs cannot assume git
exists, so this replaces opencode's git snapshots.

```sh
haikode -c                          # resume the most recent session here
haikode -s ses_1a2b3c4d             # resume by id (or a unique prefix)
haikode -c --fork "try it this way" # branch, leaving the original alone
haikode session list                # the same list, without the TUI
```

```
/sessions            list, newest first
/sessions sqlite     full-text search, including message bodies
/undo                revert the last run's file changes
/compact 10          fold everything but the last 10 messages into a summary
/export notes.md     write the transcript
```

> **Known gap.** Session persistence is wired into the REPL and the desktop app
> but **not** into the curses TUI. A TUI conversation can browse, search, rename,
> delete and resume sessions, but does not write new turns back to the database,
> so `/undo` has nothing to revert. Use `--no-tui` when you need the safety net.
> Tracked in [docs/PARITY.md](docs/PARITY.md).

## Context and cost

The prompt box carries a live context meter — a bar, degrading to a bare
percentage and then to nothing as the terminal narrows — coloured by pressure.
After a response it uses the provider's latest input/cache/output/reasoning
counts rather than cumulative session totals; before the first response it uses
a local estimate. `/context` names the source of the model's window and breaks
usage down (system prompt, instructions, memory, tool schemas, history);
`/usage` and `/cost` report what the session has spent.

When the history no longer fits its budget, the old turns are folded into a
model-written summary automatically at request time (falling back to dropping
with a notice if the summariser fails); `/compact` does the same thing on
demand.

The window is per model, not per provider. A profile's `context` is the
fallback; where the endpoint states a window of its own in `/models` — xAI and
Kimi both do — that number is used instead, and for a local Ollama the window
is read from `/api/show`, preferring the server's `num_ctx` over what the
weights allow, since `num_ctx` is what will actually be served. `/context`
names which of these the current number came from. To pin one yourself:

```json
{"providers": {"kimi": {"model_context": {"k3-256k": 262144}}}}
```

Compaction budgets against the **input** share of the window, not the whole
of it: `context` is input plus output, and a request is refused on the input
half. The ChatGPT backend enforces 372k of gpt-5.6's 500k and 272k of
gpt-5.5's 400k; other providers default to the window. An `"input"` figure
in the profile pins it by hand. The token estimate behind the trigger is
calibrated against measured API counts and then corrected live, each turn,
by the ratio between what we estimated and what the provider reported —
so the trigger follows the model's arithmetic, not ours.

## MCP and LSP

Both are live. MCP is the one extensibility path on an OS with no pip: put a
block in your config and the server's tools join the agent's set, each behind
the `mcp` permission key (approving one grants the tool, "always" covers the
whole server):

```json
{"mcp": {"docs": {"command": ["python3", "/path/to/server.py"]}}}
```

Remote servers use `{"url": "https://…"}`. Startup is budgeted and a broken
server cannot stall the agent: while a server is connecting — or if it never
manages to — the model is offered a `mcp_<name>_status` stand-in that reports
the connection state, and the real tools replace it at the next turn once the
server is up. A remote tool can never shadow a built-in. `/mcp` lists every
server, its state and its tools.

LSP needs no configuration at all: when a language server for the file you are
editing exists on `$PATH`, its diagnostics are appended to `edit`/`write`
output, so the agent learns immediately that a change broke the build. No
server process exists until a file of a known language is touched, and every
server dies with haikode. `lsp: false` in the config opts out.

## Desktop app

`desktop/` is a pure BeAPI application (`BWindow`, `BMenuBar`, `BSplitView`,
`BOutlineListView`, `BTextView`, `BStatusBar`, `BFilePanel`, `ui_color`,
`be_plain_font`), signature `application/x-vnd.haikode`, reachable as
`haikode-desktop` (and `hai-desktop`).

It never parses provider JSON. It forks `python3 -m haikode.desktop_worker`,
which runs the **same** agent loop as the CLI — real tools, real permissions,
real session persistence — and streams versioned NDJSON back; and it shells out
to `python3 -m haikode.configtool` for every config and key operation. No
localhost server is involved anywhere.

Settings in the app cover provider, model, API key, Ollama LAN/Tailscale URL and
the ChatGPT/SuperGrok subscription logins.

The `HAI_*` environment variables keep their pre-rename names on purpose: they
are the wire contract between the installed C++ binary and the Python worker,
and renaming them would break a mixed-age install in both directions.

---

## Development

```sh
python3 -m compileall -q haikode
sh -n scripts/install-on-haiku.sh scripts/haikode-launcher scripts/build-hpkg.sh
HAI_DISABLE_KEYSTORE=1 python3 -m unittest discover -s tests -t . -p "test_*.py"
```

2270 tests, stdlib `unittest`, no network. `HAI_DISABLE_KEYSTORE=1` skips the
native helper so the suite never blocks on Haiku's keyring approval dialog.

To **look** at the TUI rather than guess at it, `tests/render_tui.py` is a pty +
ECMA-48 screen reconstructor with a CLI. It imports nothing from haikode, so it
also runs straight off an ssh session on the Haiku box:

```sh
python3 tests/render_tui.py --rows 34 --cols 100 --text -- python3 -m haikode
python3 tests/render_tui.py --keys $'\x10' --text -- python3 -m haikode   # ctrl+p
```

Deploying to Haiku (`scp -r` hangs on some setups; tar over ssh does not):

```sh
tar czf - haikode tests | ssh user@haiku "cd ~/haikode && tar xzf -"
```

Rules for contributions: Python 3.10, standard library only, no YAML library,
type hints on public functions, docstrings that explain *why*, no emoji.

### Layout

```
haikode/            agent loop, tools, providers, permissions, sessions,
                    memory, project config, agents, prompts, TUI, REPL,
                    OAuth, LSP/MCP clients, desktop worker
desktop/            native BeAPI desktop app (C++)
tools/hai-keystore/ native BKeyStore helper (C++)
scripts/            build-hpkg.sh, install-on-haiku.sh, haikode-launcher
tests/              unittest suite + render_tui.py
docs/PARITY.md      verified opencode-vs-haikode status
```

## How this was built

haikode was written with AI assistance, and that is worth stating plainly
rather than leaving for someone to infer from the commit style.

Most of the code was written by **Claude** (Anthropic) working from the
maintainer's direction, with **GPT-5.6** (OpenAI, via the Codex CLI) used
repeatedly as an adversarial reviewer — asked to attack the design and find
what the author had missed, not to approve it. Several of the defects
recorded in the git history were found that way, including a session-store
corruption bug, a credential-handling failure and an OAuth caching design
that would have lost tokens across concurrent processes.

What that does *not* mean is that the code is unreviewed. Every change is
covered by the test suite (`python3 -m unittest discover -s tests -t .`),
much of it was verified end-to-end on real Haiku hardware, and the failures
that shaped it came from real use rather than from imagination. Where a fix
was made on a guess, the commit message says so.

Judge it the way you would judge any other code: read it, run the tests, and
try to break it. If you find something, the history above is the honest
record of how much was already found that way.

## Licence

MIT — see [LICENSE](LICENSE). haikode is an independent reimplementation of
[opencode](https://github.com/sst/opencode) (MIT) and contains none of its
source, but its behaviour, tool surface, prompt texts and keybinding tables
are derived from that project. The same licence is used so that what was
derived carries the terms it was given.
