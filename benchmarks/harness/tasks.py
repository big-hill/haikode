"""
Task definitions: loading and validating `benchmarks/tasks/<name>/task.json`.

A task is a directory:

    tasks/<name>/task.json     the prompt(s), model, timeout and the checks
    tasks/<name>/setup/        the fixture project, copied fresh for every run

Nothing in a task file is interpreted by a model. Everything is either a
literal string handed to the agent, or a programmatic assertion.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

KNOWN_KEYS = {
    "name", "description", "proves", "category", "prompt", "turns", "provider",
    "model", "timeout", "command_timeout", "agent", "checks", "pre_checks",
    "ignore", "git_init", "outside_files", "requires_commands", "auto_approve",
    "max_steps", "notes", "env", "setup_commands", "conversation", "interface",
    "context_window",
}

# How a task wants the runner driven. "" is whatever the harness was told on
# the command line; "json" forces the runner's machine-readable interface,
# which is a different code path in both engines and therefore worth scoring
# on its own rather than assuming it matches the human one.
INTERFACES = ("", "json")


class TaskError(ValueError):
    pass


@dataclass
class Turn:
    index: int
    prompt: str


@dataclass
class Task:
    name: str
    path: Path
    setup: Path
    description: str = ""
    proves: str = ""
    category: str = ""
    provider: str = "zen"
    model: str = ""
    timeout: float = 600.0
    command_timeout: float = 120.0
    agent: str = ""
    turns: List[Turn] = field(default_factory=list)
    checks: List[dict] = field(default_factory=list)
    pre_checks: List[dict] = field(default_factory=list)
    ignore: tuple = ()
    git_init: bool = False
    outside_files: Dict[str, str] = field(default_factory=dict)
    requires_commands: List[str] = field(default_factory=list)
    auto_approve: bool = True
    max_steps: Optional[int] = None
    notes: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    setup_commands: List[str] = field(default_factory=list)
    conversation: bool = False
    interface: str = ""
    context_window: Optional[int] = None

    def missing_commands(self) -> List[str]:
        return [c for c in self.requires_commands if shutil.which(c) is None]


def load_task(directory: Path) -> Task:
    directory = Path(directory)
    manifest = directory / "task.json"
    if not manifest.is_file():
        raise TaskError("%s has no task.json" % directory)
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError as e:
        raise TaskError("%s is not valid JSON: %s" % (manifest, e)) from e
    if not isinstance(data, dict):
        raise TaskError("%s must contain a JSON object" % manifest)

    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise TaskError("%s has unknown key(s): %s"
                        % (manifest, ", ".join(sorted(unknown))))

    setup = directory / "setup"
    if not setup.is_dir():
        raise TaskError("%s has no setup/ directory" % directory)

    raw_turns = data.get("turns")
    if raw_turns is None:
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise TaskError("%s needs either 'prompt' or 'turns'" % manifest)
        turns = [Turn(0, prompt)]
    else:
        if not isinstance(raw_turns, list) or not raw_turns:
            raise TaskError("%s: 'turns' must be a non-empty list" % manifest)
        turns = []
        for index, entry in enumerate(raw_turns):
            if isinstance(entry, str):
                turns.append(Turn(index, entry))
            elif isinstance(entry, dict) and isinstance(entry.get("prompt"), str):
                turns.append(Turn(index, entry["prompt"]))
            else:
                raise TaskError("%s: turn %d needs a 'prompt' string"
                                % (manifest, index + 1))

    checks = data.get("checks") or []
    if not isinstance(checks, list) or not checks:
        raise TaskError("%s must define at least one check" % manifest)
    for spec in checks:
        if not isinstance(spec, dict) or "type" not in spec:
            raise TaskError("%s: every check needs a 'type'" % manifest)

    task = Task(
        name=data.get("name") or directory.name,
        path=directory,
        setup=setup,
        description=data.get("description", ""),
        proves=data.get("proves", ""),
        category=data.get("category", ""),
        provider=data.get("provider", "zen"),
        model=data.get("model", ""),
        timeout=float(data.get("timeout", 600)),
        command_timeout=float(data.get("command_timeout", 120)),
        agent=data.get("agent", ""),
        turns=turns,
        checks=checks,
        pre_checks=data.get("pre_checks") or [],
        ignore=tuple(data.get("ignore") or ()),
        git_init=bool(data.get("git_init", False)),
        outside_files=dict(data.get("outside_files") or {}),
        requires_commands=list(data.get("requires_commands") or []),
        auto_approve=bool(data.get("auto_approve", True)),
        max_steps=data.get("max_steps"),
        notes=data.get("notes", ""),
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        setup_commands=list(data.get("setup_commands") or ()),
        conversation=bool(data.get("conversation", False)),
        interface=data.get("interface", "") or "",
        context_window=data.get("context_window"),
    )
    if task.name != directory.name:
        raise TaskError("%s: 'name' (%s) must match the directory name (%s)"
                        % (manifest, task.name, directory.name))
    if task.interface not in INTERFACES:
        raise TaskError("%s: unknown interface %r (known: %s)"
                        % (manifest, task.interface,
                           ", ".join(i or "(default)" for i in INTERFACES)))
    if task.conversation and len(task.turns) < 2:
        raise TaskError("%s: 'conversation' needs at least two turns — one turn "
                        "resumes nothing" % manifest)
    if task.context_window is not None and int(task.context_window) <= 0:
        raise TaskError("%s: 'context_window' must be a positive number of tokens"
                        % manifest)
    return task


def discover(tasks_dir: Path) -> List[Task]:
    tasks_dir = Path(tasks_dir)
    found = []
    for entry in sorted(tasks_dir.iterdir()):
        if entry.is_dir() and (entry / "task.json").is_file():
            found.append(load_task(entry))
    return found
