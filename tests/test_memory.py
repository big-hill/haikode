import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode.memory import (INDEX_NAME, MEMORY_TOOLS, Memory, MemoryReadTool,
                            MemoryStore, MemoryWriteTool, derive_description,
                            derive_name, normalize_scope, parse_memory,
                            parse_quick_capture, slugify)
from haikode.permission import PermissionDenied, Permissions
from haikode.tool.base import ToolContext


class StoreTestCase(unittest.TestCase):
    """Every store in these tests is fully sandboxed: neither the real global
    config dir nor the real project is ever touched."""

    def setUp(self):
        self._project = tempfile.TemporaryDirectory()
        self._global = tempfile.TemporaryDirectory()
        self.addCleanup(self._project.cleanup)
        self.addCleanup(self._global.cleanup)
        self.project_root = Path(self._project.name).resolve()
        self.global_root = Path(self._global.name).resolve()
        self.store = MemoryStore(str(self.project_root),
                                 global_dir=str(self.global_root / "memory"))

    @property
    def project_dir(self) -> Path:
        return self.store.project_dir

    @property
    def user_dir(self) -> Path:
        return self.store.global_dir


class RoundTripTests(StoreTestCase):
    def test_write_get_delete_round_trip(self):
        memory = self.store.write("The test suite runs with unittest discover.",
                                  name="test-command",
                                  description="How to run the tests",
                                  tags=["build", "tests"])

        self.assertEqual(memory.name, "test-command")
        self.assertEqual(memory.scope, "project")
        self.assertEqual(memory.path, self.project_dir / "test-command.md")
        self.assertTrue(memory.path.exists())

        loaded = self.store.get("test-command")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.body, "The test suite runs with unittest discover.")
        self.assertEqual(loaded.description, "How to run the tests")
        self.assertEqual(loaded.tags, ("build", "tests"))
        self.assertEqual(loaded.created, memory.created)
        self.assertEqual(loaded.updated, memory.updated)

        self.assertTrue(self.store.delete("test-command"))
        self.assertIsNone(self.store.get("test-command"))
        self.assertFalse(memory.path.exists())
        self.assertFalse(self.store.delete("test-command"))

    def test_frontmatter_fidelity_on_disk(self):
        memory = self.store.write("Body text.", name="fidelity",
                                  description="A description: with a colon",
                                  tags=("a", "b"))
        raw = memory.path.read_text()

        self.assertTrue(raw.startswith("---\n"))
        parsed = parse_memory(raw, path=memory.path)
        self.assertEqual(parsed.name, "fidelity")
        self.assertEqual(parsed.description, "A description: with a colon")
        self.assertEqual(parsed.scope, "project")
        self.assertEqual(parsed.tags, ("a", "b"))
        self.assertEqual(parsed.created, memory.created)
        self.assertEqual(parsed.updated, memory.updated)
        self.assertEqual(parsed.body, "Body text.")

    def test_multi_line_body_survives(self):
        body = "First line.\n\n- bullet one\n- bullet two"
        self.store.write(body, name="multi")

        self.assertEqual(self.store.get("multi").body, body)

    def test_plain_markdown_file_without_frontmatter_is_readable(self):
        self.project_dir.mkdir(parents=True)
        (self.project_dir / "hand-written.md").write_text("A note typed by a human.\n")

        loaded = self.store.get("hand-written")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, "hand-written")
        self.assertEqual(loaded.body, "A note typed by a human.")
        self.assertEqual(loaded.description, "A note typed by a human.")
        self.assertEqual(self.store.warnings, [])

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.write("   \n  ")


class NamingTests(StoreTestCase):
    def test_name_is_derived_from_first_sentence(self):
        memory = self.store.write(
            "Deploy with tar over ssh. scp hangs on this box.")

        self.assertEqual(memory.name, "deploy-with-tar-over-ssh")

    def test_derived_name_folds_non_ascii(self):
        self.assertEqual(derive_name("Bruk høyre side når du blar"),
                         "bruk-hoyre-side-nar-du-blar")

    def test_derived_name_is_truncated_at_a_word_boundary(self):
        name = derive_name("this sentence keeps going and going and going "
                           "well past the maximum name length allowed")

        self.assertLessEqual(len(name), 48)
        self.assertFalse(name.endswith("-"))
        self.assertTrue(name.startswith("this-sentence-keeps-going"))

    def test_explicit_name_is_slugified(self):
        memory = self.store.write("Body", name="Some Name/With Junk!")

        self.assertEqual(memory.name, "some-name-with-junk")

    def test_unnameable_text_falls_back(self):
        self.assertEqual(derive_name("!!! ???"), "memory")

    def test_explicit_name_collision_updates_in_place(self):
        first = self.store.write("Old text.", name="conventions")
        second = self.store.write("New text.", name="conventions")

        self.assertEqual(second.name, "conventions")
        self.assertEqual(second.created, first.created)
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.get("conventions").body, "New text.")

    def test_update_bumps_updated_timestamp(self):
        with patch("haikode.memory._timestamp", return_value="2026-01-01T00:00:00Z"):
            self.store.write("Old text.", name="stamped")
        with patch("haikode.memory._timestamp", return_value="2026-02-02T00:00:00Z"):
            updated = self.store.write("New text.", name="stamped")

        self.assertEqual(updated.created, "2026-01-01T00:00:00Z")
        self.assertEqual(updated.updated, "2026-02-02T00:00:00Z")

    def test_update_preserves_tags_but_refreshes_description(self):
        self.store.write("Old text.", name="keep", tags=["one"])
        updated = self.store.write("Completely different text.", name="keep")

        self.assertEqual(updated.tags, ("one",))
        self.assertEqual(updated.description, "Completely different text.")

    def test_derived_name_collision_with_different_text_does_not_overwrite(self):
        first = self.store.write("Tests run with unittest. Use discover.")
        second = self.store.write("Tests run with unittest. But not on Haiku.")

        self.assertEqual(first.name, "tests-run-with-unittest")
        self.assertEqual(second.name, "tests-run-with-unittest-2")
        self.assertEqual(len(self.store.all()), 2)
        self.assertEqual(self.store.get("tests-run-with-unittest").body,
                         "Tests run with unittest. Use discover.")

    def test_derived_name_collision_with_identical_text_is_idempotent(self):
        self.store.write("Same fact stated once.")
        self.store.write("Same fact stated once.")

        self.assertEqual(len(self.store.all()), 1)

    def test_slugify_and_scope_helpers(self):
        self.assertEqual(slugify("  Hello   World  "), "hello-world")
        self.assertEqual(slugify(""), "")
        self.assertEqual(normalize_scope("USER"), "user")
        self.assertEqual(normalize_scope("global"), "user")
        self.assertEqual(normalize_scope(""), "project")
        self.assertEqual(normalize_scope("nonsense"), "project")

    def test_derive_description_collapses_and_truncates(self):
        self.assertEqual(derive_description("two\n\nlines"), "two lines")
        long = derive_description("word " * 100)
        self.assertTrue(long.endswith("..."))
        self.assertLessEqual(len(long), 123)


class ScopeTests(StoreTestCase):
    def test_scopes_use_separate_directories(self):
        project = self.store.write("Project fact.", name="p", scope="project")
        user = self.store.write("User fact.", name="u", scope="user")

        self.assertEqual(project.path.parent, self.project_dir)
        self.assertEqual(user.path.parent, self.user_dir)
        self.assertTrue((self.project_dir / "p.md").exists())
        self.assertTrue((self.user_dir / "u.md").exists())
        self.assertFalse((self.project_dir / "u.md").exists())

    def test_all_lists_project_memories_first(self):
        self.store.write("User fact.", name="aaa-user", scope="user")
        self.store.write("Project fact.", name="zzz-project", scope="project")

        names = [m.name for m in self.store.all()]
        scopes = [m.scope for m in self.store.all()]

        self.assertEqual(names, ["zzz-project", "aaa-user"])
        self.assertEqual(scopes, ["project", "user"])

    def test_same_name_can_exist_in_both_scopes(self):
        self.store.write("Project version.", name="dup", scope="project")
        self.store.write("User version.", name="dup", scope="user")

        self.assertEqual(len(self.store.all()), 2)
        self.assertEqual(self.store.get("dup").scope, "project")
        self.assertEqual(self.store.get("dup", scope="user").body, "User version.")

    def test_delete_respects_scope(self):
        self.store.write("Project version.", name="dup", scope="project")
        self.store.write("User version.", name="dup", scope="user")

        self.assertTrue(self.store.delete("dup", scope="user"))

        remaining = self.store.all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].scope, "project")

    def test_directory_on_disk_wins_over_frontmatter_scope(self):
        # A hand-edited file claiming the wrong scope must not make the store
        # disagree with the filesystem.
        self.project_dir.mkdir(parents=True)
        (self.project_dir / "liar.md").write_text(
            "---\nname: liar\nscope: user\n---\n\nBody.\n")

        self.assertEqual(self.store.get("liar").scope, "project")

    def test_missing_directories_are_not_an_error(self):
        self.assertEqual(self.store.all(), [])
        self.assertEqual(self.store.warnings, [])
        self.assertIsNone(self.store.get("nothing"))
        self.assertEqual(self.store.search("anything"), [])


class SearchTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.write("Deploy to Haiku with tar over ssh; scp hangs.",
                         name="deploy-haiku")
        self.store.write("Run the suite with python3 -m unittest discover.",
                         name="run-tests")
        self.store.write("The owner writes Norwegian in chat.",
                         name="owner-language", scope="user")

    def test_query_ranks_the_matching_memory_first(self):
        self.assertEqual(self.store.search("deploy haiku")[0].name, "deploy-haiku")
        self.assertEqual(self.store.search("unittest")[0].name, "run-tests")
        self.assertEqual(self.store.search("norwegian")[0].name, "owner-language")

    def test_body_text_is_searchable(self):
        self.assertEqual(self.store.search("scp hangs")[0].name, "deploy-haiku")

    def test_non_matching_query_returns_nothing(self):
        self.assertEqual(self.store.search("zzzqqq"), [])

    def test_empty_query_returns_everything_in_store_order(self):
        results = self.store.search("")

        self.assertEqual([m.name for m in results],
                         [m.name for m in self.store.all()])

    def test_limit_is_respected(self):
        self.assertEqual(len(self.store.search("e", limit=2)), 2)
        self.assertEqual(len(self.store.search("", limit=1)), 1)


class IndexTests(StoreTestCase):
    def test_index_file_is_written_and_rebuilt(self):
        self.store.write("Alpha fact.", name="alpha")
        self.store.write("Beta fact.", name="beta")

        index = (self.project_dir / INDEX_NAME).read_text()

        self.assertIn("- [alpha](alpha.md) — Alpha fact.", index)
        self.assertIn("- [beta](beta.md) — Beta fact.", index)

        self.store.delete("alpha")
        index = (self.project_dir / INDEX_NAME).read_text()

        self.assertNotIn("alpha.md", index)
        self.assertIn("beta.md", index)

    def test_index_lines_can_be_scoped(self):
        self.store.write("Project fact.", name="p")
        self.store.write("User fact.", name="u", scope="user")

        self.assertEqual(self.store.index_lines("project"),
                         ["- [p](p.md) — Project fact."])
        self.assertEqual(self.store.index_lines("user"),
                         ["- [u](u.md) — User fact."])
        self.assertEqual(len(self.store.index_lines()), 2)

    def test_index_is_not_itself_a_memory(self):
        self.store.write("Only fact.", name="only")

        self.assertEqual([m.name for m in self.store.all()], ["only"])
        self.assertIsNone(self.store.get("memory"))

    def test_rebuild_index_recreates_a_deleted_index(self):
        self.store.write("Fact.", name="fact")
        (self.project_dir / INDEX_NAME).unlink()

        written = self.store.rebuild_index()

        self.assertIn(self.project_dir / INDEX_NAME, written)
        self.assertIn("fact.md", (self.project_dir / INDEX_NAME).read_text())

    def test_rebuild_index_skips_directories_that_do_not_exist(self):
        self.assertEqual(self.store.rebuild_index(), [])


class ContextBlockTests(StoreTestCase):
    def test_empty_store_exposes_memory_without_inventing_entries(self):
        block = self.store.context_block()
        self.assertIn("# Memory", block)
        self.assertIn("No saved memories yet.", block)

    def test_block_lists_all_descriptions_and_project_bodies(self):
        self.store.write("Project body text here.", name="proj",
                         description="Project summary")
        self.store.write("User body text here.", name="usr", scope="user",
                         description="User summary")

        block = self.store.context_block(4000)

        self.assertIn("Project summary", block)
        self.assertIn("User summary", block)
        self.assertIn("Project body text here.", block)
        # User bodies are deliberately not inlined; only their descriptions are.
        self.assertNotIn("User body text here.", block)

    def test_truncation_drops_oldest_body_first_with_a_marker(self):
        for index in range(4):
            stamp = f"2026-0{index + 1}-01T00:00:00Z"
            with patch("haikode.memory._timestamp", return_value=stamp):
                self.store.write("X" * 400, name=f"m{index}",
                                 description=f"summary {index}")

        block = self.store.context_block(1800)

        self.assertLessEqual(len(block), 1800)
        self.assertIn("omitted to fit the context budget", block)
        # Descriptions are cheap, so all four survive...
        for index in range(4):
            self.assertIn(f"summary {index}", block)
        # ...while the newest body is the one kept in full.
        self.assertIn("### m3", block)
        self.assertNotIn("### m0", block)

    def test_marker_counts_the_dropped_bodies(self):
        for index in range(3):
            stamp = f"2026-0{index + 1}-01T00:00:00Z"
            with patch("haikode.memory._timestamp", return_value=stamp):
                self.store.write("Y" * 500, name=f"n{index}",
                                 description=f"summary {index}")

        block = self.store.context_block(1400)

        self.assertIn("[... 2 older project memories omitted", block)

    def test_singular_marker_reads_correctly(self):
        for index in range(2):
            stamp = f"2026-0{index + 1}-01T00:00:00Z"
            with patch("haikode.memory._timestamp", return_value=stamp):
                self.store.write("Z" * 600, name=f"s{index}",
                                 description=f"summary {index}")

        block = self.store.context_block(1500)

        self.assertIn("[... 1 older project memory omitted", block)

    def test_tiny_budget_still_announces_that_memories_exist(self):
        self.store.write("Something worth remembering.", name="tiny")

        block = self.store.context_block(10)

        self.assertIn("# Memory", block)
        self.assertIn("omitted to fit the context budget", block)

    def test_generous_budget_has_no_marker(self):
        self.store.write("Short fact.", name="short")

        self.assertNotIn("omitted", self.store.context_block(4000))


class CorruptFileTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.write("A perfectly good memory.", name="good")

    def test_undecodable_file_is_skipped_into_warnings(self):
        (self.project_dir / "broken.md").write_bytes(b"---\nname: broken\n---\n\xff\xfe\x00bad")

        names = [m.name for m in self.store.all()]

        self.assertEqual(names, ["good"])
        self.assertEqual(len(self.store.warnings), 1)
        self.assertIn("broken.md", self.store.warnings[0])
        self.assertIn("unreadable", self.store.warnings[0])

    def test_empty_file_is_skipped_into_warnings(self):
        (self.project_dir / "blank.md").write_text("   \n\n")

        self.assertEqual([m.name for m in self.store.all()], ["good"])
        self.assertIn("empty memory file blank.md", self.store.warnings)

    def test_frontmatter_only_file_is_skipped_into_warnings(self):
        (self.project_dir / "headless.md").write_text(
            "---\nname: headless\ndescription: nothing here\n---\n\n")

        self.assertEqual([m.name for m in self.store.all()], ["good"])
        self.assertIn("headless.md has no body", self.store.warnings[0])

    def test_duplicate_names_keep_the_first_file(self):
        (self.project_dir / "aaa.md").write_text("---\nname: good\n---\n\nImpostor.\n")

        memories = self.store.all()

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].body, "Impostor.")
        self.assertTrue(any("duplicate memory name" in w for w in self.store.warnings))

    def test_warnings_are_reset_between_scans(self):
        (self.project_dir / "blank.md").write_text("")
        self.store.all()
        self.assertEqual(len(self.store.warnings), 1)

        (self.project_dir / "blank.md").unlink()
        self.store.all()

        self.assertEqual(self.store.warnings, [])

    def test_non_markdown_files_are_ignored_silently(self):
        (self.project_dir / "notes.txt").write_text("not a memory")
        (self.project_dir / "sub").mkdir()

        self.assertEqual([m.name for m in self.store.all()], ["good"])
        self.assertEqual(self.store.warnings, [])


class QuickCaptureTests(unittest.TestCase):
    def test_single_hash_is_project_scoped(self):
        self.assertEqual(parse_quick_capture("# tests live in tests/"),
                         ("tests live in tests/", "project"))

    def test_hash_without_space_still_captures(self):
        self.assertEqual(parse_quick_capture("#remember this"),
                         ("remember this", "project"))

    def test_double_hash_is_user_scoped(self):
        self.assertEqual(parse_quick_capture("## the owner prefers Norwegian"),
                         ("the owner prefers Norwegian", "user"))

    def test_explicit_scope_prefix_overrides(self):
        self.assertEqual(parse_quick_capture("#user: likes tabs"),
                         ("likes tabs", "user"))
        self.assertEqual(parse_quick_capture("# global: likes tabs"),
                         ("likes tabs", "user"))
        self.assertEqual(parse_quick_capture("## project: uses stdlib only"),
                         ("uses stdlib only", "project"))

    def test_leading_whitespace_is_tolerated(self):
        self.assertEqual(parse_quick_capture("   # indented note"),
                         ("indented note", "project"))

    def test_non_capture_lines_return_none(self):
        for line in ("", "   ", "hello", "issue #42 is fixed", "#", "  ##  ",
                     "### a markdown heading", "code = '#'"):
            self.assertIsNone(parse_quick_capture(line), line)

    def test_capture_text_is_stripped(self):
        self.assertEqual(parse_quick_capture("#   padded note   "),
                         ("padded note", "project"))


class ToolTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        # The tools build their own store from ctx.cwd, so the only way to keep
        # them off the real config dir is to redirect global_config_dir().
        patcher = patch("haikode.memory.global_config_dir",
                        return_value=self.global_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_tool = MemoryWriteTool()
        self.read_tool = MemoryReadTool()
        self.ctx = ToolContext(cwd=str(self.project_root))

    def test_registry_exports_both_tools(self):
        names = [tool.name for tool in MEMORY_TOOLS]

        self.assertEqual(names, ["memory_write", "memory_read"])
        for tool in MEMORY_TOOLS:
            self.assertTrue(tool.description)
            self.assertEqual(tool.parameters["type"], "object")
        self.assertEqual(MEMORY_TOOLS[0].permission, "memory_write")
        self.assertEqual(MEMORY_TOOLS[1].permission, "read")

    def test_write_tool_creates_a_project_memory(self):
        result = self.write_tool.execute(
            {"text": "The build script lives in scripts/build.sh.",
             "name": "build-script", "tags": ["build"]}, self.ctx)

        self.assertIn("build-script", result.title)
        self.assertEqual(result.metadata["scope"], "project")
        stored = self.store.get("build-script")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.tags, ("build",))
        self.assertEqual(stored.path.parent, self.project_dir)

    def test_write_tool_honours_user_scope(self):
        self.write_tool.execute(
            {"text": "The owner is based in Norway.", "scope": "user"}, self.ctx)

        stored = self.store.get("the-owner-is-based-in-norway")

        self.assertIsNotNone(stored)
        self.assertEqual(stored.scope, "user")
        self.assertEqual(stored.path.parent, self.user_dir)

    def test_write_tool_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            self.write_tool.execute({"text": "  "}, self.ctx)

    def test_write_tool_asks_under_the_memory_write_key(self):
        asked = []
        ctx = ToolContext(cwd=str(self.project_root),
                          permissions=Permissions(asker=lambda request:
                                                  asked.append(request) or "once"))

        ctx_result = self.write_tool.execute({"text": "Asked before writing."}, ctx)

        self.assertEqual([request.key for request in asked], ["memory_write"])
        self.assertIn("Remember (project)", asked[0].title)
        self.assertTrue(ctx_result.metadata["path"])

    def test_write_tool_respects_a_rejection(self):
        ctx = ToolContext(cwd=str(self.project_root),
                          permissions=Permissions(asker=lambda request: "reject"))

        with self.assertRaises(PermissionDenied):
            self.write_tool.execute({"text": "Never stored."}, ctx)

        self.assertEqual(self.store.all(), [])

    def test_read_tool_lists_everything_without_a_query(self):
        self.store.write("Project note about the build.", name="build")
        self.store.write("User note about the person.", name="person", scope="user")

        result = self.read_tool.execute({}, self.ctx)

        self.assertEqual(result.metadata["count"], 2)
        self.assertEqual(result.metadata["names"], ["build", "person"])
        self.assertIn("Project note about the build.", result.output)
        self.assertIn("User note about the person.", result.output)
        self.assertIn("## person (user)", result.output)

    def test_read_tool_ranks_a_query(self):
        self.store.write("Deploy with tar over ssh.", name="deploy")
        self.store.write("Run tests with unittest.", name="tests")

        result = self.read_tool.execute({"query": "deploy tar"}, self.ctx)

        self.assertEqual(result.metadata["names"][0], "deploy")
        self.assertIn("tar over ssh", result.output)

    def test_read_tool_reports_an_empty_store(self):
        result = self.read_tool.execute({}, self.ctx)

        self.assertEqual(result.metadata["count"], 0)
        self.assertIn("No memories saved yet", result.output)

    def test_read_tool_never_prompts(self):
        # "read" defaults to ALLOW, so a headless Permissions (asker=None,
        # which rejects every ASK) must still be able to recall memories.
        self.store.write("Readable without approval.", name="free")
        ctx = ToolContext(cwd=str(self.project_root), permissions=Permissions())

        result = self.read_tool.execute({}, ctx)

        self.assertEqual(result.metadata["count"], 1)

    def test_round_trip_through_both_tools(self):
        self.write_tool.execute(
            {"text": "Haiku has no pip, so stdlib only.", "name": "stdlib-only"},
            self.ctx)

        result = self.read_tool.execute({"query": "stdlib"}, self.ctx)

        self.assertIn("Haiku has no pip, so stdlib only.", result.output)
        self.assertEqual(result.metadata["names"][0], "stdlib-only")


class MemoryRecordTests(unittest.TestCase):
    def test_summary_falls_back_to_the_body(self):
        memory = Memory(name="x", body="Body sentence here.")

        self.assertEqual(memory.summary(), "Body sentence here.")
        self.assertEqual(memory.filename, "x.md")
        self.assertEqual(memory.prompt_line(),
                         "- x (project) — Body sentence here.")

    def test_sort_key_prefers_updated_then_created(self):
        older = Memory(name="a", body="b", created="2026-01-01T00:00:00Z")
        newer = Memory(name="b", body="b", created="2026-01-01T00:00:00Z",
                       updated="2026-05-01T00:00:00Z")

        self.assertEqual([m.name for m in sorted([newer, older],
                                                 key=lambda m: m.sort_key())],
                         ["a", "b"])

    def test_parse_memory_tolerates_bracketed_tag_lists(self):
        parsed = parse_memory("---\nname: t\ntags: [alpha, beta]\n---\n\nBody\n")

        self.assertEqual(parsed.tags, ("alpha", "beta"))


if __name__ == "__main__":
    unittest.main()
