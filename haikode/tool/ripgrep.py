"""
ripgrep backend for glob and grep.

opencode delegates all searching to ripgrep; the stdlib walk in search.py
exists because Haiku's base install has no `rg`. It is single-threaded Python,
so it needs file caps and a wall-clock budget that ripgrep does not: on a
286k-file home directory the Python path spends its whole budget before it
finishes, while `rg` finishes the same tree in well under a second.

So: use ripgrep when it is on PATH, fall back otherwise. Both paths must
produce the same results, which is why this module returns None — meaning
"caller, do it yourself" — for every case it cannot serve identically:

  * no `rg` on PATH;
  * a pattern Rust's regex engine rejects. Python's `re` has lookaround and
    backreferences and ripgrep's default engine does not, so a pattern using
    them must go to the Python matcher rather than fail the user's search;
  * any unexpected exit, so a broken or unusual rg build degrades instead of
    breaking search entirely.

Install it on Haiku with `pkgman install ripgrep`.

Security note: ripgrep does not follow symlinks unless asked, which is the
same containment the Python walk enforces by resolving each link and skipping
the ones that leave the tree. `--follow` must therefore never be passed here.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Resolved once per process: PATH does not change under a running agent, and
# a which() per search would be a syscall on every call.
_RG_PATH: Any = False


def ripgrep_path() -> Optional[str]:
    """The `rg` binary, or None when it is not installed."""
    global _RG_PATH
    if _RG_PATH is False:
        _RG_PATH = shutil.which("rg")
    return _RG_PATH


def reset_cache() -> None:
    """Forget the cached lookup. For tests, and for a `/doctor` re-probe."""
    global _RG_PATH
    _RG_PATH = False


def _exclusions(ignored_dirs: Sequence[str]) -> List[str]:
    """Our ignored directories as ripgrep globs.

    ripgrep already honours .gitignore, but the directories in IGNORED_DIRS
    are skipped whether or not the project has one — that is the behaviour the
    Python walk has, and the two backends must not disagree about it.
    """
    globs: List[str] = []
    for name in ignored_dirs:
        globs.extend(["-g", "!%s/" % name])
    return globs


def _base_args(root: Path, ignored_dirs: Sequence[str]) -> List[str]:
    args = [
        "--hidden",          # .github/.config/.claude are searchable for us
        "--no-follow",       # containment: never leave the tree via a symlink
        "--no-messages",     # unreadable files are skipped, not reported
        # ripgrep only reads .gitignore inside a git repository; the Python
        # walk reads it wherever it finds one. Without this the two backends
        # disagree about every ignored file in a non-repo directory, which a
        # comparison run caught immediately.
        "--no-require-git",
    ]
    args.extend(_exclusions(ignored_dirs))
    return args


class RipgrepUnavailable(Exception):
    """Raised internally when the caller must fall back to the Python walk."""


def _run(argv: List[str], timeout: float) -> subprocess.Popen:
    try:
        return subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                errors="replace")
    except OSError as exc:
        raise RipgrepUnavailable(str(exc))


def _finish(process: subprocess.Popen) -> Tuple[int, str]:
    """Drain and reap. Returns (exit code, stderr)."""
    try:
        _, err = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        _, err = process.communicate()
    return process.returncode or 0, err or ""


def grep(pattern: str, root: Path, keep: Any, ignored_dirs: Sequence[str],
         max_matches: int, max_file_bytes: int, max_line_chars: int,
         budget: Any, check_abort: Any = None) -> Optional[Dict[str, Any]]:
    """Content search through ripgrep.

    Returns the same shape the Python matcher builds — `lines` formatted
    "path:lineno: text" — or None when the caller must do it itself.

    `keep(path)` decides whether a hit survives the caller's `include` glob.
    That filter is deliberately not handed to ripgrep as `-g`: a command-line
    glob takes precedence over .gitignore there, so `-g '*.py'` silently
    resurrects every ignored .py file. A comparison run against the stdlib
    backend caught exactly that.

    `--json` rather than plain output: a path containing a colon makes
    "path:line:text" ambiguous, and the JSON stream also states plainly when
    a file was skipped as binary.
    """
    binary = ripgrep_path()
    if not binary:
        return None

    argv = [binary, "--json", "--max-filesize", str(max_file_bytes)]
    argv.extend(_base_args(root, ignored_dirs))
    argv.extend(["-e", pattern, "--", str(root)])

    try:
        process = _run(argv, budget.seconds)
    except RipgrepUnavailable:
        return None

    lines: List[str] = []
    files: set = set()
    long_lines = 0
    truncated = False
    stdout = process.stdout
    assert stdout is not None
    try:
        for raw in stdout:
            if check_abort is not None:
                check_abort()
            if budget.check():
                break
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path = ((data.get("path") or {}).get("text") or "")
            number = data.get("line_number") or 0
            text = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
            if not path:
                continue
            if keep is not None and not keep(Path(path)):
                continue
            if len(text) > max_line_chars:
                text = text[:max_line_chars]
                long_lines += 1
            if len(text) > 300:
                text = text[:300] + "…"
            files.add(path)
            lines.append("%s:%d: %s" % (path, number, text))
            if len(lines) >= max_matches:
                truncated = True
                break
    finally:
        if process.poll() is None:
            process.kill()
        code, err = _finish(process)

    # Exit 0 = matches, 1 = none, 2 = error. A rejected pattern (lookaround,
    # backreference) lands in 2 and must go to the Python engine, which
    # supports them, rather than surface as "your search failed".
    if code >= 2 and not lines:
        return None

    return {
        "lines": lines,
        "files": len(files),
        "truncated": truncated,
        "long_lines": long_lines,
        "backend": "ripgrep",
    }


def list_files(root: Path, ignored_dirs: Sequence[str], budget: Any,
               check_abort: Any = None) -> Optional[List[Path]]:
    """Every searchable file under `root`, via `rg --files`.

    Unfiltered and unsorted on purpose: the caller owns both the glob matcher
    and the ordering. Passing the pattern as `-g` would be faster but wrong —
    a command-line glob overrides .gitignore in ripgrep, so ignored files
    would come back.
    """
    binary = ripgrep_path()
    if not binary:
        return None

    argv = [binary, "--files"]
    argv.extend(_base_args(root, ignored_dirs))
    argv.extend(["--", str(root)])

    try:
        process = _run(argv, budget.seconds)
    except RipgrepUnavailable:
        return None

    found: List[Path] = []
    stdout = process.stdout
    assert stdout is not None
    try:
        for raw in stdout:
            if check_abort is not None:
                check_abort()
            if budget.check():
                break
            name = raw.rstrip("\n")
            if name:
                found.append(Path(name))
    finally:
        if process.poll() is None:
            process.kill()
        code, _err = _finish(process)

    if code >= 2 and not found:
        return None
    return found


def describe() -> str:
    """One line for /status and doctor."""
    binary = ripgrep_path()
    if not binary:
        return ("search: stdlib walk (install ripgrep with "
                "`pkgman install ripgrep` for unbounded, faster search)")
    return "search: ripgrep at %s" % binary


__all__ = ["ripgrep_path", "reset_cache", "grep", "list_files", "describe"]
