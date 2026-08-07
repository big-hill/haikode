#!/usr/bin/env python3
"""
haikode parity benchmark.

    python3 benchmarks/run.py --runner haikode --provider zen \
        --model deepseek-v4-flash-free --repeat 3
    python3 benchmarks/run.py --compare --task fix-failing-test
    python3 benchmarks/run.py --validate      # fixtures only, no model calls
    python3 benchmarks/run.py --list

Scores the same task suite against `haikode` and against the real `opencode`
binary and prints one number per task per runner: how many of N runs passed
every check.

Design rules, in priority order:
  * an error is an error — never a silent skip, never a quiet zero
  * a runner that cannot run says why, in words
  * every run gets a fresh copy of the fixture and a hard timeout
  * the exact provider and model per run are recorded, because a comparison
    across two different models is not a parity measurement
"""

import argparse
import datetime
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from harness import checks as checks_mod            # noqa: E402
from harness import procs, report, runners, sandbox as sandbox_mod, tasks as tasks_mod  # noqa: E402

DEFAULT_TASKS_DIR = HERE / "tasks"
DEFAULT_RESULTS_DIR = HERE / "results"


# --------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="benchmarks/run.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    parser.add_argument("--runner", action="append", default=[],
                        choices=sorted(runners.RUNNERS),
                        help="runner to score (repeatable). Default: haikode")
    parser.add_argument("--compare", action="store_true",
                        help="score every selected task on both runners and diff them")
    parser.add_argument("--task", action="append", default=[],
                        help="task name to run (repeatable). Default: all")
    parser.add_argument("--category", action="append", default=[],
                        help="only tasks in this category (repeatable)")
    parser.add_argument("--provider", default="",
                        help="override the provider profile named in task.json")
    parser.add_argument("--model", default="",
                        help="override the model named in task.json")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="run each task N times and report the pass rate "
                             "(models are stochastic; one sample is not a result)")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="override every task's wall-clock timeout, seconds")
    parser.add_argument("--pause", type=float, default=0.0, metavar="S",
                        help="sleep S seconds between runs. Free tiers rate-limit; "
                             "a 429 is reported as an error, but pacing turns it "
                             "into a measurement instead of a coin flip")
    parser.add_argument("--haikode-mode", choices=("driver", "cli"), default="driver",
                        help="driver: harness/driver_haikode.py (reports tokens). "
                             "cli: python3 -m haikode --yes (the real entry point)")
    parser.add_argument("--haikode-repo", default="",
                        help="directory containing the haikode package")
    parser.add_argument("--opencode-bin", default="",
                        help="path to the opencode binary")
    parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--work-dir", default="",
                        help="where sandboxes live. Default: a temp dir, removed at exit")
    parser.add_argument("--keep-sandbox", action="store_true",
                        help="do not delete the per-run sandboxes (for debugging)")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--validate", action="store_true",
                        help="set every fixture up and run its pre_checks only — "
                             "no model is called. Proves the tasks are not trivially "
                             "passable and that the fixtures still work.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the per-check breakdown")
    return parser.parse_args(argv)


def select_tasks(args):
    all_tasks = tasks_mod.discover(Path(args.tasks_dir))
    chosen = all_tasks
    if args.task:
        wanted = set(args.task)
        known = {t.name for t in all_tasks}
        missing = wanted - known
        if missing:
            raise SystemExit("unknown task(s): %s\nknown: %s"
                             % (", ".join(sorted(missing)), ", ".join(sorted(known))))
        chosen = [t for t in chosen if t.name in wanted]
    if args.category:
        wanted = set(args.category)
        chosen = [t for t in chosen if t.category in wanted]
    return chosen


def list_tasks(task_list):
    width = max(len(t.name) for t in task_list)
    for task in task_list:
        missing = task.missing_commands()
        flag = ("  [needs %s — NOT INSTALLED]" % ", ".join(missing)) if missing else ""
        print("%-*s  %-22s %s%s" % (width, task.name, task.category,
                                    task.proves or task.description, flag))
        print("%-*s  %d turn(s), %d check(s), timeout %.0fs, model %s/%s"
              % (width, "", len(task.turns), len(task.checks), task.timeout,
                 task.provider, task.model or "(task default)"))


# --------------------------------------------------------------------------


def make_run_dir(base: Path, task_name: str, runner_key: str, repeat: int) -> Path:
    path = base / task_name / runner_key / ("run%d" % repeat)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_transcript(run_dir: Path, task, turn_results) -> None:
    lines = ["# %s" % task.name, "", task.description or "", ""]
    for turn in turn_results:
        lines += ["## turn %d — prompt" % (turn.index + 1), "", "```",
                  turn.prompt, "```", "",
                  "## turn %d — tool calls" % (turn.index + 1), ""]
        if turn.tool_calls:
            for call in turn.tool_calls:
                args = json.dumps(call.get("args"), default=str)
                lines.append("- `%s` %s" % (call.get("name"), args[:300]))
        else:
            lines.append("(none)")
        lines += ["", "## turn %d — answer" % (turn.index + 1), "", turn.text or
                  "(no final text)", ""]
        if turn.error:
            lines += ["## turn %d — ERROR" % (turn.index + 1), "", turn.error, ""]
    (run_dir / "transcript.md").write_text("\n".join(lines))


def run_once(task, runner, args, work_root: Path, results_root: Path,
             repeat_index: int, home: Path, home_baseline=None) -> dict:
    """One full attempt at one task. Never raises: failures become records."""
    run_dir = make_run_dir(results_root, task.name, runner.key, repeat_index)
    record = {
        "task": task.name,
        "runner": runner.key,
        "repeat": repeat_index,
        "status": "error",
        "checks": [],
        "turns": [],
        "errors": [],
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "results_dir": str(run_dir),
    }

    box = None
    started = time.monotonic()
    try:
        # A previous run may have written global config into the shared $HOME.
        # Put it back before this one starts, and say so if it had to.
        if home_baseline is not None:
            repaired = sandbox_mod.restore_home_config(home, home_baseline)
            # Reverting the harness's own per-task pin is bookkeeping, not
            # contamination; only what the *agent* wrote is worth a warning.
            owned = runner.owned_home_paths()
            mine = [r for r in repaired if r.split(" ", 1)[-1] in owned]
            theirs = [r for r in repaired if r not in mine]
            if mine:
                record["home_pins_reverted"] = mine
            if theirs:
                record["home_repaired"] = theirs
                print("\n  [shared $HOME config repaired before this run: %s]"
                      % "; ".join(theirs[:4]), file=sys.stderr)

        box_root = work_root / task.name / ("%s-run%d" % (runner.key, repeat_index))
        box = sandbox_mod.Sandbox.create(
            root=box_root, setup_dir=task.setup, home=home,
            outside_files=task.outside_files, git_init=task.git_init)
        record["sandbox"] = str(box.root)
        for note in box.notes:
            record["errors"].append("sandbox: %s" % note)

        env = runner.env_for(box.home, task.env)
        ctx = checks_mod.CheckContext(box=box, turns=[], target_index=0, env=env,
                                      command_timeout=task.command_timeout,
                                      ignore_globs=task.ignore)

        # 0. anything the fixture cannot express as plain files — a BFS
        #    attribute, a mode bit — plus whatever the runner needs written
        #    into the freshly restored $HOME.
        for command in task.setup_commands:
            outcome = procs.shell(command, cwd=box.project, env=env,
                                  timeout=task.command_timeout)
            record.setdefault("setup_commands", []).append(
                {"command": command, "exit_code": outcome.exit_code,
                 "stderr": outcome.stderr.strip()[-300:]})
            if not outcome.ok:
                record["status"] = "error"
                record["errors"].append(
                    "fixture setup command failed, the task was not run: `%s` "
                    "exited %s: %s" % (command, outcome.exit_code,
                                       (outcome.stderr or outcome.stdout).strip()[-300:]))
                return record
        runner.prepare(task, box)

        # 1. pre-checks: prove the fixture is in the state the task assumes.
        pre = checks_mod.evaluate(ctx, task.pre_checks) if task.pre_checks else []
        record["pre_checks"] = [r.as_dict() for r in pre]
        broken = [r for r in pre if r.status != checks_mod.PASS]
        if broken:
            record["status"] = "error"
            record["errors"].append(
                "fixture pre-check failed, the task was not run: "
                + "; ".join("%s (%s)" % (r.label, r.detail) for r in broken))
            return record

        # 2. snapshots taken *after* the pre-checks so their artefacts
        #    (__pycache__, build/) do not read as agent edits.
        ctx.before = sandbox_mod.snapshot(box.project, task.ignore)
        ctx.outside_before = sandbox_mod.snapshot(box.outside)
        ctx.home_before = sandbox_mod.snapshot(box.home / "Documents")

        # 3. the turns.
        turn_results = []
        for turn in task.turns:
            result = runner.run_turn(task, turn, box, run_dir)
            turn_results.append(result)
            (run_dir / ("stdout-%d.txt" % (turn.index + 1))).write_text(result.stdout)
            (run_dir / ("stderr-%d.txt" % (turn.index + 1))).write_text(result.stderr)
            if result.error:
                record["errors"].append("turn %d: %s" % (turn.index + 1, result.error))
            if result.timed_out:
                break
        ctx.turns = turn_results
        record["turns"] = [t.as_dict() for t in turn_results]
        write_transcript(run_dir, task, turn_results)

        # 4. after-state and the real checks.
        ctx.after = sandbox_mod.snapshot(box.project, task.ignore)
        ctx.outside_after = sandbox_mod.snapshot(box.outside)
        ctx.home_after = sandbox_mod.snapshot(box.home / "Documents")

        change = sandbox_mod.diff_snapshots(ctx.before, ctx.after)
        (run_dir / "changes.json").write_text(json.dumps(change, indent=2))
        (run_dir / "changes.diff").write_text(
            sandbox_mod.unified_changes(box.project, box.pristine, change))

        results = checks_mod.evaluate(ctx, task.checks)
        record["checks"] = [r.as_dict() for r in results]

        errored = [r for r in results if r.status == checks_mod.ERROR]
        failed = [r for r in results if r.status == checks_mod.FAIL]
        fatal_turn = any(t.error for t in turn_results)
        if errored:
            record["status"] = "error"
            record["errors"] += ["check error: %s (%s)" % (r.label, r.detail)
                                 for r in errored]
        elif fatal_turn:
            # The agent may still have satisfied every check — say so, but the
            # run is an error, not a clean pass.
            record["status"] = "error"
        elif failed:
            record["status"] = "fail"
        else:
            record["status"] = "pass"

        record["checks_passed"] = sum(1 for r in results if r.status == checks_mod.PASS)
        record["checks_total"] = len(results)
        record["tokens_in"] = _sum_optional(t.tokens_in for t in turn_results)
        record["tokens_out"] = _sum_optional(t.tokens_out for t in turn_results)
        record["tool_calls"] = sum(len(t.tool_calls) for t in turn_results)
        histogram = {}
        for turn in turn_results:
            for name, count in turn.tool_histogram().items():
                histogram[name] = histogram.get(name, 0) + count
        record["tools"] = histogram
        record["models_used"] = sorted({t.model for t in turn_results if t.model})
        record["providers_used"] = sorted({t.provider for t in turn_results if t.provider})
    except Exception as e:
        record["status"] = "error"
        record["errors"].append("harness exception: %s: %s" % (type(e).__name__, e))
        (run_dir / "harness-traceback.txt").write_text(traceback.format_exc())
    finally:
        record["wall_s"] = round(time.monotonic() - started, 2)
        if box is not None and not args.keep_sandbox:
            box.cleanup()
        (run_dir / "record.json").write_text(json.dumps(record, indent=2, default=str))
    return record


def _sum_optional(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


# --------------------------------------------------------------------------


def aggregate(records, task_list, runner_keys, runner_info, repeat, started,
              skips=None):
    skips = skips or {}
    results = {
        "started": started,
        "repeat": repeat,
        "runners": runner_keys,
        "runner_info": runner_info,
        "tasks": [],
        "totals": {},
        "warnings": [],
        "runs": records,
    }
    for task in task_list:
        entry = {"name": task.name, "category": task.category,
                 "proves": task.proves or task.description,
                 "by_runner": {}}
        for key in runner_keys:
            info = runner_info[key]
            if info.get("unavailable"):
                entry["by_runner"][key] = {"unavailable": info["unavailable"]}
                continue
            mine = [r for r in records if r["task"] == task.name and r["runner"] == key]
            if not mine:
                entry["by_runner"][key] = {
                    "unavailable": skips.get((task.name, key),
                                             "not run (reason not recorded)")}
                continue
            passed = sum(1 for r in mine if r["status"] == "pass")
            failed = sum(1 for r in mine if r["status"] == "fail")
            errored = sum(1 for r in mine if r["status"] == "error")
            per_check = {}
            for record in mine:
                for check in record["checks"]:
                    stats = per_check.setdefault(
                        check["label"], {"runs": 0, "passed": 0, "errors": 0,
                                         "last_detail": ""})
                    stats["runs"] += 1
                    if check["status"] == "pass":
                        stats["passed"] += 1
                    else:
                        stats["last_detail"] = check["detail"]
                        if check["status"] == "error":
                            stats["errors"] += 1
            entry["by_runner"][key] = {
                "runs": len(mine), "passed_runs": passed, "failed_runs": failed,
                "error_runs": errored,
                "checks": per_check,
                "errors": [e for r in mine for e in r["errors"]],
                "mean_wall_s": round(sum(r["wall_s"] for r in mine) / len(mine), 1),
                "tokens_in": _sum_optional(r.get("tokens_in") for r in mine),
                "tokens_out": _sum_optional(r.get("tokens_out") for r in mine),
                "tool_calls": sum(r.get("tool_calls") or 0 for r in mine),
            }
        results["tasks"].append(entry)

    for key in runner_keys:
        info = runner_info[key]
        if info.get("unavailable"):
            results["totals"][key] = {"unavailable": info["unavailable"]}
            continue
        mine = [r for r in records if r["runner"] == key]
        results["totals"][key] = {
            "runs": len(mine),
            "passed_runs": sum(1 for r in mine if r["status"] == "pass"),
            "failed_runs": sum(1 for r in mine if r["status"] == "fail"),
            "error_runs": sum(1 for r in mine if r["status"] == "error"),
            "tokens_in": _sum_optional(r.get("tokens_in") for r in mine),
            "tokens_out": _sum_optional(r.get("tokens_out") for r in mine),
            "wall_s": round(sum(r["wall_s"] for r in mine), 1),
        }
    return results


def validate(task_list, args, work_root: Path) -> int:
    """Set every fixture up and run its pre_checks. No model is called."""
    home = sandbox_mod.Sandbox.prepare_home(work_root / "home-validate")
    failures = 0
    for task in task_list:
        missing = task.missing_commands()
        prefix = "%-26s" % task.name
        if missing:
            print("%s SKIP  needs %s, not installed" % (prefix, ", ".join(missing)))
            continue
        box = sandbox_mod.Sandbox.create(
            root=work_root / "validate" / task.name, setup_dir=task.setup,
            home=home, outside_files=task.outside_files, git_init=task.git_init)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env.update(task.env)
        ctx = checks_mod.CheckContext(box=box, turns=[], target_index=0, env=env,
                                      command_timeout=task.command_timeout,
                                      ignore_globs=task.ignore)
        broken_setup = ""
        for command in task.setup_commands:
            outcome = procs.shell(command, cwd=box.project, env=env,
                                  timeout=task.command_timeout)
            if not outcome.ok:
                broken_setup = "`%s` exited %s: %s" % (
                    command, outcome.exit_code,
                    (outcome.stderr or outcome.stdout).strip()[-200:])
                break
        if broken_setup:
            failures += 1
            print("%s FAIL  fixture setup command failed: %s" % (prefix, broken_setup))
            if not args.keep_sandbox:
                box.cleanup()
            continue
        results = checks_mod.evaluate(ctx, task.pre_checks)
        bad = [r for r in results if r.status != checks_mod.PASS]
        if not task.pre_checks:
            print("%s WARN  no pre_checks — the fixture's starting state is unverified"
                  % prefix)
        elif bad:
            failures += 1
            print("%s FAIL  %s" % (prefix, "; ".join("%s: %s" % (r.label, r.detail)
                                                     for r in bad)))
        else:
            print("%s ok    %d pre-check(s) hold" % (prefix, len(results)))
        # Checks that need no model are also worth validating: answer_* and
        # tool_* cannot run, but regexes must at least compile.
        for spec in task.checks:
            try:
                if "pattern" in spec:
                    checks_mod._compile(spec)
            except Exception as e:
                failures += 1
                print("%s FAIL  bad check regex %r: %s" % (prefix, spec.get("pattern"), e))
            if spec.get("type") not in checks_mod.CHECKS:
                failures += 1
                print("%s FAIL  unknown check type %r" % (prefix, spec.get("type")))
        if not args.keep_sandbox:
            box.cleanup()
    return 1 if failures else 0


def _die_gracefully(signum, frame):
    """Turn SIGTERM/SIGINT into an exception so the cleanup in `finally` runs.

    Without this a `kill` on the harness leaves its sandbox tree behind, and
    "no temp dirs left behind after a run" stops being true the first time
    someone interrupts one.
    """
    raise KeyboardInterrupt("signal %d" % signum)


def main(argv=None) -> int:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _die_gracefully)
        except (OSError, ValueError):  # not on the main thread, or unsupported
            pass

    args = parse_args(argv)
    task_list = select_tasks(args)
    if not task_list:
        raise SystemExit("no tasks selected")

    if args.list:
        list_tasks(task_list)
        return 0

    started = datetime.datetime.now()
    stamp = started.strftime("%Y%m%d-%H%M%S")

    temp_work = None
    if args.work_dir:
        work_root = Path(args.work_dir) / stamp
    else:
        temp_work = tempfile.mkdtemp(prefix="haikode-bench-")
        work_root = Path(temp_work)
    work_root.mkdir(parents=True, exist_ok=True)

    try:
        if args.validate:
            return validate(task_list, args, work_root)

        runner_keys = args.runner or (["haikode", "opencode"] if args.compare
                                      else ["haikode"])
        if args.compare:
            for key in ("haikode", "opencode"):
                if key not in runner_keys:
                    runner_keys.append(key)

        results_root = Path(args.results_dir) / stamp
        results_root.mkdir(parents=True, exist_ok=True)

        instances = {}
        runner_info = {}
        for key in runner_keys:
            options = {"provider": args.provider, "model": args.model}
            if key == "haikode":
                options["mode"] = args.haikode_mode
                if args.haikode_repo:
                    options["repo"] = args.haikode_repo
            if key == "opencode" and args.opencode_bin:
                options["binary"] = args.opencode_bin
            runner = runners.build(key, options)
            available, reason = runner.availability()
            instances[key] = runner
            runner_info[key] = {"identity": runner.identity(),
                                "models_used": set()}
            if not available:
                runner_info[key]["unavailable"] = reason
                print("runner %s is UNAVAILABLE: %s" % (key, reason), file=sys.stderr)

        homes = {key: sandbox_mod.Sandbox.prepare_home(work_root / ("home-%s" % key))
                 for key in runner_keys}
        # Warm each home once (the credential preflight is the first thing that
        # touches it), then freeze its config so no run can poison the next.
        home_baselines = {}

        records = []
        warnings = []
        skips = {}
        preflights = {}
        for task in task_list:
            if args.provider:
                task.provider = args.provider
            if args.model:
                task.model = args.model
            if args.timeout:
                task.timeout = args.timeout

            missing = task.missing_commands()
            for key in runner_keys:
                if runner_info[key].get("unavailable"):
                    continue
                if missing:
                    skips[(task.name, key)] = (
                        "requires %s, not installed" % ", ".join(missing))
                    message = ("task %s requires %s which is not installed — "
                               "reported as unavailable, not as a failure"
                               % (task.name, ", ".join(missing)))
                    if message not in warnings:
                        warnings.append(message)
                        print("! " + message, file=sys.stderr)
                    continue
                runner = instances[key]
                supported, why = runner.supports(task)
                if not supported:
                    skips[(task.name, key)] = why
                    warnings.append("task %s on %s: %s" % (task.name, key, why))
                    print("! task %s cannot run on %s: %s" % (task.name, key, why),
                          file=sys.stderr)
                    continue

                # Credentials: checked once per (runner, provider, model), so a
                # missing key is one sentence rather than N identical 401s.
                model_key = (key, task.provider, task.model)
                if model_key not in preflights:
                    preflights[model_key] = runner.preflight(task, homes[key])
                    if not preflights[model_key][0]:
                        print("! " + preflights[model_key][1], file=sys.stderr)
                ready, why = preflights[model_key]
                if not ready:
                    skips[(task.name, key)] = why
                    if why not in warnings:
                        warnings.append(why)
                    continue

                if key not in home_baselines:
                    home_baselines[key] = sandbox_mod.capture_home_config(homes[key])

                for repeat in range(1, args.repeat + 1):
                    if args.pause and records:
                        time.sleep(args.pause)
                    print("→ %-26s %-9s run %d/%d ..."
                          % (task.name, key, repeat, args.repeat),
                          end="", flush=True, file=sys.stderr)
                    record = run_once(task, runner, args, work_root, results_root,
                                      repeat, homes[key], home_baselines.get(key))
                    records.append(record)
                    if record.get("home_repaired"):
                        warnings.append(
                            "a previous run had written global config into the "
                            "shared $HOME for %s; it was reverted before %s run %d "
                            "(%s)" % (key, task.name, repeat,
                                      "; ".join(record["home_repaired"][:4])))
                    runner_info[key]["models_used"].update(record.get("models_used") or [])
                    print(" %s (%.0fs, %d/%d checks)"
                          % (record["status"], record["wall_s"],
                             record.get("checks_passed", 0),
                             record.get("checks_total", 0)),
                          file=sys.stderr)

        for key in runner_keys:
            runner_info[key]["models_used"] = sorted(runner_info[key]["models_used"])
            runner_info[key]["notes"] = list(instances[key].notes)
            warnings.extend(instances[key].notes)

        results = aggregate(records, task_list, runner_keys, runner_info,
                            args.repeat, started.isoformat(timespec="seconds"),
                            skips=skips)
        seen = set()
        results["warnings"] = [w for w in warnings
                               if not (w in seen or seen.add(w))]
        results["orphan_process_groups"] = procs.survivors()
        results["command_line"] = sys.argv
        results["work_root"] = str(work_root)
        results["sandboxes_removed"] = not args.keep_sandbox

        report.write_results(results_root / "results.json", results)
        rendered = report.render(results, verbose=args.verbose)
        (results_root / "summary.txt").write_text(rendered)
        print()
        print(rendered)
        print()
        print("results: %s" % (results_root / "results.json"))

        any_failure = any(r["status"] != "pass" for r in records)
        return 1 if any_failure else 0
    finally:
        if temp_work and not args.keep_sandbox:
            shutil.rmtree(temp_work, ignore_errors=True)
        elif temp_work:
            print("sandboxes kept in %s" % temp_work, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
