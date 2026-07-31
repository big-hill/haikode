"""
Model and provider catalogue — the data behind the model dialog (<leader>m),
the provider dialog (ctrl+a) and "add provider".

Ported from opencode's packages/tui/src/component/dialog-model.tsx and
dialog-provider.tsx: the same three sections (favourites, then recents, then
one group per provider), the same de-duplication between those sections, and
the same "most recent first, capped at ten, no duplicates" recents list.

Nothing here draws or prompts. Dialogs get plain data in and plain data out, so
the same catalogue can be rendered by curses, by the desktop app or by a test.

Listing a provider's models costs a network round trip (configtool.list_models)
which, on a Haiku box over a slow link, is far too expensive to repeat every
time the dialog opens — so results are cached in memory and on disk.
"""

import ipaddress
import json
import urllib.parse
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import configtool
from .context import global_config_dir
# The provider dialog and /status must never disagree about whether a provider
# is usable, so the wording comes from the one place that decides it.
from .status import _auth as _auth_state

RECENT_LIMIT = 10          # opencode's recentModels() slices at 10
FAVOURITES_KEY = "model_favourites"
RECENT_KEY = "model_recent"

FAVOURITES_CATEGORY = "Favourites"
RECENT_CATEGORY = "Recent"
POPULAR_CATEGORY = "Popular"
PROVIDERS_CATEGORY = "Providers"

CACHE_FILE = "model-cache.json"
CACHE_VERSION = 1
CACHE_TTL = 24 * 3600      # seconds; model line-ups change on the order of days

DIALECTS = ("openai", "anthropic")

# opencode ranks the providers it ships with above everything else
# (PROVIDER_PRIORITY in dialog-provider.tsx); this is the Haiku line-up in the
# same spirit — hosted defaults first, local endpoints last, custom providers
# after them sorted by name.
PROVIDER_PRIORITY: Dict[str, int] = {
    "zen": 0,
    "openai": 1,
    "anthropic": 2,
    "chatgpt": 3,
    "supergrok": 4,
    "xai": 5,
    "ollama": 6,
    "ollama-local": 7,
}
UNRANKED = 99


@dataclass
class ModelRef:
    """One selectable model. `category` is the section header a palette draws
    above it, so the same ref can appear as "Favourites" or as its provider."""

    provider: str
    model: str
    label: str = ""
    category: str = ""
    free: bool = False
    context: int = 0

    def __post_init__(self):
        if not self.label:
            self.label = self.model

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.model}"


def parse_model_id(value: str) -> Tuple[str, str]:
    """"provider/model" -> (provider, model).

    Only the first separator counts, because model ids may themselves contain
    slashes (openrouter style) — same as opencode's parseModel(). A value with
    no slash has no provider and is treated as invalid by the callers.
    """
    provider, sep, model = str(value or "").strip().partition("/")
    if not sep:
        return "", provider
    return provider, model


def _is_free(model_id: str, provider_conf: Dict[str, Any]) -> bool:
    """opencode flags zero-cost models with a "Free" footer from the price
    feed. We have no price feed, so infer it: providers name free models
    "...-free", and a key-less endpoint (local Ollama) costs nothing to call."""
    parts = model_id.lower().replace("_", "-").replace(":", "-").split("-")
    if "free" in parts:
        return True
    return (not provider_conf.get("requires_key", True)
            and not provider_conf.get("oauth_provider"))


def sort_models(refs: List[ModelRef]) -> List[ModelRef]:
    """Free models first, then by label — opencode's sortModelOptions() minus
    the release-date key, which /models does not give us."""
    return sorted(refs, key=lambda ref: (not ref.free, ref.label.lower(), ref.label))


class ModelCatalog:
    """Providers, their models, favourites and recents, as data.

    `cache_path` exists so tests (and a --config run) can keep the on-disk
    cache away from the user's real settings directory.
    """

    def __init__(self, config, cache_path: Union[str, Path, None] = None):
        self.config = config
        self.errors: Dict[str, str] = {}
        self._ids: Dict[str, List[str]] = {}
        self._cache_path = (Path(cache_path) if cache_path is not None
                            else global_config_dir() / CACHE_FILE)
        self._disk: Optional[Dict[str, Any]] = None

    # --- providers -------------------------------------------------------

    def _providers(self) -> Dict[str, Any]:
        providers = self.config.data.get("providers")
        return providers if isinstance(providers, dict) else {}

    def provider_names(self) -> List[str]:
        """Configured providers, ranked like opencode's provider dialog."""
        return sorted(self._providers(),
                      key=lambda name: (PROVIDER_PRIORITY.get(name, UNRANKED),
                                        name.lower(), name))

    def providers(self) -> List[Dict[str, Any]]:
        """Rows for the provider dialog.

        Purely local: key_source() looks at the keystore, the config file and
        the environment, never the network — the dialog has to open instantly
        even when the machine is offline. get_api_key() is deliberately not
        called on top of it: it would spawn a second hai-keystore process per
        provider without telling us anything key_source() did not already.
        """
        default = self.config.data.get("default_provider", "")
        rows: List[Dict[str, Any]] = []
        for name in self.provider_names():
            conf = self._providers().get(name) or {}
            auth, auth_ok = _auth_state(self.config, name, conf)
            rows.append({
                "name": name,
                "dialect": conf.get("dialect", "openai"),
                "base_url": conf.get("base_url", ""),
                "model": conf.get("model", ""),
                "auth": auth,
                "auth_ok": auth_ok,
                "is_default": name == default,
                # dialog-provider.tsx splits the list into "Popular" (the ids
                # it ranks) and "Providers" (everything else).
                "category": (POPULAR_CATEGORY if name in PROVIDER_PRIORITY
                             else PROVIDERS_CATEGORY),
            })
        return rows

    # --- models ----------------------------------------------------------

    def _ref(self, provider: str, model: str) -> Optional[ModelRef]:
        """A labelled ref, or None when the provider is no longer configured."""
        conf = self._providers().get(provider)
        if conf is None or not model:
            return None
        try:
            context = int(conf.get("context") or 0)
        except (TypeError, ValueError):
            context = 0
        return ModelRef(provider=provider, model=model, label=model,
                        category=provider, free=_is_free(model, conf),
                        context=context)

    def _coerce(self, ref: Union[ModelRef, str]) -> ModelRef:
        if isinstance(ref, ModelRef):
            return ref
        provider, model = parse_model_id(str(ref))
        return ModelRef(provider=provider, model=model)

    def models(self, provider: Optional[str] = None,
               refresh: bool = False) -> List[ModelRef]:
        """Models offered by one provider, or by all of them.

        A provider that fails contributes nothing and lands in `.errors`: one
        unreachable endpoint must not empty the dialog for the others.
        """
        names = [provider] if provider else self.provider_names()
        out: List[ModelRef] = []
        for name in names:
            refs = [self._ref(name, model_id)
                    for model_id in self._ids_for(name, refresh)]
            out.extend(sort_models([ref for ref in refs if ref is not None]))
        return out

    def invalidate(self, provider: Optional[str] = None):
        """Forget what has been listed, so the next models() call asks again.

        Storing a key, or pointing a provider somewhere else, changes what it
        will answer — and the failure remembered from before must not outlive
        the fix for it. The on-disk cache is left alone: it is keyed by
        base_url and only ever holds line-ups that were fetched successfully.
        """
        if provider is None:
            self._ids.clear()
            self.errors.clear()
            return
        self._ids.pop(provider, None)
        self.errors.pop(provider, None)

    def _ids_for(self, name: str, refresh: bool) -> List[str]:
        conf = self._providers().get(name) or {}
        base_url = str(conf.get("base_url", ""))

        if not refresh:
            cached = self._ids.get(name)
            if cached is None:
                cached = self._read_cache(name, base_url)
                if cached is not None:
                    self._ids[name] = cached
            if cached is not None:
                return cached

        try:
            ids, err = configtool.list_models(self.config, name)
        except Exception as exc:      # a broken provider must not raise into a dialog
            ids, err = [], (str(exc) or exc.__class__.__name__)

        ids = [str(model_id) for model_id in (ids or []) if model_id]
        if err or not ids:
            self.errors[name] = err or "no models returned"
            return self._after_failure(name, base_url)
        self.errors.pop(name, None)
        self._ids[name] = ids
        self._write_cache(name, base_url, ids)
        return ids

    def _after_failure(self, name: str, base_url: str) -> List[str]:
        """What a provider still contributes once listing it has failed.

        Two things matter offline, which on Haiku is the normal case. The last
        known line-up is served even once it is past the TTL — a day-old list
        for an unchanged endpoint beats an empty dialog, and `.errors` still
        says the refresh failed. And the outcome is remembered for the rest of
        the session, so a dead endpoint costs one connection timeout rather
        than another one every single time the dialog is opened.
        """
        stale = self._ids.get(name)
        if stale is None:
            stale = self._read_cache(name, base_url, ttl=None) or []
        self._ids[name] = stale
        return stale

    # --- on-disk cache ---------------------------------------------------

    def _entries(self, reload: bool = False) -> Dict[str, Any]:
        if self._disk is None or reload:
            entries: Dict[str, Any] = {}
            try:
                raw = json.loads(self._cache_path.read_text())
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                stored = raw.get("providers")
                if isinstance(stored, dict):
                    entries = stored
            self._disk = entries
        return self._disk

    def _read_cache(self, name: str, base_url: str,
                    ttl: Optional[float] = CACHE_TTL) -> Optional[List[str]]:
        """The cached ids for a provider, or None. ttl=None accepts any age."""
        entry = self._entries().get(name)
        if not isinstance(entry, dict):
            return None
        ids = entry.get("models")
        if not isinstance(ids, list) or not ids:
            return None
        # A retargeted endpoint serves a different line-up, so the old list is
        # not just stale, it is wrong.
        if str(entry.get("base_url", "")) != base_url:
            return None
        if ttl is not None:
            try:
                age = time.time() - float(entry.get("time", 0))
            except (TypeError, ValueError):
                return None
            if age < 0 or age > ttl:
                return None
        return [str(model_id) for model_id in ids if model_id]

    def _write_cache(self, name: str, base_url: str, ids: List[str]):
        # Re-read before writing: the whole file is rewritten, so a snapshot
        # taken when this catalogue was created would drop whatever another
        # haikode process (or a second catalogue) cached in the meantime.
        entries = self._entries(reload=True)
        entries[name] = {"time": time.time(), "base_url": base_url,
                         "models": list(ids)}
        payload = {"version": CACHE_VERSION, "providers": entries}
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass  # a read-only settings directory must not break the dialog

    # --- favourites and recents ------------------------------------------

    def _stored(self, key: str) -> List[str]:
        """The saved "provider/model" list, junk and duplicates removed."""
        raw = self.config.data.get(key)
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        seen = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            provider, model = parse_model_id(item)
            if not provider or not model:
                continue
            value = f"{provider}/{model}"
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _refs(self, ids: List[str]) -> List[ModelRef]:
        """Refs for saved ids, dropping providers that no longer exist.

        A favourite whose provider is still configured is kept even when its
        model list could not be fetched: offline, the saved picks are the only
        thing left to choose from.
        """
        refs = []
        for value in ids:
            provider, model = parse_model_id(value)
            ref = self._ref(provider, model)
            if ref is not None:
                refs.append(ref)
        return refs

    def favourites(self) -> List[ModelRef]:
        return self._refs(self._stored(FAVOURITES_KEY))

    def toggle_favourite(self, ref: Union[ModelRef, str]) -> bool:
        """Add or remove a favourite. Returns whether it is now a favourite."""
        model = self._coerce(ref)
        if not model.provider or not model.model:
            return False
        stored = self._stored(FAVOURITES_KEY)
        if model.id in stored:
            stored.remove(model.id)
            now_favourite = False
        else:
            stored.insert(0, model.id)  # opencode prepends: [model, ...favorite]
            now_favourite = True
        self.config.data[FAVOURITES_KEY] = stored
        self.config.save()
        return now_favourite

    def recent(self) -> List[ModelRef]:
        return self._refs(self._stored(RECENT_KEY))

    def record_use(self, ref: Union[ModelRef, str]):
        """Move a model to the front of the recents, capped at RECENT_LIMIT."""
        model = self._coerce(ref)
        if not model.provider or not model.model:
            return
        stored = [value for value in self._stored(RECENT_KEY) if value != model.id]
        self.config.data[RECENT_KEY] = [model.id] + stored[:RECENT_LIMIT - 1]
        self.config.save()

    def cycle_recent(self, current: Union[ModelRef, str, None],
                     step: int = 1) -> Optional[ModelRef]:
        """The next recent model, wrapping at both ends (the f2 binding).

        opencode gives up when the current model is not in the list; here it
        falls back to the newest entry instead, so f2 still does something in a
        fresh session where nothing has been recorded yet.
        """
        recent = self.recent()
        if not recent:
            return None
        key = self._coerce(current).id if current is not None else ""
        index = next((i for i, ref in enumerate(recent) if ref.id == key), -1)
        if index < 0:
            return recent[0]
        return recent[(index + step) % len(recent)]

    def cycle_favourite(self, current: Union[ModelRef, str, None],
                        step: int = 1) -> Optional[ModelRef]:
        """The next favourite model (opencode's cycleFavorite, bound to
        model_cycle_favorite / _reverse).

        Off the list, cycling forwards starts at the first favourite and
        backwards at the last, exactly as in local.tsx. None means there is
        nothing to cycle through, which is where opencode shows "Add a
        favorite model to use this shortcut".
        """
        favourites = self.favourites()
        if not favourites:
            return None
        key = self._coerce(current).id if current is not None else ""
        index = next((i for i, ref in enumerate(favourites) if ref.id == key), -1)
        if index < 0:
            return favourites[0] if step >= 0 else favourites[-1]
        return favourites[(index + step) % len(favourites)]

    # --- the dialog list --------------------------------------------------

    def choices(self, refresh: bool = False) -> List[ModelRef]:
        """Everything the model dialog lists, in opencode's order.

        Favourites, then recents that are not already favourites, then one
        group per provider with the entries shown above filtered out — see
        dialog-model.tsx. `category` carries the section header.
        """
        grouped: Dict[str, List[ModelRef]] = {}
        for ref in self.models(refresh=refresh):
            grouped.setdefault(ref.provider, []).append(ref)

        out: List[ModelRef] = []
        seen = set()

        def take(refs: List[ModelRef], category: str):
            for ref in refs:
                if ref.id in seen:
                    continue
                seen.add(ref.id)
                out.append(replace(ref, category=category))

        take(self.favourites(), FAVOURITES_CATEGORY)
        take(self.recent(), RECENT_CATEGORY)
        for name in self.provider_names():
            take(grouped.get(name, []), name)
        return out

    # --- selection -------------------------------------------------------

    def current(self) -> Optional[ModelRef]:
        """The model a new request would use, per the config.

        A default_provider naming a profile that is not there falls back to the
        first configured one, because that is what Config.get_provider() does:
        reporting "no model" while requests quietly go to another provider
        would be worse than reporting the provider actually in use.
        """
        providers = self._providers()
        name = self.config.data.get("default_provider", "")
        if name not in providers:
            name = next(iter(providers), "")
        conf = providers.get(name) or {}
        return self._ref(name, str(conf.get("model", "")))

    def select(self, ref: Union[ModelRef, str]) -> ModelRef:
        """Make `ref` the active model and remember the choice.

        The provider's own default model is what the rest of haikode reads, so
        picking a model from another provider has to move `default_provider`
        too or the selection would silently not take effect.
        """
        model = self._coerce(ref)
        providers = self._providers()
        if model.provider not in providers:
            raise KeyError(f"Unknown provider: {model.provider}")
        if not model.model:
            raise ValueError("no model id given")
        providers[model.provider]["model"] = model.model
        self.config.data["providers"] = providers
        self.config.data["default_provider"] = model.provider
        self.record_use(model)      # persists the whole config
        self.config.save()
        selected = self._ref(model.provider, model.model)
        return selected if selected is not None else model


# --------------------------------------------------------------------------
# provider management — thin wrappers that report instead of raising, so a
# dialog can put the reason on screen
# --------------------------------------------------------------------------


def _is_local_endpoint(base_url: str) -> bool:
    """True for a host that is this machine or the local network.

    Ollama, LM Studio and llama.cpp all serve without authentication, so a
    profile pointing at one must not be created demanding a key: the user is
    then told "no key set — run /login <name>" for a service that has no
    login, and the provider reports itself unusable. Getting this wrong the
    other way is harmless by comparison — a keyed service refuses the request
    and says so plainly.
    """
    try:
        host = urllib.parse.urlparse(base_url).hostname or ""
    except ValueError:
        return False
    if host in ("localhost", "::1") or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Tailscale hands out 100.64/10, which is carrier-grade NAT space and
    # already covered by is_private on modern Python; keep it explicit.
    return (address.is_loopback or address.is_private
            or address in ipaddress.ip_network("100.64.0.0/10"))


def add_provider(config, name: str, base_url: str, model: str = "",
                 dialect: str = "openai", requires_key: Optional[bool] = None,
                 update: bool = False) -> Tuple[bool, str]:
    """Create a provider profile, or update one when `update` is set.

    `requires_key` defaults to None, meaning "decide from the endpoint":
    local and LAN addresses are treated as keyless. Pass True or False to
    override.
    """
    name = (name or "").strip()
    if not name:
        return False, "provider name is required"
    providers = config.data.get("providers") or {}
    existed = name in providers
    if existed and not update:
        return False, f"provider '{name}' already exists"
    # Snapshot before config.add_provider() replaces the profile object.
    existing = providers.get(name) or {}
    # Config.add_provider() rebuilds a profile from scratch and only accepts
    # the openai and anthropic dialects, so pushing a subscription profile
    # through it would drop oauth_provider and its "chatgpt"/"supergrok"
    # dialect, leaving an endpoint nothing can sign in to.
    if existing.get("oauth_provider"):
        return False, (f"'{name}' signs in with OAuth; its endpoint is not "
                       "editable here")
    if dialect not in DIALECTS:
        return False, "dialect must be openai or anthropic"
    base_url = (base_url or "").strip()
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return False, "base URL must start with http:// or https://"
    if requires_key is None:
        requires_key = not _is_local_endpoint(base_url)
    try:
        config.add_provider(name, dialect, base_url, (model or "").strip(),
                            requires_key=bool(requires_key))
    except (ValueError, KeyError, OSError) as exc:
        return False, str(exc)
    # That same rebuild resets context to its 128k default; editing a model id
    # must not silently shrink the window a provider was configured with.
    kept = existing.get("context")
    if kept and config.data["providers"][name].get("context") != kept:
        config.data["providers"][name]["context"] = kept
        config.save()
    return True, (f"updated provider '{name}'" if existed
                  else f"added provider '{name}'")


def remove_provider(config, name: str) -> Tuple[bool, str]:
    """Delete a provider profile and its stored key."""
    name = (name or "").strip()
    providers = config.data.get("providers") or {}
    if name not in providers:
        return False, f"unknown provider '{name}'"
    if len(providers) <= 1:
        return False, f"cannot remove '{name}': it is the only provider"
    if config.data.get("default_provider") == name:
        return False, (f"'{name}' is the default provider; "
                       "make another one default first")
    try:
        config.remove_provider(name)
    except (KeyError, OSError) as exc:
        return False, str(exc)
    return True, f"removed provider '{name}'"


def set_default(config, name: str) -> Tuple[bool, str]:
    """Point default_provider at an existing profile."""
    name = (name or "").strip()
    if not name:
        return False, "provider name is required"
    if name not in (config.data.get("providers") or {}):
        return False, f"unknown provider '{name}'"
    try:
        config.set_default_provider(name)
    except (KeyError, OSError) as exc:
        return False, str(exc)
    return True, f"default provider is now '{name}'"


def probe(config, name: str) -> Tuple[bool, str]:
    """Live "Test" result for a provider dialog. Never raises."""
    name = (name or "").strip()
    if name not in (config.data.get("providers") or {}):
        return False, f"unknown provider '{name}'"
    try:
        ok, detail = configtool.test_provider(config, name)
    except Exception as exc:
        # str() first: an exception object is always truthy, so `exc or ...`
        # would print an empty reason for e.g. RuntimeError().
        return False, f"test failed: {str(exc) or exc.__class__.__name__}"
    return bool(ok), str(detail)
