"""
Model-family system prompt selection.

opencode does not ship one system prompt: it picks a text tuned to the model
family that will read it, because the families were trained with very different
instruction-following habits. This is the port of that selection
(packages/opencode/src/session/system.ts, `provider()`), plus the two things
haikode adds on top: the `# Haiku OS` section every variant must carry, and the
plan-mode preamble for the read-only `plan` agent.

Nothing here touches the network or the terminal, and loading the module has no
side effects beyond defining tables — the prompt files are read lazily and
cached, so a TUI can call select_prompt() on every keystroke if it wants to.
"""

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

PROMPTS_DIR = Path(__file__).parent / "prompts"

DEFAULT_VARIANT = "default"
HAIKU_FILE = "haiku.md"
PLAN_FILE = "plan.md"
PLAN_MODE_FILE = "plan-mode.md"
BUILD_SWITCH_FILE = "build-switch.md"

# A prompt name resolves to a file inside PROMPTS_DIR and nowhere else. The name
# can reach us from an agent definition, and an agent definition can arrive with
# a checked-out repository, so "haiku" must not be spellable as "/etc/shadow" or
# "../../../boot/home/config/settings/haikode/auth". No separators, no leading
# dot, no drive letters: anything else is refused before it reaches the disk.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# The marker that proves a prompt text carries the Haiku OS briefing. Kept as a
# literal rather than derived from haiku.md so a mangled haiku.md cannot make
# every variant silently look "already Haiku-aware".
HAIKU_MARKER = "# Haiku OS"

# These instructions describe haikode-owned tools, so they are appended after
# the model-family prompt instead of copied into eight vendor prompt files. The
# tool list is consulted first: telling a restricted agent to call a tool it was
# deliberately denied creates a retry loop rather than useful behaviour.
_TODO_GUIDANCE = """# Task tracking
Use todowrite for work with three or more meaningful steps or multiple requested deliverables. Keep the list current as the work changes, with at most one item in progress. Skip it for simple tasks."""

_MEMORY_GUIDANCE = """# Durable memory
Use memory_write when the user states or corrects a durable preference or project fact, or when you discover a stable decision or non-obvious gotcha that a future session could not cheaply recover. Write it when learned; after substantial work, briefly consider whether any such fact remains unsaved. Do not write a memory every turn, and do not store secrets, guesses, current-task progress, or facts cheaply read from project files. Reuse the same name to correct an existing memory. Use memory_read when the saved-memory index points to relevant detail."""

_MEMORY_READ_GUIDANCE = """# Durable memory
Use memory_read when the saved-memory index points to relevant detail. Treat saved memories as established facts about this user and project."""

_HISTORY_GUIDANCE = """# Earlier sessions
This conversation does not contain previous ones. When the user refers to earlier work - "last time", "the previous session", "where we left off" - call session_history rather than saying you cannot see it: it lists recent sessions and reads any of them back."""

_CONFIG_GUIDANCE = """# Live configuration
Configuration files are snapshots for this running session. External edits take effect only after the user runs /reload or restarts haikode."""

# Last-resort text if even system.md cannot be read. A degraded agent is still
# better than an exception during startup on a half-installed system.
FALLBACK_PROMPT = (
    "You are haikode, an interactive CLI coding agent running natively on "
    "Haiku OS. Be concise and direct. Use the tools available to you rather "
    "than describing what should be done."
)

# Last-resort texts for the short auxiliary files. These must NOT degrade to the
# default system prompt: plan_preamble() and build_switch() get appended to a
# prompt or injected into the conversation, so falling back to system.md would
# staple a second full system prompt onto the message instead of a reminder.
_AUX_FALLBACKS: Dict[str, str] = {
    "haiku": (
        "# Haiku OS\n"
        "You are running natively on Haiku, a BeOS-compatible desktop OS. It "
        "is not Linux; many assumptions do not carry over.\n"
        "- Package management is `pkgman`, and packages are HPKG. Development "
        "headers come from `haiku_devel`.\n"
        "- Native GUI applications use the BeAPI and link against `-lbe`; "
        "`BFilePanel` additionally needs `-ltracker`.\n"
        "- The native build tool is `jam` (Jamfile).\n"
        "- Processes are called teams: `ps` lists them.\n"
        "- User settings live under `/boot/home/config`.\n"
        "- Do not launch GUI applications unless the user asks."
    ),
    "plan": (
        "<system-reminder>\n"
        "Plan mode is ACTIVE - you are in a READ-ONLY phase. Any file edit, "
        "file creation or system change is STRICTLY FORBIDDEN, including via "
        "shell commands. This overrides all other instructions. Observe, "
        "analyse, and present a plan.\n"
        "</system-reminder>"
    ),
    "plan-mode": (
        "<system-reminder>\n"
        "Plan mode is ACTIVE - you are in a READ-ONLY phase. Any file edit, "
        "file creation or system change is STRICTLY FORBIDDEN. ${planInfo}\n"
        "</system-reminder>"
    ),
    "build-switch": (
        "<system-reminder>\n"
        "Your operational mode has changed from plan to build.\n"
        "You are no longer in read-only mode: file edits, shell commands and "
        "the rest of your tools are permitted again.\n"
        "</system-reminder>"
    ),
}

# Filenames that back each variant, in the order available() reports them.
# "default" maps to system.md, haikode's own prompt, which predates this
# library and already reads like opencode's default.txt.
VARIANT_FILES: Dict[str, str] = {
    "anthropic": "anthropic.md",
    "gpt": "gpt.md",
    "codex": "codex.md",
    "beast": "beast.md",
    "gemini": "gemini.md",
    "kimi": "kimi.md",
    "meta": "meta.md",
    "trinity": "trinity.md",
    DEFAULT_VARIANT: "system.md",
}

_NAME_BY_FILE = {filename: name for name, filename in VARIANT_FILES.items()}

# Warnings collected while loading prompt files. A missing file degrades to the
# default variant instead of raising, but the reason must survive so /status can
# surface a broken install.
LOAD_WARNINGS: List[str] = []

_CACHE: Dict[str, str] = {}


# --- matchers ---------------------------------------------------------------


def _contains(*needles: str) -> Callable[[str], bool]:
    return lambda model: any(needle in model for needle in needles)


def _contains_all(*needles: str) -> Callable[[str], bool]:
    return lambda model: all(needle in model for needle in needles)


# OpenAI's reasoning series is named "o1"/"o3" with nothing else around it.
# opencode matches the bare substring; haikode requires a non-alphanumeric
# boundary so an unrelated id that merely happens to contain the digraph (say a
# local build tagged "...-o3x") does not get routed to the beast prompt.
_O_SERIES = re.compile(r"(?:^|[^a-z0-9])o[13](?:[^a-z0-9]|$)")


def _o_series(model: str) -> bool:
    return bool(_O_SERIES.search(model))


# Ordered selection rules. First match wins. Each rule cites where it comes
# from in packages/opencode/src/session/system.ts (function `provider`).
PROMPT_VARIANTS: List[Tuple[Callable[[str], bool], str]] = [
    # system.ts:28 — id.includes("muse-spark")
    (_contains("muse-spark"), "meta.md"),
    # system.ts:29-30 — id.includes("gpt-4") || includes("o1") || includes("o3")
    # The older GPT-4 line and the o-series reasoning models get the "beast"
    # keep-going prompt; gpt-5 deliberately does not.
    (lambda m: "gpt-4" in m or _o_series(m), "beast.md"),
    # system.ts:31-34 — includes("gpt") && includes("codex")
    (_contains_all("gpt", "codex"), "codex.md"),
    # haikode addition: OpenAI also ships codex ids without "gpt" in them
    # (eg. "codex-mini-latest"), which would otherwise fall through to default.
    (_contains("codex"), "codex.md"),
    # system.ts:35 — includes("gpt"); catches gpt-5* and ollama's "gpt-oss:*"
    (_contains("gpt"), "gpt.md"),
    # system.ts:37 — includes("gemini-"); loosened to "gemini" because local
    # and OpenAI-compatible gateways are not consistent about the hyphen.
    (_contains("gemini"), "gemini.md"),
    # system.ts:38 — includes("claude"); "anthropic" added because haikode
    # routinely carries provider-qualified ids like "anthropic/claude-...".
    (_contains("claude", "anthropic"), "anthropic.md"),
    # system.ts:39 — lowercased includes("trinity")
    (_contains("trinity"), "trinity.md"),
    # system.ts:40 — lowercased includes("kimi")
    (_contains("kimi"), "kimi.md"),
    # system.ts:41 — everything else. Deliberately catches the ollama-style ids
    # with no family signal at all: glm-*, qwen*, llama*, mistral*, deepseek*.
]


# --- loading ----------------------------------------------------------------


def clear_cache() -> None:
    """Drop cached prompt texts and warnings so edits on disk are picked up."""
    _CACHE.clear()
    LOAD_WARNINGS.clear()


def _warn(message: str) -> None:
    if message not in LOAD_WARNINGS:
        LOAD_WARNINGS.append(message)


def _read(filename: str) -> Optional[str]:
    """Read one file from PROMPTS_DIR, or None if it is unreadable.

    UnicodeDecodeError is caught alongside OSError: a prompt file that got
    truncated mid-codepoint or replaced by a binary blob must degrade like a
    missing file, not crash the session on the first turn.
    """
    if not _SAFE_NAME.match(filename):
        return None
    try:
        return (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
        return None


def _resolve(name: str) -> str:
    """Normalise a prompt name to its cache key.

    Strips a redundant ".md" and folds a file stem onto the variant it backs, so
    load("system"), load("default") and load("default.md") share one cache entry
    instead of holding three copies of the same eight kilobytes.
    """
    key = name.strip().lower()
    if key.endswith(".md"):
        key = key[:-3]
    return _NAME_BY_FILE.get(key + ".md", key)


def load(name: str) -> str:
    """Return the text of a prompt file by variant name or file stem.

    Accepts variant names ("anthropic", "default") and the bare stems of the
    other prompt files ("plan", "haiku", "build-switch"). A name that cannot be
    resolved degrades to the default variant and records a warning; this never
    raises, because a broken prompt file must not stop the agent from running.
    Names are confined to PROMPTS_DIR — see _SAFE_NAME.
    """
    key = _resolve(name)
    if key in _CACHE:
        return _CACHE[key]

    if not _SAFE_NAME.match(key):
        # Refused before any filesystem access; do not cache attacker-chosen
        # keys, or a repository could grow LOAD_WARNINGS and _CACHE without
        # bound just by naming agents.
        _warn(f"refused prompt name '{name}', using {DEFAULT_VARIANT}")
        return _default_text()

    filename = VARIANT_FILES.get(key, key + ".md")
    text = _read(filename)
    if text is None:
        if key == DEFAULT_VARIANT:
            _warn(f"prompt file {filename} missing, using built-in fallback")
            text = FALLBACK_PROMPT
        elif key in _AUX_FALLBACKS:
            _warn(f"prompt file {filename} missing, using built-in fallback")
            text = _AUX_FALLBACKS[key]
        elif key in VARIANT_FILES:
            _warn(f"prompt file {filename} missing, using {DEFAULT_VARIANT}")
            text = _default_text()
        else:
            _warn(f"unknown prompt '{name}', using {DEFAULT_VARIANT}")
            text = _default_text()
    _CACHE[key] = text
    return text


def _default_text() -> str:
    """The default variant. Cannot recurse: load() resolves DEFAULT_VARIANT
    against FALLBACK_PROMPT directly rather than calling back into here."""
    return load(DEFAULT_VARIANT)


def available() -> List[str]:
    """Variant names in selection order; the default variant is last."""
    return list(VARIANT_FILES)


def haiku_section() -> str:
    """The `# Haiku OS` briefing shared by every variant."""
    return load(HAIKU_FILE)


def plan_preamble() -> str:
    """The read-only plan-mode reminder, ported from opencode's plan.txt."""
    return load(PLAN_FILE)


def plan_mode(plan_info: str = "") -> str:
    """The experimental plan-mode reminder, with `${planInfo}` substituted.

    opencode injects this one as a synthetic user part and fills the placeholder
    with the location of the plan file (session/reminders.ts). The text is
    useless with the literal placeholder left in it, so substitution happens
    here rather than at every call site.
    """
    return load(PLAN_MODE_FILE).replace("${planInfo}", plan_info)


def build_switch() -> str:
    """The reminder injected when the session leaves plan mode for build."""
    return load(BUILD_SWITCH_FILE)


# --- selection --------------------------------------------------------------


def select_variant(model: str) -> str:
    """Return the variant name a model id resolves to, for /status and debug."""
    normalized = (model or "").lower()
    for matcher, filename in PROMPT_VARIANTS:
        if matcher(normalized):
            return _NAME_BY_FILE[filename]
    return DEFAULT_VARIANT


def select_prompt(model: str, agent: str = "build",
                  agent_prompt: str = "") -> str:
    """Assemble the system prompt for a model id and agent.

    Variant text, then the Haiku briefing (re-appended only if the variant file
    somehow lost it), then the plan-mode preamble for the plan agent. The plan
    text goes last on purpose: it claims to override everything above it, and a
    model weighs the end of a long system prompt more heavily.

    `agent_prompt` is the body of an agent definition (AGENT.md / the `prompt`
    field of an agent file). opencode's session/llm/request.ts:60 uses it
    *instead of* the model-family prompt rather than in addition to it, and this
    follows that: an agent author who writes a prompt gets the prompt they
    wrote. The Haiku briefing is still appended, because an agent file cannot be
    allowed to make the model forget which operating system it is on.
    """
    override = (agent_prompt or "").strip()
    text = override or load(select_variant(model)).rstrip()
    for extra in (haiku_section() if HAIKU_MARKER not in text else "",
                  plan_preamble()
                  if (agent or "").strip().lower() == "plan" else ""):
        extra = extra.rstrip()
        if extra:
            text = text + "\n\n" + extra
    return text


def capability_guidance(tool_names: Sequence[str]) -> str:
    """Instructions for stateful tools that models otherwise never discover."""
    available = set(tool_names)
    parts: List[str] = []
    if "todowrite" in available:
        parts.append(_TODO_GUIDANCE)
    if "memory_write" in available:
        parts.append(_MEMORY_GUIDANCE)
    elif "memory_read" in available:
        # A read-only agent told to memory_write would loop on tool-not-found
        # retries — the comment above exists precisely for this case.
        parts.append(_MEMORY_READ_GUIDANCE)
    if "session_history" in available:
        parts.append(_HISTORY_GUIDANCE)
    return "\n\n".join(parts)


def build_system_prompt(model: str, agent: str = "build",
                        instructions: str = "", environment: str = "",
                        agent_prompt: str = "",
                        tool_names: Sequence[str] = ()) -> str:
    """Full system message: variant, environment block, project instructions.

    Same order and separators agent.py assembles today, so the agent can be
    switched to this function without changing what the model sees.
    """
    parts = [select_prompt(model, agent, agent_prompt),
             capability_guidance(tool_names),
             _CONFIG_GUIDANCE,
             (environment or "").strip()]
    instructions = (instructions or "").strip()
    if instructions:
        parts.append("# Project instructions\n" + instructions)
    return "\n\n".join(part for part in parts if part)
