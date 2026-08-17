"""Validate haikode's agent-agnostic project context.

The default startup path refreshes the canonical remote reference.  Offline
validation deliberately refuses to treat NOW.md as newly fetched truth; the
stable contract and documentation integrity remain usable without a network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "docs" / "project"
REQUIRED_DOCS = (
    PROJECT / "CONTEXT.md",
    PROJECT / "CODEMAP.md",
    PROJECT / "WORKFLOW.md",
    PROJECT / "INDEX.md",
    PROJECT / "decisions" / "README.md",
)
REQUIRED_MARKERS = {
    PROJECT / "CONTEXT.md": (
        "# Project context", "## Authority model", "## Required startup sequence"),
    PROJECT / "CODEMAP.md": ("# Code map",),
    PROJECT / "WORKFLOW.md": ("# Development and release workflow",),
    PROJECT / "INDEX.md": ("# Documentation routes",),
    PROJECT / "decisions" / "README.md": ("# Architectural decision records",),
}
ADAPTERS = (ROOT / "AGENTS.md", ROOT / "CLAUDE.md")
LEGACY_DOCS = (
    ROOT / "DESKTOP_SPEC.md",
    ROOT / "specs" / "DESKTOP_SPEC.md",
    ROOT / "specs" / "hai_desktop_spec_clean.md",
)
ADR_NAME = re.compile(r"^\d{8}-\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REFERENCE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
ALLOWED_ADR_STATUS = {"proposed", "accepted", "rejected", "superseded"}
MAX_NOW_LIFETIME = dt.timedelta(days=7)
ADR_HEADINGS = (
    "## Context and problem",
    "## Alternatives considered",
    "## Decision",
    "## Rationale",
    "## Consequences",
    "## Reversal conditions",
)


class Result:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.okays: List[str] = []

    def ok(self, text: str) -> None:
        self.okays.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def fail(self, text: str) -> None:
        self.errors.append(text)


def command(args: Sequence[str], *, timeout: int = 30) -> Tuple[int, str]:
    try:
        run = subprocess.run(
            list(args), cwd=str(ROOT), capture_output=True, text=True,
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = (run.stdout or "") + (run.stderr or "")
    return run.returncode, output.strip()


def git(*args: str, timeout: int = 30) -> Tuple[int, str]:
    return command(("git",) + args, timeout=timeout)


def read(path: Path, result: Result) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        result.fail("cannot read %s: %s" % (path.relative_to(ROOT), exc))
        return ""


def frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    data: Dict[str, str] = {}
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return data, "\n".join(lines[index + 1:])
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"\'')
    return {}, text


def instant(value: str) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def validate_required(result: Result) -> None:
    problems = 0
    for path in REQUIRED_DOCS:
        relative = str(path.relative_to(ROOT))
        if not path.is_file():
            result.fail("missing required project context: %s" % relative)
            problems += 1
            continue
        text = read(path, result)
        if not text.strip():
            result.fail("required project context is empty: %s" % relative)
            problems += 1
            continue
        for marker in REQUIRED_MARKERS.get(path, ()):
            if marker not in text:
                result.fail("%s lacks required section %s" % (relative, marker))
                problems += 1
    if not problems:
        result.ok("Project context readable")


def validate_adapters(result: Result) -> None:
    problems = 0
    for path in ADAPTERS:
        text = read(path, result)
        if not text.strip():
            result.fail("%s is empty or unreadable" % path.name)
            problems += 1
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) > 10:
            result.fail("%s is not a thin adapter (%d non-empty lines)"
                        % (path.name, len(lines)))
            problems += 1
        if "docs/project/CONTEXT.md" not in text:
            result.fail("%s does not point to the project contract" % path.name)
            problems += 1
        if "stop" not in text.lower():
            result.fail("%s lacks the stop-on-missing-context rule" % path.name)
            problems += 1
    if not problems:
        result.ok("Agent adapters are thin and share one contract")


def canonical_reference(result: Result) -> str:
    """Read the canonical ref from the contract, never from transient state."""
    context = read(PROJECT / "CONTEXT.md", result)
    data, _ = frontmatter(context)
    reference = data.get("canonical_reference", "")
    if not reference:
        result.fail("CONTEXT.md lacks canonical_reference frontmatter")
        return ""
    if not REFERENCE.match(reference):
        result.fail("CONTEXT.md canonical_reference is not remote/branch")
        return ""
    workflow = read(PROJECT / "WORKFLOW.md", result)
    if "`%s`" % reference not in context:
        result.fail("CONTEXT.md does not document canonical reference %s"
                    % reference)
    if "`%s`" % reference not in workflow:
        result.fail("WORKFLOW.md does not document canonical reference %s"
                    % reference)
    if not result.errors:
        result.ok("Canonical reference declared by project contract")
    return reference


def local_link_target(document: Path, raw: str) -> Optional[Path]:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = target.split()[0] if " " in target else target
    target = target.split("#", 1)[0]
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    return (document.parent / unquote(target)).resolve()


def validate_links(result: Result) -> None:
    documents = list(PROJECT.glob("*.md"))
    documents.extend((PROJECT / "decisions").glob("*.md"))
    dead: List[str] = []
    for document in documents:
        text = read(document, result)
        for raw in LINK.findall(text):
            target = local_link_target(document, raw)
            if target is None:
                continue
            try:
                target.relative_to(ROOT)
            except ValueError:
                dead.append("%s -> %s (outside repository)" % (
                    document.relative_to(ROOT), raw))
                continue
            if not target.exists():
                dead.append("%s -> %s" % (
                    document.relative_to(ROOT), raw))
    if dead:
        for item in dead:
            result.fail("dead internal documentation link: %s" % item)
    else:
        result.ok("CODEMAP and documentation routes resolve")


def validate_adrs(result: Result) -> None:
    directory = PROJECT / "decisions"
    problems = 0
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        text = read(path, result)
        if not ADR_NAME.match(path.name):
            result.fail("invalid ADR filename: %s" % path.name)
            problems += 1
        data, _ = frontmatter(text)
        for key in ("status", "date", "decision"):
            if not data.get(key):
                result.fail("%s lacks ADR field %s" % (path.name, key))
                problems += 1
        if data.get("status") not in ALLOWED_ADR_STATUS:
            result.fail("%s has invalid ADR status %r"
                        % (path.name, data.get("status", "")))
            problems += 1
        try:
            dt.date.fromisoformat(data.get("date", ""))
        except (TypeError, ValueError):
            result.fail("%s has invalid ADR date" % path.name)
            problems += 1
        if data.get("status") == "superseded" and not data.get("superseded_by"):
            result.fail("%s is superseded without superseded_by" % path.name)
            problems += 1
        for heading in ADR_HEADINGS:
            if heading not in text:
                result.fail("%s lacks ADR section %s" % (path.name, heading))
                problems += 1
    if not problems:
        count = len([p for p in directory.glob("*.md")
                     if p.name != "README.md"])
        result.ok("ADR structure valid (%d decisions)" % count)


def validate_legacy_markers(result: Result) -> None:
    bad = []
    for path in LEGACY_DOCS:
        text = read(path, result)
        if "superseded" not in "\n".join(text.splitlines()[:20]).lower():
            bad.append(str(path.relative_to(ROOT)))
    if bad:
        for path in bad:
            result.fail("legacy entry is not marked superseded: %s" % path)
    else:
        result.ok("Legacy desktop specifications are marked superseded")


def now_metadata(result: Result, reference: str) -> Dict[str, str]:
    """The local handoff's frontmatter, or {} when there is no handoff.

    NOW.md is the maintainer's untracked workbench note, so a fresh clone
    legitimately has none -- and an outside contributor's preflight must not
    fail over a file the repository deliberately does not ship. Absence is
    simply "no active handoff". A NOW.md that exists is still validated as
    strictly as ever: a broken one on the maintainer's own machine is a lie
    waiting to be believed.
    """
    path = PROJECT / "NOW.md"
    if not path.exists():
        result.ok("NOW.md absent: no active handoff (local, untracked file)")
        return {}
    data, _ = frontmatter(read(path, result))
    for key in ("last_reconciled", "verified_sha", "reference_branch",
                "valid_until"):
        if not data.get(key):
            result.fail("NOW.md lacks frontmatter field %s" % key)
    if data.get("verified_sha") and not SHA.match(data["verified_sha"]):
        result.fail("NOW.md verified_sha is not a full commit SHA")
    if (reference and data.get("reference_branch")
            and data["reference_branch"] != reference):
        result.fail("NOW.md cannot override canonical reference %s"
                    % reference)
    timestamps: Dict[str, Optional[dt.datetime]] = {}
    for key in ("last_reconciled", "valid_until"):
        timestamps[key] = instant(data[key]) if data.get(key) else None
        if data.get(key) and timestamps[key] is None:
            result.fail("NOW.md %s is not a timezone-aware ISO timestamp" % key)
    reconciled = timestamps.get("last_reconciled")
    expiry = timestamps.get("valid_until")
    if reconciled is not None and expiry is not None:
        lifetime = expiry - reconciled
        if lifetime <= dt.timedelta(0):
            result.fail("NOW.md valid_until must follow last_reconciled")
        elif lifetime > MAX_NOW_LIFETIME:
            result.fail("NOW.md validity window exceeds seven days")
    return data


def validate_now(data: Dict[str, str], reference: str, result: Result,
                 *, fetched: bool) -> None:
    if result.errors:
        return
    if not data:
        return                    # no handoff file: nothing to validate
    verified = data["verified_sha"]
    code, reference_sha = git("rev-parse", "--verify", reference + "^{commit}")
    if code != 0 or not SHA.match(reference_sha):
        result.fail("canonical reference is unavailable: %s" % reference)
        return
    code, _ = git("cat-file", "-e", verified + "^{commit}")
    if code != 0:
        result.warn("NOW.md verified commit is unavailable - IGNORE NOW.md")
        return
    expiry = instant(data["valid_until"])
    assert expiry is not None
    current = dt.datetime.now(dt.timezone.utc)
    if expiry.astimezone(dt.timezone.utc) <= current:
        result.warn("NOW.md expired - IGNORE NOW.md")
        return
    relationship = "matches"
    if reference_sha != verified:
        code, _ = git("merge-base", "--is-ancestor", verified, reference_sha)
        if code != 0:
            result.warn(
                "NOW.md SHA is not an ancestor of the reference - IGNORE NOW.md")
            return
        relationship = "verified SHA is an ancestor of"
    if not fetched:
        result.warn(
            "NOW.md ancestry is valid against cached reference only; "
            "remote was not fetched")
        return
    result.ok("NOW.md valid: %s fetched %s" % (relationship, reference))


def fetch_reference(reference: str, result: Result, *, offline: bool) -> bool:
    if offline:
        result.warn("Git reference not refreshed in offline mode")
        return False
    remote = reference.split("/", 1)[0]
    code, output = git("fetch", "--prune", remote, timeout=45)
    if code != 0:
        result.warn("could not fetch %s; NOW.md will be ignored (%s)"
                    % (remote, output or "unknown error"))
        return False
    result.ok("Git reference fetched from %s" % remote)
    return True


def git_status(reference: str, result: Result) -> None:
    code, root = git("rev-parse", "--show-toplevel")
    if code != 0 or Path(root).resolve() != ROOT:
        result.fail("script is not running in its Git repository")
        return
    code, branch = git("branch", "--show-current")
    branch = branch if code == 0 and branch else "detached HEAD"
    code, dirty = git("status", "--porcelain=v1")
    if code != 0:
        result.fail("cannot inspect Git worktree status")
    elif dirty:
        result.warn("Git worktree has changes on %s" % branch)
    else:
        result.ok("Git worktree clean on %s" % branch)
    code, counts = git(
        "rev-list", "--left-right", "--count",
        "HEAD..." + reference)
    if code != 0:
        result.fail("cannot compare HEAD with %s" % reference)
        return
    try:
        ahead, behind = (int(value) for value in counts.split())
    except (TypeError, ValueError):
        result.fail("unexpected Git comparison output for %s"
                    % reference)
        return
    if ahead == 0 and behind == 0:
        result.ok("HEAD matches %s" % reference)
    elif ahead and behind:
        result.warn("HEAD has diverged from %s (%d ahead, %d behind)"
                    % (reference, ahead, behind))
    elif ahead:
        result.warn("HEAD is %d commit(s) ahead of %s"
                    % (ahead, reference))
    else:
        result.warn("HEAD is %d commit(s) behind %s"
                    % (behind, reference))


def validate_ops_context(result: Result) -> None:
    """Check the optional pointer without reading or printing private data."""
    raw = os.environ.get("HAIKODE_OPS_CONTEXT", "").strip()
    if not raw:
        result.ok("External operations context not loaded (pointer unset)")
        return
    path = Path(raw).expanduser()
    if path.is_file() and os.access(str(path), os.R_OK):
        result.ok("External operations context registered but not loaded")
    else:
        result.warn("HAIKODE_OPS_CONTEXT is set but unreadable")


def print_result(result: Result, *, integrity_only: bool) -> int:
    for item in result.okays:
        print("[ok]   " + item)
    for item in result.warnings:
        print("[warn] " + item)
    for item in result.errors:
        print("[fail] " + item)
    print()
    if result.errors:
        print("PROJECT CONTEXT NOT TRUSTED")
        return 1
    if integrity_only:
        print("PROJECT CONTEXT STRUCTURE VALID")
    else:
        print("PROJECT CONTEXT READY")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true",
        help="do not fetch; validate NOW only against the cached reference")
    parser.add_argument(
        "--integrity-only", action="store_true",
        help="validate files, adapters, links and ADRs without Git/NOW state")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = Result()
    validate_required(result)
    validate_adapters(result)
    validate_links(result)
    validate_adrs(result)
    validate_legacy_markers(result)
    reference = canonical_reference(result)
    data = now_metadata(result, reference)
    if not args.integrity_only and not result.errors:
        fetched = fetch_reference(reference, result, offline=args.offline)
        validate_now(data, reference, result, fetched=fetched)
        git_status(reference, result)
        validate_ops_context(result)
    return print_result(result, integrity_only=args.integrity_only)


if __name__ == "__main__":
    sys.exit(main())
