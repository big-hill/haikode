#!/usr/bin/env python3
"""
haikode — AI coding agent running natively on Haiku OS.

Front-ends: a curses TUI by default, a plain REPL as fallback, one-shot runs
for scripting and `--json` for driving haikode from another program.
Sub-commands: run, doctor, login, provider, session, models, agent, export,
import.

Every one of them exits with a code that says what happened (see repl.EXIT_*),
because a script that cannot tell "the model answered" from "the provider
refused" cannot be a script.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from .config import Config
from .repl import (EXIT_DENIED, EXIT_ERROR, EXIT_INTERRUPTED, EXIT_LIMIT,
                   EXIT_OK, EXIT_USAGE, copy_session, find_session)


def doctor(cwd: str = "."):
    from .agents import AgentRegistry
    from .config import _keystore_bin
    from .memory import MemoryStore
    from .prompt import LOAD_WARNINGS, select_variant
    from .runtime import load_project, project_warnings, provider_status
    from .tool import REGISTRY

    print("haikode doctor")

    try:
        import ssl
        ssl.create_default_context()
        print("✓ SSL OK")
    except Exception as e:
        print(f"✗ SSL: {e} → pkgman install ca_root_certificates")

    try:
        import curses  # noqa: F401
        print("✓ curses available (TUI enabled)")
    except ImportError:
        print("! curses missing — falling back to the plain REPL")

    try:
        import sqlite3  # noqa: F401
        print("✓ sqlite3 available (sessions enabled)")
    except ImportError:
        print("! sqlite3 missing — sessions disabled")

    config = Config()
    print(f"✓ Config: {config.path}"
          + ("" if config.path.exists() else " (defaults, not written yet)"))

    keystore = _keystore_bin()
    print(f"{'✓' if keystore else '!'} Keystore helper: "
          f"{keystore or 'not installed (keys go to the config file)'}")

    print(f"✓ Tools: {', '.join(sorted(REGISTRY))}")

    default = config.data.get("default_provider", "ollama")
    for name in config.data.get("providers", {}):
        marker = "*" if name == default else " "
        print(f"{marker} {name:<12} {provider_status(config, name)}")

    # --- the per-project half: what this directory actually runs under ---

    project = load_project(config, cwd)
    if project.sources:
        print(f"✓ Project config: {', '.join(str(p) for p in project.sources)}")
    else:
        print(f"! Project config: none found under {project.root} (using defaults)")
    instructions = project.resolve_instructions()
    if instructions:
        print(f"✓ Instruction files: {', '.join(str(p) for p in instructions)}")

    model = config.get_provider(default).get("model", "")
    print(f"✓ Prompt variant: {select_variant(model)} (for {model or 'no model'})")

    registry = AgentRegistry.load(cwd, project.data)
    print("✓ Agents: " + ", ".join(
        f"{d.name}{'*' if d.name == registry.default().name else ''}"
        for d in registry.primary())
        + " | subagents: " + (", ".join(d.name for d in registry.subagents())
                              or "none"))

    store = MemoryStore(cwd)
    memories = store.all()
    print(f"✓ Memory: {len(memories)} saved ({store.dir_for('project')})")

    for warning in (project_warnings(project) + registry.warnings
                    + list(store.warnings) + list(LOAD_WARNINGS)):
        print(f"! {warning}")

    print('\nRun: haikode   |   haikode "your task"   |   haikode login <provider>')


def login(argv):
    from .auth import interactive_login
    provider = argv[0] if argv else ""
    sys.exit(0 if interactive_login(Config(), provider) else 1)


def provider_command(argv, config=None):
    parser = argparse.ArgumentParser(prog="haikode provider")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="list configured providers")

    add = subparsers.add_parser("add", help="add or update a provider")
    add.add_argument("name")
    add.add_argument("--dialect", choices=("openai", "anthropic", "gemini"),
                     default="openai")
    add.add_argument("--base-url", required=True)
    add.add_argument("--model", default="")
    add.add_argument("--no-key", action="store_true",
                     help="endpoint does not require an API key")

    remove = subparsers.add_parser("remove", help="remove a provider")
    remove.add_argument("name")
    default = subparsers.add_parser("default", help="set the default provider")
    default.add_argument("name")

    args = parser.parse_args(argv)
    config = config or Config()
    from .runtime import provider_status

    if args.command in (None, "list"):
        selected = config.data.get("default_provider", "")
        for name, provider in config.data.get("providers", {}).items():
            marker = "*" if name == selected else " "
            print(f"{marker} {name:<16} {provider.get('dialect', ''):<10} "
                  f"{provider_status(config, name):<22} {provider.get('base_url', '')}")
        return 0

    try:
        if args.command == "add":
            from . import models as models_mod
            # --no-key forces keyless; without it the endpoint decides, so a
            # local Ollama is not created demanding a login it does not have.
            ok, message = models_mod.add_provider(
                config, args.name, args.base_url, args.model, args.dialect,
                requires_key=False if args.no_key else None, update=True)
            if not ok:
                print(f"[error] {message}")
                return 1
            profile = config.data["providers"][args.name]
            print(f"Provider '{args.name}' saved."
                  + (f" Run `haikode login {args.name}` to set its API key."
                     if profile.get("requires_key") else " No key required."))
        elif args.command == "remove":
            config.remove_provider(args.name)
            print(f"Provider '{args.name}' removed.")
        elif args.command == "default":
            config.set_default_provider(args.name)
            print(f"Default provider → {args.name}")
    except (KeyError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


# --- sessions on the command line ----------------------------------------


def _open_store():
    """(store, error): the session database, or why there isn't one."""
    try:
        from .session import SessionStore
        return SessionStore(), ""
    except Exception as exc:        # sqlite3 missing, unwritable home, ...
        return None, "sessions unavailable: %s" % exc


def _stamp(value) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return ""


def _session_row(row: dict) -> str:
    """One listing line, with the id spelled out in full.

    Not the eight-character form the TUI shows: session ids are time-prefixed
    (`ses_%013x…`), so every id opened this decade shares its first eight
    characters and a user pasting one back would only ever be told it is
    ambiguous.
    """
    flag = " [archived]" if row.get("archived") else ""
    return "%-24s %3d msgs  %-16s %-40s%s" % (
        row["id"], row.get("message_count", 0), _stamp(row.get("updated")),
        (row.get("title") or "(untitled)")[:40], flag)


def _write_out(text: str, target: str) -> int:
    """Print, or write to `target`. Returns an exit code."""
    if not target:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return EXIT_OK
    try:
        path = Path(target).expanduser()
        path.write_text(text)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    print("Wrote %d bytes to %s" % (len(text), path))
    return EXIT_OK


def import_session(store, data: dict, cwd: str = "", title: str = ""):
    """Rebuild a session from what `session export --format json` produced.

    Validation is strict, and it happens *before* anything is written: a
    half-understood file would otherwise import as a conversation with holes
    in it, and the first thing that happens to an imported session is that it
    gets replayed to a provider.
    """
    from .schema import Msg, ToolCall
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError("not a haikode session export: no 'messages' list")

    messages = []
    for index, entry in enumerate(data["messages"]):
        if not isinstance(entry, dict) or not entry.get("role"):
            raise ValueError("message %d has no role" % index)
        calls = []
        for raw in entry.get("tool_calls") or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                raise ValueError("message %d has a malformed tool call" % index)
            calls.append(ToolCall(id=str(raw.get("id") or ""),
                                  name=str(raw["name"]),
                                  arguments=dict(raw.get("arguments") or {})))
        display = entry.get("display")
        messages.append(Msg(
            role=str(entry["role"]),
            content=str(entry.get("content") or ""),
            tool_calls=calls,
            tool_call_id=str(entry.get("tool_call_id") or ""),
            display=dict(display) if isinstance(display, dict) else {}))

    session = store.new_session(cwd or str(data.get("cwd") or os.getcwd()),
                                str(data.get("provider") or ""),
                                str(data.get("model") or ""),
                                title or str(data.get("title") or ""))
    for message in messages:
        session.append(message)
    return session


def _export_session(session, fmt: str, target: str) -> int:
    try:
        text = session.export(fmt)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return _write_out(text, target)


def session_command(argv) -> int:
    """`haikode session …` — the sessions the TUI shows, without the TUI."""
    parser = argparse.ArgumentParser(prog="haikode session")
    sub = parser.add_subparsers(dest="command")

    listing = sub.add_parser("list", help="list saved sessions")
    listing.add_argument("-n", "--limit", type=int, default=50)
    listing.add_argument("-C", "--directory", default="",
                         help="only sessions opened in this directory")
    listing.add_argument("--all", action="store_true",
                         help="include archived sessions")
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one session")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="render a transcript")
    export.add_argument("id")
    export.add_argument("-f", "--format", default="markdown",
                        choices=("markdown", "md", "text", "txt", "json"))
    export.add_argument("-o", "--output", default="")

    imports = sub.add_parser("import", help="create a session from an export")
    imports.add_argument("file")
    imports.add_argument("--title", default="")
    imports.add_argument("-C", "--directory", default="")

    delete = sub.add_parser("delete", help="delete a session and its snapshots")
    delete.add_argument("id")

    rename = sub.add_parser("rename", help="retitle a session")
    rename.add_argument("id")
    rename.add_argument("title", nargs="+")

    fork = sub.add_parser("fork", help="copy a session so it can be branched")
    fork.add_argument("id")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE

    store, error = _open_store()
    if store is None:
        print(error, file=sys.stderr)
        return EXIT_ERROR
    try:
        return _run_session_command(store, args)
    finally:
        try:
            store.close()
        except Exception:
            pass


def _run_session_command(store, args) -> int:
    if args.command == "list":
        rows = store.list_sessions(
            limit=max(1, args.limit), include_archived=args.all,
            cwd=os.path.abspath(args.directory) if args.directory else None)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            for row in rows:
                print(_session_row(row))
        return EXIT_OK

    if args.command == "import":
        try:
            data = json.loads(Path(args.file).expanduser().read_text())
            session = import_session(
                store, data,
                cwd=os.path.abspath(args.directory) if args.directory else "",
                title=args.title)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        print("Imported %s (%d messages)" % (session.id, len(session.messages)))
        return EXIT_OK

    session, error = find_session(store, args.id)
    if session is None:
        print(error, file=sys.stderr)
        return EXIT_USAGE

    if args.command == "show":
        if args.json:
            print(json.dumps(session.export_data(), indent=2, default=str))
            return EXIT_OK
        stats = session.stats()
        print("Session   %s" % session.id)
        print("Title     %s" % (session.title or "(untitled)"))
        print("Directory %s" % session.cwd)
        print("Model     %s" % "/".join(p for p in (session.provider,
                                                    session.model) if p))
        print("Created   %s" % _stamp(session.created))
        print("Updated   %s" % _stamp(session.updated))
        print("Messages  %d (%d tool calls)" % (stats["messages"],
                                                stats["tool_calls"]))
        if stats.get("tools"):
            print("Tools     " + ", ".join(
                "%s×%d" % (name, count)
                for name, count in sorted(stats["tools"].items())))
        if stats.get("files"):
            print("Files     " + ", ".join(stats["files"][:10]))
        print("Tokens    %s" % stats["tokens"]["total"])
        return EXIT_OK

    if args.command == "export":
        return _export_session(session, args.format, args.output)

    if args.command == "delete":
        store.delete(session.id)
        print("Deleted %s" % session.id)
        return EXIT_OK

    if args.command == "rename":
        print("Renamed %s to '%s'" % (session.id,
                                      session.rename(" ".join(args.title))))
        return EXIT_OK

    if args.command == "fork":
        forked = copy_session(store, session)
        print("Forked %s -> %s (%d messages)" % (session.id, forked.id,
                                                 len(forked.messages)))
        return EXIT_OK
    return EXIT_USAGE


def export_command(argv) -> int:
    """`haikode export [ID]` — the transcript, defaulting to the latest here."""
    parser = argparse.ArgumentParser(prog="haikode export")
    parser.add_argument("id", nargs="?", default="")
    parser.add_argument("-f", "--format", default="json",
                        choices=("markdown", "md", "text", "txt", "json"),
                        help="json round-trips through `haikode import`")
    parser.add_argument("-o", "--output", default="")
    parser.add_argument("-C", "--directory", default=".")
    args = parser.parse_args(argv)

    store, error = _open_store()
    if store is None:
        print(error, file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.id:
            session, error = find_session(store, args.id)
        else:
            rows = store.list_sessions(limit=1,
                                       cwd=os.path.abspath(args.directory))
            session, error = ((store.load(rows[0]["id"]), "") if rows
                              else (None, "no session for this directory"))
        if session is None:
            print(error, file=sys.stderr)
            return EXIT_USAGE
        return _export_session(session, args.format, args.output)
    finally:
        try:
            store.close()
        except Exception:
            pass


def import_command(argv) -> int:
    """`haikode import FILE` — the inverse of `haikode export --format json`."""
    parser = argparse.ArgumentParser(prog="haikode import")
    parser.add_argument("file")
    parser.add_argument("--title", default="")
    parser.add_argument("-C", "--directory", default="")
    args = parser.parse_args(argv)
    return session_command(
        ["import", args.file]
        + (["--title", args.title] if args.title else [])
        + (["-C", args.directory] if args.directory else []))


# --- inspecting what is available ----------------------------------------


def models_command(argv, config: Optional[Config] = None) -> int:
    """`haikode models [PROVIDER]` — what a --model flag may name."""
    parser = argparse.ArgumentParser(prog="haikode models")
    parser.add_argument("provider", nargs="?", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="ask the providers again instead of using the cache")
    args = parser.parse_args(argv)

    config = config or Config()
    from .models import ModelCatalog
    catalog = ModelCatalog(config)
    if args.provider and args.provider not in catalog.provider_names():
        print("unknown provider '%s'. Configured: %s"
              % (args.provider, ", ".join(catalog.provider_names()) or "none"),
              file=sys.stderr)
        return EXIT_USAGE

    refs = catalog.models(args.provider or None, refresh=args.refresh)
    if args.json:
        print(json.dumps([{"id": ref.id, "provider": ref.provider,
                           "model": ref.model, "free": ref.free,
                           "context": ref.context} for ref in refs],
                         indent=2))
    else:
        for ref in refs:
            print(ref.id + (" (free)" if ref.free else ""))
    for name, why in catalog.errors.items():
        print("! %s: %s" % (name, why), file=sys.stderr)
    # Nothing at all, and every provider errored, is a failure and not an
    # empty catalogue: a script must not read "no models" as "none exist".
    return EXIT_ERROR if not refs and catalog.errors else EXIT_OK


def agent_command(argv, config: Optional[Config] = None) -> int:
    """`haikode agent [NAME]` — what a --agent flag may name, here."""
    parser = argparse.ArgumentParser(prog="haikode agent")
    parser.add_argument("name", nargs="?", default="")
    parser.add_argument("-C", "--directory", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from .agents import AgentRegistry
    from .runtime import load_project
    from .tool import REGISTRY

    config = config or Config()
    cwd = os.path.abspath(args.directory)
    project = load_project(config, cwd)
    registry = AgentRegistry.load(cwd, project.data)
    default = registry.default().name

    def describe(defn):
        return {
            "name": defn.name,
            "description": defn.description,
            "mode": defn.mode,
            "builtin": defn.builtin,
            "default": defn.name == default,
            "model": defn.model,
            "steps": defn.steps,
            "tools": AgentRegistry.resolve_tools(defn, list(REGISTRY)),
            "permission": {key: value for key, value in defn.permission.items()},
        }

    if args.name:
        defn = registry.get(args.name)
        if defn is None:
            print("unknown agent '%s'. Available: %s"
                  % (args.name, ", ".join(registry.names())), file=sys.stderr)
            return EXIT_USAGE
        info = describe(defn)
        if args.json:
            print(json.dumps(info, indent=2, default=str))
            return EXIT_OK
        print("Agent       %s%s" % (info["name"],
                                    " (default)" if info["default"] else ""))
        print("Mode        %s%s" % (info["mode"],
                                    " builtin" if info["builtin"] else ""))
        print("Description %s" % (info["description"] or "-"))
        print("Model       %s" % (info["model"] or "(session default)"))
        print("Steps       %s" % (info["steps"] or "(session default)"))
        print("Tools       %s" % ", ".join(info["tools"]))
        for key, value in info["permission"].items():
            print("Permission  %-12s %s" % (key, value))
        return EXIT_OK

    agents = [describe(defn) for defn in registry.primary()]
    subagents = [describe(defn) for defn in registry.subagents()]
    if args.json:
        print(json.dumps({"default": default, "primary": agents,
                          "subagents": subagents,
                          "warnings": registry.warnings},
                         indent=2, default=str))
        return EXIT_OK
    for info in agents:
        print("%s%-12s %s" % ("* " if info["default"] else "  ", info["name"],
                              info["description"]))
    if subagents:
        print("  subagents: " + ", ".join(info["name"] for info in subagents))
    for warning in registry.warnings:
        print("! %s" % warning, file=sys.stderr)
    return EXIT_OK


def split_model(value: str):
    """`--model provider/model` -> (provider, model); a bare id keeps the provider."""
    provider, separator, model = (value or "").partition("/")
    if separator and model:
        return provider.strip(), model.strip()
    return "", (value or "").strip()


def build_repl(config: Config, args, cwd: str,
               report: Optional[Callable[[str], Any]] = None):
    """The one place the CLI flags become a REPL, shared by every front-end.

    `report` receives the startup notices (resumed, forked). It defaults to
    printing, but `--json` collects them instead and re-emits them as events —
    a bare print into a JSON Lines stream would corrupt it.
    """
    from .repl import JSONREPL, REPL

    provider, model = split_model(args.model)
    factory = JSONREPL if getattr(args, "json", False) else REPL
    # Phase timing, printed with --print-logs. A user with several windows
    # reported occasionally slow starts; every phase measured fast in
    # isolation (imports 0.19s, agent 0.03s, store 0.02s on the reference
    # machine — with other sessions open), so when it happens again, this
    # line names the phase instead of leaving us to guess. The likely
    # culprits are the episodic kind: a queued keystore approval, a WiFi
    # blip mid-lookup.
    started = time.monotonic()
    repl = factory(config, provider=args.provider or provider, cwd=cwd,
                   auto_approve=args.yes, yolo=getattr(args, "yolo", False),
                   agent_name=args.agent, model=model,
                   print_logs=args.print_logs,
                   reasoning_effort=getattr(args, "effort", ""))
    if args.print_logs:
        print("[startup] agent+providers ready in %.2fs"
              % (time.monotonic() - started), file=sys.stderr)

    say = report or print
    resumed = ""
    if args.session:
        resumed = repl.resume_session(args.session)
    elif args.resume:
        resumed = repl.resume_latest()
    if resumed:
        say(resumed)
    # After the resume, never before: --fork forks whatever was resumed, and
    # main() has already refused the combination without one.
    if getattr(args, "fork", False) and repl.session is not None:
        say(repl.fork_session())
    if getattr(args, "title", ""):
        repl.set_title(args.title)
    return repl


REPROVISION_FALLBACK = frozenset(
    {"/provider", "/model", "/login", "/logout", "/reload"})


class CommandBridge:
    """The TUI's door into the command layer.

    A class and not a closure, because the TUI feature-detects the
    CommandRegistry behind this callback: it fills the ctrl+p palette from it
    and uses it to tell a custom command (which renders to a prompt and must
    run as a turn) from a built-in. A bare function exposes neither, so the
    palette listed no slash commands at all and every custom command fell
    through to the blocking send() path on the curses thread.

    It also remembers whether the last command reprovisioned, which is the one
    distinction `agent_factory()` cannot make on its own.
    """

    def __init__(self, repl, reprovision):
        self.repl = repl
        self.reprovision = frozenset(reprovision)
        self.reprovisioned = False

    @property
    def commands(self):
        return self.repl.commands

    def __call__(self, line):
        result = self.repl.handle_command(line)
        name = line.strip().split(" ", 1)[0]
        if result is not None and name in self.reprovision:
            self.reprovisioned = True
        return result


def _start_tui(repl, config: Config, cwd: str) -> bool:
    """Try the curses TUI. Returns False if it is unavailable."""
    try:
        from . import tui as tui_module
        from .tui import run_tui
    except ImportError as e:
        print(f"[tui unavailable: {e}]", file=sys.stderr)
        return False

    # The TUI asks the factory for an agent in two situations it cannot tell
    # apart through a no-argument callable: after a command that reprovisions
    # (take whatever the command layer built, conversation and all) and for
    # /new (give me an empty one). Watching which commands went past is the
    # only place that distinction exists, so it is drawn here.
    #
    # Startup is deliberately NOT one of them: the agent the TUI starts with is
    # passed in below. `haikode --continue` has already resumed a session into
    # it, and asking the factory instead ran new_conversation() and erased the
    # resumption before the first frame.
    reprovision = getattr(tui_module, "REPROVISION_COMMANDS",
                          REPROVISION_FALLBACK)
    on_command = CommandBridge(repl, reprovision)

    def agent_factory():
        if on_command.reprovisioned:
            on_command.reprovisioned = False
            return repl.agent
        repl.new_conversation()
        return repl.agent

    def completer(prefix):
        if repl.commands is not None:
            return repl.commands.complete(prefix)
        stem = prefix.lstrip("/")
        return sorted(n for n, _, _ in repl._builtins() if n.startswith(stem))

    try:
        run_tui(agent_factory=agent_factory, config=config, cwd=cwd,
                on_command=on_command, completer=completer,
                header=f"haikode — {repl.provider_name}",
                agent=repl.agent, turn=repl.turn)
        return True
    except RuntimeError as e:
        print(f"[tui unavailable: {e}]", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[tui error: {e}]", file=sys.stderr)
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haikode", add_help=False)
    parser.add_argument("-p", "--provider", default="")
    parser.add_argument("-m", "--model", default="",
                        help="PROVIDER/MODEL, or a bare model id for this provider")
    parser.add_argument("-a", "--agent", default="",
                        help="agent to start in (build, plan, or a custom one)")
    parser.add_argument("--effort", default="",
                        choices=("none", "low", "medium", "high", "xhigh", "max"),
                        help="reasoning effort for providers that expose it")
    parser.add_argument("-C", "--directory", default=".")
    parser.add_argument("-c", "--continue", dest="resume", action="store_true",
                        help="resume the most recent session for this directory")
    parser.add_argument("-s", "--session", default="",
                        help="resume a session by id (a unique prefix will do)")
    parser.add_argument("--fork", action="store_true",
                        help="continue in a copy, leaving the original session "
                             "untouched (needs --continue or --session)")
    parser.add_argument("--title", default="",
                        help="title for the session instead of one derived "
                             "from the prompt")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON object per line instead of text "
                             "(implies --no-tui); see the schema in repl.py")
    parser.add_argument("--print-logs", action="store_true",
                        help="print configuration warnings to stderr")
    parser.add_argument("--no-tui", action="store_true",
                        help="use the plain REPL instead of the curses TUI")
    parser.add_argument("--yes", action="store_true",
                        help="auto-approve tool permissions (use with care)")
    parser.add_argument("--yolo", action="store_true",
                        help="no prompts, no deny rules, no repo trust check")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("prompt", nargs="*")
    return parser


# Built from the constants rather than typed out, so `--help` cannot come to
# disagree with what the process actually exits with.
EXIT_CODE_HELP = [
    (EXIT_OK, "the turn finished and the model answered"),
    (EXIT_ERROR, "the provider or the agent failed"),
    (EXIT_USAGE, "bad arguments, or an id/name that does not exist"),
    (EXIT_DENIED, "a tool call was refused by the permission layer"),
    (EXIT_LIMIT, "the agent hit its step limit without finishing"),
    (EXIT_INTERRUPTED, "interrupted (Ctrl-C)"),
]

HELP_EPILOGUE = """
Sub-commands:
  haikode run [options] PROMPT…      one turn, then exit (the default form)
  haikode doctor [DIR]               check this machine's setup
  haikode login <provider>           store an API key / sign in
  haikode provider [list|add|remove|default]
  haikode session [list|show|export|import|delete|rename|fork]
  haikode models [PROVIDER]          what the configured providers offer
  haikode agent [NAME]               what agents this directory has
  haikode export [ID]  /  haikode import FILE

Exit codes:
%s
A run of several prompts exits with the worst code any of them earned.

With a prompt, stdin (when it is not a terminal) is appended to it, so
`haikode "review this" < patch.diff` works. Without one, haikode reads one
prompt per line — that, with --json, is the scripting loop.
""" % "\n".join("  %-4d %s" % row for row in EXIT_CODE_HELP)


def read_piped_stdin() -> str:
    """Everything on stdin when it is not a terminal, else "".

    opencode's resolveRunInput: a piped body extends the prompt rather than
    replacing it, so `haikode "explain" < file.py` asks about the file.
    """
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, UnicodeDecodeError, KeyboardInterrupt):
        return ""


def compose_prompt(words: List[str]) -> str:
    """The prompt for a one-shot run: the positional words plus piped stdin.

    Stdin is only read when there *are* positional words. With none, haikode
    stays a line-oriented REPL over the pipe, which is how a caller sends
    several prompts (and slash commands) down one connection.
    """
    message = " ".join(words).strip()
    if not message:
        return ""
    piped = read_piped_stdin().strip()
    return message + "\n" + piped if piped else message


SUBCOMMANDS = {
    "doctor", "login", "provider", "providers", "session", "sessions",
    "models", "agent", "agents", "export", "import", "run",
}


def _dispatch_subcommand(argv: List[str]) -> Optional[int]:
    """Run `argv` as a sub-command, or return None when it is not one.

    A bare first word is only ever a sub-command when it matches exactly, so
    `haikode "export the parser"` is still a prompt. `haikode run …` exists
    for the cases where the two would otherwise collide.
    """
    if not argv:
        return None
    head = argv[0]
    if head not in SUBCOMMANDS or head == "run":
        return None
    rest = argv[1:]
    if head == "doctor":
        doctor(os.path.abspath(rest[0]) if rest else os.getcwd())
        return EXIT_OK
    if head == "login":
        login(rest)            # exits on its own
        return EXIT_OK
    if head in ("provider", "providers"):
        return provider_command(rest)
    if head in ("session", "sessions"):
        return session_command(rest)
    if head == "models":
        return models_command(rest)
    if head in ("agent", "agents"):
        return agent_command(rest)
    if head == "export":
        return export_command(rest)
    if head == "import":
        return import_command(rest)
    return None


def main():
    argv = sys.argv[1:]

    code = _dispatch_subcommand(argv)
    if code is not None:
        sys.exit(code)
    if argv and argv[0] == "run":
        argv = argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.help:
        parser.print_help()
        print(HELP_EPILOGUE)
        return
    if args.fork and not (args.session or args.resume):
        print("--fork needs --continue or --session: there is nothing to fork "
              "from otherwise.", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    config = Config()
    cwd = os.path.abspath(args.directory)
    prompt = compose_prompt(args.prompt)

    notices: List[str] = []
    try:
        repl = build_repl(config, args, cwd, report=notices.append)
    except ValueError as exc:
        # What build_provider raises for a provider or model the user has not
        # configured. That is a naming mistake in the invocation, and it used
        # to reach the shell as a traceback with an accidental exit 1.
        print(str(exc), file=sys.stderr)
        sys.exit(EXIT_USAGE)
    # An explicit --session that resolved to nothing is a broken invocation,
    # not an empty conversation: say so on stderr and fail, rather than
    # silently starting a new conversation the caller never asked for.
    failed = bool(args.session) and repl.session is None
    for notice in notices:
        if args.json:
            repl.emit("notice", text=notice)
        else:
            print(notice, file=sys.stderr if failed else sys.stdout)
    if failed:
        sys.exit(EXIT_ERROR if any(n.startswith("[error]") for n in notices)
                 else EXIT_USAGE)

    if prompt:
        # One-shot runs go through the same turn as the interactive ones, so a
        # scripted `haikode "..."` writes a session and can be undone too.
        repl.send(prompt)
        sys.exit(repl.exit_code())

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not (args.no_tui or args.json):
        if _start_tui(repl, config, cwd):
            return

    repl.run()
    sys.exit(repl.exit_code())


if __name__ == "__main__":
    main()
