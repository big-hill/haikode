"""
Regression tests for two fail-open holes found while re-attacking the
security fixes. Both fail against the code as it was before this file existed.

1. `permission.<key> = "DENY"` (a shift key, or a trailing space) degraded to
   ASK, and `--yes` then approved it. A deny is the one decision that must
   never fail open.
2. An "always" answer to a path whose parent is the filesystem root was stored
   as the glob `/*`. fnmatch's `*` spans `/`, so that one answer granted every
   directory on the machine while the prompt named a single file.
"""

import unittest

from haikode.permission import ALLOW, ASK, DENY, PermissionRequest, Permissions
from haikode.schema import PermissionDenied
from haikode.tool.paths import parent_glob


class _Config:
    def __init__(self, rules):
        self.data = {"permission": rules}

    def save(self):
        pass


def _perms(rules, **kwargs):
    return Permissions(config=_Config(rules), **kwargs)


class DecisionSpelling(unittest.TestCase):
    """A deny must survive the spelling the user actually typed."""

    def test_uppercase_deny_is_a_deny(self):
        self.assertEqual(_perms({"bash": "DENY"}).decide("bash", "rm -rf /"),
                         DENY)

    def test_capitalised_deny_is_a_deny(self):
        self.assertEqual(_perms({"bash": {"*": "Deny"}}).decide("bash", "x"),
                         DENY)

    def test_trailing_whitespace_does_not_disarm_a_deny(self):
        self.assertEqual(_perms({"bash": "deny "}).decide("bash", "x"), DENY)

    def test_auto_approve_does_not_run_a_mis_capitalised_deny(self):
        permissions = _perms({"bash": "DENY"}, asker=None, auto_approve=True)
        with self.assertRaises(PermissionDenied):
            permissions.ask(PermissionRequest("bash", ["rm -rf /"], "t"))

    def test_allow_and_ask_are_normalised_too(self):
        self.assertEqual(_perms({"bash": "ALLOW"}).decide("bash", "x"), ALLOW)
        self.assertEqual(_perms({"read": " Ask "}).decide("read", "x"), ASK)

    def test_a_word_that_is_not_a_decision_still_asks(self):
        # Unchanged behaviour: we must not guess what "yes" meant.
        self.assertEqual(_perms({"bash": "yes"}).decide("bash", "x"), ASK)

    def test_describe_reports_the_normalised_decision(self):
        rows = [row for row in _perms({"bash": "DENY"}).describe()
                if row[0] == "bash"]
        self.assertEqual(rows, [("bash", "*", DENY, True)])


class RootGrantWidth(unittest.TestCase):
    """An approval for one path at / must not become an approval for the disk."""

    def test_a_file_at_the_root_grants_only_itself(self):
        self.assertEqual(parent_glob("/some-file"), "/some-file")

    def test_the_root_directory_itself_grants_only_itself(self):
        self.assertEqual(parent_glob("/", kind="directory"), "/")

    def test_a_deeper_path_still_grants_its_parent(self):
        self.assertEqual(parent_glob("/etc/hosts"), "/etc/*")
        self.assertEqual(parent_glob("/etc", kind="directory"), "/etc/*")

    def test_an_always_answer_at_the_root_does_not_cover_the_home_directory(self):
        permissions = _perms({})
        permissions.grant_always("external_directory",
                                 [parent_glob("/some-file")])
        self.assertEqual(
            permissions.decide("external_directory", "/home/user/.ssh/*"), ASK)

    def test_a_deny_on_everything_at_the_root_still_matches(self):
        permissions = _perms({"external_directory": {"/*": DENY}})
        self.assertEqual(
            permissions.decide("external_directory", parent_glob("/some-file")),
            DENY)


if __name__ == "__main__":
    unittest.main()
