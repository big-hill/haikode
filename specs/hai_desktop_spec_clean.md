# hai — Full Native AI Coding Agent Desktop App for Haiku OS

> **Superseded early proposal.** Its localhost server and C++ policy ownership
> were not adopted. Start with
> [`docs/project/CONTEXT.md`](../docs/project/CONTEXT.md),
> [`docs/project/CODEMAP.md`](../docs/project/CODEMAP.md), and the current code.

**Version**: 0.2 (Desktop)
**Status**: Superseded early proposal.

## 1. High-level Architecture

**Recommended: Hybrid architecture (Phase 1-2)**

- **Frontend (UI)**: Pure native BeAPI C++ application (`hai` binary).
  - Owns all windows, views, event loop, rendering, user interaction, approvals, diff display, notifications.
  - Uses BeAPI for everything (BApplication, BWindow, BView, BTextView, BOutlineListView, BSplitView, BLayoutBuilder, BAlert, BFilePanel, etc.).

- **Backend (Agent Core)**: The existing Python "hai" (or a slimmed "hai-server" mode).
  - Runs as a local HTTP server (localhost, e.g. port 7878).
  - Reuses all current logic: providers, tool registry, agent loop, context manager, sessions, Haiku knowledge pack.
  - Exposes a simple JSON API for chat, tool approval, context, etc.

- **Communication**: C++ frontend uses libcurl (or BNetKit) to talk to the Python backend over HTTP/JSON (or later Unix domain sockets / pipes for tighter integration).

**Long-term option (M4+)**: Pure C++ version where the agent loop, providers, and tool logic are ported to C++ (using libcurl + a small JSON library or rapidjson, or even BNetKit + manual parsing). The Python backend can be deprecated or kept for power users/scripts.

**Why hybrid first?**
- Reuses the working Python CLI immediately.
- Allows fast iteration on agent/tool logic in Python.
- The GUI can be developed in parallel in C++.
- Clean separation: the "brain" (Python) and the "face" (BeAPI C++).
- Easy to replace the worker later without rewriting the UI.

The GUI is always the source of truth for mutations and policy.

## 2. UI/UX Design (BeAPI)

Main window (BWindow, titled "hai"):

- **Layout**: BSplitView or BLayoutBuilder.
  - Left sidebar (20-25%): BOutlineListView for project file tree + pinned context files. Support drag & drop or buttons to add context. Context menu for "Add to chat as context".
  - Center (main chat area): 
    - Scrollable history: Custom BView or BTextView with rich text support (or multiple BStringView + BTextView for messages). Support code blocks, diffs, tool call cards.
    - Bottom: Multi-line BTextView or BTextControl for user input + Send button. Support Shift+Enter for new line, Enter to send. @-completion for files (popup list).
  - Right or bottom pane (tabbed or split): 
    - Tool Log: BListView or BColumnListView showing tool calls with status (pending/approved/running/done/error). Click to see details.
    - Diff Viewer: When a write/edit is proposed, show side-by-side or unified diff in a BTextView with syntax coloring (simple for starters).
  - Top toolbar or menu: Model selector, Provider selector, New Session, Load Session, Settings.

Tool approval UI:
- When the backend proposes tool calls, the frontend shows a non-modal or sheet "Pending Actions" list.
- For each:
  - Description + preview (for writes: the diff; for run: the command).
  - Buttons: Approve, Deny (with optional "always for this session"), Edit (for commands/files).
- Once approved, the tool runs (in the backend), results stream back into the chat log and tool log.
- "Apply All Approved" batch button.

Other windows:
- Settings window (BWindow): Tabs for Providers (API keys, models, base URLs), Behavior (auto-approve read-only, max steps), Haiku (notifications, default build command), Appearance.
- Session manager (list + load/delete).
- "New from template" (BeAPI app skeleton, simple window, etc.).

Haiku native touches:
- Use `notify` for long-running task completion.
- Respect Haiku look (no custom dark mode hacks if possible; use system colors).
- Keyboard shortcuts native where possible.
- Support for "hey" scripting if useful.
- File open/save using BFilePanel.

## 3. Core Features (priorities)

High priority (M1-M2):
- Full chat with streaming responses.
- Tool calling with mandatory UI approval for mutating tools (write, run, pkg install, etc.). Read-only can be auto or one-click.
- Persistent conversation history and named sessions.
- Basic project context (file tree + ability to add files to context).

Medium (M3):
- Smart context (repo map style, recent files, symbol search).
- @-file and @-symbol mentions in input.
- Good diff viewer + one-click apply or reject.
- Multi-file edit support with transaction (all or nothing, or rollback).

High Haiku value (M4+):
- Built-in BeAPI templates and snippets.
- `pkg` tool integration (search/install with output in log).
- `build` tool that understands Jam / makefile_engine / CMake and surfaces errors nicely.
- Native notifications for task completion.
- Ability to open generated files in Tracker or edit in Pe/Koder.

Safety:
- All mutating tools go through approval UI.
- Dry-run where possible.
- Clear "what will this do" description before approval.
- Configurable auto-approve for safe reads.

## 4. Technical Stack & Implementation Details

**Frontend (C++ BeAPI)**:
- Standard BeAPI: BApplication, BWindow, BView hierarchy, BLayoutBuilder (or legacy), BTextView for chat (or custom view for better message rendering), BOutlineListView for tree.
- Networking: libcurl (pkgman install curl_devel) for talking to the Python backend. Or BNetKit for more native.
- JSON: Use a small portable library (e.g. nlohmann/json header-only if it builds, or cJSON, or hand-rolled for the small protocol).
- Build: Jamfile or the Haiku makefile_engine.

**Backend communication (hybrid)**:
- The Python "hai" gains a `--server` or `--desktop-backend` mode that listens on localhost.
- Simple REST-like API (or even SSE for streaming):
  - POST /chat { "messages": [...], "tools": [...] }
  - Streaming responses for tokens and tool proposals.
  - POST /approve_tool { "id": "...", "approved": true }
  - GET /context, etc.
- Tool calls from the model are returned to the GUI for approval before execution.
- The backend never executes mutating tools without explicit approval from the GUI.

**Pure C++ path (future)**:
- Port the provider adapters and agent loop to C++.
- Use libcurl + a JSON library.
- Same tool registry but implemented in C++.
- Same prompt / tool schemas.

**Persistence**:
- Sessions in `~/config/settings/hai/sessions/` (JSON or sqlite, same as CLI).
- Config in `~/config/settings/hai/config.json`.

**Tool execution**:
- The backend (Python or C++) executes tools in a controlled way.
- For shell: use the same guarded allowlist + user confirmation in GUI.
- For file writes: the GUI shows the diff; on approval the backend applies it.

## 5. Tool System

Reuse and extend the CLI tools, but surface them in the UI:

Core tools (same as CLI + desktop specific):
- Filesystem: list, read, write/edit (with diff), grep.
- Execution: run_command (guarded).
- Haiku: pkg_info, build_project, perhaps query for BFS, addattr, etc.
- Meta: get_context, add_to_context, etc.

In the UI:
- When the model proposes a tool, create a "ToolCall" card with id, name, args, status (proposed / approved / running / done / error).
- For write/edit: render a nice diff (use the same logic as CLI or a BeAPI diff view).
- User can edit the args before approving (for run commands, for example).
- Results streamed back into the chat as special "tool result" messages.

Protocol upgrade (as Codex noted): Use durable tool_call_id, explicit states, preview-before-mutation.

## 6. Data Model

- Session: id, name, created, last_used, messages: List[Message]
- Message: role (user/assistant/system/tool), content, tool_calls?: List[ToolCall], timestamp
- ToolCall: id, name, args, status, result?, approved_by_user?
- ContextItem: path, pinned, relevance_score, content_snippet

Use JSON or sqlite for storage (same as current CLI session.py).

## 7. Integration with existing Python CLI

**Recommended hybrid**:
- Add `--server` mode to the Python hai (binds to localhost, implements the chat/tool API).
- The C++ app can launch the Python worker if not running (`python3 -m hai --server &`).
- Shared config and sessions directory.
- The Python worker can still be used from the terminal as before.
- Later, the worker can be replaced by a C++ one without changing the GUI.

This keeps the first desktop releases aligned with the working CLI.

## 8. Haiku-specific considerations

- Use `~/config/settings/hai/` for all settings and sessions (B_USER_SETTINGS_DIRECTORY).
- Use `notify` command or BNotification for completion.
- Deep knowledge of BeAPI in the system prompt and in tool descriptions.
- Build tools understand Jam and the makefile_engine.
- Packaging: HaikuPorts recipe that depends on python3 (for the worker) + the C++ binary. Installs the launcher that starts the worker if needed.
- Old hardware: keep UI responsive, good progress indication, ability to interrupt.
- File system: be careful with BFS attributes if we use them for sessions.

## 9. Inspirations from FOSS

- **Continue**: @-context, symbol awareness, clean tool calling in chat, multi-model, "apply" button flow. The sidebar + main chat split is a good mental model.
- **Aider**: The gold standard for diff-based editing and "the model proposes, user approves, clean apply". Repo map. Excellent prompts for careful editing.
- **Open Interpreter**: Desktop tool execution visualization (live output, status). Conversation + actions side by side.
- **OpenHands**: Explicit step/trajectory view, clear tool approval UI, session history and replay.

We will adapt the best parts into native BeAPI widgets (no web views).

## 10. Phased Implementation Roadmap

**M0 – Desktop skeleton (1-2 weeks)**
- Basic BeAPI BApplication + BWindow with chat area + input.
- Connect to Python backend over localhost.
- Basic streaming token display.
- Simple project tree (hardcoded or basic scan).

**M1 – History + basic tools (1-2 weeks)**
- Persistent sessions (reuse/extend CLI session logic).
- Sidebar for sessions.
- First tools surfaced in UI (read, list, simple write with confirmation).
- Multi-turn works end-to-end.

**M2 – Full tool-calling with approvals (2 weeks)**
- Tool proposal cards with preview (especially diffs for writes).
- Approve/Deny/Edit per tool.
- Tool log with live results.
- Full set of tools from CLI + Haiku ones.
- Agent can do real work with user in the loop.

**M3 – Context & project awareness (2 weeks)**
- Repo map + file relevance.
- @-mentions and pinning.
- Token budgeting + compaction.
- Better context injection.

**M4 – Polish + Haiku deep integration (2 weeks)**
- Full Haiku knowledge pack.
- Native notifications.
- Templates for BeAPI apps/windows.
- Better build/pkg integration.
- Settings UI.
- Packaging as .hpkg.

**M5 – Advanced / pure native (later)**
- Optional pure C++ worker.
- More advanced context (symbols, embeddings if feasible).
- GUI BeAPI preview tools if useful.
- Plugin system or extra tools.

## 11. Risks, Open Issues, Non-Goals

Risks:
- Hybrid process management (starting/stopping the Python worker cleanly).
- Streaming + tool calls in the same response (need clear protocol).
- BeAPI UI complexity for chat (rich text, good scrolling, code blocks).
- Old hardware performance with large contexts.

Open issues:
- Best way to do rich chat rendering in BeAPI (BTextView limitations vs custom view).
- How much of the agent logic to keep in Python vs move to C++.
- Token estimation accuracy without tiktoken.

Non-goals for v1:
- Local models (user request).
- Full computer-use / browser / GUI automation (focus on coding + Haiku tools).
- Electron or non-native UI.

## 12. Example User Flow

User: "implement a simple BeAPI window with a button that says Hello and shows a BAlert"

1. Desktop sends the prompt + current context (or user has opened some files).
2. Backend (Python) streams thinking + proposes tools: read relevant files if needed, then eventually proposes an edit to a new or existing file with a diff.
3. UI shows the proposed write/edit with nice diff.
4. User clicks Approve.
5. Backend applies the edit, streams confirmation.
6. UI shows the result in chat + updates the file tree if visible.
7. Backend may suggest "now build with jam and run".
8. User approves the build/run tool if wanted.

All with full history saved.

## 13. API/Interfaces (hybrid)

Simple example (the Python backend can expose this):

POST /v1/chat
{
  "messages": [...],
  "tools": [...],
  "model": "...",
  "stream": true
}

Responses stream token deltas and tool proposals with IDs.

POST /v1/approve
{ "tool_call_id": "...", "approved": true, "edited_args": {...} }

The GUI controls all approvals.

## 14. Suggested System Prompts / Tool Definitions

Use the existing excellent Haiku knowledge pack from the CLI, plus desktop-specific instructions:

" You are hai running in a native Haiku desktop app. The user sees a nice BeAPI chat. Always propose one tool at a time when exploring or editing. For writes, produce a clear unified diff. Prefer small changes. Use Haiku tools (pkgman, jam) when appropriate. The GUI will handle showing diffs and getting approval."

Tool definitions should be the same JSON schemas as the CLI, possibly extended with "requires_approval": true for mutating ones.

---

This spec is ready to start implementation from. It builds directly on the existing Python CLI work and the previous plans.

The codex run inspected the current code and Haiku BeAPI to make the architecture realistic.

You can copy the content above as the starting spec document.

If you want, I can now start generating the first C++ BeAPI skeleton based on this spec (main window, chat view, connection to the Python backend, etc.).

Just say the word.
