"""
Skills: discovery, frontmatter, the `skill` tool — and the registry check that
keeps a tool from existing but being invisible to the model.

Also covers the MCP presentation helpers, which are what a `/mcp` command and
an MCP dialog render.
"""

import importlib
import inspect
import pkgutil
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import skills  # noqa: E402
from haikode.permission import Permissions  # noqa: E402
from haikode.schema import PermissionDenied  # noqa: E402
from haikode.tool import REGISTRY, tool_specs  # noqa: E402
from haikode.tool.base import Tool, ToolContext  # noqa: E402

SIMPLE = """---
name: %s
description: %s
---

# %s

%s
"""


class FakeConfig:
    """Minimal stand-in for Config: a `data` dict, as Permissions expects."""

    def __init__(self, permission=None):
        self.data = {"permission": dict(permission or {})}


class SkillTestCase(unittest.TestCase):
    """A project directory and a global config directory, both empty."""

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="haikode-skills-project-")
        self.globaldir = tempfile.mkdtemp(prefix="haikode-skills-global-")
        skills.clear_cache()
        patcher = patch.object(skills, "global_config_dir",
                               return_value=Path(self.globaldir))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(skills.clear_cache)

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)
        shutil.rmtree(self.globaldir, ignore_errors=True)

    # -- helpers --

    def write(self, scope: str, name: str, text: str,
              directory: str = "skill", filename: str = "SKILL.md") -> Path:
        base = (Path(self.project) / ".haikode" if scope == "project"
                else Path(self.globaldir))
        path = base / directory / name / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def skill_file(self, scope: str, name: str, description: str = "desc",
                   body: str = "Do the thing.", **kwargs) -> Path:
        return self.write(scope, name,
                          SIMPLE % (name, description, name, body), **kwargs)

    def context(self, permissions=None) -> ToolContext:
        return ToolContext(cwd=self.project,
                           permissions=permissions or Permissions(auto_approve=True))

    def run_skill(self, name, permissions=None):
        return REGISTRY["skill"].execute({"name": name},
                                         self.context(permissions))


class TestDiscovery(SkillTestCase):
    def test_project_beats_global(self):
        self.skill_file("global", "deploy", "global copy", "GLOBAL BODY")
        self.skill_file("project", "deploy", "project copy", "PROJECT BODY")

        registry = skills.discover(self.project)

        self.assertEqual(registry.names(), ["deploy"])
        skill = registry.get("deploy")
        self.assertIn("PROJECT BODY", skill.body)
        self.assertEqual(skill.description, "project copy")
        # A deliberate override is not a problem to report.
        self.assertEqual(registry.warnings, [])

    def test_a_global_skill_is_found_when_the_project_has_none(self):
        self.skill_file("global", "notes", "global only")
        registry = skills.discover(self.project)
        self.assertEqual(registry.names(), ["notes"])

    def test_both_directory_spellings_are_scanned(self):
        self.skill_file("project", "one", directory="skill")
        self.skill_file("project", "two", directory="skills")
        self.assertEqual(skills.discover(self.project).names(), ["one", "two"])

    def test_nested_skill_directories_are_found(self):
        self.write("project", "team/deep",
                   SIMPLE % ("deep", "nested", "deep", "body"))
        self.assertEqual(skills.discover(self.project).names(), ["deep"])

    def test_a_duplicate_inside_one_scope_is_warned_about(self):
        self.write("project", "a", SIMPLE % ("dup", "first", "dup", "one"))
        self.write("project", "b", SIMPLE % ("dup", "second", "dup", "two"))
        registry = skills.discover(self.project)
        self.assertEqual(registry.names(), ["dup"])
        self.assertTrue(any("duplicate name" in w for w in registry.warnings),
                        registry.warnings)

    def test_no_skill_directories_is_not_an_error(self):
        registry = skills.discover(self.project)
        self.assertEqual(registry.names(), [])
        self.assertEqual(registry.warnings, [])
        self.assertEqual(registry.prompt_block(), "")

    def test_the_cache_is_refreshed_on_demand(self):
        self.skill_file("project", "one")
        self.assertEqual(skills.load(self.project).names(), ["one"])
        self.skill_file("project", "two")
        self.assertEqual(skills.load(self.project).names(), ["one"])
        self.assertEqual(skills.load(self.project, refresh=True).names(),
                         ["one", "two"])


class TestFrontmatter(SkillTestCase):
    def test_fields_and_body(self):
        path = self.write("project", "review", """---
name: review
description: Review a change before it ships.
when to use: before any release
---

# Review

Step one.
Step two.
""")
        registry = skills.discover(self.project)
        skill = registry.get("review")
        self.assertEqual(skill.description, "Review a change before it ships.")
        self.assertEqual(skill.when, "before any release")
        self.assertEqual(skill.path, path)
        self.assertEqual(skill.directory, path.parent)
        self.assertTrue(skill.body.startswith("# Review"))
        self.assertIn("Step two.", skill.body)
        self.assertFalse(skill.truncated)

    def test_quoted_values_and_colons_survive(self):
        self.write("project", "x", """---
name: "x"
description: 'Use when: the build fails'
---

body
""")
        skill = skills.discover(self.project).get("x")
        self.assertEqual(skill.description, "Use when: the build fails")

    def test_summary_is_one_bounded_line(self):
        self.skill_file("project", "long", "a " * 400)
        skill = skills.discover(self.project).get("long")
        summary = skill.summary()
        self.assertNotIn("\n", summary)
        self.assertLessEqual(len(summary), skills.MAX_SUMMARY_CHARS + 3)

    def test_a_huge_body_is_truncated_not_dropped(self):
        self.skill_file("project", "big", "desc",
                        "x" * (skills.MAX_BODY_CHARS + 5000))
        skill = skills.discover(self.project).get("big")
        self.assertTrue(skill.truncated)
        self.assertLess(len(skill.body), skills.MAX_BODY_CHARS + 200)
        self.assertIn("truncated", skill.body)


class TestMalformed(SkillTestCase):
    """A broken skill is skipped with a warning; it never raises, and it never
    takes a healthy sibling with it."""

    def assert_skipped(self, needle: str):
        registry = skills.discover(self.project)
        self.assertEqual(registry.names(), ["good"])
        self.assertTrue(any(needle in w for w in registry.warnings),
                        registry.warnings)

    def setUp(self):
        super().setUp()
        self.skill_file("project", "good")

    def test_no_frontmatter(self):
        self.write("project", "bad", "# Just markdown\n\nno frontmatter\n")
        self.assert_skipped("no 'name'")

    def test_frontmatter_without_a_name(self):
        self.write("project", "bad", "---\ndescription: nameless\n---\n\nbody\n")
        self.assert_skipped("no 'name'")

    def test_unusable_name(self):
        self.write("project", "bad",
                   "---\nname: ../../etc/passwd\n---\n\nbody\n")
        self.assert_skipped("unusable skill name")

    def test_empty_body(self):
        self.write("project", "bad", "---\nname: bad\ndescription: d\n---\n\n")
        self.assert_skipped("empty body")

    def test_binary_file(self):
        path = self.write("project", "bad", "placeholder")
        path.write_bytes(b"---\nname: bad\n---\n\n\x00\x01binary")
        self.assert_skipped("not a text file")

    def test_oversized_file(self):
        path = self.write("project", "bad", "placeholder")
        path.write_text("---\nname: bad\n---\n\n"
                        + "y" * (skills.MAX_FILE_BYTES + 10))
        self.assert_skipped("skipped")

    def test_a_name_that_does_not_match_its_directory_is_reported(self):
        self.write("project", "folder",
                   SIMPLE % ("other", "d", "other", "body"))
        registry = skills.discover(self.project)
        self.assertIn("other", registry.names())
        self.assertTrue(any("does not match" in w for w in registry.warnings),
                        registry.warnings)


class TestPromptBlock(SkillTestCase):
    def test_names_and_descriptions_only(self):
        body = "SECRET BODY TEXT\n" * 500
        self.skill_file("project", "alpha", "does alpha things", body)
        self.skill_file("project", "beta", "does beta things", body)

        block = skills.prompt_block(self.project)

        self.assertIn("- **alpha**: does alpha things", block)
        self.assertIn("- **beta**: does beta things", block)
        self.assertNotIn("SECRET BODY TEXT", block)
        # The point of the whole design: the listing costs a line per skill.
        self.assertLess(len(block), 400)

    def test_a_skill_without_a_description_is_not_advertised(self):
        self.write("project", "quiet", "---\nname: quiet\n---\n\nbody\n")
        self.skill_file("project", "loud", "listed")
        block = skills.prompt_block(self.project)
        self.assertIn("loud", block)
        self.assertNotIn("quiet", block)

    def test_the_block_is_bounded_when_there_are_many_skills(self):
        for index in range(60):
            self.skill_file("project", "skill-%02d" % index, "d" * 200)
        block = skills.prompt_block(self.project)
        self.assertLess(len(block), skills.MAX_PROMPT_CHARS + 200)
        self.assertIn("more; call the skill tool by name", block)

    def test_a_denied_skill_is_not_advertised(self):
        self.skill_file("project", "secret", "hidden")
        self.skill_file("project", "open", "shown")
        permissions = Permissions(
            config=FakeConfig({"skill": {"*": "allow", "secret": "deny"}}))
        block = skills.prompt_block(self.project, permissions=permissions)
        self.assertIn("open", block)
        self.assertNotIn("secret", block)

    def test_a_broken_permission_layer_does_not_hide_skills(self):
        self.skill_file("project", "open", "shown")

        class Exploding:
            def decide(self, key, pattern):
                raise RuntimeError("no")

        self.assertIn("open", skills.prompt_block(self.project,
                                                  permissions=Exploding()))


class TestReport(SkillTestCase):
    """What a /skills command prints."""

    def test_lists_names_summaries_and_warnings(self):
        self.skill_file("project", "deploy", "ship it")
        self.write("project", "bad", "no frontmatter here")
        report = skills.report(self.project)
        self.assertIn("Skills:", report)
        self.assertIn("deploy", report)
        self.assertIn("ship it", report)
        self.assertIn("Warnings:", report)

    def test_says_so_when_there_are_none(self):
        report = skills.report(self.project)
        self.assertIn("No skills found.", report)
        self.assertIn("SKILL.md", report)


class TestSkillTool(SkillTestCase):
    def test_returns_the_body_and_the_base_directory(self):
        path = self.skill_file("project", "deploy", "ship it",
                               "Run scripts/deploy.sh with --dry-run first.")
        (path.parent / "scripts").mkdir()
        (path.parent / "scripts" / "deploy.sh").write_text("#!/bin/sh\n")

        result = self.run_skill("deploy")

        # ToolContext resolves its cwd, so compare against the resolved path:
        # on macOS /var is a symlink to /private/var.
        directory = path.parent.resolve()
        self.assertEqual(result.title, "Loaded skill: deploy")
        self.assertIn('<skill_content name="deploy">', result.output)
        self.assertIn("Run scripts/deploy.sh with --dry-run first.",
                      result.output)
        self.assertIn("Base directory for this skill: %s" % directory,
                      result.output)
        self.assertIn("deploy.sh", result.output)
        self.assertEqual(result.metadata["name"], "deploy")
        self.assertEqual(result.metadata["location"],
                         str(directory / "SKILL.md"))

    def test_when_to_use_is_carried_into_the_body(self):
        self.write("project", "x", """---
name: x
description: d
when to use: only on Tuesdays
---

body
""")
        self.assertIn("only on Tuesdays", self.run_skill("x").output)

    def test_unknown_name_fails_cleanly(self):
        self.skill_file("project", "known")
        with self.assertRaises(ValueError) as caught:
            self.run_skill("nope")
        message = str(caught.exception)
        self.assertIn("Unknown skill", message)
        self.assertIn("known", message)

    def test_unknown_name_with_no_skills_at_all(self):
        with self.assertRaises(ValueError) as caught:
            self.run_skill("nope")
        self.assertIn("none", str(caught.exception))

    def test_a_missing_name_argument_is_refused(self):
        with self.assertRaises(ValueError):
            REGISTRY["skill"].execute({}, self.context())

    def test_a_skill_written_mid_session_is_found_without_a_restart(self):
        self.skill_file("project", "first")
        skills.load(self.project)  # warm the cache without the new skill
        self.skill_file("project", "second", "added later")
        self.assertIn("Loaded skill: second", self.run_skill("second").title)

    def test_permission_is_asked_for_that_one_skill(self):
        self.skill_file("project", "deploy")
        seen = []

        def asker(request):
            seen.append(request)
            return "once"

        self.run_skill("deploy", Permissions(asker=asker))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].key, "skill")
        self.assertEqual(seen[0].patterns, ["deploy"])
        self.assertEqual(seen[0].always, ["deploy"])

    def test_a_denied_skill_is_not_loaded(self):
        self.skill_file("project", "deploy")
        # A deny rule outranks the auto-approve a --yes run would carry.
        permissions = Permissions(config=FakeConfig({"skill": "deny"}),
                                  auto_approve=True)
        with self.assertRaises(PermissionDenied):
            self.run_skill("deploy", permissions)

    def test_an_allow_rule_loads_without_asking(self):
        self.skill_file("project", "deploy")

        def asker(request):
            raise AssertionError("should not have been asked")

        permissions = Permissions(config=FakeConfig({"skill": "allow"}),
                                  asker=asker)
        self.assertIn("Loaded skill: deploy",
                      self.run_skill("deploy", permissions).title)

    def test_a_headless_run_without_an_asker_is_refused(self):
        self.skill_file("project", "deploy")
        with self.assertRaises(PermissionDenied):
            self.run_skill("deploy", Permissions())

    def test_permission_patterns_name_the_skill(self):
        tool = REGISTRY["skill"]
        ctx = self.context()
        self.assertEqual(list(tool.permission_patterns({"name": "deploy"}, ctx)),
                         ["deploy"])
        self.assertEqual(list(tool.permission_patterns({}, ctx)), ["skill"])


class TestRegistry(unittest.TestCase):
    """A tool that exists but is not registered is invisible to the model."""

    def test_the_skill_tool_is_registered(self):
        self.assertIn("skill", REGISTRY)
        self.assertTrue(REGISTRY["skill"].description.strip())

    def test_every_tool_class_in_the_package_is_registered(self):
        import haikode.tool as package

        missing = []
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module("haikode.tool." + info.name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if not issubclass(obj, Tool) or obj is Tool:
                    continue
                if obj.__module__ != module.__name__:
                    continue  # imported into this module, owned by another
                if not getattr(obj, "name", ""):
                    continue  # abstract or dynamically named (MCP proxies)
                if obj.name not in REGISTRY:
                    missing.append("%s.%s (%s)"
                                   % (module.__name__, obj.__name__, obj.name))
        self.assertEqual(missing, [], "tool classes missing from REGISTRY")

    def test_the_memory_tools_are_still_registered(self):
        self.assertIn("memory_write", REGISTRY)
        self.assertIn("memory_read", REGISTRY)

    def test_the_skill_tool_is_offered_to_the_model(self):
        specs = {spec.name: spec for spec in tool_specs(dict(REGISTRY))}
        self.assertIn("skill", specs)
        self.assertIn("name", specs["skill"].parameters["properties"])


class _FakeMCPTool:
    def __init__(self, server, remote_name):
        self.server = server
        self.remote_name = remote_name
        self.name = "mcp_%s_%s" % (server, remote_name)


class _FakeManager:
    def __init__(self, status=None, tools=(), warnings=()):
        self._status = status or {}
        self._tools = list(tools)
        self.warnings = list(warnings)

    def status(self):
        return dict(self._status)

    def tools(self):
        return list(self._tools)


class TestMCPView(unittest.TestCase):
    def manager(self):
        return _FakeManager(
            status={"files": "connected", "slow": "connecting",
                    "broken": "failed: spawn failed"},
            tools=[_FakeMCPTool("files", "read"),
                   _FakeMCPTool("files", "write")],
            warnings=["mcp broken: unusable entry, skipped"])

    def test_rows_carry_state_detail_and_tools(self):
        rows = {row["name"]: row for row in skills.mcp_rows(self.manager())}
        self.assertEqual(sorted(rows), ["broken", "files", "slow"])
        self.assertEqual(rows["files"]["state"], "connected")
        self.assertEqual(rows["files"]["tools"], 2)
        self.assertEqual(rows["files"]["tool_names"], ["read", "write"])
        self.assertEqual(rows["slow"]["state"], "connecting")
        self.assertEqual(rows["broken"]["state"], "failed")
        self.assertEqual(rows["broken"]["detail"], "spawn failed")

    def test_report_lists_servers_and_warnings(self):
        report = skills.mcp_report(self.manager())
        self.assertIn("MCP servers:", report)
        self.assertIn("files", report)
        self.assertIn("(2 tools)", report)
        self.assertIn("failed: spawn failed", report)
        self.assertIn("Warnings:", report)
        self.assertIn("unusable entry", report)

    def test_no_manager_and_no_servers_say_so(self):
        for manager in (None, _FakeManager()):
            report = skills.mcp_report(manager)
            self.assertIn("No MCP servers configured.", report)
            self.assertEqual(skills.mcp_rows(manager), [])

    def test_a_broken_manager_does_not_raise(self):
        class Exploding:
            warnings = ["boom"]

            def status(self):
                raise RuntimeError("no")

            def tools(self):
                raise RuntimeError("no")

        self.assertEqual(skills.mcp_rows(Exploding()), [])
        self.assertIn("No MCP servers configured.", skills.mcp_report(Exploding()))


if __name__ == "__main__":
    unittest.main()
