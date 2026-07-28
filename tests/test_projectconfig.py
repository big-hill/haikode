import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode.config import Config
from haikode.permission import Permissions, PermissionRequest
from haikode.projectconfig import (MAX_CONFIG_BYTES, MAX_INSTRUCTION_FILES,
                                   ProjectConfig, discover_files,
                                   init_project_config, project_root)
from haikode.schema import PermissionDenied


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


class ProjectConfigTestCase(unittest.TestCase):
    """Every test runs inside a fake git project with an isolated global dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

        self.global_dir = self.tmp / "global"
        self.global_dir.mkdir()
        patcher = patch("haikode.projectconfig.global_config_dir",
                        return_value=self.global_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.root = self.tmp / "project"
        (self.root / ".git").mkdir(parents=True)
        self.nested = self.root / "src" / "deep"
        self.nested.mkdir(parents=True)

    def load(self, cwd=None, global_config=None, trusted=None) -> ProjectConfig:
        return ProjectConfig.load(str(cwd or self.root), global_config,
                                  trusted=trusted)


class DiscoveryTests(ProjectConfigTestCase):
    def test_nearer_the_cwd_wins(self):
        write_json(self.root / "haikode.json", {"model": "root", "shell": "bash"})
        write_json(self.nested / "haikode.json", {"model": "deep"})

        config = self.load(self.nested)

        self.assertEqual(config.data["model"], "deep")
        self.assertEqual(config.data["shell"], "bash")
        self.assertEqual([p.parent for p in config.sources],
                         [self.root, self.nested])

    def test_sources_are_listed_weakest_first(self):
        write_json(self.global_dir / "haikode.json", {"model": "global"})
        write_json(self.root / "haikode.json", {"model": "project"})

        config = self.load()

        self.assertEqual(config.sources,
                         [self.global_dir / "haikode.json",
                          self.root / "haikode.json"])
        self.assertEqual(config.data["model"], "project")

    def test_global_config_is_the_weakest_layer(self):
        write_json(self.global_dir / "haikode.json",
                   {"theme": "haiku", "max_steps": 5})
        write_json(self.root / "haikode.json", {"max_steps": 9})

        config = self.load()

        self.assertEqual(config.data["theme"], "haiku")
        self.assertEqual(config.data["max_steps"], 9)

    def test_dot_directory_beats_the_plain_file(self):
        write_json(self.root / "haikode.json", {"model": "plain"})
        write_json(self.root / ".haikode" / "haikode.json", {"model": "dotdir"})

        self.assertEqual(self.load().data["model"], "dotdir")

    def test_discovery_stops_at_the_git_root(self):
        write_json(self.tmp / "haikode.json", {"model": "outside"})
        write_json(self.root / "haikode.json", {"model": "inside"})

        config = self.load(self.nested)

        self.assertEqual(config.data["model"], "inside")
        self.assertNotIn(self.tmp / "haikode.json", config.sources)

    def test_explicit_stop_overrides_the_git_boundary(self):
        write_json(self.tmp / "haikode.json", {"model": "outside"})

        found = discover_files(str(self.root), stop=str(self.tmp))

        self.assertIn(self.tmp / "haikode.json", found)

    def test_stop_that_is_not_an_ancestor_is_ignored(self):
        # Honouring it would turn discovery into "read every config between /
        # and here"; the git root must still bound the walk.
        outside = self.tmp / "elsewhere"
        outside.mkdir()

        self.assertEqual(project_root(str(self.nested), stop=str(outside)),
                         self.root)
        found = discover_files(str(self.nested), stop=str(outside))

        self.assertTrue(all(str(p).startswith(str(self.root))
                            or str(p).startswith(str(self.global_dir))
                            for p in found), found)

    def test_oversized_file_is_refused_without_reading_it(self):
        path = self.root / "haikode.json"
        path.write_text("{" + " " * (MAX_CONFIG_BYTES + 1) + "}")

        config = self.load()

        self.assertEqual(config.data, {})
        self.assertIn("too large", config.errors[0])

    def test_missing_files_are_not_errors(self):
        config = self.load()

        self.assertEqual(config.sources, [])
        self.assertEqual(config.errors, [])
        self.assertEqual(config.data, {})


class OpencodeCompatTests(ProjectConfigTestCase):
    def test_opencode_json_is_read(self):
        write_json(self.root / "opencode.json",
                   {"model": "anthropic/claude-2", "shell": "/bin/zsh"})

        config = self.load()

        self.assertEqual(config.data["model"], "anthropic/claude-2")
        self.assertEqual(config.resolve_model(), ("anthropic", "claude-2"))

    def test_native_file_wins_over_opencode_file(self):
        write_json(self.root / "opencode.json", {"model": "compat"})
        write_json(self.root / "haikode.json", {"model": "native"})

        config = self.load()

        self.assertEqual(config.data["model"], "native")
        self.assertEqual(len(config.sources), 2)

    def test_singular_opencode_names_are_aliased(self):
        write_json(self.root / "opencode.json", {
            "agent": {"review": {"description": "reviewer"}},
            "command": {"ship": {"template": "ship it"}},
        })

        config = self.load()

        self.assertIn("review", config.data["agents"])
        self.assertEqual(config.data["commands"]["ship"]["template"], "ship it")

    def test_opencode_provider_record_becomes_providers(self):
        # `model` and not `options`: opencode hides baseURL/apiKey in options,
        # so that key is PRIVILEGED and a project file never keeps it. See
        # test_trust.ProviderRedirection for the refusal itself.
        write_json(self.root / "opencode.json",
                   {"provider": {"anthropic": {"model": "claude-x"}}})

        config = self.load()

        self.assertEqual(config.data["providers"]["anthropic"]["model"], "claude-x")
        self.assertNotIn("provider", config.data)

    def test_unsupported_opencode_keys_are_not_reported(self):
        write_json(self.root / "opencode.json",
                   {"plugin": ["x"], "lsp": {}, "share": "manual", "$schema": "u"})

        config = self.load()

        self.assertEqual(config.unknown, [])
        self.assertEqual(config.errors, [])


class MergeTests(ProjectConfigTestCase):
    def test_dicts_deep_merge_and_lists_replace(self):
        write_json(self.root / "haikode.json", {
            "permission": {"bash": "ask", "edit": "allow"},
            "tools": {"webfetch": False},
        })
        write_json(self.nested / "haikode.json", {
            "permission": {"bash": "deny"},
            "tools": {"task": False},
        })

        config = self.load(self.nested)

        self.assertEqual(config.data["permission"],
                         {"bash": "deny", "edit": "allow"})
        self.assertEqual(config.data["tools"], {"webfetch": False, "task": False})

    def test_instructions_concatenate_across_layers(self):
        write_json(self.root / "haikode.json", {"instructions": ["a.md"]})
        write_json(self.nested / "haikode.json", {"instructions": ["b.md"]})

        config = self.load(self.nested)

        self.assertEqual(config.data["instructions"], ["a.md", "b.md"])

    def test_merged_with_neither_mutates_nor_aliases_the_project_data(self):
        write_json(self.root / "haikode.json", {
            "model": "acme/x", "context": 9,
            "providers": {"acme": {}}, "agents": {"a": {"m": 1}},
        })
        config = self.load()
        before = json.dumps(config.data, sort_keys=True)

        merged = config.merged_with({"default_provider": "acme"})
        merged["agents"]["a"]["m"] = "clobbered"

        self.assertEqual(json.dumps(config.data, sort_keys=True), before)

    def test_merged_with_global_config(self):
        write_json(self.root / "haikode.json",
                   {"model": "anthropic/claude-x", "context": 4242})
        global_config = Config(path=str(self.tmp / "config.json"))

        merged = self.load().merged_with(global_config)

        self.assertEqual(merged["default_provider"], "anthropic")
        self.assertEqual(merged["providers"]["anthropic"]["model"], "claude-x")
        self.assertEqual(merged["providers"]["anthropic"]["context"], 4242)
        # the live global config must not be touched
        self.assertEqual(global_config.data["providers"]["anthropic"]["model"],
                         "claude-sonnet-5")
        self.assertEqual(global_config.data["default_provider"], "ollama")

    def test_merged_with_uses_global_config_from_load(self):
        write_json(self.root / "haikode.json", {"provider": "openai"})
        global_config = Config(path=str(self.tmp / "config.json"))

        merged = self.load(global_config=global_config).merged_with()

        self.assertEqual(merged["default_provider"], "openai")

    def test_global_permission_loses_to_project_but_beats_nothing_else(self):
        write_json(self.root / "haikode.json", {"permission": {"bash": "deny"}})
        source = types.SimpleNamespace(
            data={"permission": {"bash": "allow", "webfetch": "allow"}})

        merged = self.load().merged_with(source)

        self.assertEqual(merged["permission"]["bash"], "deny")
        self.assertEqual(merged["permission"]["webfetch"], "allow")


class DegradationTests(ProjectConfigTestCase):
    def test_malformed_json_is_reported_not_raised(self):
        write_json(self.root / "haikode.json", "{ this is not json")
        write_json(self.nested / "haikode.json", {"model": "still-works"})

        config = self.load(self.nested)

        self.assertEqual(config.data["model"], "still-works")
        self.assertEqual(len(config.errors), 1)
        self.assertIn("invalid JSON", config.errors[0])
        self.assertIn(str(self.root / "haikode.json"), config.errors[0])

    def test_non_object_top_level_is_reported(self):
        write_json(self.root / "haikode.json", ["nope"])

        config = self.load()

        self.assertEqual(config.data, {})
        self.assertIn("expected a JSON object", config.errors[0])

    def test_empty_file_is_not_an_error(self):
        write_json(self.root / "haikode.json", "   \n")

        config = self.load()

        self.assertEqual(config.errors, [])

    def test_unknown_keys_are_collected_not_fatal(self):
        write_json(self.root / "haikode.json",
                   {"model": "m", "wat": 1, "_note": "ignored"})

        config = self.load()

        self.assertEqual(config.data["model"], "m")
        self.assertEqual(config.unknown,
                         [f"{self.root / 'haikode.json'}: wat"])
        self.assertEqual(config.errors, [])

    def test_bad_shapes_are_dropped_with_an_error(self):
        write_json(self.root / "haikode.json", {
            "agents": {"good": {"model": "m"}, "bad": "nope"},
            "commands": {"ok": {"template": "t"}, "broken": {"description": "d"}},
            "tools": {"bash": "false"},
            "max_steps": True,
            "permission": {"bash": "maybe"},
        })

        config = self.load()

        self.assertEqual(list(config.data["agents"]), ["good"])
        self.assertEqual(list(config.data["commands"]), ["ok"])
        self.assertEqual(config.data["tools"], {})
        self.assertNotIn("max_steps", config.data)
        self.assertEqual(config.data["permission"], {})
        self.assertEqual(len(config.errors), 5)


class InstructionTests(ProjectConfigTestCase):
    def test_resolved_relative_to_the_declaring_file(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "style.md").write_text("style")
        (self.nested / "local.md").write_text("local")
        write_json(self.root / "haikode.json", {"instructions": ["docs/style.md"]})
        write_json(self.nested / "haikode.json", {"instructions": ["local.md"]})

        resolved = self.load(self.nested).resolve_instructions()

        self.assertEqual(resolved, [(self.root / "docs" / "style.md"),
                                    (self.nested / "local.md")])

    def test_globs_are_expanded(self):
        docs = self.root / "docs"
        docs.mkdir()
        for name in ("b.md", "a.md", "skip.txt"):
            (docs / name).write_text(name)
        write_json(self.root / "haikode.json", {"instructions": ["docs/*.md"]})

        resolved = self.load().resolve_instructions()

        self.assertEqual([p.name for p in resolved], ["a.md", "b.md"])

    def test_recursive_glob_and_deduplication(self):
        deep = self.root / "docs" / "sub"
        deep.mkdir(parents=True)
        (deep / "one.md").write_text("one")
        write_json(self.root / "haikode.json",
                   {"instructions": ["docs/**/*.md", "docs/sub/one.md"]})

        resolved = self.load().resolve_instructions()

        self.assertEqual(resolved, [deep / "one.md"])

    def test_missing_files_and_directories_are_skipped(self):
        (self.root / "adir").mkdir()
        write_json(self.root / "haikode.json",
                   {"instructions": ["gone.md", "adir"]})

        self.assertEqual(self.load().resolve_instructions(), [])

    def test_cap_at_32_files_with_overflow_reported(self):
        notes = self.root / "notes"
        notes.mkdir()
        for index in range(MAX_INSTRUCTION_FILES + 8):
            (notes / f"{index:03d}.md").write_text("x")
        write_json(self.root / "haikode.json", {"instructions": ["notes/*.md"]})

        config = self.load()
        resolved = config.resolve_instructions()

        self.assertEqual(len(resolved), MAX_INSTRUCTION_FILES)
        self.assertEqual(len(config.warnings), 1)
        self.assertIn("8 file(s) past", config.warnings[0])
        # repeated calls must not stack duplicate warnings
        config.resolve_instructions()
        self.assertEqual(len(config.warnings), 1)


class InstructionContainmentTests(ProjectConfigTestCase):
    """A config that arrived with a checkout must not read outside the project."""

    def setUp(self):
        super().setUp()
        self.secret = self.tmp / "secret.md"
        self.secret.write_text("private key")

    def test_parent_traversal_is_refused(self):
        write_json(self.root / "haikode.json",
                   {"instructions": ["../secret.md"]})

        config = self.load()

        self.assertEqual(config.resolve_instructions(), [])
        self.assertIn("outside the project", config.warnings[0])

    def test_absolute_path_outside_the_project_is_refused(self):
        write_json(self.root / "haikode.json",
                   {"instructions": [str(self.secret)]})

        self.assertEqual(self.load().resolve_instructions(), [])

    def test_symlink_out_of_the_project_is_refused(self):
        link = self.root / "notes.md"
        link.symlink_to(self.secret)
        write_json(self.root / "haikode.json", {"instructions": ["notes.md"]})

        self.assertEqual(self.load().resolve_instructions(), [])

    def test_caller_can_opt_out(self):
        write_json(self.root / "haikode.json",
                   {"instructions": ["../secret.md"]})

        resolved = self.load().resolve_instructions(allow_outside=True)

        self.assertEqual(resolved, [self.secret.resolve()])

    def test_the_users_own_global_config_is_not_restricted(self):
        write_json(self.global_dir / "haikode.json",
                   {"instructions": [str(self.secret)]})

        self.assertEqual(self.load().resolve_instructions(),
                         [self.secret.resolve()])

    def test_absolute_glob_does_not_walk_the_filesystem(self):
        # "/**/*.md" must glob the basename inside "/**" (opencode's rule),
        # not turn into a whole-disk scan.
        write_json(self.root / "haikode.json", {"instructions": ["/**/*.md"]})

        self.assertEqual(self.load().resolve_instructions(), [])

    def test_url_instructions_are_skipped(self):
        write_json(self.root / "haikode.json",
                   {"instructions": ["https://example.com/AGENTS.md"]})

        config = self.load()

        self.assertEqual(config.resolve_instructions(), [])
        self.assertEqual(config.errors, [])


class EscalationTests(ProjectConfigTestCase):
    def test_project_widening_is_reported(self):
        write_json(self.root / "haikode.json", {
            "permission": {"bash": "allow", "edit": {"src/*": "allow"}},
            "tools": {"webfetch": True, "write": False},
        })

        keys = [(e.key, e.pattern) for e in self.load().escalations()]

        self.assertIn(("bash", None), keys)
        self.assertIn(("edit", "src/*"), keys)
        self.assertIn(("webfetch", None), keys)
        # tightening is not an escalation
        self.assertNotIn(("write", None), keys)

    def test_tightening_only_config_has_no_escalations(self):
        write_json(self.root / "haikode.json",
                   {"permission": {"bash": "deny", "read": "ask"}})

        self.assertEqual(self.load().escalations(), [])

    def test_the_users_own_global_config_raises_the_baseline(self):
        write_json(self.global_dir / "haikode.json",
                   {"permission": {"bash": "allow"}})
        write_json(self.root / "haikode.json", {"permission": {"bash": "allow"}})

        self.assertEqual(self.load().escalations(), [])

    def test_hardened_permissions_drop_widenings_and_keep_tightenings(self):
        write_json(self.root / "haikode.json", {
            "permission": {"bash": "allow", "edit": "deny"},
            "tools": {"webfetch": True, "write": False},
        })

        rules = self.load().effective_permissions(allow_escalation=False)

        self.assertNotIn("bash", rules)
        self.assertNotIn("webfetch", rules)
        self.assertEqual(rules["edit"], "deny")
        self.assertEqual(rules["write"], "deny")

    def test_agent_level_widening_is_reported(self):
        write_json(self.root / "haikode.json",
                   {"agents": {"sneaky": {"permission": {"bash": "allow"}}}})

        found = self.load().escalations()

        self.assertEqual([(e.agent, e.key) for e in found], [("sneaky", "bash")])
        self.assertIn("agents.sneaky.permission.bash", found[0].message)

    def test_agent_level_rules_are_not_filtered_out_of_permissions(self):
        # They are not part of the returned ruleset in the first place; the
        # agents module has to apply the report itself.
        write_json(self.root / "haikode.json",
                   {"agents": {"sneaky": {"permission": {"bash": "allow"}}}})

        rules = self.load().effective_permissions(allow_escalation=False)

        self.assertEqual(rules, {})

    def test_escalations_show_up_in_describe(self):
        write_json(self.root / "haikode.json", {"permission": {"bash": "allow"}})

        report = "\n".join(self.load().describe())

        self.assertIn("widens ask to allow", report)

    def test_merged_with_can_harden(self):
        write_json(self.root / "haikode.json", {"permission": {"bash": "allow"}})

        merged = self.load().merged_with({}, allow_escalation=False)

        self.assertNotIn("bash", merged["permission"])


class ToolAndPermissionTests(ProjectConfigTestCase):
    def test_enabled_tools_filters_disabled_entries(self):
        write_json(self.root / "haikode.json",
                   {"tools": {"bash": False, "read": True}})

        names = self.load().enabled_tools(["read", "bash", "edit"])

        self.assertEqual(names, ["read", "edit"])

    def test_enabled_tools_without_config_keeps_everything(self):
        self.assertEqual(self.load().enabled_tools(["a", "b"]), ["a", "b"])

    def test_enabled_tools_longest_pattern_wins(self):
        write_json(self.root / "haikode.json",
                   {"tools": {"mcp_*": False, "mcp_keep": True}})

        names = self.load().enabled_tools(["read", "mcp_drop", "mcp_keep"])

        self.assertEqual(names, ["read", "mcp_keep"])

    def test_enabled_tools_exact_name_beats_a_longer_glob(self):
        write_json(self.root / "haikode.json",
                   {"tools": {"read*": False, "read": True}})

        self.assertEqual(self.load().enabled_tools(["read", "readfile"]),
                         ["read"])

    def test_effective_permissions_folds_in_the_tools_map(self):
        # Trusted, because switching webfetch on is a widening: what an
        # untrusted checkout gets instead is in test_trust.PermissionLoosening.
        write_json(self.root / "haikode.json", {
            "tools": {"bash": False, "webfetch": True},
            "permission": {"edit": {"src/*": "allow", "*": "ask"}},
        })

        rules = self.load(trusted=True).effective_permissions()

        self.assertEqual(rules["bash"], "deny")
        self.assertEqual(rules["webfetch"], "allow")
        self.assertEqual(rules["edit"], {"src/*": "allow", "*": "ask"})

    def test_explicit_permission_beats_the_tools_map(self):
        write_json(self.root / "haikode.json", {
            "tools": {"bash": False},
            "permission": {"bash": "allow"},
        })

        self.assertEqual(
            self.load(trusted=True).effective_permissions()["bash"], "allow")
        # ...but only for a project the user vouched for.
        self.assertEqual(self.load().effective_permissions()["bash"], "deny")

    def test_permission_string_applies_to_every_key(self):
        write_json(self.root / "haikode.json", {"permission": "deny"})

        rules = self.load().effective_permissions()

        self.assertEqual(rules["bash"], "deny")
        self.assertEqual(rules["read"], "deny")

    def test_glob_tool_keys_do_not_become_permission_keys(self):
        write_json(self.root / "haikode.json", {"tools": {"mcp_*": False}})

        self.assertEqual(self.load().effective_permissions(), {})

    def test_result_is_consumable_by_the_permission_engine(self):
        write_json(self.root / "haikode.json", {"tools": {"bash": False}})
        shim = types.SimpleNamespace(
            data={"permission": self.load().effective_permissions()})

        permissions = Permissions(config=shim)

        with self.assertRaises(PermissionDenied):
            permissions.ask(PermissionRequest("bash", ["rm -rf /"], "danger"))


class InitTests(ProjectConfigTestCase):
    def test_creates_a_documented_skeleton(self):
        path = init_project_config(str(self.root))

        payload = json.loads(path.read_text())
        self.assertEqual(path, self.root / "haikode.json")
        self.assertIn("_comment", payload)
        self.assertEqual(payload["permission"], {})
        # the comment key must not come back as an unknown-key warning
        self.assertEqual(self.load().unknown, [])

    def test_settings_are_written(self):
        path = init_project_config(str(self.root), model="anthropic/claude-x",
                                   instructions=["docs/*.md"])

        payload = json.loads(path.read_text())
        self.assertEqual(payload["model"], "anthropic/claude-x")
        self.assertEqual(payload["instructions"], ["docs/*.md"])
        self.assertNotIn("tools", payload)

    def test_refuses_to_clobber(self):
        write_json(self.root / "haikode.json", {"model": "precious"})

        with self.assertRaises(FileExistsError):
            init_project_config(str(self.root))

        self.assertEqual(self.load().data["model"], "precious")

    def test_overwrite_is_explicit(self):
        write_json(self.root / "haikode.json", {"model": "old"})

        init_project_config(str(self.root), overwrite=True, model="new")

        self.assertEqual(self.load().data["model"], "new")

    def test_unsupported_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            init_project_config(str(self.root), nonsense=1)
        self.assertFalse((self.root / "haikode.json").exists())

    def test_bad_setting_value_is_rejected_before_writing(self):
        # Otherwise /init happily writes a file the next load would reject.
        with self.assertRaises(ValueError):
            init_project_config(str(self.root), max_steps="lots")
        self.assertFalse((self.root / "haikode.json").exists())


class DescribeTests(ProjectConfigTestCase):
    def test_reports_sources_warnings_and_errors(self):
        (self.root / "guide.md").write_text("guide")
        write_json(self.root / "haikode.json", {
            "model": "anthropic/claude-x",
            "instructions": ["guide.md"],
            "tools": {"bash": False},
            "wat": True,
        })
        write_json(self.nested / "haikode.json", "{oops")

        report = "\n".join(self.load(self.nested).describe())

        self.assertIn(str(self.root / "haikode.json"), report)
        self.assertIn("anthropic/claude-x", report)
        self.assertIn("guide.md", report)
        self.assertIn("Disabled tools: bash", report)
        self.assertIn("unknown key", report)
        self.assertIn("Error:", report)

    def test_describe_without_any_config(self):
        report = self.load().describe()

        self.assertTrue(report[0].startswith("Project config for"))
        self.assertIn("Loaded: none (using defaults)", report)


if __name__ == "__main__":
    unittest.main()
