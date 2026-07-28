"""
glob / grep / list — content and filename search.

opencode delegates to ripgrep; Haiku has no ripgrep in the base install, so
this is a stdlib implementation with the same interface and the same output
shape (path:line: content). It skips ignored directories and binary files so
it stays usable on a real repository.
"""

import collections
import fnmatch
import functools
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import ripgrep
from .base import Tool, ToolContext, ToolResult, load_prompt
from .paths import assert_external_directory

# Directories that are never a project's own source: version-control metadata,
# vendored dependency trees, tool caches. `build`, `dist` and `generated` used
# to be in here and are not any more — plenty of real code lives in them, and
# a .gitignore is the project's own way of saying otherwise.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".bzr", "CVS", "__pycache__", "node_modules",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".cache",
}
IGNORED_SUFFIXES = {
    ".o", ".a", ".so", ".dylib", ".pyc", ".pyo", ".class", ".hpkg", ".zip",
    ".gz", ".bz2", ".xz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".ico", ".bin", ".exe", ".wasm", ".mp3", ".mp4", ".woff", ".woff2",
}
MAX_MATCHES = 100
# Two very different costs, measured on a real Haiku home of 286k files:
# traversing 20k entries takes 0.3 s, while opening and reading 20k takes 5 s.
# One shared cap therefore throttled glob roughly 15x harder than its own cost
# justified, and a home directory hit it constantly — the tool answered "no
# files found" while admitting it had stopped early. The wall-clock budget
# below is the real guard for both; these are the backstop under it.
MAX_FILES_SCANNED = 20000       # grep: bounded by reading file contents
MAX_FILES_LISTED = 300000       # glob: bounded by stat() alone
MAX_FILE_BYTES = 2_000_000
# `re` has no timeout and does not release the GIL, so nothing can interrupt a
# catastrophic pattern once it starts backtracking. Bounding the text handed to
# it per line bounds the damage a single line can do; the deadline check in
# front of every search stops the file after the first such line.
MAX_LINE_CHARS = 32768
# A match cap is not enough on its own: a tree with a million files and no
# matches at all would walk for minutes with the UI frozen behind it. Every
# search also gets a wall-clock budget and returns what it has when it runs out.
MAX_SEARCH_SECONDS = 8.0
# One level of `{a,b}` can name a handful of extensions; anything past this is
# a pattern designed to blow up the matcher rather than to find files.
MAX_BRACE_VARIANTS = 64


def _is_ignored_dir(name: str) -> bool:
    """
    Only the directories above.

    The old rule also hid every name starting with a dot, which silently made
    `.github`, `.config` and `.claude` unsearchable — a wrong answer dressed
    up as an empty one.
    """
    return name in IGNORED_DIRS


# --- .gitignore --------------------------------------------------------

def _translate(pattern: str) -> str:
    """gitignore glob -> regex source, matched against a posix relative path."""
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(char))
                i += 1
            else:
                body = pattern[i + 1:close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = close + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


# --- glob matching -----------------------------------------------------

def _expand_into(pattern: str, out: List[str]) -> None:
    """Depth-first brace expansion that stops the moment the cap is reached."""
    if len(out) >= MAX_BRACE_VARIANTS:
        return
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        out.append(pattern)
        return
    for option in match.group(1).split(","):
        if len(out) >= MAX_BRACE_VARIANTS:
            return
        _expand_into(pattern[:match.start()] + option + pattern[match.end():],
                     out)


@functools.lru_cache(maxsize=512)
def _expand_braces(pattern: str) -> Tuple[str, ...]:
    """
    `*.{ts,tsx}` -> ('*.ts', '*.tsx'). Nested groups expand too.

    Cached and capped: this runs once per file in the walk, and `{a,b}` twenty
    times over is 2**20 variants — a pattern written to burn the budget rather
    than to find anything.
    """
    out: List[str] = []
    _expand_into(pattern, out)
    return tuple(out)


@functools.lru_cache(maxsize=512)
def _glob_regex(pattern: str):
    """Compiled path matcher for one brace-free glob, or None if malformed."""
    try:
        return re.compile("^" + _translate(pattern) + "$")
    except re.error:
        return None


def _glob_matches(rel_posix: str, name: str, pattern: str) -> bool:
    """
    ripgrep-style glob matching, against the path relative to the search root.

    fnmatch cannot do this: its `*` spans `/`, so `**/*.py` never matched a
    file at the root and `src/*.py` never matched anything at all. `_translate`
    already knows that `**/` means "zero or more directories", so the gitignore
    translator does double duty here. A pattern with no `/` in it still matches
    on the basename at any depth, which is what `*.py` is expected to mean.
    """
    for expanded in _expand_braces(pattern):
        regex = _glob_regex(expanded)
        if regex is not None and regex.match(rel_posix):
            return True
        if "/" not in expanded and fnmatch.fnmatch(name, expanded):
            return True
    return False


class GitIgnore:
    """
    Line-based .gitignore support — no library, and deliberately not a complete
    implementation of git's matching rules. It handles what actually appears in
    real files: comments, blanks, negation, anchoring, directory-only patterns
    and `**`. Anything it cannot express it simply does not ignore, which is the
    safe direction to be wrong in for a search tool.
    """

    def __init__(self, root: Path):
        self.root = root
        self.rules: List[tuple] = []       # (regex, negated, dir_only, base)
        self._loaded: set = set()

    def load(self, directory: Path) -> None:
        """Add the .gitignore in `directory`, if there is one. Idempotent."""
        key = str(directory)
        if key in self._loaded:
            return
        self._loaded.add(key)
        candidate = directory / ".gitignore"
        try:
            if not candidate.is_file():
                return
            text = candidate.read_text(errors="replace")
        except OSError:
            return
        try:
            base = directory.relative_to(self.root).as_posix()
        except ValueError:
            base = ""
        self.add_text(text, "" if base == "." else base)

    def add_text(self, text: str, base: str = "") -> None:
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            if not line:
                continue
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = line.startswith("/") or "/" in line
            line = line.lstrip("/")
            body = _translate(line)
            prefix = (re.escape(base) + "/") if base else ""
            if anchored:
                source = "^" + prefix + body
            else:
                source = "^" + prefix + "(?:.*/)?" + body
            try:
                # `whole` matches the named entity itself; `full` also matches
                # everything beneath it, which is how ignoring a directory
                # ignores its contents.
                whole = re.compile(source + "$")
                full = re.compile(source + r"(?:/.*)?$")
            except re.error:
                continue
            self.rules.append((whole, full, negated, dir_only, base))

    def ignored(self, rel_posix: str, is_dir: bool) -> bool:
        """Last matching rule wins, exactly like git."""
        decision = False
        for whole, full, negated, dir_only, base in self.rules:
            if base and not (rel_posix == base
                             or rel_posix.startswith(base + "/")):
                continue
            if not full.match(rel_posix):
                continue
            if dir_only and not is_dir and whole.match(rel_posix):
                # `build/` names a directory; a *file* called build is not it.
                continue
            decision = not negated
        return decision


def load_gitignore(root: Path) -> Optional[GitIgnore]:
    """
    The ignore set for a search, seeded with the root .gitignore.

    Always returns an object: nested .gitignore files are picked up as the walk
    reaches them, and a project whose root .gitignore is empty may still ignore
    things deeper in the tree.
    """
    ignore = GitIgnore(root)
    ignore.load(root)
    return ignore


class Budget:
    """Wall-clock deadline shared by a whole search."""

    def __init__(self, seconds: float = MAX_SEARCH_SECONDS):
        self.seconds = max(0.0, float(seconds))
        self.deadline = time.monotonic() + self.seconds
        self.expired = False

    def check(self) -> bool:
        if self.expired:
            return True
        if time.monotonic() >= self.deadline:
            self.expired = True
        return self.expired


def _budget_seconds(args: Dict[str, Any]) -> float:
    """Caller may shorten the budget; it can never lengthen it."""
    raw = args.get("timeout")
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        return MAX_SEARCH_SECONDS
    if requested <= 0:
        return MAX_SEARCH_SECONDS
    return min(requested, MAX_SEARCH_SECONDS)


class WalkReport:
    """
    What a walk had to leave out, so the caller can say so out loud.

    Hitting the file cap used to `return` silently: the model got a short list
    with no hint that it was short, which is worse than an error.
    """

    def __init__(self) -> None:
        self.file_cap = False
        self.skipped_links = 0
        self.cap = MAX_FILES_SCANNED


def _incomplete(budget: "Budget", report: WalkReport, narrow: str) -> str:
    """
    The footer that admits a search did not finish.

    Every limit gets a line. A truncated result that looks complete is the
    failure mode worth avoiding: the model will happily conclude "no such
    function exists" from a list that was cut short.
    """
    notes: List[str] = []
    if budget.expired:
        notes.append("[search stopped after %g s — results are incomplete; "
                     "narrow the path or %s]" % (budget.seconds, narrow))
    if report.file_cap:
        notes.append("[search stopped after %d files — results are "
                     "incomplete; narrow the path or %s]"
                     % (report.cap, narrow))
    if report.skipped_links:
        notes.append("[%d symlink(s) pointing outside the search tree were "
                     "skipped]" % report.skipped_links)
    return ("\n\n" + "\n".join(notes)) if notes else ""


def _under(path: Path, base: str) -> bool:
    """True when `path` is `base` or lives under it."""
    root = os.path.normpath(base)
    target = os.path.normpath(str(path))
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def _breadth_first(root: str) -> Iterator[Tuple[str, List[str], List[str]]]:
    """os.walk's contract, but level by level instead of depth first.

    Same tuples and the same in-place pruning of `dirnames`, so callers do not
    change. The order is what matters: a bounded walk that goes depth first
    spends its whole file budget inside whichever subtree it entered first,
    and reports "no files found" for a name sitting one level down. Breadth
    first spends the budget across the tree, so shallow — and usually more
    relevant — matches are found before the cap is reached.

    Symlinked directories are classified as directories but never descended
    into, matching os.walk(followlinks=False).
    """
    queue = collections.deque([str(root)])
    while queue:
        dirpath = queue.popleft()
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            continue
        dirnames: List[str] = []
        filenames: List[str] = []
        for entry in entries:
            try:
                (dirnames if entry.is_dir() else filenames).append(entry.name)
            except OSError:
                filenames.append(entry.name)
        yield dirpath, dirnames, filenames
        for name in dirnames:      # after the caller pruned the list
            child = os.path.join(dirpath, name)
            if not os.path.islink(child):
                queue.append(child)


def _walk(root: Path, extra_ignores: List[str] = None,
          budget: "Budget" = None, gitignore: "GitIgnore" = None,
          nested_gitignore: bool = True,
          contain: Optional[Sequence[str]] = None,
          report: Optional[WalkReport] = None,
          max_files: int = 0) -> Iterator[Tuple[Path, str]]:
    """
    Yield (path, path-relative-to-root-in-posix-form) for every searchable file.

    `contain` is the set of trees the search is allowed to read. A symlink is
    resolved with lstat and skipped when its target leaves them all: following
    it would read an approved-looking name into a file nobody approved, and
    os.stat cannot tell the difference.
    """
    extra = extra_ignores or []
    scanned = 0
    for dirpath, dirnames, filenames in _breadth_first(str(root)):
        if budget is not None and budget.check():
            return
        here = Path(dirpath)
        if gitignore is not None and nested_gitignore:
            gitignore.load(here)

        keep = []
        for name in dirnames:
            if _is_ignored_dir(name):
                continue
            if gitignore is not None:
                rel_dir = (here / name)
                try:
                    posix = rel_dir.relative_to(root).as_posix()
                except ValueError:
                    posix = name
                if gitignore.ignored(posix, True):
                    continue
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            path = here / name
            if path.suffix.lower() in IGNORED_SUFFIXES:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = str(path)
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat)
                   for pat in extra):
                continue
            if gitignore is not None and gitignore.ignored(rel, False):
                continue
            # Only regular files. A FIFO in the tree would block grep's open()
            # forever, and a device file would stream without end.
            try:
                info = os.lstat(str(path))
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = path.resolve()
                    if not stat.S_ISREG(os.stat(str(target)).st_mode):
                        continue
                except OSError:
                    continue
                if contain and not any(_under(target, base) for base in contain):
                    if report is not None:
                        report.skipped_links += 1
                    continue
            elif not stat.S_ISREG(info.st_mode):
                continue
            scanned += 1
            if scanned > (max_files or MAX_FILES_SCANNED):
                if report is not None:
                    report.file_cap = True
                    report.cap = max_files or MAX_FILES_SCANNED
                return
            if budget is not None and scanned % 64 == 0 and budget.check():
                return
            yield path, rel


class GlobTool(Tool):
    name = "glob"
    description = load_prompt("glob.txt")
    permission = "glob"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "The glob pattern to match files against"},
            "path": {"type": "string",
                     "description": "The directory to search in. Omit to use the current working directory."},
        },
        "required": ["pattern"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args["pattern"]
        root = ctx.resolve(args["path"]) if args.get("path") else Path(ctx.cwd)
        if not root.is_dir():
            raise NotADirectoryError(f"glob path must be a directory: {root}")

        # Listing a directory is reading it. The pattern says nothing about
        # *where* the search happens, so the root gets its own question.
        assert_external_directory(ctx, root, kind="directory", action="Glob")
        ctx.ask("glob", [pattern], f"Glob {pattern}")

        budget = Budget(_budget_seconds(args))
        gitignore = load_gitignore(root)
        report = WalkReport()
        contain = (str(root), ctx.cwd)
        matches = []
        fast = ripgrep.list_files(root, sorted(IGNORED_DIRS), budget,
                                  ctx.check_abort)
        if fast is not None:
            for path in fast:
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    rel = path.name
                if not _glob_matches(rel, path.name, pattern):
                    continue
                try:
                    matches.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        else:
            for path, rel in _walk(root, budget=budget, gitignore=gitignore,
                                   contain=contain, report=report,
                                   max_files=MAX_FILES_LISTED):
                ctx.check_abort()
                if _glob_matches(rel, path.name, pattern):
                    try:
                        matches.append((path.stat().st_mtime, path))
                    except OSError:
                        continue

        # newest first, like opencode
        matches.sort(key=lambda item: item[0], reverse=True)
        truncated = len(matches) > MAX_MATCHES
        shown = [str(p) for _, p in matches[:MAX_MATCHES]]

        out = "\n".join(shown) or "No files found"
        if truncated:
            out += f"\n\n[showing {MAX_MATCHES} of {len(matches)} matches]"
        out += _incomplete(budget, report, "the pattern")
        return ToolResult(title=pattern, output=out,
                          metadata={"count": len(matches),
                                    "truncated": truncated or budget.expired
                                    or report.file_cap,
                                    "fileCap": report.file_cap,
                                    "skippedLinks": report.skipped_links,
                                    "timedOut": budget.expired})


class GrepTool(Tool):
    name = "grep"
    description = load_prompt("grep.txt")
    permission = "grep"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "The regex pattern to search for in file contents"},
            "path": {"type": "string",
                     "description": "The directory to search in. Defaults to the current working directory."},
            "include": {"type": "string",
                        "description": 'File pattern to include in the search (e.g. "*.js", "*.{ts,tsx}")'},
        },
        "required": ["pattern"],
    }

    @staticmethod
    def _include_matches(rel_posix: str, name: str, include: str) -> bool:
        """
        `include` matched the *basename* before, so `src/*.py` matched nothing
        at all and `**/*.ts` missed the root. It is a path glob like glob's own.
        """
        return _glob_matches(rel_posix, name, include)

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args["pattern"]
        if not pattern:
            raise ValueError("pattern is required")
        root = ctx.resolve(args["path"]) if args.get("path") else Path(ctx.cwd)
        include = args.get("include") or ""

        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex {pattern!r}: {e}")

        # Reading file contents outside the working directory is exactly what
        # `read` asks about; doing it a thousand files at a time is not less.
        assert_external_directory(ctx, root, kind="directory", action="Grep")
        ctx.ask("grep", [pattern], f"Grep {pattern}")

        results: List[str] = []
        files_with_matches = 0
        truncated = False
        long_lines = 0
        budget = Budget(_budget_seconds(args))
        gitignore = load_gitignore(root)
        report = WalkReport()
        contain = (str(root), ctx.cwd)

        def keep(path: Path) -> bool:
            if not include:
                return True
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.name
            return self._include_matches(rel, path.name, include)

        fast = ripgrep.grep(pattern, root, keep, sorted(IGNORED_DIRS),
                            MAX_MATCHES, MAX_FILE_BYTES, MAX_LINE_CHARS,
                            budget, ctx.check_abort)
        if fast is not None:
            return self._result(pattern, fast["lines"], fast["files"],
                                fast["truncated"], fast["long_lines"],
                                budget, report, backend="ripgrep")

        for path, rel in _walk(root, budget=budget, gitignore=gitignore,
                               contain=contain, report=report):
            ctx.check_abort()
            if include and not self._include_matches(rel, path.name, include):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                with open(path, "r", errors="replace") as f:
                    hit = False
                    for lineno, line in enumerate(f, 1):
                        # The deadline is checked *before* the regex runs, not
                        # after: re has no timeout and holds the GIL, so once
                        # a catastrophic pattern starts backtracking nothing
                        # can stop it. Checking first bounds the damage to one
                        # line; capping the line bounds that line's cost.
                        if budget.check():
                            break
                        if len(line) > MAX_LINE_CHARS:
                            line = line[:MAX_LINE_CHARS]
                            long_lines += 1
                        if regex.search(line):
                            hit = True
                            text = line.rstrip("\n")
                            if len(text) > 300:
                                text = text[:300] + "…"
                            results.append(f"{path}:{lineno}: {text}")
                            if len(results) >= MAX_MATCHES:
                                truncated = True
                                break
                    if hit:
                        files_with_matches += 1
            except (OSError, UnicodeDecodeError):
                continue
            if truncated or budget.check():
                break

        return self._result(pattern, results, files_with_matches, truncated,
                            long_lines, budget, report, backend="stdlib")

    @staticmethod
    def _result(pattern: str, results: List[str], files: int, truncated: bool,
                long_lines: int, budget: "Budget", report: WalkReport,
                backend: str) -> ToolResult:
        """One formatter for both backends, so their output cannot drift."""
        out = "\n".join(results) or "No matches found"
        if truncated:
            out += f"\n\n[stopped at {MAX_MATCHES} matches — narrow the pattern or use include]"
        out += _incomplete(budget, report, "use include")
        if long_lines:
            out += (f"\n\n[{long_lines} line(s) longer than {MAX_LINE_CHARS} "
                    "characters were matched only up to that point]")
        return ToolResult(
            title=pattern,
            output=out,
            metadata={"matches": len(results), "files": files,
                      "truncated": truncated or budget.expired
                      or report.file_cap,
                      "fileCap": report.file_cap,
                      "skippedLinks": report.skipped_links,
                      "longLines": long_lines,
                      "timedOut": budget.expired,
                      "backend": backend})


class ListTool(Tool):
    name = "list"
    description = load_prompt("list.txt")
    permission = "list"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "The absolute path to the directory to list. Defaults to the current working directory."},
            "ignore": {"type": "array", "items": {"type": "string"},
                       "description": "Glob patterns to skip in addition to the built-in ignores"},
        },
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(args["path"]) if args.get("path") else Path(ctx.cwd)
        ignores = args.get("ignore") or []
        if not root.exists():
            raise FileNotFoundError(f"Directory not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {root}")

        assert_external_directory(ctx, root, kind="directory", action="List")
        ctx.ask("list", [ctx.relative(root)], f"List {ctx.relative(root)}")

        entries = []
        for entry in sorted(root.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
            if _is_ignored_dir(entry.name) and entry.is_dir():
                continue
            if any(fnmatch.fnmatch(entry.name, pat) for pat in ignores):
                continue
            entries.append(entry.name + "/" if entry.is_dir() else entry.name)

        out = "\n".join(entries) or "(empty directory)"
        return ToolResult(title=ctx.relative(root), output=out,
                          metadata={"count": len(entries)})
