# Native Haiku UI in the "haikode" desktop app (M0+)

The goal (repeated user request): **use the UI that is as native and recognizable from Haiku as possible**.

## What "native" means here

- **Only standard BeAPI**:
  - BApplication + BWindow (B_TITLED_WINDOW + proper flags)
  - BMenuBar + BMenu + BMenuItem (with keyboard shortcuts)
  - BSplitView (horizontal resizable panes — the classic Haiku splitter)
  - BOutlineListView + BStringItem (exactly like Tracker sidebars / project views)
  - BTextView (for transcript / document content area)
  - BLayoutBuilder (modern recommended layout)
  - BScrollView
  - BStatusBar
  - BTextControl + BButton (standard controls)
  - BStringView for labels
  - BAlert for dialogs
  - BFilePanel (future for open)

- **System appearance only**:
  - `ui_color(B_PANEL_BACKGROUND_COLOR)`
  - `ui_color(B_DOCUMENT_BACKGROUND_COLOR)`
  - `ui_color(B_STATUS_BAR_COLOR)`
  - `be_plain_font`, `be_bold_font`
  - No hardcoded colors, no custom drawing that fights the current Haiku theme (light/dark via system settings)
  - No web views, Qt, wx, FLTK, Electron, or anything foreign.

- **Behavior**:
  - Menu shortcuts feel right
  - Double-click / Enter on outline lists invokes (MSG_PROJECT_INVOKED)
  - Drag from Tracker supported via RefsReceived
  - Status bar at bottom
  - Clean spacing and insets that match other Haiku apps
  - Window sizing + auto limits

## Current M0 layout (very Haiku-like)

```
+---------------------------+
| File  Edit  Session  Help |   ← BMenuBar (standard)
+---------------------------+
| Project |  Transcript +   | Tools & Approvals
| tree    |  model strip    |  (BOutlineListView)
| (BOut-  |  (BTextView)    |
| lineLV) |                 |
|         +-----------------+
|         | [input] Send Stop |  ← composer
+---------+-------------------+--------------------+
| Project: ...  Model: ...  Backend: Python  Ready |  ← BStatusBar
+--------------------------------------------------+
```

Left + center + right panes via one BSplitView — user can drag the splitters.

This combination (menus + 3-pane split + outline trees + status) is instantly familiar to any Haiku user.

## How to build & run on Haiku

From ~/haikode/desktop :

Preferred:
    jam -f Jamfile

Fallback:
    make

Then:
    ./haikode

(Requires haiku_devel and a working compiler toolchain.)

Later: the Python backend will be launched alongside or via localhost HTTP.

## Future (M1-Mx) native enhancements (still 100% BeAPI)

- Real BFilePanel for Open Project (implemented)
- Proper multi-line input BTextView with Shift+Enter support
- Optional richer approval cards; the current native outline already supports
  Once/Always/Deny for live local-agent permission requests
- Diff display inside BTextView or custom view
- BColumnListView for richer tool log
- BNotification / notify integration
- Settings window with BTabView
- Templates for new Haiku projects

All will continue using the same native primitives.

This skeleton was iteratively improved following the Codex spec + the explicit instruction to stay "mest mulig native og gjenkjennelig fra haiku".

## Claude (fable high-effort) QA applied (2026-07-13)

All P0 + P1 items addressed:
- Proper controller + cancellable child-process gateway in the domain layer
- Versioned NDJSON stream from `haikode.desktop_worker`; no provider JSON reaches UI code
- Shared provider, model, API-key, Ollama LAN and local subscription OAuth settings with the CLI
- Messages.h with scoped constants
- Correct BTextView layout constructor + Insert at TextLength() + ScrollToOffset
- RefsReceived loop + ArgvReceived + controller quit
- rdef fixed (no B_ARGV_ONLY, correct syntax)
- makefile-engine Makefile (reliable resource + link)
- Jamfile cleaned
- Native Haiku look preserved and enhanced: BSplitView + two BOutlineListView (project + tools), full BMenuBar, BStatusBar, ui_color, be_fonts, BAlert, info strip, etc.

The UI still feels like a first-party Haiku app while the architecture now matches the spec (UI never blocks, gateway seam ready for Python worker).

**Latest native addition (after this Claude QA):**  
"Open Project..." now shows a **real `BFilePanel`** (directory mode). Selecting a folder refreshes the left `BOutlineListView`. This is extremely recognizable to Haiku users (same panel used everywhere in Tracker, Pe, etc.).

## The desktop app runs the real agent (2026-07-27)

`haikode.desktop_worker` used to stream a plain chat completion, so the GUI had
no tools, no permissions and no agent loop. It now goes through
`runtime.build_agent()` — the same call the CLI and the TUI make — and maps
`Agent.run()`'s event stream onto the NDJSON protocol.

Protocol version stays **1**: frames were only added, so an older installed
binary keeps working against a newer worker and vice versa.

| event | fields | rendered as |
| --- | --- | --- |
| `started` | provider, model, directory, session | the app adopts `session` |
| `info` | agent, provider, model, directory, tools, window | the header strip; `tools` is its tooltip |
| `delta` | text | assistant text, document colour |
| `reasoning` | text | dimmed (text/background midpoint, theme-correct) |
| `tool` | name, title | `· name  title` + Tool Log entry |
| `tool_result` | name, title, diff, output, exit | unified diff, `+`/`-` in `ui_color(B_SUCCESS_COLOR)` / `B_FAILURE_COLOR`, `be_fixed_font` |
| `tool_error` | name, error, kind, denied | failure colour, or dimmed when the user declined it |
| `todos` | text, summary | the "Plan" branch of the tool outline, replaced each time |
| `usage` | used, window, percent, context, summary, tokens, cost | the context BStatusBar; amber at 60%, red at 85% |
| `permission` | id, text, permission, title, patterns, command, diff, path, url | approval item + the command/diff in the transcript, Once/Always/Deny |
| `status` | text | BStatusBar |
| `completed` | finish, cost, tokens, steps, session, summary, context | run ends; `summary` becomes the status line |
| `cancelled` | — | run ends |
| `error` | message, kind, retryable, status, provider, model, body | run ends; `kind` picks the advice underneath |

Notes:

- `AppController` maps events to window messages through one `kFrameSpecs`
  table; unknown events are dropped rather than mis-rendered.
- The NDJSON reader tracks string and nesting boundaries. Diffs and shell
  output flow through these frames, and a substring search would have matched a
  `"text":` that was really part of a patched source file. It also reads
  unquoted scalars, so `"percent":37.5` and `"denied":true` arrive as their
  literals and the worker does not have to quote numbers for our benefit.
- Permission answers still travel back over the duplex stdin pipe as
  `permission\t<id>\t<once|always|reject>`; the worker blocks inside the tool
  that asked, so the window thread is never held up.

## The turn lifecycle is the CLI's (2026-07-27)

The worker no longer keeps its own copy of "open the session, checkpoint, run,
persist". It goes through `haikode.turn.TurnController`, the same object the
REPL and the curses TUI use, so the desktop app gets quick-capture (`# ...`),
`@file` expansion, per-turn revert points and file snapshots — and `/undo`,
`--continue` and the session list describe what the GUI actually did.

Two consequences worth knowing:

- A session store that cannot be opened is now a warning (`status` frame,
  "undo unavailable"), not a failed run. The answer still arrives.
- Provider failures arrive as the structured `error` frame described above,
  built from `providers/base.py`'s `ProviderError`. The app no longer looks
  for the `[stream error]` text marker; that marker only ever existed because
  this worker used to.

### `setenv()` between `fork()` and `exec()` did not work

`AppController::_StartRun` configured the worker with `setenv()` in the forked
child. That is not async-signal-safe in a process with a looper thread, and on
Haiku the assignment did not survive the `exec` at all: the worker started
without `PYTHONPATH` and died with `No module named 'haikode'`. The app only
appeared to work when whoever launched it had already exported the variable,
which Tracker and Deskbar do not.

The child's environment is now built in the parent and installed with a single
pointer store (`environ = environment`) before `execlp`. `ControllerSmokeTest`
calls `unsetenv("PYTHONPATH")` on purpose so it keeps testing that.

## Device sign-in in Settings (2026-07-27)

`Sign in` used to fire `configtool oauth-start` and put the result in the
status line, which is not enough to complete a device flow — the user needs the
code in front of them while they type it. Settings now opens a panel with the
address and the code in selectable `BTextControl`s, an "Open in browser"
button, and a "Stop waiting" button, while a worker thread polls
`configtool test <provider>` every four seconds (for up to ten minutes) until
the detached completer has stored the token. Every poll is its own short-lived
thread that snoozes before it runs, so the window looper is never blocked and
Cancel is always live; a generation counter drops replies from an abandoned
flow.
