"""
Config bridge CLI — shared by the haikode CLI and the native desktop app.

The BeAPI Settings window shells out to this instead of parsing JSON in C++:
    python3 -m haikode.configtool list-providers
    python3 -m haikode.configtool get providers.xai.base_url
    python3 -m haikode.configtool set providers.xai.model grok-4
    printf '%s' <secret> | python3 -m haikode.configtool set-key-stdin xai
    python3 -m haikode.configtool set-key xai <secret>   (DEPRECATED, see below)
    python3 -m haikode.configtool clear-key xai
    python3 -m haikode.configtool test xai

`set-key` is deprecated and will be removed: a secret passed as an argument is
visible to every user on the machine through `ps`. It still works so existing
scripts keep running, but it warns on stderr. Pipe the key to `set-key-stdin`.

Exit codes: 0 = ok, 1 = failure, 2 = usage error.
"""
import json
import sys
import urllib.error
import urllib.request

from .config import Config
from .net import USER_AGENT, _with_ua
from .oauth import (OAuthError, OAuthStore, access_token,
                    begin_device_authorization, spawn_background_completion)

TEST_TIMEOUT = 8

# Wire-protocol client id for the ChatGPT Codex backend, NOT a display name.
# It is deliberately left at the pre-rename value so it keeps matching
# providers/subscription.py; the backend sees the same originator for the
# connectivity test and for real requests.
CODEX_ORIGINATOR = "hai"

SET_KEY_DEPRECATION = (
    "warning: `set-key <provider> <secret>` is deprecated — the secret is "
    "visible in `ps` to every user on this machine. Pipe it instead: "
    "printf '%s' <secret> | configtool set-key-stdin <provider>")


def _resolve(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _assign(data, dotted, value):
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ValueError(f"{dotted}: '{part}' is not an object")
    node[parts[-1]] = value


def test_provider(config: Config, name: str, key_override: str = ""):
    """Live connectivity/key check. Returns (ok: bool, detail: str)."""
    prov = config.data["providers"].get(name)
    if not prov:
        return False, f"unknown provider '{name}'"
    dialect = prov.get("dialect", "openai")
    base = prov.get("base_url", "").rstrip("/")

    if prov.get("oauth_provider"):
        oauth_provider = prov["oauth_provider"]
        try:
            auth = access_token(oauth_provider, OAuthStore.for_config(config))
        except OAuthError as exc:
            return False, str(exc)
        headers = {"Authorization": f"Bearer {auth['access']}"}
        if dialect == "chatgpt":
            url = f"{base}/models?client_version=1.0.0"
            headers.update({
                "originator": CODEX_ORIGINATOR,
                "User-Agent": USER_AGENT,
            })
            if auth.get("account_id"):
                headers["ChatGPT-Account-Id"] = auth["account_id"]
        else:
            url = f"{base}/models"
    elif dialect == "anthropic":
        key = key_override or config.get_api_key(name)
        if not key:
            return False, "no API key set"
        url = f"{base}/v1/models"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:  # openai-compatible
        key = key_override or config.get_api_key(name)
        if prov.get("requires_key", True) and not key:
            return False, "no API key set"
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}

    req = urllib.request.Request(url, headers=_with_ua(headers))
    try:
        with urllib.request.urlopen(req, timeout=TEST_TIMEOUT) as resp:
            body = resp.read(2000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"HTTP {e.code}: key rejected"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"unreachable: {e}"

    if prov.get("oauth_provider"):
        return True, "local subscription token accepted"
    if not prov.get("requires_key", True):
        return True, "endpoint reachable"
    return True, "key accepted"


def list_models(config: Config, name: str):
    """Model ids offered by a provider. Returns (ids, error)."""
    entries, err = list_model_entries(config, name)
    return [entry["id"] for entry in entries], err


# Field names carrying a context window in an OpenAI-style /models entry.
# xAI and Kimi both answer `context_length`; the others are the spellings
# used by gateways that proxy for them.
CONTEXT_FIELDS = ("context_length", "context_window", "max_context_length",
                  "max_context_window_tokens", "max_input_tokens")

# Per-model /api/show calls against a local Ollama, capped: a machine with a
# large library must not turn opening the model dialog into a minute of round
# trips. They are local and fast, so the cap can be generous.
OLLAMA_SHOW_LIMIT = 32
OLLAMA_SHOW_TIMEOUT = 4


def entry_context(entry) -> int:
    """The context window an endpoint claims for one model, or 0.

    Nothing is inferred from the model's name — a number is used only where
    the endpoint stated one, so a wrong window is the endpoint's error and
    not ours.
    """
    if not isinstance(entry, dict):
        return 0
    for field in CONTEXT_FIELDS:
        try:
            value = int(entry.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    nested = entry.get("limit")
    if isinstance(nested, dict):
        return entry_context(nested) or _int_or_zero(nested.get("context"))
    return 0


def _int_or_zero(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def ollama_context(base_url: str, model: str) -> int:
    """What a local Ollama will actually serve for one model, or 0.

    Two different numbers are on offer and only one of them is the truth:
    `model_info.<arch>.context_length` is what the weights support, while a
    `num_ctx` parameter is what this server was told to allocate. A model
    published as 262144 with `num_ctx 94208` answers at 94208, so the
    parameter wins wherever it exists.
    """
    root = base_url.rstrip("/")
    for suffix in ("/v1", "/api"):
        if root.endswith(suffix):
            root = root[:-len(suffix)]
    payload = json.dumps({"model": model}).encode()
    req = urllib.request.Request(
        root + "/api/show", data=payload,
        headers=_with_ua({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_SHOW_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return 0
    if not isinstance(body, dict):
        return 0
    for line in str(body.get("parameters") or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx":
            found = _int_or_zero(parts[1])
            if found:
                return found
    info = body.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if str(key).endswith(".context_length"):
                found = _int_or_zero(value)
                if found:
                    return found
    return 0


def list_model_entries(config: Config, name: str):
    """Models offered by a provider, with any context window they declare.

    Returns ([{"id": str, "context": int}], error). `context` is 0 when the
    endpoint said nothing — the caller then keeps whatever the configuration
    holds rather than substituting a guess.
    """
    prov = config.data["providers"].get(name)
    if not prov:
        return [], f"unknown provider '{name}'"
    dialect = prov.get("dialect", "openai")
    base = prov.get("base_url", "").rstrip("/")

    if prov.get("oauth_provider"):
        try:
            auth = access_token(prov["oauth_provider"],
                                OAuthStore.for_config(config))
        except OAuthError as exc:
            return [], str(exc)
        headers = {"Authorization": f"Bearer {auth['access']}"}
        if dialect == "chatgpt":
            url = f"{base}/models?client_version=1.0.0"
            headers["originator"] = CODEX_ORIGINATOR
            if auth.get("account_id"):
                headers["ChatGPT-Account-Id"] = auth["account_id"]
        else:
            url = f"{base}/models"

    else:
        key = config.get_api_key(name)
    if not prov.get("oauth_provider") and dialect == "anthropic":
        if not key:
            return [], "no API key set"
        url = f"{base}/v1/models"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif not prov.get("oauth_provider"):  # OpenAI-compatible endpoints
        if prov.get("requires_key", True) and not key:
            return [], "no API key set"
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}

    req = urllib.request.Request(url, headers=_with_ua(headers))
    try:
        with urllib.request.urlopen(req, timeout=TEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code}"
    except Exception as e:
        return [], f"unreachable: {e}"

    key_field = "slug" if dialect == "chatgpt" else "id"
    container = "models" if dialect == "chatgpt" else "data"
    items = body.get(container, []) if isinstance(body, dict) else []
    entries = [{"id": str(m.get(key_field)), "context": entry_context(m)}
               for m in items
               if isinstance(m, dict) and m.get(key_field)]
    if entries and not any(e["context"] for e in entries) \
            and _is_ollama_endpoint(base):
        # Ollama's OpenAI-compatible listing carries no window at all, but its
        # native API does — and for a local server that number is the only
        # honest one, since it depends on how this machine loaded the model.
        #
        # The first call is also the test of whether this is Ollama at all:
        # llama.cpp and LM Studio serve the same /v1 from the same kind of
        # address and have no /api/show, and 32 timeouts at four seconds each
        # would be two minutes of nothing.
        for entry in entries[:OLLAMA_SHOW_LIMIT]:
            entry["context"] = ollama_context(base, entry["id"])
            if not entry["context"]:
                break
    return entries, "" if entries else "no models returned"


def _is_ollama_endpoint(base_url: str) -> bool:
    """True for something that answers Ollama's native API alongside /v1."""
    from .models import _is_local_endpoint
    lowered = (base_url or "").lower()
    return ":11434" in lowered or "ollama" in lowered \
        or _is_local_endpoint(base_url)


def format_transcript(messages) -> str:
    """Plain text for the desktop transcript view.

    Only the conversation is shown; tool traffic stays out of the BTextView
    because the native app renders tool activity in its own outline list.
    Labels match the styled runs the window writes while streaming.
    """
    blocks = []
    for message in messages:
        label = {"user": "You:", "assistant": "haikode:"}.get(message.role, "")
        text = (message.content or "").strip()
        if label and text:
            blocks.append(f"{label}\n{text}\n")
    return "\n".join(blocks)


def start_oauth(config: Config, name: str):
    """Start device OAuth locally and poll in a detached local process."""
    prov = config.data["providers"].get(name)
    if not prov:
        return False, f"unknown provider '{name}'"
    if not prov.get("oauth_provider"):
        return False, f"'{name}' is not an OAuth subscription provider"
    try:
        oauth_provider = prov["oauth_provider"]
        pending = begin_device_authorization(oauth_provider)
        store = OAuthStore.for_config(config)
        spawn_background_completion(oauth_provider, store, pending)
    except OAuthError as exc:
        return False, str(exc)
    url = str(pending.get("verification_uri_complete")
              or pending["verification_uri"])
    return True, {
        "url": url,
        "method": "auto",
        "instructions": (
            f"Enter code {pending['user_code']}; authorization will be "
            "stored locally on Haiku."),
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    config = Config()

    if cmd == "list-providers":
        for name, prov in config.data.get("providers", {}).items():
            if prov.get("oauth_provider"):
                key = "oauth"
            elif not prov.get("requires_key", True):
                key = "n/a"
            else:
                key = "yes" if config.get_api_key(name) else "no"
            print("\t".join([
                name,
                prov.get("dialect", ""),
                prov.get("base_url", ""),
                prov.get("model", ""),
                f"key:{key}",
                prov.get("directory", ""),
            ]))
        return 0

    if cmd == "get" and len(args) == 1:
        value = _resolve(config.data, args[0])
        if value is None:
            print(f"not found: {args[0]}", file=sys.stderr)
            return 1
        print(value if isinstance(value, str) else json.dumps(value))
        return 0

    if cmd == "set" and len(args) == 2:
        raw = args[1]
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        try:
            _assign(config.data, args[0], value)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        config.save()
        print("ok")
        return 0

    if cmd == "set-key" and len(args) == 2:
        # Deprecated: the secret sits in this process's argv, which every user
        # on the machine can read with `ps`. Kept for one release because
        # scripts may still call it; use set-key-stdin instead.
        print(SET_KEY_DEPRECATION, file=sys.stderr)
        try:
            where = config.set_api_key(args[0], args[1])
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"ok {where}")
        return 0

    if cmd == "set-key-stdin" and len(args) == 1:
        key = sys.stdin.read().rstrip("\r\n")
        if not key:
            print("empty key", file=sys.stderr)
            return 1
        try:
            where = config.set_api_key(args[0], key)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"ok {where}")
        return 0

    if cmd == "clear-key" and len(args) == 1:
        config.clear_api_key(args[0])
        print("ok")
        return 0

    if cmd == "test" and len(args) == 1:
        ok, detail = test_provider(config, args[0])
        print(("OK " if ok else "FAIL ") + detail)
        return 0 if ok else 1

    if cmd == "models" and len(args) == 1:
        ids, err = list_models(config, args[0])
        if err:
            print(err, file=sys.stderr)
            return 1
        for model_id in ids:
            print(model_id)
        return 0

    if cmd == "oauth-start" and len(args) == 1:
        ok, result = start_oauth(config, args[0])
        if not ok:
            print(result, file=sys.stderr)
            return 1
        # TSV is intentionally easy for the native Settings window to parse.
        url = str(result.get("url", "")).replace("\t", " ").replace("\n", " ")
        instructions = str(result.get("instructions", "")).replace("\t", " ").replace("\n", " ")
        print(f"{url}\t{result.get('method', '')}\t{instructions}")
        return 0

    if cmd == "sessions" and not args:
        from .session import SessionStore
        for record in SessionStore().list_sessions():
            title = (record["title"] or record["id"]).replace(
                "\t", " ").replace("\n", " ")
            print(f"{record['id']}\t{title}")
        return 0

    if cmd == "session-text" and len(args) == 1:
        from .session import SessionStore
        session = SessionStore().load(args[0])
        if session is None:
            print(f"unknown session '{args[0]}'", file=sys.stderr)
            return 1
        sys.stdout.write(format_transcript(session.messages))
        return 0

    if cmd == "add-provider" and len(args) == 5:
        name, dialect, base_url, model, requires_key = args
        try:
            config.add_provider(
                name, dialect, base_url, model,
                requires_key=requires_key.lower() in ("1", "true", "yes"))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print("ok")
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
