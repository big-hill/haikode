"""
Slash commands, custom project commands and @-file references.

Mirrors opencode's command system:
- markdown command files under `.haikode/command/` (project) and the global
  config dir, with YAML-ish frontmatter (description/agent/model) and the body
  as the prompt template
- `$ARGUMENTS` and `$1`..`$9` substitution plus !`shell command` inline
  execution, exactly like opencode's session/prompt.ts
- `@path` references expanded into the message so the model sees the file
  without spending a tool call on it

Frontmatter is parsed by a deliberately dumb "key: value" scanner: pyyaml does
not exist on Haiku and command frontmatter never holds more than flat scalars.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .context import global_config_dir

MAX_MENTION_CHARS = 20000
MAX_MENTION_ENTRIES = 200
SHELL_TIMEOUT = 10

# opencode scans {command,commands}/**/*.md; accept both spellings.
COMMAND_DIRS = ("command", "commands")

# Mirrors opencode's FILE_REGEX. The lookbehind keeps "user@example.com" and
# "`@decorator`" out, and the trailing-dot exclusion means "@main.py." at the
# end of a sentence resolves to "main.py" and leaves the period in the prose.
_SEGMENT = r"[^\s`,.\"']"
MENTION_RE = re.compile(
    r"(?<![\w`])@(?:\"([^\"\n]+)\"|'([^'\n]+)'|"
    r"(\.?" + _SEGMENT + r"*(?:\." + _SEGMENT + r"+)*))"
)

# Argument splitting: quoted runs stay together, everything else is a bare
# token. Unlike opencode we allow quotes inside a bare token so "don't" does
# not split into two arguments.
_ARG_RE = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_QUOTE_RE = re.compile(r"^[\"']|[\"']$")
_POSITIONAL_RE = re.compile(r"\$(\d+)")
_SHELL_RE = re.compile(r"!`([^`]+)`")


# --- @-file references ---------------------------------------------------

def _resolve_mention(name: str, cwd: str) -> Optional[Path]:
    """Return the path an @token points at, or None when it is not a file."""
    # "." / ".." / "/" match the filesystem but are never what someone typing
    # "@." in prose meant, and dumping the whole tree is disruptive.
    if not name or not name.strip("./~"):
        return None
    if name.startswith("~"):
        expanded = Path(os.path.expanduser(name))
        return expanded if expanded.exists() else None
    try:
        candidate = Path(cwd) / name
        if candidate.exists():
            return candidate
        absolute = Path(name)
        if absolute.is_absolute() and absolute.exists():
            return absolute
    except (OSError, ValueError):
        return None
    return None


def _directory_listing(path: Path) -> str:
    """Directories are summarised, never dumped — a listing is the useful part."""
    try:
        entries = sorted(entry.name + ("/" if entry.is_dir() else "")
                         for entry in path.iterdir())
    except OSError as err:
        return f"[unreadable directory: {err}]"
    if not entries:
        return "[empty directory]"
    shown = entries[:MAX_MENTION_ENTRIES]
    text = "\n".join(shown)
    if len(entries) > len(shown):
        text += f"\n... [{len(entries) - len(shown)} more entries]"
    return text


def _file_body(path: Path) -> str:
    """
    Read a bounded prefix, never the whole file.

    Slurping first would hold a multi-megabyte log in memory to emit 20k
    characters of it; four bytes per character is UTF-8's worst case, so
    reading that much always yields enough characters to fill the budget and
    to notice that more was left behind. UTF-8 is pinned because the locale on
    Haiku may be POSIX, which would decode every non-ASCII byte to U+FFFD.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            head = handle.read(4096)
            if b"\0" in head:
                return f"[binary file, {size} bytes]"
            raw = head + handle.read(MAX_MENTION_CHARS * 4)
    except OSError as err:
        return f"[unreadable file: {err}]"
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_MENTION_CHARS:
        return (text[:MAX_MENTION_CHARS] +
                f"\n... [truncated at {MAX_MENTION_CHARS} chars of {size} bytes]")
    return text


def expand_mentions(text: str, cwd: str = ".") -> Tuple[str, List[str]]:
    """
    Append the contents of every @path in `text` that actually exists.

    Tokens that do not resolve (emails, @handles) are left completely alone so
    ordinary prose survives; the returned text is byte-identical to the input
    when nothing matched. Returns (expanded_text, absolute_paths_expanded).
    """
    if not text:
        return text, []
    blocks: List[str] = []
    paths: List[str] = []
    seen = set()
    for match in MENTION_RE.finditer(text):
        name = match.group(1) or match.group(2) or match.group(3)
        if not name:
            continue
        resolved = _resolve_mention(name, cwd)
        if resolved is None:
            continue
        try:
            key = str(resolved.resolve())
        except OSError:
            key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(key)
        if resolved.is_dir():
            body = _directory_listing(resolved)
        elif resolved.is_file():
            body = _file_body(resolved)
        else:
            # FIFOs, sockets and device nodes exist and are not directories,
            # but opening one blocks until a writer appears — which in a REPL
            # means hanging the whole UI on a stray @token.
            body = "[not a regular file]"
        blocks.append(f"\n\n--- @{name} ---\n{body}")
    return text + "".join(blocks), paths


# --- frontmatter ---------------------------------------------------------

def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """
    Split optional `---` delimited frontmatter from the body.

    Only flat "key: value" lines are understood; anything else (nested maps,
    lists, comments) is skipped rather than raising, because a broken key must
    never make a command file disappear.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text
    data: Dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            data[key] = value
    return data, "\n".join(lines[end + 1:])


# --- custom commands -----------------------------------------------------

def _split_args(args: str) -> List[str]:
    return [_QUOTE_RE.sub("", token) for token in _ARG_RE.findall(args or "")]


def _run_inline_shell(text: str, cwd: str, trusted: bool = False) -> str:
    """Substitute !`cmd` with the command's output (opencode's shell syntax).

    A command file arrives with a checkout exactly like haikode.json does, and
    this substitution launches a process. Every other door into process launch
    is already gated — an `mcp` entry is refused with the words "registering an
    MCP server starts a process" — and this one was not: `git clone` plus one
    `/name` ran arbitrary shell with no prompt, in both front ends, on Haiku as
    gid=0. So an untrusted project's commands are rendered inert instead:
    the user still sees what the file wanted to run, and nothing runs.
    """
    def run(match: "re.Match") -> str:
        command = match.group(1)
        if not trusted:
            return ("[not run: this project is not trusted. "
                    "`%s` — run /trust to allow its commands]" % command.strip())
        try:
            # stdin is closed so a command that reads it (git without a pager,
            # `cat`) cannot steal the REPL's keystrokes or stall for the whole
            # timeout; encoding is pinned for the same reason as _file_body.
            result = subprocess.run(command, shell=True, cwd=cwd,
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    stdin=subprocess.DEVNULL,
                                    timeout=SHELL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return f"[command timed out after {SHELL_TIMEOUT}s: {command}]"
        except (OSError, ValueError) as err:
            return f"[command failed: {err}]"
        output = (result.stdout or "").strip()
        if result.returncode != 0 and (result.stderr or "").strip():
            output = (output + "\n" + result.stderr.strip()).strip()
        return output

    return _SHELL_RE.sub(run, text)


class CustomCommand:
    """A prompt template loaded from a markdown file in a command directory."""

    def __init__(self, name: str, template: str, description: str = "",
                 agent: str = "", model: str = "",
                 path: Optional[Path] = None, cwd: str = ".",
                 trusted: bool = False):
        self.name = name
        self.template = template
        self.description = description
        self.agent = agent
        self.model = model
        self.path = path
        self.cwd = str(cwd)
        # Whether this file's inline shell may run. A file the user wrote in
        # their own config directory is theirs; one that came with a checkout
        # is not, until they say so.
        self.trusted = bool(trusted)

    @classmethod
    def from_markdown(cls, text: str, name: str, path: Optional[Path] = None,
                      cwd: str = ".", trusted: bool = False) -> "CustomCommand":
        data, body = parse_frontmatter(text)
        return cls(name=name, template=body.strip(),
                   description=data.get("description", ""),
                   agent=data.get("agent", ""),
                   model=data.get("model", ""),
                   path=path, cwd=cwd, trusted=trusted)

    def summary(self) -> str:
        """Description if given, else the first line of the template."""
        if self.description:
            return self.description
        for line in self.template.splitlines():
            if line.strip():
                return line.strip()[:60]
        return ""

    def render(self, args: str = "", cwd: Optional[str] = None) -> str:
        """
        Substitute $1..$N, $ARGUMENTS and !`shell` into the template.

        @path references are deliberately left in place — the caller runs
        expand_mentions() on the finished prompt so command files and typed
        messages resolve files the same way.
        """
        args = args or ""
        parts = _split_args(args)
        positions = [int(m.group(1)) for m in _POSITIONAL_RE.finditer(self.template)
                     if int(m.group(1)) >= 1]
        last = max(positions) if positions else 0

        def substitute(match: "re.Match") -> str:
            position = int(match.group(1))
            if position < 1:
                return match.group(0)
            index = position - 1
            if index >= len(parts):
                return ""
            # The highest placeholder soaks up the rest, so "/fix a b c" against
            # a $1-only template keeps every word instead of dropping "b c".
            if position == last:
                return " ".join(parts[index:])
            return parts[index]

        text = _POSITIONAL_RE.sub(substitute, self.template)
        uses_arguments = "$ARGUMENTS" in self.template
        text = text.replace("$ARGUMENTS", args)
        if not positions and not uses_arguments and args.strip():
            # No placeholder anywhere: append the arguments so they are not lost.
            text = text + "\n\n" + args
        return _run_inline_shell(text, cwd or self.cwd, self.trusted).strip()


def _command_name(relative: Path) -> str:
    """command/foo/bar.md -> "foo/bar", matching opencode's entry naming."""
    return relative.with_suffix("").as_posix()


def load_custom_commands(cwd: str = ".", trusted: Optional[bool] = None
                         ) -> Dict[str, CustomCommand]:
    """
    Load *.md command files, global first then project so the project wins.
    """
    commands: Dict[str, CustomCommand] = {}
    if trusted is None:
        try:
            from .projectconfig import is_trusted
            trusted = is_trusted(cwd)
        except Exception:
            trusted = False       # fail closed: a broken trust store is not consent
    global_root = Path(global_config_dir())
    roots = (global_root, Path(cwd) / ".haikode")
    for root in roots:
        # Files under the user's own config directory are theirs; a project's
        # are untrusted input until the user trusts that project.
        root_trusted = bool(trusted) or Path(root) == global_root
        for directory in COMMAND_DIRS:
            base = Path(root) / directory
            if not base.is_dir():
                continue
            try:
                files = sorted(base.rglob("*.md"))
            except OSError:
                continue
            for path in files:
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                name = _command_name(path.relative_to(base))
                if not name:
                    continue
                commands[name] = CustomCommand.from_markdown(
                    text, name, path, str(cwd), trusted=root_trusted)
    return commands


# --- registry ------------------------------------------------------------

class Builtin:
    """A command implemented by the UI layer rather than by a prompt."""

    def __init__(self, name: str, handler: Callable[[str], Optional[str]],
                 help_text: str = "", aliases: Sequence[str] = ()):
        self.name = name
        self.handler = handler
        self.help = help_text
        self.aliases = list(aliases)


class CommandRegistry:
    """
    Slash-command table: built-ins registered by the UI plus the custom prompt
    commands found on disk.
    """

    def __init__(self, cwd: str = ".", trusted: Optional[bool] = None):
        self.cwd = str(cwd)
        # None asks projectconfig per directory; True is yolo saying every
        # project is trusted, which is what lets a repo's own command files
        # run their inline shell without /trust.
        self.trusted = trusted
        self.builtins: Dict[str, Builtin] = {}
        self.aliases: Dict[str, str] = {}
        self.custom: Dict[str, CustomCommand] = {}
        self._loaded = False

    # -- registration --

    def register(self, name: str, handler: Callable[[str], Optional[str]],
                 help_text: str = "", aliases: Sequence[str] = ()) -> None:
        previous = self.builtins.get(name)
        if previous is not None:
            # Re-registering with a shorter alias list must not leave the old
            # aliases behind in completion, still pointing at this command.
            for alias in previous.aliases:
                if self.aliases.get(alias) == name:
                    del self.aliases[alias]
        self.builtins[name] = Builtin(name, handler, help_text, aliases)
        for alias in aliases:
            self.aliases[alias] = name

    def load_custom(self, cwd: Optional[str] = None) -> Dict[str, CustomCommand]:
        if cwd is not None:
            self.cwd = str(cwd)
        self.custom = load_custom_commands(self.cwd, trusted=self.trusted)
        self._loaded = True
        return self.custom

    def _ensure_loaded(self, cwd: Optional[str] = None) -> None:
        if cwd is not None and str(cwd) != self.cwd:
            self.cwd = str(cwd)
            self._loaded = False
        if not self._loaded:
            self.load_custom()

    # -- lookup --

    def get(self, name: str):
        """Custom command, Builtin, or None (custom shadows built-in)."""
        self._ensure_loaded()
        if name in self.custom:
            return self.custom[name]
        return self.builtins.get(self.aliases.get(name, name))

    def dispatch(self, line: str, cwd: Optional[str] = None):
        """
        Run a slash command.

        Returns ("builtin", handler_result) | ("prompt", rendered_prompt) |
        ("unknown", name), or None when the line is not a command at all.
        As in opencode a custom command shadows a built-in of the same name.
        """
        if not line or not line.startswith("/"):
            return None
        parts = line.strip()[1:].split(None, 1)
        if not parts:
            return "unknown", ""
        name = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        self._ensure_loaded(cwd)
        # Built-ins win. opencode lets a custom file shadow one, but there a
        # command cannot silently replace /undo or /status for someone who
        # merely cloned a repository; here a shadowing file would claim the
        # very commands a user reaches for when they distrust what is going on.
        builtin = self.builtins.get(self.aliases.get(name, name))
        if builtin is not None:
            return "builtin", builtin.handler(arg)
        command = self.custom.get(name)
        if command is not None:
            return "prompt", command.render(arg, self.cwd)
        return "unknown", name

    def complete(self, prefix: str = "") -> List[str]:
        """Command names matching `prefix` (with or without its leading slash)."""
        if prefix.startswith("/"):
            prefix = prefix[1:]
        self._ensure_loaded()
        names = set(self.builtins) | set(self.aliases) | set(self.custom)
        return sorted(name for name in names if name.startswith(prefix))

    def help_text(self) -> str:
        self._ensure_loaded()
        lines = ["Commands:"]
        for name in sorted(self.builtins):
            builtin = self.builtins[name]
            label = "/" + name
            if builtin.aliases:
                label += " " + ", ".join("/" + a for a in sorted(builtin.aliases))
            lines.append(f"  {label:<26}{builtin.help}")
        if self.custom:
            lines.append("")
            lines.append("Custom commands:")
            for name in sorted(self.custom):
                lines.append(f"  {'/' + name:<26}{self.custom[name].summary()}")
        return "\n".join(lines)


# --- /init ---------------------------------------------------------------

def generate_agents_md_prompt(cwd: str = ".") -> str:
    """
    The /init prompt: ask the model to write AGENTS.md for this project.

    Adapted from opencode's initialize.txt — the goal is a short, high-signal
    file, not a tour of the repository.
    """
    root = str(Path(cwd).resolve())
    return f"""Create or update `AGENTS.md` for this repository.

The goal is a compact instruction file that helps future haikode sessions
avoid mistakes and ramp up quickly. Every line must answer: "would an agent
likely get this wrong without help?" If not, leave it out.

## How to investigate

Read the highest-value sources first:
- README files, root manifests, lockfiles, workspace config
- build, test, lint and formatter config; Jamfiles and makefiles
- CI workflows and pre-commit configuration
- existing instruction files (AGENTS.md, CLAUDE.md, HAIKODE.md, .cursorrules)

If the architecture is still unclear, read a small number of representative
source files to find the real entrypoints and how the pieces are wired
together. Prefer executable sources of truth over prose: when documentation
disagrees with a build script, trust the build script.

## What to extract

- exact developer commands, especially the non-obvious ones
- how to run a single test or verify one focused change
- required command order when it matters (lint -> typecheck -> test)
- package boundaries, ownership of major directories, real entrypoints
- toolchain quirks: codegen, migrations, generated files, env loading
- repo-specific conventions that differ from language defaults
- testing quirks: fixtures, prerequisites, slow or flaky suites

## Writing rules

Exclude generic software advice, tutorials, exhaustive file trees, obvious
language conventions, and anything you could not verify by reading the repo.
Prefer short sections and bullets. When in doubt, omit.

If `AGENTS.md` already exists in {root}, improve it in place: keep the
guidance that is still true, delete stale claims, and reconcile it with the
current code. Write the file with the write or edit tool when you are done.
"""
