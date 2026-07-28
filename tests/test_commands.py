import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode import commands
from haikode.commands import (CommandRegistry, CustomCommand, expand_mentions,
                              generate_agents_md_prompt, load_custom_commands,
                              parse_frontmatter)


class MentionTests(unittest.TestCase):
    def test_existing_file_is_expanded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.py"
            path.write_text("print('hi')\n")

            text, paths = expand_mentions("explain @main.py please", directory)

            self.assertIn("--- @main.py ---", text)
            self.assertIn("print('hi')", text)
            self.assertTrue(text.startswith("explain @main.py please"))
            self.assertEqual(paths, [str(path.resolve())])

    def test_unmatched_token_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            original = "mail jorgis@example.com about @nope-not-here"
            text, paths = expand_mentions(original, directory)

            self.assertEqual(text, original)
            self.assertEqual(paths, [])

    def test_quoted_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "my file.txt"
            path.write_text("spaced content")

            text, paths = expand_mentions('read @"my file.txt" now', directory)

            self.assertIn('--- @my file.txt ---', text)
            self.assertIn("spaced content", text)
            self.assertEqual(paths, [str(path.resolve())])

    def test_duplicate_mentions_expand_once(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "a.txt").write_text("body")

            text, paths = expand_mentions("@a.txt vs @a.txt", directory)

            self.assertEqual(len(paths), 1)
            self.assertEqual(text.count("--- @a.txt ---"), 1)

    def test_directory_is_listed_not_dumped(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / "src"
            nested.mkdir()
            (nested / "one.py").write_text("secret body")
            (nested / "sub").mkdir()

            text, _ = expand_mentions("look at @src", directory)

            self.assertIn("one.py", text)
            self.assertIn("sub/", text)
            self.assertNotIn("secret body", text)

    def test_binary_and_truncation_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
            (Path(directory) / "big.txt").write_text("x" * (commands.MAX_MENTION_CHARS + 50))

            text, paths = expand_mentions("@blob.bin and @big.txt", directory)

            self.assertIn("[binary file, 4 bytes]", text)
            self.assertIn("truncated at", text)
            self.assertEqual(len(paths), 2)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs FIFOs")
    def test_fifo_does_not_block(self):
        # Opening a FIFO with no writer blocks forever; a stray @token must
        # never hang the REPL.
        with tempfile.TemporaryDirectory() as directory:
            os.mkfifo(os.path.join(directory, "pipe"))

            def bail(signum, frame):
                raise AssertionError("expand_mentions blocked on a FIFO")

            previous = signal.signal(signal.SIGALRM, bail)
            signal.alarm(5)
            try:
                text, _ = expand_mentions("check @pipe", directory)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)

            self.assertIn("[not a regular file]", text)

    def test_non_ascii_content_is_decoded_as_utf8(self):
        # The locale may be POSIX/ASCII on Haiku; decoding must not depend on it.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "n.txt"
            path.write_text("kjøre tester\n", encoding="utf-8")

            text, _ = expand_mentions("@n.txt", directory)

            self.assertIn("kjøre tester", text)

    def test_large_file_is_not_fully_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "huge.txt"
            with open(path, "w", encoding="utf-8") as handle:
                for _ in range(40):
                    handle.write("y" * 100000)

            body = commands._file_body(path)

            self.assertLess(len(body), commands.MAX_MENTION_CHARS + 200)
            self.assertIn("4000000 bytes", body)


class FrontmatterTests(unittest.TestCase):
    def test_parses_known_keys_and_body(self):
        raw = ('---\n'
               'description: Run tests with coverage\n'
               'agent: build\n'
               'model: "anthropic/claude-sonnet-4"\n'
               '# a comment\n'
               'unknown: ignored\n'
               '---\n'
               '\nRun the suite for $ARGUMENTS.\n')

        data, body = parse_frontmatter(raw)

        self.assertEqual(data["description"], "Run tests with coverage")
        self.assertEqual(data["agent"], "build")
        self.assertEqual(data["model"], "anthropic/claude-sonnet-4")
        self.assertEqual(body.strip(), "Run the suite for $ARGUMENTS.")

    def test_body_without_frontmatter_is_untouched(self):
        data, body = parse_frontmatter("just a prompt\n")

        self.assertEqual(data, {})
        self.assertEqual(body, "just a prompt\n")

    def test_command_file_frontmatter(self):
        raw = ('---\n'
               'description: Review a component\n'
               'agent: plan\n'
               '---\n'
               'Review @src/Button.tsx carefully.\n')

        command = CustomCommand.from_markdown(raw, "review")

        self.assertEqual(command.name, "review")
        self.assertEqual(command.description, "Review a component")
        self.assertEqual(command.agent, "plan")
        self.assertEqual(command.model, "")
        self.assertEqual(command.template, "Review @src/Button.tsx carefully.")


class RenderTests(unittest.TestCase):
    def test_arguments_placeholder(self):
        command = CustomCommand("component", "Create a component named $ARGUMENTS.")

        self.assertEqual(command.render("Button primary"),
                         "Create a component named Button primary.")

    def test_positional_placeholders(self):
        command = CustomCommand("create", "Make $1 inside $2.")

        self.assertEqual(command.render('config.json "my dir"'),
                         "Make config.json inside my dir.")

    def test_last_placeholder_absorbs_remaining_arguments(self):
        command = CustomCommand("fix", "Fix $1.")

        self.assertEqual(command.render("the broken parser"),
                         "Fix the broken parser.")

    def test_missing_positional_becomes_empty(self):
        command = CustomCommand("create", "Make $1 inside $2.")

        self.assertEqual(command.render("only"), "Make only inside .")

    def test_arguments_appended_when_template_has_no_placeholder(self):
        command = CustomCommand("plain", "Do the thing.")

        self.assertEqual(command.render("with feeling"),
                         "Do the thing.\n\nwith feeling")

    def test_inline_shell_is_executed_for_a_trusted_command(self):
        with tempfile.TemporaryDirectory() as directory:
            command = CustomCommand("sh", "Output: !`echo haikode`",
                                    cwd=directory, trusted=True)

            self.assertEqual(command.render(""), "Output: haikode")

    def test_inline_shell_is_inert_until_the_project_is_trusted(self):
        """The default must be "do not run".

        This test used to assert the opposite by omission: it constructed a
        command without saying anything about trust and required the shell to
        run, which is exactly the behaviour that let a cloned repository
        execute arbitrary commands.
        """
        with tempfile.TemporaryDirectory() as directory:
            command = CustomCommand("sh", "Output: !`echo haikode`",
                                    cwd=directory)

            rendered = command.render("")

            self.assertNotIn("Output: haikode", rendered)
            self.assertIn("not trusted", rendered)

    def test_inline_shell_does_not_read_stdin(self):
        # `cat` with an inherited terminal would eat the user's keystrokes and
        # stall for the full timeout; stdin must be closed.
        with tempfile.TemporaryDirectory() as directory:
            # trusted=True, or nothing runs and the test proves nothing.
            command = CustomCommand("sh", "Output: !`cat`", cwd=directory,
                                    trusted=True)

            def bail(signum, frame):
                raise AssertionError("inline shell blocked on stdin")

            previous = signal.signal(signal.SIGALRM, bail)
            signal.alarm(5)
            try:
                self.assertEqual(command.render(""), "Output:")
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)

    def test_mentions_survive_rendering(self):
        command = CustomCommand("review", "Review @src/main.py for $ARGUMENTS.")

        self.assertEqual(command.render("bugs"),
                         "Review @src/main.py for bugs.")


class LoadCustomCommandTests(unittest.TestCase):
    def _write(self, base: Path, name: str, text: str):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_project_commands_win_over_global(self):
        with tempfile.TemporaryDirectory() as project, \
                tempfile.TemporaryDirectory() as globaldir:
            self._write(Path(globaldir) / "command", "test.md", "global template")
            self._write(Path(globaldir) / "command", "only-global.md", "global only")
            self._write(Path(project) / ".haikode" / "command", "test.md",
                        "---\ndescription: project\n---\nproject template")
            self._write(Path(project) / ".haikode" / "command", "deep/nested.md",
                        "nested template")

            with patch.object(commands, "global_config_dir",
                              return_value=Path(globaldir)):
                loaded = load_custom_commands(project)

            self.assertEqual(loaded["test"].template, "project template")
            self.assertEqual(loaded["test"].description, "project")
            self.assertEqual(loaded["only-global"].template, "global only")
            self.assertEqual(loaded["deep/nested"].template, "nested template")


class RegistryTests(unittest.TestCase):
    def _registry(self, project: str, globaldir: str) -> CommandRegistry:
        registry = CommandRegistry(project)
        registry.register("help", lambda arg: "help output", "show help",
                          aliases=("h",))
        registry.register("clear", lambda arg: None, "clear the session")
        with patch.object(commands, "global_config_dir",
                          return_value=Path(globaldir)):
            registry.load_custom()
        return registry

    def test_dispatch_builtin_custom_and_unknown(self):
        with tempfile.TemporaryDirectory() as project, \
                tempfile.TemporaryDirectory() as globaldir:
            command_dir = Path(project) / ".haikode" / "command"
            command_dir.mkdir(parents=True)
            (command_dir / "test.md").write_text(
                "---\ndescription: Run tests\n---\nRun tests for $ARGUMENTS.\n")
            registry = self._registry(project, globaldir)

            self.assertEqual(registry.dispatch("/help"), ("builtin", "help output"))
            self.assertEqual(registry.dispatch("/h"), ("builtin", "help output"))
            self.assertEqual(registry.dispatch("/clear"), ("builtin", None))
            self.assertEqual(registry.dispatch("/test parser"),
                             ("prompt", "Run tests for parser."))
            self.assertEqual(registry.dispatch("/nope arg"), ("unknown", "nope"))

    def test_non_command_lines_return_none(self):
        with tempfile.TemporaryDirectory() as project, \
                tempfile.TemporaryDirectory() as globaldir:
            registry = self._registry(project, globaldir)

            self.assertIsNone(registry.dispatch("hello there"))
            self.assertIsNone(registry.dispatch(""))
            self.assertIsNone(registry.dispatch("read @main.py"))

    def test_reregistering_drops_stale_aliases(self):
        with tempfile.TemporaryDirectory() as project, \
                tempfile.TemporaryDirectory() as globaldir:
            registry = self._registry(project, globaldir)
            registry.register("help", lambda arg: "new help", "show help")

            self.assertEqual(registry.complete("h"), ["help"])
            self.assertEqual(registry.dispatch("/help"), ("builtin", "new help"))

    def test_complete_and_help_text(self):
        with tempfile.TemporaryDirectory() as project, \
                tempfile.TemporaryDirectory() as globaldir:
            command_dir = Path(project) / ".haikode" / "command"
            command_dir.mkdir(parents=True)
            (command_dir / "coverage.md").write_text(
                "---\ndescription: Coverage report\n---\nShow coverage.\n")
            registry = self._registry(project, globaldir)

            self.assertEqual(registry.complete("/c"), ["clear", "coverage"])
            self.assertEqual(registry.complete("he"), ["help"])
            self.assertEqual(registry.complete("zz"), [])

            text = registry.help_text()
            self.assertIn("/help /h", text)
            self.assertIn("show help", text)
            self.assertIn("Custom commands:", text)
            self.assertIn("Coverage report", text)


class InitPromptTests(unittest.TestCase):
    def test_prompt_mentions_agents_md_and_root(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = generate_agents_md_prompt(directory)

            self.assertIn("AGENTS.md", prompt)
            self.assertIn(str(Path(directory).resolve()), prompt)
            self.assertTrue(len(prompt) > 500)


if __name__ == "__main__":
    unittest.main()
