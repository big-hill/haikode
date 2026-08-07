"""Non-interactive NDJSON worker for the native Haiku desktop app.

One process handles one prompt. Stdout is protocol-only; diagnostics and
provider failures are represented as versioned JSON events. This keeps Python,
TLS and provider-specific wire formats outside the BeAPI window process.

The desktop app runs the SAME agent loop as the CLI: real tools, real
permissions, real session persistence. `runtime.build_agent()` builds the
agent and `turn.TurnController` owns the turn — quick-capture, @-mention
expansion, the revert checkpoint, the transcript rows and the file snapshots.
Both are shared with the REPL and the curses TUI, so the GUI can neither drift
into a tools-less chat box nor into a second, subtly different lifecycle that
writes different rows than /undo and the session list expect.

The HAI_* environment variables below are the wire contract with the installed
C++ desktop binary (desktop/src/domain/AppController.cpp). They keep their
pre-rename names on purpose so an already-installed desktop app keeps working
against a freshly updated Python tree, and vice versa.

Protocol (NDJSON, one frame per line, all frames carry {"v":1,"event":...}):

    started      provider, model, directory, session
    info         agent, provider, model, directory, session, tools, window
    delta        text            assistant text as it streams
    reasoning    text            visible chain-of-thought, when the model emits it
    tool         name, title     a tool is about to run
    tool_result  name, title, output, and diff/exit when the tool reports them
    tool_error   name, error, kind ("failed"/"denied"), denied
    todos        text, summary   the plan todowrite last published
    usage        used, window, percent, context, summary, tokens, cost
    permission   id, text, permission, title, patterns, and diff/command/path/url
    status       text            non-fatal notices (attachments, warnings, ...)
    completed    finish, cost, tokens, steps, session, summary, context
    cancelled
    error        message, kind, retryable, and status/provider/model/body

Frames are only ever ADDED. Version 1 readers ignore what they do not know,
so an older installed desktop binary keeps working against this worker.

Numbers are emitted as JSON numbers and booleans as JSON booleans: the C++
side reads any top-level scalar, so `percent` does not have to be smuggled
through as a string to drive the context meter.
"""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import runtime
from .config import Config
from .permission import PermissionRequest, Permissions
from .turn import TurnController
from .usage import ContextState, format_context, measure_context, summary_line

PROTOCOL_VERSION = 1

# Frames cross a pipe into a BTextView; a 200 KiB tool output would stall the
# window looper for no benefit. The model still sees the untruncated text.
MAX_FRAME_TEXT = 8000

# measure_context() re-reads AGENTS.md and re-prices every tool schema, so a
# fresh measurement per tool result would cost more than the tools do.
CONTEXT_TTL = 2.0

# Argument keys that make a readable one-line label for a tool call, in the
# order tools tend to carry them.
_TOOL_LABEL_KEYS = ("command", "filePath", "path", "pattern", "url",
                    "description", "query")

# Same marker set the TUI draws (tui.TODO_STYLES), in ASCII: the desktop list
# renders one plain row per todo.
_TODO_MARKERS = {"completed": "x", "in_progress": ">", "cancelled": "-",
                 "pending": " "}


def emit(event: str, **fields):
    frame = {"v": PROTOCOL_VERSION, "event": event}
    frame.update(fields)
    print(json.dumps(frame, ensure_ascii=False, separators=(",", ":")), flush=True)


def _handle_termination(_signum, _frame):
    emit("cancelled")
    raise SystemExit(130)


def _clip(text: str, limit: int = MAX_FRAME_TEXT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} characters truncated ...]"


def _read_prompt() -> str:
    if os.environ.get("HAI_FRAMED_STDIN") != "1":
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    header = sys.stdin.buffer.readline().decode("ascii", errors="strict").strip()
    try:
        length = int(header)
    except ValueError as exc:
        raise ValueError("Invalid desktop prompt frame") from exc
    if length < 0 or length > 8 * 1024 * 1024:
        raise ValueError("Desktop prompt frame is too large")
    chunks = []
    remaining = length
    while remaining:
        chunk = sys.stdin.buffer.read(remaining)
        if not chunk:
            raise ValueError("Desktop prompt frame ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _await_permission(permission: dict) -> str:
    """Emit a permission frame and block until the app answers on stdin.

    Blocking is correct here: the agent loop is suspended inside the tool that
    asked, and the desktop app answers from its own looper, so nothing else in
    this process has work to do until the decision arrives.
    """
    permission_id = str(permission.get("id", ""))
    name = str(permission.get("permission", permission.get("type", "tool")))
    patterns = [str(item) for item in (permission.get("patterns") or [])]
    detail = ", ".join(patterns[:3])
    fields: Dict[str, Any] = {
        "id": permission_id,
        # `text` is the pre-agent field name; an already-installed desktop
        # binary renders only this one, so it must stay populated.
        "text": name + (f": {detail}" if detail else ""),
        "permission": name,
        "patterns": patterns[:4],
    }
    for key in ("title", "diff", "command", "path", "url"):
        value = permission.get(key)
        if value:
            fields[key] = _clip(str(value))
    emit("permission", **fields)

    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return "reject"
        parts = raw.decode("utf-8", errors="replace").rstrip("\r\n").split("\t")
        if len(parts) == 3 and parts[0] == "permission" and parts[1] == permission_id:
            return parts[2] if parts[2] in ("once", "always", "reject") else "reject"


class DesktopAsker:
    """Permissions asker that round-trips a request through the GUI."""

    def __init__(self):
        self._counter = 0

    def __call__(self, request: PermissionRequest) -> str:
        self._counter += 1
        metadata = request.metadata or {}
        payload = {
            "id": f"per_{self._counter}",
            "permission": request.key,
            "title": request.title,
            "patterns": list(request.patterns),
        }
        for key in ("diff", "command", "path", "url"):
            if metadata.get(key):
                payload[key] = str(metadata[key])
        return _await_permission(payload)


def _tool_title(name: str, args: Dict[str, Any]) -> str:
    """A short human label for a tool call, mirroring the TUI's tool lines."""
    if isinstance(args, dict):
        for key in _TOOL_LABEL_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(" ".join(value.split()), 160)
    return name


def _todo_block(todos: Any) -> Tuple[str, str]:
    """(checklist, "2/5 done") for a todowrite payload, or ("", "").

    Anything that is not a {"content": ..., "status": ...} mapping is skipped
    rather than raising: this renders data a model produced.
    """
    lines: List[str] = []
    done = 0
    for raw in todos or []:
        if not isinstance(raw, dict):
            continue
        content = " ".join(str(raw.get("content", "")).split())
        if not content:
            continue
        status = str(raw.get("status", "pending"))
        if status in ("completed", "cancelled"):
            done += 1
        lines.append("[%s] %s" % (_TODO_MARKERS.get(status, " "), content))
    if not lines:
        return "", ""
    return "\n".join(lines), "%d/%d done" % (done, len(lines))


def _emit_provider_error(payload: Any):
    """The agent's structured provider failure, as an error frame.

    The shape is providers.base.ProviderError.as_dict(): the app switches on
    `kind` (an auth failure points at Settings, a rate limit does not) instead
    of matching the "[stream error]" prefix the text channel used to carry.
    """
    data = payload if isinstance(payload, dict) else {}
    fields: Dict[str, Any] = {
        "message": _clip(str(data.get("message") or "").strip()
                         or "Provider stream failed", 2000),
        "kind": str(data.get("kind") or "unknown"),
        "retryable": bool(data.get("retryable")),
    }
    for key in ("provider", "model"):
        if data.get(key):
            fields[key] = str(data[key])
    if data.get("status") is not None:
        try:
            fields["status"] = int(data["status"])
        except (TypeError, ValueError):
            pass
    if data.get("body"):
        fields["body"] = _clip(str(data["body"]), 500)
    emit("error", **fields)


def _emit_agent_event(kind: str, payload):
    """Map one Agent.on_event callback onto the NDJSON protocol."""
    if kind == "reasoning":
        text = payload if isinstance(payload, str) else str(payload)
        if text.strip():
            emit("reasoning", text=_clip(text))
        return
    if kind == "error":
        _emit_provider_error(payload)
        return
    if kind == "compaction":
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("text", ""))
        emit("status", text=text or "Compacting the conversation"
             + "\u2026")
        return

    data = payload if isinstance(payload, dict) else {}
    name = str(data.get("name", ""))

    if kind == "tool":
        emit("tool", name=name, title=_tool_title(name, data.get("args") or {}))
    elif kind == "tool_result":
        metadata = data.get("metadata") or {}
        fields: Dict[str, Any] = {
            "name": name,
            "title": _clip(str(data.get("title") or name), 160),
        }
        if metadata.get("diff"):
            fields["diff"] = _clip(str(metadata["diff"]))
        if metadata.get("exit") is not None:
            try:
                fields["exit"] = int(metadata["exit"])
            except (TypeError, ValueError):
                pass
        output = str(data.get("output") or "")
        if output:
            fields["output"] = _clip(output, 2000)
        emit("tool_result", **fields)
        # The plan is the point of todowrite; its JSON output is the same list
        # four times longer, so the app gets the checklist as its own frame.
        checklist, summary = _todo_block(metadata.get("todos"))
        if checklist:
            emit("todos", text=_clip(checklist), summary=summary)
    elif kind == "tool_denied":
        emit("tool_error", name=name, error=str(data.get("reason", "denied")),
             kind="denied", denied=True)
    elif kind == "tool_error":
        emit("tool_error", name=name, kind="failed", denied=False,
             error=_clip(str(data.get("error", "")), 2000))
    elif kind == "limit":
        emit("status", text=f"Stopped after {data.get('steps', 0)} steps")


class _ContextMeter:
    """The context numbers behind the app's meter, cached.

    Measuring walks the instruction files and re-prices every tool schema, so
    the meter is refreshed on a timer rather than per frame — exactly what the
    TUI does with the same function.
    """

    def __init__(self, agent, ttl: float = CONTEXT_TTL):
        self._agent = agent
        self._ttl = ttl
        self._state: Optional[ContextState] = None
        self._at = 0.0

    def state(self, refresh: bool = False) -> ContextState:
        now = time.monotonic()
        if refresh or self._state is None or now - self._at > self._ttl:
            try:
                self._state = measure_context(self._agent)
            except Exception:
                self._state = ContextState()
            self._at = now
        return self._state


def _emit_usage(agent, state: ContextState):
    """One frame carrying both the numbers and a ready-made label.

    The label exists so the C++ side never has to format tokens or money; the
    raw numbers exist so the meter can be drawn from `percent`.
    """
    emit("usage",
         used=state.used, window=state.window,
         percent=round(state.percent, 1), messages=state.messages,
         context=format_context(state),
         summary=summary_line(getattr(agent, "usage", None), state),
         tokens=dict(getattr(agent, "tokens", None) or {}),
         cost=round(float(getattr(agent, "cost", 0.0) or 0.0), 6))


def _check_auth(config: Config, provider_name: str, provider_config: dict,
                provider: Any = None):
    """Turn a missing credential into a sentence the GUI user can act on.

    API keys are checked only after build_provider() has resolved one.  Calling
    Config.get_api_key() here first launched the Haiku keystore helper twice
    on every desktop Send -- this preflight and then the real provider build.
    OAuth has no key field on the client, so its store-status check remains
    here and is performed before construction.
    """
    if (provider_config.get("oauth_provider")
            and config.key_source(provider_name) != "oauth"):
        raise RuntimeError(
            f"Not signed in to '{provider_name}'. Open Settings or run "
            f"`haikode login {provider_name}`.")
    if (provider is not None and provider_config.get("requires_key", True)
            and not str(getattr(provider, "api_key", "") or "")):
        raise RuntimeError(
            f"No API key for '{provider_name}'. Open Settings or run "
            f"`haikode login {provider_name}`.")


def _run_smoke(reply: str, provider_name: str, model: str,
               session_name: str) -> int:
    """Deterministic end-to-end path used on Haiku without spending API quota.

    It runs before any session is opened so smoke checks leave no rows behind.
    """
    emit("started", provider=provider_name, model=model,
         directory=str(Path.cwd()), session=session_name)
    expected_permission = os.environ.get("HAI_DESKTOP_TEST_PERMISSION")
    if expected_permission:
        decision = _await_permission({
            "id": "per_desktop_smoke",
            "permission": "bash",
            "patterns": ["echo native smoke"],
            "title": "Run: echo native smoke",
            "command": "echo native smoke",
        })
        if decision != expected_permission:
            emit("error", message=(
                f"Expected permission {expected_permission}, got {decision}"))
            return 1
        reply += ":" + decision
    emit("delta", text=reply)
    emit("completed", finish="stop", cost=0, tokens={})
    return 0


def _attach_session(controller: TurnController, session_name: str):
    """Continue the conversation the app named, or start a new one.

    An EMPTY name means "start a new conversation"; the real id goes back
    out in `started` so the app can adopt it. A non-empty name must load:
    an adversarial review showed the old fall-through silently FORKING the
    conversation — a transient load failure became a fresh blank session
    under a new id, and the model lost the whole history without anyone
    being told. Returns (session, error); exactly one is meaningful.
    """
    store = controller.store()
    if not session_name:
        return controller.open_session(), None
    if store is None:
        return None, ("the session store is unavailable, so session %r "
                      "cannot be continued" % session_name)
    try:
        existing = store.load(session_name)
    except Exception as exc:
        return None, ("could not load session %r: %s"
                      % (session_name, exc))
    if existing is None:
        return None, ("unknown session %r - start a new session"
                      % session_name)
    controller.adopt(existing)
    return existing, None


def _turn(controller: TurnController, config: Config, provider_name: str,
          provider_config: dict, model: str, model_override: str,
          session_name: str, cwd: str, prompt: str) -> int:
    session, attach_error = _attach_session(controller, session_name)
    if attach_error:
        # Refuse to run rather than answer with amnesia: calling the
        # provider on a blank history when the user named a session is the
        # worse failure, however available the model is.
        emit("error", message=attach_error, kind="session", retryable=True)
        return 1
    session_id = getattr(session, "id", "") or session_name
    emit("started", provider=provider_name, model=model, directory=cwd,
         session=session_id)
    # A machine with no sqlite3 (or an unwritable home) must still answer; it
    # just may not advertise an undo, so the app is told why.
    notice = controller.persistence_notice()
    if notice:
        emit("status", text=notice)

    try:
        _check_auth(config, provider_name, provider_config)
        permissions = Permissions(config=config, asker=DesktopAsker())
        agent = runtime.build_agent(config, provider_name, cwd,
                                    permissions=permissions)
        _check_auth(config, provider_name, provider_config,
                    getattr(agent, "provider", None))
        if model_override:
            agent.model = model_override
        # Replaying the durable transcript keeps tool calls paired with their
        # results, which providers reject if we drop one half.
        agent.messages = list(getattr(session, "messages", None) or [])
    except Exception as exc:
        emit("error", message=str(exc) or exc.__class__.__name__,
             kind="unknown", retryable=False)
        return 1

    emit("info", agent=str(getattr(agent, "agent_name", "") or ""),
         provider=provider_name, model=str(getattr(agent, "model", "") or ""),
         directory=cwd, session=session_id,
         tools=", ".join(sorted(getattr(agent, "tools", None) or {})),
         window=int(getattr(agent, "context_window", 0) or 0))
    for warning in list(getattr(agent, "warnings", None) or [])[:5]:
        emit("status", text=_clip(str(warning), 400))

    meter = _ContextMeter(agent)
    _emit_usage(agent, meter.state())

    produced = {"text": False, "tools": False, "error": False}

    def on_text(text: str):
        produced["text"] = True
        emit("delta", text=text)

    def on_event(kind: str, payload):
        if kind in ("tool", "tool_result", "tool_error", "tool_denied"):
            produced["tools"] = True
        elif kind == "error":
            produced["error"] = True
        _emit_agent_event(kind, payload)
        if kind in ("tool_result", "tool_error", "tool_denied"):
            # A step just closed, so the token counters moved: refresh the
            # meter while the user is watching rather than only at the end.
            _emit_usage(agent, meter.state())

    def on_attach(paths: List[str]):
        emit("status", text="attached: " + ", ".join(paths))

    result = controller.run_turn(agent, prompt, on_text=on_text,
                                 on_event=on_event, on_attach=on_attach)
    if result.persistence_error:
        emit("status", text=controller.persistence_notice())

    if result.captured:
        emit("status", text=result.captured)
        emit("completed", finish="capture", cost=0, tokens={}, steps=0,
             session=session_id)
        return 0
    if result.interrupted:
        emit("cancelled")
        return 130
    if result.error:
        # The provider's own structured failure already went out as `error`;
        # re-reporting it as the exception's repr would say it twice, in a
        # worse shape.
        if not produced["error"]:
            emit("error", message=result.error, kind="unknown", retryable=False)
        return 1
    if not produced["text"] and not produced["tools"]:
        emit("error", message="Provider returned no text", kind="unknown",
             retryable=False)
        return 1

    state = meter.state(refresh=True)
    _emit_usage(agent, state)
    emit("completed", finish="stop",
         cost=round(float(getattr(agent, "cost", 0.0) or 0.0), 6),
         tokens=dict(getattr(agent, "tokens", None) or {}),
         steps=int(getattr(agent, "steps_used", 0) or 0),
         session=result.session_id or session_id,
         summary=summary_line(getattr(agent, "usage", None), state),
         context=format_context(state))
    return 0


def run(prompt: str, provider_name: str = "", model_override: str = "",
        directory: str = "", session_name: str = "") -> int:
    config = Config()
    provider_name = provider_name or config.data.get("default_provider", "ollama")
    provider_config = config.data.get("providers", {}).get(provider_name)
    if not provider_config:
        emit("error", message=f"Unknown provider: {provider_name}")
        return 2

    model = model_override or provider_config.get("model", "")
    # Empty means "create a new session" — the one unambiguous contract.
    # The old synthetic "desktop-default" fallback made every non-empty id
    # ambiguous, which is what allowed a load failure to fork the
    # conversation silently.
    session_name = session_name or os.environ.get("HAI_SESSION_ID", "")
    project_dir = directory or os.environ.get("HAI_PROJECT_DIR", "")
    if project_dir:
        try:
            os.chdir(project_dir)
        except OSError as exc:
            emit("error", message=f"Cannot open project directory: {exc}")
            return 2

    test_reply = os.environ.get("HAI_DESKTOP_TEST_REPLY")
    if test_reply is not None:
        return _run_smoke(test_reply, provider_name, model, session_name)

    cwd = str(Path.cwd())
    controller = TurnController(cwd=cwd, provider_name=provider_name,
                                model=model)
    try:
        return _turn(controller, config, provider_name, provider_config, model,
                     model_override, session_name, cwd, prompt)
    except KeyboardInterrupt:
        emit("cancelled")
        return 130
    except Exception as exc:
        emit("error", message=str(exc) or exc.__class__.__name__)
        return 1
    finally:
        controller.close()


def main(argv=None):
    signal.signal(signal.SIGTERM, _handle_termination)
    signal.signal(signal.SIGINT, _handle_termination)
    parser = argparse.ArgumentParser(
        prog="haikode.desktop_worker",
        description="haikode desktop NDJSON worker")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--directory", default="")
    parser.add_argument("--session", default="")
    args = parser.parse_args(argv)
    try:
        prompt = _read_prompt()
    except ValueError as exc:
        emit("error", message=str(exc))
        return 2
    if not prompt.strip():
        emit("error", message="Prompt is empty")
        return 2
    return run(prompt, args.provider, args.model, args.directory, args.session)


if __name__ == "__main__":
    sys.exit(main())
