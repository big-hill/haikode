"""
Summary rendering and results.json.

Three rules this module exists to enforce:

  1. A task that errored is printed as ERR, never folded into "failed" and
     never dropped from the table.
  2. A runner that could not run is printed as `n/a` with the reason spelled
     out underneath — an empty column must never look like a score of zero.
  3. If the two runners did not use the same provider and model, the report
     says in words that the comparison is not a parity measurement.
"""

import json
from pathlib import Path
from typing import Dict, List

BAR = "─"


COL = 15


def _cell(entry: dict) -> str:
    if entry is None:
        return "-".rjust(COL)
    if entry.get("unavailable"):
        return "n/a".rjust(COL)
    passed, total = entry["passed_runs"], entry["runs"]
    errors = entry.get("error_runs", 0)
    if total == 0:
        return "ERR".rjust(COL)
    if errors == total:
        # Every attempt errored, so nothing was measured. Printing "0/3 0%"
        # here would read as "the agent failed three times", which it did not:
        # it never got to try.
        return ("ERR %d/%d" % (errors, total)).rjust(COL)
    text = "%d/%d %3.0f%%" % (passed, total, 100.0 * passed / total)
    if errors:
        text += " !%d" % errors
    return text.rjust(COL)


def summary_table(results: dict) -> str:
    runner_keys: List[str] = results["runners"]
    tasks: List[str] = [t["name"] for t in results["tasks"]]
    name_width = max([len("task"), len("TOTAL")] + [len(t) for t in tasks]) + 2

    lines = []
    header = "task".ljust(name_width) + "".join(k.rjust(COL) for k in runner_keys)
    lines.append(header)
    lines.append(BAR * len(header))

    for task in results["tasks"]:
        row = task["name"].ljust(name_width)
        for key in runner_keys:
            row += _cell(task["by_runner"].get(key))
        lines.append(row)

    lines.append(BAR * len(header))
    totals = results["totals"]
    row = "TOTAL".ljust(name_width)
    for key in runner_keys:
        row += _cell(totals.get(key))
    lines.append(row)
    return "\n".join(lines)


def unavailable_notes(results: dict) -> str:
    """Spell out every `n/a` cell. An unexplained gap is indistinguishable from a zero."""
    lines = []
    for task in results["tasks"]:
        for key in results["runners"]:
            entry = task["by_runner"].get(key) or {}
            if entry.get("unavailable"):
                lines.append("  n/a  %-26s %-9s %s"
                             % (task["name"], key, entry["unavailable"]))
    if not lines:
        return ""
    return "not measured (this is not a score of zero):\n" + "\n".join(lines)


def cost_table(results: dict) -> str:
    """What each run cost and how it behaved: time, tokens, tool calls."""
    runner_keys = results["runners"]
    name_width = max([len("task")] + [len(t["name"]) for t in results["tasks"]]) + 2
    lines = ["%s%s" % ("task".ljust(name_width),
                       "".join(("%s: s / tok-in / tok-out / tools" % k).ljust(42)
                               for k in runner_keys))]
    lines.append(BAR * (name_width + 42 * len(runner_keys)))
    for task in results["tasks"]:
        row = task["name"].ljust(name_width)
        for key in runner_keys:
            entry = task["by_runner"].get(key) or {}
            if entry.get("unavailable") or not entry.get("runs"):
                row += "n/a".ljust(42)
                continue
            runs = entry["runs"]

            def per_run(value):
                return "-" if value is None else str(int(value / runs))

            row += ("%6.0f  %8s  %8s  %6s"
                    % (entry["mean_wall_s"], per_run(entry.get("tokens_in")),
                       per_run(entry.get("tokens_out")),
                       per_run(entry.get("tool_calls")))).ljust(42)
        lines.append(row.rstrip())
    lines.append("(per run, averaged over the repeats; `-` means the runner does "
                 "not report that number)")
    return "\n".join(lines)


def tool_usage(results: dict) -> str:
    """Tool-call histogram by name, per runner — how each engine works, not just whether."""
    totals = {}
    for record in results.get("runs", []):
        bucket = totals.setdefault(record["runner"], {})
        for name, count in (record.get("tools") or {}).items():
            bucket[name] = bucket.get(name, 0) + count
    lines = []
    for key in results["runners"]:
        counts = totals.get(key)
        if not counts:
            lines.append("  %-9s (no tool calls recorded)" % key)
            continue
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("  %-9s %s" % (key, ", ".join("%s=%d" % kv for kv in ordered)))
    return "\n".join(lines)


def check_table(results: dict) -> str:
    """Per-check pass rate, so a task that "fails" shows what part failed."""
    lines = []
    for task in results["tasks"]:
        lines.append("")
        lines.append("%s — %s" % (task["name"], task.get("proves", "")))
        for key in results["runners"]:
            entry = task["by_runner"].get(key)
            if entry is None:
                continue
            if entry.get("unavailable"):
                lines.append("  %-9s n/a — %s" % (key, entry["unavailable"]))
                continue
            lines.append("  %s" % key)
            for label, stats in entry.get("checks", {}).items():
                mark = "ok  " if stats["passed"] == stats["runs"] else "FAIL"
                if stats["errors"]:
                    mark = "ERR "
                lines.append("    %s %-52s %d/%d  %s"
                             % (mark, label[:52], stats["passed"], stats["runs"],
                                stats.get("last_detail", "")[:90]))
            for note in entry.get("errors", [])[:4]:
                lines.append("    !!   %s" % note[:150])
    return "\n".join(lines)


def preamble(results: dict) -> str:
    lines = []
    lines.append("haikode parity benchmark — %s" % results["started"])
    lines.append("repeats per task: %d" % results["repeat"])
    for key in results["runners"]:
        info = results["runner_info"][key]
        if info.get("unavailable"):
            lines.append("  %-9s UNAVAILABLE — %s" % (key, info["unavailable"]))
        else:
            ident = ", ".join("%s=%s" % (k, v) for k, v in info["identity"].items()
                              if v and k != "runner")
            lines.append("  %-9s %s" % (key, ident))
            lines.append("  %-9s models used: %s" % (
                "", ", ".join(sorted(info.get("models_used") or [])) or "(none yet)"))
    for warning in results.get("warnings", []):
        lines.append("  ! %s" % warning)
    return "\n".join(lines)


def comparison_note(results: dict) -> str:
    """Say out loud whether the two columns can be compared at all."""
    available = [k for k in results["runners"]
                 if not results["runner_info"][k].get("unavailable")]
    if len(available) < 2:
        return ("Only one runner ran, so this is a capability report for that "
                "runner, not a parity measurement.")
    models = {k: sorted(results["runner_info"][k].get("models_used") or [])
              for k in available}
    stripped = {k: sorted(m.split("/")[-1] for m in v) for k, v in models.items()}
    values = list(stripped.values())
    if all(v == values[0] for v in values) and values[0]:
        return ("Both runners used the same underlying model (%s), so the columns "
                "are comparable. Model output is stochastic: treat differences "
                "smaller than one run in %d as noise."
                % (", ".join(values[0]), results["repeat"]))
    return ("WARNING: the runners did not use the same model (%s). A comparison "
            "across different models is NOT a parity measurement — rerun with a "
            "single --model that both runners can serve."
            % "; ".join("%s=%s" % (k, ",".join(v) or "?") for k, v in models.items()))


def write_results(path: Path, results: dict) -> None:
    path.write_text(json.dumps(results, indent=2, sort_keys=False, default=str))


def render(results: dict, verbose: bool = False) -> str:
    parts = [preamble(results), "", summary_table(results), "",
             "legend: passed/total runs, then pass rate. `!n` = n of those runs "
             "errored. `ERR n/n` = every run errored, so nothing was measured. "
             "n/a = the runner or a prerequisite was unavailable."]
    gaps = unavailable_notes(results)
    if gaps:
        parts += ["", gaps]
    parts += ["", comparison_note(results),
              "", cost_table(results),
              "", "tool calls by name, whole suite:", tool_usage(results)]
    if verbose:
        parts += ["", "per-check detail:", check_table(results)]
    if results.get("orphan_process_groups"):
        parts += ["", "WARNING: process groups still alive after the run: %s"
                  % results["orphan_process_groups"]]
    else:
        parts += ["", "no orphan process groups survived the run."]
    return "\n".join(parts)
