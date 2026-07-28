"""
Skills: named instruction sets the model loads on demand.

Port of opencode's `packages/opencode/src/skill/index.ts` and the `skill` tool
that goes with it. A skill is a directory holding a `SKILL.md` with `---`
frontmatter (name, description, when to use) and a markdown body. Only the name
and a one-line summary reach the system prompt; the body is read when the model
calls the `skill` tool. That split is the whole point — twenty skills cost
twenty lines of context instead of twenty documents.

Discovery, global first then project so a project skill of the same name wins:

    <global config dir>/skill(s)/**/SKILL.md
    <project>/.haikode/skill(s)/**/SKILL.md

Both spellings are accepted because opencode scans `{skill,skills}/**/SKILL.md`
and commands.py already does the same for command/commands.

opencode additionally scans `~/.claude/skills` and `.agents/skills`; haikode
does not. opencode gates those behind runtime flags that haikode has no
equivalent of, and quietly serving another tool's skills — with another tool's
instructions inside them — is a surprise rather than a feature.

Frontmatter is parsed with commands.parse_frontmatter: skill frontmatter is
flat `key: value` scalars, exactly what that scanner handles, and it already
tolerates junk lines instead of raising. Its one limitation (no folded or
multi-line values, no lists) costs nothing here — name, description and "when
to use" are all single-line strings.

Nothing in this module raises. A SKILL.md that is malformed, unreadable,
enormous or binary is skipped and the reason collected in `warnings`, because
one bad file in a shared skill directory must not take every other skill — or
the session — down with it.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .commands import parse_frontmatter
from .context import global_config_dir
from .permission import DENY

# opencode scans {skill,skills}/**/SKILL.md; accept both spellings.
SKILL_DIRS = ("skill", "skills")
SKILL_FILE = "SKILL.md"
PROJECT_DIR = ".haikode"

# A skill name is a tool argument and an XML attribute in the loaded block, so
# it is restricted to what cannot break either. Directory-style names ("a/b")
# are deliberately excluded: nothing resolves a name against the filesystem,
# and a name that looks like a path invites someone to try.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Frontmatter keys that answer "when should I load this?", in priority order.
# parse_frontmatter lowercases keys, so "When to use:" arrives as "when to use".
WHEN_KEYS = ("when to use", "when_to_use", "when-to-use", "when")

MAX_SKILLS = 200                 # scanned files, across every root
MAX_FILE_BYTES = 512 * 1024      # a SKILL.md larger than this is not prose
MAX_BODY_CHARS = 48000           # what one loaded skill may spend of the window
MAX_SUMMARY_CHARS = 200          # per line in the system prompt
MAX_PROMPT_CHARS = 4000          # the whole prompt block
MAX_SKILL_FILES = 10             # sampled resource files, as in opencode
MAX_WALK_ENTRIES = 500           # entries the resource walk may visit

BODY_TRUNCATED = "\n\n[... skill truncated at %d characters ...]"

_WHITESPACE = re.compile(r"\s+")


def _one_line(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Collapse to a single bounded line, for the system prompt listing."""
    collapsed = _WHITESPACE.sub(" ", (text or "")).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


class Skill:
    """One SKILL.md: its identity, its summary, and its body."""

    def __init__(self, name: str, body: str, path: Path,
                 description: str = "", when: str = "",
                 truncated: bool = False):
        self.name = name
        self.body = body
        self.path = Path(path)
        self.description = description
        self.when = when
        self.truncated = truncated

    @property
    def directory(self) -> Path:
        """Where the skill's own scripts and references live."""
        return self.path.parent

    def summary(self, limit: int = MAX_SUMMARY_CHARS) -> str:
        """The one line the system prompt gets."""
        return _one_line(self.description or self.when, limit)

    def instructions(self, files: Optional[Sequence[Path]] = None) -> str:
        """The block the `skill` tool returns to the model.

        Same shape as opencode's tool/skill.ts, including the base-directory
        note: skill bodies routinely say "run scripts/check.sh", and without
        being told where that is relative to, the model guesses the session cwd
        and the call fails.
        """
        listed = list(files) if files is not None else resource_files(self.directory)
        lines = ['<skill_content name="%s">' % self.name,
                 "# Skill: %s" % self.name,
                 ""]
        if self.when:
            lines += ["When to use: %s" % _one_line(self.when, MAX_SUMMARY_CHARS), ""]
        lines += [self.body.strip(),
                  "",
                  "Base directory for this skill: %s" % self.directory,
                  "Relative paths in this skill (e.g., scripts/, reference/) "
                  "are relative to this base directory."]
        if listed:
            lines += ["Note: file list is sampled.",
                      "",
                      "<skill_files>"]
            lines += ["<file>%s</file>" % path for path in listed]
            lines.append("</skill_files>")
        lines.append("</skill_content>")
        return "\n".join(lines)


def resource_files(directory: Path,
                   limit: int = MAX_SKILL_FILES) -> List[Path]:
    """Up to `limit` files shipped alongside SKILL.md, sampled.

    opencode runs ripgrep for this. Haiku frequently has no ripgrep, and a
    skill directory is small, so a bounded walk is both enough and one process
    fewer. os.walk does not follow directory symlinks, which is what keeps a
    linked-in tree from turning this into a filesystem crawl.
    """
    found: List[Path] = []
    visited = 0
    try:
        for root, dirs, names in os.walk(str(directory)):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(names):
                visited += 1
                if visited > MAX_WALK_ENTRIES:
                    return found
                if name == SKILL_FILE or name.startswith("."):
                    continue
                found.append(Path(root) / name)
                if len(found) >= limit:
                    return found
    except OSError:
        return found
    return found


# --- parsing -------------------------------------------------------------


def parse_skill(text: str, path: Path) -> Tuple[Optional[Skill], str]:
    """Build a Skill from a SKILL.md, or explain why it was skipped.

    Returns (skill, warning); exactly one of the two is meaningful. A missing
    or unusable `name` is fatal to the file and nothing else: the model
    addresses a skill by name, so a nameless one is unreachable however good
    its body is.
    """
    data, body = parse_frontmatter(text or "")
    name = (data.get("name") or "").strip()
    if not name:
        return None, "no 'name' in frontmatter"
    if not NAME_RE.match(name):
        return None, "unusable skill name %r" % name[:40]
    body = body.strip()
    if not body:
        return None, "empty body"

    truncated = False
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + BODY_TRUNCATED % MAX_BODY_CHARS
        truncated = True

    when = ""
    for key in WHEN_KEYS:
        if data.get(key):
            when = data[key].strip()
            break

    skill = Skill(name=name, body=body, path=Path(path),
                  description=(data.get("description") or "").strip(),
                  when=when, truncated=truncated)
    return skill, ""


def _read(path: Path) -> Tuple[Optional[str], str]:
    """Read one SKILL.md defensively. Returns (text, warning)."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return None, "file is %d bytes, skipped" % size
        raw = path.read_bytes()
    except OSError as err:
        return None, "unreadable: %s" % err
    if b"\0" in raw[:4096]:
        return None, "not a text file"
    # UTF-8 is pinned: the locale on Haiku may be POSIX, which would decode
    # every non-ASCII byte in a skill body to U+FFFD.
    return raw.decode("utf-8", errors="replace"), ""


# --- discovery -----------------------------------------------------------


def skill_roots(cwd: str = ".") -> List[Tuple[str, Path]]:
    """(scope, directory) pairs to scan, in precedence order (last wins)."""
    roots: List[Tuple[str, Path]] = []
    for scope, base in (("global", Path(global_config_dir())),
                        ("project", Path(cwd) / PROJECT_DIR)):
        for name in SKILL_DIRS:
            roots.append((scope, base / name))
    return roots


def _permitted(permissions: Any, name: str) -> bool:
    """False only when a rule explicitly denies loading this skill.

    Duck-typed on Permissions.decide(). A missing or broken permission layer
    answers True, which is not a hole: this decides what the *listing* shows,
    and every actual load goes through ctx.ask("skill", ...) in the tool, which
    fails closed on its own. Hiding a skill because the permission object threw
    would lose the feature to a bug it has nothing to do with.
    """
    if permissions is None:
        return True
    try:
        return permissions.decide("skill", name) != DENY
    except Exception:
        return True


class SkillRegistry:
    """Every skill found for one working directory, plus what went wrong."""

    def __init__(self, skills: Optional[Dict[str, Skill]] = None,
                 warnings: Optional[List[str]] = None,
                 directories: Optional[List[Path]] = None):
        self.skills: Dict[str, Skill] = skills or {}
        self.warnings: List[str] = warnings or []
        self.directories: List[Path] = directories or []

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get((name or "").strip())

    def names(self) -> List[str]:
        return sorted(self.skills)

    def all(self) -> List[Skill]:
        return [self.skills[name] for name in self.names()]

    def available(self, permissions: Any = None) -> List[Skill]:
        """The skills this session may actually load.

        Mirrors opencode's `Skill.available(agent)`: a skill an agent overlay
        denies is not worth a line of the system prompt, because loading it
        would only ever be refused. This is presentation, not enforcement —
        the refusal itself happens in the tool, under the same key.
        """
        return [skill for skill in self.all()
                if _permitted(permissions, skill.name)]

    def report(self, permissions: Any = None) -> str:
        """The text a `/skills` command prints: names, summaries, problems."""
        listed = self.available(permissions)
        lines: List[str] = []
        if not listed:
            lines.append("No skills found.")
            lines.append("Add one as .haikode/skill/<name>/SKILL.md with "
                         "'name' and 'description' frontmatter.")
        else:
            width = max(len(skill.name) for skill in listed)
            lines.append("Skills:")
            for skill in listed:
                lines.append("  %-*s  %s" % (width, skill.name,
                                             skill.summary(60)))
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend("  " + warning for warning in self.warnings)
        return "\n".join(lines)

    def prompt_block(self, limit: int = MAX_PROMPT_CHARS,
                     permissions: Any = None) -> str:
        """The system-prompt listing: names and one-line summaries only.

        Bounded on purpose. A user with fifty skills would otherwise pay for
        fifty descriptions on every single request, and the tail of that list
        is still reachable — the model can name a skill it already knows.
        """
        listed = [skill for skill in self.available(permissions)
                  if skill.description or skill.when]
        if not listed:
            return ""
        lines = ["# Available skills",
                 "Load one with the `skill` tool when the task matches its "
                 "description. The instructions arrive when you load it."]
        used = sum(len(line) + 1 for line in lines)
        shown = 0
        for skill in listed:
            line = "- **%s**: %s" % (skill.name, skill.summary())
            if shown and used + len(line) + 1 > limit:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        if shown < len(listed):
            lines.append("- ... and %d more; call the skill tool by name."
                         % (len(listed) - shown))
        return "\n".join(lines)


def discover(cwd: str = ".") -> SkillRegistry:
    """Scan the skill roots for `cwd`. Never raises."""
    found: Dict[str, Skill] = {}
    problems: List[str] = []
    directories: List[Path] = []
    origin: Dict[str, str] = {}
    scanned = 0

    def warn(message: str) -> None:
        # Deduplicated: two skills under one unreadable root would otherwise
        # report the same directory twice in /status.
        if message not in problems:
            problems.append(message)

    for scope, root in skill_roots(cwd):
        if not root.is_dir():
            continue
        directories.append(root)
        try:
            # rglob does not descend into symlinked directories on 3.10, so a
            # link back up the tree cannot make this walk forever.
            files = sorted(root.rglob(SKILL_FILE))
        except OSError as err:
            warn("skill %s: unreadable: %s" % (root, err))
            continue
        for path in files:
            if not path.is_file():
                continue
            scanned += 1
            if scanned > MAX_SKILLS:
                warn("skill: more than %d SKILL.md files, stopping the scan"
                     % MAX_SKILLS)
                break
            text, problem = _read(path)
            if text is None:
                warn("skill %s: %s" % (path, problem))
                continue
            skill, problem = parse_skill(text, path)
            if skill is None:
                warn("skill %s: %s" % (path, problem))
                continue
            if skill.name != path.parent.name:
                warn("skill %s: frontmatter name %r does not match its "
                     "directory" % (path, skill.name))
            previous = found.get(skill.name)
            if previous is not None and origin.get(skill.name) == scope:
                # Two skills of the same name in the same scope is an accident
                # the user has to resolve; project-over-global is not, and
                # warning about it would cry wolf on every deliberate override.
                warn("skill %s: duplicate name %r, %s wins"
                     % (previous.path, skill.name, path))
            found[skill.name] = skill
            origin[skill.name] = scope
        if scanned > MAX_SKILLS:
            break

    return SkillRegistry(found, problems, directories)


_CACHE: Dict[str, SkillRegistry] = {}


def clear_cache() -> None:
    """Drop cached registries so skills added on disk are picked up."""
    _CACHE.clear()


def load(cwd: str = ".", refresh: bool = False) -> SkillRegistry:
    """Cached discovery for `cwd`.

    The system prompt asks for this on every request, and discovery reads a
    handful of files; the cache is what keeps that off the hot path. Front
    ends and the `skill` tool pass refresh=True when a stale answer would be
    wrong.
    """
    key = str(Path(cwd).resolve()) if cwd else str(Path(".").resolve())
    if refresh or key not in _CACHE:
        _CACHE[key] = discover(cwd)
    return _CACHE[key]


def prompt_block(cwd: str = ".", limit: int = MAX_PROMPT_CHARS,
                 permissions: Any = None) -> str:
    """The `# Available skills` section for the system prompt, or ""."""
    return load(cwd).prompt_block(limit, permissions)


def warnings(cwd: str = ".") -> List[str]:
    """Skill-loading problems, for /status and the startup report."""
    return list(load(cwd).warnings)


# --- MCP status, for the front ends --------------------------------------
#
# This lives here rather than in mcp.py only because of who owns which file:
# it is pure presentation over MCPManager's public surface and moves there
# unchanged. Everything is duck-typed on .status()/.warnings/.tools(), so a
# front end can pass a stub — and skills.py never imports mcp.py, which keeps
# a /mcp command from starting subprocesses just by being rendered.

MCP_CONNECTED = "connected"
MCP_CONNECTING = "connecting"
MCP_FAILED = "failed"


def _mcp_state(status: str) -> Tuple[str, str]:
    """Split MCPManager's status string into (state, detail)."""
    text = str(status or "").strip()
    if text.startswith(MCP_FAILED):
        _, _, detail = text.partition(":")
        return MCP_FAILED, detail.strip()
    if text in (MCP_CONNECTED, MCP_CONNECTING):
        return text, ""
    return text or MCP_FAILED, ""


def mcp_rows(manager: Any) -> List[Dict[str, Any]]:
    """One row per configured MCP server, sorted by name.

    Each row is {name, state, detail, tools, tool_names} — enough for a plain
    text listing and enough for a dialog that wants to colour by state and
    expand a server's tools.
    """
    if manager is None:
        return []
    try:
        status = dict(manager.status() or {})
    except Exception:
        return []
    try:
        tools = list(manager.tools() or [])
    except Exception:
        tools = []

    by_server: Dict[str, List[str]] = {}
    for tool in tools:
        server = str(getattr(tool, "server", "") or "")
        name = str(getattr(tool, "remote_name", "") or getattr(tool, "name", ""))
        by_server.setdefault(server, []).append(name)

    rows: List[Dict[str, Any]] = []
    for name in sorted(status):
        state, detail = _mcp_state(status[name])
        names = sorted(by_server.get(name, []))
        rows.append({"name": name, "state": state, "detail": detail,
                     "tools": len(names), "tool_names": names})
    return rows


def mcp_warnings(manager: Any) -> List[str]:
    """The manager's collected warnings, defensively."""
    if manager is None:
        return []
    try:
        return [str(item) for item in (getattr(manager, "warnings", None) or [])]
    except Exception:
        return []


def mcp_report(manager: Any) -> str:
    """The text a `/mcp` command prints."""
    rows = mcp_rows(manager)
    lines: List[str] = []
    if not rows:
        lines.append("No MCP servers configured.")
        lines.append('Add one under "mcp" in .haikode/haikode.json or in the '
                     "global config.json.")
    else:
        lines.append("MCP servers:")
        width = max(len(row["name"]) for row in rows)
        for row in rows:
            label = "%-*s  %s" % (width, row["name"], row["state"])
            if row["detail"]:
                label += ": %s" % row["detail"]
            elif row["state"] == MCP_CONNECTED:
                label += "  (%d tool%s)" % (row["tools"],
                                            "" if row["tools"] == 1 else "s")
            lines.append("  " + label)
    problems = mcp_warnings(manager)
    if problems:
        lines.append("")
        lines.append("Warnings:")
        lines.extend("  " + problem for problem in problems)
    return "\n".join(lines)


def report(cwd: str = ".", permissions: Any = None) -> str:
    """The text a `/skills` command prints."""
    return load(cwd).report(permissions)


__all__ = ["Skill", "SkillRegistry", "clear_cache", "discover", "load",
           "mcp_report", "mcp_rows", "mcp_warnings", "parse_skill",
           "prompt_block", "report", "resource_files", "skill_roots",
           "warnings"]
