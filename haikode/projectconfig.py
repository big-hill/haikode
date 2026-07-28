"""
Per-project configuration: haikode's equivalent of opencode.json,
.claude/settings.json and codex' config.toml.

Ported from opencode's config/config.ts + config/paths.ts:

- the global config file is the weakest layer, then every ancestor directory
  from the project root down to the cwd, so the file nearest the cwd wins
- within one directory a dotted `.haikode/haikode.json` beats a plain
  `haikode.json`, the same way opencode prefers `.opencode/opencode.json`
- `opencode.json` is read as a compatibility source so an existing opencode
  project behaves sensibly without being converted first; a native
  `haikode.json` in the same directory always wins over it
- `instructions` arrays are concatenated across layers (opencode's
  mergeConfigConcatArrays) instead of replaced, everything else deep-merges

Nothing here raises on bad input. A syntax error in a project file must never
stop the agent from starting: the file is skipped and the reason is recorded in
`.errors`, so /status and doctor views can show what went wrong.

A project config arrives with a checked-out repository, so it is untrusted
input — `git clone && haikode` must not be a way to hand a stranger the user's
API key. Every setting is therefore classified as one of two kinds:

- SAFE: a project may set it freely (model, instructions, agents, commands,
  context, theme, ...). The worst case is a worse coding session.
- PRIVILEGED: a project may only *narrow* it, never widen or redirect it.
  That is anything under `providers` which decides where a request goes or
  which credential rides with it (see PRIVILEGED_PROVIDER_FIELDS), any
  `permission` or `tools` rule that loosens what the user settled on, and
  every `mcp` entry, because registering an MCP server starts a process.

A PRIVILEGED setting from an untrusted file is dropped and recorded in
`.refusals`, so the front-end can say so rather than the setting silently not
working. The user opts in per repository with `trust(cwd)`; the decision lives
in the user's global config keyed by the repository root, never in the project
(a repository that could grant itself trust would not be a boundary at all).

Three further limits have no counterpart in opencode:

- instruction paths declared by a project file must resolve inside the project
  (`resolve_instructions(allow_outside=True)` opts out); without this,
  `"instructions": ["../../../.ssh/id_rsa"]` in a cloned repo would paste a
  private key into the system prompt on the first turn
- globs are bounded (see MAX_GLOB_SCAN) and an absolute entry only globs its
  own basename, the way opencode does, so `/**/*.md` cannot walk the disk
- `escalations()` reports every project-level rule that loosens permissions
  relative to the defaults plus the user's own global config, and
  `effective_permissions()` drops them unless the project is trusted. This is
  deliberately *not* opencode's behaviour, where the project file always wins.

Nothing here ever fails open: an unreadable trust store, a corrupt one, or an
exception while classifying means "untrusted".
"""

import copy
import fnmatch
import json
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import (Any, Dict, List, NamedTuple, Optional, Sequence, Tuple)

from .config import deep_merge
from .context import global_config_dir
from .permission import ALLOW, ASK, DENY, DEFAULTS as PERMISSION_DEFAULTS

CONFIG_NAME = "haikode.json"
CONFIG_DIR = ".haikode"
COMPAT_NAME = "opencode.json"
COMPAT_DIR = ".opencode"

# Where per-repository trust decisions are recorded, inside the user's own
# global config directory.
TRUST_NAME = "trust.json"

# Provider fields that decide where a request goes and which credential rides
# with it. runtime.build_provider() pairs `base_url` with a globally stored API
# key, so a checked-out repository able to set one of these could redirect that
# key to a host of its choosing — the worst thing a config file can do here.
# opencode's camelCase spellings are listed alongside haikode's, because a
# compatibility source is read with exactly the same trust as a native one, and
# `options` is listed because that is where opencode hides baseURL/apiKey.
PRIVILEGED_PROVIDER_FIELDS = frozenset({
    "api_key", "apiKey", "auth", "base_url", "baseURL", "dialect", "headers",
    "key_env", "keyEnv", "npm", "oauth_method", "oauth_provider",
    "oauthProvider", "options", "requires_key", "requiresKey",
})

# The only keys an untrusted `mcp` entry may carry: turning a server the user
# configured off is a narrowing, everything else registers a process to launch.
MCP_DISABLE_KEYS = ("enabled", "disabled")

# opencode caps nothing here, but an unbounded instruction glob (say `**/*.md`
# in a docs repo) would silently eat the whole context window.
MAX_INSTRUCTION_FILES = 32

# Entries the glob is allowed to look at before giving up. The cap above only
# limits what is kept; without this one `**/*.md` still walks the whole tree.
MAX_GLOB_SCAN = 5000

# A config file is hand-written JSON. Anything larger is a mistake or a bomb,
# and read_text() on it would be charged to the user's RAM before parsing.
MAX_CONFIG_BYTES = 1024 * 1024

DECISIONS = (ALLOW, ASK, DENY)

# How permissive each decision is, for detecting a project file that loosens
# what the user's own configuration had settled on.
RANK = {DENY: 0, ASK: 1, ALLOW: 2}

GLOB_CHARS = "*?["

# Keys this implementation understands. Names match opencode's where opencode
# has one, so a config can be shared between the two tools.
KNOWN_KEYS: Tuple[str, ...] = (
    "model",
    "provider",
    "providers",
    "instructions",
    "agents",
    "commands",
    "permission",
    "tools",
    "mcp",
    "shell",
    "max_steps",
    "context",
    "default_agent",
    "theme",
    "username",
)

# opencode spells a few of these in the singular; accept both spellings so a
# copied opencode.json keeps working.
ALIASES: Dict[str, str] = {
    "agent": "agents",
    "command": "commands",
}

# Real opencode keys that haikode has no equivalent for. Silently ignored
# rather than reported, otherwise every imported opencode.json would produce a
# wall of "unknown key" warnings that trains users to ignore the warning line.
IGNORED_KEYS = frozenset({
    "$schema", "_comment", "attachment", "autoshare", "autoupdate",
    "compaction", "disabled_providers", "enabled_providers", "enterprise",
    "experimental", "formatter", "keybinds", "layout", "logLevel", "lsp",
    "mode", "plugin", "reference", "references", "server", "share", "skills",
    "small_model", "snapshot", "subagent_depth", "tool_output", "tui",
    "watcher",
})

INIT_COMMENT = [
    "haikode project configuration. JSON has no comment syntax, so this key",
    "carries the documentation instead; haikode ignores keys starting with _.",
    "Supported keys: model (\"provider/model\"), provider, instructions",
    "(paths or globs, relative to this file), agents, commands, permission",
    "(key -> allow|ask|deny, or key -> {glob: decision}), tools (name -> false",
    "to disable), mcp, shell, max_steps, context, default_agent, theme,",
    "username. Files nearer the working directory override files further up.",
]


def _project_root(start: Path) -> Path:
    """The git worktree root if there is one, else the filesystem root.

    Mirrors opencode, which stops walking up at the worktree so a config in an
    unrelated parent directory cannot leak into the project.
    """
    for directory in [start, *start.parents]:
        if (directory / ".git").exists():
            return directory
    return Path(start.anchor) if start.anchor else Path("/")


def _boundary(start: Path, stop: Optional[Path]) -> Path:
    """Where the upward walk stops.

    `stop` is only honoured when it actually contains `start`; a boundary that
    is not an ancestor would never be hit by the walk, which would silently
    turn discovery into "read every haikode.json between / and here".
    """
    if stop is not None and (stop == start or stop in start.parents):
        return stop
    return _project_root(start)


def project_root(cwd: str = ".", stop: Optional[str] = None) -> Path:
    """The directory discovery treats as the project boundary."""
    start = Path(cwd).expanduser().resolve()
    return _boundary(start, Path(stop).expanduser().resolve() if stop else None)


def _directory_chain(start: Path, stop: Optional[Path]) -> List[Path]:
    """Root-first list of directories to scan, weakest layer first."""
    boundary = _boundary(start, stop)
    chain = [start]
    if start != boundary:
        for parent in start.parents:
            chain.append(parent)
            if parent == boundary:
                break
    chain.reverse()
    return chain


def _candidates(directory: Path) -> List[Path]:
    """Config files for one directory, weakest first.

    Compatibility sources lose to native ones, and the dotted config directory
    wins over the plain file (opencode loads `.opencode/opencode.json` last).
    """
    return [
        directory / COMPAT_NAME,
        directory / COMPAT_DIR / COMPAT_NAME,
        directory / CONFIG_NAME,
        directory / CONFIG_DIR / CONFIG_NAME,
    ]


def discover_files(cwd: str = ".", stop: Optional[str] = None) -> List[Path]:
    """Existing config files in load order: weakest (global) first."""
    start = Path(cwd).expanduser().resolve()
    boundary = Path(stop).expanduser().resolve() if stop else None

    paths: List[Path] = []
    global_dir = global_config_dir()
    paths.append(global_dir / COMPAT_NAME)
    paths.append(global_dir / CONFIG_NAME)
    for directory in _directory_chain(start, boundary):
        paths.extend(_candidates(directory))

    found: List[Path] = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            continue
    return found


# --- per-repository trust ------------------------------------------------
#
# The store is a plain file in the user's global config directory. It is read
# on every load, so it stays a flat {path: metadata} map: no index, no cache
# that could go stale and answer "trusted" for a directory that no longer is.


def _trust_file() -> Path:
    return global_config_dir() / TRUST_NAME


def _read_trust() -> Dict[str, Any]:
    """The trust store, or {} if it cannot be read or understood.

    Fails closed by construction: every failure path yields "nothing is
    trusted", never "trust everything".
    """
    try:
        text = _trust_file().read_text()
    except OSError:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    entries = parsed.get("trusted") if isinstance(parsed, dict) else None
    if isinstance(entries, list):        # accept a bare list of paths
        entries = {p: {} for p in entries if isinstance(p, str)}
    if not isinstance(entries, dict):
        return {}
    return {key: value for key, value in entries.items() if isinstance(key, str)}


def _write_trust(entries: Dict[str, Any]) -> None:
    path = _trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "trusted": entries}, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass                              # a filesystem without modes (Haiku fs)


def trust_key(cwd: str = ".") -> str:
    """The path a trust decision for `cwd` is recorded under.

    The repository root, so trusting a project covers its subdirectories but
    stops at the next worktree: a vendored checkout or submodule inside a
    trusted project has a `.git` of its own and stays untrusted, and a sibling
    clone never inherits anything. A directory that is in no worktree is only
    ever trusted for itself — its "root" would be `/`, and recording trust for
    that would trust every directory on the machine.
    """
    start = Path(cwd).expanduser().resolve()
    root = _project_root(start)
    return str(start if root == Path(root.anchor) else root)


def is_trusted(cwd: str = ".") -> bool:
    """True when the user has marked the repository containing `cwd` trusted."""
    try:
        return trust_key(cwd) in _read_trust()
    except OSError:
        return False


def trust(cwd: str = ".") -> str:
    """Record that the user trusts the repository containing `cwd`.

    Stored in the user's own global config and keyed by the resolved
    repository root; nothing about this decision is ever written into the
    project, which is the whole point. Returns the key it was recorded under.
    """
    key = trust_key(cwd)
    entries = _read_trust()
    entries[key] = {"granted_at": datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")}
    _write_trust(entries)
    return key


def untrust(cwd: str = ".") -> bool:
    """Revoke trust for the repository containing `cwd`; True if it had any."""
    key = trust_key(cwd)
    entries = _read_trust()
    if key not in entries:
        return False
    del entries[key]
    _write_trust(entries)
    return True


def trusted_projects() -> List[str]:
    """Every path the user has trusted, for a listing or a revoke picker."""
    return sorted(_read_trust())


def _is_glob(entry: str) -> bool:
    return any(char in entry for char in GLOB_CHARS)


def _tool_enabled(name: str, rules: Dict[str, bool]) -> bool:
    """Resolve one tool name against one `tools` map.

    The longest matching key wins, so a specific name can re-enable one tool
    inside a disabled group. An exact name always beats a glob, however long
    the glob is — otherwise `{"read*": false, "read": true}` would disable the
    tool it just named.
    """
    decision, best = True, (-1, -1)
    for pattern, enabled in rules.items():
        if not fnmatch.fnmatch(name, pattern):
            continue
        score = (1 if pattern == name else 0, len(pattern))
        if score > best:
            decision, best = bool(enabled), score
    return decision


def _under(path: Path, root: Path) -> bool:
    """True when `path` is `root` or lives inside it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class Escalation(NamedTuple):
    """A project-level rule that is more permissive than the user's baseline.

    `agent` is set when the rule is nested in an agent definition rather than
    the top-level `permission` block. Those cannot be filtered out here (the
    agents module owns that dict), so they are reported and nothing more.
    """

    source: Path
    key: str
    pattern: Optional[str]
    decision: str
    baseline: str
    agent: Optional[str] = None

    @property
    def message(self) -> str:
        target = f"{self.key}.{self.pattern}" if self.pattern else self.key
        scope = f"agents.{self.agent}.permission" if self.agent else "permission"
        return (f"{self.source}: {scope}.{target} widens "
                f"{self.baseline} to {self.decision}")


class Refusal(NamedTuple):
    """A PRIVILEGED setting an untrusted project config was not allowed to make.

    Kept separate from `.errors` (the file is not malformed) and from
    `.warnings` (nothing was merely capped): this is the one list that says a
    checkout tried to change where credentials go, and a front-end should be
    able to show exactly that.
    """

    source: Path
    setting: str
    reason: str

    @property
    def message(self) -> str:
        return f"{self.source}: ignored {self.setting} ({self.reason})"


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


class ProjectConfig:
    """The merged per-project settings, plus where each piece came from."""

    def __init__(self, cwd: str = ".", global_config: Any = None,
                 stop: Optional[str] = None, trusted: Optional[bool] = None):
        self.cwd = Path(cwd).expanduser().resolve()
        self.root = project_root(str(self.cwd), stop)
        self.global_config = global_config
        # Whether the user has vouched for this repository. None means "ask the
        # trust store"; an explicit value is for callers that just asked the
        # user, and for tests. Anything other than True is untrusted.
        self.trusted = is_trusted(str(self.cwd)) if trusted is None else trusted is True
        self.data: Dict[str, Any] = {}
        self.sources: List[Path] = []
        self.unknown: List[str] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.refusals: List[Refusal] = []
        # (declaring file, raw entry) so globs resolve against the file that
        # declared them and not against the cwd.
        self.instruction_sources: List[Tuple[Path, str]] = []
        # (file, validated contents) per layer. Kept because "which file asked
        # for this?" is unanswerable once everything is merged into .data, and
        # escalation reporting has to distinguish the user's own global config
        # from whatever the checked-out repository ships.
        self.layers: List[Tuple[Path, Dict[str, Any]]] = []

    # --- loading ---------------------------------------------------------

    @classmethod
    def load(cls, cwd: str = ".", global_config: Any = None,
             stop: Optional[str] = None,
             trusted: Optional[bool] = None) -> "ProjectConfig":
        """Discover and merge every config file that applies to `cwd`.

        `stop` overrides the automatic git-root boundary; it exists so callers
        (and tests) can scope discovery to a known directory. `trusted`
        overrides the stored trust decision, for a caller that has just asked
        the user.
        """
        config = cls(cwd, global_config, stop, trusted)
        for path in discover_files(str(config.cwd), stop):
            raw = config._read(path)
            if raw is None:
                continue
            config.sources.append(path)
            config._merge(config._normalize(raw, path), path)
        return config

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            size = path.stat().st_size
            if size > MAX_CONFIG_BYTES:
                self.errors.append(
                    f"{path}: too large to be a config file ({size} bytes)")
                return None
            text = path.read_text(errors="replace")
        except OSError as e:
            self.errors.append(f"{path}: {e}")
            return None
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as e:
            self.errors.append(f"{path}: invalid JSON ({e})")
            return None
        if not isinstance(parsed, dict):
            self.errors.append(f"{path}: expected a JSON object at the top level")
            return None
        return parsed

    def _merge(self, incoming: Dict[str, Any], path: Path):
        self.layers.append((path, incoming))
        previous = self.data.get("instructions") or []
        self.data = deep_merge(self.data, incoming)
        if previous and incoming.get("instructions"):
            # opencode concatenates instructions rather than replacing them, so
            # a project file adds to the global list instead of hiding it.
            self.data["instructions"] = _dedupe(
                list(previous) + list(incoming["instructions"]))
        for entry in incoming.get("instructions", []):
            self.instruction_sources.append((path, entry))

    # --- validation ------------------------------------------------------

    def _may_privilege(self, path: Path) -> bool:
        """True when `path` may set PRIVILEGED keys.

        Either the user wrote the file themselves, or they explicitly vouched
        for this repository. Nothing else earns it.
        """
        return self._user_authored(path) or self.trusted

    def _refuse(self, path: Path, setting: str, reason: str) -> None:
        """Record a PRIVILEGED setting an untrusted file was not allowed to make.

        Deduplicated by value: merged_with() may be called once per frame by a
        status view, and a warning list that grows on every render is worse
        than no warning at all.
        """
        entry = Refusal(path, setting, reason)
        if entry not in self.refusals:
            self.refusals.append(entry)

    def _strip_privileged(self, raw: Dict[str, Any],
                          path: Path) -> Dict[str, Any]:
        """Drop the routing, credential and process settings from one layer.

        Runs before validation so the values never reach `.data` at all: every
        consumer of a merged config — build_provider, the MCP manager, /status
        — would otherwise have to remember to re-check them.
        """
        out = dict(raw)
        for key in ("provider", "providers"):
            record = out.get(key)
            if not isinstance(record, dict):
                continue        # `provider` as a string is a name, checked later
            cleaned: Dict[str, Any] = {}
            for name, spec in record.items():
                if not isinstance(spec, dict):
                    cleaned[name] = spec
                    continue
                kept = {}
                for field, value in spec.items():
                    if field in PRIVILEGED_PROVIDER_FIELDS:
                        self._refuse(
                            path, f"{key}.{name}.{field}",
                            "a project may not change where a request goes or "
                            "which credential it carries")
                    else:
                        kept[field] = value
                cleaned[name] = kept
            out[key] = cleaned

        servers = out.get("mcp")
        if isinstance(servers, dict) and servers:
            narrowed = {}
            for name, entry in servers.items():
                off = {k: v for k, v in entry.items() if k in MCP_DISABLE_KEYS} \
                    if isinstance(entry, dict) else {}
                if off.get("enabled") is False or off.get("disabled") is True:
                    narrowed[name] = off   # turning one off is a narrowing
                    continue
                self._refuse(path, f"mcp.{name}",
                             "registering an MCP server starts a process")
            out["mcp"] = narrowed
        return out

    def _normalize(self, raw: Dict[str, Any], path: Path) -> Dict[str, Any]:
        """Keep the keys we understand, record the rest, drop bad shapes."""
        if not self._may_privilege(path):
            raw = self._strip_privileged(raw, path)
        out: Dict[str, Any] = {}
        for key, value in raw.items():
            name = ALIASES.get(key, key)
            if key in IGNORED_KEYS or key.startswith("_"):
                continue
            if name not in KNOWN_KEYS:
                self.unknown.append(f"{path}: {key}")
                continue
            if name == "provider" and isinstance(value, dict):
                # opencode's `provider` is a record of provider overrides;
                # haikode's is the provider name. Route a dict to `providers`.
                checked = self._check_providers(value, path)
                if checked is not None:
                    out["providers"] = deep_merge(out.get("providers", {}), checked)
                continue
            handler = getattr(self, "_check_" + name, None)
            checked = handler(value, path) if handler else value
            if checked is None:
                continue
            if name == "providers" and isinstance(out.get("providers"), dict):
                # A file may carry both spellings; neither should clobber the
                # other regardless of which one JSON listed first.
                out["providers"] = deep_merge(out["providers"], checked)
            else:
                out[name] = checked
        return out

    def _reject(self, path: Path, message: str) -> None:
        self.errors.append(f"{path}: {message}")
        return None

    def _check_str(self, key: str, value: Any, path: Path) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return self._reject(path, f"{key} must be a non-empty string")
        return value.strip()

    def _check_int(self, key: str, value: Any, path: Path) -> Optional[int]:
        # bool is an int in Python; "max_steps": true is a mistake, not a 1.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return self._reject(path, f"{key} must be a positive integer")
        return value

    def _check_model(self, value: Any, path: Path) -> Optional[str]:
        return self._check_str("model", value, path)

    def _check_shell(self, value: Any, path: Path) -> Optional[str]:
        return self._check_str("shell", value, path)

    def _check_theme(self, value: Any, path: Path) -> Optional[str]:
        return self._check_str("theme", value, path)

    def _check_username(self, value: Any, path: Path) -> Optional[str]:
        return self._check_str("username", value, path)

    def _check_default_agent(self, value: Any, path: Path) -> Optional[str]:
        return self._check_str("default_agent", value, path)

    def _check_max_steps(self, value: Any, path: Path) -> Optional[int]:
        return self._check_int("max_steps", value, path)

    def _check_context(self, value: Any, path: Path) -> Optional[int]:
        return self._check_int("context", value, path)

    def _check_provider(self, value: Any, path: Path) -> Optional[str]:
        return self._check_str("provider", value, path)

    def _check_providers(self, value: Any, path: Path) -> Optional[Dict]:
        if not isinstance(value, dict):
            return self._reject(path, "providers must be an object")
        return value

    def _check_mcp(self, value: Any, path: Path) -> Optional[Dict]:
        if not isinstance(value, dict):
            return self._reject(path, "mcp must be an object")
        return value

    def _check_instructions(self, value: Any, path: Path) -> Optional[List[str]]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return self._reject(path, "instructions must be a list of paths")
        entries = []
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                entries.append(entry.strip())
            else:
                self.errors.append(
                    f"{path}: instructions entries must be strings")
        return entries

    def _check_agents(self, value: Any, path: Path) -> Optional[Dict]:
        """Shape only — the agents module owns what the fields mean."""
        if not isinstance(value, dict):
            return self._reject(path, "agents must be an object")
        agents = {}
        for name, spec in value.items():
            if isinstance(spec, dict):
                agents[name] = spec
            else:
                self.errors.append(f"{path}: agents.{name} must be an object")
        return agents

    def _check_commands(self, value: Any, path: Path) -> Optional[Dict]:
        if not isinstance(value, dict):
            return self._reject(path, "commands must be an object")
        commands = {}
        for name, spec in value.items():
            if isinstance(spec, str):
                spec = {"template": spec}
            if not isinstance(spec, dict):
                self.errors.append(f"{path}: commands.{name} must be an object")
                continue
            if not isinstance(spec.get("template"), str) or not spec["template"]:
                self.errors.append(
                    f"{path}: commands.{name} needs a template string")
                continue
            commands[name] = spec
        return commands

    def _check_tools(self, value: Any, path: Path) -> Optional[Dict[str, bool]]:
        if not isinstance(value, dict):
            return self._reject(path, "tools must be an object of name -> bool")
        tools = {}
        for name, enabled in value.items():
            if isinstance(enabled, bool):
                tools[name] = enabled
            else:
                self.errors.append(f"{path}: tools.{name} must be true or false")
        return tools

    def _check_permission(self, value: Any, path: Path) -> Optional[Dict]:
        # opencode normalizes a bare string to {"*": action}; haikode looks
        # rules up by permission key, so spread it over the known keys instead.
        if isinstance(value, str):
            if value not in DECISIONS:
                return self._reject(
                    path, f"permission must be one of {', '.join(DECISIONS)}")
            return {key: value for key in PERMISSION_DEFAULTS}
        if not isinstance(value, dict):
            return self._reject(path, "permission must be an object or a decision")

        rules: Dict[str, Any] = {}
        for key, rule in value.items():
            if isinstance(rule, str):
                if rule not in DECISIONS:
                    self.errors.append(
                        f"{path}: permission.{key} must be one of "
                        f"{', '.join(DECISIONS)}")
                    continue
                rules[key] = rule
            elif isinstance(rule, dict):
                patterns = {}
                for pattern, decision in rule.items():
                    if decision in DECISIONS:
                        patterns[pattern] = decision
                    else:
                        self.errors.append(
                            f"{path}: permission.{key}.{pattern} must be one "
                            f"of {', '.join(DECISIONS)}")
                if patterns:
                    rules[key] = patterns
            else:
                self.errors.append(
                    f"{path}: permission.{key} must be a decision or a "
                    "pattern object")
        return rules

    # --- effective settings ---------------------------------------------

    def merged_with(self, global_config: Any = None,
                    allow_escalation: Optional[bool] = None) -> Dict[str, Any]:
        """Effective settings: global config first, project settings on top.

        Dicts deep-merge, scalars and lists are replaced. The project keys that
        opencode names differently from haikode's global config (`model`,
        `provider`) are also projected onto the global schema, so the result can
        be handed straight to the pieces that read `default_provider` and
        `providers[...]`.

        `allow_escalation` defaults to this project's trust: an untrusted
        checkout can only ever tighten permissions.
        """
        source = global_config if global_config is not None else self.global_config
        base = getattr(source, "data", source) or {}
        # Deep copy both sides: deep_merge shares every sub-dict it does not
        # overwrite, so without this the projections below would write into the
        # live global Config, and the caller would be handed dicts that alias
        # self.data.
        merged = deep_merge(copy.deepcopy(dict(base)),
                            copy.deepcopy(self.data))
        global_permission = base.get("permission")

        provider, model = self._selected_model(base)
        providers = merged.setdefault("providers", {})
        if provider:
            merged["default_provider"] = provider
        target = provider or merged.get("default_provider")
        if target and isinstance(providers.get(target), dict):
            if model:
                providers[target]["model"] = model
            if self.data.get("context"):
                providers[target]["context"] = self.data["context"]
        elif model and not provider:
            # A bare model id with no provider: apply it to whatever the
            # default provider is, if that provider is known.
            default = merged.get("default_provider")
            if isinstance(providers.get(default), dict):
                providers[default]["model"] = model

        # Rebuild from the global rules so the tools-derived rules land between
        # the two layers instead of on top of the project's own permission block.
        # {} rather than None: the baseline is the config we were handed, even
        # when it declares no rules, not whatever self.global_config holds.
        merged["permission"] = self.effective_permissions(
            global_permission if isinstance(global_permission, dict) else {},
            allow_escalation=allow_escalation)
        return merged

    def resolve_model(self) -> Tuple[Optional[str], Optional[str]]:
        """Split `model` into (provider, model), honouring an explicit `provider`."""
        provider = self.data.get("provider")
        model = self.data.get("model")
        if model and "/" in model:
            head, _, tail = model.partition("/")
            if tail:
                provider = provider or head
                model = tail
        return provider or None, model or None

    def _declared_by(self, key: str) -> Optional[Path]:
        """The last layer that set `key`, so a refusal can name a file."""
        for path, layer in reversed(self.layers):
            if key in layer:
                return path
        return None

    def _user_providers(self, base: Dict[str, Any]) -> set:
        """Provider names the user themselves configured."""
        names = set(base.get("providers") or {})
        for path, layer in self.layers:
            if self._user_authored(path):
                names.update(layer.get("providers") or {})
        return names

    def _selected_model(self, base: Dict[str, Any]) -> Tuple[Optional[str],
                                                             Optional[str]]:
        """resolve_model(), with an untrusted provider *name* refused.

        Stripping `base_url` is not enough on its own: a checkout that names a
        provider the user never configured would otherwise decide which entry
        the credential lookup and the endpoint are read from. A project may
        pick among the user's own providers, and nothing else.
        """
        provider, model = self.resolve_model()
        if not provider:
            return provider, model
        declaring = self._declared_by("provider") or self._declared_by("model")
        if declaring is None or self._may_privilege(declaring):
            return provider, model
        if provider in self._user_providers(base):
            return provider, model
        self._refuse(declaring, f"provider {provider!r}",
                     "a project may only select a provider the user configured")
        return None, None

    def _user_authored(self, path: Path) -> bool:
        """True for files the user wrote, as opposed to files a repo shipped."""
        return _under(path, global_config_dir())

    def _user_tools(self) -> Dict[str, bool]:
        """The `tools` map from configuration the user wrote themselves."""
        rules: Dict[str, bool] = {}
        source = getattr(self.global_config, "data", self.global_config)
        if isinstance(source, dict):
            for name, enabled in (source.get("tools") or {}).items():
                if isinstance(enabled, bool):
                    rules[name] = enabled
        for path, layer in self.layers:
            if self._user_authored(path):
                rules.update(layer.get("tools") or {})
        return rules

    def _project_tools(self) -> Dict[str, bool]:
        """The `tools` map from configuration that arrived with the checkout."""
        rules: Dict[str, bool] = {}
        for path, layer in self.layers:
            if not self._user_authored(path):
                rules.update(layer.get("tools") or {})
        return rules

    def _baseline(self, base: Optional[Dict] = None) -> Dict[str, str]:
        """The strictest thing a project file is allowed to assume.

        Built-in defaults, then the global Config, then any config file found
        under the global config directory — everything the user themselves
        opted into. A per-pattern block counts at its most permissive pattern,
        so `bash: {"git *": "allow"}` in the user's own config does not make a
        project-level blanket `bash: allow` look like an escalation.
        """
        ranks = dict(PERMISSION_DEFAULTS)

        def absorb(layer: Optional[Dict[str, Any]]) -> None:
            for key, rule in (layer or {}).items():
                if isinstance(rule, str) and rule in RANK:
                    ranks[key] = rule
                elif isinstance(rule, dict):
                    decisions = [d for d in rule.values() if d in RANK]
                    if decisions:
                        ranks[key] = max(decisions, key=lambda d: RANK[d])

        absorb(base if isinstance(base, dict) else None)
        for path, layer in self.layers:
            if self._user_authored(path):
                absorb(layer.get("permission"))
        for name, enabled in self._user_tools().items():
            if not _is_glob(name):
                ranks[name] = ALLOW if enabled else DENY
        return ranks

    def escalations(self, base: Optional[Dict] = None) -> List[Escalation]:
        """Project-level rules that loosen permissions, worst offender first.

        A checked-out repository can ship a haikode.json, so "this file wants
        bash to stop asking" has to be something the caller can see and refuse
        rather than something that just happens.
        """
        if base is None:
            source = self.global_config
            data = getattr(source, "data", source)
            base = data.get("permission") if isinstance(data, dict) else None
        ranks = self._baseline(base if isinstance(base, dict) else None)
        found: List[Escalation] = []

        def check(path: Path, key: str, pattern: Optional[str], decision: str,
                  agent: Optional[str] = None):
            floor = ranks.get(key, ASK)
            if RANK.get(decision, 1) > RANK[floor]:
                found.append(
                    Escalation(path, key, pattern, decision, floor, agent))

        def scan(path: Path, rules: Any, agent: Optional[str] = None):
            for key, rule in (rules or {}).items():
                if isinstance(rule, str):
                    check(path, key, None, rule, agent)
                elif isinstance(rule, dict):
                    for pattern, decision in rule.items():
                        if isinstance(decision, str):
                            check(path, key, pattern, decision, agent)

        for path, layer in self.layers:
            if self._user_authored(path):
                continue
            scan(path, layer.get("permission"))
            for name, enabled in (layer.get("tools") or {}).items():
                if enabled and not _is_glob(name):
                    check(path, name, None, ALLOW)
            # An agent definition carries its own permission block, so a repo
            # could otherwise widen bash there and never show up here.
            for name, spec in (layer.get("agents") or {}).items():
                if isinstance(spec, dict):
                    scan(path, spec.get("permission"), name)

        found.sort(key=lambda e: -RANK.get(e.decision, 1))
        return found

    def effective_permissions(self, base: Optional[Dict] = None,
                              allow_escalation: Optional[bool] = None
                              ) -> Dict[str, Any]:
        """Permission rules ready for permission.Permissions via a config object.

        `tools` is folded in the way opencode does it: a disabled tool becomes a
        deny rule, an explicitly enabled one an allow rule, and anything spelled
        out under `permission` still wins.

        Unless the project is trusted (or `allow_escalation=True` overrides
        that), every project-level rule that would loosen the user's own
        baseline is dropped, so an untrusted checkout can only ever tighten.
        Tightening rules survive either way.
        """
        if allow_escalation is None:
            allow_escalation = self.trusted
        blocked = set()
        if not allow_escalation:
            # Agent-scoped escalations are reported, not filtered: those rules
            # live in the agents dict, which this function does not produce.
            blocked = {(e.key, e.pattern) for e in self.escalations(base)
                       if e.agent is None}

        # The user's own tool rules first, so that dropping a checkout's
        # re-enable leaves the user's "off" standing rather than nothing at all.
        derived: Dict[str, Any] = {}
        for group in (self._user_tools(), self._project_tools()):
            for name, enabled in group.items():
                if _is_glob(name):
                    continue  # a pattern is a tool filter, not a permission key
                if enabled and (name, None) in blocked:
                    continue
                derived[name] = ALLOW if enabled else DENY

        rules = deep_merge(copy.deepcopy(dict(base or {})), derived)

        declared: Dict[str, Any] = {}
        for key, rule in (self.data.get("permission") or {}).items():
            if isinstance(rule, dict):
                kept = {pattern: decision for pattern, decision in rule.items()
                        if (key, pattern) not in blocked}
                if kept:
                    declared[key] = kept
            elif (key, None) not in blocked:
                declared[key] = rule
        return deep_merge(rules, copy.deepcopy(declared))

    def enabled_tools(self, all_names: Sequence[str]) -> List[str]:
        """Filter tool names through the `tools` on/off maps.

        Within one map the longest matching key wins (see _tool_enabled), so a
        specific name can re-enable one tool inside a disabled group.

        The user's map and the checkout's map are resolved separately and then
        ANDed. A repository can still carve an exception out of a group rule it
        wrote itself, but it can never switch back on a tool the user turned
        off — that is the same widening `permission` is refused, and merging
        the two maps first would have hidden it. A trusted project is one
        layer again, which is opencode's behaviour.
        """
        user, project = self._user_tools(), self._project_tools()
        groups = [{**user, **project}] if self.trusted else [user, project]
        groups = [rules for rules in groups if rules]
        if not groups:
            return list(all_names)
        return [name for name in all_names
                if all(_tool_enabled(name, rules) for rules in groups)]

    def _instruction_roots(self, declaring: Path) -> List[Path]:
        """Directories a project-declared instruction file may live in."""
        roots = [global_config_dir()]
        if self.root != Path(self.root.anchor):
            roots.append(self.root)
        else:
            # No git worktree to bound the project: fall back to the two
            # directories that are unambiguously in play.
            roots.extend([self.cwd, declaring.parent])
        return roots

    def resolve_instructions(self, allow_outside: bool = False) -> List[Path]:
        """Expand `instructions` entries relative to the file that declared them.

        Entries declared by a project file must land inside the project (or the
        global config directory); the config came with the checkout, so letting
        it name `../../../.ssh/id_rsa` would hand the model a private key.
        Entries in the user's own global config are not restricted, and
        `allow_outside=True` turns the check off wholesale.
        """
        resolved: List[Path] = []
        seen = set()
        overflow = 0
        rejected: List[str] = []

        for declaring, entry in self.instruction_sources:
            restricted = not allow_outside and not self._user_authored(declaring)
            roots = self._instruction_roots(declaring) if restricted else []
            for path in self._expand(declaring, entry):
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                if restricted and not any(_under(path, r) for r in roots):
                    rejected.append(key)
                    continue
                if len(resolved) >= MAX_INSTRUCTION_FILES:
                    overflow += 1
                    continue
                resolved.append(path)

        # Idempotent: describe() may call this repeatedly, and a warning list
        # that grows on every render is worse than no warning at all.
        self.warnings = [w for w in self.warnings
                         if not w.startswith("instructions:")]
        if overflow:
            self.warnings.append(
                f"instructions: {overflow} file(s) past the "
                f"{MAX_INSTRUCTION_FILES}-file limit were ignored")
        for key in rejected:
            self.warnings.append(
                f"instructions: {key} is outside the project and was ignored")
        return resolved

    def _expand(self, declaring: Path, entry: str) -> List[Path]:
        if entry.startswith(("http://", "https://")):
            # opencode fetches remote instructions separately; haikode does not
            # pull config-declared URLs into the prompt at all.
            return []
        raw = Path(entry).expanduser()
        if raw.is_absolute():
            # Only the basename is globbed, exactly like opencode. Treating the
            # whole path as a pattern would make "/**/*.md" walk the disk.
            base, pattern = raw.parent, raw.name
        else:
            base, pattern = declaring.parent, str(raw)

        if not _is_glob(pattern):
            candidate = (base / pattern)
            try:
                return [candidate.resolve()] if candidate.is_file() else []
            except OSError:
                return []
        try:
            # islice before sorted: base.glob() is lazy, so this bounds the
            # walk itself and not just the result list.
            matches = sorted(islice(base.glob(pattern), MAX_GLOB_SCAN))
        except (OSError, ValueError, NotImplementedError, RecursionError) as e:
            self.errors.append(f"{declaring}: instructions '{entry}': {e}")
            return []
        out = []
        for match in matches:
            try:
                if match.is_file():
                    out.append(match.resolve())
            except OSError:
                continue
        return out

    # --- reporting -------------------------------------------------------

    def describe(self) -> List[str]:
        """Human-readable summary for /status or a doctor view."""
        lines = [f"Project config for {self.cwd}"]
        lines.append("Trust: " + ("trusted by the user" if self.trusted else
                                  "untrusted (privileged settings are ignored)"))
        if self.sources:
            lines.append("Loaded (weakest first, last wins):")
            lines.extend(f"  {path}" for path in self.sources)
        else:
            lines.append("Loaded: none (using defaults)")

        provider, model = self.resolve_model()
        if provider or model:
            lines.append(f"Model: {provider or '-'}/{model or '-'}")
        for key in ("default_agent", "shell", "theme", "username",
                    "max_steps", "context"):
            if self.data.get(key) is not None:
                lines.append(f"{key}: {self.data[key]}")

        instructions = self.resolve_instructions()
        if instructions:
            lines.append(f"Instruction files: {len(instructions)}")
            lines.extend(f"  {path}" for path in instructions)

        disabled = [name for name, enabled in (self.data.get("tools") or {}).items()
                    if not enabled]
        if disabled:
            lines.append("Disabled tools: " + ", ".join(sorted(disabled)))
        for key in ("agents", "commands", "mcp"):
            entries = self.data.get(key) or {}
            if entries:
                lines.append(f"{key}: " + ", ".join(sorted(entries)))
        if self.data.get("permission"):
            lines.append("Permission rules: " +
                         ", ".join(sorted(self.data["permission"])))

        for refusal in self.refusals:
            lines.append(f"Refused: {refusal.message}")
        for escalation in self.escalations():
            verb = "applied" if self.trusted or escalation.agent else "refused"
            lines.append(f"Warning ({verb}): {escalation.message}")
        for entry in self.unknown:
            lines.append(f"Warning: unknown key {entry}")
        for entry in self.warnings:
            lines.append(f"Warning: {entry}")
        for entry in self.errors:
            lines.append(f"Error: {entry}")
        return lines


def init_project_config(cwd: str = ".", overwrite: bool = False,
                        **settings: Any) -> Path:
    """Scaffold a haikode.json in `cwd` for a /init flow.

    Refuses to clobber an existing file unless `overwrite=True`; a config is
    hand-edited state and losing it to a stray /init is unforgivable. JSON has
    no comments, so the documentation goes in a leading `_comment` key, which
    the loader ignores along with every other underscore-prefixed key.
    """
    directory = Path(cwd).expanduser().resolve()
    path = directory / CONFIG_NAME
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} already exists; pass overwrite=True to replace it")

    bad = [key for key in settings if key not in KNOWN_KEYS]
    if bad:
        raise ValueError("unsupported setting(s): " + ", ".join(sorted(bad)))

    # Run the values through the loader's own validation, so /init cannot write
    # a file that the very next load would reject. trusted=True because this is
    # a shape check: /init writes what the user asked for, and whether the
    # result will be honoured is the trust store's business at load time.
    probe = ProjectConfig(cwd, trusted=True)
    probe._normalize(dict(settings), path)
    if probe.errors:
        raise ValueError("; ".join(probe.errors))

    payload: Dict[str, Any] = {"_comment": list(INIT_COMMENT)}
    if not settings:
        # An empty skeleton documents the shape without changing behaviour.
        payload.update({"instructions": [], "permission": {}, "tools": {}})
    payload.update(settings)

    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
