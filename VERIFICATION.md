# hai completion and verification matrix

Last updated: 2026-07-14. This file records executable evidence, not intended
behavior. Account-dependent checks remain partial until the account owner
completes the provider's device authorization.

| Requirement | Current evidence | Status |
|---|---|---|
| Standalone CLI on Haiku | Installed at `/boot/home/config/non-packaged/bin/hai`; no OpenCode client, server profile, tunnel script, Node, or Bun runtime remains. Native routing audit returned `HAI_NATIVE_STANDALONE_ROUTING_OK`. | PASS |
| Local agent/runtime | REPL, ReAct loop, tools, session state, provider clients, OAuth polling, and refresh are Python processes launched on Haiku. | PASS |
| OpenCode-compatible subscription protocols | ChatGPT headless device OAuth and Codex Responses SSE, plus xAI RFC 8628 and bearer chat, follow the current upstream contracts. Native contract tests cover all request/response branches used by hai. | PASS |
| Direct API keys | Hidden CLI input, validation, mode-0600 fallback, native BKeyStore helper, and stdin-only desktop transport are implemented and tested. | PASS |
| ChatGPT subscription OAuth | Local initiation, polling, exchange, JWT account extraction, refresh rotation, mode-0600 persistence, model listing, and Responses streaming tests pass natively. A real account login still requires the user to authorize a device code. | PARTIAL |
| SuperGrok subscription OAuth | Local RFC 8628 initiation/polling/backoff, refresh rotation, mode-0600 persistence, model listing path, and bearer chat tests pass natively. A real account login still requires the user. | PARTIAL |
| Ollama Cloud | Direct provider path is installed locally; earlier native live CLI and desktop runs returned `HAI_OLLAMA_OK` and `HAI_NATIVE_LIVE_OK`. Current post-reboot BKeyStore status was `none` in the headless audit, so no new billed live call was made. | PARTIAL |
| Ollama local/LAN/Tailscale | Configurable URL/model/no-key profiles and native keyless OpenAI-compatible SSE contract test pass. No reachable real LAN Ollama endpoint was available for this run. | PARTIAL |
| Custom providers | `list/add/remove/default`, desktop Add bridge, validation, persistence, and isolation tests pass. Only local direct `openai` or `anthropic` dialects are accepted; the former external-server dialect is rejected. | PASS |
| Native desktop build | Current Settings and worker revision compiled, linked, received resources, and installed on Haiku. | PASS |
| Local desktop worker | Native smoke emitted `started`, `HAI_STANDALONE_WORKER_OK`, and `completed`; it launches no localhost or remote agent server. | PASS |
| Install | Source installer built and installed CLI, BKeyStore helper, and desktop app on Haiku. HPKG packaging is not yet provided. | PARTIAL |

## Automated checks

```sh
python3 -m compileall -q hai tests
HAI_DISABLE_KEYSTORE=1 python3 -m unittest discover -s tests -v
sh -n scripts/install-on-haiku.sh scripts/hai-launcher
```

The suite contains 20 tests. It covers provider/config isolation, custom
providers, secret stdin transport, both local device OAuth flows, pending and
refresh behavior, refresh-token rotation, private token-file permissions,
ChatGPT account-ID extraction, ChatGPT Responses SSE, ChatGPT model-list
shape, SuperGrok bearer chat, migration away from the former tunnel profile,
keyless Ollama SSE, desktop framing and permission decisions, multi-turn
reuse, and SQLite session metadata.

## Native acceptance performed

One non-multiplexed SSH connection performed the complete deployment and
acceptance chain on `shredder`; it closed after the final assertion. No reverse
forward, ControlMaster, or background SSH process was created by the run.

```sh
cd /boot/home/hai
python3 -m compileall -q hai tests
HAI_DISABLE_KEYSTORE=1 python3 -m unittest discover -s tests -v
sh scripts/install-on-haiku.sh
hai doctor
hai provider list
```

Results:

- 20/20 Python tests passed natively.
- `hai-keystore` and the BeAPI app compiled and installed.
- The installed profile list contains local `chatgpt` and `supergrok`
  dialects and no `opencode` server profile.
- The old client module and `serve-for-haiku.sh` are absent.
- Local desktop-worker and provider-class routing assertions passed.

Remaining acceptance requiring user/external state: complete one real ChatGPT
device login, complete one real SuperGrok device login, and test a reachable
real Ollama LAN/Tailscale endpoint. These are not replaced by claims based only
on mocked credentials.

