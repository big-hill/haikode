"""A small, size-capped log of provider failures.

Provider failures are deliberately never written into the conversation
(agent.ProviderFailure explains why: an error stored as something the
model said gets argued with next turn). The consequence, found in the
field, is that they are also unrecoverable afterwards: printed once, red,
and gone. A user correlating drops with their flaky wifi — or writing a
bug report — has nothing to point at.

So they go here instead: one line per failure, newest last, trimmed when
the file outgrows its cap. Nothing in this module raises; a logging
failure must never be the reason a session ends.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .context import global_config_dir

FAILURE_LOG = "failures.log"
MAX_LOG_BYTES = 128 * 1024
KEEP_LINES = 400


def log_path() -> Path:
    return Path(global_config_dir()) / FAILURE_LOG


def record_failure(label: str, message: str,
                   error: Optional[Dict[str, Any]] = None) -> None:
    """Append one failure. Never raises, never blocks on anything slow."""
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label": str(label or "failure"),
        "message": " ".join(str(message or "").split())[:400],
    }
    if isinstance(error, dict):
        for key in ("kind", "provider", "model", "status", "retryable"):
            value = error.get(key)
            if value not in (None, "", False):
                entry[key] = value
    try:
        directory = Path(global_config_dir())
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / FAILURE_LOG
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim(target)
    except OSError:
        pass


def _trim(target: Path) -> None:
    """Keep the newest KEEP_LINES once the file outgrows its cap."""
    try:
        if target.stat().st_size <= MAX_LOG_BYTES:
            return
        lines = target.read_text("utf-8", errors="replace").splitlines()
        keep = lines[-KEEP_LINES:]
        temporary = target.with_suffix(".tmp")
        temporary.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(target))
    except OSError:
        pass


def recent_failures(limit: int = 20) -> List[Dict[str, Any]]:
    """The newest entries, newest first. Unreadable lines are skipped."""
    try:
        lines = log_path().read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def report(limit: int = 20) -> str:
    """The text a `/failures` command prints."""
    entries = recent_failures(limit)
    if not entries:
        return ("No provider failures recorded.\n%s" % log_path())
    lines = ["Recent provider failures (newest first):"]
    for entry in entries:
        detail = entry.get("message", "")
        marks = [str(entry[key]) for key in ("provider", "kind")
                 if entry.get(key)]
        if marks:
            detail = "[%s] %s" % (" ".join(marks), detail)
        lines.append("  %s  %s" % (entry.get("time", "?"), detail))
    lines.append("")
    lines.append(str(log_path()))
    return "\n".join(lines)


__all__ = ["FAILURE_LOG", "log_path", "recent_failures", "record_failure",
           "report"]
