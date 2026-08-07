import contextlib
import json
import copy
import os
import shutil
import stat
import subprocess
import sys
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set

try:
    import fcntl
except ImportError:  # non-POSIX; the lock falls back to an O_EXCL sentinel
    fcntl = None

DEFAULT_CONFIG = {
    "default_provider": "ollama",
    "providers": {
        "ollama": {
            "dialect": "openai",
            "base_url": "https://ollama.com/v1",
            "key_env": "OLLAMA_API_KEY",
            "model": "gpt-oss:120b",
            "context": 128000,
        },
        "zen": {
            # OpenCode Zen free tier — works without a personal key ("public").
            # Handy for testing the pipeline at zero cost: haikode -p zen "..."
            # The free line-up rotates (hy3-free was retired); `/models zen`
            # lists what is currently offered if this default stops resolving.
            "dialect": "openai",
            "base_url": "https://opencode.ai/zen/v1",
            "api_key": "public",
            "model": "deepseek-v4-flash-free",
            "context": 190000,
        },
        "ollama-local": {
            # Ollama's OpenAI-compatible endpoint. Change this to the LAN or
            # Tailscale address of the machine running Ollama, for example
            # http://192.168.1.20:11434/v1 or http://host.tailnet.ts.net:11434/v1.
            "dialect": "openai",
            "base_url": "http://127.0.0.1:11434/v1",
            "requires_key": False,
            "model": "qwen3-coder",
            "context": 32768,
        },
        "chatgpt": {
            # Standalone device OAuth and Responses API; no OpenCode server.
            "dialect": "chatgpt",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "oauth_provider": "chatgpt",
            "requires_key": False,
            "model": "gpt-5.4",
            "context": 200000,
            "reasoning_effort": "medium",
        },
        "supergrok": {
            # Standalone RFC 8628 device OAuth against xAI.
            "dialect": "supergrok",
            "base_url": "https://api.x.ai/v1",
            "oauth_provider": "supergrok",
            "requires_key": False,
            "model": "grok-4",
            "context": 131072,
        },
        "xai": {
            "dialect": "openai",
            "base_url": "https://api.x.ai/v1",
            "key_env": "XAI_API_KEY",
            "model": "grok-4",
            "context": 131072,
        },
        "anthropic": {
            "dialect": "anthropic",
            "base_url": "https://api.anthropic.com",
            "key_env": "ANTHROPIC_API_KEY",
            "model": "claude-sonnet-5",
            "context": 200000,
        },
        "openai": {
            "dialect": "openai",
            "base_url": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
            "context": 128000,
        },
    },
    # No global wall: an agent iterates until it finishes or the user stops it.
    # A positive value opts into a per-turn cost/safety budget.
    "max_steps": None,
}

KEYSTORE_TIMEOUT = 5  # seconds; first access may show a GUI approval dialog on Haiku
KEYSTORE_USAGE_EXIT = 2  # helper exit code for "I do not know that verb"

# The helper BINARY is still called "hai-keystore" and must stay that way.
# Haiku's keystore_server grants keyring access per (app signature, binary
# path). Renaming the binary — or its signature — invalidates the existing
# grant, which re-triggers the "Application keyring access" dialog on the
# machine's PHYSICAL screen and orphans every key stored by the old binary.
# Only the *identifier namespace* below was renamed, because that is data we
# can migrate in software. Do not "fix" this to hai<something>-keystore.
KEYSTORE_BIN = "hai-keystore"

# Secrets live under "haikode:<provider>"; "hai:<provider>" is the pre-rename
# namespace we transparently upgrade from on first read.
KEYSTORE_NAMESPACE = "haikode"
LEGACY_KEYSTORE_NAMESPACE = "hai"


def _without_defaults(data: Any, defaults: Any) -> Any:
    """`data` minus every key it shares, unchanged, with `defaults`.

    The inverse of deep_merge for the round trip load -> edit -> save: what
    comes back is the user's own choices, so the defaults stay live and a
    later release can still move them. A key the user set to the default
    value is dropped, which changes nothing about how it resolves.
    """
    if not isinstance(data, dict) or not isinstance(defaults, dict):
        return data
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in defaults:
            out[key] = value
            continue
        default = defaults[key]
        if isinstance(value, dict) and isinstance(default, dict):
            pruned = _without_defaults(value, default)
            if pruned:
                out[key] = pruned
        elif value != default:
            out[key] = value
    return out


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _keystore_bin() -> Optional[str]:
    """Path to the native hai-keystore helper (Haiku BKeyStore), if available."""
    if os.environ.get("HAI_DISABLE_KEYSTORE") == "1":
        return None
    path = shutil.which(KEYSTORE_BIN)
    if path:
        return path
    candidate = os.path.expanduser(f"~/config/non-packaged/bin/{KEYSTORE_BIN}")
    if os.path.exists(candidate):
        return candidate
    return None


def _keystore_call(args: List[str], input_text: Optional[str] = None):
    """Run the helper. Returns the CompletedProcess, or None if unavailable.

    Callers must never put a secret in `args`: argv is world-readable through
    `ps`, so anything passed there leaks to every user on the machine.
    """
    bin_path = _keystore_bin()
    if not bin_path:
        return None
    # input= and stdin= are mutually exclusive; verbs that read nothing get a
    # closed stdin so the helper can never block on an inherited terminal.
    stdin_kwargs = ({"input": input_text} if input_text is not None
                    else {"stdin": subprocess.DEVNULL})
    try:
        return subprocess.run(
            [bin_path] + args,
            capture_output=True,
            text=True,
            timeout=KEYSTORE_TIMEOUT,
            **stdin_kwargs,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _keystore_run(args, input_text=None) -> Optional[str]:
    result = _keystore_call(args, input_text)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def _keystore_set(identifier: str, secret: str) -> bool:
    """Store a secret, handing it to the helper on stdin. True when stored.

    `hai-keystore set <id> <secret>` put the key in argv, where any user could
    read it out of `ps`, so it is never used any more. A helper too old to
    know `set-stdin` is refused rather than fed on the command line: the
    caller then falls back to the mode-0600 config file, which is private.
    """
    result = _keystore_call(["set-stdin", identifier], input_text=secret)
    if result is None:
        return False
    if result.returncode == 0:
        register_secret(secret)
        return True
    if result.returncode == KEYSTORE_USAGE_EXIT:
        print("[config] Warning: this hai-keystore cannot take a secret on "
              "stdin. Rebuild and reinstall it from tools/hai-keystore; the "
              "key will not be passed on the command line, where `ps` would "
              "expose it.", file=sys.stderr)
    return False


def _keystore_get(provider_name: str) -> str:
    """Read a secret, upgrading a pre-rename "hai:<provider>" entry if found.

    The legacy entry is left in place on purpose: re-saving can fail (locked
    keyring, denied dialog, helper too old for `set-stdin`) and we must never
    be the reason a user loses the only copy of a key.
    """
    secret = _keystore_run(["get", f"{KEYSTORE_NAMESPACE}:{provider_name}"])
    if secret:
        register_secret(secret)
        return secret
    legacy = _keystore_run(["get", f"{LEGACY_KEYSTORE_NAMESPACE}:{provider_name}"])
    if not legacy:
        return ""
    register_secret(legacy)
    _keystore_set(f"{KEYSTORE_NAMESPACE}:{provider_name}", legacy)
    return legacy


def default_config_path() -> Path:
    """Haiku-native settings location, with an XDG fallback elsewhere."""
    if os.name == "posix" and os.path.exists("/boot/home"):
        return Path(os.path.expanduser("~/config/settings/haikode/config.json"))
    return Path(os.path.expanduser("~/.config/haikode/config.json"))


# --- durable, private persistence ----------------------------------------
# Everything under the settings directory is a credential or points at one, so
# writes must be atomic (a crash must not empty the file), private from the
# first byte (never a window where the file is world-readable), and serialised
# (two providers refreshing at once must not overwrite each other).

LOCK_TIMEOUT = 5.0  # seconds to wait for another process before giving up
STALE_LOCK_SECONDS = 60.0  # sentinel left by a killed process is taken over


def ensure_private_dir(directory: Path) -> None:
    """Create/tighten a settings directory to 0700.

    Existing installs were created with the umask default (usually 0755), so
    tightening has to cover directories we did not just create. The user's
    home and the working directory are left alone: a bare relative config path
    resolves its parent to ".", and locking that down would break unrelated
    tools.
    """
    directory = Path(directory)
    existed = directory.is_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        if not existed:
            os.chmod(directory, 0o700)  # makedirs' mode is filtered by umask
            return
        resolved = directory.resolve()
        if resolved in (Path.home(), Path.cwd()) or resolved == Path(resolved.anchor):
            return
        if stat.S_IMODE(os.stat(directory).st_mode) & 0o077:
            os.chmod(directory, 0o700)
    except OSError as exc:
        print(f"[config] Warning: could not secure {directory}: {exc}",
              file=sys.stderr)


def _fsync_dir(directory: Path) -> None:
    """Flush the directory entry so the rename itself survives a power cut."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return  # not every filesystem lets you open a directory
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def secure_write_json(path: Path, value: Any) -> None:
    """Write JSON atomically and privately.

    mkstemp gives a unique O_EXCL file created 0600, so the content is never
    visible to other users and two writers cannot share a temp name. fsync
    then os.replace means a crash leaves either the old file or the new one —
    never the truncated file that writing in place produced.
    """
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        # Includes KeyboardInterrupt and unserialisable data: leave the
        # previous file untouched and take the half-written temp with us.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


@contextlib.contextmanager
def settings_lock(path: Path) -> Iterator[None]:
    """Serialise read-modify-write of `path` across processes.

    flock() where the platform has it (Haiku and macOS both do), otherwise an
    O_EXCL sentinel with stale-lock takeover so a killed process cannot wedge
    the CLI forever. Waiting is bounded: the write is atomic either way, so
    proceeding late is better than hanging a user's terminal.
    """
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    ensure_private_dir(path.parent)
    deadline = time.monotonic() + LOCK_TIMEOUT

    if fcntl is not None:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        print(f"[config] Warning: timed out waiting for "
                              f"{lock_path}; writing anyway", file=sys.stderr)
                        break
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        return

    acquired = False
    while True:
        try:
            os.close(os.open(str(lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            acquired = True
            break
        except FileExistsError:
            if _lock_is_stale(lock_path):
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                print(f"[config] Warning: timed out waiting for {lock_path}; "
                      "writing anyway", file=sys.stderr)
                break
            time.sleep(0.02)
        except OSError:
            break  # unwritable directory: the caller's write will report it
    try:
        yield
    finally:
        if acquired:
            try:
                os.unlink(lock_path)
            except OSError:
                pass


def _copy_private(source: Path, target: Path) -> None:
    """Copy a settings file so it is mode 0600 from its very first byte.

    shutil.copyfile would create it with the umask default first and chmod
    afterwards, leaving a window in which another user can read the copy.
    """
    with open(source, "rb") as handle:
        data = handle.read()
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        return (time.time() - os.stat(lock_path).st_mtime) > STALE_LOCK_SECONDS
    except OSError:
        return False


# --- credential redaction -------------------------------------------------
# The rules themselves live in haikode/redact.py so that tools can import them
# without dragging in the whole config module (and its file I/O). Re-exported
# here because the redactor grew out of this file and the older
# `from .config import redact` call sites are still valid.
from .redact import (MIN_SECRET_LENGTH, REDACTED,  # noqa: E402,F401
                     credential_env_names, redact, register_config_secrets,
                     register_secret, reset_redaction_cache, scrub_env)


class Config:
    def __init__(self, path: str = None):
        self.path = Path(path) if path is not None else default_config_path()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self._migrate_config_dir()
        self._load()

    def _legacy_dir(self) -> Optional[Path]:
        """The pre-rename sibling of the settings directory, i.e. .../hai/."""
        parent = self.path.parent
        if parent.name != "haikode":
            return None
        return parent.parent / "hai"

    def _migrate_config_dir(self):
        """Copy pre-rename settings into the haikode directory, once.

        oauth.json must move together with config.json: OAuthStore derives its
        path from config.path.parent, so migrating only the config would sign
        every subscription provider out and force another device-code dance on
        the machine's physical screen.

        Each file is considered independently, so an install that already has a
        config.json but no oauth.json still recovers its tokens. The old files
        are kept so downgrading, or an older installed build, still finds its
        settings; copy rather than move for the same reason.
        """
        legacy_dir = self._legacy_dir()
        if legacy_dir is None:
            return
        # Device-authorization "pending" files are deliberately not migrated:
        # they expire within minutes, so a stale one is worse than none.
        for name in (self.path.name, "oauth.json"):
            target = self.path.parent / name
            legacy = legacy_dir / name
            if target.exists() or not legacy.is_file():
                continue
            try:
                ensure_private_dir(target.parent)
                _copy_private(legacy, target)
            except OSError as e:
                print(f"[config] Warning: could not migrate {legacy}: {e}",
                      file=sys.stderr)
                continue
            print(f"[config] Migrated {legacy} -> {target} (original kept)",
                  file=sys.stderr)

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    user = json.load(f)
                    self.data = deep_merge(self.data, user)
            except Exception as e:
                print(f"[config] Warning: could not load {self.path}: {e}",
                      file=sys.stderr)
        if self._migrate_standalone_oauth():
            self.save()

    def reload(self) -> bool:
        """Atomically re-read settings so a bad edit cannot erase live state.

        A Config is otherwise an intentional process snapshot. Front-ends call
        this only for an explicit reload, then rebuild the agent while retaining
        its conversation.
        """
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as handle:
                    user = json.load(handle)
                if not isinstance(user, dict):
                    raise ValueError("top level must be a JSON object")
                candidate = deep_merge(candidate, user)
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(f"could not reload {self.path}: {exc}") from exc
        changed = candidate != self.data
        previous = self.data
        self.data = candidate
        try:
            if self._migrate_standalone_oauth():
                self.save()
        except Exception:
            self.data = previous
            raise
        return changed

    def _migrate_standalone_oauth(self) -> bool:
        """Replace the former SSH/OpenCode subscription profiles in place."""
        changed = False
        providers = self.data.setdefault("providers", {})
        replacements = {
            "chatgpt": copy.deepcopy(DEFAULT_CONFIG["providers"]["chatgpt"]),
            "supergrok": copy.deepcopy(DEFAULT_CONFIG["providers"]["supergrok"]),
        }
        for name, replacement in replacements.items():
            current = providers.get(name, {})
            if current.get("dialect") == "opencode" or not current:
                providers[name] = replacement
                changed = True

        legacy = providers.get("opencode", {})
        if (legacy.get("dialect") == "opencode"
                and legacy.get("base_url", "").rstrip("/")
                == "http://127.0.0.1:4096"):
            del providers["opencode"]
            changed = True
            if self.data.get("default_provider") == "opencode":
                self.data["default_provider"] = "ollama"
        return changed

    def get_provider(self, name: str = None) -> Dict[str, Any]:
        name = name or self.data["default_provider"]
        prov = self.data["providers"].get(name, {})
        if not prov:
            # fallback to first available
            prov = next(iter(self.data["providers"].values()), {})
        return prov

    def get_api_key(self, provider_name: str) -> str:
        """Key lookup order: native keystore (Haiku) → config file → env var."""
        prov = self.data["providers"].get(provider_name, {})
        if prov.get("oauth_provider"):
            return ""  # subscription credentials live in the local OAuth store
        if not prov.get("requires_key", True):
            return ""  # e.g. Ollama on a trusted LAN/Tailscale endpoint

        secret = _keystore_get(provider_name)
        if secret:
            return secret

        if prov.get("api_key"):
            # Registered so redact() can mask it if a tool ever echoes it.
            register_secret(prov["api_key"])
            return prov["api_key"]

        env = prov.get("key_env", "")
        secret = os.environ.get(env, "") if env else ""
        register_secret(secret)
        return secret

    def key_source(self, provider_name: str) -> str:
        """Where the key would come from: keystore | config | env | none | n/a."""
        prov = self.data["providers"].get(provider_name, {})
        if prov.get("oauth_provider"):
            from .oauth import OAuthStore
            return OAuthStore.for_config(self).status(provider_name)
        if not prov.get("requires_key", True):
            return "n/a"
        if _keystore_get(provider_name):
            return "keystore"
        if prov.get("api_key"):
            return "config"
        env = prov.get("key_env", "")
        if env and os.environ.get(env):
            return "env"
        return "none"

    def set_api_key(self, provider_name: str, key: str) -> str:
        """Store a key; prefers the native keystore, falls back to the config file.
        Returns "keystore" or "config" depending on where it was stored."""
        if provider_name not in self.data["providers"]:
            raise KeyError(f"Unknown provider: {provider_name}")
        # No pre-emptive "remove": the helper replaces an existing entry
        # itself, and removing first would destroy the stored key if the
        # store then refused the new one.
        if _keystore_set(f"{KEYSTORE_NAMESPACE}:{provider_name}", key):
            return "keystore"
        register_secret(key)
        self.data["providers"][provider_name]["api_key"] = key
        self.save()
        return "config"

    def add_provider(self, name: str, dialect: str, base_url: str,
                     model: str = "", requires_key: bool = True,
                     directory: str = "", oauth_provider: str = "",
                     oauth_method: int = 1):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
            raise ValueError(
                "provider name must use 1-64 letters, digits, '.', '_' or '-'")
        if dialect not in ("openai", "anthropic", "gemini"):
            raise ValueError("dialect must be openai, anthropic or gemini")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError("base URL must start with http:// or https://")

        existing = self.data.get("providers", {}).get(name, {})
        provider = {
            "dialect": dialect,
            "base_url": base_url.rstrip("/"),
            "model": model,
            "context": 128000,
        }
        provider["requires_key"] = bool(requires_key)
        if existing.get("api_key"):
            provider["api_key"] = existing["api_key"]
        if existing.get("key_env"):
            provider["key_env"] = existing["key_env"]
        self.data.setdefault("providers", {})[name] = provider
        self.save()

    def remove_provider(self, name: str):
        providers = self.data.setdefault("providers", {})
        if name not in providers:
            raise KeyError(f"Unknown provider: {name}")
        self.clear_api_key(name)
        del providers[name]
        if self.data.get("default_provider") == name:
            self.data["default_provider"] = next(iter(providers), "")
        self.save()

    def set_default_provider(self, name: str):
        if name not in self.data.get("providers", {}):
            raise KeyError(f"Unknown provider: {name}")
        self.data["default_provider"] = name
        self.save()

    def clear_api_key(self, provider_name: str):
        if self.data["providers"].get(provider_name, {}).get("oauth_provider"):
            from .oauth import OAuthStore
            OAuthStore.for_config(self).remove(provider_name)
            return
        # Clear both namespaces, otherwise the legacy entry would be silently
        # resurrected by _keystore_get on the next read.
        _keystore_run(["remove", f"{KEYSTORE_NAMESPACE}:{provider_name}"])
        _keystore_run(["remove", f"{LEGACY_KEYSTORE_NAMESPACE}:{provider_name}"])
        if self.data["providers"].get(provider_name, {}).pop("api_key", None) is not None:
            self.save()

    def save(self):
        """Persist the config atomically, privately and one writer at a time.

        Only what differs from DEFAULT_CONFIG is written. `self.data` is the
        defaults merged with the user's file, so writing it whole would freeze
        every default into the file the first time anything saves — after
        which a shipped default can never reach that user again. That is not
        hypothetical: it is how a `max_steps` of 20 survived the change to no
        limit at all and kept cutting turns short.
        """
        with settings_lock(self.path):
            secure_write_json(self.path, _without_defaults(self.data,
                                                           DEFAULT_CONFIG))
