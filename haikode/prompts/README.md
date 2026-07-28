# haikode prompt library

`haikode` does not use a single system prompt. Like opencode, it picks a prompt
tuned to the model family that is about to read it, because the families were
trained with very different instruction-following habits (Anthropic models want
terse policy, GPT models want an explicit workspace contract, Gemini wants
worked examples). `haikode/prompt.py` owns the selection.

## Provenance

The following files are ported from **opencode** (MIT licensed,
<https://github.com/anomalyco/opencode>), from
`packages/opencode/src/session/prompt/`:

| haikode file       | opencode source            |
| ------------------ | -------------------------- |
| `anthropic.md`     | `anthropic.txt`            |
| `beast.md`         | `beast.txt`                |
| `codex.md`         | `codex.txt`                |
| `gemini.md`        | `gemini.txt`               |
| `gpt.md`           | `gpt.txt`                  |
| `kimi.md`          | `kimi.txt`                 |
| `meta.md`          | `meta.txt`                 |
| `trinity.md`       | `trinity.txt`              |
| `plan.md`          | `plan.txt`                 |
| `plan-mode.md`     | `plan-mode.txt`            |
| `build-switch.md`  | `build-switch.txt`         |

The model-family selection order in `haikode/prompt.py` is ported from
`packages/opencode/src/session/prompt/../system.ts` (`SystemPrompt.provider`).

Changes made to the ported texts are deliberately minimal:

1. The product name `opencode` / `OpenCode` became `haikode`.
2. The "ask the docs site" and "file an issue" paragraphs were rewritten, since
   haikode has no documentation site to `webfetch` — the model is told to read
   the local config and source instead.
3. The `# Haiku OS` section (see `haiku.md`) is appended to every variant.

## Not ported from opencode

`system.md` is haikode's own default variant. It predates this library and
already reads like `default.txt`, so `default.txt` was not copied on top of it;
its "Code references" and `<system-reminder>` paragraphs were back-ported from
`default.txt` because nothing else told the model about either convention.
`haiku-pack.md` is haikode's own optional deep-dive on Haiku APIs and is not an
opencode file.

`copilot-gpt-5.txt` and `plan-reminder-anthropic.txt` exist upstream but are
imported by nothing in opencode, so they were not ported either.

`plan-mode.md` still carries opencode's literal `${planInfo}` placeholder;
substitute it with `prompt.plan_mode(plan_info)` rather than loading the file
directly.

## The Haiku section

`haiku.md` is the single source of truth for the `# Haiku OS` block. It is
copied verbatim into every variant file so that `prompt.load(name)` shows the
model exactly what it will be sent, and `prompt.select_prompt()` re-appends it
if a variant file is ever missing it.

## opencode license

opencode is distributed under the MIT License. The ported prompt texts remain
under that license; see the upstream repository for the full text.
