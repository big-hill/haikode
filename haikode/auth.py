"""
Interactive login/key management, shared by the REPL (/login) and `haikode login`.
"""
import getpass

from .config import Config
from .configtool import test_provider
from .oauth import OAuthError, OAuthStore, login_interactive


def key_status_lines(config: Config):
    lines = []
    for name, prov in config.data.get("providers", {}).items():
        if prov.get("oauth_provider"):
            status = OAuthStore.for_config(config).status(name)
            lines.append(f"  {name:<12} local subscription OAuth: {status}")
        elif not prov.get("requires_key", True):
            lines.append(f"  {name:<12} no key (LAN/Tailscale endpoint)")
        else:
            lines.append(f"  {name:<12} key: {config.key_source(name)}")
    return lines


def interactive_login(config: Config, provider: str = "") -> bool:
    """Prompt for an API key, validate it, store it. Returns True on success."""
    providers = config.data.get("providers", {})

    if not provider:
        print("Providers:")
        for line in key_status_lines(config):
            print(line)
        provider = input("Provider: ").strip()

    prov = providers.get(provider)
    if not prov:
        print(f"Unknown provider '{provider}'. Available: {', '.join(providers)}")
        return False

    if prov.get("oauth_provider"):
        try:
            login_interactive(provider, OAuthStore.for_config(config))
        except (OAuthError, KeyboardInterrupt) as exc:
            print(f"OAuth failed: {exc}")
            return False
        print(f"{provider} subscription token saved locally on Haiku.")
        return True

    if not prov.get("requires_key", True):
        ok, detail = test_provider(config, provider)
        print(("Connection OK: " if ok else "Connection failed: ") + detail)
        return ok

    try:
        key = getpass.getpass(f"API key for {provider} (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not key:
        print("Empty key — aborted.")
        return False

    # Validate before saving so a typo doesn't silently break the setup.
    ok, detail = test_provider(config, provider, key_override=key)

    if not ok:
        print(f"Validation failed: {detail}")
        keep = input("Save anyway? [y/N] ").strip().lower()
        if keep != "y":
            return False

    where = config.set_api_key(provider, key)
    place = "Haiku keystore (BKeyStore)" if where == "keystore" else str(config.path)
    print(f"Key for {provider} saved in {place}.")
    return True
