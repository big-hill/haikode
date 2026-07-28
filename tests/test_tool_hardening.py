"""
What the tools do when the input is hostile rather than merely wrong.

Every test here corresponds to a way a careless or prompt-injected model can
hurt the user: reading a device node that never returns, walking a tree until
the UI freezes, borrowing an approval granted to a harmless command, or asking
webfetch to go fetch something off the user's own loopback interface.
"""

import http.server
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.permission import Permissions  # noqa: E402
from haikode.schema import PermissionDenied, ToolAborted  # noqa: E402
from haikode.tool import misc as misc_module  # noqa: E402
from haikode.tool import search as search_module  # noqa: E402
from haikode.tool import shell as shell_module  # noqa: E402
from haikode.tool.base import ToolContext  # noqa: E402
from haikode.tool.files import ReadTool  # noqa: E402
from haikode.tool.misc import (WebFetchBlocked, WebFetchTool,  # noqa: E402
                               _blocked_reason, _build_opener)
from haikode.tool.search import (Budget, GlobTool, GitIgnore,  # noqa: E402
                                 GrepTool, ListTool)
from haikode.tool.shell import (BashTool, _BoundedSink, _canonical,  # noqa: E402
                                _candidates, _permission_patterns,
                                _permission_target, _scan)

HAS_FIFO = hasattr(os, "mkfifo")


def run_with_watchdog(function, seconds=8.0):
    """
    Call `function` on a daemon thread and fail if it does not come back.

    A plain call would wedge the whole test run: open() on a FIFO with no
    writer never returns, which is exactly the bug being guarded against.
    """
    box = {}

    def target():
        try:
            box["value"] = function()
        except BaseException as error:      # noqa: BLE001 - reported below
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return thread.is_alive(), box


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-hard-")
        self.outside = tempfile.mkdtemp(prefix="haikode-out-")
        self.ctx = ToolContext(cwd=self.dir,
                               permissions=Permissions(auto_approve=True))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def write(self, name, content):
        path = Path(self.dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path.resolve()


# --- read --------------------------------------------------------------

class ReadHardeningTests(ToolCase):
    @unittest.skipUnless(HAS_FIFO, "platform has no mkfifo")
    def test_a_fifo_is_refused_instead_of_blocking(self):
        os.mkfifo(str(Path(self.dir) / "pipe"))
        stuck, box = run_with_watchdog(
            lambda: ReadTool().execute({"filePath": "pipe"}, self.ctx))
        self.assertFalse(stuck, "read() on a FIFO blocked forever")
        self.assertIsInstance(box.get("error"), ValueError)
        self.assertIn("named pipe", str(box["error"]))

    def test_a_device_node_is_refused(self):
        if not Path("/dev/zero").exists():
            self.skipTest("no /dev/zero on this platform")
            return
        ctx = ToolContext(cwd="/dev", permissions=Permissions(auto_approve=True))
        stuck, box = run_with_watchdog(
            lambda: ReadTool().execute({"filePath": "/dev/zero"}, ctx))
        self.assertFalse(stuck, "read() on /dev/zero never returned")
        self.assertIsInstance(box.get("error"), ValueError)

    def test_byte_budget_caps_a_file_with_many_short_lines(self):
        # 4000 lines x ~100 bytes is well under the 2000-line limit's reach but
        # far past the byte budget: a line cap alone would let this through.
        self.write("big.txt", "".join("x" * 100 + "\n" for _ in range(4000)))
        result = ReadTool().execute({"filePath": "big.txt"}, self.ctx)
        self.assertLess(len(result.output), 80_000)
        self.assertIn("stopped at", result.output)
        self.assertIn("call read again with offset=", result.output)
        self.assertTrue(result.metadata["truncated"])
        self.assertLess(result.metadata["lineEnd"], 4000)

    def test_line_budget_still_reports_the_real_total(self):
        self.write("b.txt", "\n".join(str(i) for i in range(1, 51)))
        result = ReadTool().execute({"filePath": "b.txt", "limit": 10}, self.ctx)
        self.assertIn("of 50", result.output)
        self.assertIn("offset=11", result.output)

    def test_a_huge_file_is_not_slurped_into_memory(self):
        path = Path(self.dir) / "huge.txt"
        with open(path, "w") as handle:
            for _ in range(20000):
                handle.write("y" * 200 + "\n")
        started = time.monotonic()
        result = ReadTool().execute({"filePath": "huge.txt"}, self.ctx)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertLess(len(result.output), 80_000)

    def test_reading_outside_the_working_directory_asks_first(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        seen = []
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(
            asker=lambda request: seen.append(request) or "once"))

        result = ReadTool().execute({"filePath": str(victim)}, ctx)
        self.assertIn("api-key", result.output)
        self.assertEqual([request.key for request in seen], ["external_directory"])
        self.assertIn(self.outside, seen[0].patterns[0])

    def test_reading_outside_the_working_directory_can_be_refused(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        ctx = ToolContext(cwd=self.dir,
                          permissions=Permissions(asker=lambda request: "reject"))
        with self.assertRaises(PermissionDenied):
            ReadTool().execute({"filePath": str(victim)}, ctx)

    def test_a_headless_run_cannot_read_outside_the_working_directory(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        ctx = ToolContext(cwd=self.dir, permissions=Permissions())
        with self.assertRaises(PermissionDenied):
            ReadTool().execute({"filePath": str(victim)}, ctx)

    def test_a_symlink_out_of_the_tree_is_treated_as_external(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        link = Path(self.dir) / "innocent.txt"
        try:
            link.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("no symlink support")
            return
        ctx = ToolContext(cwd=self.dir, permissions=Permissions())
        with self.assertRaises(PermissionDenied):
            ReadTool().execute({"filePath": "innocent.txt"}, ctx)

    def test_reading_inside_the_working_directory_asks_nothing(self):
        self.write("fine.txt", "hello\n")
        seen = []
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(
            asker=lambda request: seen.append(request) or "once"))
        ReadTool().execute({"filePath": "fine.txt"}, ctx)
        self.assertEqual(seen, [])


# --- grep / glob -------------------------------------------------------

class SearchHardeningTests(ToolCase):
    @unittest.skipUnless(HAS_FIFO, "platform has no mkfifo")
    def test_a_fifo_in_the_tree_does_not_stall_grep(self):
        self.write("real.py", "needle\n")
        os.mkfifo(str(Path(self.dir) / "pipe"))
        stuck, box = run_with_watchdog(
            lambda: GrepTool().execute({"pattern": "needle"}, self.ctx))
        self.assertFalse(stuck, "grep blocked on a FIFO")
        self.assertIsNone(box.get("error"))
        self.assertIn("real.py", box["value"].output)

    def test_wall_clock_budget_stops_grep(self):
        for index in range(50):
            self.write(f"f{index}.txt", "needle\n")
        result = GrepTool().execute(
            {"pattern": "needle", "timeout": 0.000001}, self.ctx)
        self.assertTrue(result.metadata["timedOut"])
        self.assertIn("search stopped after", result.output)

    def test_wall_clock_budget_stops_glob(self):
        for index in range(50):
            self.write(f"f{index}.py", "x\n")
        result = GlobTool().execute(
            {"pattern": "*.py", "timeout": 0.000001}, self.ctx)
        self.assertTrue(result.metadata["timedOut"])

    def test_a_caller_cannot_lengthen_the_budget(self):
        self.assertEqual(search_module._budget_seconds({"timeout": 9999}),
                         search_module.MAX_SEARCH_SECONDS)
        self.assertEqual(search_module._budget_seconds({}),
                         search_module.MAX_SEARCH_SECONDS)
        self.assertEqual(search_module._budget_seconds({"timeout": 1.5}), 1.5)

    def test_budget_expires(self):
        budget = Budget(0.0)
        self.assertTrue(budget.check())
        self.assertTrue(budget.expired)
        self.assertFalse(Budget(30).check())

    def test_grep_honours_gitignore(self):
        self.write(".gitignore", "secret.txt\n*.log\n!keep.log\nbuild/\n")
        self.write("secret.txt", "needle\n")
        self.write("noisy.log", "needle\n")
        self.write("keep.log", "needle\n")
        self.write("build/out.txt", "needle\n")
        self.write("real.py", "needle\n")

        result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        self.assertIn("real.py", result.output)
        self.assertIn("keep.log", result.output)
        self.assertNotIn("secret.txt", result.output)
        self.assertNotIn("noisy.log", result.output)
        self.assertNotIn("out.txt", result.output)

    def test_glob_honours_gitignore(self):
        self.write(".gitignore", "vendor/\n")
        self.write("vendor/lib.py", "x\n")
        self.write("app/main.py", "x\n")
        result = GlobTool().execute({"pattern": "**/*.py"}, self.ctx)
        self.assertIn("main.py", result.output)
        self.assertNotIn("lib.py", result.output)

    def test_nested_gitignore_is_scoped_to_its_directory(self):
        self.write(".gitignore", "# nothing here\n")
        self.write("pkg/.gitignore", "local.txt\n")
        self.write("pkg/local.txt", "needle\n")
        self.write("other/local.txt", "needle\n")
        result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        self.assertIn("other", result.output)
        self.assertNotIn("pkg/local.txt", result.output)

    def test_gitignore_comments_blanks_and_anchors(self):
        ignore = GitIgnore(Path(self.dir))
        ignore.add_text("# comment\n\n/root-only.txt\nany.txt\ndocs/\n")
        self.assertTrue(ignore.ignored("root-only.txt", False))
        self.assertFalse(ignore.ignored("sub/root-only.txt", False))
        self.assertTrue(ignore.ignored("any.txt", False))
        self.assertTrue(ignore.ignored("sub/any.txt", False))
        self.assertTrue(ignore.ignored("docs", True))
        self.assertTrue(ignore.ignored("docs/x.md", False))
        self.assertFalse(ignore.ignored("docs", False))

    def test_a_tree_without_gitignore_still_works(self):
        self.write("a.py", "needle\n")
        result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        self.assertIn("a.py", result.output)


class SearchContainmentTests(ToolCase):
    """
    glob/grep/list used to resolve any root the model named and ask only about
    the *pattern*, all three defaulting to allow — a one-call read of the whole
    disk. The root is a directory access and gets its own question.
    """

    def asker_context(self, answer="once"):
        seen = []
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(
            asker=lambda request: seen.append(request) or answer))
        return ctx, seen

    def test_glob_asks_before_searching_outside_the_working_directory(self):
        Path(self.outside, "secret.py").write_text("x\n")
        ctx, seen = self.asker_context()
        GlobTool().execute({"pattern": "*.py", "path": self.outside}, ctx)
        self.assertEqual([request.key for request in seen],
                         ["external_directory"])
        self.assertIn(self.outside, seen[0].patterns[0])

    def test_grep_asks_before_searching_outside_the_working_directory(self):
        Path(self.outside, "secret.txt").write_text("api-key\n")
        ctx, seen = self.asker_context()
        GrepTool().execute({"pattern": "api-key", "path": self.outside}, ctx)
        self.assertEqual([request.key for request in seen],
                         ["external_directory"])

    def test_list_asks_before_listing_outside_the_working_directory(self):
        ctx, seen = self.asker_context()
        ListTool().execute({"path": self.outside}, ctx)
        self.assertEqual([request.key for request in seen],
                         ["external_directory"])

    def test_a_headless_run_cannot_search_outside_the_working_directory(self):
        Path(self.outside, "secret.txt").write_text("api-key\n")
        for tool, args in ((GlobTool(), {"pattern": "*", "path": self.outside}),
                           (GrepTool(), {"pattern": "api-key",
                                         "path": self.outside}),
                           (ListTool(), {"path": self.outside})):
            ctx = ToolContext(cwd=self.dir, permissions=Permissions())
            with self.assertRaises(PermissionDenied):
                tool.execute(args, ctx)

    def test_searching_inside_the_working_directory_asks_nothing(self):
        self.write("a.py", "needle\n")
        ctx, seen = self.asker_context()
        GrepTool().execute({"pattern": "needle"}, ctx)
        GlobTool().execute({"pattern": "*.py"}, ctx)
        ListTool().execute({}, ctx)
        self.assertEqual(seen, [])

    def test_grep_does_not_follow_a_symlink_out_of_the_tree(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        link = Path(self.dir) / "innocent.txt"
        try:
            link.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("no symlink support")
            return
        self.write("real.txt", "api-key\n")
        result = GrepTool().execute({"pattern": "api-key"}, self.ctx)
        self.assertIn("real.txt", result.output)
        self.assertNotIn("innocent.txt", result.output)
        self.assertEqual(result.metadata["skippedLinks"], 1)
        self.assertIn("skipped", result.output)

    def test_a_symlink_inside_the_tree_is_still_searched(self):
        self.write("real.txt", "api-key\n")
        link = Path(self.dir) / "alias.txt"
        try:
            link.symlink_to(Path(self.dir) / "real.txt")
        except (OSError, NotImplementedError):
            self.skipTest("no symlink support")
            return
        result = GrepTool().execute({"pattern": "api-key"}, self.ctx)
        self.assertIn("alias.txt", result.output)


class SearchCorrectnessTests(ToolCase):
    """Wrong answers and silently short ones — the other half of the audit."""

    def test_dot_directories_that_are_not_caches_are_searchable(self):
        self.write(".github/workflows/ci.yml", "needle\n")
        self.write(".git/config", "needle\n")
        result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        self.assertIn(".github", result.output)
        self.assertNotIn(".git/config", result.output)

    def test_a_build_directory_holding_real_source_is_searchable(self):
        for name in ("build/gen.py", "dist/app.py", "generated/model.py"):
            self.write(name, "needle\n")
        result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        for name in ("build", "dist", "generated"):
            self.assertIn(name, result.output)

    def test_a_gitignored_build_directory_is_still_skipped(self):
        self.write(".gitignore", "build/\n")
        self.write("build/gen.py", "needle\n")
        self.write("src/app.py", "needle\n")
        result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        self.assertIn("app.py", result.output)
        self.assertNotIn("gen.py", result.output)

    def test_a_root_file_matches_a_doublestar_pattern(self):
        self.write("a.py", "x\n")
        self.write("src/deep/b.py", "x\n")
        self.write("c.js", "x\n")
        result = GlobTool().execute({"pattern": "**/*.py"}, self.ctx)
        self.assertIn("a.py", result.output)
        self.assertIn("b.py", result.output)
        self.assertNotIn("c.js", result.output)

    def test_a_directory_scoped_glob_matches_by_path(self):
        self.write("src/a.py", "x\n")
        self.write("other/b.py", "x\n")
        result = GlobTool().execute({"pattern": "src/*.py"}, self.ctx)
        self.assertIn("src/a.py", result.output.replace(os.sep, "/"))
        self.assertNotIn("other", result.output)

    def test_grep_include_is_a_path_glob_not_a_basename_glob(self):
        self.write("src/a.py", "needle\n")
        self.write("other/b.py", "needle\n")
        result = GrepTool().execute(
            {"pattern": "needle", "include": "src/*.py"}, self.ctx)
        self.assertIn("src", result.output.replace(os.sep, "/"))
        self.assertNotIn("other", result.output)

    def test_grep_include_still_matches_a_bare_extension_at_any_depth(self):
        self.write("src/a.py", "needle\n")
        self.write("b.js", "needle\n")
        result = GrepTool().execute(
            {"pattern": "needle", "include": "*.py"}, self.ctx)
        self.assertIn("a.py", result.output)
        self.assertNotIn("b.js", result.output)

    def test_hitting_the_file_cap_says_so(self):
        for index in range(6):
            self.write(f"f{index}.txt", "needle\n")
        real = search_module.MAX_FILES_SCANNED
        search_module.MAX_FILES_SCANNED = 2
        try:
            result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        finally:
            search_module.MAX_FILES_SCANNED = real
        self.assertTrue(result.metadata["fileCap"])
        self.assertIn("results are incomplete", result.output)

    def test_a_capped_walk_spends_its_budget_broadly_not_down_one_tree(self):
        """
        Field bug: a glob from the home directory reported "No files found"
        while also admitting it stopped after 20000 files. Depth first, the
        cap was consumed inside one deep tree before the shallow match was
        ever reached; the model then repeated the identical search.
        """
        self.write("wanted.txt", "needle\n")
        deep = "noise"
        for level in range(12):
            deep = os.path.join(deep, "level%d" % level)
            self.write(os.path.join(deep, "f.txt"), "needle\n")

        real = search_module.MAX_FILES_SCANNED
        search_module.MAX_FILES_SCANNED = 3
        try:
            result = GrepTool().execute({"pattern": "needle"}, self.ctx)
        finally:
            search_module.MAX_FILES_SCANNED = real

        self.assertIn("wanted.txt", result.output)

    def test_the_deadline_is_checked_before_the_regex_runs(self):
        """
        `re` has no timeout and holds the GIL: once a catastrophic pattern
        starts backtracking, a deadline checked *afterwards* never gets a turn.
        """
        self.write("bomb.txt", "a" * 26 + "\n")

        class OneShotBudget:
            """Alive for the walk, expired by the time grep reads a line."""

            def __init__(self, seconds=1.0):
                self.seconds = seconds
                self.expired = False
                self.calls = 0

            def check(self):
                self.calls += 1
                if self.calls > 1:
                    self.expired = True
                return self.expired

        real = search_module.Budget
        search_module.Budget = OneShotBudget
        try:
            stuck, box = run_with_watchdog(
                lambda: GrepTool().execute({"pattern": r"(a+)+b"}, self.ctx),
                seconds=4.0)
        finally:
            search_module.Budget = real
        self.assertFalse(stuck, "grep ran a catastrophic regex before the "
                                "deadline check")
        self.assertIsNone(box.get("error"))

    def test_a_very_long_line_is_not_handed_to_the_regex_whole(self):
        self.write("wide.txt", "b" * (search_module.MAX_LINE_CHARS + 500) + "\n")
        result = GrepTool().execute({"pattern": "b"}, self.ctx)
        self.assertEqual(result.metadata["longLines"], 1)
        self.assertIn("longer than", result.output)


# --- bash --------------------------------------------------------------

class BashPermissionPatternTests(unittest.TestCase):
    def test_simple_commands_widen_to_the_program(self):
        self.assertEqual(_permission_patterns("echo hi"), ["echo", "echo *"])
        self.assertEqual(_permission_patterns("git status -s"),
                         ["git status", "git status *"])

    def test_leading_and_inner_whitespace_is_canonicalised(self):
        self.assertEqual(_canonical("   echo    hi   "), "echo hi")
        self.assertEqual(_permission_target("   echo    hi   "), "echo hi")

    def test_newlines_are_never_folded_into_spaces(self):
        target = _permission_target("echo hi\nrm -rf /")
        self.assertIn("\n", target)
        self.assertTrue(target.startswith("shell: "))

    def test_chains_get_an_exact_grant_only(self):
        for command in ("echo hi; rm -rf /", "echo a && rm -rf /",
                        "echo a | sh", "echo `id`", "echo $(id)",
                        "cat </etc/passwd", "echo hi > /etc/passwd"):
            patterns = _permission_patterns(command)
            self.assertEqual(len(patterns), 1, command)
            self.assertNotIn("echo *", patterns, command)

    def test_assignments_are_not_program_names(self):
        self.assertEqual(_permission_patterns("FOO=1 rm -rf /"),
                         ["FOO=1 rm -rf /"])

    def test_runner_programs_never_widen(self):
        for command in ("env rm -rf /", "sudo rm -rf /", "xargs rm",
                        "sh -c whatever", "python3 -c pass", "make install",
                        "git submodule foreach rm"):
            self.assertEqual(len(_permission_patterns(command)), 1, command)

    def test_glob_metacharacters_in_a_command_are_escaped(self):
        import fnmatch
        command = "rm -rf /tmp/*.txt"
        pattern = _permission_patterns(command)[0]
        target = _permission_target(command)
        self.assertTrue(fnmatch.fnmatch(target, pattern))
        self.assertFalse(fnmatch.fnmatch("shell: rm -rf /etc/passwd", pattern))

    def test_an_approved_prefix_cannot_match_a_chained_command(self):
        import fnmatch
        for command in ("echo hi; rm -rf /", "echo hi && rm -rf /",
                        "echo hi | sh", "echo hi\nrm -rf /"):
            self.assertFalse(
                fnmatch.fnmatch(_permission_target(command), "echo *"), command)
            self.assertFalse(
                fnmatch.fnmatch(_permission_target(command), "echo"), command)


class BashPermissionTests(ToolCase):
    def asker_context(self, answer="always"):
        seen = []
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(
            asker=lambda request: seen.append(request.metadata.get("command"))
            or answer))
        return ctx, seen

    def test_an_always_grant_on_echo_does_not_cover_a_chain(self):
        ctx, seen = self.asker_context()
        BashTool().execute({"command": "echo one"}, ctx)
        BashTool().execute({"command": "echo two"}, ctx)          # covered
        BashTool().execute({"command": "echo three; true"}, ctx)  # must re-ask
        self.assertEqual(seen, ["echo one", "echo three; true"])

    def test_an_always_grant_survives_whitespace_variants(self):
        ctx, seen = self.asker_context()
        BashTool().execute({"command": "echo one"}, ctx)
        BashTool().execute({"command": "   echo    two   "}, ctx)
        self.assertEqual(seen, ["echo one"])

    def test_an_always_grant_on_rm_does_not_cover_an_assignment_prefix(self):
        ctx, seen = self.asker_context()
        BashTool().execute({"command": "rm -f nothing-here"}, ctx)
        BashTool().execute({"command": "FOO=1 rm -f nothing-here"}, ctx)
        self.assertEqual(len(seen), 2, "assignment prefix reused an rm grant")

    def test_a_deny_rule_cannot_be_dodged_with_whitespace(self):
        # Catch-all first: rules are evaluated last-wins, so `rm *` has to come
        # after `*` to survive it.
        class FakeConfig:
            data = {"permission": {"bash": {"*": "allow", "rm *": "deny"}}}

            def save(self):
                pass

        # An in-tree target, so the refusal can only come from the rule and not
        # from the working-directory guard.
        self.write("doomed.txt", "x\n")
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(config=FakeConfig()))
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": "rm -rf doomed.txt"}, ctx)
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": "   rm    -rf   doomed.txt"}, ctx)
        self.assertTrue((Path(self.dir) / "doomed.txt").exists())

    def test_an_allow_rule_for_echo_does_not_allow_a_chain(self):
        class FakeConfig:
            data = {"permission": {"bash": {"*": "deny", "echo *": "allow"}}}

            def save(self):
                pass

        ctx = ToolContext(cwd=self.dir, permissions=Permissions(config=FakeConfig()))
        result = BashTool().execute({"command": "echo hi"}, ctx)
        self.assertIn("hi", result.output)
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": "echo hi; id"}, ctx)


class BashPathScanTests(unittest.TestCase):
    """The conservative reader that decides which files a command names."""

    def scan(self, command):
        commands, certain = _scan(command)
        return [_candidates(item) for item in commands], certain

    def test_arguments_of_file_commands_are_paths(self):
        self.assertEqual(self.scan("cat /etc/hosts"), ([["/etc/hosts"]], True))
        self.assertEqual(self.scan("rm -rf /"), ([["/"]], True))
        self.assertEqual(self.scan("cat passwd"), ([["passwd"]], True))

    def test_an_assignment_prefix_is_not_the_program(self):
        self.assertEqual(self.scan("FOO=1 rm -rf /"), ([["/"]], True))

    def test_redirection_targets_are_paths_and_descriptors_are_not(self):
        self.assertEqual(self.scan("echo hi > /etc/x"), ([["/etc/x"]], True))
        self.assertEqual(self.scan("cat < /etc/passwd"),
                         ([["/etc/passwd"]], True))
        candidates, _ = self.scan("make 2>&1 | tail")
        self.assertEqual(candidates, [[], []])

    def test_every_command_in_a_pipeline_is_read(self):
        candidates, _ = self.scan("cat a.txt | tee /etc/evil")
        self.assertEqual(candidates, [["a.txt"], ["/etc/evil"]])

    def test_a_glob_is_reduced_to_the_directory_it_names(self):
        candidates, _ = self.scan("rm -rf /etc/*")
        self.assertEqual(candidates, [["/etc/*"]])
        self.assertEqual(shell_module._resolve_arg("/etc/*", "/tmp"),
                         Path("/etc").resolve())

    def test_a_bare_glob_names_no_directory(self):
        self.assertIsNone(shell_module._resolve_arg("*.txt", "/tmp"))

    def test_substitution_and_variables_make_the_parse_uncertain(self):
        for command in ("echo $(id)", "echo `id`", "cat $FILE",
                        "cat ${FILE}", "cat 'unterminated",
                        "cat <<EOF\nbody\nEOF"):
            self.assertFalse(self.scan(command)[1], command)

    def test_an_uncertain_parse_never_widens_a_grant(self):
        for command in ("cat $FILE", "echo $(id)", "cat 'unterminated"):
            self.assertEqual(len(_permission_patterns(command)), 1, command)


class BashContainmentTests(ToolCase):
    """
    Defect 1: an "always" grant is a grant for a command *shape*. Approving
    `cat README.md` widens to `cat *`, so containment cannot live in that
    grant — every path the command reaches gets its own question.
    """

    def asker_context(self, answer="always"):
        seen = []
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(
            asker=lambda request: seen.append(request) or answer))
        return ctx, seen

    def bash_granted_context(self):
        """
        Approve the command, refuse the directory.

        Denying everything would prove nothing: the bash question alone
        refuses a headless run. This says yes to the command shape — which is
        what "always allow `cat`" gives an attacker — and no to the directory,
        so the only thing that can raise is the containment check.
        """
        seen = []

        def asker(request):
            seen.append(request)
            return "always" if request.key == "bash" else "reject"

        return ToolContext(cwd=self.dir,
                           permissions=Permissions(asker=asker)), seen

    def test_an_always_grant_on_cat_does_not_cover_a_file_outside_the_tree(self):
        self.write("README.md", "hello\n")
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        ctx, seen = self.asker_context()

        BashTool().execute({"command": "cat README.md"}, ctx)
        self.assertEqual([request.key for request in seen], ["bash"])

        result = BashTool().execute({"command": f"cat {victim}"}, ctx)
        self.assertIn("api-key", result.output)
        self.assertEqual([request.key for request in seen],
                         ["bash", "external_directory"])
        self.assertIn(self.outside, seen[1].patterns[0])

    def test_an_external_file_is_refused_even_when_the_command_is_approved(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        ctx, seen = self.bash_granted_context()
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": f"cat {victim}"}, ctx)
        self.assertEqual([request.key for request in seen],
                         ["external_directory"])

    def test_an_external_workdir_is_asked_about(self):
        Path(self.outside, "passwd").write_text("root:x:0:0\n")
        ctx, seen = self.asker_context()
        BashTool().execute({"command": "cat passwd", "workdir": self.outside},
                           ctx)
        keys = [request.key for request in seen]
        self.assertEqual(keys[0], "external_directory")
        self.assertIn(self.outside, seen[0].patterns[0])

    def test_a_relative_escape_out_of_the_tree_is_asked_about(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        ctx, _seen = self.bash_granted_context()
        escape = os.path.relpath(str(victim), self.dir)
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": f"cat {escape}"}, ctx)

    def test_a_redirection_out_of_the_tree_is_asked_about(self):
        ctx, _seen = self.bash_granted_context()
        target = Path(self.outside) / "planted.txt"
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": f"echo pwned > {target}"}, ctx)
        self.assertFalse(target.exists())

    def test_a_command_inside_the_tree_asks_nothing_extra(self):
        self.write("README.md", "hello\n")
        ctx, seen = self.asker_context()
        BashTool().execute({"command": "cat README.md"}, ctx)
        BashTool().execute({"command": "cat README.md"}, ctx)
        self.assertEqual([request.key for request in seen], ["bash"])

    def test_dev_null_is_not_an_external_directory(self):
        ctx, seen = self.asker_context()
        BashTool().execute({"command": "echo hi 2>/dev/null"}, ctx)
        self.assertEqual([request.key for request in seen], ["bash"])

    def test_a_symlink_out_of_the_tree_is_asked_about(self):
        victim = Path(self.outside) / "secrets.txt"
        victim.write_text("api-key\n")
        link = Path(self.dir) / "innocent.txt"
        try:
            link.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("no symlink support")
            return
        ctx, _seen = self.bash_granted_context()
        with self.assertRaises(PermissionDenied):
            BashTool().execute({"command": "cat innocent.txt"}, ctx)

    def test_an_approved_command_still_cannot_read_the_users_api_keys(self):
        """
        The command grant is about *what runs*, not about what it may see.

        `echo` is the most harmless thing a user can approve, and it was
        enough to exfiltrate the key: the child inherited os.environ and the
        result went into the history, to the provider and into the session
        database. See tests/test_redact.py for the rest of that story.
        """
        key = "sk-proj-Zz99Yy88Xx77Ww66Vv55Uu44Tt33Ss22"
        previous = os.environ.get("OPENAI_API_KEY")

        def restore():
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous

        os.environ["OPENAI_API_KEY"] = key
        self.addCleanup(restore)
        ctx, _seen = self.asker_context()
        result = BashTool().execute({"command": "echo \"$OPENAI_API_KEY\""},
                                    ctx)
        self.assertNotIn(key, result.output)


class BashStreamingTests(ToolCase):
    """Defect 5: unbounded buffering and an abort nobody was listening for."""

    def pidfile_command(self, seconds=30):
        pidfile = Path(self.dir) / "child.pid"
        return pidfile, f"echo $$ > {pidfile}; sleep {seconds}"

    def run_in_thread(self, args, ctx):
        box = {}

        def target():
            try:
                box["value"] = BashTool().execute(args, ctx)
            except BaseException as error:      # noqa: BLE001 - reported below
                box["error"] = error

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, box

    def test_an_abort_flag_stops_a_running_command(self):
        ctx = ToolContext(cwd=self.dir,
                          permissions=Permissions(auto_approve=True))
        pidfile, command = self.pidfile_command()
        thread, box = self.run_in_thread({"command": command, "timeout": 60},
                                         ctx)
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        ctx.aborted = True
        thread.join(6)

        self.assertFalse(thread.is_alive(), "bash never noticed the abort")
        self.assertIsInstance(box.get("error"), ToolAborted)

        pid = int(pidfile.read_text().strip())
        gone = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                gone = True
                break
            time.sleep(0.1)
        self.assertTrue(gone, f"pid {pid} survived the abort")

    def test_an_abort_event_on_the_context_is_honoured(self):
        """The Event is what makes the abort immediate rather than polled."""
        ctx = ToolContext(cwd=self.dir,
                          permissions=Permissions(auto_approve=True))
        _pidfile, command = self.pidfile_command()
        thread, box = self.run_in_thread({"command": command, "timeout": 60},
                                         ctx)
        time.sleep(0.5)
        ctx.abort_event.set()
        thread.join(6)
        self.assertFalse(thread.is_alive(), "bash ignored ctx.abort_event")
        self.assertIsInstance(box.get("error"), ToolAborted)

    def test_output_is_not_buffered_whole_before_truncation(self):
        emitter = self.write(
            "emit.py",
            "import sys\n"
            "for _ in range(16384):\n"
            "    sys.stdout.write('x' * 1024 + '\\n')\n")
        command = f"{sys.executable} {emitter}"

        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            result = BashTool().execute({"command": command, "timeout": 120},
                                        self.ctx)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(result.metadata["exit"], 0)
        self.assertLessEqual(len(result.output), shell_module.MAX_OUTPUT + 500)
        self.assertTrue(result.metadata["truncated"])
        # The child prints ~16 MB. communicate() held all of it at once.
        self.assertLess(peak, 8_000_000,
                        f"peak traced memory was {peak} bytes for 16 MB of output")


class BoundedSinkTests(unittest.TestCase):
    def test_the_head_and_the_tail_survive_and_the_middle_is_counted(self):
        sink = _BoundedSink(100)
        for index in range(100):
            sink.add(f"{index:04d}")
        text = sink.text()
        self.assertTrue(text.startswith("0000"))
        self.assertTrue(text.endswith("0099"))
        self.assertIn("characters truncated", text)
        self.assertEqual(sink.total, 400)
        self.assertLessEqual(len(text.split("\n\n")[0]), 50)
        self.assertLessEqual(len(text.split("\n\n")[-1]), 50)

    def test_short_output_is_untouched(self):
        sink = _BoundedSink(100)
        sink.add("hello ")
        sink.add("world")
        self.assertEqual(sink.text(), "hello world")
        self.assertEqual(sink.dropped, 0)

    def test_one_oversized_chunk_is_bounded(self):
        sink = _BoundedSink(100)
        sink.add("z" * 10000)
        self.assertLessEqual(len(sink.text()), 140)
        self.assertEqual(sink.total, 10000)


class BashProcessGroupTests(ToolCase):
    def test_a_timeout_kills_orphaned_background_children(self):
        pidfile = Path(self.dir) / "child.pid"
        command = f"sleep 30 & echo $! > {pidfile}; sleep 30"
        result = BashTool().execute({"command": command, "timeout": 1}, self.ctx)
        self.assertTrue(result.metadata.get("timeout"))

        raw = pidfile.read_text().strip()
        self.assertTrue(raw.isdigit(), f"background pid not captured: {raw!r}")
        pid = int(raw)

        deadline = time.monotonic() + 5
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                alive = False
                break
            time.sleep(0.1)
        self.assertFalse(alive, f"pid {pid} survived the timeout as an orphan")

    def test_a_timed_out_command_still_returns_its_partial_output(self):
        result = BashTool().execute(
            {"command": "echo partial; sleep 30", "timeout": 1}, self.ctx)
        self.assertTrue(result.metadata.get("timeout"))
        self.assertIn("timed out", result.output)

    def test_the_child_runs_in_its_own_process_group(self):
        result = BashTool().execute(
            {"command": "echo $$; ps -o pgid= -p $$ 2>/dev/null || true"},
            self.ctx)
        self.assertEqual(result.metadata["exit"], 0)


# --- webfetch ----------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            target = f"http://127.0.0.1:{self.server.server_address[1]}/secret"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if self.path == "/secret":
            body = b"SECRET LOCAL DATA"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/huge-declared":
            body = b"z" * 600_000
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if self.path == "/endless":
            # No Content-Length: the connection close delimits the body, which
            # is exactly how a hostile server floods a naive client.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"q" * 65536)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                self.wfile.write(b"start")
                self.wfile.flush()
                time.sleep(6)
                self.wfile.write(b"end")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        self.send_response(404)
        self.end_headers()


class LocalServerCase(unittest.TestCase):
    """
    Plumbing tests need a server they can actually reach, and the only one
    available is on loopback — which the guard exists to refuse. Only the
    address pin is relaxed here; _assert_public_url stays strict, so the
    redirect tests still exercise the real refusal.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._real_pin = misc_module._pinned_address
        misc_module._pinned_address = lambda host: (
            host if host in ("127.0.0.1", "::1") else self._real_pin(host))

    def tearDown(self):
        misc_module._pinned_address = self._real_pin


class WebFetchGuardTests(unittest.TestCase):
    def test_loopback_and_private_addresses_are_refused(self):
        for url in ("http://127.0.0.1:8080/", "https://127.0.0.1/",
                    "http://[::1]:9000/", "http://10.0.0.5/",
                    "http://192.168.1.1/admin", "http://172.16.0.1/",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://0.0.0.0/"):
            self.assertIsNotNone(_blocked_reason(url), url)

    def test_localhost_by_name_is_refused(self):
        self.assertIsNotNone(_blocked_reason("http://localhost:8080/"))

    def test_public_literal_addresses_are_allowed(self):
        self.assertIsNone(_blocked_reason("https://93.184.216.34/"))
        self.assertIsNone(_blocked_reason("https://[2606:2800:220:1::1]/"))

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x",
                    "gopher://example.com/"):
            reason = _blocked_reason(url)
            self.assertIsNotNone(reason, url)
            self.assertIn("not allowed", reason)

    def test_an_unresolvable_host_is_refused(self):
        """
        It used to be waved through on the theory that the connect would fail
        anyway. It would not: urllib resolves again, and a resolver that
        answers the second time gets a free pass. Nothing can be pinned to an
        address we never saw.
        """
        reason = _blocked_reason("https://this-host-does-not-exist.invalid/")
        self.assertIsNotNone(reason)
        self.assertIn("does not resolve", reason)

    def test_the_tool_refuses_a_loopback_url_before_any_io(self):
        directory = tempfile.mkdtemp(prefix="haikode-fetch-")
        try:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            with self.assertRaises(WebFetchBlocked):
                WebFetchTool().execute({"url": "http://127.0.0.1:9/"}, ctx)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_the_tool_refuses_a_hostname_that_resolves_to_loopback(self):
        """DNS pointing a public-looking name at 127.0.0.1 is the usual trick."""
        directory = tempfile.mkdtemp(prefix="haikode-fetch-")
        real = misc_module._resolve_addresses
        misc_module._resolve_addresses = lambda host: ["127.0.0.1"]
        try:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            with self.assertRaises(WebFetchBlocked) as cm:
                WebFetchTool().execute({"url": "https://totally-public.test/"}, ctx)
            self.assertIn("private or loopback", str(cm.exception))
        finally:
            misc_module._resolve_addresses = real
            shutil.rmtree(directory, ignore_errors=True)


class WebFetchRebindingTests(unittest.TestCase):
    """
    Defect 4: the host was screened with one DNS lookup and connected with
    another. Between the two, an attacker's short-TTL record flips from a
    public address to 127.0.0.1 and the guard has already said yes.
    """

    def test_the_socket_goes_to_the_address_that_was_screened(self):
        lookups = []
        attempted = []
        real_getaddrinfo = socket.getaddrinfo
        real_create = socket.create_connection

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host != "rebind.test":
                return real_getaddrinfo(host, port, *args, **kwargs)
            lookups.append(host)
            # First answer public, every answer after that loopback.
            address = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (address, port or 443))]

        def fake_create_connection(address, *args, **kwargs):
            attempted.append(address[0])
            raise OSError("connection refused by test")

        socket.getaddrinfo = fake_getaddrinfo
        socket.create_connection = fake_create_connection
        try:
            with self.assertRaises(RuntimeError):
                WebFetchTool._fetch("https://rebind.test/x", 5)
        finally:
            socket.getaddrinfo = real_getaddrinfo
            socket.create_connection = real_create

        # The old code handed urllib the *name* and let it resolve again.
        self.assertEqual(attempted, ["93.184.216.34"])

    def test_a_host_that_rebinds_to_loopback_is_refused_outright(self):
        lookups = []
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host != "rebind.test":
                return real_getaddrinfo(host, port, *args, **kwargs)
            lookups.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", port or 443))]

        socket.getaddrinfo = fake_getaddrinfo
        try:
            with self.assertRaises(WebFetchBlocked):
                WebFetchTool._fetch("https://rebind.test/x", 5)
        finally:
            socket.getaddrinfo = real_getaddrinfo


class WebFetchRedirectTests(LocalServerCase):
    def test_the_target_is_reachable_without_a_redirect(self):
        """Positive control: only the redirect is supposed to be blocked."""
        with _build_opener().open(self.base + "/secret", timeout=5) as response:
            self.assertIn(b"SECRET", response.read())

    def test_a_redirect_to_loopback_is_refused(self):
        with self.assertRaises(WebFetchBlocked) as cm:
            _build_opener().open(self.base + "/redirect", timeout=5)
        self.assertIn("127.0.0.1", str(cm.exception))

    def test_a_redirect_to_a_private_address_is_refused(self):
        import urllib.request
        handler = misc_module._GuardedRedirectHandler()
        request = urllib.request.Request("https://example.com/")
        for target in ("http://10.1.2.3/", "http://169.254.169.254/",
                       "file:///etc/passwd"):
            with self.assertRaises(WebFetchBlocked):
                handler.redirect_request(request, None, 302, "Found", {}, target)


class WebFetchBudgetTests(LocalServerCase):
    def test_a_declared_oversize_response_is_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            WebFetchTool._fetch(self.base + "/huge-declared", 10)
        self.assertIn("Refusing to fetch", str(cm.exception))

    def test_an_undeclared_flood_is_capped(self):
        raw, _, _, truncated = WebFetchTool._fetch(self.base + "/endless", 20)
        self.assertLessEqual(len(raw), misc_module.MAX_FETCH_BYTES)
        self.assertTrue(truncated)

    def test_a_slow_server_cannot_outlive_the_budget(self):
        started = time.monotonic()
        try:
            WebFetchTool._fetch(self.base + "/slow", 1)
        except RuntimeError:
            pass
        self.assertLess(time.monotonic() - started, 4.0,
                        "the total time budget was not enforced")

    def test_the_output_is_capped(self):
        directory = tempfile.mkdtemp(prefix="haikode-fetch-")
        real = misc_module._blocked_reason
        misc_module._blocked_reason = lambda url: None
        try:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            tool = WebFetchTool()
            url = self.base.replace("http://", "https://")
            fetched = {}

            def fake_fetch(target, budget):
                fetched["url"] = target
                return b"w" * 400_000, "utf-8", "text/plain", True

            tool._fetch = fake_fetch
            result = tool.execute({"url": url, "format": "text"}, ctx)
            self.assertLessEqual(len(result.output),
                                 misc_module.MAX_FETCH_OUTPUT + 200)
            self.assertTrue(result.metadata["truncated"])
        finally:
            misc_module._blocked_reason = real
            shutil.rmtree(directory, ignore_errors=True)


class WebFetchMiscTests(unittest.TestCase):
    def test_http_is_upgraded_to_https(self):
        directory = tempfile.mkdtemp(prefix="haikode-fetch-")
        try:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            with self.assertRaises(WebFetchBlocked) as cm:
                WebFetchTool().execute({"url": "http://127.0.0.1/"}, ctx)
            self.assertIn("https://127.0.0.1/", str(cm.exception))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_a_non_http_url_is_rejected(self):
        directory = tempfile.mkdtemp(prefix="haikode-fetch-")
        try:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            with self.assertRaises(ValueError):
                WebFetchTool().execute({"url": "file:///etc/passwd"}, ctx)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_ipv4_mapped_ipv6_loopback_is_refused(self):
        self.assertTrue(misc_module._private("::ffff:127.0.0.1"))
        self.assertFalse(misc_module._private("93.184.216.34"))

    def test_resolve_failures_are_swallowed(self):
        self.assertEqual(
            misc_module._resolve_addresses("no-such-host.invalid"), [])


if __name__ == "__main__":
    unittest.main()
