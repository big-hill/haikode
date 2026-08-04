"""The update check: quiet, honest, and architecture-aware."""

import unittest

from haikode import update


def release(tag, assets=()):
    return {"tag_name": tag, "html_url": "https://example.invalid/rel",
            "assets": [{"name": name,
                        "browser_download_url": "https://example.invalid/"
                        + name} for name in assets]}


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

    def test_the_opt_out_wins_without_any_network(self):
        def explode():
            raise AssertionError("must not be called")
        self.assertEqual("", update.startup_notice(
            {"update_check": False}, fetch=explode))

    def test_quiet_when_current(self):
        self.assertEqual("", update.startup_notice(
            {}, fetch=lambda: release("v0.0.1")))


if __name__ == "__main__":
    unittest.main()
