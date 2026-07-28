"""
Runners: the two things a task can be scored against.

  haikode   the reimplementation under test, driven either through
            `harness/driver_haikode.py` (default — reports token counts) or
            through the real CLI, `python3 -m haikode --yes` (`--haikode-mode cli`).
  opencode  the installed `opencode` binary, `opencode run --format json`.

Both are handed the *same* prompt, the same fixture copy, the same wall-clock
timeout and an isolated $HOME. If a runner cannot run at all — no binary, no
credentials, no importable package — `availability()` says so in words. The
report then prints that sentence instead of a column of zeros, because an
unavailable runner is not a failing runner.
"""

import json
import re
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import procs

HARNESS_DIR = Path(__file__).resolve().parent
DRIVER = HARNESS_DIR / "driver_haikode.py"


@dataclass
class TurnResult:
    index: int
    prompt: str
    text: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    exit_code: Optional[int] = None
    duration: float = 0.0
    timed_out: bool = False
    error: str = ""
    stdout: str = ""
    stderr: str = ""
    model: str = ""
    provider: str = ""
    argv: List[str] = field(default_factory=list)

    def tool_histogram(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for call in self.tool_calls:
            name = call.get("name") or "?"
            counts[name] = counts.get(name, 0) + 1
        return counts

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "duration_s": round(self.duration, 2),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "error": self.error,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tool_calls": len(self.tool_calls),
            "tools": self.tool_histogram(),
            "model": self.model,
            "provider": self.provider,
            "answer_chars": len(self.text),
        }


class Runner:
    key = ""
    label = ""

    def __init__(self, options=None):
        self.options = options or {}
        self.notes: List[str] = []

    # -- capability reporting -------------------------------------------

    def availability(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def identity(self) -> Dict[str, str]:
        """What exactly was executed — recorded verbatim in results.json."""
        return {"runner": self.key}

    def supports(self, task) -> Tuple[bool, str]:
        """Can this runner express what the task asks for?"""
        return True, ""

    def preflight(self, task, home: Path) -> Tuple[bool, str]:
        """Are there credentials for this task's provider? Checked once, cheaply.

        A missing key must be reported in words before any run, not discovered
        as eight identical HTTP 401s that read like eight agent failures.
        """
        return True, ""

    def resolve_model(self, task) -> str:
        raise NotImplementedError

    def env_for(self, home: Path, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("XDG_CONFIG_HOME", None)
        env.pop("XDG_DATA_HOME", None)
        env.pop("XDG_CACHE_HOME", None)
        env.pop("XDG_STATE_HOME", None)
        # A task may plant variables of its own — a canary credential, say.
        # They go in last so a task can also override one of the above.
        env.update(extra or {})
        return env

    def prepare(self, task, box) -> None:
        """Per-run setup a runner needs before its first turn.

        Called after the shared $HOME has been restored to its baseline, so
        anything written here lives for exactly one run and is reverted before
        the next one starts.
        """
        return None

    def owned_home_paths(self) -> Tuple[str, ...]:
        """$HOME-relative config the *harness* writes, not the agent.

        The home config guard reverts these along with everything else, but
        reporting that as "a previous run poisoned the shared home" would be a
        lie about our own pin — and a warning that cries wolf on every run is
        how a real contamination gets ignored.
        """
        return ()

    def run_turn(self, task, turn, box, run_dir: Path) -> TurnResult:
        raise NotImplementedError


# --------------------------------------------------------------------------
# haikode


def _find_repo(start: Path) -> Optional[Path]:
    for candidate in [start] + list(start.parents):
        if (candidate / "haikode" / "__init__.py").is_file():
            return candidate
    return None


def _haikode_config_path(home: Path) -> Path:
    """Mirror haikode.config.default_config_path() for a given $HOME."""
    if os.path.exists("/boot/home"):
        return home / "config" / "settings" / "haikode" / "config.json"
    return home / ".config" / "haikode" / "config.json"


class HaikodeRunner(Runner):
    key = "haikode"
    label = "haikode"

    def __init__(self, options=None):
        super().__init__(options)
        self.repo = Path(self.options.get("repo") or "") if self.options.get("repo") \
            else _find_repo(HARNESS_DIR)
        self.mode = self.options.get("mode", "driver")
        self.python = self.options.get("python") or sys.executable
        self._cli_flags: Optional[set] = None

    def cli_flags(self) -> set:
        """Which long options `haikode --help` actually offers.

        Probed rather than assumed: haikode is under active development, and a
        benchmark that hard-codes "the CLI cannot do X" will keep saying so for
        weeks after someone added X.
        """
        if self._cli_flags is None:
            self._cli_flags = set()
            if self.repo is not None:
                probe = procs.run_process(
                    [self.python, "-m", "haikode", "--help"], cwd=self.repo,
                    env={**os.environ, "PYTHONPATH": str(self.repo),
                         "HAI_DISABLE_KEYSTORE": "1"},
                    timeout=90)
                for token in re.findall(r"--[a-z][a-z0-9-]*",
                                        probe.stdout + probe.stderr):
                    self._cli_flags.add(token)
        return self._cli_flags

    def availability(self) -> Tuple[bool, str]:
        if self.repo is None:
            return False, ("no haikode package found — looked for haikode/__init__.py "
                           "above %s" % HARNESS_DIR)
        probe = procs.run_process([self.python, "-c", "import haikode, sys; "
                                   "sys.stdout.write(haikode.__name__)"],
                                  cwd=self.repo, timeout=60,
                                  env={**os.environ, "PYTHONPATH": str(self.repo)})
        if not probe.ok:
            return False, "haikode is not importable: %s" % (
                (probe.stderr or probe.error).strip().splitlines()[-1:] or ["unknown"])[0]
        return True, ""

    def identity(self) -> Dict[str, str]:
        return {"runner": self.key, "mode": self.mode,
                "repo": str(self.repo), "python": self.python}

    def uses_cli(self, task) -> bool:
        """Is this task driven through the real CLI rather than the driver?

        A task asking for the JSON interface or for a multi-turn conversation
        has to be: the driver builds one throwaway agent per turn and never
        touches the session store, so it can neither emit haikode's `--json`
        stream nor resume anything.
        """
        return self.mode == "cli" or task.interface == "json" or task.conversation

    def supports(self, task) -> Tuple[bool, str]:
        flags = self.cli_flags()
        cli = self.uses_cli(task)
        if task.interface == "json" and "--json" not in flags:
            return False, ("`python3 -m haikode --help` lists no --json flag, so "
                           "there is no machine-readable interface to score")
        if task.conversation and "--continue" not in flags:
            return False, ("`python3 -m haikode --help` lists no --continue flag, "
                           "so a multi-turn conversation cannot be resumed")
        if task.agent and task.agent != "build":
            if "--agent" not in flags:
                if cli:
                    return False, ("haikode's CLI offers no --agent flag, so the %r "
                                   "agent cannot be selected in cli mode" % task.agent)
                self._note("haikode: the %r agent was reached through the engine's "
                           "own API from the driver. `python3 -m haikode --help` "
                           "lists no --agent flag, so a scripted one-shot run "
                           "cannot select it." % task.agent)
        if cli and "--model" not in flags:
            self._note("haikode's CLI offers no --model flag; the benchmark pinned "
                       "the model by writing a config file into the sandbox $HOME "
                       "instead, which is not what a user would do.")
        if cli and self.mode != "cli":
            self._note("task %s was driven through haikode's real CLI rather than "
                       "the driver (%s), so it reports no separate token count."
                       % (task.name,
                          "it needs the --json event stream" if task.interface == "json"
                          else "it needs a resumable session"))
        return True, ""

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def resolve_model(self, task) -> str:
        return self.options.get("model") or task.model

    def preflight(self, task, home: Path) -> Tuple[bool, str]:
        provider = self.options.get("provider") or task.provider
        script = (
            "import sys, json;"
            "sys.path.insert(0, %r);"
            "from haikode.config import Config;"
            "from haikode.runtime import provider_status;"
            "c = Config();"
            "print(json.dumps({'known': %r in c.data.get('providers', {}),"
            " 'status': provider_status(c, %r)}))"
            % (str(self.repo), provider, provider))
        probe = procs.run_process([self.python, "-c", script],
                                  env=self.env_for(home), timeout=60)
        if not probe.ok:
            return False, "could not read haikode's provider status: %s" % (
                (probe.stderr or probe.error).strip()[-200:])
        try:
            info = json.loads(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return True, ""
        if not info["known"]:
            return False, "haikode has no provider profile named %r" % provider
        status = info["status"]
        if status.endswith("none") or "missing" in status:
            return False, ("no credentials for haikode provider %r (%s) — set the "
                           "key env var or run `haikode login %s`"
                           % (provider, status, provider))
        return True, ""

    def env_for(self, home: Path, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = super().env_for(home, extra)
        env["PYTHONPATH"] = str(self.repo)
        # The Haiku keystore helper can pop a GUI approval dialog; a benchmark
        # must never block on one.
        env["HAI_DISABLE_KEYSTORE"] = "1"
        return env

    def _write_cli_config(self, home: Path, provider: str, model: str,
                          max_steps: Optional[int],
                          context_window: Optional[int] = None) -> None:
        path = _haikode_config_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"default_provider": provider, "providers": {}}
        profile: Dict[str, object] = {}
        if model:
            profile["model"] = model
        if context_window:
            # `providers.<name>.context` is what runtime.build_agent reads for
            # Agent.context_window, and therefore what the compaction threshold
            # is a fraction of. Pinning it is the only way to make a benchmark
            # cross that threshold without a six-figure token bill.
            profile["context"] = int(context_window)
        if profile:
            data["providers"][provider] = profile
        if max_steps:
            data["max_steps"] = max_steps
        path.write_text(json.dumps(data, indent=2))
        os.chmod(path, 0o600)

    def prepare(self, task, box) -> None:
        if task.context_window:
            provider = self.options.get("provider") or task.provider
            self._write_cli_config(box.home, provider, self.resolve_model(task),
                                   task.max_steps, task.context_window)

    def owned_home_paths(self) -> Tuple[str, ...]:
        anchor = Path("/__home__")
        return (_haikode_config_path(anchor).relative_to(anchor).as_posix(),)

    def run_turn(self, task, turn, box, run_dir: Path) -> TurnResult:
        env = self.env_for(box.home, task.env)
        provider = self.options.get("provider") or task.provider
        model = self.resolve_model(task)
        prompt_file = run_dir / ("prompt-%d.txt" % (turn.index + 1))
        prompt_file.write_text(turn.prompt)

        if self.uses_cli(task):
            flags = self.cli_flags()
            json_mode = task.interface == "json" or task.conversation
            argv = [self.python, "-m", "haikode", "-C", str(box.project),
                    "-p", provider]
            if model and "--model" in flags:
                argv += ["--model", model]
            if not model or "--model" not in flags or task.context_window:
                # No --model flag, or a pinned context window: both live in the
                # config file in the sandbox $HOME.
                self._write_cli_config(box.home, provider, model, task.max_steps,
                                       task.context_window)
            if task.agent and "--agent" in flags:
                argv += ["--agent", task.agent]
            if json_mode:
                argv.append("--json")
            if task.conversation and turn.index > 0:
                argv.append("--continue")
            if task.auto_approve:
                argv.append("--yes")
            argv.append(turn.prompt)
            result = procs.run_process(argv, cwd=box.project, env=env,
                                       timeout=task.timeout)
            if json_mode:
                parsed = _parse_haikode_json(result.stdout)
            else:
                parsed = TurnResult(index=turn.index, prompt=turn.prompt,
                                    text=_strip_cli_noise(result.stdout),
                                    tool_calls=_parse_cli_tool_calls(result.stdout))
            parsed.index = turn.index
            parsed.prompt = turn.prompt
            parsed.exit_code = result.exit_code
            parsed.duration = result.duration
            parsed.timed_out = result.timed_out
            parsed.stdout = result.stdout
            parsed.stderr = result.stderr
            parsed.argv = result.argv
            parsed.model = parsed.model or model
            parsed.provider = parsed.provider or provider
            if result.timed_out:
                parsed.error = "timed out after %.0fs" % task.timeout
            elif result.error:
                parsed.error = result.error
            elif not parsed.error and HAIKODE_STREAM_ERROR in result.stdout:
                parsed.error = ("the provider stream failed and haikode returned "
                                "it as assistant text with exit status 0")
            elif not parsed.error and result.exit_code in HAIKODE_FATAL_EXITS:
                parsed.error = "haikode exited %s (%s): %s" % (
                    result.exit_code, HAIKODE_FATAL_EXITS[result.exit_code],
                    (result.stderr or "").strip()[-300:])
            return parsed

        events_path = run_dir / ("events-%d.jsonl" % (turn.index + 1))
        argv = [self.python, str(DRIVER),
                "--repo", str(self.repo),
                "--cwd", str(box.project),
                "--provider", provider,
                "--events", str(events_path),
                "--prompt-file", str(prompt_file)]
        if model:
            argv += ["--model", model]
        if task.agent:
            argv += ["--agent", task.agent]
        if task.max_steps:
            argv += ["--max-steps", str(task.max_steps)]
        if not task.auto_approve:
            argv.append("--no-auto-approve")

        result = procs.run_process(argv, cwd=box.project, env=env,
                                   timeout=task.timeout)
        turn_result = _parse_driver_events(events_path)
        turn_result.index = turn.index
        turn_result.prompt = turn.prompt
        turn_result.exit_code = result.exit_code
        turn_result.duration = result.duration
        turn_result.timed_out = result.timed_out
        turn_result.stdout = result.stdout
        turn_result.stderr = result.stderr
        turn_result.argv = result.argv
        turn_result.provider = turn_result.provider or provider
        turn_result.model = turn_result.model or model
        if result.timed_out:
            turn_result.error = "timed out after %.0fs" % task.timeout
        elif result.error:
            turn_result.error = result.error
        elif result.exit_code != 0 and not turn_result.error:
            turn_result.error = "driver exited %s: %s" % (
                result.exit_code, (result.stderr or "").strip()[-400:])
        if not turn_result.text and not turn_result.error:
            # The driver streams assistant text to stdout as well; fall back to
            # it rather than silently scoring an empty answer.
            turn_result.text = result.stdout
        return turn_result


_TOOL_LINE_PREFIX = "⏺"  # ⏺ — the REPL's tool marker


def _parse_cli_tool_calls(stdout: str) -> List[dict]:
    """Recover tool calls from the plain REPL transcript.

    The CLI prints `⏺ <tool>  <summary>` per call and colour is disabled when
    stdout is not a tty, so the line is stable. Arguments are only available in
    summarised form — enough for `arg_pattern` on bash commands and file paths.
    """
    calls = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_TOOL_LINE_PREFIX):
            continue
        body = stripped[len(_TOOL_LINE_PREFIX):].strip()
        if not body:
            continue
        parts = body.split(None, 1)
        calls.append({"name": parts[0],
                      "args": {"summary": parts[1].strip() if len(parts) > 1 else ""}})
    return calls


def _strip_cli_noise(stdout: str) -> str:
    """Approximate the assistant's own text in the plain REPL transcript.

    The REPL streams model text unindented and renders tool activity as a `⏺`
    header plus two-space-indented output, so dropping both recovers something
    close to the answer. It is an approximation: an indented code block in the
    model's own reply is lost too. `--haikode-mode driver` has the exact text;
    this exists so that answer_* checks in cli mode are not trivially satisfied
    by a grep hit inside a tool result.
    """
    keep = []
    for line in stdout.splitlines():
        if line.startswith(_TOOL_LINE_PREFIX) or line.lstrip().startswith(_TOOL_LINE_PREFIX):
            continue
        if line.startswith("  "):
            continue
        keep.append(line)
    return "\n".join(keep)


# haikode folds a failed provider stream — a 429, a dropped connection — into
# the assistant's own text and still exits 0. Left alone that reads as "the
# agent tried and got it wrong", which is a lie: the model never answered. The
# marker is haikode's own, so matching it is precise, and a task prompt that
# happens to discuss rate limits cannot trigger it.
HAIKODE_STREAM_ERROR = "[stream error]"

# haikode's CLI exit codes. Only these two mean "the engine did not get to
# answer"; 3 (a tool was refused) and 4 (the step limit) are outcomes a task's
# checks are entitled to judge for themselves, and scoring them as harness
# errors would hide a correct refusal behind an `!n`.
HAIKODE_FATAL_EXITS = {1: "provider or agent failure", 2: "bad arguments"}


def _parse_haikode_json(stdout: str) -> TurnResult:
    """Parse `haikode --json`: one JSON object per line, schema in repl.py.

    The `done` event carries the turn's whole final text, so it is preferred
    over stitching the `text` deltas back together; the deltas are the fallback
    for a run that died before `done`.
    """
    result = TurnResult(index=0, prompt="")
    deltas: List[str] = []
    final: Optional[str] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "run":
            result.model = event.get("model") or ""
            result.provider = event.get("provider") or ""
        elif kind == "text":
            deltas.append(event.get("text") or "")
        elif kind == "tool":
            result.tool_calls.append({"name": event.get("name"),
                                      "args": event.get("args") or {}})
        elif kind == "usage":
            result.tokens_in = event.get("input")
            result.tokens_out = event.get("output")
        elif kind == "error":
            result.error = "%s: %s" % (event.get("source") or "?",
                                       event.get("message") or "unknown error")
        elif kind == "done":
            final = event.get("text") or ""
            if event.get("error"):
                result.error = str(event["error"])
    result.text = final if final is not None else "".join(deltas)
    return result


def _parse_driver_events(path: Path) -> TurnResult:
    result = TurnResult(index=0, prompt="")
    if not path.is_file():
        result.error = "driver wrote no events file"
        return result
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "start":
            result.model = event.get("model") or ""
            result.provider = event.get("provider") or ""
        elif kind == "tool":
            result.tool_calls.append({"name": event.get("name"),
                                      "args": event.get("args") or {}})
        elif kind == "result":
            result.text = event.get("text") or ""
            tokens = event.get("tokens") or {}
            result.tokens_in = tokens.get("input")
            result.tokens_out = tokens.get("output")
        elif kind == "error":
            result.error = event.get("error") or "unknown driver error"
    if not result.error and HAIKODE_STREAM_ERROR in result.text:
        marker = result.text.index(HAIKODE_STREAM_ERROR)
        result.error = ("the provider stream failed and haikode returned it as "
                        "assistant text with exit status 0: %s"
                        % result.text[marker:marker + 220].strip())
    return result


# --------------------------------------------------------------------------
# opencode


# opencode names its providers differently from haikode's profiles.
OPENCODE_PROVIDER_ALIAS = {
    "zen": "opencode",
    "ollama": "ollama-cloud",
    "anthropic": "anthropic",
    "openai": "openai",
    "xai": "xai",
}


class OpencodeRunner(Runner):
    key = "opencode"
    label = "opencode"

    def __init__(self, options=None):
        super().__init__(options)
        self.binary = self._find_binary()
        self.version = ""

    def _find_binary(self) -> Optional[str]:
        explicit = self.options.get("binary") or os.environ.get("OPENCODE_BIN")
        if explicit:
            return explicit if os.path.exists(explicit) else None
        found = shutil.which("opencode")
        if found:
            return found
        default = os.path.expanduser("~/.opencode/bin/opencode")
        return default if os.path.exists(default) else None

    def availability(self) -> Tuple[bool, str]:
        if not self.binary:
            return False, ("no opencode binary — set --opencode-bin or $OPENCODE_BIN, "
                           "or install it at ~/.opencode/bin/opencode")
        probe = procs.run_process([self.binary, "--version"], timeout=90)
        if not probe.ok:
            return False, "`%s --version` failed: %s" % (
                self.binary, (probe.stderr or probe.error).strip()[:200])
        self.version = probe.stdout.strip()
        return True, ""

    def identity(self) -> Dict[str, str]:
        return {"runner": self.key, "binary": self.binary or "",
                "version": self.version}

    def resolve_model(self, task) -> str:
        override = self.options.get("model")
        if override:
            return override if "/" in override else "%s/%s" % (
                OPENCODE_PROVIDER_ALIAS.get(task.provider, task.provider), override)
        provider = self.options.get("provider") or task.provider
        alias = OPENCODE_PROVIDER_ALIAS.get(provider)
        if alias is None:
            return task.model
        return "%s/%s" % (alias, task.model)

    def preflight(self, task, home: Path) -> Tuple[bool, str]:
        """`opencode models` lists only providers that are actually configured."""
        model = self.resolve_model(task)
        provider = model.split("/")[0] if "/" in model else ""
        if not provider:
            return True, ""
        probe = procs.run_process([self.binary, "models"],
                                  env=self.env_for(home), timeout=120)
        if not probe.ok:
            return False, "`opencode models` failed: %s" % (
                (probe.stderr or probe.error).strip()[-200:])
        offered = {line.strip() for line in probe.stdout.splitlines() if line.strip()}
        if model in offered:
            return True, ""
        providers = sorted({m.split("/")[0] for m in offered if "/" in m})
        if provider not in providers:
            return False, ("opencode has no configured provider %r — it offers %s. "
                           "Run `opencode auth login`."
                           % (provider, ", ".join(providers) or "nothing"))
        return False, ("opencode's %r provider does not offer the model %r"
                       % (provider, model.split("/", 1)[1]))

    def prepare(self, task, box) -> None:
        """Pin the model's declared context limit, when the task asks for one.

        opencode reads the window from its models.dev catalogue; the documented
        override is `provider.<id>.models.<id>.limit.context`, which is what
        its own auto-summarisation measures against. Written per run, into a
        $HOME subtree the harness restores before the next one.
        """
        if not task.context_window:
            return
        model = self.resolve_model(task)
        if "/" not in model:
            return
        provider_id, model_id = model.split("/", 1)
        path = box.home / ".config" / "opencode" / "opencode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {provider_id: {"models": {model_id: {"limit": {
                "context": int(task.context_window),
                "output": max(1024, int(task.context_window) // 4),
            }}}}},
        }, indent=2))

    def owned_home_paths(self) -> Tuple[str, ...]:
        return (".config/opencode/opencode.json",)

    def run_turn(self, task, turn, box, run_dir: Path) -> TurnResult:
        env = self.env_for(box.home, task.env)
        model = self.resolve_model(task)
        (run_dir / ("prompt-%d.txt" % (turn.index + 1))).write_text(turn.prompt)

        argv = [self.binary, "run", "--dir", str(box.project), "--format", "json"]
        if task.conversation and turn.index > 0:
            argv.append("--continue")
        else:
            argv += ["--title", "bench-%s" % task.name]
        if model:
            argv += ["-m", model]
        if task.agent:
            argv += ["--agent", task.agent]
        if task.auto_approve:
            argv.append("--auto")
        argv.append(turn.prompt)

        result = procs.run_process(argv, cwd=box.project, env=env,
                                   timeout=task.timeout)
        parsed = _parse_opencode_events(result.stdout)
        parsed.index = turn.index
        parsed.prompt = turn.prompt
        parsed.exit_code = result.exit_code
        parsed.duration = result.duration
        parsed.timed_out = result.timed_out
        parsed.stdout = result.stdout
        parsed.stderr = result.stderr
        parsed.argv = result.argv
        parsed.model = model
        parsed.provider = self.options.get("provider") or task.provider
        if result.timed_out:
            parsed.error = "timed out after %.0fs" % task.timeout
        elif result.error:
            parsed.error = result.error
        elif result.exit_code != 0:
            parsed.error = "opencode exited %s: %s" % (
                result.exit_code, (result.stderr or result.stdout).strip()[-400:])
        (run_dir / ("events-%d.jsonl" % (turn.index + 1))).write_text(result.stdout)
        return parsed


def _parse_opencode_events(stdout: str) -> TurnResult:
    result = TurnResult(index=0, prompt="")
    texts: List[str] = []
    tokens_in = tokens_out = 0
    saw_tokens = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "tool_use":
            state = part.get("state") or {}
            result.tool_calls.append({"name": part.get("tool"),
                                      "args": state.get("input") or {}})
        elif kind == "text":
            text = part.get("text")
            if text:
                texts.append(text)
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            if tokens:
                saw_tokens = True
                tokens_in += int(tokens.get("input") or 0)
                tokens_out += int(tokens.get("output") or 0)
        elif kind == "error":
            result.error = json.dumps(event.get("error") or event)[:500]
    result.text = "\n".join(texts)
    if saw_tokens:
        result.tokens_in, result.tokens_out = tokens_in, tokens_out
    return result


RUNNERS = {HaikodeRunner.key: HaikodeRunner, OpencodeRunner.key: OpencodeRunner}


def build(key: str, options: dict) -> Runner:
    if key not in RUNNERS:
        raise KeyError("unknown runner %r (known: %s)" % (key, ", ".join(RUNNERS)))
    return RUNNERS[key](options)
