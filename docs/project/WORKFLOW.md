# Development and release workflow

This is the operational workflow for changing haikode. It describes mechanics;
the product contract remains in [CONTEXT.md](CONTEXT.md).

## Reference and workspace model

- Canonical remote: `origin`
- Canonical reference branch: `origin/main`
- Landing branch: local `main`, reconciled with `origin/main`
- Permanent checkout: a clean, stable clone used for fetch, preflight, review,
  and landing
- Task worktrees: sibling paths outside the permanent checkout, normally
  `../haikode-worktrees/<task>`

Start every session in the permanent checkout:

```sh
./scripts/project-preflight
git status --short --branch
```

If local `main` and `origin/main` differ, understand and reconcile the commits
before starting an unrelated task. Do not erase a local landing chain merely to
make the branch names match.

For a new task after reconciliation:

```sh
mkdir -p ../haikode-worktrees
git worktree add -b task/<short-name> \
  ../haikode-worktrees/<short-name> origin/main
```

Continue an explicitly recorded local landing chain from `main`, not from an
older remote ref. Never use an abandoned worktree as the permanent entry.

## Change discipline

1. State the behavior and its verification boundary.
2. Add or identify a test that fails for the defect before changing code.
3. Keep changes within one ownership seam from [CODEMAP.md](CODEMAP.md), or
   coordinate one writer when a seam must span files.
4. Preserve user changes in a dirty tree. Do not reset, checkout, or overwrite
   files to make the workspace convenient.
5. Treat prompt text, project instructions, provider payloads, migrations, and
   package metadata as code.
6. Recheck whether context docs or an ADR need updating before landing.
7. Diagnose from complete current evidence: reproduce the live path, read the
   full error/response metadata, and measure the behavior actually in question.
   A static replay or remembered explanation is not proof of a live bottleneck.

Parallel agents may inspect independently, but must not compete to edit the
same high-coupling seam. Only the landing/main-maintainer step writes
`NOW.md`, after the result is actually landed.

## Local QA

The canonical suite intentionally has four failing executable backlog checks,
all in `tests/test_wiring_audit.py`. Use the baseline runner so a fifth failure
cannot hide in expected red output:

```sh
HAI_DISABLE_KEYSTORE=1 python3 scripts/ci_baseline.py
```

Common supporting checks:

```sh
python3 -m compileall -q haikode
sh -n scripts/project-preflight scripts/install-on-haiku.sh \
  scripts/haikode-launcher scripts/build-hpkg.sh scripts/deploy-to-haiku.sh
./scripts/project-preflight --offline
python3 benchmarks/run.py --validate
HAI_DISABLE_KEYSTORE=1 python3 benchmarks/performance_audit.py --pretty
git diff --check
```

Run only the checks relevant while iterating, then run the full baseline before
calling a code change done. A fixed wiring gap should make its audit test pass;
do not weaken the audit to preserve a count.

## Physical Haiku gate

Code reading and macOS/Linux tests do not prove Haiku behavior. Validate on
physical Haiku when a change touches terminal rendering, BeAPI, packagefs/BFS
attributes, SQLite concurrency, process lifecycle, keyring access, networking,
or HPKG layout.

- Use at most one simultaneous SSH connection to a small Haiku target.
- Execute an explicit operator instruction such as shutdown or reboot before
  starting long QA; never leave it queued behind a slow test.
- Never launch the BeAPI GUI over SSH; the owner performs visual UI steps on
  the physical display.
- Never launch a browser automatically on Haiku for OAuth.
- Do not rename `hai-keystore`.
- Stop every remote process started for QA and prove cleanup with `ps`.
- Diagnose a fresh machine with `haikode doctor` before changing code.
- Do not read a live session database through a second process. For diagnosis,
  make a consistent copy of the database together with its WAL/SHM files.

### Single-connection SSH routine

"One SSH connection" means one underlying SSH transport and no parallel remote
jobs. Do not run a series of independent `ssh`, `scp`, or `sftp` connections.
For the first remote operation, set `HAIKODE_SSH_TARGET` from the external
operations context, create a private temporary socket directory, and start one
multiplexing master:

```sh
HAIKODE_SSH_DIR=$(mktemp -d /tmp/haikode-ssh.XXXXXX)
HAIKODE_SSH_SOCKET=$HAIKODE_SSH_DIR/control
export HAIKODE_SSH_DIR HAIKODE_SSH_SOCKET

ssh -M -S "$HAIKODE_SSH_SOCKET" \
  -o ControlMaster=yes \
  -o ControlPersist=600 \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -fN "$HAIKODE_SSH_TARGET"
```

Retain the target and socket path for the whole task. Before use, verify the
master, then send every remote command sequentially through that same socket:

```sh
ssh -S "$HAIKODE_SSH_SOCKET" -O check "$HAIKODE_SSH_TARGET"
ssh -S "$HAIKODE_SSH_SOCKET" "$HAIKODE_SSH_TARGET" '<remote command>'
```

File-transfer tools must likewise receive the same `ControlPath`; do not let
them open an independent transport. If the master check fails, stop and
diagnose it instead of silently starting several connections. Close the master
explicitly after remote cleanup and remove the now-empty private directory:

```sh
ssh -S "$HAIKODE_SSH_SOCKET" -O exit "$HAIKODE_SSH_TARGET"
rmdir "$HAIKODE_SSH_DIR"
```

When haikode edits its own checkout, the running process continues with modules
already loaded in memory. Source changes take effect on the next launch; config
changes require reload or restart. Verify the next process, not only the one
that authored the edit.

Machine names, addresses, and credentials are external operations context, not
repository documentation. Consult `HAIKODE_OPS_CONTEXT` only when the task
requires them.

## Migrations and persistent state

Session schema changes belong in `haikode/session.py` and must be additive or
use an explicitly tested transactional rebuild. Test existing databases,
locked/busy failures, concurrent processes, raw transcript preservation,
context checkpoint invalidation, and undo/restore.

Config and OAuth writes must preserve private permissions and atomic replacement.
An app/package upgrade must leave configuration, OAuth tokens, keyring entries,
and session history intact. A purge is a separate, explicit operation.

## Landing

Before commit:

```sh
git diff --check
./scripts/project-preflight --offline
HAI_DISABLE_KEYSTORE=1 python3 scripts/ci_baseline.py
git config core.hooksPath scripts/hooks
```

Use the repository-local public identity documented in
[CONTRIBUTING.md](../../CONTRIBUTING.md). Review the staged file list and let the
pre-push scanner inspect the actual commit range. Land through a reviewed PR or
an explicitly authorized direct push. A build, local commit, or successful
hardware test does not itself authorize publication.

After landing, the single `NOW.md` writer rewrites its transient handoff against
the fetched pre-handoff reference SHA, removes completed items, and sets a short
validity window. The commit carrying `NOW.md` may then advance the reference;
the recorded SHA must remain its ancestor. Permanent findings go to their
owning document or a new ADR instead.

## Deployment and release

`scripts/deploy-to-haiku.sh` deploys a Git commit to a guarded source checkout.
The workstation Git tree is code authority; a Haiku checkout is a build/deploy
target unless a remote change is explicitly brought back into Git.

`scripts/build-hpkg.sh` must run on Haiku and derives a monotonic package
revision from Git. Build release architectures from the same commit. For a
release, verify at least:

- baseline QA and relevant performance/fixture checks;
- native builds and package metadata on each supported architecture;
- package contents, signature, application flags, icon attributes, links,
  documentation, and absence of AppleDouble/bytecode debris;
- a visible application icon in HaikuDepot and Deskbar, plus a current
  screenshot in public release/listing material when the desktop UI is shown;
- checksums copied without transformation;
- a human GUI installation through `Open with -> HaikuDepot`, Deskbar launch,
  first-run/upgrade state preservation, and a simple real prompt;
- no non-packaged developer copy shadows `/boot/system` during the package
  test.

Only then create the release tag and assets. Record evidence in
[VERIFICATION.md](../../VERIFICATION.md); do not turn `NOW.md` into a release
log.

## Rollback

- Source deployment: select the last verified commit/tag, run its tests, and
  deploy it through the same guarded Git path.
- Package deployment: reinstall the last verified HPKG revision through the
  package manager. Do not delete user config or sessions as part of rollback.
- Database/config migration: preserve a consistent pre-change backup and prove
  restoration before relying on rollback. Source rollback alone may not reverse
  persistent schema or config changes.
- If the failure is runtime-only, capture observed process/package state before
  replacing it; Markdown status is not evidence of what was installed.
