"""Tests for haikode.prompt — model-family prompt selection and assembly."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from haikode import prompt


class PromptTestCase(unittest.TestCase):
    """Every test starts and ends with a clean loader cache."""

    def setUp(self):
        prompt.clear_cache()

    def tearDown(self):
        prompt.clear_cache()


class TestSelection(PromptTestCase):

    def test_anthropic_family(self):
        for model in ("claude-sonnet-4-5", "claude-3-5-haiku-20241022",
                      "anthropic/claude-opus-4-5", "us.anthropic.claude-v2"):
            self.assertEqual(prompt.select_variant(model), "anthropic", model)

    def test_gpt5_family(self):
        for model in ("gpt-5", "gpt-5-mini", "gpt-5.1", "openai/gpt-5-pro"):
            self.assertEqual(prompt.select_variant(model), "gpt", model)

    def test_codex_models(self):
        # "gpt" + "codex" is the opencode rule; the bare id is haikode's addition.
        self.assertEqual(prompt.select_variant("gpt-5-codex"), "codex")
        self.assertEqual(prompt.select_variant("codex-mini-latest"), "codex")

    def test_beast_covers_gpt4_and_o_series(self):
        for model in ("gpt-4", "gpt-4o", "gpt-4.1", "o1", "o1-preview",
                      "o3-mini", "openai/o3"):
            self.assertEqual(prompt.select_variant(model), "beast", model)

    def test_gemini_family(self):
        for model in ("gemini-2.5-pro", "google/gemini-3-flash", "gemini"):
            self.assertEqual(prompt.select_variant(model), "gemini", model)

    def test_kimi_family(self):
        for model in ("kimi-k2", "moonshotai/Kimi-K2-Instruct"):
            self.assertEqual(prompt.select_variant(model), "kimi", model)

    def test_meta_and_trinity(self):
        self.assertEqual(prompt.select_variant("muse-spark-1"), "meta")
        self.assertEqual(prompt.select_variant("Trinity-Large"), "trinity")

    def test_ollama_style_ids(self):
        # Local ids carry no family signal, so they get the default variant --
        # except gpt-oss, whose name contains "gpt" and rides the gpt prompt.
        self.assertEqual(prompt.select_variant("glm-5.2"), "default")
        self.assertEqual(prompt.select_variant("qwen3-coder:480b"), "default")
        self.assertEqual(prompt.select_variant("llama3.3:70b"), "default")
        self.assertEqual(prompt.select_variant("deepseek-v3.2:latest"), "default")
        self.assertEqual(prompt.select_variant("gpt-oss:120b"), "gpt")

    def test_o_series_needs_a_boundary(self):
        # A bare "o3" inside a longer word must not hijack the beast prompt.
        self.assertEqual(prompt.select_variant("qwen3-coder:480b"), "default")
        self.assertEqual(prompt.select_variant("mytuneo3x-v1"), "default")

    def test_empty_and_none_model_fall_back(self):
        self.assertEqual(prompt.select_variant(""), "default")
        self.assertEqual(prompt.select_variant(None), "default")

    def test_selection_is_case_insensitive(self):
        self.assertEqual(prompt.select_variant("CLAUDE-SONNET-4-5"), "anthropic")
        self.assertEqual(prompt.select_variant("GPT-5-Codex"), "codex")


class TestVariantFiles(PromptTestCase):

    def test_available_lists_every_variant(self):
        names = prompt.available()
        self.assertIn("anthropic", names)
        self.assertIn("gpt", names)
        self.assertIn("codex", names)
        self.assertIn("beast", names)
        self.assertIn("gemini", names)
        self.assertIn("kimi", names)
        self.assertEqual(names[-1], "default")
        self.assertEqual(len(names), len(set(names)))

    def test_every_variant_file_exists_and_is_substantial(self):
        for name in prompt.available():
            text = prompt.load(name)
            self.assertGreater(len(text), 1000, name)
        self.assertEqual(prompt.LOAD_WARNINGS, [])

    def test_haiku_section_present_in_every_variant(self):
        for name in prompt.available():
            text = prompt.load(name)
            self.assertIn(prompt.HAIKU_MARKER, text, name)
            for marker in ("pkgman", "jam", "BeAPI", "-lbe", "-ltracker",
                           "teams", "/boot/home/config"):
                self.assertIn(marker, text, f"{name} missing {marker}")

    def test_haiku_section_present_in_every_assembled_prompt(self):
        for model in ("claude-sonnet-4-5", "gpt-5", "gpt-5-codex", "o3-mini",
                      "gemini-2.5-pro", "kimi-k2", "muse-spark-1",
                      "trinity-large", "glm-5.2"):
            text = prompt.select_prompt(model)
            self.assertIn(prompt.HAIKU_MARKER, text, model)
            self.assertIn("Do not launch GUI applications", text, model)

    def test_no_stray_opencode_branding(self):
        for name in prompt.available():
            self.assertNotIn("opencode", prompt.load(name).lower(), name)

    def test_haiku_section_is_not_duplicated(self):
        text = prompt.select_prompt("claude-sonnet-4-5")
        self.assertEqual(text.count(prompt.HAIKU_MARKER), 1)

    def test_load_accepts_bare_file_stems(self):
        self.assertIn(prompt.HAIKU_MARKER, prompt.load("haiku"))
        self.assertIn("Plan Mode", prompt.load("plan"))
        self.assertIn("plan to build", prompt.load("build-switch"))
        self.assertEqual(prompt.LOAD_WARNINGS, [])

    def test_prompts_readme_records_provenance(self):
        readme = (prompt.PROMPTS_DIR / "README.md").read_text()
        self.assertIn("opencode", readme)
        self.assertIn("MIT", readme)

    def test_haiku_pack_is_still_shipped(self):
        self.assertTrue((prompt.PROMPTS_DIR / "haiku-pack.md").exists())


class TestPlanMode(PromptTestCase):

    def test_plan_preamble_only_for_plan_agent(self):
        needle = "Plan Mode"
        self.assertIn(needle, prompt.select_prompt("claude-sonnet-4-5", "plan"))
        self.assertNotIn(needle, prompt.select_prompt("claude-sonnet-4-5", "build"))
        self.assertNotIn(needle, prompt.select_prompt("claude-sonnet-4-5"))
        self.assertNotIn(needle, prompt.select_prompt("gpt-5", "general"))

    def test_plan_preamble_comes_last(self):
        text = prompt.select_prompt("gpt-5", "plan")
        self.assertGreater(text.index("Plan Mode"), text.index(prompt.HAIKU_MARKER))
        self.assertTrue(text.rstrip().endswith("</system-reminder>"))

    def test_plan_agent_name_is_normalised(self):
        self.assertIn("Plan Mode", prompt.select_prompt("gpt-5", " Plan "))

    def test_plan_prompt_forbids_edits(self):
        self.assertIn("READ-ONLY", prompt.plan_preamble())

    def test_build_switch_releases_read_only(self):
        self.assertIn("no longer in read-only mode", prompt.build_switch())


class TestCaching(PromptTestCase):

    def test_load_is_cached_until_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("FIRST")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                self.assertEqual(prompt.load("default"), "FIRST")
                (directory / "system.md").write_text("SECOND")
                self.assertEqual(prompt.load("default"), "FIRST")
                prompt.clear_cache()
                self.assertEqual(prompt.load("default"), "SECOND")

    def test_clear_cache_also_drops_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                prompt.load("anthropic")
                self.assertTrue(prompt.LOAD_WARNINGS)
                prompt.clear_cache()
                self.assertEqual(prompt.LOAD_WARNINGS, [])


class TestDegradedInstall(PromptTestCase):

    def test_missing_variant_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                self.assertEqual(prompt.load("anthropic"), "DEFAULT TEXT")
                self.assertTrue(any("anthropic.md" in w
                                    for w in prompt.LOAD_WARNINGS))

    def test_unknown_variant_name_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                self.assertEqual(prompt.load("no-such-prompt"), "DEFAULT TEXT")
                self.assertTrue(any("no-such-prompt" in w
                                    for w in prompt.LOAD_WARNINGS))

    def test_empty_prompt_dir_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                text = prompt.select_prompt("claude-sonnet-4-5", "plan")
                self.assertIn("haikode", text)
                self.assertIn(prompt.HAIKU_MARKER, text)
                self.assertIn("pkgman", text)
                self.assertTrue(prompt.LOAD_WARNINGS)

    def test_missing_haiku_file_still_yields_a_haiku_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("NO HAIKU SECTION HERE")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                text = prompt.select_prompt("glm-5.2")
                self.assertTrue(text.startswith("NO HAIKU SECTION HERE"))
                self.assertIn(prompt.HAIKU_MARKER, text)
                self.assertIn("pkgman", text)


class TestNameConfinement(PromptTestCase):
    """A prompt name can come from an agent file that arrived with a repo."""

    def test_absolute_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT")
            secret = directory / "secret.md"
            secret.write_text("SSH KEY")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                self.assertEqual(prompt.load(str(secret)), "DEFAULT TEXT")
                self.assertNotIn("SSH KEY", prompt.load(str(secret)))
                self.assertTrue(any("refused" in w
                                    for w in prompt.LOAD_WARNINGS))

    def test_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "prompts"
            directory.mkdir()
            (directory / "system.md").write_text("DEFAULT TEXT")
            (Path(tmp) / "outside.md").write_text("OUTSIDE")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                for name in ("../outside", "../outside.md", "..\\outside",
                             "sub/../../outside"):
                    self.assertEqual(prompt.load(name), "DEFAULT TEXT", name)

    def test_refused_names_are_not_cached(self):
        # Otherwise a repo full of hostile agent names grows the cache for free.
        prompt.clear_cache()
        prompt.load("../../etc/hosts")
        self.assertEqual(list(prompt._CACHE), ["default"])

    def test_undecodable_file_degrades_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT")
            (directory / "anthropic.md").write_bytes(b"\xff\xfe\x00binary")
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                self.assertEqual(prompt.load("anthropic"), "DEFAULT TEXT")


class TestAuxiliaryFallbacks(PromptTestCase):
    """plan/build-switch get injected as reminders, so they must never
    degrade into a second copy of the whole system prompt."""

    def test_missing_plan_file_does_not_yield_the_default_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT " * 200)
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                text = prompt.plan_preamble()
                self.assertNotIn("DEFAULT TEXT", text)
                self.assertIn("READ-ONLY", text)
                self.assertLess(len(text), 800)

    def test_missing_build_switch_does_not_yield_the_default_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT " * 200)
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                text = prompt.build_switch()
                self.assertNotIn("DEFAULT TEXT", text)
                self.assertIn("no longer in read-only mode", text)

    def test_missing_haiku_file_does_not_yield_the_default_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "system.md").write_text("DEFAULT TEXT " * 200)
            with mock.patch.object(prompt, "PROMPTS_DIR", directory):
                prompt.clear_cache()
                text = prompt.haiku_section()
                self.assertNotIn("DEFAULT TEXT", text)
                self.assertIn(prompt.HAIKU_MARKER, text)


class TestPlanModeText(PromptTestCase):

    def test_plan_mode_substitutes_the_placeholder(self):
        text = prompt.plan_mode("No plan file exists yet.")
        self.assertNotIn("${planInfo}", text)
        self.assertIn("No plan file exists yet.", text)

    def test_plan_mode_default_leaves_no_placeholder(self):
        self.assertNotIn("${planInfo}", prompt.plan_mode())


class TestCacheKeys(PromptTestCase):

    def test_file_stem_and_variant_name_share_one_entry(self):
        self.assertEqual(prompt.load("system"), prompt.load("default"))
        self.assertEqual(prompt.load("default.md"), prompt.load("default"))
        self.assertEqual(len(prompt._CACHE), 1)
        self.assertEqual(prompt.LOAD_WARNINGS, [])

    def test_name_lookup_is_case_and_space_insensitive(self):
        self.assertEqual(prompt.load(" Anthropic.MD "), prompt.load("anthropic"))
        self.assertEqual(prompt.LOAD_WARNINGS, [])


class TestAgentPrompt(PromptTestCase):
    """An agent definition with its own prompt replaces the family prompt,
    matching opencode session/llm/request.ts:60."""

    def test_agent_prompt_replaces_the_variant(self):
        text = prompt.select_prompt("claude-sonnet-4-5", "build",
                                    agent_prompt="You review diffs. Nothing else.")
        self.assertTrue(text.startswith("You review diffs. Nothing else."))
        self.assertNotIn("the best coding agent on the planet", text)

    def test_agent_prompt_still_gets_the_haiku_briefing(self):
        text = prompt.select_prompt("gpt-5", "build",
                                    agent_prompt="You review diffs.")
        self.assertIn(prompt.HAIKU_MARKER, text)
        self.assertIn("pkgman", text)

    def test_agent_prompt_keeps_its_own_haiku_section(self):
        own = "You review diffs.\n\n# Haiku OS\nBespoke briefing."
        text = prompt.select_prompt("gpt-5", "build", agent_prompt=own)
        self.assertEqual(text.count(prompt.HAIKU_MARKER), 1)

    def test_blank_agent_prompt_falls_back_to_the_variant(self):
        self.assertEqual(prompt.select_prompt("gpt-5", "build", "   \n "),
                         prompt.select_prompt("gpt-5", "build"))

    def test_plan_reminder_still_wins_over_an_agent_prompt(self):
        text = prompt.select_prompt("gpt-5", "plan",
                                    agent_prompt="You review diffs.")
        self.assertIn("Plan Mode", text)
        self.assertGreater(text.index("Plan Mode"), text.index("You review"))

    def test_agent_prompt_flows_through_build_system_prompt(self):
        text = prompt.build_system_prompt("gpt-5", "build", agent_prompt="X only.",
                                          environment="<env></env>")
        self.assertTrue(text.startswith("X only."))
        self.assertIn("<env></env>", text)


class TestBuildSystemPrompt(PromptTestCase):

    def test_order_is_variant_environment_instructions(self):
        text = prompt.build_system_prompt(
            "claude-sonnet-4-5",
            instructions="Always run jam before claiming success.",
            environment="<env>\nWorking directory: /boot/home/dev\n</env>",
        )
        variant_at = text.index("You are haikode")
        env_at = text.index("<env>")
        instr_at = text.index("# Project instructions")
        self.assertLess(variant_at, env_at)
        self.assertLess(env_at, instr_at)
        self.assertIn("Always run jam before claiming success.", text)

    def test_empty_sections_are_dropped(self):
        text = prompt.build_system_prompt("claude-sonnet-4-5")
        self.assertNotIn("# Project instructions", text)
        self.assertFalse(text.endswith("\n\n"))
        self.assertEqual(
            text,
            prompt.select_prompt("claude-sonnet-4-5")
            + "\n\n" + prompt._CONFIG_GUIDANCE,
        )

    def test_whitespace_only_instructions_are_dropped(self):
        text = prompt.build_system_prompt("gpt-5", instructions="   \n\n ",
                                          environment="  ")
        self.assertNotIn("# Project instructions", text)
        self.assertEqual(
            text,
            prompt.select_prompt("gpt-5") + "\n\n" + prompt._CONFIG_GUIDANCE,
        )

    def test_matches_the_agent_assembly(self):
        # agent.py joins its stable guidance, environment and instructions with
        # a blank line; build_system_prompt must be a drop-in for that.
        environment = "<env>\nPlatform: haiku\n</env>"
        instructions = "Use jam."
        expected = "\n\n".join([
            prompt.select_prompt("gpt-5", "build"),
            prompt._CONFIG_GUIDANCE,
            environment,
            "# Project instructions\n" + instructions,
        ])
        self.assertEqual(
            prompt.build_system_prompt("gpt-5", "build", instructions, environment),
            expected,
        )

    def test_plan_agent_flows_through(self):
        text = prompt.build_system_prompt("claude-sonnet-4-5", "plan",
                                          environment="<env></env>")
        self.assertIn("Plan Mode", text)
        self.assertIn("<env></env>", text)
        # The environment block still follows the system prompt, plan and all.
        self.assertLess(text.index("Plan Mode"), text.index("<env></env>"))


if __name__ == "__main__":
    unittest.main()
