"""
Persistent agent memory.

opencode leans entirely on AGENTS.md -- a file the *human* maintains. Claude
Code adds a second layer that the *agent* maintains itself: short notes it
writes when it learns something durable, and reads back on every later run.
This module is that second layer.

Storage is one markdown file per memory plus a generated MEMORY.md index, in
two scopes:

    <global config>/memory/       facts about the user, valid in every project
    <project>/.haikode/memory/    facts about this codebase

Markdown-with-frontmatter rather than a database, deliberately: a memory a
person cannot open, edit and delete in a text editor is a memory they cannot
trust, and trust is the whole point of a store the model writes to unattended.
The frontmatter scanner is the flat "key: value" one from commands.py, for the
same reason it exists there -- Haiku has no pyyaml.

Three consumers:

  * ``context_block()`` renders memories for the system prompt (all
    descriptions, plus the full text of project memories, oldest dropped first
    when the budget runs out).
  * ``MEMORY_TOOLS`` gives the model ``memory_write`` / ``memory_read`` so it
    can save and recall on its own. They are defined here and registered by
    the caller, so the tool registry stays a plain list of built-ins.
  * ``parse_quick_capture()`` implements the "#" convention: a line the user
    starts with # is a memory, not a message.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .commands import parse_frontmatter
from .context import global_config_dir
from .palette import fuzzy_score
from .tool.base import Tool, ToolContext, ToolResult

# Layout
PROJECT_CONFIG_DIR = ".haikode"
MEMORY_DIRNAME = "memory"
INDEX_NAME = "MEMORY.md"

USER_SCOPE = "user"
PROJECT_SCOPE = "project"
SCOPES = (PROJECT_SCOPE, USER_SCOPE)

# A derived name is a filename a human has to live with; long enough to stay
# meaningful, short enough to read in a directory listing.
MAX_NAME_LEN = 48
MAX_DESCRIPTION_LEN = 120
MAX_READ_OUTPUT = 20000

_PREAMBLE = (
    "Notes you saved in earlier sessions. Treat them as established facts about "
    "this user and this project, and prefer them over guessing. If memory_write "
    "is available, correct a memory by writing it again under the same name."
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SENTENCE_RE = re.compile(r"[.!?;\n]")
# "#", "##" or "#user:" / "# project:" prefixes, then the memory text.
_CAPTURE_RE = re.compile(r"^\s*(#{1,2})\s*(?:(user|global|project|local)\s*:)?\s*(.*)$",
                         re.IGNORECASE | re.DOTALL)


# --- helpers -------------------------------------------------------------

def _timestamp() -> str:
    """UTC ISO-8601 to the second: sortable as a plain string, which is all
    the truncation and ordering code needs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fold_ascii(text: str) -> str:
    """Transliterate before slugifying so Norwegian names survive.

    Without this "hoyre" and "høyre" slugify to different things and "å" turns
    into a word break, which makes derived filenames unreadable.
    """
    for source, target in (("æ", "ae"), ("ø", "o"), ("å", "a"),
                           ("Æ", "AE"), ("Ø", "O"), ("Å", "A"),
                           ("ß", "ss")):
        text = text.replace(source, target)
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def slugify(text: str, max_len: int = MAX_NAME_LEN) -> str:
    """kebab-case identifier usable as both a memory name and a filename."""
    slug = _SLUG_RE.sub("-", _fold_ascii(text or "").lower()).strip("-")
    if len(slug) <= max_len:
        return slug
    cut = slug[:max_len]
    if "-" in cut[1:]:
        cut = cut[:cut.rindex("-")]
    return cut.strip("-")


def derive_name(text: str) -> str:
    """Name a memory after its opening sentence, the way a person would."""
    first = ""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    slug = slugify(_SENTENCE_RE.split(first)[0] if first else "")
    return slug or "memory"


def derive_description(text: str, max_len: int = MAX_DESCRIPTION_LEN) -> str:
    """One-line summary; frontmatter and index lines must not wrap."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_len:
        return collapsed
    cut = collapsed[:max_len]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(",;:-") + "..."


def normalize_scope(scope: str) -> str:
    """Anything that is not clearly user-scoped is project-scoped.

    Project is the safe default: a note filed against the wrong project is
    noise in one workspace, while a project note leaking into the global scope
    is noise in every future session.
    """
    value = str(scope or "").strip().lower()
    if value in ("user", "global", "home", "personal"):
        return USER_SCOPE
    return PROJECT_SCOPE


def _split_tags(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        cleaned = value.strip().strip("[]")
        parts = [part.strip().strip("\"'") for part in cleaned.split(",")]
    else:
        parts = [str(part).strip() for part in value]
    return tuple(part for part in parts if part)


# --- the memory record ---------------------------------------------------

@dataclass
class Memory:
    name: str
    body: str
    description: str = ""
    scope: str = PROJECT_SCOPE
    created: str = ""
    updated: str = ""
    tags: Tuple[str, ...] = ()
    path: Optional[Path] = None

    @property
    def filename(self) -> str:
        return f"{self.name}.md"

    def summary(self) -> str:
        """Description, falling back to the body — never an empty label."""
        return self.description or derive_description(self.body)

    def to_markdown(self) -> str:
        """Serialise to the on-disk form. Round-trips through parse_memory()."""
        lines = ["---",
                 f"name: {self.name}",
                 f"description: {self.summary()}",
                 f"scope: {self.scope}",
                 f"created: {self.created}",
                 f"updated: {self.updated}"]
        if self.tags:
            lines.append("tags: " + ", ".join(self.tags))
        lines += ["---", "", self.body.strip(), ""]
        return "\n".join(lines)

    def index_line(self) -> str:
        """Row for MEMORY.md: a markdown link so the index is navigable."""
        return f"- [{self.name}]({self.filename}) — {self.summary()}"

    def prompt_line(self) -> str:
        """Row for the system prompt: no links, scope spelled out."""
        return f"- {self.name} ({self.scope}) — {self.summary()}"

    def sort_key(self) -> Tuple[str, str]:
        """Oldest first. `updated` is ISO-8601, so string order is time order."""
        return (self.updated or self.created or "", self.name)


def parse_memory(text: str, path: Optional[Path] = None,
                 scope: str = PROJECT_SCOPE) -> Memory:
    """
    Build a Memory from a file's contents.

    Missing frontmatter is not an error: a memory the user created by hand with
    nothing but prose in it is still a memory, so name and description are
    derived from the filename and the body instead.
    """
    data, body = parse_frontmatter(text or "")
    body = body.strip()
    fallback = slugify(path.stem) if path is not None else ""
    name = slugify(data.get("name", "")) or fallback or derive_name(body)
    return Memory(
        name=name,
        body=body,
        description=data.get("description", "").strip() or derive_description(body),
        scope=normalize_scope(data.get("scope", scope)),
        created=data.get("created", "").strip(),
        updated=data.get("updated", "").strip() or data.get("created", "").strip(),
        tags=_split_tags(data.get("tags")),
        path=path,
    )


# --- the store -----------------------------------------------------------

class MemoryStore:
    """
    Reads and writes both memory scopes.

    Every query re-reads the directories rather than caching. The files are a
    few kilobytes at most, and the user is invited to edit them in another
    window — a cache here would mean the agent quietly working from a version
    of a memory the user already deleted.
    """

    def __init__(self, cwd: str = ".", global_dir: Optional[str] = None,
                 project_dir: Optional[str] = None):
        self.cwd = Path(cwd).resolve()
        self.project_dir = (Path(project_dir) if project_dir is not None
                            else self.cwd / PROJECT_CONFIG_DIR / MEMORY_DIRNAME)
        self.global_dir = (Path(global_dir) if global_dir is not None
                           else global_config_dir() / MEMORY_DIRNAME)
        self.warnings: List[str] = []

    # -- locations --

    def dir_for(self, scope: str) -> Path:
        return self.global_dir if normalize_scope(scope) == USER_SCOPE else self.project_dir

    def path_for(self, name: str, scope: str = PROJECT_SCOPE) -> Path:
        return self.dir_for(scope) / f"{slugify(name)}.md"

    def index_path(self, scope: str) -> Path:
        return self.dir_for(scope) / INDEX_NAME

    # -- reading --

    def _scan(self, directory: Path, scope: str) -> Tuple[List[Memory], List[str]]:
        """Load one directory. Bad files are reported, never raised."""
        memories: List[Memory] = []
        warnings: List[str] = []
        seen: Dict[str, Path] = {}
        try:
            entries = sorted(p for p in directory.iterdir()
                             if p.is_file() and p.suffix == ".md")
        except (OSError, ValueError):
            return [], []          # a missing scope directory is normal
        for path in entries:
            if path.name.lower() == INDEX_NAME.lower():
                continue           # generated, not a memory
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as err:
                warnings.append(f"unreadable memory {path.name}: {err}")
                continue
            if not raw.strip():
                warnings.append(f"empty memory file {path.name}")
                continue
            memory = parse_memory(raw, path=path, scope=scope)
            memory.scope = scope   # the directory decides, not the frontmatter
            if not memory.body:
                warnings.append(f"memory {path.name} has no body")
                continue
            if memory.name in seen:
                warnings.append(
                    f"duplicate memory name {memory.name!r} in {path.name}, "
                    f"keeping {seen[memory.name].name}")
                continue
            seen[memory.name] = path
            memories.append(memory)
        memories.sort(key=lambda m: m.name)
        return memories, warnings

    def all(self) -> List[Memory]:
        """Project memories first: the closer scope is the more specific one."""
        project, project_warnings = self._scan(self.project_dir, PROJECT_SCOPE)
        user, user_warnings = self._scan(self.global_dir, USER_SCOPE)
        self.warnings = project_warnings + user_warnings
        return project + user

    def scoped(self, scope: str) -> List[Memory]:
        scope = normalize_scope(scope)
        memories, warnings = self._scan(self.dir_for(scope), scope)
        self.warnings = warnings
        return memories

    def get(self, name: str, scope: Optional[str] = None) -> Optional[Memory]:
        wanted = slugify(name)
        if not wanted:
            return None
        pool = self.all() if scope is None else self.scoped(scope)
        for memory in pool:
            if memory.name == wanted:
                return memory
        return None

    def search(self, query: str, limit: int = 10) -> List[Memory]:
        """
        Rank memories with the palette's fuzzy scorer.

        Reusing fuzzy_score matters beyond code sharing: what the model gets
        back from memory_read and what the user sees when they search the same
        memories in a dialog are then the same ordering.
        """
        memories = self.all()
        if not (query or "").strip():
            return memories[:limit] if limit else memories
        scored: List[Tuple[int, int, Memory]] = []
        for index, memory in enumerate(memories):
            haystack = f"{memory.name} {memory.summary()} {memory.body}"
            result = fuzzy_score(query, haystack)
            if result is None:
                continue
            # index breaks ties in favour of project scope / alphabetical order
            scored.append((result[0], -index, memory))
        scored.sort(key=lambda row: (-row[0], -row[1]))
        ranked = [row[2] for row in scored]
        return ranked[:limit] if limit else ranked

    # -- writing --

    def _unique_name(self, base: str, taken: Dict[str, Memory]) -> str:
        suffix = 2
        while f"{base}-{suffix}" in taken:
            suffix += 1
        return f"{base}-{suffix}"

    def write(self, text: str, name: str = "", description: str = "",
              scope: str = PROJECT_SCOPE, tags: Sequence[str] = ()) -> Memory:
        """
        Save a memory, updating an existing one with the same name.

        Collision rule: an *explicit* name means "this memory", so writing it
        again edits it in place and bumps `updated`. A *derived* name is a
        guess, so if it already belongs to a memory with different text we take
        the next free name instead — the model must never lose an old note just
        because two of them start with the same sentence.
        """
        body = (text or "").strip()
        if not body:
            raise ValueError("refusing to store an empty memory")
        scope = normalize_scope(scope)
        explicit = bool(str(name or "").strip())
        slug = slugify(name) if explicit else derive_name(body)
        if not slug:
            slug = derive_name(body)

        existing_list, warnings = self._scan(self.dir_for(scope), scope)
        self.warnings = warnings
        existing = {memory.name: memory for memory in existing_list}
        if not explicit and slug in existing and existing[slug].body != body:
            slug = self._unique_name(slug, existing)

        previous = existing.get(slug)
        now = _timestamp()
        memory = Memory(
            name=slug,
            body=body,
            # An explicit description wins; otherwise re-derive from the new
            # text, because a summary that no longer matches its body is worse
            # than a lost hand-edit (and the file stays editable either way).
            description=(description or "").strip() or derive_description(body),
            scope=scope,
            created=(previous.created if previous and previous.created else now),
            updated=now,
            # Tags are classification, not a summary of the body, so they
            # survive an update that does not mention them.
            tags=_split_tags(tags) or (previous.tags if previous else ()),
            path=self.dir_for(scope) / f"{slug}.md",
        )
        directory = self.dir_for(scope)
        directory.mkdir(parents=True, exist_ok=True)
        memory.path.write_text(memory.to_markdown(), encoding="utf-8")
        self.rebuild_index()
        return memory

    def delete(self, name: str, scope: Optional[str] = None) -> bool:
        memory = self.get(name, scope=scope)
        if memory is None or memory.path is None:
            return False
        try:
            memory.path.unlink()
        except OSError as err:
            self.warnings.append(f"could not delete {memory.name}: {err}")
            return False
        self.rebuild_index()
        return True

    # -- index --

    def index_lines(self, scope: Optional[str] = None) -> List[str]:
        """MEMORY.md rows, for one scope or for everything."""
        memories = self.all() if scope is None else self.scoped(scope)
        return [memory.index_line() for memory in memories]

    def rebuild_index(self) -> List[Path]:
        """
        Regenerate MEMORY.md in every scope directory that exists.

        The index is derived data — it exists so a human (or `cat`) can see
        what the agent remembers without opening every file.
        """
        written: List[Path] = []
        for scope in SCOPES:
            directory = self.dir_for(scope)
            if not directory.is_dir():
                continue
            lines = [f"# Memory index ({scope})", "",
                     "Generated by haikode. Edit the memory files themselves; "
                     "this list is rebuilt on every write.", ""]
            rows = self.index_lines(scope)
            lines.extend(rows or ["_No memories saved._"])
            path = directory / INDEX_NAME
            try:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError as err:
                self.warnings.append(f"could not write {path}: {err}")
                continue
            written.append(path)
        return written

    # -- prompt injection --

    def context_block(self, limit_chars: int = 4000) -> str:
        """
        Render memories for the system prompt.

        Every memory contributes its one-line description (cheap, and the model
        needs to know a memory exists before it can ask for it); project
        memories additionally contribute their full text, because those are the
        ones about the code in front of it. When the budget is tight the oldest
        entries drop out first and leave an explicit marker, so the model can
        tell "nothing more is stored" apart from "more is stored, ask for it".
        """
        memories = self.all()
        if not memories:
            return "# Memory\n\nNo saved memories yet."
        ordered = sorted(memories, key=lambda m: m.sort_key())
        head = ["# Memory", "", _PREAMBLE, "", "## Saved memories"]

        kept = list(ordered)
        block = list(head)
        while kept:
            block = head + [memory.prompt_line() for memory in kept]
            dropped = len(ordered) - len(kept)
            if dropped:
                block.append(_omitted_marker(dropped, "memories"))
            if len("\n".join(block)) <= limit_chars:
                break
            kept.pop(0)
        if not kept:
            # The header alone is over budget; still say that memories exist.
            return "\n".join(head + [_omitted_marker(len(ordered), "memories")])

        full = [memory for memory in kept if memory.scope == PROJECT_SCOPE]
        if not full:
            return "\n".join(block)
        bodies = list(full)
        while bodies:
            candidate = list(block) + ["", "## Project memory in full"]
            dropped = len(full) - len(bodies)
            if dropped:
                candidate.append(_omitted_marker(dropped, "project memories"))
            for memory in bodies:
                candidate += ["", f"### {memory.name}", memory.body]
            if len("\n".join(candidate)) <= limit_chars:
                return "\n".join(candidate)
            bodies.pop(0)
        return "\n".join(block + ["", "## Project memory in full",
                                  _omitted_marker(len(full), "project memories")])


_PLURALS = {"memories": "memory", "project memories": "project memory"}


def _omitted_marker(count: int, noun: str) -> str:
    label = noun if count != 1 else _PLURALS.get(noun, noun)
    return (f"[... {count} older {label} omitted to fit the context budget; "
            f"use memory_read to load them]")


# --- quick capture -------------------------------------------------------

def parse_quick_capture(line: str) -> Optional[Tuple[str, str]]:
    """
    Interpret Claude Code's "#" convention: a line starting with # is a memory.

    Returns (text, scope) or None when the line is an ordinary message.
    "#" saves to the project, "##" to the user scope, and an explicit
    "#user: ..." / "#project: ..." prefix overrides both. A bare "#" (or a
    setext-style "###" heading with no text) is not a memory — the user is
    almost certainly writing markdown.
    """
    if not line or "#" not in line:
        return None
    if line.lstrip().startswith("###"):
        return None
    match = _CAPTURE_RE.match(line)
    if match is None:
        return None
    hashes, explicit, text = match.groups()
    text = (text or "").strip()
    if not text:
        return None
    if explicit:
        scope = normalize_scope(explicit)
    else:
        scope = USER_SCOPE if len(hashes) == 2 else PROJECT_SCOPE
    return text, scope


# --- tools ---------------------------------------------------------------

class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "Save a durable note to memory so it survives into later sessions.\n\n"
        "Use it for facts that will still be true next week: how this project "
        "is built and tested, conventions the user insists on, decisions and "
        "their reasons, gotchas you had to discover the hard way, and the "
        "user's stated preferences.\n\n"
        "Do NOT use it for the current task's state, for anything you can read "
        "back out of a file, or for secrets.\n\n"
        "Write the note as a short standalone paragraph or a few bullets — it "
        "will be read by a future session with no other context. To correct or "
        "extend an existing memory, write it again with the same `name`; that "
        "replaces it rather than adding a duplicate.\n\n"
        "scope defaults to 'project' (this codebase). Use 'user' only for "
        "facts about the person that hold in every project."
    )
    permission = "memory_write"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "The memory itself, written as standalone prose"},
            "name": {"type": "string",
                     "description": "Optional kebab-case id; reuse one to update that memory"},
            "scope": {"type": "string", "enum": ["project", "user"],
                      "description": "project (default) = this codebase, user = the person"},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "Optional labels for grouping memories"},
        },
        "required": ["text"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("memory_write requires text")
        scope = normalize_scope(args.get("scope") or PROJECT_SCOPE)
        name = str(args.get("name") or "")
        tags = _split_tags(args.get("tags"))
        label = slugify(name) or derive_name(text)

        ctx.ask("memory_write", [f"{scope}/{label}"],
                f"Remember ({scope}): {derive_description(text, 80)}",
                {"scope": scope, "name": label, "text": text},
                always=[f"{scope}/*"])

        store = MemoryStore(ctx.cwd)
        memory = store.write(text, name=name, scope=scope, tags=tags)
        lines = [f"Saved memory '{memory.name}' ({memory.scope} scope) to "
                 f"{memory.path}.", "", memory.summary()]
        if store.warnings:
            lines += [""] + [f"warning: {w}" for w in store.warnings]
        return ToolResult(
            title=f"{memory.name} ({memory.scope})",
            output="\n".join(lines),
            metadata={"name": memory.name, "scope": memory.scope,
                      "path": str(memory.path), "tags": list(memory.tags),
                      "updated": memory.updated})


class MemoryReadTool(Tool):
    name = "memory_read"
    # Reading back what you wrote yourself is as harmless as reading a file, so
    # it shares the "read" permission key instead of prompting on its own.
    permission = "read"
    description = (
        "Recall notes saved earlier with memory_write.\n\n"
        "With `query` the memories are fuzzy-matched and ranked; without it "
        "every memory is returned. The system prompt already contains a short "
        "index of what exists — use this tool to read the full text of a "
        "memory whose description looks relevant, or to check whether "
        "something was recorded before asking the user."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Optional search text; omit to list every memory"},
        },
        "required": [],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        ctx.ask("read", ["memory"], "Read saved memories", {"query": query})

        store = MemoryStore(ctx.cwd)
        memories = store.search(query) if query else store.all()
        if not memories:
            note = (f"No memories match {query!r}." if query
                    else "No memories saved yet.")
            return ToolResult(title="no memories", output=note,
                              metadata={"count": 0, "query": query})

        sections: List[str] = []
        for memory in memories:
            header = [f"## {memory.name} ({memory.scope})"]
            if memory.tags:
                header.append(f"tags: {', '.join(memory.tags)}")
            if memory.updated:
                header.append(f"updated: {memory.updated}")
            sections.append("\n".join(header + ["", memory.body]))
        output = "\n\n".join(sections)
        if len(output) > MAX_READ_OUTPUT:
            output = (output[:MAX_READ_OUTPUT]
                      + "\n\n[... truncated; narrow the query to see the rest]")
        if store.warnings:
            output += "\n\n" + "\n".join(f"warning: {w}" for w in store.warnings)
        return ToolResult(
            title=f"{len(memories)} memor{'y' if len(memories) == 1 else 'ies'}",
            output=output,
            metadata={"count": len(memories), "query": query,
                      "names": [m.name for m in memories]})


# Registered by the orchestrator; see haikode/tool/__init__.py for the
# built-in list this is appended to.
MEMORY_TOOLS: List[Tool] = [MemoryWriteTool(), MemoryReadTool()]


__all__ = ["Memory", "MemoryStore", "MemoryWriteTool", "MemoryReadTool",
           "MEMORY_TOOLS", "parse_memory", "parse_quick_capture",
           "derive_description", "derive_name", "normalize_scope", "slugify",
           "INDEX_NAME", "PROJECT_SCOPE", "USER_SCOPE", "SCOPES"]
