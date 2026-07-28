"""
Credential redaction: keep keys out of the transcript, the database and the wire.

A tool result is not a private thing. It is shown to the user, appended to the
agent history, replayed to the model provider on every subsequent request and
written to the session SQLite file. So `printenv`, `env`, `echo $OPENAI_API_KEY`
or a webfetch of a page that happens to quote a token all end the same way: the
user's key is permanently on disk and has been handed to a third party.

Two defences, and both are needed:

* `scrub_env` — a tool subprocess never inherits the credentials in the first
  place, so there is nothing for it to print.
* `redact` — whatever a tool did manage to capture is masked on the way out,
  because the key can also arrive from a file, an HTTP response or a config
  dump that we do not control.

Every rule below is a single linear regex pass so this is cheap enough to run
on every tool result; see tests/test_redact.py for the measured cost.

The hard part is not catching keys, it is *not* catching everything else. A
redactor that rewrites the user's source code is its own bug, so the masking
rules deliberately refuse anything that looks like an expression
(`api_key=load_key(),`) and anything too short to be a credential.

This module owns the redaction rules; haikode/config.py re-exports them so the
older `from .config import redact` call sites keep working.
"""

import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

REDACTED = "[redacted]"
MIN_SECRET_LENGTH = 8  # shorter values ("public", "none") are not secrets

_KNOWN_SECRETS: Set[str] = set()
_KNOWN_SECRETS_ORDERED: List[str] = []
_ENVIRONMENT_SCANNED = False

# Names that hold credentials, and the suffixes that mean "where the
# credential lives" rather than the credential itself (SSH_KEY_PATH).
_CREDENTIAL_NAME_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret|token|password|passwd|"
    r"credential|private[_-]?key|session[_-]?key|auth[_-]?key|apikey|_key$)")
_CREDENTIAL_NAME_EXEMPT_RE = re.compile(
    r"(?i)_(file|path|dir|url|uri|name|id|type|prompt|command|timeout|expiry)$")

# Provider families whose whole namespace is assumed credential-bearing, so a
# variable we have never heard of (OPENAI_ADMIN_KEY, AWS_SESSION_TOKEN) is
# still withheld from a subprocess. Fail closed: unknown means denied.
_WELL_KNOWN_PREFIXES = (
    "OPENAI_", "ANTHROPIC_", "XAI_", "OLLAMA_", "GEMINI_", "GOOGLE_",
    "VERTEX_", "AWS_", "AZURE_OPENAI_", "GROQ_", "MISTRAL_", "COHERE_",
    "DEEPSEEK_", "OPENROUTER_", "TOGETHER_", "FIREWORKS_", "PERPLEXITY_",
    "REPLICATE_", "ANYSCALE_", "CEREBRAS_", "SAMBANOVA_",
)

# Credential variables outside those families. Most are also caught by
# _CREDENTIAL_NAME_RE; they are named anyway so the denylist is a stable,
# reviewable document rather than whatever the regex happens to do today.
_WELL_KNOWN_NAMES = frozenset({
    "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT",
    "GITLAB_TOKEN", "CI_JOB_TOKEN",
    "HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGINGFACE_HUB_TOKEN",
    "NPM_TOKEN", "PYPI_TOKEN", "TWINE_PASSWORD",
    "DOCKER_PASSWORD", "DOCKER_AUTH_CONFIG",
    "VAULT_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "SLACK_TOKEN", "STRIPE_SECRET_KEY", "SENTRY_AUTH_TOKEN",
    "CLOUDFLARE_API_TOKEN", "DIGITALOCEAN_ACCESS_TOKEN",
    "NETLIFY_AUTH_TOKEN", "VERCEL_TOKEN", "SUPABASE_SERVICE_ROLE_KEY",
    "LANGCHAIN_API_KEY", "LANGSMITH_API_KEY", "TAVILY_API_KEY",
})

# Members of a denied family that provably hold configuration, not secrets.
# Withholding these buys no security and breaks ordinary commands: an `aws`
# invocation with no AWS_REGION fails before it ever asks for credentials.
_BENIGN_NAMES = frozenset({
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_PAGER", "AWS_RETRY_MODE", "AWS_SDK_LOAD_CONFIG",
    "AWS_EC2_METADATA_DISABLED", "AWS_DEFAULT_OUTPUT",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION",
    "OLLAMA_HOST", "OLLAMA_MODELS", "OLLAMA_KEEP_ALIVE",
    "OLLAMA_NUM_PARALLEL", "OLLAMA_ORIGINS",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_LOG", "OPENAI_ORG_ID",
    "ANTHROPIC_BASE_URL", "GEMINI_BASE_URL",
})

# Recognisable provider key shapes. Each carries its own prefix, so these
# match keys even when they appear in the middle of prose or JSON.
_PROVIDER_KEY_RE = re.compile(
    r"\b(?:"
    r"sk-(?:ant-|proj-|or-v1-|live-|test-)?[A-Za-z0-9_-]{16,}"
    r"|xai-[A-Za-z0-9]{16,}"
    r"|gsk_[A-Za-z0-9]{16,}"
    r"|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|AIza[A-Za-z0-9_-]{30,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{12,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|(?:xoxb|xoxp|xoxa|xoxs)-[A-Za-z0-9-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"  # JWT
    r")")

# Authorization: Bearer <token>, x-api-key: <key>, "access_token": "...", and
# friends — the field name is kept so the output still reads sensibly.
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api[_-]?key|api[_-]?key|"
    r"x-goog-api-key|x-auth-token|x-amz-security-token|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|client[_-]?secret)"
    # The closing quote of a JSON key ("api_key": ...) sits between the name
    # and the separator, so it has to be part of the kept prefix.
    r"([\"']?\s*[:=]\s*)([\"']?)((?:bearer|basic|token|digest)\s+)?"
    r"([^\s\"',;)\]}]{6,})")

# NAME=value as printenv/export/set emit it.
# The optional `<digits>:` head is the read tool's line-number prefix, and the
# optional `<path>:<digits>:` head is grep's. Anchoring on the bare line start
# alone meant `read .env` and `grep KEY` handed the value over untouched, while
# the same text through `bash` was masked — the leak was in the framing, not in
# the rule.
_ASSIGNMENT_RE = re.compile(
    r"(?m)^([ \t]*(?:[^\s:]*:)?(?:[0-9]+:[ \t]*)?"
    r"(?:export[ \t]+|declare[ \t]+-x[ \t]+|setenv[ \t]+)?)"
    r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# scheme://user:password@host — a DATABASE_URL carries its password in plain
# sight and no shape rule would ever recognise it.
_URL_CREDENTIAL_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+.\-]*://)([^\s/:@]{1,256}):([^\s/@]{1,256})@")

# A long, mixed-alphabet run: opaque bearer tokens look like this, while
# words, hex digests (no case mix) and paths (excluded by the boundaries) do
# not.
_LONG_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_\-./+])[A-Za-z0-9_\-+=]{32,}(?![A-Za-z0-9_\-./+])")

# Characters that only occur in an expression, never inside a credential.
# Their presence is what separates `api_key=load_key(),` in the user's source
# from `api_key=Q1w2E3r4...` in the user's environment.
_CODE_CHARS = frozenset("()[]{}<>\\$`;|&*!?~^\t")
_CODE_WORDS = frozenset({
    "none", "null", "nil", "true", "false", "undefined", "self", "this",
    "os.environ", "process.env", "notset", "not-set", "changeme",
})


def register_secret(value: str) -> None:
    """Remember a live credential so redact() can mask it verbatim.

    Shape rules cannot recognise every provider's key format; the keys this
    process actually loaded are the ones we are certain about. Kept in memory
    only — a secret is never written anywhere by this module.
    """
    if not value or len(value) < MIN_SECRET_LENGTH or value in _KNOWN_SECRETS:
        return
    _KNOWN_SECRETS.add(value)
    # Longest first, so masking a key never leaves a shorter key's suffix
    # exposed when one is a substring of the other.
    _KNOWN_SECRETS_ORDERED[:] = sorted(_KNOWN_SECRETS, key=len, reverse=True)


def register_config_secrets(config: Any) -> None:
    """Learn the api_key values an already-loaded config holds.

    Reads the in-memory config only: no file is opened and nothing is written,
    so calling this can never persist a credential that was not already there.
    """
    data = getattr(config, "data", config) or {}
    for provider in (data.get("providers") or {}).values():
        if isinstance(provider, dict):
            register_secret(provider.get("api_key") or "")


def reset_redaction_cache() -> None:
    """Forget registered secrets. For tests; production never un-learns one."""
    global _ENVIRONMENT_SCANNED
    _KNOWN_SECRETS.clear()
    _KNOWN_SECRETS_ORDERED.clear()
    _ENVIRONMENT_SCANNED = False


def _is_credential_name(name: str) -> bool:
    return bool(_CREDENTIAL_NAME_RE.search(name)
                and not _CREDENTIAL_NAME_EXEMPT_RE.search(name))


def _is_high_entropy(token: str) -> bool:
    """Long and drawn from at least three alphabets — key-shaped, not word-shaped."""
    if len(token) < 32:
        return False
    has_lower = has_upper = has_digit = False
    for char in token:
        if char.islower():
            has_lower = True
        elif char.isupper():
            has_upper = True
        elif char.isdigit():
            has_digit = True
    return has_lower and has_upper and has_digit


def _looks_like_secret(value: str) -> bool:
    value = (value or "").strip().strip("\"'")
    if len(value) < MIN_SECRET_LENGTH:
        return False
    return bool(_PROVIDER_KEY_RE.fullmatch(value)) or _is_high_entropy(value)


def _looks_like_code(value: str) -> bool:
    """True when the value is an expression rather than a literal.

    Without this, redacting `printenv` output also rewrites the user's source:
    `api_key=os.getenv("OPENAI_API_KEY")` and `api_key=load_key(),` both parse
    as NAME=value, and masking them destroys working code for no gain.
    """
    stripped = value.strip()
    if not stripped:
        return True
    if _CODE_CHARS.intersection(stripped):
        return True
    if stripped.rstrip(",").strip().strip("\"'").lower() in _CODE_WORDS:
        return True
    # Trailing comma/colon: an argument or a mapping entry, not a shell value.
    return stripped.endswith((",", ":", "+", "%"))


def _provider_key_envs(config: Any = None) -> Set[str]:
    """The key_env of every configured provider, user-added ones included."""
    data = getattr(config, "data", config)
    if not isinstance(data, dict):
        # Imported lazily: config.py imports this module at its top, so a
        # module-level import here would be circular.
        from .config import DEFAULT_CONFIG
        data = DEFAULT_CONFIG
    names = set()
    for provider in (data.get("providers") or {}).values():
        if isinstance(provider, dict) and provider.get("key_env"):
            names.add(provider["key_env"])
    return names


def is_credential_env(name: str, value: str = "") -> bool:
    """Whether one variable must be withheld from a tool subprocess.

    Name-shaped (ANTHROPIC_API_KEY), family-shaped (any AWS_*) and
    value-shaped, so a variable with an innocent name holding an sk-... key is
    caught too.
    """
    if name in _BENIGN_NAMES:
        return False
    if name in _WELL_KNOWN_NAMES:
        return True
    # A path or a URL names where the credential lives; it is not the
    # credential, and dropping it only breaks the tool that needed it.
    if _CREDENTIAL_NAME_EXEMPT_RE.search(name):
        # ...unless the URL carries its own password (postgres://u:pw@host).
        return bool(value) and bool(_URL_CREDENTIAL_RE.search(value))
    if name.startswith(_WELL_KNOWN_PREFIXES):
        return True
    if _CREDENTIAL_NAME_RE.search(name):
        return True
    return _looks_like_secret(value)


def credential_env_names(environ: Optional[Mapping[str, str]] = None,
                         config: Any = None) -> List[str]:
    """Environment variables a subprocess must not inherit.

    The providers' key_env names are always included, even when unset, so the
    result is a stable denylist rather than a snapshot of what happens to be
    exported right now. `environ` defaults to this process's environment;
    `config` widens the list with a user's own providers.
    """
    source = os.environ if environ is None else environ
    names = set(_provider_key_envs(config))
    names.update(_WELL_KNOWN_NAMES)
    for name, value in source.items():
        if is_credential_env(name, value):
            names.add(name)
    return sorted(names)


def scrub_env(env: Optional[Mapping[str, str]] = None,
              config: Any = None) -> Dict[str, str]:
    """A copy of `env` with every credential removed.

    What a subprocess never receives it cannot print, and a tool result that
    never contains the key cannot carry it into the history, the provider
    request or the session database.
    """
    source = os.environ if env is None else env
    return {name: value for name, value in source.items()
            if not is_credential_env(name, value)}


def _mask_header(match: "re.Match") -> str:
    """Mask an Authorization/x-api-key style value, but not a code expression.

    A value is a credential when it was quoted (JSON, a header dump), when it
    followed an auth scheme (`Bearer ...`), or when it is key-shaped. Anything
    else — `api_key = os.getenv(...)` — is left alone.
    """
    quote, scheme, value = match.group(3), match.group(4), match.group(5)
    if not (quote or scheme or _looks_like_secret(value)):
        return match.group(0)
    return (f"{match.group(1)}{match.group(2)}{quote or ''}"
            f"{scheme or ''}{REDACTED}")


def _mask_assignment(match: "re.Match") -> str:
    name, value = match.group(2), match.group(3)
    if _looks_like_secret(value):
        return f"{match.group(1)}{name}={REDACTED}"
    # A credential name is only enough when the value could actually be a
    # credential: `MAX_KEY=1024` and `api_key=load_key(),` stay as they are.
    if (_is_credential_name(name)
            and len(value.strip().strip("\"'")) >= MIN_SECRET_LENGTH
            and not _looks_like_code(value)):
        return f"{match.group(1)}{name}={REDACTED}"
    return match.group(0)


def _mask_long_token(match: "re.Match") -> str:
    token = match.group(0)
    return REDACTED if _is_high_entropy(token) else token


def _scan_environment_once() -> None:
    """Learn the credential values in our own environment, lazily and once."""
    global _ENVIRONMENT_SCANNED
    if _ENVIRONMENT_SCANNED:
        return
    _ENVIRONMENT_SCANNED = True
    for name, value in os.environ.items():
        if is_credential_env(name, value):
            register_secret(value)


def redact(text: str, secrets: Iterable[str] = (),
           heuristic: bool = True) -> str:
    """Mask credential-shaped values in text on its way to the model or disk.

    Covers the secrets this process has loaded, any `secrets` the caller knows
    about, credential environment assignments, Authorization-style headers,
    URLs with an embedded password, known provider key prefixes and — when
    `heuristic` is on — long high-entropy tokens. Ordinary prose, paths, hex
    digests and source code are left exactly as they were.

    `heuristic=False` keeps only the rules that identify a secret by its
    *shape in context* (`KEY=value`, `Authorization:`, `sk-…`). Those cannot
    misfire on ordinary data. The entropy rule can: a lockfile digest or a
    base64 blob read out of a file looks exactly like a token, and silently
    replacing it would corrupt what the model is trying to reason about. So
    the entropy pass belongs where output is *command output* — the shell —
    and not where it is the literal contents of a file the user asked for.

    Cheap by construction: a fixed number of linear passes, no backtracking
    regex, and str.replace for the literal secrets.
    """
    if not text:
        return text
    _scan_environment_once()
    extra = [s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH]
    for secret in sorted(extra, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for secret in _KNOWN_SECRETS_ORDERED:
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = _ASSIGNMENT_RE.sub(_mask_assignment, text)
    text = _HEADER_RE.sub(_mask_header, text)
    text = _URL_CREDENTIAL_RE.sub(rf"\g<1>\g<2>:{REDACTED}@", text)
    text = _PROVIDER_KEY_RE.sub(REDACTED, text)
    if not heuristic:
        return text
    return _LONG_TOKEN_RE.sub(_mask_long_token, text)
