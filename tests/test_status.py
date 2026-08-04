import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from haikode import status
from haikode.config import Config
from haikode.permission import ALLOW, ASK, DENY
from haikode.status import SetupInfo, collect, detail_lines, summary_lines, truncate

HAS_GIT = shutil.which("git") is not None


class _FakeStore:
    """Stands in for SessionStore so tests never touch the real database."""

    rows = []

    def __init__(self, *args, **kwargs):
        pass

    def list_sessions(self, limit=50):
        return list(self.rows)[:limit]


class _BrokenStore:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("sqlite3 unavailable")


@contextmanager
def isolated(directory, store=_FakeStore):
    """Keep collect() away from the machine's keystore, sessions and AGENTS.md."""
    with (patch.dict(os.environ, {"HAI_DISABLE_KEYSTORE": "1"}),
          patch("haikode.session.SessionStore", store),
          patch("haikode.status.global_config_dir",
                return_value=Path(directory) / "global-config")):
        yield


def make_config(directory, **provider):
    config = Config(os.path.join(directory, "config.json"))
    demo = {
        "dialect": "openai",
        "base_url": "https://example.invalid/v1",
        "key_env": "HAIKODE_TEST_UNSET_KEY",
        "model": "demo-model",
        "context": 4096,
    }
    demo.update(provider)
    config.data["providers"]["demo"] = demo
    config.data["default_provider"] = "demo"
    return config


def init_repo(path, branch="main"):
    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True,
                       capture_output=True, text=True, timeout=30)

    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True, text=True, timeout=30)
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "haikode test")
    git("config", "commit.gpgsign", "false")
    (Path(path) / "a.txt").write_text("hello\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("branch", "-M", branch)


class CollectTests(unittest.TestCase):
    def test_reports_provider_model_and_config_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, api_key="sk-test")
            with isolated(directory):
                info = collect(config, "demo", directory)
        self.assertEqual(info.provider, "demo")
        self.assertEqual(info.model, "demo-model")
        self.assertEqual(info.auth, "key from config file")
        self.assertTrue(info.auth_ok)
        self.assertEqual(info.config_path, os.path.join(directory, "config.json"))
        self.assertEqual(info.tool_count, len(info.tool_names))
        self.assertIn("bash", info.tool_names)

    def test_falls_back_to_the_default_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, None, directory)
        self.assertEqual(info.provider, "demo")

    def test_unset_key_is_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "demo", directory)
        self.assertEqual(info.auth, "no key set")
        self.assertFalse(info.auth_ok)

    def test_environment_key_names_its_variable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with (isolated(directory),
                  patch.dict(os.environ, {"HAIKODE_TEST_UNSET_KEY": "sk-env"})):
                info = collect(config, "demo", directory)
        self.assertEqual(info.auth, "key from $HAIKODE_TEST_UNSET_KEY")
        self.assertTrue(info.auth_ok)

    def test_provider_without_key_requirement_needs_no_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, requires_key=False)
            with isolated(directory):
                info = collect(config, "demo", directory)
        self.assertEqual(info.auth, "no key required")
        self.assertTrue(info.auth_ok)

    def test_oauth_provider_reports_signed_out(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "chatgpt", directory)
        self.assertEqual(info.auth, "oauth: not signed in")
        self.assertFalse(info.auth_ok)

    def test_unknown_provider_degrades_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "nope", directory)
        self.assertEqual(info.provider, "nope")
        self.assertEqual(info.model, "")
        self.assertEqual(info.auth, "unknown provider")
        self.assertFalse(info.auth_ok)

    def test_without_a_git_repository_there_is_no_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "demo", directory)
        self.assertEqual(info.git_branch, "")
        self.assertTrue(info.cwd_label)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_git_branch_is_detected_from_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            init_repo(repo)
            sub = repo / "pkg"
            sub.mkdir()
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "demo", str(sub))
        self.assertEqual(info.git_branch, "main")
        # short_label keeps a path that fits whole and falls back to the
        # basename only when it does not — /tmp/... on Linux fits, macOS's
        # deep temp paths do not, so only the tail is stable across both.
        self.assertTrue(info.cwd_label.endswith("pkg"), info.cwd_label)

    def test_instruction_files_found_up_to_the_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "sub").mkdir(parents=True)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("root rules\n")
            (root / "sub" / "CLAUDE.md").write_text("nested rules\n")
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "demo", str(root / "sub"))
        names = [os.path.basename(path) for path in info.instructions_files]
        self.assertEqual(names, ["AGENTS.md", "CLAUDE.md"])
        self.assertTrue(all(os.path.isabs(path) for path in info.instructions_files))

    def test_tool_selection_limits_the_reported_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory):
                info = collect(config, "demo", directory, tools=["read", "bash"])
        self.assertEqual(info.tool_names, ["bash", "read"])
        self.assertEqual(info.tool_count, 2)

    def test_session_count_comes_from_the_store(self):
        class TwoSessions(_FakeStore):
            rows = [{"id": "a"}, {"id": "b"}]

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory, store=TwoSessions):
                info = collect(config, "demo", directory)
        self.assertEqual(info.session_count, 2)

    def test_a_broken_session_store_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with isolated(directory, store=_BrokenStore):
                info = collect(config, "demo", directory)
        self.assertEqual(info.session_count, 0)
        self.assertEqual(info.provider, "demo")

    def test_a_missing_working_directory_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            missing = os.path.join(directory, "gone")
            with isolated(directory):
                info = collect(config, "demo", missing)
        self.assertEqual(info.git_branch, "")
        self.assertEqual(info.instructions_files, [])


class PermissionPolicyTests(unittest.TestCase):
    def policies(self, rules):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            config.data["permission"] = rules
            with isolated(directory):
                return collect(config, "demo", directory)

    def test_defaults_apply_when_nothing_is_configured(self):
        info = self.policies({})
        self.assertIn("read", info.allow_tools)
        self.assertIn("bash", info.ask_tools)
        self.assertEqual(info.deny_tools, [])

    def test_plain_string_rule(self):
        info = self.policies({"bash": DENY, "edit": ALLOW})
        self.assertIn("bash", info.deny_tools)
        self.assertIn("edit", info.allow_tools)
        self.assertNotIn("bash", info.ask_tools)

    def test_pattern_rule_uses_its_catch_all(self):
        info = self.policies({"bash": {"git status": ALLOW, "*": DENY}})
        self.assertIn("bash", info.deny_tools)

    def test_pattern_rule_without_a_catch_all_keeps_the_default(self):
        info = self.policies({"bash": {"rm *": DENY}})
        self.assertIn("bash", info.ask_tools)
        self.assertNotIn("bash", info.deny_tools)

    def test_configured_key_without_a_tool_is_still_reported(self):
        info = self.policies({"lsp": ALLOW})
        self.assertIn("lsp", info.allow_tools)

    def test_malformed_permission_block_is_ignored(self):
        info = self.policies("not a mapping")
        self.assertIn("bash", info.ask_tools)
        self.assertEqual(info.deny_tools, [])

    def test_every_key_lands_in_exactly_one_bucket(self):
        info = self.policies({"bash": DENY})
        buckets = info.allow_tools + info.ask_tools + info.deny_tools
        self.assertEqual(len(buckets), len(set(buckets)))
        for policy in (ALLOW, ASK, DENY):
            self.assertIsInstance(policy, str)


class SummaryTests(unittest.TestCase):
    def info(self, **overrides):
        base = dict(
            provider="ollama", model="glm-5.2", auth="key from keystore",
            auth_ok=True, cwd="/boot/home/haikode-demo",
            cwd_label="~/haikode-demo", git_branch="main",
            tool_count=10, tool_names=["bash", "edit"],
            ask_tools=["bash", "edit", "write"], allow_tools=["read"],
            deny_tools=[], config_path="/boot/home/config.json",
            instructions_files=["/boot/home/haikode-demo/AGENTS.md"],
            session_count=4)
        base.update(overrides)
        return SetupInfo(**base)

    def test_shape_and_styles(self):
        lines = summary_lines(self.info())
        self.assertTrue(3 <= len(lines) <= 5)
        for text, style in lines:
            self.assertIn(style, ("info", "muted", "warn"))
            self.assertTrue(text)
        self.assertEqual(lines[0][0], "~/haikode-demo · main")
        self.assertEqual(lines[1][0], "ollama · glm-5.2 · key from keystore")
        self.assertIn("10 tools", lines[2][0])
        self.assertIn("ask first", lines[2][0])

    def test_warn_line_when_no_key(self):
        lines = summary_lines(self.info(auth="no key set", auth_ok=False))
        warnings = [text for text, style in lines if style == "warn"]
        self.assertEqual(warnings, ["no key for ollama — run /login ollama"])
        self.assertTrue(3 <= len(lines) <= 5)

    def test_warn_line_for_a_signed_out_oauth_provider(self):
        lines = summary_lines(self.info(provider="chatgpt",
                                        auth="oauth: not signed in", auth_ok=False))
        warnings = [text for text, style in lines if style == "warn"]
        self.assertEqual(warnings, ["not signed in to chatgpt — run /login chatgpt"])

    def test_no_warn_line_when_authenticated(self):
        self.assertEqual([s for _, s in summary_lines(self.info()) if s == "warn"], [])

    def test_lines_are_truncated_to_width(self):
        info = self.info(cwd_label="~/a-very-long-project-directory-name-indeed",
                         model="some-extremely-long-model-identifier-v3",
                         auth="key from keystore", git_branch="feature/long-branch-name")
        lines = summary_lines(info, width=24)
        for text, _ in lines:
            self.assertLessEqual(len(text), 24)
        self.assertTrue(any(text.endswith("...") for text, _ in lines))

    def test_truncation_prefers_a_word_boundary(self):
        self.assertEqual(truncate("ollama glm five two", 12), "ollama...")
        self.assertEqual(truncate("short", 12), "short")
        self.assertEqual(truncate("abc", 2), "ab")
        self.assertEqual(truncate("abc", 0), "")
        self.assertLessEqual(len(truncate("supercalifragilistic", 10)), 10)

    def test_ascii_fallback_avoids_unicode_separators(self):
        lines = summary_lines(self.info(auth_ok=False), width=200, unicode_ok=False)
        for text, _ in lines:
            self.assertTrue(all(ord(ch) < 128 for ch in text), text)

    def test_deny_and_missing_extras_still_produce_a_summary(self):
        lines = summary_lines(self.info(deny_tools=["bash"], instructions_files=[],
                                        session_count=0, git_branch=""))
        self.assertTrue(3 <= len(lines) <= 5)
        self.assertIn("denied", lines[2][0])


class DetailTests(unittest.TestCase):
    def test_report_covers_every_field(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, api_key="sk-test")
            with isolated(directory):
                info = collect(config, "demo", directory)
        text = "\n".join(detail_lines(info))
        for label in ("Provider:", "Model:", "Auth:", "Config:", "Keystore:",
                      "Directory:", "Branch:", "Instructions:", "Tools (",
                      "Permissions:", "Sessions:", "Python:"):
            self.assertIn(label, text)
        self.assertIn("demo-model", text)
        self.assertIn("key from config file", text)
        self.assertIn("not a git repository", text)

    def test_permission_buckets_are_listed(self):
        info = SetupInfo(allow_tools=["read"], ask_tools=["bash"], deny_tools=["write"])
        text = "\n".join(detail_lines(info))
        self.assertIn("allow: read", text)
        self.assertIn("ask: bash", text)
        self.assertIn("deny: write", text)

    def test_empty_info_still_formats(self):
        lines = detail_lines(SetupInfo())
        self.assertTrue(all(isinstance(line, str) for line in lines))
        self.assertIn("(none configured)", lines[0])


class HelperTests(unittest.TestCase):
    def test_home_relative(self):
        home = os.path.expanduser("~")
        self.assertEqual(status.home_relative(os.path.join(home, "x")), "~/x")
        self.assertEqual(status.home_relative(home), "~")
        self.assertEqual(status.home_relative("/opt/x"), "/opt/x")
        self.assertEqual(status.home_relative(""), "")

    def test_short_label_falls_back_to_the_basename(self):
        deep = "/opt/one/two/three/four/five/six/seven/project"
        self.assertEqual(status.short_label(deep), "project")
        self.assertEqual(status.short_label("/opt/project"), "/opt/project")


if __name__ == "__main__":
    unittest.main()
