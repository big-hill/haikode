"""
apply_patch — opencode's structured multi-file edit format.

Ported from `packages/opencode/src/tool/apply_patch.ts`. The value over `edit`
is that one tool call can add, update, move and delete several files, and the
whole thing is checked before a single byte is written.

Safety properties this implementation guarantees, in order:

1. all-or-nothing: every hunk is parsed and applied *in memory* first. A patch
   whose third file does not match leaves the first two untouched. If the write
   phase itself fails (disk full, permissions), files already written are
   restored from the snapshot taken during validation.
2. containment: any path resolving outside ctx.cwd aborts the patch. Not asked
   about — refused. A model can hide one escaping path in fifty hunks.
3. read-before-edit: updating a file the model has not read is refused, exactly
   as `edit` does, so blind rewrites stay impossible.
4. one prompt: the user approves the whole patch once, with the combined diff
   in the request metadata.
5. revertible: every touched path is handed to ctx.record_original before the
   write phase, so /undo can put all of them back.
"""

import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..schema import PermissionDenied, ToolAborted
from .base import Tool, ToolContext, ToolResult, load_prompt
from .diagnostics import append_diagnostics
from .files import atomic_write, path_lock, read_source, to_newline
from .patch import (PatchError, derive_new_contents, is_empty_patch,
                    join_bom, parse_patch, split_bom)
from .paths import assert_inside

VERIFY_PREFIX = "apply_patch verification failed: "
MAX_DIFF = 60000

#: The same writer `edit` and `write` use, so a patched file keeps its mode,
#: its ownership, its dates and its BFS attributes. Bound as a module global
#: because the rollback test replaces it.
_atomic_write = atomic_write


def _diff(old: str, new: str, path: str) -> str:
    import difflib
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))
    return "".join(lines)[:MAX_DIFF]


def _counts(old: str, new: str):
    import difflib
    additions = deletions = 0
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines())
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1
    return additions, deletions


class _Change:
    """One validated file operation, ready to be written."""

    def __init__(self, kind: str, path: Path, old: str, new: str,
                 move_path: Optional[Path] = None, bom: bool = False,
                 existed: bool = True, ending: str = "\n"):
        self.kind = kind                # add | update | move | delete
        self.path = path
        self.old = old
        self.new = new
        self.move_path = move_path
        self.bom = bom
        self.existed = existed
        # The file's own line ending. Hunks are matched against LF-normalised
        # text, so it has to be put back on the way out or an edit to one line
        # of a CRLF file would rewrite every other line in it.
        self.ending = ending
        self.target = move_path or path
        self.diff = ""
        self.additions = 0
        self.deletions = 0

    def render(self, text: str) -> str:
        """`text` as it should hit the disk: file's line ending, file's BOM."""
        return join_bom(to_newline(text, self.ending), self.bom)


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = load_prompt("apply_patch.txt")
    permission = "edit"
    parameters = {
        "type": "object",
        "properties": {
            "patchText": {
                "type": "string",
                "description": "The full patch text that describes all changes to be made",
            },
        },
        "required": ["patchText"],
    }

    # --- validation -----------------------------------------------------

    def _plan(self, patch_text: str, ctx: ToolContext) -> List[_Change]:
        """Turn patch text into validated changes, or raise. Touches no disk."""
        try:
            hunks = parse_patch(patch_text)
        except PatchError as error:
            raise ValueError(VERIFY_PREFIX + str(error))

        if not hunks:
            if is_empty_patch(patch_text):
                raise ValueError("patch rejected: empty patch")
            raise ValueError(VERIFY_PREFIX + "no hunks found")

        changes: List[_Change] = []
        seen: Dict[str, str] = {}

        for hunk in hunks:
            ctx.check_abort()
            path = ctx.resolve(hunk.path)
            assert_inside(ctx, path, "apply_patch path")

            # Two hunks fighting over one file is always a model mistake, and
            # the second would silently win. Refuse before anything is written.
            key = str(path)
            if key in seen:
                raise ValueError(
                    VERIFY_PREFIX
                    + "%s appears twice in the patch (%s then %s)"
                    % (ctx.relative(path), seen[key], hunk.type))
            seen[key] = hunk.type

            if hunk.type == "add":
                changes.append(self._plan_add(hunk, path, ctx))
            elif hunk.type == "update":
                changes.append(self._plan_update(hunk, path, ctx))
            elif hunk.type == "delete":
                changes.append(self._plan_delete(hunk, path, ctx))

        for change in changes:
            change.diff = _diff(change.old, change.new, ctx.relative(change.target))
            change.additions, change.deletions = _counts(change.old, change.new)

        return changes

    def _plan_add(self, hunk, path: Path, ctx: ToolContext) -> _Change:
        if path.exists():
            raise ValueError(
                VERIFY_PREFIX
                + "Cannot add %s: it already exists. Use *** Update File: instead."
                % ctx.relative(path))
        contents = hunk.contents
        if contents and not contents.endswith("\n"):
            contents += "\n"
        text, bom = split_bom(contents)
        return _Change("add", path, "", text, bom=bom, existed=False)

    def _plan_update(self, hunk, path: Path, ctx: ToolContext) -> _Change:
        if not path.exists() or path.is_dir():
            raise ValueError(VERIFY_PREFIX
                             + "Failed to read file to update: %s" % path)
        # Same guard as `edit`: never rewrite a file the model has not seen.
        if str(path) not in ctx.read_files:
            raise ValueError(
                "%s has not been read. Use the read tool before editing it."
                % ctx.relative(path))
        try:
            # Raises rather than corrupting when the file is not valid UTF-8.
            original, ending, had_bom = read_source(path)
        except OSError as error:
            raise ValueError(VERIFY_PREFIX + str(error))

        try:
            new_text, bom = derive_new_contents(
                str(path), hunk.chunks, join_bom(original, had_bom))
        except PatchError as error:
            raise ValueError(VERIFY_PREFIX + str(error))

        move_path = None
        if hunk.move_path:
            move_path = ctx.resolve(hunk.move_path)
            assert_inside(ctx, move_path, "apply_patch move path")
            if move_path != path and move_path.exists():
                raise ValueError(
                    VERIFY_PREFIX
                    + "Cannot move %s onto existing file %s"
                    % (ctx.relative(path), ctx.relative(move_path)))

        # `original` already comes back BOM-free from read_source.
        return _Change("move" if move_path else "update", path, original,
                       new_text, move_path=move_path, bom=bom, ending=ending)

    def _plan_delete(self, hunk, path: Path, ctx: ToolContext) -> _Change:
        if not path.exists():
            raise ValueError(VERIFY_PREFIX
                             + "Failed to read file to delete: %s" % path)
        if path.is_dir():
            raise ValueError(VERIFY_PREFIX
                             + "Cannot delete a directory: %s" % path)
        try:
            # Strict, because a rollback has to write these bytes back exactly.
            text, ending, bom = read_source(path)
        except OSError as error:
            raise ValueError(VERIFY_PREFIX + str(error))
        return _Change("delete", path, text, "", bom=bom, ending=ending)

    # --- execution ------------------------------------------------------

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        patch_text = args.get("patchText") or ""
        if not patch_text.strip():
            raise ValueError("patchText is required")

        # Every path this patch could touch is locked for the whole call, so a
        # concurrent edit cannot slip between validation and the write phase
        # and have its change silently overwritten. Sorted, and taken in one
        # place, so two patches sharing files can never deadlock on each other.
        with ExitStack() as locks:
            for target in self._targets(patch_text, ctx):
                locks.enter_context(path_lock(target))
            return self._execute_locked(patch_text, ctx)

    def _targets(self, patch_text: str, ctx: ToolContext) -> List[Path]:
        """Every resolved path the patch names, deduplicated and ordered.

        Parsing twice is cheap next to the file IO, and it keeps lock
        acquisition ahead of the first read without threading locks through
        the planner.
        """
        try:
            hunks = parse_patch(patch_text)
        except PatchError:
            return []          # _plan reports the parse error properly
        found: Dict[str, Path] = {}
        for hunk in hunks:
            for raw in (hunk.path, hunk.move_path):
                if not raw:
                    continue
                try:
                    resolved = ctx.resolve(raw)
                except (OSError, ValueError):
                    continue
                found[str(resolved)] = resolved
        return [found[key] for key in sorted(found)]

    def _execute_locked(self, patch_text: str, ctx: ToolContext) -> ToolResult:
        changes = self._plan(patch_text, ctx)

        total_diff = "".join(change.diff + "\n" for change in changes if change.diff)
        files = [{
            "filePath": str(change.path),
            "relativePath": ctx.relative(change.target),
            "type": change.kind,
            "patch": change.diff,
            "additions": change.additions,
            "deletions": change.deletions,
            "movePath": str(change.move_path) if change.move_path else None,
        } for change in changes]
        relatives = [ctx.relative(change.path) for change in changes]

        ctx.check_abort()
        # One prompt for the whole patch, with the combined diff attached.
        ctx.ask("edit", relatives,
                "Apply patch to %d file%s" % (len(changes),
                                              "" if len(changes) == 1 else "s"),
                {"diff": total_diff, "files": files,
                 "filepath": ", ".join(relatives)},
                always=["*"])

        self._apply(changes, ctx)

        summary = []
        for change in changes:
            mark = {"add": "A", "delete": "D"}.get(change.kind, "M")
            summary.append("%s %s" % (mark, ctx.relative(change.target)))
        output = "Success. Updated the following files:\n" + "\n".join(summary)

        for change in changes:
            if change.kind == "delete":
                continue
            output = append_diagnostics(ctx, change.target, output,
                                        label=ctx.relative(change.target))

        return ToolResult(
            title="%d file%s patched" % (len(changes),
                                         "" if len(changes) == 1 else "s"),
            output=output,
            metadata={"diff": total_diff, "files": files,
                      "count": len(changes)})

    def _apply(self, changes: List[_Change], ctx: ToolContext) -> None:
        """
        Write phase. Every original is recorded first (so /undo works even if
        we crash halfway), and a failure part-way rolls the earlier files back.
        """
        for change in changes:
            ctx.record_original(change.path)
            if change.move_path is not None:
                ctx.record_original(change.move_path)

        done: List[_Change] = []
        try:
            for change in changes:
                if change.kind == "add":
                    _atomic_write(change.path, change.render(change.new))
                    ctx.read_files.add(str(change.path))
                elif change.kind == "update":
                    _atomic_write(change.path, change.render(change.new))
                elif change.kind == "move":
                    # source=: the destination does not exist yet, so the mode,
                    # the dates and the BFS attributes have to be inherited
                    # from the file being renamed rather than from the target.
                    _atomic_write(change.move_path, change.render(change.new),
                                  source=change.path)
                    os.unlink(change.path)
                    ctx.read_files.discard(str(change.path))
                    ctx.read_files.add(str(change.move_path))
                elif change.kind == "delete":
                    os.unlink(change.path)
                    ctx.read_files.discard(str(change.path))
                done.append(change)
        except (OSError, ToolAborted, PermissionDenied):
            self._rollback(done)
            raise

    @staticmethod
    def _rollback(done: List[_Change]) -> None:
        """Best effort restore of files this call already wrote."""
        for change in reversed(done):
            try:
                if change.kind == "add":
                    if change.path.exists():
                        os.unlink(change.path)
                elif change.kind == "move":
                    # Undo the rename in the same direction it was made, so the
                    # metadata travels back with the contents.
                    _atomic_write(change.path, change.render(change.old),
                                  source=change.move_path)
                    if change.move_path and change.move_path.exists():
                        os.unlink(change.move_path)
                else:
                    _atomic_write(change.path, change.render(change.old))
            except OSError:
                continue


APPLY_PATCH_TOOL = ApplyPatchTool()

__all__ = ["ApplyPatchTool", "APPLY_PATCH_TOOL"]
