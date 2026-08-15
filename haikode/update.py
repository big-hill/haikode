"""Update checking and (where it is safe) one-confirmation updating.

The check asks GitHub's releases API for the newest tag and compares it
against the running version. It is quiet by design: no network at import,
a short timeout, and every failure — offline, private repository, rate
limit — degrades to "no update found" with the reason available to a
front end that asks. `update_check: false` in the config turns the
passive startup check off entirely.

Applying an update depends on how haikode got here:

* a git checkout (the developer install) fast-forwards itself, which IS
  the one-confirmation update;
* a packaged install downloads the right architecture's .hpkg, verifies the
  SHA-256 digest published by GitHub, and gives it to pkgman in non-interactive
  mode. The already-running process keeps its loaded code until it is closed.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import __version__

RELEASES_URL = ("https://api.github.com/repos/big-hill/haikode/"
                "releases/latest")
CHECK_TIMEOUT = 6.0
DOWNLOAD_TIMEOUT = 120.0
INSTALL_TIMEOUT = 300.0
UPDATE_DIR = Path("/tmp")
DOWNLOAD_CHUNK = 128 * 1024

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SHA256_RE = re.compile(r"sha256:([0-9a-fA-F]{64})\Z")


def parse_version(text: str) -> Tuple[int, int, int, int]:
    """(major, minor, micro, is_release) for ordering.

    A pre-release ("0.1.0-m0m1") sorts BELOW the plain release with the
    same numbers, matching how the hpkg versioning treats it.
    """
    match = _VERSION_RE.search(text or "")
    if match is None:
        return (0, 0, 0, 0)
    numbers = tuple(int(part) for part in match.groups())
    is_release = 0 if "-" in (text or "").split(match.group(0))[-1] else 1
    return (*numbers, is_release)


def _architecture() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("bepc", "x86", "i386", "i486", "i586", "i686"):
        return "x86_gcc2"
    return "x86_64"


def install_kind(package_file: str = "") -> str:
    """"package", "checkout" or "other" — decides how an update applies."""
    location = package_file or getattr(sys.modules.get("haikode"),
                                       "__file__", "") or ""
    if "vendor-packages" in location:
        return "package"
    root = Path(location).resolve().parent.parent if location else None
    if root is not None and (root / ".git").exists():
        return "checkout"
    return "other"


def checkout_root() -> Optional[Path]:
    location = getattr(sys.modules.get("haikode"), "__file__", "") or ""
    if not location:
        return None
    root = Path(location).resolve().parent.parent
    return root if (root / ".git").exists() else None


def check(fetch=None) -> Dict[str, Any]:
    """One quiet check. Returns a dict a front end can render directly.

    {available, current, latest, url, asset, digest, error} — `available` is
    only True when the latest release genuinely sorts above the running
    version and the response parsed.
    """
    result: Dict[str, Any] = {"available": False, "current": __version__,
                              "latest": "", "url": "", "asset": "",
                              "digest": "", "error": ""}
    try:
        if fetch is None:
            request = urllib.request.Request(
                RELEASES_URL, headers={"User-Agent": "haikode-update-check",
                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(request,
                                        timeout=CHECK_TIMEOUT) as reply:
                payload = json.loads(reply.read().decode("utf-8"))
        else:
            payload = fetch()
    except Exception as exc:
        result["error"] = str(exc)
        return result
    if not isinstance(payload, dict):
        result["error"] = "unexpected reply"
        return result
    latest = str(payload.get("tag_name") or "")
    result["latest"] = latest
    result["url"] = str(payload.get("html_url") or "")
    if parse_version(latest) <= parse_version(__version__):
        return result
    wanted = _architecture()
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.endswith(".hpkg") and wanted in name:
            result["asset"] = str(asset.get("browser_download_url") or "")
            result["digest"] = str(asset.get("digest") or "")
            break
    result["available"] = True
    return result


def _download_package(asset: str, expected_sha256: str) -> Path:
    """Download one HPKG to a private temporary file and verify its bytes."""
    asset_name = Path(urllib.parse.unquote(
        urllib.parse.urlparse(asset).path)).name
    if not asset_name.endswith(".hpkg"):
        raise ValueError("release asset is not an HPKG")

    directory = Path(tempfile.mkdtemp(prefix="haikode-update-",
                                      dir=str(UPDATE_DIR)))
    target = directory / asset_name
    digest = hashlib.sha256()
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            with urllib.request.urlopen(
                    asset, timeout=DOWNLOAD_TIMEOUT) as reply:
                while True:
                    chunk = reply.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise ValueError("checksum mismatch (expected %s, got %s)"
                             % (expected_sha256, actual))
        return target
    except BaseException:
        _remove_download(target)
        raise


def _remove_download(target: Path) -> None:
    try:
        target.unlink()
    except OSError:
        pass
    try:
        target.parent.rmdir()
    except OSError:
        pass


def _process_error(completed: subprocess.CompletedProcess) -> str:
    detail = str(completed.stderr or completed.stdout or "").strip()
    if len(detail) > 1200:
        detail = detail[-1200:]
    return detail or "pkgman exited with status %s" % completed.returncode


def apply_update(state: Dict[str, Any]) -> str:
    """Do what can safely be done; return the message for the user."""
    if not state.get("available"):
        return "haikode is up to date (%s)." % __version__
    kind = install_kind()
    if kind == "checkout":
        root = checkout_root()
        if root is None:
            return "checkout not found; update by hand with git pull."
        pull = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        if pull.returncode != 0:
            return ("git pull failed: %s"
                    % (pull.stderr.strip() or pull.stdout.strip()))
        return ("updated the checkout to %s - restart haikode to run it."
                % state.get("latest"))
    asset = str(state.get("asset") or "")
    if kind == "package":
        if not asset:
            return ("no package for this architecture was attached to %s: %s"
                    % (state.get("latest"),
                       state.get("url") or "see the releases"))
        raw_digest = str(state.get("digest") or "").strip()
        digest_match = _SHA256_RE.fullmatch(raw_digest)
        if digest_match is None:
            if not raw_digest:
                return ("release %s has no SHA-256 digest; it was not "
                        "installed automatically."
                        % state.get("latest"))
            return ("release %s has an invalid SHA-256 digest; it was not "
                    "installed automatically."
                    % state.get("latest"))
        pkgman = shutil.which("pkgman")
        if not pkgman:
            return ("pkgman was not found; install %s from %s"
                    % (state.get("latest"),
                       state.get("url") or "the releases page"))
        try:
            target = _download_package(asset,
                                       digest_match.group(1).lower())
        except Exception as exc:
            return "download failed: %s" % exc
        try:
            installed = subprocess.run(
                [pkgman, "install", "-y", str(target)],
                capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ("package install timed out; the verified package remains "
                    "at %s" % target)
        except OSError as exc:
            return ("package install failed: %s; the verified package remains "
                    "at %s" % (exc, target))
        if installed.returncode != 0:
            return ("package install failed: %s; the verified package remains "
                    "at %s" % (_process_error(installed), target))
        _remove_download(target)
        return ("installed haikode %s. Close and reopen haikode to use the "
                "new version." % state.get("latest"))
    return ("update %s is available: %s"
            % (state.get("latest"), state.get("url") or "see the releases"))


def startup_notice(config_data: Dict[str, Any], fetch=None) -> str:
    """The passive one-liner for session start, or "".

    Off with `update_check: false`; silent on every failure — a startup
    must never stall or nag about the network.
    """
    if (config_data or {}).get("update_check") is False:
        return ""
    state = check(fetch=fetch)
    if not state.get("available"):
        return ""
    return ("haikode %s is available (running %s) - /update to install it"
            % (state.get("latest"), __version__))


__all__ = ["apply_update", "check", "checkout_root", "install_kind",
           "parse_version", "startup_notice"]
