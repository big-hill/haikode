"""Tool behaviour: the guards and output shapes opencode relies on."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.permission import Permissions, PermissionRequest  # noqa: E402
from haikode.schema import PermissionDenied  # noqa: E402
from haikode.tool import REGISTRY, get_tools, tool_specs  # noqa: E402
from haikode.tool.base import ToolContext  # noqa: E402


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-tools-")
        self.ctx = ToolContext(cwd=self.dir,
                               permissions=Permissions(auto_approve=True))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, content):
        path = Path(self.dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        # resolve(): the tools key off normalised paths, and on macOS /var is a
        # symlink to /private/var, so tests must compare the same form.
        return path.resolve()

    def run_tool(self, name, args):
        return REGISTRY[name].execute(args, self.ctx)


class TestRead(ToolTestCase):
    def test_line_numbers_and_offset(self):
        self.write("a.txt", "one\ntwo\nthree\nfour\n")
        result = self.run_tool("read", {"filePath": "a.txt"})
        self.assertIn("1: one", result.output)
        self.assertIn("4: four", result.output)

        result = self.run_tool("read", {"filePath": "a.txt", "offset": 3})
        self.assertIn("3: three", result.output)
        self.assertNotIn("1: one", result.output)

    def test_limit_reports_remaining(self):
        self.write("b.txt", "\n".join(str(i) for i in range(1, 51)))
        result = self.run_tool("read", {"filePath": "b.txt", "limit": 10})
        self.assertIn("offset=11", result.output)

    def test_marks_file_as_read(self):
        path = self.write("c.txt", "x")
        self.run_tool("read", {"filePath": "c.txt"})
        self.assertIn(str(path), self.ctx.read_files)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.run_tool("read", {"filePath": "nope.txt"})

    def test_directory_listing(self):
        self.write("sub/x.txt", "x")
        result = self.run_tool("read", {"filePath": "sub"})
        self.assertIn("x.txt", result.output)


class TestEditAndWrite(ToolTestCase):
    def test_edit_requires_prior_read(self):
        self.write("a.py", "value = 1\n")
        with self.assertRaises(ValueError) as cm:
            self.run_tool("edit", {"filePath": "a.py", "oldString": "1",
                                   "newString": "2"})
        self.assertIn("has not been read", str(cm.exception))

    def test_edit_rejects_ambiguous_match(self):
        self.write("a.py", "x = 1\ny = 1\n")
        self.run_tool("read", {"filePath": "a.py"})
        with self.assertRaises(ValueError) as cm:
            self.run_tool("edit", {"filePath": "a.py", "oldString": "1",
                                   "newString": "2"})
        self.assertIn("2 matches", str(cm.exception))

    def test_edit_replace_all(self):
        path = self.write("a.py", "x = 1\ny = 1\n")
        self.run_tool("read", {"filePath": "a.py"})
        result = self.run_tool("edit", {"filePath": "a.py", "oldString": "1",
                                        "newString": "2", "replaceAll": True})
        self.assertEqual(path.read_text(), "x = 2\ny = 2\n")
        self.assertIn("diff", result.metadata)

    def test_edit_missing_old_string(self):
        self.write("a.py", "x = 1\n")
        self.run_tool("read", {"filePath": "a.py"})
        with self.assertRaises(ValueError) as cm:
            self.run_tool("edit", {"filePath": "a.py", "oldString": "zzz",
                                   "newString": "2"})
        self.assertIn("not found", str(cm.exception))

    def test_edit_identical_strings_rejected(self):
        self.write("a.py", "x\n")
        self.run_tool("read", {"filePath": "a.py"})
        with self.assertRaises(ValueError):
            self.run_tool("edit", {"filePath": "a.py", "oldString": "x",
                                   "newString": "x"})

    def test_write_new_file(self):
        result = self.run_tool("write", {"filePath": "new.txt", "content": "hello\n"})
        self.assertEqual((Path(self.dir) / "new.txt").read_text(), "hello\n")
        self.assertIn("Created", result.output)

    def test_write_existing_requires_read(self):
        self.write("a.txt", "old")
        with self.assertRaises(ValueError):
            self.run_tool("write", {"filePath": "a.txt", "content": "new"})

    def test_original_recorded_for_revert(self):
        self.write("a.txt", "before")
        self.run_tool("read", {"filePath": "a.txt"})
        self.run_tool("write", {"filePath": "a.txt", "content": "after"})
        key = str((Path(self.dir) / "a.txt").resolve())
        self.assertEqual(self.ctx.modified_files[key], "before")


class TestSearch(ToolTestCase):
    def test_grep_regex_and_include(self):
        self.write("a.py", "def alpha():\n    pass\n")
        self.write("b.js", "function alpha() {}\n")
        result = self.run_tool("grep", {"pattern": r"def\s+\w+"})
        self.assertIn("a.py", result.output)
        self.assertNotIn("b.js", result.output)

        result = self.run_tool("grep", {"pattern": "alpha", "include": "*.js"})
        self.assertIn("b.js", result.output)
        self.assertNotIn("a.py", result.output)

    def test_grep_brace_include(self):
        self.write("a.ts", "alpha\n")
        self.write("b.tsx", "alpha\n")
        self.write("c.py", "alpha\n")
        result = self.run_tool("grep", {"pattern": "alpha", "include": "*.{ts,tsx}"})
        self.assertIn("a.ts", result.output)
        self.assertIn("b.tsx", result.output)
        self.assertNotIn("c.py", result.output)

    def test_grep_invalid_regex(self):
        with self.assertRaises(ValueError):
            self.run_tool("grep", {"pattern": "([unclosed"})

    def test_grep_skips_ignored_dirs(self):
        self.write(".git/config", "needle\n")
        self.write("node_modules/x.js", "needle\n")
        self.write("real.py", "needle\n")
        result = self.run_tool("grep", {"pattern": "needle"})
        self.assertIn("real.py", result.output)
        self.assertNotIn("node_modules", result.output)
        self.assertNotIn(".git", result.output)

    def test_glob_matches_nested(self):
        self.write("src/deep/x.py", "x")
        self.write("src/y.js", "y")
        result = self.run_tool("glob", {"pattern": "**/*.py"})
        self.assertIn("x.py", result.output)
        self.assertNotIn("y.js", result.output)

    def test_list_marks_directories(self):
        self.write("sub/x.txt", "x")
        self.write("top.txt", "t")
        result = self.run_tool("list", {})
        self.assertIn("sub/", result.output)
        self.assertIn("top.txt", result.output)


class TestBash(ToolTestCase):
    def test_pipes_and_operators_work(self):
        """The old implementation blocked every metacharacter; this must not."""
        result = self.run_tool("bash", {"command": "printf 'a\\nb\\nc\\n' | grep b"})
        self.assertIn("b", result.output)
        self.assertEqual(result.metadata["exit"], 0)

    def test_chained_commands(self):
        result = self.run_tool("bash", {"command": "true && echo chained"})
        self.assertIn("chained", result.output)

    def test_nonzero_exit_reported(self):
        result = self.run_tool("bash", {"command": "exit 3"})
        self.assertEqual(result.metadata["exit"], 3)
        self.assertIn("exit code 3", result.output)

    def test_timeout(self):
        result = self.run_tool("bash", {"command": "sleep 5", "timeout": 1})
        self.assertTrue(result.metadata.get("timeout"))

    def test_workdir(self):
        os.mkdir(os.path.join(self.dir, "sub"))
        result = self.run_tool("bash", {"command": "pwd", "workdir": "sub"})
        self.assertIn("sub", result.output)


class TestTodo(ToolTestCase):
    def test_todos_stored_on_context(self):
        result = self.run_tool("todowrite", {"todos": [
            {"content": "first", "status": "in_progress", "priority": "high"},
            {"content": "second", "status": "completed", "priority": "low"},
        ]})
        self.assertEqual(len(self.ctx.todos), 2)
        self.assertIn("1 todo", result.title)

    def test_invalid_status_normalised(self):
        self.run_tool("todowrite", {"todos": [{"content": "x", "status": "bogus"}]})
        self.assertEqual(self.ctx.todos[0]["status"], "pending")


class TestPermissions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-perm-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reject_raises(self):
        perms = Permissions(asker=lambda request: "reject")
        ctx = ToolContext(cwd=self.dir, permissions=perms)
        with self.assertRaises(PermissionDenied):
            REGISTRY["write"].execute(
                {"filePath": "x.txt", "content": "hi"}, ctx)
        self.assertFalse((Path(self.dir) / "x.txt").exists())

    def test_always_remembered_for_session(self):
        calls = []

        def asker(request):
            calls.append(request.key)
            return "always"

        perms = Permissions(asker=asker)
        ctx = ToolContext(cwd=self.dir, permissions=perms)
        REGISTRY["write"].execute({"filePath": "a.txt", "content": "1"}, ctx)
        REGISTRY["write"].execute({"filePath": "b.txt", "content": "2"}, ctx)
        self.assertEqual(len(calls), 1, "second write should reuse the always grant")

    def test_bash_always_grant_is_scoped_to_the_command(self):
        """'always' on `echo` must not silently authorise `rm`."""
        seen = []

        def asker(request):
            seen.append(request.metadata.get("command"))
            return "always"

        ctx = ToolContext(cwd=self.dir, permissions=Permissions(asker=asker))
        REGISTRY["bash"].execute({"command": "echo one"}, ctx)
        REGISTRY["bash"].execute({"command": "echo two"}, ctx)
        REGISTRY["bash"].execute({"command": "true"}, ctx)
        self.assertEqual(seen, ["echo one", "true"])

    def test_read_allowed_by_default_without_asker(self):
        Path(self.dir, "r.txt").write_text("data")
        ctx = ToolContext(cwd=self.dir, permissions=Permissions())
        result = REGISTRY["read"].execute({"filePath": "r.txt"}, ctx)
        self.assertIn("data", result.output)

    def test_bash_denied_without_asker(self):
        ctx = ToolContext(cwd=self.dir, permissions=Permissions())
        with self.assertRaises(PermissionDenied):
            REGISTRY["bash"].execute({"command": "echo hi"}, ctx)

    def test_config_rule_allows(self):
        """Rules are evaluated in order and the LAST match wins.

        This test used to be written the other way round, back when the
        longest glob won and order was irrelevant. The catch-all now has to
        come first, or it swallows every rule after it — which is exactly the
        migration hazard the change introduced, and worth encoding in a test
        rather than only in a docstring.
        """
        class FakeConfig:
            data = {"permission": {"bash": {"*": "deny", "echo *": "allow"}}}

            def save(self):
                pass

        perms = Permissions(config=FakeConfig())
        ctx = ToolContext(cwd=self.dir, permissions=perms)
        result = REGISTRY["bash"].execute({"command": "echo hi"}, ctx)
        self.assertIn("hi", result.output)
        with self.assertRaises(PermissionDenied):
            REGISTRY["bash"].execute({"command": "rm -rf /"}, ctx)

    def test_a_catch_all_placed_last_overrides_earlier_rules(self):
        """The migration hazard itself: order the old way and nothing is allowed."""
        class FakeConfig:
            data = {"permission": {"bash": {"echo *": "allow", "*": "deny"}}}

            def save(self):
                pass

        ctx = ToolContext(cwd=self.dir, permissions=Permissions(config=FakeConfig()))
        with self.assertRaises(PermissionDenied):
            REGISTRY["bash"].execute({"command": "echo hi"}, ctx)


class TestRegistry(unittest.TestCase):
    def test_expected_tools_present(self):
        expected = {"bash", "edit", "glob", "grep", "list", "read", "task",
                    "todowrite", "webfetch", "write"}
        self.assertTrue(expected.issubset(set(REGISTRY)))

    def test_specs_are_valid_json_schema_objects(self):
        for spec in tool_specs(get_tools()):
            self.assertTrue(spec.description, f"{spec.name} has no description")
            self.assertEqual(spec.parameters.get("type"), "object", spec.name)
            self.assertIn("properties", spec.parameters, spec.name)
            for prop, definition in spec.parameters["properties"].items():
                self.assertIn("type", definition, f"{spec.name}.{prop}")

    def test_subset_selection(self):
        tools = get_tools(["read", "write"])
        self.assertEqual(set(tools), {"read", "write"})


if __name__ == "__main__":
    unittest.main()
