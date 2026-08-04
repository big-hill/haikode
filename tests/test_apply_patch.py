"""
apply_patch, question, and the LSP diagnostics that hang off edit/write.

The security-relevant assertions here are the containment ones: a patch that
names a path outside the working directory must be refused outright, and a
patch that fails to apply anywhere must not have written anything anywhere.
"""

import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.permission import Permissions  # noqa: E402
from haikode.schema import PermissionDenied  # noqa: E402
from haikode.tool import files as files_module  # noqa: E402
from haikode.tool import patch as patch_module  # noqa: E402
from haikode.tool.apply_patch import ApplyPatchTool  # noqa: E402
from haikode.tool.base import ToolContext  # noqa: E402
from haikode.tool.diagnostics import (append_diagnostics,  # noqa: E402
                                      diagnostics_block)
from haikode.tool.patch import (PatchError, derive_new_contents,  # noqa: E402
                                parse_patch, seek_sequence)
from haikode.tool.question import QuestionTool  # noqa: E402


def envelope(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"]) + "\n"


class PatchParserTests(unittest.TestCase):
    def test_add_update_delete_and_move(self):
        hunks = parse_patch(envelope(
            "*** Add File: hello.txt",
            "+Hello world",
            "*** Update File: src/app.py",
            "*** Move to: src/main.py",
            "@@ def greet():",
            '-print("Hi")',
            '+print("Hello, world!")',
            "*** Delete File: obsolete.txt",
        ))
        self.assertEqual([h.type for h in hunks], ["add", "update", "delete"])
        self.assertEqual(hunks[0].path, "hello.txt")
        self.assertEqual(hunks[0].contents, "Hello world")
        self.assertEqual(hunks[1].move_path, "src/main.py")
        self.assertEqual(hunks[1].chunks[0].change_context, "def greet():")
        self.assertEqual(hunks[1].chunks[0].old_lines, ['print("Hi")'])
        self.assertEqual(hunks[1].chunks[0].new_lines, ['print("Hello, world!")'])
        self.assertEqual(hunks[2].path, "obsolete.txt")

    def test_missing_markers_rejected(self):
        with self.assertRaises(PatchError) as cm:
            parse_patch("*** Add File: x.txt\n+hi\n")
        self.assertIn("missing Begin/End markers", str(cm.exception))

    def test_end_before_begin_rejected(self):
        with self.assertRaises(PatchError):
            parse_patch("*** End Patch\n*** Begin Patch\n")

    def test_heredoc_is_unwrapped(self):
        raw = "cat <<'EOF'\n" + envelope("*** Delete File: gone.txt").strip() + "\nEOF"
        hunks = parse_patch(raw)
        self.assertEqual([h.type for h in hunks], ["delete"])

    def test_empty_patch_detected(self):
        self.assertTrue(patch_module.is_empty_patch(
            "*** Begin Patch\r\n*** End Patch\r\n"))
        self.assertFalse(patch_module.is_empty_patch(envelope("*** Delete File: x")))

    def test_seek_sequence_tolerates_whitespace_and_smart_quotes(self):
        lines = ["def f():", "    return “hi”  ", "done"]
        self.assertEqual(seek_sequence(lines, ["def f():"], 0), 0)
        self.assertEqual(seek_sequence(lines, ['    return "hi"'], 0), 1)

    def test_derive_keeps_trailing_newline(self):
        chunks = parse_patch(envelope(
            "*** Update File: a.txt",
            "@@",
            "-one",
            "+ONE",
        ))[0].chunks
        text, bom = derive_new_contents("a.txt", chunks, "one\ntwo\n")
        self.assertEqual(text, "ONE\ntwo\n")
        self.assertFalse(bom)

    def test_derive_preserves_byte_order_mark(self):
        chunks = parse_patch(envelope(
            "*** Update File: a.txt", "@@", "-one", "+ONE"))[0].chunks
        text, bom = derive_new_contents("a.txt", chunks, "﻿one\n")
        self.assertTrue(bom)
        self.assertFalse(text.startswith("﻿"))

    def test_unmatched_chunk_raises(self):
        chunks = parse_patch(envelope(
            "*** Update File: a.txt", "@@", "-nope", "+yes"))[0].chunks
        with self.assertRaises(PatchError) as cm:
            derive_new_contents("a.txt", chunks, "one\ntwo\n")
        self.assertIn("Failed to find expected lines", str(cm.exception))

    def test_missing_context_raises(self):
        chunks = parse_patch(envelope(
            "*** Update File: a.txt", "@@ class Missing:", "-one", "+two"))[0].chunks
        with self.assertRaises(PatchError) as cm:
            derive_new_contents("a.txt", chunks, "one\n")
        self.assertIn("Failed to find context", str(cm.exception))


class ApplyPatchCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-patch-")
        self.outside = tempfile.mkdtemp(prefix="haikode-outside-")
        self.ctx = ToolContext(cwd=self.dir,
                               permissions=Permissions(auto_approve=True))
        self.tool = ApplyPatchTool()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def write(self, name, content, mark_read=True):
        path = Path(self.dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path = path.resolve()
        if mark_read:
            self.ctx.read_files.add(str(path))
        return path

    def apply(self, *body, ctx=None):
        return self.tool.execute({"patchText": envelope(*body)}, ctx or self.ctx)


class ApplyPatchTests(ApplyPatchCase):
    def test_add_update_delete_in_one_call(self):
        self.write("keep.py", "value = 1\n")
        self.write("gone.txt", "bye\n")

        result = self.apply(
            "*** Add File: new/created.txt",
            "+fresh",
            "*** Update File: keep.py",
            "@@",
            "-value = 1",
            "+value = 2",
            "*** Delete File: gone.txt",
        )

        self.assertEqual((Path(self.dir) / "new/created.txt").read_text(), "fresh\n")
        self.assertEqual((Path(self.dir) / "keep.py").read_text(), "value = 2\n")
        self.assertFalse((Path(self.dir) / "gone.txt").exists())
        self.assertIn("A new/created.txt", result.output)
        self.assertIn("M keep.py", result.output)
        self.assertIn("D gone.txt", result.output)
        self.assertEqual(result.metadata["count"], 3)

    def test_move_renames_and_rewrites(self):
        self.write("src/app.py", 'print("Hi")\n')
        self.apply(
            "*** Update File: src/app.py",
            "*** Move to: src/main.py",
            "@@",
            '-print("Hi")',
            '+print("Hello")',
        )
        self.assertFalse((Path(self.dir) / "src/app.py").exists())
        self.assertEqual((Path(self.dir) / "src/main.py").read_text(), 'print("Hello")\n')

    def test_update_requires_prior_read(self):
        self.write("a.py", "x = 1\n", mark_read=False)
        with self.assertRaises(ValueError) as cm:
            self.apply("*** Update File: a.py", "@@", "-x = 1", "+x = 2")
        self.assertIn("has not been read", str(cm.exception))
        self.assertEqual((Path(self.dir) / "a.py").read_text(), "x = 1\n")

    def test_empty_patch_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.tool.execute({"patchText": "*** Begin Patch\n*** End Patch\n"},
                              self.ctx)
        self.assertIn("patch rejected: empty patch", str(cm.exception))

    def test_patch_without_hunks_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.apply("nothing useful here")
        self.assertIn("no hunks found", str(cm.exception))

    def test_malformed_patch_reports_verification_failure(self):
        with self.assertRaises(ValueError) as cm:
            self.tool.execute({"patchText": "*** Add File: x\n+hi\n"}, self.ctx)
        self.assertIn("apply_patch verification failed", str(cm.exception))
        self.assertIn("missing Begin/End markers", str(cm.exception))

    def test_missing_update_target_reports_verification_failure(self):
        with self.assertRaises(ValueError) as cm:
            self.apply("*** Update File: ghost.py", "@@", "-a", "+b")
        self.assertIn("Failed to read file to update", str(cm.exception))

    def test_add_over_existing_file_refused(self):
        self.write("a.txt", "original\n")
        with self.assertRaises(ValueError) as cm:
            self.apply("*** Add File: a.txt", "+clobbered")
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "original\n")

    def test_same_file_twice_refused(self):
        self.write("a.txt", "one\n")
        with self.assertRaises(ValueError) as cm:
            self.apply("*** Update File: a.txt", "@@", "-one", "+two",
                       "*** Delete File: a.txt")
        self.assertIn("appears twice", str(cm.exception))
        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "one\n")


class ApplyPatchContainmentTests(ApplyPatchCase):
    """A patch must never reach outside the session's working directory."""

    def test_relative_escape_refused(self):
        victim = Path(self.outside) / "victim.txt"
        victim.write_text("untouched\n")
        relative = "../" + Path(self.outside).name + "/victim.txt"
        self.ctx.read_files.add(str(victim.resolve()))

        with self.assertRaises(ValueError) as cm:
            self.apply("*** Update File: " + relative, "@@", "-untouched", "+owned")
        self.assertIn("escapes the working directory", str(cm.exception))
        self.assertEqual(victim.read_text(), "untouched\n")

    def test_absolute_escape_refused(self):
        target = Path(self.outside) / "planted.txt"
        with self.assertRaises(ValueError) as cm:
            self.apply("*** Add File: " + str(target), "+payload")
        self.assertIn("escapes the working directory", str(cm.exception))
        self.assertFalse(target.exists())

    def test_delete_outside_cwd_refused(self):
        victim = Path(self.outside) / "important.txt"
        victim.write_text("keep\n")
        with self.assertRaises(ValueError):
            self.apply("*** Delete File: " + str(victim))
        self.assertTrue(victim.exists())

    def test_move_target_outside_cwd_refused(self):
        self.write("a.txt", "one\n")
        escape = str(Path(self.outside) / "stolen.txt")
        with self.assertRaises(ValueError) as cm:
            self.apply("*** Update File: a.txt", "*** Move to: " + escape,
                       "@@", "-one", "+two")
        self.assertIn("escapes the working directory", str(cm.exception))
        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "one\n")
        self.assertFalse(Path(escape).exists())

    def test_escaping_hunk_aborts_the_whole_patch(self):
        """The escape is hidden behind a legitimate change; nothing applies."""
        self.write("a.txt", "one\n")
        escape = str(Path(self.outside) / "planted.txt")
        with self.assertRaises(ValueError):
            self.apply("*** Update File: a.txt", "@@", "-one", "+two",
                       "*** Add File: " + escape, "+payload")
        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "one\n")
        self.assertFalse(Path(escape).exists())


class ApplyPatchAtomicityTests(ApplyPatchCase):
    def test_a_failing_hunk_leaves_every_file_untouched(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "two\n")
        with self.assertRaises(ValueError):
            self.apply("*** Update File: a.txt", "@@", "-one", "+ONE",
                       "*** Update File: b.txt", "@@", "-nonexistent", "+x")
        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "one\n")
        self.assertEqual((Path(self.dir) / "b.txt").read_text(), "two\n")

    def test_a_failure_during_the_write_phase_rolls_back(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "two\n")

        import haikode.tool.apply_patch as module
        real = module._atomic_write

        def flaky(path, content, source=None):
            if path.name == "b.txt":
                raise OSError("disk full")
            return real(path, content, source)

        module._atomic_write = flaky
        try:
            with self.assertRaises(OSError):
                self.apply("*** Update File: a.txt", "@@", "-one", "+ONE",
                           "*** Update File: b.txt", "@@", "-two", "+TWO")
        finally:
            module._atomic_write = real

        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "one\n")
        self.assertEqual((Path(self.dir) / "b.txt").read_text(), "two\n")


class ApplyPatchPermissionTests(ApplyPatchCase):
    def test_one_prompt_for_the_whole_patch_with_a_combined_diff(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "two\n")
        seen = []

        ctx = ToolContext(cwd=self.dir,
                          permissions=Permissions(asker=lambda r: seen.append(r) or "once"))
        ctx.read_files.update(self.ctx.read_files)

        self.apply("*** Update File: a.txt", "@@", "-one", "+ONE",
                   "*** Update File: b.txt", "@@", "-two", "+TWO",
                   "*** Add File: c.txt", "+three", ctx=ctx)

        self.assertEqual(len(seen), 1, "a patch must cost exactly one prompt")
        request = seen[0]
        self.assertEqual(request.key, "edit")
        self.assertIn("a.txt", request.metadata["diff"])
        self.assertIn("b.txt", request.metadata["diff"])
        self.assertIn("+ONE", request.metadata["diff"])
        self.assertEqual(len(request.metadata["files"]), 3)

    def test_rejection_writes_nothing(self):
        self.write("a.txt", "one\n")
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(asker=lambda r: "reject"))
        ctx.read_files.update(self.ctx.read_files)
        with self.assertRaises(PermissionDenied):
            self.apply("*** Update File: a.txt", "@@", "-one", "+ONE",
                       "*** Add File: b.txt", "+new", ctx=ctx)
        self.assertEqual((Path(self.dir) / "a.txt").read_text(), "one\n")
        self.assertFalse((Path(self.dir) / "b.txt").exists())

    def test_every_touched_file_is_recorded_for_undo(self):
        a = self.write("a.txt", "one\n")
        gone = self.write("gone.txt", "bye\n")
        self.apply("*** Update File: a.txt", "@@", "-one", "+ONE",
                   "*** Add File: fresh.txt", "+new",
                   "*** Delete File: gone.txt")

        fresh = (Path(self.dir) / "fresh.txt").resolve()
        self.assertEqual(self.ctx.modified_files[str(a)], "one\n")
        self.assertEqual(self.ctx.modified_files[str(gone)], "bye\n")
        self.assertIsNone(self.ctx.modified_files[str(fresh)])

    def test_move_records_both_ends(self):
        source = self.write("a.txt", "one\n")
        self.apply("*** Update File: a.txt", "*** Move to: b.txt",
                   "@@", "-one", "+two")
        destination = (Path(self.dir) / "b.txt").resolve()
        self.assertEqual(self.ctx.modified_files[str(source)], "one\n")
        self.assertIn(str(destination), self.ctx.modified_files)


# --- what the atomic replace must not destroy --------------------------
#
# edit, write and apply_patch all go through haikode.tool.files.atomic_write,
# so these cover every writer in the project. They live here because
# apply_patch is the only one of the three with a test module of its own.


OLD_TIME = 1600000000        # a fixed, obviously-not-now mtime


def atime_is_settable(directory) -> bool:
    """Whether this filesystem honours the atime handed to utime().

    BFS does not: on hrev57937 os.utime() applies the mtime and leaves atime
    at the current time, whatever it was passed. No writer can work around
    that, so the atime half of the assertion only runs where the platform can
    actually deliver it.
    """
    probe = Path(directory) / ".atime-probe"
    probe.write_text("x")
    try:
        os.utime(probe, (OLD_TIME, OLD_TIME))
        # An edit READS the file before writing it back, and relatime-style
        # mounts (Linux ext4) bump atime on exactly that read — so a probe
        # that never reads reports "settable" on a filesystem where the
        # value is unpreservable in practice. Mirror what the writer does.
        probe.read_text()
        return abs(os.stat(probe).st_atime - OLD_TIME) < 1
    finally:
        probe.unlink()


class WriterCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-writer-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ctx = ToolContext(cwd=self.dir,
                               permissions=Permissions(auto_approve=True))
        self.edit = files_module.EditTool()
        self.write_tool = files_module.WriteTool()
        self.patcher = ApplyPatchTool()

    def seed(self, name, data, mode=0o644, dated=True):
        """A file with a known mode and a known, old timestamp."""
        path = Path(self.dir) / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.chmod(path, mode)
        if dated:
            os.utime(path, (OLD_TIME, OLD_TIME))
        path = path.resolve()
        self.ctx.read_files.add(str(path))
        return path

    def do_edit(self, path, old, new, **extra):
        args = {"filePath": str(path), "oldString": old, "newString": new}
        args.update(extra)
        return self.edit.execute(args, self.ctx)

    def do_write(self, path, content):
        return self.write_tool.execute(
            {"filePath": str(path), "content": content}, self.ctx)

    def do_patch(self, *body):
        return self.patcher.execute({"patchText": envelope(*body)}, self.ctx)


class PreservedMetadataTests(WriterCase):
    """mkstemp + os.replace() gives the file a brand new inode.

    That inode starts out mode 0600, owned by this process, freshly dated and
    — on BFS — with none of the original's extended attributes. Everything the
    old inode carried has to be put back before the rename, or an agent strips
    a script's executable bit and a file's BEOS:TYPE on every single edit.
    """

    def test_edit_keeps_the_executable_bit(self):
        path = self.seed("script.sh", "#!/bin/sh\necho one\n", mode=0o755)
        self.do_edit(path, "one", "two")
        self.assertEqual(path.read_text(), "#!/bin/sh\necho two\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755)

    def test_edit_keeps_a_group_readable_mode_too(self):
        # 0o640 rather than 0o600, which is what mkstemp would have given the
        # replacement anyway and so proves nothing.
        path = self.seed("secret.env", "TOKEN=one\n", mode=0o640)
        self.do_edit(path, "one", "two")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_write_keeps_the_executable_bit(self):
        path = self.seed("script.sh", "#!/bin/sh\necho one\n", mode=0o755)
        self.do_write(path, "#!/bin/sh\necho two\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755)

    def test_apply_patch_keeps_the_executable_bit(self):
        path = self.seed("script.sh", "#!/bin/sh\necho one\n", mode=0o755)
        self.do_patch("*** Update File: script.sh", "@@", "-echo one", "+echo two")
        self.assertEqual(path.read_text(), "#!/bin/sh\necho two\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o755)

    def test_a_move_inherits_from_the_file_it_came_from(self):
        # The destination does not exist yet, so there is nothing at the target
        # to inherit: the mode has to come from the file being renamed.
        source = self.seed("old.sh", "echo one\n", mode=0o755)
        self.do_patch("*** Update File: old.sh", "*** Move to: new.sh",
                      "@@", "-echo one", "+echo two")
        moved = Path(self.dir) / "new.sh"
        self.assertFalse(source.exists())
        self.assertEqual(moved.read_text(), "echo two\n")
        self.assertEqual(stat.S_IMODE(os.stat(moved).st_mode), 0o755)

    def test_edit_keeps_the_files_dates(self):
        path = self.seed("notes.txt", "one\n")
        settable = atime_is_settable(self.dir)
        self.do_edit(path, "one", "two")
        after = os.stat(path)
        self.assertAlmostEqual(after.st_mtime, OLD_TIME, delta=1)
        if settable:
            self.assertAlmostEqual(after.st_atime, OLD_TIME, delta=1)

    def test_apply_patch_keeps_the_files_dates(self):
        path = self.seed("notes.txt", "one\n")
        self.do_patch("*** Update File: notes.txt", "@@", "-one", "+two")
        self.assertAlmostEqual(os.stat(path).st_mtime, OLD_TIME, delta=1)

    def test_a_new_file_is_not_given_a_stale_date(self):
        fresh = Path(self.dir) / "fresh.txt"
        self.do_write(fresh, "hi\n")
        self.assertGreater(os.stat(fresh).st_mtime, OLD_TIME)

    # --- BFS attributes ---------------------------------------------------

    def _spy(self, seen, live):
        def spy(src, dst, names=None):
            seen.append((Path(src), Path(dst),
                         Path(dst).read_text(), live.read_text()))
            return True
        return spy

    def test_attributes_are_copied_onto_the_temp_file_before_the_rename(self):
        """Ordering is the whole design: copy first, then swap.

        Copying after the rename would leave a window in which the file the
        user sees has already lost its BEOS:TYPE, and would stop being atomic.
        """
        path = self.seed("doc.txt", "one\n")
        seen = []
        with patch("haikode.haiku.copy_attributes", self._spy(seen, path)):
            self.do_edit(path, "one", "two")

        self.assertEqual(len(seen), 1)
        src, dst, staged, live = seen[0]
        self.assertEqual(src, path, "attributes must be read off the original")
        self.assertNotEqual(dst, path, "and written onto the temp file")
        self.assertEqual(dst.parent, path.parent,
                         "temp file must share the volume, or BFS cannot copy")
        self.assertEqual(staged, "two\n", "temp file already holds the new text")
        self.assertEqual(live, "one\n", "and the target is still untouched")

    def test_apply_patch_copies_attributes_too(self):
        path = self.seed("doc.txt", "one\n")
        seen = []
        with patch("haikode.haiku.copy_attributes", self._spy(seen, path)):
            self.do_patch("*** Update File: doc.txt", "@@", "-one", "+two")
        self.assertEqual([entry[0] for entry in seen], [path])

    def test_a_move_copies_the_attributes_off_the_original(self):
        source = self.seed("old.txt", "one\n")
        seen = []

        def spy(src, dst, names=None):
            seen.append(Path(src))
            return True

        with patch("haikode.haiku.copy_attributes", spy):
            self.do_patch("*** Update File: old.txt", "*** Move to: new.txt",
                          "@@", "-one", "+two")
        self.assertEqual(seen, [source])

    def test_a_brand_new_file_has_nothing_to_carry_forward(self):
        seen = []
        with patch("haikode.haiku.copy_attributes",
                   lambda *a, **k: seen.append(a)):
            self.do_write(Path(self.dir) / "fresh.txt", "hi\n")
        self.assertEqual(seen, [])


class TextIntegrityTests(WriterCase):
    """Bytes the model never mentioned must come back out unchanged."""

    LATIN1 = b"caf\xe9 one\n"

    def test_edit_refuses_a_file_that_is_not_utf8(self):
        path = self.seed("legacy.txt", self.LATIN1)
        with self.assertRaises(ValueError) as caught:
            self.do_edit(path, "one", "two")
        self.assertIn("not valid UTF-8", str(caught.exception))

    def test_a_refused_edit_leaves_every_byte_alone(self):
        path = self.seed("legacy.txt", self.LATIN1)
        with self.assertRaises(ValueError):
            self.do_edit(path, "one", "two")
        self.assertEqual(path.read_bytes(), self.LATIN1)

    def test_apply_patch_refuses_a_file_that_is_not_utf8(self):
        path = self.seed("legacy.txt", self.LATIN1)
        with self.assertRaises(ValueError) as caught:
            self.do_patch("*** Update File: legacy.txt", "@@", "-one", "+two")
        self.assertIn("not valid UTF-8", str(caught.exception))
        self.assertEqual(path.read_bytes(), self.LATIN1)

    def test_an_undecodable_file_in_a_patch_stops_the_whole_patch(self):
        good = self.seed("good.txt", "one\n")
        self.seed("legacy.txt", self.LATIN1)
        with self.assertRaises(ValueError):
            self.do_patch("*** Update File: good.txt", "@@", "-one", "+ONE",
                          "*** Update File: legacy.txt", "@@", "-x", "+y")
        self.assertEqual(good.read_text(), "one\n")

    def test_edit_keeps_crlf_line_endings(self):
        path = self.seed("dos.txt", b"one\r\ntwo\r\nthree\r\n")
        self.do_edit(path, "two", "TWO")
        self.assertEqual(path.read_bytes(), b"one\r\nTWO\r\nthree\r\n")

    def test_a_multi_line_lf_oldstring_still_matches_a_crlf_file(self):
        # The model always writes LF. Matching happens in normalised form, but
        # the result goes back in the file's own ending rather than converting
        # every untouched line to LF.
        path = self.seed("dos.txt", b"one\r\ntwo\r\nthree\r\n")
        self.do_edit(path, "one\ntwo", "ONE\nTWO")
        self.assertEqual(path.read_bytes(), b"ONE\r\nTWO\r\nthree\r\n")

    def test_apply_patch_keeps_crlf_line_endings(self):
        path = self.seed("dos.txt", b"one\r\ntwo\r\n")
        self.do_patch("*** Update File: dos.txt", "@@", "-one", "+ONE")
        self.assertEqual(path.read_bytes(), b"ONE\r\ntwo\r\n")

    def test_an_lf_file_is_not_given_crlf(self):
        path = self.seed("unix.txt", b"one\ntwo\n")
        self.do_edit(path, "one", "ONE")
        self.assertEqual(path.read_bytes(), b"ONE\ntwo\n")

    def test_write_keeps_an_existing_byte_order_mark(self):
        path = self.seed("bom.txt", b"\xef\xbb\xbfone\n")
        self.do_write(path, "two\n")
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbftwo\n")

    def test_edit_keeps_an_existing_byte_order_mark(self):
        path = self.seed("bom.txt", b"\xef\xbb\xbfone\n")
        self.do_edit(path, "one", "two")
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbftwo\n")

    def test_a_file_without_a_bom_does_not_grow_one(self):
        path = self.seed("plain.txt", b"one\n")
        self.do_edit(path, "one", "two")
        self.assertEqual(path.read_bytes(), b"two\n")

    def test_non_ascii_survives_a_round_trip(self):
        path = self.seed("no.txt", "blåbærsyltetøy one\n")
        self.do_edit(path, "one", "to")
        self.assertEqual(path.read_bytes(), "blåbærsyltetøy to\n".encode("utf-8"))


class EditSerialisationTests(WriterCase):
    """One lock per resolved path — opencode's `locks` map, ported."""

    def test_the_same_path_hands_back_the_same_lock(self):
        target = Path(self.dir) / "x.txt"
        with files_module.path_lock(target):
            held = files_module._PATH_LOCKS[str(target)]
            self.assertFalse(held.acquire(blocking=False),
                             "a second holder must wait")
        self.assertTrue(held.acquire(blocking=False))
        held.release()

    def test_different_paths_do_not_block_each_other(self):
        a, b = Path(self.dir) / "a.txt", Path(self.dir) / "b.txt"
        with files_module.path_lock(a), files_module.path_lock(b):
            pass          # a single shared lock would deadlock here

    def test_two_concurrent_edits_of_one_file_do_not_lose_a_change(self):
        path = self.seed("both.txt", "one\ntwo\n")
        real_read = files_module.read_source

        def slow_read(target):
            result = real_read(target)
            # Widen the read-modify-write window so an unserialised pair would
            # reliably both read the same stale text and the second would win.
            time.sleep(0.05)
            return result

        failures = []

        def run(old, new):
            try:
                self.do_edit(path, old, new)
            except BaseException as error:      # noqa: BLE001 - reported below
                failures.append(error)

        with patch.object(files_module, "read_source", slow_read):
            first = threading.Thread(target=run, args=("one", "ONE"))
            second = threading.Thread(target=run, args=("two", "TWO"))
            first.start()
            time.sleep(0.01)
            second.start()
            first.join(10)
            second.join(10)

        self.assertEqual(failures, [])
        self.assertEqual(path.read_text(), "ONE\nTWO\n")

    def test_apply_patch_locks_every_path_it_names(self):
        self.seed("b.txt", "one\n")
        targets = self.patcher._targets(
            envelope("*** Update File: b.txt", "*** Move to: a.txt",
                     "@@", "-one", "+two"),
            self.ctx)
        # Both ends of the move, and in a stable order so two patches sharing
        # files can never take their locks in opposite orders and deadlock.
        self.assertEqual([p.name for p in targets], ["a.txt", "b.txt"])
        self.assertEqual([str(p) for p in targets],
                         sorted(str(p) for p in targets))

    def test_an_unparseable_patch_asks_for_no_locks(self):
        self.assertEqual(self.patcher._targets("not a patch at all", self.ctx), [])


# --- LSP diagnostics ---------------------------------------------------

class FakeLSP:
    def __init__(self, text="", delay=0.0, boom=False):
        self.text = text
        self.delay = delay
        self.boom = boom
        self.calls = []

    def diagnostics(self, path, wait=2.0):
        return []

    def report(self, path, wait=2.0):
        self.calls.append((str(path), wait))
        if self.delay:
            time.sleep(self.delay)
        if self.boom:
            raise RuntimeError("language server exploded")
        return self.text


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-diag-")
        self.ctx = ToolContext(cwd=self.dir,
                               permissions=Permissions(auto_approve=True))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_absent_lsp_is_a_silent_no_op(self):
        self.assertEqual(diagnostics_block(self.ctx, "x.py"), "")
        self.assertEqual(append_diagnostics(self.ctx, "x.py", "done"), "done")

    def test_disabled_lsp_is_a_silent_no_op(self):
        self.ctx.lsp = False
        self.assertEqual(append_diagnostics(self.ctx, "x.py", "done"), "done")

    def test_block_is_appended_to_edit_output(self):
        path = Path(self.dir) / "a.py"
        path.write_text("x = 1\n")
        self.ctx.read_files.add(str(path.resolve()))
        self.ctx.lsp = FakeLSP("a.py:1:1 error: bad")

        result = files_module.EditTool().execute(
            {"filePath": "a.py", "oldString": "1", "newString": "2"}, self.ctx)
        self.assertIn("LSP errors detected in a.py, please fix:", result.output)
        self.assertIn("error: bad", result.output)
        self.assertEqual(path.read_text(), "x = 2\n")

    def test_block_is_appended_to_write_output(self):
        self.ctx.lsp = FakeLSP("b.py:2:3 warning: hm")
        result = files_module.WriteTool().execute(
            {"filePath": "b.py", "content": "y = 1\n"}, self.ctx)
        self.assertIn("LSP errors detected in b.py", result.output)

    def test_clean_file_adds_nothing(self):
        self.ctx.lsp = FakeLSP("")
        result = files_module.WriteTool().execute(
            {"filePath": "c.py", "content": "z = 1\n"}, self.ctx)
        self.assertNotIn("LSP", result.output)

    def test_a_broken_server_never_fails_the_edit(self):
        self.ctx.lsp = FakeLSP(boom=True)
        result = files_module.WriteTool().execute(
            {"filePath": "d.py", "content": "q = 1\n"}, self.ctx)
        self.assertIn("Created d.py", result.output)
        self.assertNotIn("LSP", result.output)

    def test_a_wedged_server_cannot_block_past_the_budget(self):
        self.ctx.lsp = FakeLSP("never arrives", delay=30)
        started = time.monotonic()
        result = files_module.WriteTool().execute(
            {"filePath": "e.py", "content": "w = 1\n"}, self.ctx)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "diagnostics blocked the edit")
        self.assertNotIn("LSP", result.output)
        self.assertIn("Created e.py", result.output)

    def test_apply_patch_reports_diagnostics(self):
        path = Path(self.dir) / "a.py"
        path.write_text("x = 1\n")
        self.ctx.read_files.add(str(path.resolve()))
        self.ctx.lsp = FakeLSP("a.py:1:1 error: nope")
        result = ApplyPatchTool().execute(
            {"patchText": envelope("*** Update File: a.py", "@@",
                                   "-x = 1", "+x = 2")}, self.ctx)
        self.assertIn("LSP errors detected in a.py", result.output)


# --- question ----------------------------------------------------------

QUESTIONS = [{
    "question": "Which database should I use?",
    "header": "Database",
    "options": [{"label": "SQLite (Recommended)", "description": "No server"},
                {"label": "Postgres", "description": "Needs a server"}],
}]


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-question-")
        self.tool = QuestionTool()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def context(self, asker=None, **kwargs):
        return ToolContext(cwd=self.dir,
                           permissions=Permissions(asker=asker, **kwargs))

    def test_an_aware_front_end_supplies_the_answer(self):
        def asker(request):
            self.assertEqual(request.key, "question")
            self.assertEqual(request.metadata["questions"][0]["header"], "Database")
            request.metadata["answers"] = [["SQLite (Recommended)"]]
            return "once"

        result = self.tool.execute({"questions": QUESTIONS}, self.context(asker))
        self.assertIn("SQLite (Recommended)", result.output)
        self.assertIn("User has answered", result.output)
        self.assertEqual(result.metadata["answers"], [["SQLite (Recommended)"]])
        self.assertEqual(result.metadata["answered"], 1)

    def test_a_bare_string_answer_is_accepted(self):
        def asker(request):
            request.metadata["answers"] = "Postgres"
            return "once"

        result = self.tool.execute({"questions": QUESTIONS}, self.context(asker))
        self.assertEqual(result.metadata["answers"], [["Postgres"]])

    def test_a_flat_list_answers_a_single_question(self):
        def asker(request):
            request.metadata["answers"] = ["SQLite (Recommended)", "Postgres"]
            return "once"

        result = self.tool.execute({"questions": QUESTIONS}, self.context(asker))
        self.assertEqual(result.metadata["answers"],
                         [["SQLite (Recommended)", "Postgres"]])

    def test_an_unaware_front_end_degrades_to_unanswered(self):
        """A plain permission prompt approves and writes nothing back."""
        result = self.tool.execute({"questions": QUESTIONS},
                                   self.context(lambda request: "once"))
        self.assertIn("Unanswered", result.output)
        self.assertIn("did not answer", result.output)
        self.assertEqual(result.metadata["answers"], [[]])

    def test_a_headless_run_does_not_raise(self):
        result = self.tool.execute({"questions": QUESTIONS}, self.context(None))
        self.assertIn("did not answer", result.output)
        self.assertTrue(result.metadata["dismissed"])

    def test_a_dismissed_question_does_not_raise(self):
        result = self.tool.execute({"questions": QUESTIONS},
                                   self.context(lambda request: "reject"))
        self.assertIn("did not answer", result.output)
        self.assertTrue(result.metadata["dismissed"])

    def test_the_call_never_blocks(self):
        """No condition variable, no deferred, no way for a UI to wedge us."""
        started = time.monotonic()
        self.tool.execute({"questions": QUESTIONS}, self.context(None))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_multiple_questions_are_paired_with_their_answers(self):
        questions = [
            dict(QUESTIONS[0]),
            {"question": "Run migrations now?", "header": "Migrations",
             "options": [{"label": "Yes", "description": ""},
                         {"label": "No", "description": ""}]},
        ]

        def asker(request):
            request.metadata["answers"] = [["Postgres"], []]
            return "once"

        result = self.tool.execute({"questions": questions}, self.context(asker))
        self.assertEqual(result.metadata["answers"], [["Postgres"], []])
        self.assertIn('"Which database should I use?"="Postgres"', result.output)
        self.assertIn('"Run migrations now?"="Unanswered"', result.output)
        self.assertEqual(result.title, "Asked 2 questions")

    def test_empty_questions_rejected(self):
        with self.assertRaises(ValueError):
            self.tool.execute({"questions": []}, self.context(None))

    def test_schema_is_a_valid_object_schema(self):
        params = self.tool.parameters
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["properties"]["questions"]["type"], "array")
        self.assertTrue(self.tool.description)


if __name__ == "__main__":
    unittest.main()
