"""
Subprocess execution with hard timeouts and no orphan processes.

Every child is started in its own process group (`start_new_session=True`) so
that a timeout can kill the *group* — an agent that shelled out to a compiler,
a test runner or a sleeping `curl` leaves those behind otherwise, and an
orphaned child holding the pipe would make `communicate()` hang forever.

Every group we ever created is remembered in `SPAWNED_GROUPS`; `survivors()`
reports the ones still alive at the end of a benchmark run, so "no orphan
processes" is a measurement rather than a hope.
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# Seconds to wait after SIGTERM before escalating to SIGKILL.
GRACE = 5.0

# (pgid, argv0) for every process group this harness has ever started.
SPAWNED_GROUPS: List[tuple] = []


@dataclass
class ProcResult:
    argv: List[str]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    error: str = ""
    pgid: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def as_dict(self) -> dict:
        return {
            "argv": self.argv,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration, 3),
            "timed_out": self.timed_out,
            "error": self.error,
        }


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the whole group, then SIGKILL it. Never raises."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            return
        deadline = time.monotonic() + GRACE
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)


def run_process(argv: Sequence[str], cwd=None, env=None, timeout: float = 300.0,
                stdin_text: Optional[str] = None) -> ProcResult:
    """Run argv to completion or to the timeout. Always reaps the whole group."""
    argv = [str(a) for a in argv]
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        return ProcResult(argv, None, "", "", time.monotonic() - start,
                          error="could not start process: %s" % e)

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        SPAWNED_GROUPS.append((pgid, argv[0]))

    timed_out = False
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc)
        try:
            out, err = proc.communicate(timeout=GRACE)
        except Exception:  # pragma: no cover - pipes already torn down
            out, err = "", ""
    except KeyboardInterrupt:
        _terminate(proc)
        raise

    # Belt and braces: a grandchild that outlived the direct child would keep
    # the group alive. Sweep it whether or not we timed out.
    if pgid is not None:
        try:
            os.killpg(pgid, 0)
        except (OSError, ProcessLookupError):
            pass
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    return ProcResult(argv, proc.returncode, out or "", err or "",
                      time.monotonic() - start, timed_out=timed_out, pgid=pgid)


def survivors() -> List[dict]:
    """Process groups this harness started that are still alive."""
    alive = []
    for pgid, argv0 in SPAWNED_GROUPS:
        if pgid == os.getpgid(0):  # never report our own group
            continue
        try:
            os.killpg(pgid, 0)
        except (OSError, ProcessLookupError):
            continue
        alive.append({"pgid": pgid, "argv0": argv0})
    return alive


def shell(command: str, cwd=None, env=None, timeout: float = 120.0) -> ProcResult:
    """Run a shell command line — the form task checks use."""
    return run_process(["/bin/sh", "-c", command], cwd=cwd, env=env, timeout=timeout)
