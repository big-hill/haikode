"""Standalone subscription OAuth for Haiku.

This module mirrors the public device-code flows implemented by OpenCode's
built-in OpenAI Codex and xAI plugins, but runs entirely inside haikode's
Python process.  It deliberately has no OpenCode server, Bun, Node, or SSH
runtime dependency.

OAuth tokens are stored in a separate mode-0600 JSON file.  API keys continue
to use Haiku BKeyStore through :mod:`haikode.config`.
"""
import base64
import errno
import hashlib
import json
import resource
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .config import register_secret, secure_write_json, settings_lock
from .net import USER_AGENT


CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_ISSUER = "https://auth.openai.com"
CHATGPT_DEVICE_URL = f"{CHATGPT_ISSUER}/codex/device"
CHATGPT_API_BASE = "https://chatgpt.com/backend-api/codex"

# Public Grok CLI OAuth client used by OpenCode's built-in xAI plugin.
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_ISSUER = "https://auth.x.ai"
XAI_DEVICE_AUTHORIZATION_URL = f"{XAI_ISSUER}/oauth2/device/code"
XAI_TOKEN_URL = f"{XAI_ISSUER}/oauth2/token"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Public Claude Code OAuth client, the same one OpenCode's anthropic plugin
# uses. Claude subscription sign-in is not a device flow: it is PKCE
# authorization code where claude.ai displays the resulting code for the user
# to carry back by hand (`code=true`), so there is nothing to poll.
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
CLAUDE_SCOPE = "org:create_api_key user:profile user:inference"
# Every API request made with a subscription token must carry this beta
# header; without it the token is rejected as if it were a bad API key.
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"

POLL_SAFETY_SECONDS = 3
REFRESH_SKEW_SECONDS = 120
# Where a failed credential read leaves its trace, beside the file itself.
CREDENTIAL_LOG = "credential-reads.log"


class OAuthError(RuntimeError):
    pass


def _json_bytes(value: Dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _http_json(request: urllib.request.Request, timeout: int = 30,
               opener: Callable = urllib.request.urlopen) -> Tuple[int, Dict[str, Any]]:
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {"error_description": raw.decode("utf-8", errors="replace")[:500]}
    return int(status), body if isinstance(body, dict) else {}


def _post_json(url: str, value: Dict[str, Any], timeout: int = 30,
               opener: Callable = urllib.request.urlopen) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(
        url, data=_json_bytes(value), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    return _http_json(request, timeout, opener)


def _post_form(url: str, value: Dict[str, Any], timeout: int = 30,
               opener: Callable = urllib.request.urlopen) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(value).encode("utf-8"), method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
    return _http_json(request, timeout, opener)


def _error_detail(body: Dict[str, Any]) -> str:
    return str(body.get("error_description") or body.get("error") or "").strip()


def _jwt_claims(token: str) -> Dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def extract_chatgpt_account_id(tokens: Dict[str, Any]) -> str:
    for key in ("id_token", "access_token"):
        claims = _jwt_claims(str(tokens.get(key, "")))
        auth = claims.get("https://api.openai.com/auth", {})
        candidates = [
            claims.get("chatgpt_account_id"),
            auth.get("chatgpt_account_id") if isinstance(auth, dict) else None,
        ]
        organizations = claims.get("organizations", [])
        if isinstance(organizations, list) and organizations:
            first = organizations[0]
            if isinstance(first, dict):
                candidates.append(first.get("id"))
        for candidate in candidates:
            if candidate:
                return str(candidate)
    return ""


def _describe_read_failure(exc: BaseException) -> str:
    """The failure with its errno kept — the part the old code threw away."""
    number = getattr(exc, "errno", None)
    if number is None:
        return "%s: %s" % (type(exc).__name__, exc)
    return "%s [errno %d %s]: %s" % (type(exc).__name__, number,
                                     errno.errorcode.get(number, "?"), exc)


def _record_read_failure(path: Path, attempt: int, exc: BaseException) -> None:
    """Append one line about a failed credential read, then get out of the way.

    Written to a file opened per call rather than held open, and every failure
    here is swallowed: this is diagnostics, and diagnostics that can break a
    login are worse than none. Identity of the file is recorded (device,
    inode, size, mtime) because a rename, a symlink swap or a replaced parent
    directory would all look identical from the error alone — and the file's
    own mtime was what ruled out the first theory. No token material is ever
    written: only shape.
    """
    try:
        line = {
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "attempt": attempt + 1,
            "error": _describe_read_failure(exc),
            "path": str(path),
        }
        for label, target in (("file", path), ("parent", path.parent)):
            try:
                info = os.stat(str(target))
                line[label] = {"dev": info.st_dev, "ino": info.st_ino,
                               "size": info.st_size,
                               "mtime_ns": info.st_mtime_ns,
                               "ctime_ns": info.st_ctime_ns}
            except OSError as stat_exc:
                line[label] = {"stat_failed": _describe_read_failure(stat_exc)}
        line["fds_open"] = _open_descriptor_count()
        with open(str(path.parent / CREDENTIAL_LOG), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except Exception:
        return


def _open_descriptor_count() -> int:
    """Descriptors this process holds, without allocating one to find out.

    os.fstat on each number in turn: socket.fromfd() would duplicate the
    descriptor and so change the very thing being measured, and Haiku has no
    /proc to read instead. Returns -1 if even this fails.
    """
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = int(soft) if 0 < soft < 65536 else 4096
        count = 0
        for number in range(limit):
            try:
                os.fstat(number)
                count += 1
            except OSError:
                continue
        return count
    except Exception:
        return -1


class OAuthStore:
    def __init__(self, path: str):
        self.path = Path(path)
        # Why the last _read() came back empty: "" when the file simply is
        # not there, otherwise the failure. Read by access_token().
        self.read_error = ""

    @classmethod
    def for_config(cls, config) -> "OAuthStore":
        return cls(str(config.path.parent / "oauth.json"))

    def _read(self) -> Dict[str, Any]:
        """The token file, or {} — with `read_error` saying which.

        A file that cannot be read is not the same thing as a user who never
        signed in, and conflating the two produced "Not signed in to chatgpt"
        in the middle of a working session with valid tokens on disk.

        Every failed attempt is recorded even when a later one succeeds. The
        one field report of this cost us the answer: the code caught the
        exception, dropped its errno and returned {}, so afterwards there was
        no way to tell EMFILE from EIO from ENOENT — and those point at
        completely different causes. A read that recovers on the second try is
        exactly the evidence worth keeping, because it is the only trace the
        transient failure leaves.

        There is deliberately no exists() check in front: it is a second
        syscall whose own failure would slip past unrecorded, and the file can
        change between the two calls. Open once; FileNotFoundError is the
        answer to "is it there".
        """
        last = ""
        for attempt in range(3):
            try:
                with self.path.open(encoding="utf-8") as handle:
                    value = json.load(handle)
                self.read_error = ""
                if not isinstance(value, dict):
                    self.read_error = ("malformed: top level is %s, not an "
                                       "object" % type(value).__name__)
                    return {}
                return value
            except FileNotFoundError:
                self.read_error = ""       # genuinely never signed in
                return {}
            except (OSError, json.JSONDecodeError,
                    UnicodeDecodeError) as exc:
                last = _describe_read_failure(exc)
                _record_read_failure(self.path, attempt, exc)
                if attempt < 2:
                    time.sleep(0.05)
        self.read_error = last
        return {}

    def _write(self, value: Dict[str, Any]):
        """Caller must hold settings_lock(self.path)."""
        secure_write_json(self.path, value)

    def get(self, provider: str) -> Dict[str, Any]:
        value = self._read().get(provider, {})
        if not isinstance(value, dict):
            return {}
        for field in ("access", "refresh"):
            register_secret(str(value.get(field, "")))
        return dict(value)

    def _set_locked(self, provider: str, tokens: Dict[str, Any]):
        """Merge one provider into the file. Caller holds the lock."""
        value = self._read()
        value[provider] = dict(tokens)
        self._write(value)

    def set(self, provider: str, tokens: Dict[str, Any]):
        # The read and the write must be one critical section: two providers
        # refreshing at the same time would otherwise each write back the file
        # they read, and the loser's tokens would vanish.
        with settings_lock(self.path):
            self._set_locked(provider, tokens)

    def remove(self, provider: str):
        with settings_lock(self.path):
            value = self._read()
            if provider in value:
                del value[provider]
                self._write(value)

    def status(self, provider: str) -> str:
        tokens = self.get(provider)
        return "oauth" if tokens.get("refresh") or tokens.get("access") else "none"

    def pending_path(self, provider: str) -> Path:
        safe = "".join(c for c in provider if c.isalnum() or c in "-_")
        return self.path.with_name(f"oauth-{safe}.pending.json")

    def save_pending(self, provider: str, pending: Dict[str, Any]):
        # A device code is a bearer credential until it is redeemed, so it gets
        # the same 0600-from-creation treatment as the tokens themselves.
        secure_write_json(self.pending_path(provider), pending)

    def load_pending(self, provider: str) -> Dict[str, Any]:
        path = self.pending_path(provider)
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def clear_pending(self, provider: str):
        try:
            self.pending_path(provider).unlink()
        except FileNotFoundError:
            pass


def begin_device_authorization(provider: str,
                               opener: Callable = urllib.request.urlopen) -> Dict[str, Any]:
    now = int(time.time())
    if provider == "chatgpt":
        status, body = _post_json(
            f"{CHATGPT_ISSUER}/api/accounts/deviceauth/usercode",
            {"client_id": CHATGPT_CLIENT_ID}, opener=opener)
        if status // 100 != 2:
            raise OAuthError(
                f"ChatGPT device authorization failed ({status}): {_error_detail(body)}")
        required = ("device_auth_id", "user_code")
        if any(not body.get(key) for key in required):
            raise OAuthError("ChatGPT device response is missing an ID or user code")
        interval = max(int(body.get("interval") or 5), 1)
        return {
            "provider": provider,
            "device_auth_id": body["device_auth_id"],
            "user_code": body["user_code"],
            "verification_uri": CHATGPT_DEVICE_URL,
            "verification_uri_complete": CHATGPT_DEVICE_URL,
            "interval": interval,
            "expires_at": now + int(body.get("expires_in") or 600),
        }

    if provider == "supergrok":
        status, body = _post_form(XAI_DEVICE_AUTHORIZATION_URL, {
            "client_id": XAI_CLIENT_ID,
            "scope": XAI_SCOPE,
        }, opener=opener)
        if status // 100 != 2:
            raise OAuthError(
                f"xAI device authorization failed ({status}): {_error_detail(body)}")
        required = ("device_code", "user_code", "verification_uri")
        if any(not body.get(key) for key in required):
            raise OAuthError("xAI device response is missing device_code, user_code or URL")
        return {
            "provider": provider,
            "device_code": body["device_code"],
            "user_code": body["user_code"],
            "verification_uri": body["verification_uri"],
            "verification_uri_complete": (
                body.get("verification_uri_complete") or body["verification_uri"]),
            "interval": max(int(body.get("interval") or 5), 1),
            "expires_at": now + int(body.get("expires_in") or 300),
        }

    raise OAuthError(f"Unsupported subscription OAuth provider: {provider}")


def begin_claude_authorization() -> Dict[str, Any]:
    """The Claude authorization URL and the PKCE verifier that unlocks it.

    Purely local — the verifier is random, the challenge is its SHA-256, and
    no network request happens until the user pastes the code back. The
    verifier doubles as `state`, which is what claude.ai echoes after the
    `#` in the code it shows.
    """
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    url = CLAUDE_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code": "true",
        "client_id": CLAUDE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT_URI,
        "scope": CLAUDE_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    })
    return {"provider": "claude", "url": url, "verifier": verifier,
            "expires_at": int(time.time()) + 600}


def exchange_claude_code(code_input: str, verifier: str,
                         opener: Callable = urllib.request.urlopen) -> Dict[str, Any]:
    """Trade the pasted `code#state` for tokens.

    The page shows one string with a `#` in the middle; users paste it whole,
    with whitespace, or occasionally just the part before the `#`. All three
    must work — the state half is recoverable because it is our verifier.
    """
    raw = (code_input or "").strip()
    if not raw:
        raise OAuthError("no authorization code was pasted")
    code, _, state = raw.partition("#")
    status, tokens = _post_json(CLAUDE_TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code.strip(),
        "state": (state.strip() or verifier),
        "client_id": CLAUDE_CLIENT_ID,
        "redirect_uri": CLAUDE_REDIRECT_URI,
        "code_verifier": verifier,
    }, opener=opener)
    if status // 100 != 2:
        raise OAuthError(
            f"Claude token exchange failed ({status}): {_error_detail(tokens)}")
    return _normalize_tokens("claude", tokens)


def _normalize_tokens(provider: str, tokens: Dict[str, Any],
                      previous_refresh: str = "") -> Dict[str, Any]:
    access = str(tokens.get("access_token", ""))
    refresh = str(tokens.get("refresh_token") or previous_refresh)
    if not access or not refresh:
        raise OAuthError(f"{provider} token response is missing access/refresh token")
    normalized = {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": int((time.time() + int(tokens.get("expires_in") or 3600)) * 1000),
    }
    if provider == "chatgpt":
        account_id = extract_chatgpt_account_id(tokens)
        if account_id:
            normalized["account_id"] = account_id
    return normalized


def poll_device_authorization(provider: str, pending: Dict[str, Any],
                              opener: Callable = urllib.request.urlopen,
                              sleep: Callable[[float], None] = time.sleep,
                              now: Callable[[], float] = time.time) -> Dict[str, Any]:
    interval = max(int(pending.get("interval") or 5), 1)
    deadline = int(pending.get("expires_at") or int(now()) + 300)

    while now() < deadline:
        if provider == "chatgpt":
            status, body = _post_json(
                f"{CHATGPT_ISSUER}/api/accounts/deviceauth/token", {
                    "device_auth_id": pending.get("device_auth_id", ""),
                    "user_code": pending.get("user_code", ""),
                }, opener=opener)
            if status // 100 == 2:
                authorization_code = body.get("authorization_code")
                verifier = body.get("code_verifier")
                if not authorization_code or not verifier:
                    raise OAuthError("ChatGPT authorization response is incomplete")
                token_status, tokens = _post_form(f"{CHATGPT_ISSUER}/oauth/token", {
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{CHATGPT_ISSUER}/deviceauth/callback",
                    "client_id": CHATGPT_CLIENT_ID,
                    "code_verifier": verifier,
                }, opener=opener)
                if token_status // 100 != 2:
                    raise OAuthError(
                        f"ChatGPT token exchange failed ({token_status}): "
                        f"{_error_detail(tokens)}")
                return _normalize_tokens(provider, tokens)
            if status not in (403, 404):
                raise OAuthError(
                    f"ChatGPT device polling failed ({status}): {_error_detail(body)}")
        else:
            status, body = _post_form(XAI_TOKEN_URL, {
                "grant_type": XAI_DEVICE_GRANT,
                "client_id": XAI_CLIENT_ID,
                "device_code": pending.get("device_code", ""),
            }, opener=opener)
            if status // 100 == 2:
                return _normalize_tokens(provider, body)
            error = str(body.get("error", ""))
            if error == "slow_down":
                interval += 5
            elif error not in ("authorization_pending", ""):
                if error in ("access_denied", "authorization_denied"):
                    raise OAuthError("xAI device authorization was denied")
                if error == "expired_token":
                    raise OAuthError("xAI device code expired; run login again")
                raise OAuthError(
                    f"xAI device polling failed ({status}): {_error_detail(body)}")
        remaining = max(0, deadline - now())
        sleep(min(interval + POLL_SAFETY_SECONDS, remaining))

    raise OAuthError(f"{provider} device authorization timed out")


def refresh_tokens(provider: str, current: Dict[str, Any],
                   opener: Callable = urllib.request.urlopen) -> Dict[str, Any]:
    refresh = str(current.get("refresh", ""))
    if not refresh:
        raise OAuthError(f"No refresh token stored for {provider}; run `haikode login {provider}`")
    if provider == "claude":
        # Anthropic's token endpoint takes JSON, not form encoding.
        status, tokens = _post_json(CLAUDE_TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CLAUDE_CLIENT_ID,
        }, opener=opener)
        if status // 100 != 2:
            raise OAuthError(
                f"claude token refresh failed ({status}): {_error_detail(tokens)}")
        return _normalize_tokens(provider, tokens, previous_refresh=refresh)
    if provider == "chatgpt":
        url = f"{CHATGPT_ISSUER}/oauth/token"
        client_id = CHATGPT_CLIENT_ID
    elif provider == "supergrok":
        url = XAI_TOKEN_URL
        client_id = XAI_CLIENT_ID
    else:
        raise OAuthError(f"Unsupported subscription OAuth provider: {provider}")
    status, tokens = _post_form(url, {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }, opener=opener)
    if status // 100 != 2:
        raise OAuthError(
            f"{provider} token refresh failed ({status}): {_error_detail(tokens)}")
    normalized = _normalize_tokens(provider, tokens, previous_refresh=refresh)
    if provider == "chatgpt" and not normalized.get("account_id"):
        normalized["account_id"] = current.get("account_id", "")
    return normalized


def _is_expired(tokens: Dict[str, Any]) -> bool:
    expires = int(tokens.get("expires") or 0)
    return expires <= int((time.time() + REFRESH_SKEW_SECONDS) * 1000)


def access_token(provider: str, store: OAuthStore,
                 opener: Callable = urllib.request.urlopen) -> Dict[str, Any]:
    current = store.get(provider)
    if not current.get("access"):
        if store.read_error:
            # Telling a signed-in user to sign in again sends them to fix the
            # wrong thing — and `login` would overwrite tokens that are fine.
            raise OAuthError(
                "Could not read the saved credentials for %s (%s). The file "
                "is %s; nothing was changed. Try again, and run `haikode "
                "login %s` only if it keeps failing."
                % (provider, store.read_error, store.path, provider))
        raise OAuthError(f"Not signed in to {provider}; run `haikode login {provider}`")
    if not _is_expired(current):
        return current
    # Refreshing under the store lock keeps two processes from rotating the
    # same refresh token, which would invalidate one of the two copies. The
    # re-read is what makes it worthwhile: whoever waited usually finds the
    # other process has already put a fresh token in place.
    with settings_lock(store.path):
        latest = store.get(provider)
        if latest.get("access") and not _is_expired(latest):
            return latest
        # Prefer the stored refresh token, but never lose ours to a file that
        # another process emptied or truncated between our two reads.
        source = latest if latest.get("refresh") else current
        refreshed = refresh_tokens(provider, source, opener=opener)
        store._set_locked(provider, refreshed)
    return refreshed


def open_authorization_url(url: str):
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


def login_interactive(provider: str, store: OAuthStore) -> Dict[str, Any]:
    if provider == "claude":
        return _login_claude_interactive(store)
    pending = begin_device_authorization(provider)
    print(f"Open: {pending['verification_uri']}")
    print(f"Code: {pending['user_code']}")
    print("Waiting for authorization; Ctrl-C cancels locally.")
    open_authorization_url(str(pending.get("verification_uri_complete")
                               or pending["verification_uri"]))
    tokens = poll_device_authorization(provider, pending)
    store.set(provider, tokens)
    return tokens


def _login_claude_interactive(store: OAuthStore) -> Dict[str, Any]:
    """Claude's paste-code half of login_interactive.

    The account must have extra usage enabled for external clients to be
    authorized; without it the page refuses before any code is shown, so
    saying it up front saves the round trip.
    """
    pending = begin_claude_authorization()
    print(f"Open: {pending['url']}")
    print("Approve haikode there; the page then shows a code to copy.")
    print("(Requires a Claude subscription with extra usage enabled — "
          "that is what authorizes external clients.)")
    open_authorization_url(pending["url"])
    try:
        code = input("Paste the code here: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise OAuthError("sign-in cancelled before a code was pasted")
    tokens = exchange_claude_code(code, pending["verifier"])
    store.set("claude", tokens)
    return tokens


def spawn_background_completion(provider: str, store: OAuthStore,
                                pending: Dict[str, Any]):
    store.save_pending(provider, pending)
    env = os.environ.copy()
    subprocess.Popen(
        [sys.executable, "-m", "haikode.oauth", "complete", provider,
         str(store.path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, env=env)


def complete_pending(provider: str, store: OAuthStore) -> int:
    pending = store.load_pending(provider)
    if not pending:
        return 2
    try:
        tokens = poll_device_authorization(provider, pending)
        store.set(provider, tokens)
        return 0
    except OAuthError:
        return 1
    finally:
        store.clear_pending(provider)


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) == 3 and argv[0] == "complete":
        return complete_pending(argv[1], OAuthStore(argv[2]))
    print("usage: python3 -m haikode.oauth complete <provider> <store>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
