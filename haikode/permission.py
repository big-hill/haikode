"""
Permission system (phase 3) — modelled on opencode's ask/allow/deny flow.

Every tool declares a permission key ("edit", "bash", "read", ...). Before a
side-effecting action the tool calls ctx.ask(...), which consults, per pattern:

  1. persisted rules in config: permission.<key> = "allow" | "ask" | "deny"
     and per-pattern rules: permission.bash = {"*": "ask", "git status": "allow"}
     (catch-all first — the LAST match wins, see the migration note below)
     (an agent overlay such as plan mode arrives through this same channel)
  2. session-scoped "always" grants (remembered for this run)
  3. the interactive asker (TUI/REPL); headless runs deny by default

Three properties are load-bearing, and each one is a fix for a reproduced
escape:

* A request carries *every* pattern it touches (a multi-file apply_patch passes
  every filename). ALL of them are evaluated: any DENY denies, and the request
  is only allowed when every pattern is allowed. Checking patterns[0] alone let
  ["ok.txt", ".env"] through under {"ok.txt": allow, "*": deny}.
* DENY is absolute. Neither --yes/auto_approve nor a session "always" grant can
  turn it into an allow, because both of those are answers to a question that a
  deny means we never got to ask. opencode documents --yes as "auto-approve
  permissions that are not explicitly denied" (cli/cmd/run.ts).
* The LAST matching rule wins, not the most specific one — same as opencode's
  `findLast` in permission/index.ts.

MIGRATION NOTE (rule order changed)
-----------------------------------
Until now the *longest* matching glob won, so rule order in the config was
irrelevant. It is now significant, and the change silently inverts some
existing configs. Rewrite catch-alls to come last:

    before (longest-wins)          after (last-wins, opencode semantics)
    {"rm *": "deny", "*": "allow"} -> {"*": "allow", "rm *": "deny"}
    {"git *": "allow", "*": "deny"} -> {"*": "deny", "git *": "allow"}

Any rule placed before a `*` is now dead, because `*` matches it too.
`Permissions.describe()` yields the rules in evaluation order so `/permissions`
can show a user which one will actually win, rather than leaving them to
discover it by being surprised.

JSON objects preserve insertion order in Python 3.7+, so a plain object is
enough to express order; a list of ["pattern", "decision"] pairs is accepted
too, for configs that want the ordering to be obvious on the page.

Patterns are glob-style, matched with `fnmatch` (deliberately *not* opencode's
regex translation: haikode's callers escape literals as one-element character
classes — `[*]` — which fnmatch understands and opencode's matcher does not).
"""

import fnmatch
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import PermissionDenied

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

DECISIONS = (ALLOW, ASK, DENY)

# What each permission defaults to when nothing is configured.
DEFAULTS = {
    "read": ALLOW,
    "list": ALLOW,
    "glob": ALLOW,
    "grep": ALLOW,
    "todowrite": ALLOW,
    "webfetch": ASK,
    "edit": ASK,
    "write": ASK,
    "bash": ASK,
    "task": ALLOW,
    # Both already fall back to ASK through the DEFAULTS.get() below; they are
    # spelled out so /permissions and /status list them and a user can find
    # them to configure.
    "question": ASK,
    "external_directory": ASK,
}


def _decision(value: str) -> str:
    """
    One decision word, or ASK when it is not one we know.

    Case and surrounding whitespace are ignored, because `"DENY"` or
    `"deny "` in a hand-edited config used to degrade to ASK — and an ASK is
    exactly what --yes turns into an allow. The one word whose entire job is
    to fail closed must not fail open over a shift key. agents.py normalises
    identically (`_decision` there), so the two channels agree.
    """
    word = value.strip().lower()
    return word if word in DECISIONS else ASK


def _iter_rules(rule) -> Iterable[Tuple[str, str]]:
    """
    (glob, decision) pairs for one permission key, in evaluation order.

    Three spellings are accepted, because order now matters and a list is the
    only shape that makes the order unmistakable to a reader:

        "deny"                                  -> a single catch-all
        {"*": "allow", "rm *": "deny"}          -> object, insertion order
        [["*", "allow"], ["rm *", "deny"]]      -> explicit list of pairs

    A decision that is not allow/ask/deny is yielded as ASK rather than
    skipped: the user meant *something* by writing it, and silently dropping
    the rule would let an earlier, looser rule stand.
    """
    if isinstance(rule, str):
        yield ("*", _decision(rule))
        return
    if isinstance(rule, dict):
        items: Iterable = rule.items()
    elif isinstance(rule, (list, tuple)):
        items = _pairs_from_list(rule)
    else:
        return
    for glob, decision in items:
        if not isinstance(glob, str) or not isinstance(decision, str):
            continue
        yield (glob, _decision(decision))


def _pairs_from_list(rule: Sequence) -> Iterable[Tuple[object, object]]:
    """Unpack the list form: ["pat", "decision"] pairs or one-key objects."""
    for entry in rule:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            yield (entry[0], entry[1])
        elif isinstance(entry, dict):
            if "pattern" in entry and ("action" in entry or "decision" in entry):
                yield (entry["pattern"], entry.get("action", entry.get("decision")))
            elif len(entry) == 1:
                for pair in entry.items():
                    yield pair


class PermissionRequest:
    """
    `patterns` identify this specific action (used for rule matching). Every
    entry is evaluated, so a tool that touches five files must pass five
    patterns: a deny on any one of them denies the whole request.
    `always` is what gets granted when the user answers "always" — opencode
    keeps these separate so that approving one file edit grants edits
    generally, while approving `git status` only grants that command shape.
    """

    def __init__(self, key: str, patterns: List[str], title: str,
                 metadata: Optional[Dict] = None,
                 always: Optional[List[str]] = None):
        self.key = key
        self.patterns = patterns or ["*"]
        self.title = title
        self.metadata = metadata or {}
        self.always = always or self.patterns


class Permissions:
    """
    asker(request) -> "once" | "always" | "reject"
    If asker is None every ASK resolves to reject (headless safety).
    """

    def __init__(self, config=None, asker: Optional[Callable] = None,
                 auto_approve: bool = False, yolo: bool = False):
        self.config = config
        self.asker = asker
        self.auto_approve = auto_approve
        # Every gate off, deny rules included. Unlike auto_approve this is not
        # "approve what was unresolved" but "there is nothing to resolve", so
        # it is deliberately a separate flag: code that means --yes must not
        # accidentally get this.
        self.yolo = yolo
        self._session_grants: Dict[str, List[str]] = {}

    # --- rule lookup --------------------------------------------------

    def _ruleset(self) -> Dict:
        """The permission block of whatever config object we were handed.

        Defensive about shape because the object is not always a Config: an
        agent overlay (agents.AgentPermissions) and status.py's reporting view
        both pass through here.
        """
        data = getattr(self.config, "data", None)
        rules = data.get("permission") if isinstance(data, dict) else None
        return rules if isinstance(rules, dict) else {}

    def _configured(self, key: str, pattern: str) -> Optional[str]:
        """
        The configured decision for `pattern`, or None when no rule matches.

        LAST matching rule wins (opencode permission/index.ts `findLast`), so
        a catch-all placed after a specific rule overrides it. See the
        migration note in the module docstring.
        """
        rule = self._ruleset().get(key)
        if rule is None:
            return None
        decision = None
        for glob, action in _iter_rules(rule):
            if fnmatch.fnmatch(pattern, glob):
                decision = action
        return decision

    def _granted_in_session(self, key: str, pattern: str) -> bool:
        for glob in self._session_grants.get(key, []):
            if fnmatch.fnmatch(pattern, glob):
                return True
        return False

    def decide(self, key: str, pattern: str) -> str:
        """
        allow / ask / deny for one pattern, before the user is consulted.

        A configured DENY is checked first and short-circuits: a session grant
        must not be able to outlive the ruleset it was made under, which is
        exactly what happens on an agent switch into plan mode.
        """
        if self.yolo:
            return ALLOW
        configured = self._configured(key, pattern)
        if configured == DENY:
            return DENY
        if self._granted_in_session(key, pattern):
            return ALLOW
        if configured is not None:
            return configured
        return DEFAULTS.get(key, ASK)

    def grant_always(self, key: str, patterns: List[str]):
        """
        Remember an "always" answer for the rest of the session.

        A grant is a glob matched with `fnmatch`, where `*` spans *every*
        character — `;`, newlines, quotes included. It is therefore the
        caller's job to hand over shapes that cannot describe more than the
        user was shown: see tool/shell.py `_permission_patterns`, which only
        widens `git status` to `git status *` for commands that survive
        `_is_simple`, escapes everything else with `_fnmatch_literal`, and
        prefixes compound commands with `shell: ` so that no grant written for
        a single command can ever match a chain.

        A grant can only relax an ASK. It never overrides a configured deny —
        `decide()` resolves DENY before consulting grants.
        """
        stored = self._session_grants.setdefault(key, [])
        for glob in (patterns or ["*"]):
            if isinstance(glob, str) and glob and glob not in stored:
                stored.append(glob)

    def persist(self, key: str, pattern: str, decision: str) -> bool:
        """
        Write a rule to the config file so it survives restarts.

        Refuses to write anything that would loosen an existing deny. Since
        the last matching rule wins, an appended `{"git status": "allow"}`
        would otherwise silently defeat a `{"*": "deny"}` the project config
        put there — turning "the user answered always once" into a permanent
        hole in someone else's policy. Returns True when a rule was written.
        """
        if self.config is None:
            return False
        if decision != DENY and self._would_widen_a_deny(key, pattern):
            return False
        data = getattr(self.config, "data", None)
        if not isinstance(data, dict):
            return False
        rules = data.setdefault("permission", {})
        if not isinstance(rules, dict):
            return False
        existing = rules.get(key)
        if not isinstance(existing, dict):
            # Whatever form the rule was written in is flattened to an ordered
            # object so the new pattern can be appended after it: a flat "ask"
            # becomes {"*": "ask"} (agents._from_agent relies on that shape),
            # and a list keeps its order rather than being thrown away.
            rules[key] = dict(_iter_rules(existing))
        # Deleting first keeps re-persisting an existing pattern meaningful:
        # dict assignment would leave it in its old, now-outranked position.
        rules[key].pop(pattern, None)
        rules[key][pattern] = decision
        self.config.save()
        return True

    def _would_widen_a_deny(self, key: str, pattern: str) -> bool:
        """
        True when a non-deny rule for `pattern` would eat into a deny.

        Whether one glob is a subset of another is undecidable in general, so
        this errs strict in both directions: refuse when the new pattern is
        covered by a deny glob, and also when the new pattern (read as a glob)
        covers a deny rule's text.
        """
        for glob, action in _iter_rules(self._ruleset().get(key)):
            if action != DENY:
                continue
            if fnmatch.fnmatch(pattern, glob) or fnmatch.fnmatch(glob, pattern):
                return True
        return False

    # --- introspection -------------------------------------------------

    def describe(self) -> List[Tuple[str, str, str, bool]]:
        """
        Every rule as (key, pattern, decision, configured), evaluation order.

        Within a key the rows are in the order they will be evaluated, and the
        LAST matching row wins — which is the whole reason this exists. A key
        with no rules of its own contributes one synthetic row carrying its
        DEFAULTS entry with configured=False.
        """
        ruleset = self._ruleset()
        rows: List[Tuple[str, str, str, bool]] = []
        for key in sorted(set(DEFAULTS) | {k for k in ruleset if isinstance(k, str)}):
            pairs = list(_iter_rules(ruleset.get(key)))
            if not pairs:
                rows.append((key, "*", DEFAULTS.get(key, ASK), False))
                continue
            for glob, decision in pairs:
                rows.append((key, glob, decision, True))
        return rows

    def session_grants(self) -> Dict[str, List[str]]:
        """
        A copy of the "always" answers given this run, by permission key.

        These are not in `describe()` because they are not rules: they only
        upgrade an ASK, and a configured deny still wins over all of them. A
        front-end listing rules should show them separately for the same
        reason.
        """
        return {key: list(globs) for key, globs in self._session_grants.items()}

    # --- the call tools use -------------------------------------------

    def ask(self, request: PermissionRequest):
        """Raises PermissionDenied unless every pattern in the request is allowed."""
        if self.yolo:
            if ((request.metadata or {}).get("kind") == "question"
                    and self.asker is not None):
                # yolo switches gates off, but it cannot answer on the
                # user's behalf: a question collects an answer, it does not
                # grant a permission. Skipping the asker here left the
                # question tool and plan approval silently unanswerable in
                # every --yolo session.
                answer = self.asker(request)
                if answer in ("once", "always"):
                    return
                raise PermissionDenied(f"User rejected: {request.title}")
            return
        key = request.key
        needs_ask = False

        # Deny wins over everything, so the whole loop runs before either
        # auto_approve or the asker gets a say.
        for pattern in request.patterns:
            decision = self.decide(key, pattern)
            if decision == DENY:
                raise PermissionDenied(
                    f"{key} denied by configuration: {pattern}")
            if decision != ALLOW:
                needs_ask = True

        if not needs_ask:
            return
        if self.auto_approve:
            # --yes auto-approves what is merely unresolved, never a deny.
            return
        if self.asker is None:
            raise PermissionDenied(
                f"{key} requires approval but no interactive session is available")

        answer = self.asker(request)
        if answer == "always":
            self.grant_always(key, request.always)
            return
        if answer == "once":
            return
        raise PermissionDenied(f"User rejected: {request.title}")
