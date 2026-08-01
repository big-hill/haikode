"""
Context engine: environment block, project instructions (AGENTS.md) and
history compaction.

opencode injects a small environment header plus the instruction files it
finds, then compacts old turns when the window fills. This does the same, but
keeps tool call/result pairs intact — dropping half a pair makes providers
reject the request.

Compaction follows packages/core/src/session/compaction.ts: the folded turns
are handed to the model as a transcript and come back as one anchored summary
(objective, constraints, work state, next move, files), which then replaces
them. Dropping the turns instead — which is what this module used to do — loses
the architectural decisions and the constraints the user stated, silently and
mid-task. The model call can fail, so it is only ever an improvement on the old
behaviour: a failed or absent summariser falls back to the drop-with-a-notice
path rather than losing the conversation.

Instruction discovery follows packages/opencode/src/session/instruction.ts:
the first existing global file, then the first *name* in INSTRUCTION_FILES that
matches anywhere between the working directory and the worktree root (that name
is then taken from every directory in that range, nearest first, because
fs-util's findUp returns the whole chain), then whatever the config declared.
The upward walk stops at the worktree — the git root, or the working directory
itself when there is no repository — so a stray AGENTS.md in /tmp or in a
parent of an unrelated checkout can never steer the agent.
"""

import glob as globmod
import hashlib
import json
import os
import platform
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from .schema import Msg

# Checked in this order; the first name with a match anywhere up the tree wins,
# exactly like opencode's ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"].
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "HAIKODE.md")
MAX_INSTRUCTION_CHARS = 12000
# Per file, so one runaway match cannot be read into memory whole before the
# concatenation is capped. A glob can hit a multi-megabyte generated file.
MAX_INSTRUCTION_FILE_CHARS = MAX_INSTRUCTION_CHARS
# A config entry like "**/*.md" in a monorepo can expand to thousands of paths;
# stop walking once we have more than the prompt could ever hold.
MAX_GLOB_MATCHES = 64
TRUNCATION_MARKER = "[... instructions truncated at {limit} characters ...]"
TREE_LIMIT = 200
GIT_TIMEOUT = 3            # seconds; git may be missing or hung on Haiku
GLOB_MAGIC = "*?["

IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                "build", "dist", "objects", ".cache"}

PathLike = Union[str, Path]


# Characters per token. The old value, 4.0, was measured against the API on
# a real code- and tool-output-heavy session and undercounted every time:
#
#     messages   estimated   reported   estimated/reported
#     60         40 739      46 033     88%
#     150        81 995     103 200     79%
#     300       119 179     146 773     81%
#
# 3.3 puts the same measurements at 96–107%. Still an estimate — the live
# correction is Agent.token_scale, which reweighs it against what the
# provider actually reports; this constant only has to be right enough for
# the requests made before the first response.
CHARS_PER_TOKEN = 3.3


def estimate_tokens(text: str) -> int:
    """Rough but stable, calibrated against reported counts (see above)."""
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def message_tokens(message: Msg) -> int:
    total = estimate_tokens(message.content or "")
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(str(call.arguments))
    return total + 4


def global_config_dir() -> Path:
    """Where settings, sessions and user memories live.

    HAIKODE_CONFIG_DIR overrides it, which is how a second profile is kept
    apart from the real one — and how the test suite guarantees it cannot
    write sessions into the user's own store. Individual tests redirecting
    this function still work; the variable is the backstop for the ones that
    forget.
    """
    override = os.environ.get("HAIKODE_CONFIG_DIR")
    if override:
        return Path(os.path.expanduser(override))
    if Path("/boot/home").exists():
        return Path(os.path.expanduser("~/config/settings/haikode"))
    return Path(os.path.expanduser("~/.config/haikode"))


def home_dir() -> Path:
    """Separate from global_config_dir() so tests can redirect just one."""
    return Path(os.path.expanduser("~"))


def global_instruction_files() -> List[Path]:
    """Global candidates, most specific first.

    opencode adds only the first one that exists (instruction.ts:115-120): a
    user with a haikode AGENTS.md has already opted out of the Claude Code
    file, and loading both would double the global preamble.
    """
    return [global_config_dir() / "AGENTS.md",
            home_dir() / ".claude" / "CLAUDE.md"]


def is_instruction_url(entry: str) -> bool:
    return entry.startswith("http://") or entry.startswith("https://")


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _read(path: Path, limit: int = MAX_INSTRUCTION_FILE_CHARS) -> str:
    """Read at most `limit` characters of an instruction file.

    read_text() would pull the whole file into memory just to have the prompt
    throw most of it away, and it decodes with the locale encoding — markdown
    written on another machine must not turn into mojibake on Haiku.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit).strip()
    except OSError:
        return ""


def _is_inside(child: PathLike, parent: PathLike) -> bool:
    """Lexical containment test.

    Deliberately abspath and not resolve(): `..` must be normalised away, but a
    symlinked docs directory inside the project is legitimate and resolve()
    would report it as an escape.
    """
    child_path = os.path.abspath(str(child))
    parent_path = os.path.abspath(str(parent))
    try:
        return os.path.commonpath([child_path, parent_path]) == parent_path
    except ValueError:      # different drives / unrelated roots
        return False


class ContextManager:
    def __init__(self, root: str = "."):
        self.root = str(Path(root).resolve())

    # --- environment ---------------------------------------------------

    def _git_branch(self) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.root, capture_output=True, text=True,
                timeout=GIT_TIMEOUT)
        except (OSError, ValueError, subprocess.SubprocessError):
            return ""  # git absent (common on Haiku), not a repo, or hung
        return result.stdout.strip() if result.returncode == 0 else ""

    def project_root(self) -> Path:
        """Nearest ancestor holding a .git, else the working directory itself.

        Mirrors opencode's ctx.worktree, which is what bounds the upward
        search for instruction files.
        """
        current = Path(self.root)
        for directory in [current, *current.parents]:
            if (directory / ".git").exists():
                return directory
        return current

    def project_tree(self, limit: int = TREE_LIMIT) -> str:
        """A shallow file listing so the model knows the layout up front."""
        entries: List[str] = []
        root = Path(self.root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in IGNORED_DIRS and not d.startswith("."))
            rel_dir = Path(dirpath).relative_to(root)
            if len(rel_dir.parts) > 3:
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                entries.append(str(rel_dir / name) if str(rel_dir) != "." else name)
                if len(entries) >= limit:
                    return "\n".join(entries) + f"\n[... truncated at {limit} files]"
        return "\n".join(entries)

    def environment_block(self) -> str:
        worktree = self.project_root()
        lines = [
            "# Environment",
            f"Working directory: {self.root}",
            f"Workspace root folder: {worktree}",
            f"Platform: {platform.system()} {platform.release()}",
            # A subdirectory of a repo is still in a repo, so ask the worktree.
            f"Is a git repository: {'yes' if (worktree / '.git').exists() else 'no'}",
            # Without this the model dates everything from its training cutoff.
            f"Today's date: {time.strftime('%Y-%m-%d')}",
        ]
        branch = self._git_branch()
        if branch:
            lines.append(f"Current branch: {branch}")
        tree = self.project_tree()
        if tree:
            lines.append("\nFiles in the working directory:\n" + tree)
        return "\n".join(lines)

    # --- project instructions (AGENTS.md) --------------------------------

    def _search_chain(self) -> List[Path]:
        """Working directory upwards, stopping at the worktree root.

        This is opencode's findUp(start=ctx.directory, stop=ctx.worktree). The
        stop matters: without a repository the worktree *is* the working
        directory, so an unrelated AGENTS.md in /tmp or in $HOME is out of
        reach instead of being walked into the system prompt.
        """
        stop = self.project_root()
        current = Path(self.root)
        chain: List[Path] = []
        for directory in [current, *current.parents]:
            chain.append(directory)
            if directory == stop:
                break
        return chain

    def _project_files(self) -> List[Path]:
        """Project instruction files, nearest first.

        The first *name* that matches anywhere in the chain wins, and every
        directory in the chain that has that name contributes (instruction.ts
        does `matches.forEach(paths.add)` over the full findUp result). So a
        monorepo keeps its root AGENTS.md alongside the package one, but a
        CLAUDE.md never stacks on top of an AGENTS.md.
        """
        chain = self._search_chain()
        for name in INSTRUCTION_FILES:
            matches = [directory / name for directory in chain
                       if _is_file(directory / name)]
            if matches:
                return matches
        return []

    def _glob(self, pattern: str, recursive: bool) -> List[Path]:
        """Bounded glob: files only, at most MAX_GLOB_MATCHES, never raising."""
        found: List[Path] = []
        try:
            for match in globmod.iglob(pattern, recursive=recursive):
                path = Path(match)
                if _is_file(path):
                    found.append(path)
                    if len(found) >= MAX_GLOB_MATCHES:
                        break
        except (OSError, ValueError, RecursionError):
            return sorted(found)
        return sorted(found)

    def resolve_entries(self, entries: Optional[Sequence[PathLike]]
                        ) -> Tuple[List[Path], List[str]]:
        """Expand config-declared instruction entries to (files, skipped urls).

        Relative entries are searched from the working directory up to the
        worktree root (opencode's globUp) and may never leave it: a config that
        travels with a checked-out repository must not be able to name
        ../../../.ssh/id_rsa and have it read into the system prompt. An
        absolute or ~ entry is an explicit user choice and is honoured, but it
        is globbed non-recursively inside its own directory, exactly like
        instruction.ts, so "/**" cannot walk the whole disk.

        URLs are collected instead of fetched: prompt assembly runs on every
        provider round and must never wait on the network.
        """
        files: List[Path] = []
        urls: List[str] = []
        if not entries:
            return files, urls
        chain = self._search_chain()
        worktree = chain[-1]
        for raw in entries:
            entry = str(raw)
            if is_instruction_url(entry):
                urls.append(entry)
                continue
            expanded = os.path.expanduser(entry)
            magic = any(ch in expanded for ch in GLOB_MAGIC)
            if os.path.isabs(expanded):
                candidates = (self._glob(expanded, recursive=False) if magic
                              else [Path(expanded)])
            else:
                candidates = []
                for directory in chain:
                    joined = os.path.join(str(directory), expanded)
                    candidates.extend(self._glob(joined, recursive=True) if magic
                                      else [Path(joined)])
                candidates = [c for c in candidates if _is_inside(c, worktree)]
            for path in candidates:
                if _is_file(path):
                    files.append(path)
        return files, urls

    def _ordered(self, extra: Sequence[Path]) -> List[Path]:
        """Global, project and config files deduplicated by resolved path."""
        ordered: Dict[str, Path] = {}

        def add(path: Path) -> None:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            ordered.setdefault(str(resolved), resolved)

        for candidate in global_instruction_files():
            if _is_file(candidate):
                add(candidate)
                break

        for project in self._project_files():
            add(project)

        for path in extra:
            add(path)

        return list(ordered.values())

    def instruction_files(self, extra_paths: Optional[Sequence[PathLike]] = None
                          ) -> List[Path]:
        """Every instruction file that would be loaded, in prompt order.

        Exposed so /status can list them without paying for the file reads.
        """
        return self._ordered(self.resolve_entries(extra_paths)[0])

    def instructions(self, extra_paths: Optional[Sequence[PathLike]] = None) -> str:
        """The instruction files concatenated for the system prompt."""
        extra, urls = self.resolve_entries(extra_paths)  # one pass, globs are IO
        blocks: List[str] = []
        used = 0
        for path in self._ordered(extra):
            if used >= MAX_INSTRUCTION_CHARS:
                break   # the rest would be truncated away; do not read it at all
            text = _read(path)
            if text:
                blocks.append(f"--- {path} ---\n{text}")
                used += len(blocks[-1])

        for url in urls:
            blocks.append(f"--- {url} ---\n"
                          "[remote instructions are not fetched during prompt "
                          "assembly; use the fetch tool if you need them]")

        joined = "\n\n".join(blocks)
        if len(joined) > MAX_INSTRUCTION_CHARS:
            marker = "\n\n" + TRUNCATION_MARKER.format(limit=MAX_INSTRUCTION_CHARS)
            keep = MAX_INSTRUCTION_CHARS - len(marker)
            joined = joined[:max(keep, 0)].rstrip() + marker
        return joined


# --- compaction ---------------------------------------------------------

# opencode's DEFAULT_TAIL_TURNS / DEFAULT_KEEP_TOKENS / SUMMARY_OUTPUT_TOKENS.
DEFAULT_TAIL_TURNS = 2
MIN_KEEP_TOKENS = 2000
# Fraction of the context window the history may occupy before it is folded,
# and the room always left for the reply on top of it. opencode compacts at
# the window minus about one maximum output (session/overflow.ts); 0.92 with a
# 20k floor lands in the same place without needing the model's output limit.
DEFAULT_RESERVE = 0.92
MIN_REPLY_RESERVE = 20000

MAX_KEEP_TOKENS = 8000
SUMMARY_MAX_TOKENS = 4096
# Per tool result fed to the summariser. A single 200 kB grep result would
# otherwise crowd out the twenty turns that actually carry the decisions.
TOOL_OUTPUT_MAX_CHARS = 2000
# Ceiling on the whole transcript handed to the summariser (~30k tokens). The
# oldest text is cut first: the summariser's own request has to fit a window
# too, and the newest folded turns are the ones the tail still depends on.
MAX_SUMMARY_INPUT_CHARS = 120000

DROP_NOTICE = ("[{dropped} earlier messages were dropped to fit the context "
               "window. Re-read files with the read tool if you need their "
               "contents again.]")

SUMMARY_SYSTEM_PROMPT = """\
You are an anchored context summarization assistant for coding sessions.

Summarize only the conversation history you are given. The newest turns are \
kept verbatim outside your summary, so focus on the older context that still \
matters for continuing the work.

If the prompt includes a <previous-summary> block, treat it as the current \
anchored summary. Update it with the new history by preserving still-true \
details, removing stale details, and merging in new facts.

Always follow the exact output structure requested by the user prompt. Keep \
every section, preserve exact file paths and identifiers when known, and \
prefer terse bullets over paragraphs.

Do not answer the conversation itself. Do not mention that you are \
summarizing, compacting, or merging context. Respond in the same language as \
the conversation."""

SUMMARY_TEMPLATE = """\
Output exactly the Markdown structure shown inside <template> and keep the \
section order unchanged. Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, \
exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and \
identifiers when known.
- Do not mention the summary process or that context was compacted."""


def is_pinned(message: Msg) -> bool:
    """True for a message the user asked to keep verbatim forever.

    Front ends set display["pinned"]; compaction then never folds it away, so
    a stated constraint ("never touch the vendor directory") survives every
    round of summarising.
    """
    return bool((message.display or {}).get("pinned"))


def is_summary(message: Msg) -> bool:
    """True for a message a previous compaction wrote."""
    return bool((message.display or {}).get("summary"))


def summary_message(text: str, folded: int) -> Msg:
    """The message that replaces the folded turns.

    role "user" deliberately: some providers reject a history that opens with
    an assistant turn, and every provider accepts one that opens with a user
    turn.
    """
    return Msg(role="user", content=text,
               display={"summary": True, "folded": int(folded)})


def drop_notice_message(dropped: int, reason: str = "") -> Msg:
    """The pre-summary fallback: say what was lost instead of pretending."""
    text = DROP_NOTICE.format(dropped=int(dropped))
    if reason:
        text = "%s\n[no summary was written: %s]" % (text, reason)
    return Msg(role="user", content=text,
               display={"summary": True, "folded": int(dropped),
                        "dropped": True})


def _serialize_arguments(arguments: Any) -> str:
    try:
        return json.dumps(arguments, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return str(arguments)


def _truncate(text: str, limit: int = TOOL_OUTPUT_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def serialize_message(message: Msg) -> str:
    """One message as the flat transcript the summariser reads.

    Mirrors core/session/compaction.ts's `serialize`: labelled lines rather
    than a nested message array, because the summariser is asked for prose
    about the conversation, not to continue it.
    """
    content = (message.content or "").strip()
    if message.role == "user":
        return "[User]: %s" % content if content else ""
    if message.role == "assistant":
        lines: List[str] = []
        if content:
            lines.append("[Assistant]: %s" % content)
        for call in message.tool_calls:
            lines.append("[Assistant tool call]: %s(%s)"
                         % (call.name, _serialize_arguments(call.arguments)))
        return "\n".join(lines)
    if message.role == "tool":
        name = str((message.display or {}).get("tool") or "tool")
        return "[Tool result: %s]: %s" % (name, _truncate(content))
    if message.role == "system":
        return "[System]: %s" % content if content else ""
    return "[%s]: %s" % (message.role or "message", content) if content else ""


def serialize_for_summary(messages: Sequence[Msg],
                          max_chars: int = MAX_SUMMARY_INPUT_CHARS) -> str:
    """The folded turns as one transcript, oldest first.

    Summary messages written by an earlier compaction are left out: they are
    passed separately as <previous-summary> so the model updates them instead
    of summarising its own summary a second time.
    """
    blocks = [serialize_message(m) for m in messages if not is_summary(m)]
    joined = "\n\n".join(block for block in blocks if block)
    if len(joined) > max_chars:
        joined = ("[... the oldest part of this transcript was cut ...]\n\n"
                  + joined[-max_chars:])
    return joined


def build_summary_prompt(previous_summary: str = "",
                         context: Sequence[str] = ()) -> str:
    """opencode's buildPrompt: instruction, output template, then the history."""
    head = ("Update the anchored summary below using the conversation history "
            "above.\nPreserve still-true details, remove stale details, and "
            "merge in the new facts.\n<previous-summary>\n%s\n</previous-summary>"
            % previous_summary.strip()
            ) if previous_summary.strip() else (
        "Create a new anchored summary from the conversation history.")
    return "\n\n".join([head, SUMMARY_TEMPLATE]
                       + [block for block in context if block])


def summarize_with_reason(messages: Sequence[Msg], provider: Any, model: str, *,
                          previous_summary: str = "",
                          max_tokens: int = SUMMARY_MAX_TOKENS,
                          max_chars: int = MAX_SUMMARY_INPUT_CHARS
                          ) -> Tuple[str, str]:
    """(summary, reason it is empty). Never raises.

    The reason matters to the caller: a compaction that falls back to dropping
    turns has to be able to say why, otherwise the loss is as silent as the
    behaviour this replaced.
    """
    history = serialize_for_summary(messages, max_chars)
    if not history.strip():
        return "", "nothing to summarise"
    request = [Msg(role="system", content=SUMMARY_SYSTEM_PROMPT),
               Msg(role="user",
                   content=build_summary_prompt(previous_summary, [history]))]
    parts: List[str] = []
    try:
        for chunk in provider.stream(request, [], model, int(max_tokens)):
            if getattr(chunk, "stop_reason", None) == "error":
                # Providers report failure as a terminal chunk whose text is
                # the error message; collecting it would file the outage as
                # the conversation's memory.
                return "", (chunk.text or "provider error").strip()[:200]
            if chunk.text:
                parts.append(chunk.text)
    except Exception as exc:            # noqa: BLE001 - see below
        # Deliberately broad: a summariser that dies for any reason (network,
        # a provider raising something undocumented, a malformed stream) must
        # degrade to the drop-with-a-notice path, never take the run down and
        # never lose the history it was handed.
        return "", "%s: %s" % (type(exc).__name__, exc)
    text = "".join(parts).strip()
    if not text:
        return "", "the model returned an empty summary"
    return text, ""


def summarize(messages: Sequence[Msg], provider: Any, model: str, *,
              previous_summary: str = "",
              max_tokens: int = SUMMARY_MAX_TOKENS,
              max_chars: int = MAX_SUMMARY_INPUT_CHARS) -> str:
    """Ask `model` for an anchored summary of `messages`; "" when it fails.

    `previous_summary` is the summary an earlier compaction wrote: passing it
    makes this an update of that text rather than a summary of a summary,
    which is how a long session keeps its oldest decisions.
    """
    return summarize_with_reason(messages, provider, model,
                                 previous_summary=previous_summary,
                                 max_tokens=max_tokens, max_chars=max_chars)[0]


# Summaries already written, keyed by exactly which messages were folded. The
# request-assembly path re-runs compaction on every provider round, and without
# this every round of a long run would pay for another summarising call — and a
# provider that is down would be dialled once per step. Bounded, because a run
# that keeps growing its history keeps producing new folds.
SUMMARY_CACHE_SIZE = 8
_SUMMARY_CACHE: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
_SUMMARY_CACHE_LOCK = threading.Lock()


def _fold_fingerprint(folded: Sequence[Msg], model: str,
                      previous_summary: str) -> str:
    """Identity of a summarising request, cheap enough to compute per round."""
    parts = [model, previous_summary]
    for message in folded:
        parts.append(message.role)
        parts.append(message.content or "")
        parts.append(message.tool_call_id or "")
        parts.extend(call.id for call in message.tool_calls)
    digest = hashlib.sha1("\x00".join(parts).encode("utf-8", "replace"))
    return digest.hexdigest()


def _remember_summary(key: str, value: Tuple[str, str]) -> None:
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[key] = value
        _SUMMARY_CACHE.move_to_end(key)
        while len(_SUMMARY_CACHE) > SUMMARY_CACHE_SIZE:
            _SUMMARY_CACHE.popitem(last=False)


def _recall_summary(key: str) -> Optional[Tuple[str, str]]:
    with _SUMMARY_CACHE_LOCK:
        value = _SUMMARY_CACHE.get(key)
        if value is not None:
            _SUMMARY_CACHE.move_to_end(key)
        return value


def clear_summary_cache() -> None:
    """Forget every cached summary. For tests and for /new."""
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()


def last_summary(messages: Sequence[Msg]) -> str:
    """The newest summary already in `messages`, for anchored updates.

    A drop notice left by a failed summariser is skipped: it records a loss,
    and re-anchoring on it would teach the next summary that the loss is the
    thing worth remembering.
    """
    for message in reversed(list(messages)):
        if not is_summary(message) or (message.display or {}).get("dropped"):
            continue
        if (message.content or "").strip():
            return message.content.strip()
    return ""


@dataclass
class CompactionPlan:
    """Which message indices survive a fold and which are handed to the model."""
    keep: List[int] = field(default_factory=list)
    folded: List[int] = field(default_factory=list)
    boundary: int = 0


@dataclass
class CompactionResult:
    """What a compaction did, in the shape both /compact and the automatic
    path return so a front end can report either one the same way."""
    messages: List[Msg] = field(default_factory=list)
    folded: int = 0
    kept: int = 0
    summary: str = ""
    # True when a real summary (model-written, or one the caller supplied)
    # replaced the folded turns; False when they were merely dropped.
    summarized: bool = False
    error: str = ""
    trigger: str = "auto"       # "auto" | "manual"
    # True when the summary came from the cache rather than from a fresh
    # provider round, so a front end can announce a compaction once instead of
    # once per step of the run that follows it.
    cached: bool = False

    @property
    def changed(self) -> bool:
        return self.folded > 0

    def notice(self) -> str:
        """One line a front end can print. Empty when nothing happened."""
        if not self.folded:
            return ""
        if self.summarized:
            return "Compacted %d messages into a summary." % self.folded
        return ("Dropped %d messages without a summary%s."
                % (self.folded, (": " + self.error) if self.error else ""))


def _turn_starts(messages: Sequence[Msg]) -> List[int]:
    """Index of every user message that opens a turn."""
    return [index for index, message in enumerate(messages)
            if message.role == "user" and not is_summary(message)]


def _span_tokens(messages: Sequence[Msg], start: int, end: int) -> int:
    return sum(message_tokens(m) for m in messages[start:end])


def _split_tail(messages: Sequence[Msg], floor: int, keep_tokens: int) -> int:
    """Keep the trailing messages that fit `keep_tokens`, never fewer than one.

    opencode's splitTurn: one turn can be larger than the whole recent budget
    (a build log pasted in), and keeping it whole would leave nothing for the
    summary to be added to.
    """
    used = 0
    split = len(messages)
    for index in range(len(messages) - 1, max(0, floor) - 1, -1):
        cost = message_tokens(messages[index])
        if used + cost > keep_tokens and split < len(messages):
            break
        used += cost
        split = index
    return split


def _tail_start(messages: Sequence[Msg], tail_turns: int,
                keep_tokens: int) -> int:
    """First index of the tail that is kept verbatim.

    At most `tail_turns` turns and at most `keep_tokens` tokens, which is
    opencode's select(): the newest turn always survives whole if it fits, and
    older turns are added only while there is budget left.
    """
    starts = _turn_starts(messages)
    recent = starts[-max(1, int(tail_turns)):] if starts else [0]
    chosen = recent[-1]
    total = _span_tokens(messages, chosen, len(messages))
    if total > keep_tokens:
        return _split_tail(messages, chosen, keep_tokens)
    for start in reversed(recent[:-1]):
        size = _span_tokens(messages, start, chosen)
        if total + size > keep_tokens:
            break
        total += size
        chosen = start
    return chosen


def _close_tool_pairs(messages: Sequence[Msg], keep: Set[int]) -> None:
    """Grow `keep` until no kept message is half of a tool exchange.

    A `tool` message whose assistant call was folded away, or an assistant call
    whose result was folded away, makes the whole request invalid at every
    provider — Anthropic rejects the turn outright and the OpenAI dialect is
    only slightly more forgiving.
    """
    owner: Dict[str, int] = {}
    answers: Dict[int, List[int]] = {}
    for index, message in enumerate(messages):
        for call in message.tool_calls:
            owner[call.id] = index
            answers.setdefault(index, [])
    for index, message in enumerate(messages):
        if message.role == "tool" and message.tool_call_id in owner:
            answers.setdefault(owner[message.tool_call_id], []).append(index)

    changed = True
    while changed:
        changed = False
        for index in sorted(keep):
            message = messages[index]
            if message.role == "tool":
                home = owner.get(message.tool_call_id)
                if home is not None and home not in keep:
                    keep.add(home)
                    changed = True
            for answer in answers.get(index, ()):
                if answer not in keep:
                    keep.add(answer)
                    changed = True


def plan_compaction(messages: Sequence[Msg], *, keep_last: int = 0,
                    tail_turns: int = DEFAULT_TAIL_TURNS,
                    keep_tokens: int = MAX_KEEP_TOKENS) -> CompactionPlan:
    """Decide what folds and what stays. The one selection rule in the package.

    Always kept: every system message, every pinned message, the tail, and
    whatever closing the tool pairs drags along with them. `keep_last` pins the
    tail to a message count (what /compact's argument means); 0 selects the
    tail by turns and tokens the way opencode does.
    """
    total = len(messages)
    if total == 0:
        return CompactionPlan(keep=[], folded=[], boundary=0)
    if keep_last and int(keep_last) > 0:
        start = max(0, total - int(keep_last))
    else:
        start = _tail_start(messages, tail_turns, keep_tokens)
    keep: Set[int] = set(range(start, total))
    for index, message in enumerate(messages):
        if message.role == "system" or is_pinned(message):
            keep.add(index)
    _close_tool_pairs(messages, keep)
    folded = [index for index in range(total) if index not in keep]
    return CompactionPlan(keep=sorted(keep), folded=folded, boundary=start)


def apply_plan(messages: Sequence[Msg], plan: CompactionPlan,
               replacement: Msg) -> List[Msg]:
    """`messages` with the folded entries replaced by `replacement`.

    The replacement lands where the *last* folded message was, so a pinned
    message from early in the conversation still reads before the summary of
    the turns that followed it.
    """
    if not plan.folded:
        return list(messages)
    keep = set(plan.keep)
    last = plan.folded[-1]
    out: List[Msg] = []
    for index, message in enumerate(messages):
        if index in keep:
            out.append(message)
        if index == last:
            out.append(replacement)
    return out


def keep_token_budget(budget: int) -> int:
    """How much of the tail stays verbatim, from opencode's preserveRecent."""
    return min(MAX_KEEP_TOKENS, max(MIN_KEEP_TOKENS, int(budget) // 4))


def needs_compaction(messages: Sequence[Msg], window: int,
                     reserve: float = DEFAULT_RESERVE,
                     scale: float = 1.0) -> bool:
    """True when the history no longer fits its share of `window`.

    `window` must be the limit a *prompt* is measured against — the model's
    input limit where the provider states one, not the combined
    input-plus-output figure. Budgeting a 500k-context ChatGPT model against
    500 000 let the prompt grow 128k past what the backend accepts, and the
    failure came back as a generic server_error rather than anything that
    looked like size (issue #5).

    `reserve` is the fraction of that window the history may occupy. The
    default moved from 0.4 to DEFAULT_RESERVE: folding the conversation away
    at 40% discarded 280k tokens the model could have used, and a summary is
    always lossier than the turns it replaces.

    `scale` reweighs the local estimate by what the provider has actually
    reported for this conversation (Agent.token_scale). The estimator is
    calibrated for the average session; the correction is for this one.
    """
    total = max(0, int(window or 0))
    budget = min(int(total * reserve), max(0, total - MIN_REPLY_RESERVE)) \
        if total > MIN_REPLY_RESERVE else int(total * reserve)
    if budget <= 0 or len(messages) <= 4:
        return False
    estimated = sum(message_tokens(m) for m in messages)
    return estimated * max(0.1, scale) > budget


def compact_messages(messages: Sequence[Msg], window: int, *,
                     reserve: float = DEFAULT_RESERVE, provider: Any = None,
                     model: str = "", keep_last: int = 0,
                     tail_turns: int = DEFAULT_TAIL_TURNS,
                     previous_summary: str = "", trigger: str = "auto",
                     force: bool = False, cache: bool = True,
                     max_tokens: int = SUMMARY_MAX_TOKENS,
                     scale: float = 1.0) -> CompactionResult:
    """
    Fold the old turns into one model-written summary.

    The in-memory half of compaction: same plan_compaction() and same
    summarize_with_reason() as Session.compact_now(), so the automatic trigger
    and /compact agree on what folds and on how it is summarised; only the
    persistence differs.

    Without a provider — or when the summarising call fails — the folded turns
    are dropped with a notice instead, exactly as this module behaved before
    summaries existed. That is a worse outcome, never a lost conversation, and
    `CompactionResult.error` says which one happened so a front end can tell
    the user.

    `cache` reuses a summary already written for the same fold. The request
    path calls this on every provider round, and a fold only changes when the
    conversation does, so without it a ten-step run would buy ten summaries of
    almost the same messages.
    """
    history = list(messages)
    if not force and not needs_compaction(history, window, reserve, scale):
        return CompactionResult(messages=history, kept=len(history),
                                trigger=trigger)

    # The keep-budget is compared against local estimates inside the plan,
    # so it is expressed in estimator units: a scale saying "the estimator
    # runs low here" must shrink what is kept, not enlarge it.
    budget = int(max(0, int(window or 0)) * reserve / max(0.1, scale))
    plan = plan_compaction(history, keep_last=keep_last, tail_turns=tail_turns,
                           keep_tokens=keep_token_budget(budget))
    if not plan.folded:
        return CompactionResult(messages=history, kept=len(history),
                                trigger=trigger)

    folded = [history[index] for index in plan.folded]
    summary, error, reused = "", "no summariser available", False
    if provider is not None:
        anchor = previous_summary or last_summary(folded)
        key = _fold_fingerprint(folded, model, anchor) if cache else ""
        remembered = _recall_summary(key) if key else None
        if remembered is not None:
            summary, error = remembered
            reused = True
        else:
            summary, error = summarize_with_reason(
                folded, provider, model, previous_summary=anchor,
                max_tokens=max_tokens)
            if key:
                # Failures are remembered too: a provider that is down must be
                # dialled once, not once per step for the rest of the run.
                _remember_summary(key, (summary, error))
    if not summary:
        return CompactionResult(
            messages=apply_plan(history, plan,
                                drop_notice_message(len(folded), error)),
            folded=len(folded), kept=len(plan.keep), error=error,
            trigger=trigger, cached=reused)
    return CompactionResult(
        messages=apply_plan(history, plan, summary_message(summary, len(folded))),
        folded=len(folded), kept=len(plan.keep), summary=summary,
        summarized=True, trigger=trigger, cached=reused)


def compact_history(messages: List[Msg], window: int,
                    reserve: float = DEFAULT_RESERVE, *,
                    provider: Any = None, model: str = "",
                    scale: float = 1.0) -> List[Msg]:
    """
    Compaction for callers that only want the new history back.

    This is the request-assembly path: `window` is the input limit, and
    `scale` is the caller's live estimator correction. Passing `provider` is
    what makes the automatic trigger summarise rather than drop; leaving it
    out keeps the mechanical fallback, unchanged, for callers that have no
    provider to spend (an in-memory /compact with no session behind it).
    """
    return compact_messages(messages, window, reserve=reserve,
                            provider=provider, model=model,
                            scale=scale).messages
