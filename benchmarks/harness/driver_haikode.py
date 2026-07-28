#!/usr/bin/env python3
"""
One-turn driver for the `haikode` runner.

Run as a subprocess so the harness can enforce a hard timeout and kill the
whole process group. It builds the agent through `haikode.runtime.build_agent`
— the same call `haikode`'s REPL, TUI and desktop worker all make — and writes
structured JSONL events, because the CLI's human-readable transcript does not
report token counts.

This driver deliberately does **not** reimplement any agent behaviour: system
prompt, tools, permissions, provider clients and the loop all come from the
installed package. What it skips relative to `python3 -m haikode "…"` is
argparse, terminal rendering and the sqlite session write. Use
`--haikode-mode cli` in the harness to exercise that path instead.

Events (one JSON object per line, to --events):

    {"type":"start",  "provider":…, "model":…, "agent":…, "tools":[…]}
    {"type":"tool",   "name":…, "args":{…}}
    {"type":"tool_result", "name":…, "title":…}
    {"type":"tool_denied"|"tool_error", "name":…, …}
    {"type":"result", "text":…, "tokens":{"input":n,"output":n}, "steps":n}
    {"type":"error",  "error":…, "traceback":…}
"""

import argparse
import inspect
import json
import os
import sys
import time
import traceback


def main() -> int:
    parser = argparse.ArgumentParser(prog="driver_haikode")
    parser.add_argument("--repo", required=True,
                        help="directory containing the haikode package")
    parser.add_argument("--cwd", required=True, help="the agent's project directory")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--agent", default="", help="named agent, e.g. plan")
    parser.add_argument("--events", required=True, help="JSONL output path")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--no-auto-approve", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, args.repo)
    events = open(args.events, "w", buffering=1)

    def emit(kind, **payload):
        payload["type"] = kind
        payload["t"] = round(time.time(), 3)
        events.write(json.dumps(payload, default=str) + "\n")

    try:
        from haikode.config import Config
        from haikode.permission import Permissions
        from haikode.runtime import build_agent
    except Exception as e:
        emit("error", error="cannot import haikode from %s: %s" % (args.repo, e),
             traceback=traceback.format_exc())
        events.close()
        return 3

    try:
        config = Config()
        provider = args.provider or config.data.get("default_provider", "")
        providers = config.data.setdefault("providers", {})
        if provider not in providers:
            emit("error", error="unknown provider %r (have: %s)"
                 % (provider, ", ".join(sorted(providers))))
            events.close()
            return 4
        if args.model:
            providers[provider]["model"] = args.model
        if args.max_steps:
            config.data["max_steps"] = args.max_steps

        # haikode is under active development and build_agent()'s signature has
        # grown. Pass only what this build actually accepts, and fall back to
        # wiring the named agent by hand on older builds, so the benchmark
        # measures the engine rather than a version skew in the harness.
        accepted = set(inspect.signature(build_agent).parameters)
        kwargs = {}
        agent_wired_by_hand = False
        if args.agent and "agent_name" in accepted:
            kwargs["agent_name"] = args.agent
        if args.agent and "agent_name" not in accepted:
            agent_wired_by_hand = True
        if args.model and "model" in accepted:
            kwargs["model"] = args.model

        permissions = Permissions(config=config, asker=None,
                                  auto_approve=not args.no_auto_approve)
        tool_names = None
        defn = None
        if agent_wired_by_hand and args.agent != "build":
            from haikode.agents import AgentPermissions, AgentRegistry
            from haikode.tool import REGISTRY as TOOL_REGISTRY
            registry = AgentRegistry.load(args.cwd, config.data)
            defn = registry.get(args.agent)
            if defn is None:
                emit("error", error="unknown agent %r (have: %s)"
                     % (args.agent, ", ".join(registry.names())))
                events.close()
                return 5
            tool_names = AgentRegistry.resolve_tools(defn, sorted(TOOL_REGISTRY))
            permissions = Permissions(config=AgentPermissions(defn, config),
                                      asker=None,
                                      auto_approve=not args.no_auto_approve)

        if "permissions" in accepted:
            kwargs["permissions"] = permissions
        if tool_names is not None and "tool_names" in accepted:
            kwargs["tool_names"] = tool_names
        agent = build_agent(config, provider, args.cwd, **kwargs)

        if defn is not None:
            if defn.prompt:
                agent.system_prompt = (agent.system_prompt or "") + "\n\n" + defn.prompt
            _, model_id = defn.model_parts()
            if model_id:
                agent.model = model_id
            if defn.steps:
                agent.max_steps = defn.steps

        selected = getattr(agent, "agent_name", "") or (
            defn.name if defn is not None else "build")
        if args.agent and selected != args.agent:
            emit("error", error="asked for agent %r but the engine selected %r"
                 % (args.agent, selected))
            events.close()
            return 5
        # Never let the report claim a model the run did not actually use.
        if args.model and agent.model != args.model:
            emit("error", error="asked for model %r but the engine selected %r"
                 % (args.model, agent.model))
            events.close()
            return 8

        emit("start", provider=provider, model=agent.model, agent=selected,
             tools=sorted(agent.tools), cwd=args.cwd,
             wired_by_hand=agent_wired_by_hand,
             auto_approve=not args.no_auto_approve)
    except Exception as e:
        emit("error", error="setup failed: %s" % e, traceback=traceback.format_exc())
        events.close()
        return 6

    prompt = open(args.prompt_file, encoding="utf-8").read()

    def on_event(kind, payload):
        if kind == "tool":
            emit("tool", name=payload.get("name"), args=payload.get("args"))
        elif kind == "tool_result":
            emit("tool_result", name=payload.get("name"),
                 title=payload.get("title"),
                 output=(payload.get("output") or "")[:2000])
        elif kind == "tool_denied":
            emit("tool_denied", name=payload.get("name"),
                 reason=payload.get("reason"))
        elif kind == "tool_error":
            emit("tool_error", name=payload.get("name"), error=payload.get("error"))
        elif kind == "limit":
            emit("limit", steps=payload.get("steps"))

    def on_text(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    try:
        final = agent.run(prompt, on_text=on_text, on_event=on_event)
    except Exception as e:
        emit("error", error="%s: %s" % (type(e).__name__, e),
             traceback=traceback.format_exc())
        events.close()
        return 7

    emit("result", text=final, tokens=dict(agent.tokens), steps=agent.steps_used)
    events.close()
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
