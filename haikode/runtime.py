"""
Wiring shared by every front-end (REPL, TUI, desktop worker):
resolve the project's configuration, build a provider from it, then build an
Agent around the two.

This module is the single place where haikode.json (and opencode.json) stops
being data and starts changing behaviour. It never prints: a broken project
file degrades to a warning on the returned Agent, and the front-end decides
whether the user sees it.

It is also where a credential and an endpoint finally meet, in
build_provider(). Everything below that line treats the merged configuration as
containing whatever haikode.json the checkout shipped: routing and credential
fields are read back out of the user's own Config unless the user has trusted
the project, and a provider name that the user never configured resolves to
nothing rather than to "whichever provider happens to be first".
"""

import copy
from typing import Any, Dict, List, Optional, Tuple

import atexit

from .agent import Agent
from .agents import AgentRegistry
from .config import Config
from .lsp import LSPManager
from .mcp import MCPManager
from .permission import Permissions
from .projectconfig import PRIVILEGED_PROVIDER_FIELDS, ProjectConfig
from .providers.anthropic import AnthropicProvider
from .providers.base import Provider
from .providers.openai_compat import OpenAICompatProvider
from .tool import REGISTRY

DEFAULT_CONTEXT = 128000


class SessionConfig:
    """A Config-shaped view of the global config merged with the project's.

    Everything that reads settings (`build_provider`, `Permissions`, the model
    dialogs) takes a config object and reads `.data`, so the merge is delivered
    as a config rather than as a pile of keyword arguments.

    Writes are the interesting half. `Permissions.persist()` calls save() after
    an "always" grant, and saving the merged data would copy the project's
    rules — possibly a checked-in repository's rules — into the user's global
    config for good. So save() writes back only the rules that were *added*
    during the session, onto the user's own config.
    """

    def __init__(self, base: Config, data: Dict[str, Any],
                 overlay: Optional[Dict[str, Any]] = None,
                 trusted: bool = False):
        self.base = base
        self.data = data
        self.path = getattr(base, "path", None)
        self._overlay = copy.deepcopy(overlay or {})
        # Whether the user vouched for the project whose settings are merged in
        # here. Defaults to False so a caller that forgets gets the safe half.
        self.trusted = trusted is True

    def get_provider(self, name: Optional[str] = None) -> Dict[str, Any]:
        """The merged record for `name`, or {} when there is no such provider.

        Descriptive fields (model, context) may legitimately come from the
        project. There is deliberately no "first provider" fallback: `-p typo`
        used to resolve to whichever provider happened to be first in the dict
        and quietly send the request — and a key — there.
        """
        providers = self.data.get("providers") or {}
        selected = name or self.data.get("default_provider", "")
        provider = providers.get(selected)
        return provider if isinstance(provider, dict) else {}

    def routing(self, name: str) -> Dict[str, Any]:
        """Where a request for `name` goes and which credential rides with it.

        The merged view contains whatever the checkout shipped and
        build_provider() is about to pair it with a globally stored API key, so
        the privileged fields are taken from the user's own config and the
        project keeps only the descriptive ones. A provider the user never
        configured has no routing at all, which is what makes "rename your way
        into someone else's key" impossible rather than merely unlikely.
        """
        merged = self.get_provider(name)
        if self.trusted:
            return merged
        base = (self.base.data.get("providers") or {}).get(name)
        if not isinstance(base, dict):
            return {}
        safe = {key: value for key, value in merged.items()
                if key not in PRIVILEGED_PROVIDER_FIELDS}
        safe.update({key: value for key, value in base.items()
                     if key in PRIVILEGED_PROVIDER_FIELDS})
        return safe

    # Credentials never come from a project file.
    def get_api_key(self, name: str) -> str:
        return self.base.get_api_key(name)

    def key_source(self, name: str) -> str:
        return self.base.key_source(name)

    def save(self) -> None:
        rules = self.data.get("permission")
        added = _added_rules(rules if isinstance(rules, dict) else {}, self._overlay)
        if added:
            base_rules = self.base.data.setdefault("permission", {})
            for key, value in added.items():
                current = base_rules.get(key)
                if isinstance(current, dict) and isinstance(value, dict):
                    current.update(value)
                else:
                    base_rules[key] = value
        self.base.save()


def _added_rules(current: Dict[str, Any],
                 overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Permission rules in `current` that the project overlay did not put there."""
    added: Dict[str, Any] = {}
    for key, value in current.items():
        known = overlay.get(key)
        if isinstance(value, dict):
            patterns = {pattern: decision for pattern, decision in value.items()
                        if not (isinstance(known, dict)
                                and known.get(pattern) == decision)}
            if patterns:
                added[key] = patterns
        elif value != known:
            added[key] = value
    return added


def load_project(config: Optional[Config], cwd: str = ".",
                 trusted: Optional[bool] = None) -> ProjectConfig:
    """Read the project configuration for `cwd`, never raising.

    A malformed haikode.json must not stop the CLI from starting, so an
    unreadable tree degrades to an empty ProjectConfig carrying the reason in
    `.errors` — exactly the shape the caller would have got from a file that
    merely failed validation.

    `trusted` overrides the stored per-repository decision, for a front-end
    that has just asked the user and does not want to record the answer.
    Omitted, the trust store decides; the fallback is always untrusted.
    """
    try:
        return ProjectConfig.load(cwd, config, trusted=trusted)
    except Exception as e:                       # unreadable dir, bad symlink
        broken = ProjectConfig(cwd, config, trusted=False)
        broken.errors.append(f"project config could not be loaded: {e}")
        return broken


def project_warnings(project: ProjectConfig) -> List[str]:
    """Everything a front-end should tell the user about the project config.

    Refusals come first on purpose. They are the only entries that mean "a
    checked-out repository tried to change where your credentials go", so a
    front-end that shows a truncated list must show those.
    """
    trusted = getattr(project, "trusted", False) is True
    warnings = [f"untrusted project config: {r.message}"
                for r in getattr(project, "refusals", [])]
    warnings += [f"config error: {entry}" for entry in project.errors]
    warnings += [f"unknown config key: {entry}" for entry in project.unknown]
    warnings += [f"config: {entry}" for entry in getattr(project, "warnings", [])]
    try:
        for escalation in project.escalations():
            # An agent-scoped rule is reported and nothing more: it lives in the
            # agents dict, which the permission ruleset here never produces.
            verb = "applied" if trusted or escalation.agent else "refused"
            warnings.append(f"permission escalation ({verb}): {escalation.message}")
    except Exception as e:
        # Reported, never swallowed: this check is the only thing standing
        # between a checked-out repository's `bash: allow` and a silent
        # widening of the user's permissions, so it failing is itself news.
        warnings.append(f"permission escalation check failed: {e}")
    return warnings


def effective_config(config: Config, cwd: str = ".",
                     project: Optional[ProjectConfig] = None
                     ) -> Tuple[SessionConfig, ProjectConfig]:
    """The settings this directory actually runs under, plus their source."""
    project = project if project is not None else load_project(config, cwd)
    try:
        merged = project.merged_with(config)
    except Exception as e:
        project.errors.append(f"project config could not be merged: {e}")
        merged = copy.deepcopy(config.data)
    overlay = merged.get("permission")
    return SessionConfig(config, merged,
                         overlay if isinstance(overlay, dict) else {},
                         trusted=getattr(project, "trusted", False) is True
                         ), project


def _routing(config: Any, name: str) -> Dict[str, Any]:
    """The provider record that decides where a request goes.

    A SessionConfig answers with the user's own endpoint and credential fields
    (see SessionConfig.routing); anything else already *is* the user's own
    config and answers for itself. An unknown name resolves to {} either way —
    Config.get_provider() would fall back to the first configured provider,
    which turns `-p typo` into a request to an endpoint nobody asked for.
    """
    router = getattr(config, "routing", None)
    if callable(router):
        return router(name)
    providers = (getattr(config, "data", None) or {}).get("providers") or {}
    record = providers.get(name)
    return record if isinstance(record, dict) else {}


def build_provider(config: Any, name: Optional[str] = None) -> Provider:
    """The provider client for `name`.

    Raises ValueError for a name the user has not configured, rather than
    guessing: the very next thing that happens to the returned object is that a
    credential is sent to its base_url.
    """
    selected = name or config.data.get("default_provider", "") or "ollama"
    prov = _routing(config, selected)
    if not prov:
        source = _credential_source(config)
        known = ", ".join(sorted((getattr(source, "data", None) or {})
                                 .get("providers") or {})) or "none"
        raise ValueError(
            f"unknown provider '{selected}' (configured: {known})")
    dialect = prov.get("dialect", "openai")

    if dialect in ("chatgpt", "supergrok"):
        from .oauth import OAuthStore
        from .providers.subscription import (ChatGPTSubscriptionProvider,
                                             SuperGrokSubscriptionProvider)
        store = OAuthStore.for_config(_credential_source(config))
        cls = (ChatGPTSubscriptionProvider if dialect == "chatgpt"
               else SuperGrokSubscriptionProvider)
        return cls(store, prov.get("base_url", ""))

    key = config.get_api_key(selected)
    if dialect == "anthropic":
        return AnthropicProvider(
            base_url=prov.get("base_url", "https://api.anthropic.com"), api_key=key)
    return OpenAICompatProvider(
        base_url=prov.get("base_url", ""), api_key=key, name=selected)


def _credential_source(config: Any) -> Any:
    """The real Config behind a merged view — OAuth tokens are never per-project."""
    return getattr(config, "base", config)


def build_agent(config: Config, provider_name: str = "", cwd: str = ".",
                permissions: Optional[Permissions] = None,
                tool_names: Optional[List[str]] = None,
                agent_name: str = "",
                model: str = "",
                reasoning_effort: str = "",
                project: Optional[ProjectConfig] = None,
                asker: Any = None,
                auto_approve: bool = False) -> Agent:
    """Build the agent this directory's configuration describes.

    Layering, weakest first: the global config, the project config chain, the
    selected agent. Each layer may narrow the tool list and tighten (or, for a
    config the user wrote themselves, loosen) the permissions.
    """
    session_config, project = effective_config(config, cwd, project)
    data = session_config.data
    warnings: List[str] = []

    selected = provider_name or data.get("default_provider", "") or "ollama"
    prov = session_config.get_provider(selected)

    registry = AgentRegistry.load(cwd, data)
    warnings.extend(registry.warnings)
    resolved_agent = agent_name or data.get("default_agent", "") or ""
    if resolved_agent and registry.get(resolved_agent) is None:
        warnings.append(f"unknown agent '{resolved_agent}', using "
                        f"{registry.default().name}")
        resolved_agent = ""

    # The project's tools map is applied here rather than inside the Agent so
    # that an agent switch can never re-enable a tool the project disabled.
    all_names = list(REGISTRY)
    allowed = list(tool_names) if tool_names is not None else all_names
    try:
        enabled = project.enabled_tools(allowed)
    except Exception as e:
        warnings.append(f"config tools: {e}")
        enabled = allowed

    if permissions is None:
        permissions = Permissions(config=session_config, asker=asker,
                                  auto_approve=auto_approve)
    else:
        # A caller-supplied Permissions keeps its asker but adopts the merged
        # rules, or the project's permission block would be ignored entirely.
        permissions.config = session_config

    try:
        instructions = project.resolve_instructions()
    except Exception as e:
        warnings.append(f"config instructions: {e}")
        instructions = []

    # Collected last, and deliberately not before: resolve_instructions() is
    # what records "this entry pointed outside the project and was ignored",
    # so snapshotting the project's warnings any earlier drops precisely the
    # warning that says a checked-out repository tried to read ~/.ssh/id_rsa
    # into the system prompt.
    warnings = project_warnings(project) + warnings

    client = build_provider(session_config, selected)
    resolved_model = model or prov.get("model", "")
    effort = reasoning_effort or str(prov.get("reasoning_effort") or "")
    if effort:
        setter = getattr(client, "set_reasoning_effort", None)
        if callable(setter):
            try:
                setter(effort, resolved_model)
            except ValueError as e:
                # A provider switch must never die on an effort chosen for
                # the previous provider: the choice is kept in the session
                # override and reapplies where it is supported.
                warnings.append(f"reasoning effort: {e}")
    configured_context = _int(prov.get("context"), DEFAULT_CONTEXT)
    context_limit = getattr(client, "context_limit", None)
    if callable(context_limit):
        context_window, context_source = context_limit(
            resolved_model, configured_context)
    else:
        # Embedders historically only had to supply stream(). Keep that
        # protocol viable while native providers can expose authoritative
        # model metadata.
        context_window, context_source = configured_context, "configuration"

    context_window, context_source = _model_context(
        session_config, selected, prov, resolved_model,
        context_window, context_source)

    agent = Agent(
        provider=client,
        model=resolved_model,
        permissions=permissions,
        cwd=cwd,
        max_steps=_optional_int(data.get("max_steps")),
        context_window=context_window,
        context_source=context_source,
        context_default=configured_context,
        # `input` in a profile pins what one prompt may be — the input share
        # of the window, which is what requests are actually refused on.
        input_override=_int(prov.get("input"), 0),
        tool_names=enabled,
        agent_name=resolved_agent,
        registry=registry,
        project=project,
        instructions=instructions,
        warnings=warnings,
    )
    # Diagnostics after edit/write are switched on by this one assignment:
    # the tools already read ctx.lsp (tool/diagnostics.py) and did so for as
    # long as nothing ever set it. Lazy by design — no server process exists
    # until a file of a known language is actually touched, so on a machine
    # with no servers installed this costs a memoised PATH miss and nothing
    # else. `lsp: false` in the config opts out entirely.
    agent.ctx.lsp = LSPManager.from_config(session_config, cwd)

    # MCP is the one extensibility path on an OS with no pip: a configured
    # server's tools join the agent's set, behind the "mcp" permission key.
    # start_all() is budgeted and never raises — a broken third-party server
    # degrades to a status stand-in, not a broken agent. With no `mcp` block
    # the cost is a dict lookup.
    if (session_config.data or {}).get("mcp"):
        manager = MCPManager(session_config, cwd)
        manager.start_all()
        agent.attach_mcp(manager)
        agent.warnings = list(agent.warnings) + list(manager.warnings)
        # Servers this process started die with it, like the LSP ones.
        atexit.register(manager.shutdown_all)
    return agent


def _model_context(config: Config, provider: str, prov: dict, model: str,
                   window: int, source: str):
    """Narrow the window from "this provider" to "this model".

    A provider profile holds one `context` for every model it offers, which
    is wrong the moment two of them differ: Kimi's k3-256k was metered
    against 128000 when the endpoint states 262144, and grok-4.5 against
    131072 when xAI states 500000 — both undercounts, so compaction fired
    early and the meter lied.

    Three sources, most specific first:
      `model_context` in the provider profile — the user's own word;
      what the endpoint said when its models were last listed;
      whatever the provider or the profile already decided.

    A provider that reports an authoritative window of its own (the ChatGPT
    backend profile) is left alone: it knows something /models does not.
    """
    override = (prov.get("model_context") or {}) if isinstance(prov, dict) else {}
    if isinstance(override, dict):
        configured = _int(override.get(model), 0)
        if configured:
            return configured, "configured for this model"
    if source != "configuration":
        return window, source
    try:
        from .models import ModelCatalog
        declared = ModelCatalog(config).context_for(provider, model)
    except Exception:
        return window, source
    if declared and declared != window:
        return declared, "endpoint metadata"
    return window, source


def _int(value: Any, fallback: int) -> int:
    """A config value that must be a positive int, or the built-in default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _optional_int(value: Any) -> Optional[int]:
    """A positive configured limit, or None for the normal unlimited loop."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def provider_status(config: Any, name: str) -> str:
    """One-line auth status used by doctor and the UIs.

    Read through _routing() so a project config cannot make the status line
    describe an auth setup that is not the one a request would actually use.
    """
    prov = _routing(config, name)
    if prov.get("oauth_provider"):
        try:
            from .oauth import OAuthStore
            return f"oauth: {OAuthStore.for_config(_credential_source(config)).status(name)}"
        except Exception as e:
            return f"oauth: unavailable ({e})"
    if not prov.get("requires_key", True):
        return "no key required"
    return f"key: {config.key_source(name)}"
