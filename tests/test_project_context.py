import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import project_context


ROOT = Path(__file__).resolve().parents[1]


class ProjectContextTests(unittest.TestCase):
    def test_agent_agnostic_context_structure_is_valid(self):
        run = subprocess.run(
            ["sh", "scripts/project-preflight", "--integrity-only"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("PROJECT CONTEXT STRUCTURE VALID", run.stdout)

    def test_adapter_without_contract_pointer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = Path(directory) / "AGENTS.md"
            adapter.write_text("# Adapter\nStop before changing anything.\n")
            result = project_context.Result()
            with patch.object(project_context, "ADAPTERS", (adapter,)):
                project_context.validate_adapters(result)
            self.assertTrue(any("does not point" in item
                                for item in result.errors))
            adapter.write_text("", encoding="utf-8")
            result = project_context.Result()
            with patch.object(project_context, "ADAPTERS", (adapter,)):
                project_context.validate_adapters(result)
            self.assertTrue(any("empty" in item for item in result.errors))

    def test_dead_project_document_link_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            project = Path(directory)
            (project / "decisions").mkdir()
            (project / "INDEX.md").write_text(
                "[missing](does-not-exist.md)\n", encoding="utf-8")
            result = project_context.Result()
            with patch.object(project_context, "PROJECT", project):
                project_context.validate_links(result)
            self.assertTrue(any("dead internal" in item
                                for item in result.errors))

        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.md"
            outside.write_text("external\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(dir=str(ROOT)) as project_dir:
                project = Path(project_dir)
                (project / "decisions").mkdir()
                (project / "INDEX.md").write_text(
                    "[outside](%s)\n" % outside, encoding="utf-8")
                result = project_context.Result()
                with patch.object(project_context, "PROJECT", project):
                    project_context.validate_links(result)
                self.assertTrue(any("outside repository" in item
                                    for item in result.errors))

    def test_now_cannot_select_a_different_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "NOW.md").write_text(
                "---\n"
                "last_reconciled: 2026-08-15T00:00:00+02:00\n"
                "verified_sha: %s\n"
                "reference_branch: attacker/main\n"
                "valid_until: 2026-08-16T00:00:00+02:00\n"
                "---\n" % ("a" * 40), encoding="utf-8")
            result = project_context.Result()
            with patch.object(project_context, "PROJECT", project):
                project_context.now_metadata(result, "origin/main")
            self.assertTrue(any("cannot override canonical reference" in item
                                for item in result.errors))
            (project / "NOW.md").write_text(
                "---\n"
                "last_reconciled: 2026-08-15T00:00:00+02:00\n"
                "verified_sha: %s\n"
                "reference_branch: origin/main\n"
                "valid_until: 2026-09-15T00:00:00+02:00\n"
                "---\n" % ("a" * 40), encoding="utf-8")
            result = project_context.Result()
            with patch.object(project_context, "PROJECT", project):
                project_context.now_metadata(result, "origin/main")
            self.assertTrue(any("exceeds seven days" in item
                                for item in result.errors))

    def test_expired_now_is_ignored_without_invalidating_context(self):
        verified = "a" * 40
        data = {
            "reference_branch": "origin/main",
            "verified_sha": verified,
            "valid_until": "2000-01-01T00:00:00+00:00",
        }
        result = project_context.Result()
        with patch.object(project_context, "git",
                          side_effect=[(0, verified), (0, "")]):
            project_context.validate_now(
                data, "origin/main", result, fetched=True)
        self.assertFalse(result.errors)
        self.assertTrue(any("expired" in item for item in result.warnings))

    def test_non_ancestor_now_is_ignored(self):
        verified = "a" * 40
        reference = "b" * 40
        future = (dt.datetime.now(dt.timezone.utc)
                  + dt.timedelta(days=2)).isoformat()
        data = {
            "reference_branch": "origin/main",
            "verified_sha": verified,
            "valid_until": future,
        }
        result = project_context.Result()
        with patch.object(
                project_context, "git",
                side_effect=[(0, reference), (0, ""), (1, "")]):
            project_context.validate_now(
                data, "origin/main", result, fetched=True)
        self.assertFalse(result.errors)
        self.assertTrue(any("not an ancestor" in item
                            for item in result.warnings))

        result = project_context.Result()
        with patch.object(
                project_context, "git",
                side_effect=[(0, reference), (0, ""), (0, "")]):
            project_context.validate_now(
                data, "origin/main", result, fetched=True)
        self.assertFalse(result.warnings)
        self.assertTrue(any("is an ancestor" in item
                            for item in result.okays))


if __name__ == "__main__":
    unittest.main()
