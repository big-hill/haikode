"""
Permission resolution, exhaustively.

Three defects were reproduced against the previous implementation and each has
an explicit regression test below, named after the defect:

  * --yes (auto_approve) returned before reading any rule, so it beat a deny
  * only patterns[0] was evaluated, so a multi-file request hid its other files
  * the longest matching glob won instead of the last one

The rest is a table over (auto_approve, configured rule, agent overlay,
session grant, one vs many patterns), because those interact: every one of the
three defects was a single branch taken in the wrong order.
"""

import unittest

from haikode.agents import AgentPermissions, BUILTIN
from haikode.permission import (ALLOW, ASK, DEFAULTS, DENY, PermissionRequest,
                                Permissions)
from haikode.schema import PermissionDenied


class FakeConfig:
    """Minimal stand-in for Config: a data dict and a counting save()."""

    def __init__(self, permission=None):
        self.data = {"permission": dict(permission or {})}
        self.saves = 0

    def save(self):
        self.saves += 1
        return True


def perms(permission=None, **kwargs) -> Permissions:
    config = FakeConfig(permission) if permission is not None else None
    return Permissions(config=config, **kwargs)


def request(key="bash", patterns=("ls",), title="t", always=None):
    return PermissionRequest(key, list(patterns), title, always=list(always)
                             if always is not None else None)


class ResolutionTable(unittest.TestCase):
    """
    (rule, grant, auto_approve, patterns) -> allow / ask / deny.

    "ask" means the asker is consulted; with asker=None that is a denial, so
    each row is checked twice: once headless (ASK must raise) and once with an
    asker that records whether it was called.
    """

    CASES = [
        # name, rule for "bash", session grants, auto_approve, patterns, expected
        ("nothing configured falls back to DEFAULTS[bash]=ask",
         None, [], False, ["ls"], ASK),
        ("flat allow", ALLOW, [], False, ["ls"], ALLOW),
        ("flat ask", ASK, [], False, ["ls"], ASK),
        ("flat deny", DENY, [], False, ["ls"], DENY),

        ("auto_approve resolves an unset key", None, [], True, ["ls"], ALLOW),
        ("auto_approve resolves a configured ask", ASK, [], True, ["ls"], ALLOW),
        ("auto_approve leaves allow alone", ALLOW, [], True, ["ls"], ALLOW),
        ("auto_approve must not beat a flat deny", DENY, [], True, ["ls"], DENY),
        ("auto_approve must not beat a pattern deny",
         {"*": ALLOW, "rm *": DENY}, [], True, ["rm -rf /"], DENY),

        ("a session grant resolves an ask", ASK, ["ls*"], False, ["ls"], ALLOW),
        ("a session grant resolves an unset key",
         None, ["ls*"], False, ["ls"], ALLOW),
        ("a session grant must not beat a deny", DENY, ["ls*"], False, ["ls"], DENY),
        ("a session grant that does not match still asks",
         ASK, ["git *"], False, ["ls"], ASK),

        ("every pattern allowed -> allow",
         {"*": ALLOW}, [], False, ["a.txt", "b.txt"], ALLOW),
        ("one pattern denied -> deny",
         {"*": ALLOW, ".env": DENY}, [], False, ["a.txt", ".env"], DENY),
        ("a deny in the last position is still found",
         {"*": ALLOW, ".env": DENY}, [], False, [".env", "a.txt"], DENY),
        ("one pattern unresolved -> ask",
         {"a.txt": ALLOW}, [], False, ["a.txt", "b.txt"], ASK),
        ("grants must cover every pattern",
         ASK, ["a.txt"], False, ["a.txt", "b.txt"], ASK),
        ("grants covering every pattern -> allow",
         ASK, ["a.txt", "b.txt"], False, ["a.txt", "b.txt"], ALLOW),

        ("last matching rule wins: catch-all after a specific rule",
         {"git *": ALLOW, "*": DENY}, [], False, ["git status"], DENY),
        ("last matching rule wins: specific rule after a catch-all",
         {"*": DENY, "git *": ALLOW}, [], False, ["git status"], ALLOW),
        ("a rule that matches nothing is inert",
         {"*": ALLOW, "npm *": DENY}, [], False, ["git status"], ALLOW),
    ]

    def _run(self, rule, grants, auto_approve, patterns, asker=None):
        permission = {"bash": rule} if rule is not None else {}
        subject = perms(permission, auto_approve=auto_approve, asker=asker)
        if grants:
            subject.grant_always("bash", list(grants))
        subject.ask(request(patterns=patterns))

    def test_table(self):
        for name, rule, grants, auto, patterns, expected in self.CASES:
            with self.subTest(name):
                asked = []
                if expected == ALLOW:
                    self._run(rule, grants, auto, patterns,
                              asker=lambda r: asked.append(r) or "reject")
                    self.assertEqual(asked, [], f"{name}: asker was consulted")
                    continue

                # Headless: both ASK and DENY refuse.
                with self.assertRaises(PermissionDenied, msg=name):
                    self._run(rule, grants, auto, patterns)

                if expected == ASK:
                    self._run(rule, grants, auto, patterns,
                              asker=lambda r: asked.append(r) or "once")
                    self.assertEqual(len(asked), 1, f"{name}: asker not consulted")
                else:
                    with self.assertRaises(PermissionDenied, msg=name):
                        self._run(rule, grants, auto, patterns,
                                  asker=lambda r: asked.append(r) or "once")
                    self.assertEqual(asked, [],
                                     f"{name}: a deny reached the asker")


class Reproductions(unittest.TestCase):
    """The three audited escapes, each executed exactly as reported."""

    def test_defect_1_yes_flag_overrides_an_explicit_deny(self):
        subject = perms({"bash": DENY}, auto_approve=True)
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["ls"]))

    def test_defect_1_yes_flag_overrides_an_agent_overlay_deny(self):
        """Plan mode's deny arrives as an AgentPermissions overlay, not config."""
        overlay = AgentPermissions(BUILTIN["plan"], FakeConfig({"edit": ALLOW}))
        subject = Permissions(config=overlay, auto_approve=True)
        with self.assertRaises(PermissionDenied):
            subject.ask(request("edit", ["a.txt"]))

    def test_defect_2_only_the_first_pattern_is_checked(self):
        subject = perms({"edit": {"ok.txt": ALLOW, "*": DENY}})
        with self.assertRaises(PermissionDenied):
            subject.ask(request("edit", ["ok.txt", ".env"]))

    def test_defect_3_rule_order_not_specificity(self):
        subject = perms({"bash": {"rm *": ALLOW, "*": DENY}})
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["rm -rf /"]))


class RuleForms(unittest.TestCase):
    def test_a_list_of_pairs_is_accepted_and_ordered(self):
        rule = [["*", ALLOW], ["rm *", DENY]]
        subject = perms({"bash": rule})
        subject.ask(request(patterns=["ls"]))
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["rm -rf /"]))

    def test_a_list_of_pairs_evaluates_last_match_wins(self):
        subject = perms({"bash": [["rm *", DENY], ["*", ALLOW]]})
        subject.ask(request(patterns=["rm -rf /"]))

    def test_a_list_of_single_key_objects_is_accepted(self):
        subject = perms({"bash": [{"*": ALLOW}, {"rm *": DENY}]})
        subject.ask(request(patterns=["ls"]))
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["rm -rf /"]))

    def test_a_list_of_pattern_action_objects_is_accepted(self):
        subject = perms({"bash": [{"pattern": "*", "action": DENY}]})
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["ls"]))

    def test_an_unknown_decision_degrades_to_ask_rather_than_vanishing(self):
        """Dropping the rule would let the earlier allow stand — fail closed."""
        subject = perms({"bash": {"*": ALLOW, "rm *": "maybe"}})
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["rm -rf /"]))
        subject.ask(request(patterns=["ls"]))

    def test_a_junk_rule_type_is_ignored_and_the_default_applies(self):
        subject = perms({"bash": 17}, asker=lambda r: "once")
        subject.ask(request(patterns=["ls"]))
        self.assertIsNone(subject._configured("bash", "ls"))

    def test_a_missing_config_resolves_from_defaults(self):
        Permissions().ask(request("read", ["a.txt"]))
        with self.assertRaises(PermissionDenied):
            Permissions().ask(request("bash", ["ls"]))


class Defaults(unittest.TestCase):
    def test_question_and_external_directory_are_listed_as_ask(self):
        self.assertEqual(DEFAULTS["question"], ASK)
        self.assertEqual(DEFAULTS["external_directory"], ASK)

    def test_describe_lists_every_default_key(self):
        rows = Permissions().describe()
        self.assertEqual({key for key, _, _, _ in rows}, set(DEFAULTS))
        self.assertTrue(all(pattern == "*" and not configured
                            for _, pattern, _, configured in rows))


class Describe(unittest.TestCase):
    def test_rules_are_rendered_in_evaluation_order(self):
        subject = perms({"bash": {"*": DENY, "git *": ALLOW, "git push": DENY}})
        rows = [(pattern, decision) for key, pattern, decision, _ in
                subject.describe() if key == "bash"]
        self.assertEqual(rows, [("*", DENY), ("git *", ALLOW), ("git push", DENY)])

    def test_a_list_rule_renders_in_its_own_order(self):
        subject = perms({"bash": [["*", ALLOW], ["rm *", DENY]]})
        rows = [(p, d) for k, p, d, _ in subject.describe() if k == "bash"]
        self.assertEqual(rows, [("*", ALLOW), ("rm *", DENY)])

    def test_a_flat_rule_renders_as_a_catch_all(self):
        subject = perms({"bash": DENY})
        rows = [(p, d, c) for k, p, d, c in subject.describe() if k == "bash"]
        self.assertEqual(rows, [("*", DENY, True)])

    def test_a_configured_key_with_no_default_is_still_listed(self):
        subject = perms({"mcp": {"*": ALLOW}})
        self.assertIn("mcp", {key for key, _, _, _ in subject.describe()})

    def test_session_grants_are_reported_separately_from_rules(self):
        subject = perms({"bash": ASK}, asker=lambda r: "always")
        subject.ask(request(patterns=["git status"], always=["git status *"]))
        self.assertEqual(subject.session_grants(), {"bash": ["git status *"]})
        self.assertNotIn("git status *",
                         {pattern for _, pattern, _, _ in subject.describe()})

    def test_session_grants_returns_a_copy(self):
        subject = Permissions()
        subject.grant_always("bash", ["ls"])
        subject.session_grants()["bash"].append("rm *")
        self.assertEqual(subject._session_grants["bash"], ["ls"])


class SessionGrants(unittest.TestCase):
    def test_always_grants_the_always_shapes_not_the_request_patterns(self):
        subject = perms({"bash": ASK}, asker=lambda r: "always")
        subject.ask(request(patterns=["git status -s"], always=["git status *"]))
        subject.ask(request(patterns=["git status --short"]))
        with self.assertRaises(PermissionDenied):
            Permissions(config=FakeConfig({"bash": ASK})).ask(
                request(patterns=["git status --short"]))

    def test_a_grant_never_outranks_a_deny_added_by_an_agent_switch(self):
        config = FakeConfig({"bash": ASK})
        subject = Permissions(config=config, asker=lambda r: "always")
        subject.ask(request(patterns=["ls"], always=["*"]))
        config.data["permission"]["bash"] = DENY
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["ls"]))

    def test_grants_are_deduplicated_and_ordered(self):
        subject = Permissions()
        subject.grant_always("bash", ["a", "b", "a"])
        subject.grant_always("bash", ["b", "c"])
        self.assertEqual(subject._session_grants["bash"], ["a", "b", "c"])

    def test_an_empty_grant_list_becomes_the_catch_all(self):
        subject = Permissions()
        subject.grant_always("bash", [])
        self.assertEqual(subject._session_grants["bash"], ["*"])

    def test_once_does_not_grant_anything(self):
        subject = perms({"bash": ASK}, asker=lambda r: "once")
        subject.ask(request(patterns=["ls"]))
        self.assertEqual(subject._session_grants, {})

    def test_reject_raises_with_the_title(self):
        subject = perms({"bash": ASK}, asker=lambda r: "reject")
        with self.assertRaises(PermissionDenied) as caught:
            subject.ask(request(patterns=["ls"], title="Run: ls"))
        self.assertIn("Run: ls", str(caught.exception))


class Persist(unittest.TestCase):
    def test_a_new_rule_is_written_and_saved(self):
        subject = perms({})
        self.assertTrue(subject.persist("webfetch", "https://x/*", ALLOW))
        self.assertEqual(subject.config.data["permission"]["webfetch"],
                         {"https://x/*": ALLOW})
        self.assertEqual(subject.config.saves, 1)

    def test_a_flat_rule_becomes_its_catch_all_first(self):
        subject = perms({"webfetch": ASK})
        subject.persist("webfetch", "https://x/*", ALLOW)
        self.assertEqual(list(subject.config.data["permission"]["webfetch"].items()),
                         [("*", ASK), ("https://x/*", ALLOW)])

    def test_it_refuses_to_widen_a_flat_deny(self):
        subject = perms({"bash": DENY})
        self.assertFalse(subject.persist("bash", "git status", ALLOW))
        self.assertEqual(subject.config.data["permission"]["bash"], DENY)
        self.assertEqual(subject.config.saves, 0)

    def test_it_refuses_to_widen_a_catch_all_deny(self):
        subject = perms({"bash": {"*": DENY}})
        self.assertFalse(subject.persist("bash", "git status", ALLOW))
        self.assertEqual(subject.config.data["permission"]["bash"], {"*": DENY})

    def test_it_refuses_a_catch_all_that_would_swallow_a_narrow_deny(self):
        subject = perms({"bash": {"rm *": DENY}})
        self.assertFalse(subject.persist("bash", "*", ALLOW))
        self.assertEqual(subject.config.data["permission"]["bash"], {"rm *": DENY})

    def test_it_still_allows_an_unrelated_rule_next_to_a_deny(self):
        subject = perms({"bash": {"rm *": DENY}})
        self.assertTrue(subject.persist("bash", "git status", ALLOW))
        self.assertEqual(list(subject.config.data["permission"]["bash"].items()),
                         [("rm *", DENY), ("git status", ALLOW)])

    def test_a_tightening_rule_is_always_allowed(self):
        subject = perms({"bash": {"*": DENY}})
        self.assertTrue(subject.persist("bash", "rm *", DENY))

    def test_re_persisting_a_pattern_moves_it_to_the_end(self):
        subject = perms({"bash": {"git *": ASK, "*": ASK}})
        subject.persist("bash", "git *", ALLOW)
        self.assertEqual(list(subject.config.data["permission"]["bash"].items()),
                         [("*", ASK), ("git *", ALLOW)])

    def test_a_list_rule_survives_being_appended_to(self):
        """Flattening the list must not throw the existing rules away."""
        subject = perms({"bash": [["*", ALLOW], ["rm *", DENY]]})
        self.assertTrue(subject.persist("bash", "git status", ALLOW))
        self.assertEqual(list(subject.config.data["permission"]["bash"].items()),
                         [("*", ALLOW), ("rm *", DENY), ("git status", ALLOW)])
        with self.assertRaises(PermissionDenied):
            subject.ask(request(patterns=["rm -rf /"]))

    def test_persisting_without_a_config_is_a_no_op(self):
        self.assertFalse(Permissions().persist("bash", "*", ALLOW))

    def test_a_persisted_allow_takes_effect_immediately(self):
        subject = perms({"bash": {"*": ASK}})
        subject.persist("bash", "git status", ALLOW)
        subject.ask(request(patterns=["git status"]))


class AgentOverlay(unittest.TestCase):
    """The overlay reaches Permissions as config.data["permission"]."""

    def test_the_overlay_deny_beats_a_user_allow(self):
        overlay = AgentPermissions(BUILTIN["plan"], FakeConfig({"edit": ALLOW}))
        with self.assertRaises(PermissionDenied):
            Permissions(config=overlay).ask(request("edit", ["a.txt"]))

    def test_the_overlay_deny_beats_a_session_grant(self):
        overlay = AgentPermissions(BUILTIN["plan"], FakeConfig({"edit": ALLOW}))
        subject = Permissions(config=overlay)
        subject.grant_always("edit", ["*"])
        with self.assertRaises(PermissionDenied):
            subject.ask(request("edit", ["a.txt"]))

    def test_the_overlay_deny_beats_auto_approve_and_an_asker(self):
        overlay = AgentPermissions(BUILTIN["plan"], FakeConfig())
        subject = Permissions(config=overlay, auto_approve=True,
                              asker=lambda r: "always")
        with self.assertRaises(PermissionDenied):
            subject.ask(request("bash", ["ls"]))

    def test_persist_cannot_write_through_an_overlay_deny(self):
        config = FakeConfig()
        overlay = AgentPermissions(BUILTIN["plan"], config)
        self.assertFalse(Permissions(config=overlay).persist("bash", "ls", ALLOW))
        self.assertEqual(config.saves, 0)


class Probing(unittest.TestCase):
    """status.effective_policy probes _configured(key, "*")."""

    def test_a_catch_all_answers_the_probe(self):
        self.assertEqual(perms({"bash": {"git *": ALLOW, "*": DENY}})
                         ._configured("bash", "*"), DENY)

    def test_only_specific_rules_leave_the_probe_unanswered(self):
        self.assertIsNone(perms({"bash": {"git *": ALLOW}})
                          ._configured("bash", "*"))

    def test_the_probe_never_returns_an_invalid_decision(self):
        self.assertEqual(perms({"bash": {"*": "nonsense"}})
                         ._configured("bash", "*"), ASK)


class YoloMode(unittest.TestCase):
    """The escape hatch: every gate off, including the ones --yes respects."""

    def test_yolo_overrides_a_configured_deny(self):
        p = perms({"bash": {"*": DENY}}, yolo=True)
        self.assertEqual(p.decide("bash", "rm -rf /"), ALLOW)
        p.ask(request(patterns=("rm -rf /",)))  # must not raise

    def test_yes_alone_still_respects_a_deny(self):
        p = perms({"bash": {"*": DENY}}, auto_approve=True)
        self.assertEqual(p.decide("bash", "rm -rf /"), DENY)
        with self.assertRaises(PermissionDenied):
            p.ask(request(patterns=("rm -rf /",)))

    def test_yolo_never_consults_the_asker(self):
        asked = []
        p = perms({"bash": {"*": ASK}}, yolo=True,
                  asker=lambda r: asked.append(r) or "reject")
        p.ask(request())
        self.assertEqual(asked, [])

    def test_yolo_still_puts_questions_to_the_user(self):
        # yolo lifts gates; it cannot answer on the user's behalf. A request
        # whose metadata says it is a question must still reach the asker,
        # or the question tool and plan approval die silently in --yolo.
        asked = []
        p = perms(yolo=True, asker=lambda r: asked.append(r) or "once")
        q = PermissionRequest("question", ["*"], "t",
                              metadata={"kind": "question"})
        p.ask(q)
        self.assertEqual(1, len(asked))

    def test_yolo_question_rejection_still_raises(self):
        p = perms(yolo=True, asker=lambda r: "reject")
        q = PermissionRequest("question", ["*"], "t",
                              metadata={"kind": "question"})
        with self.assertRaises(PermissionDenied):
            p.ask(q)

    def test_yolo_question_without_an_asker_does_not_raise(self):
        q = PermissionRequest("question", ["*"], "t",
                              metadata={"kind": "question"})
        perms(yolo=True).ask(q)  # headless --yolo: unanswerable, not fatal

    def test_yolo_is_off_by_default(self):
        self.assertFalse(perms().yolo)

    def test_turning_yolo_off_restores_the_rules(self):
        p = perms({"bash": {"*": DENY}}, yolo=True)
        p.yolo = False
        self.assertEqual(p.decide("bash", "ls"), DENY)


if __name__ == "__main__":
    unittest.main()
