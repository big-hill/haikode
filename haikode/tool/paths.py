"""
Working-directory containment for tools.

Mirrors opencode's `tool/external-directory.ts`: touching something outside the
session's directory is not forbidden, it is *asked about*, and the grant is
keyed on the parent directory so approving one file in ~/Documents does not
approve the whole filesystem.

apply_patch is the exception — a structured multi-file patch that reaches
outside the working directory is refused outright rather than negotiated,
because the model can hide a single escaping path in a wall of hunks.
"""

import os
from pathlib import Path
from typing import Any


def is_inside(ctx: Any, path: Any) -> bool:
    """True when `path` is the working directory or lives under it."""
    root = os.path.normpath(str(getattr(ctx, "cwd", "")) or os.getcwd())
    target = os.path.normpath(str(path))
    if target == root:
        return True
    return target.startswith(root.rstrip(os.sep) + os.sep)


def parent_glob(path: Any, kind: str = "file") -> str:
    """
    The pattern an 'always' grant is stored under: `<parent dir>/*`.

    With one exception: when the parent *is* the filesystem root the glob
    would be `/*`, and fnmatch's `*` spans `/` — so an "always" answer to
    `cat /some-file` or `grep -r x /` would grant every path on the machine
    while the prompt named a single file. A grant that wide can only be a
    mistake, so the root case is stored as the literal path instead. Deny
    rules are unaffected: `/*` still matches `/etc/*` and `/some-file` alike.
    """
    target = Path(str(path))
    directory = target if kind == "directory" else target.parent
    if str(directory) == directory.anchor:
        return str(target)
    return str(directory / "*")


def assert_external_directory(ctx: Any, path: Any, kind: str = "file",
                              action: str = "Access") -> bool:
    """
    No-op for paths inside the working directory; otherwise asks for
    permission and raises PermissionDenied if the user says no.

    Returns True when the path was outside and approval was granted.
    """
    if path is None:
        return False
    if is_inside(ctx, path):
        return False

    glob = parent_glob(path, kind=kind)
    ctx.ask("external_directory", [glob],
            "%s %s (outside the working directory)" % (action, path),
            {"filepath": str(path), "parentDir": str(Path(glob).parent),
             "external": True},
            always=[glob])
    return True


def assert_inside(ctx: Any, path: Any, what: str = "path") -> None:
    """Hard refusal — used where negotiating would be a foot-gun."""
    if not is_inside(ctx, path):
        raise ValueError(
            "%s escapes the working directory: %s (cwd: %s)"
            % (what, path, getattr(ctx, "cwd", "")))
