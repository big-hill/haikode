"""The update check: quiet, honest, and architecture-aware."""

import hashlib
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from haikode import update
from haikode.repl import REPL


def release(tag, assets=(), digest=""):
    return {"tag_name": tag, "html_url": "https://example.invalid/rel",
            "assets": [{"name": name,
                        "browser_download_url": "https://example.invalid/"
                        + name,
                        "digest": digest} for name in assets]}


class VersionOrdering(unittest.TestCase):
    def test_releases_sort_above_their_own_prereleases(self):
        self.assertGreater(update.parse_version("0.1.0"),
                           update.parse_version("0.1.0-m0m1"))

    def test_numbers_dominate(self):
        self.assertGreater(update.parse_version("v0.2.0"),
                           update.parse_version("0.1.9"))

    def test_garbage_is_the_floor(self):
        self.assertLess(update.parse_version("not a version"),
                        update.parse_version("0.0.1"))


class CheckTests(unittest.TestCase):
    def test_a_newer_release_is_offered_with_the_right_asset(self):
        state = update.check(fetch=lambda: release(
            "v9.9.9", ["haikode-9.9.9-60-x86_gcc2.hpkg",
                       "haikode-9.9.9-60-x86_64.hpkg"]))
        self.assertTrue(state["available"])
        self.assertEqual("v9.9.9", state["latest"])
        self.assertIn(update._architecture(), state["asset"])

    def test_the_matching_assets_github_digest_is_preserved(self):
        digest = "sha256:" + "a" * 64
        state = update.check(fetch=lambda: release(
            "v9.9.9", ["haikode-9.9.9-60-%s.hpkg"
                        % update._architecture()], digest=digest))
        self.assertEqual(digest, state["digest"])

    def test_an_unrelated_or_wrong_version_package_is_not_selected(self):
        state = update.check(fetch=lambda: release(
            "v9.9.9", ["helper-9.9.9-60-%s.hpkg" % update._architecture(),
                       "haikode-9.9.8-60-%s.hpkg"
                       % update._architecture()]))
        self.assertTrue(state["available"])
        self.assertEqual("", state["asset"])

    def test_an_older_release_is_not_an_update(self):
        state = update.check(fetch=lambda: release("v0.0.1"))
        self.assertFalse(state["available"])
        self.assertEqual("", state["error"])

    def test_failures_degrade_to_no_update_with_the_reason(self):
        def explode():
            raise OSError("offline")
        state = update.check(fetch=explode)
        self.assertFalse(state["available"])
        self.assertIn("offline", state["error"])


class StartupNoticeTests(unittest.TestCase):
    def test_the_notice_names_both_versions(self):
        notice = update.startup_notice({}, fetch=lambda: release("v9.9.9"))
        self.assertIn("v9.9.9", notice)
        self.assertIn("/update", notice)
        self.assertIn("install", notice)

    def test_the_opt_out_wins_without_any_network(self):
        def explode():
            raise AssertionError("must not be called")
        self.assertEqual("", update.startup_notice(
            {"update_check": False}, fetch=explode))

    def test_quiet_when_current(self):
        self.assertEqual("", update.startup_notice(
            {}, fetch=lambda: release("v0.0.1")))


class PackageInspectionTests(unittest.TestCase):
    def test_identity_comes_from_haikus_absolute_package_tool(self):
        completed = subprocess.CompletedProcess(
            [], 0, "haikode\t0.1.2-110\n", "")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(update, "PACKAGE_PATH",
                             Path(temp) / "package"), \
                patch.object(update.os, "access", return_value=True), \
                patch.object(update.Path, "is_file", return_value=True), \
                patch.object(update.subprocess, "run",
                             return_value=completed) as runner:
            identity = update._package_identity(Path("/tmp/release.hpkg"))

        self.assertEqual(("haikode", "0.1.2-110"), identity)
        self.assertEqual(
            [str(Path(temp) / "package"), "info", "-f",
             "%name%\t%version%\n", "/tmp/release.hpkg"],
            runner.call_args.args[0])

    def test_rejected_package_metadata_fails_closed(self):
        completed = subprocess.CompletedProcess([], 1, "", "bad package")
        with patch.object(update, "PACKAGE_PATH", Path("/system/package")), \
                patch.object(update.os, "access", return_value=True), \
                patch.object(update.Path, "is_file", return_value=True), \
                patch.object(update.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "bad package"):
                update._package_identity(Path("/tmp/release.hpkg"))


class PackagedUpdateTests(unittest.TestCase):
    def state(self, payload, digest=None):
        checksum = digest
        if checksum is None:
            checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
        return {"available": True, "current": "0.1.1", "latest": "v0.1.2",
                "url": "https://example.invalid/rel",
                "asset": "https://example.invalid/"
                         "haikode-0.1.2-110-x86_64.hpkg",
                "digest": checksum}

    def test_one_command_verifies_installs_and_only_requires_restart(self):
        payload = b"a small but validly transferred package"
        completed = subprocess.CompletedProcess([], 0, "installed\n", "")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(update, "install_kind", return_value="package"), \
                patch.object(update, "UPDATE_DIR", Path(temp)), \
                patch.object(update, "_pkgman_path",
                             return_value="/boot/system/bin/pkgman"), \
                patch.object(update, "_package_identity",
                             return_value=("haikode", "0.1.2-110")), \
                patch.object(update.urllib.request, "urlopen",
                             return_value=io.BytesIO(payload)), \
                patch.object(update.subprocess, "run",
                             return_value=completed) as runner:
            result = update.apply_update(self.state(payload))
            remaining = list(Path(temp).iterdir())

        argv = runner.call_args.args[0]
        self.assertEqual(argv[:3], ["/boot/system/bin/pkgman", "install", "-y"])
        self.assertNotIn("timeout", runner.call_args.kwargs)
        self.assertEqual(Path(argv[3]).name,
                         "haikode-0.1.2-110-x86_64.hpkg")
        self.assertIn("installed haikode v0.1.2", result)
        self.assertIn("close and reopen", result.lower())
        self.assertNotIn("pkgman install", result)
        self.assertEqual([], remaining)

    def test_a_checksum_mismatch_never_reaches_pkgman(self):
        payload = b"not the release asset"
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(update, "install_kind", return_value="package"), \
                patch.object(update, "UPDATE_DIR", Path(temp)), \
                patch.object(update, "_pkgman_path",
                             return_value="/boot/system/bin/pkgman"), \
                patch.object(update.urllib.request, "urlopen",
                             return_value=io.BytesIO(payload)), \
                patch.object(update.subprocess, "run") as runner:
            result = update.apply_update(self.state(
                payload, digest="sha256:" + "0" * 64))

            self.assertIn("checksum mismatch", result)
            runner.assert_not_called()
            self.assertEqual([], list(Path(temp).iterdir()))

    def test_a_release_without_a_digest_is_not_installed(self):
        payload = b"package"
        with patch.object(update, "install_kind", return_value="package"), \
                patch.object(update.urllib.request, "urlopen") as fetch, \
                patch.object(update.subprocess, "run") as runner:
            result = update.apply_update(self.state(payload, digest=""))

        self.assertIn("has no SHA-256 digest", result)
        fetch.assert_not_called()
        runner.assert_not_called()

    def test_wrong_package_metadata_never_reaches_pkgman(self):
        payload = b"a renamed but unrelated package"
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(update, "install_kind", return_value="package"), \
                patch.object(update, "UPDATE_DIR", Path(temp)), \
                patch.object(update, "_pkgman_path",
                             return_value="/boot/system/bin/pkgman"), \
                patch.object(update, "_package_identity",
                             return_value=("other", "0.1.2-110")), \
                patch.object(update.urllib.request, "urlopen",
                             return_value=io.BytesIO(payload)), \
                patch.object(update.subprocess, "run") as runner:
            result = update.apply_update(self.state(payload))

            self.assertIn("package validation failed", result)
            runner.assert_not_called()
            self.assertEqual([], list(Path(temp).iterdir()))

    def test_a_non_https_asset_is_never_downloaded_or_installed(self):
        payload = b"package"
        state = self.state(payload)
        state["asset"] = "http://example.invalid/haikode-0.1.2-110-x86_64.hpkg"
        with patch.object(update, "install_kind", return_value="package"), \
                patch.object(update, "_pkgman_path",
                             return_value="/boot/system/bin/pkgman"), \
                patch.object(update.urllib.request, "urlopen") as fetch, \
                patch.object(update.subprocess, "run") as runner:
            result = update.apply_update(state)

        self.assertIn("HTTPS", result)
        fetch.assert_not_called()
        runner.assert_not_called()

    def test_a_pkgman_failure_keeps_the_verified_package_for_recovery(self):
        payload = b"verified package"
        completed = subprocess.CompletedProcess([], 5, "", "transaction failed")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(update, "install_kind", return_value="package"), \
                patch.object(update, "UPDATE_DIR", Path(temp)), \
                patch.object(update, "_pkgman_path",
                             return_value="/boot/system/bin/pkgman"), \
                patch.object(update, "_package_identity",
                             return_value=("haikode", "0.1.2-110")), \
                patch.object(update.urllib.request, "urlopen",
                             return_value=io.BytesIO(payload)), \
                patch.object(update.subprocess, "run", return_value=completed):
            result = update.apply_update(self.state(payload))
            kept = list(Path(temp).rglob("*.hpkg"))

            self.assertIn("package install failed", result)
            self.assertIn("transaction failed", result)
            self.assertEqual(1, len(kept))
            self.assertIn(str(kept[0]), result)


class UpdateCommandRenderingTests(unittest.TestCase):
    def test_command_returns_text_instead_of_printing_over_curses(self):
        state = {"available": True, "current": "0.1.1",
                 "latest": "v0.1.2", "error": ""}
        printed = io.StringIO()
        with patch.object(update, "check", return_value=state), \
                patch.object(update, "apply_update",
                             return_value="installed; restart"), \
                redirect_stdout(printed):
            result = REPL._cmd_update(None, "")

        self.assertEqual("", printed.getvalue())
        self.assertIn("newest release: v0.1.2", result)
        self.assertIn("installed; restart", result)


if __name__ == "__main__":
    unittest.main()
