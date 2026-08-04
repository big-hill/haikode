"""Refuse a push that would publish something private.

Run from the `pre-push` hook with the pushed ref ranges on stdin, in git's
own format: `<local ref> <local sha> <remote ref> <remote sha>` per line.
Exit 1 blocks the push.

This exists because a scrub that depends on somebody remembering to scan is
not a scrub. Two things reached a repository that was meant to be clean: a
test written with a real machine's LAN and Tailscale addresses instead of
documentation ones, and — for every commit made before the repository had
its own `user.email` — the author's machine name. Both were found after the
push rather than before it.

Nothing private is written down here. The developer's user name and host
name are read from the environment at run time, so this file can be public
without being a leak in its own right.
"""

import ipaddress
import os
import re
import socket
import subprocess
import sys

# Commit identities this project publishes under. A commit authored by
# anything else carries a real person's address into a public history.
ALLOWED_EMAILS = {
    "haikode@localhost",
    "noreply@localhost",
    "noreply@anthropic.com",
}
ALLOWED_EMAIL_SUFFIXES = ("@users.noreply.github.com",)

# Addresses that may appear in the source: loopback, and the ranges reserved
# for documentation and examples. RFC 5737 (192.0.2/24, 198.51.100/24,
# 203.0.113/24) is the correct choice for new examples; the rest are the
# conventional stand-ins this project already uses in tests.
ALLOWED_ADDRESSES = {
    "127.0.0.1", "0.0.0.0", "255.255.255.255",
    "100.64.0.1",                    # the CGNAT range's first address
    "169.254.169.254",               # the cloud metadata endpoint the SSRF
                                     # guard exists to refuse, named in its
                                     # docstring (tool/misc.py)
    "172.16.0.1",                    # canonical private-range example in the
                                     # hardening tests' refusal list
    "10.1.2.3",                      # ditto, in historic revisions of the
                                     # same test — blobs in history cannot
                                     # be edited, only allowed
}
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",   # RFC 5737
    "192.168.1.0/24", "10.0.0.0/29",                       # conventional
))

# A literal followed by "/<bits>" is a CIDR block — a range being defined,
# as in the code that decides whether an endpoint is local, not a machine
# somebody owns.
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?!/\d)")

# Files whose whole purpose is to carry secret-shaped strings past the
# redaction machinery. Their canaries are synthetic and must not trip this.
CANARY_PATHS = (
    "tests/test_redact.py",
    "tests/test_secrets.py",
    "tests/test_wiring_audit.py",
    "tests/test_wiring_review.py",
    "benchmarks/",
)

SECRETS = (
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "an OpenAI-style key"),
    (re.compile(r"\bxai-[A-Za-z0-9]{20,}"), "an xAI key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
)


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


# Names that identify nobody. This matters most on the platform the project
# targets: Haiku puts every account under /boot/home, calls the default one
# "user", and answers to "shredder" until someone renames it — so on Haiku
# all three candidates are generic, and flagging every file containing the
# word "home" would make the hook useless exactly where it is needed.
GENERIC_NAMES = frozenset((
    "home", "user", "users", "root", "admin", "boot", "shredder",
    "shredder32", "shredder64",
    "localhost", "runner", "build", "ubuntu", "debian",
))


def local_identifiers():
    """Strings that name *this* machine or user, derived, never stored."""
    found = set()
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = ""
    for candidate in (os.path.basename(os.path.expanduser("~")), host):
        if len(candidate) > 3 and candidate.lower() not in GENERIC_NAMES:
            found.add(candidate)
    return found


def private_address(text):
    for match in IPV4.findall(text):
        if match in ALLOWED_ADDRESSES:
            continue
        try:
            address = ipaddress.ip_address(match)
        except ValueError:
            continue
        if any(address in network for network in ALLOWED_NETWORKS):
            continue
        if address.is_private or address in ipaddress.ip_network("100.64.0.0/10"):
            return match
    return ""


def is_canary(path):
    return any(path.startswith(prefix) for prefix in CANARY_PATHS)


def ranges_from_stdin():
    """(remote_sha, local_sha) per pushed ref, deletions skipped."""
    out = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if set(local_sha) == {"0"}:
            continue                                   # deleting a ref
        out.append((remote_sha, local_sha))
    return out


def commits_in(remote_sha, local_sha):
    if set(remote_sha) == {"0"}:
        # A new branch: everything it adds that no other ref already has.
        listing = run("git", "rev-list", local_sha, "--not", "--all")
    else:
        listing = run("git", "rev-list", "%s..%s" % (remote_sha, local_sha))
    return [line for line in listing.split() if line]


def scan(commits, identifiers):
    problems = []
    for commit in commits:
        short = commit[:9]
        for field, label in (("%ae", "author"), ("%ce", "committer")):
            email = run("git", "show", "-s", "--format=" + field, commit).strip()
            if email and email not in ALLOWED_EMAILS \
                    and not email.endswith(ALLOWED_EMAIL_SUFFIXES):
                problems.append(
                    "%s: %s address %r is not one this project publishes "
                    "under. Set a repository-local identity:\n"
                    "    git config user.name haikode\n"
                    "    git config user.email haikode@localhost"
                    % (short, label, email))

        names = run("git", "show", "--pretty=", "--name-only", commit).split("\n")
        for path in [name.strip() for name in names if name.strip()]:
            blob = run("git", "show", "%s:%s" % (commit, path))
            if not blob:
                continue
            address = private_address(blob)
            if address:
                problems.append(
                    "%s: %s contains the private address %s. Use a "
                    "documentation range (192.0.2.x) or 192.168.1.x."
                    % (short, path, address))
            for identifier in identifiers:
                if identifier in blob:
                    problems.append(
                        "%s: %s names this machine or account (%r)."
                        % (short, path, identifier))
            if is_canary(path):
                continue
            for pattern, what in SECRETS:
                if pattern.search(blob):
                    problems.append("%s: %s looks like it contains %s."
                                    % (short, path, what))
    return problems


def main():
    identifiers = local_identifiers()
    problems = []
    seen = set()
    for remote_sha, local_sha in ranges_from_stdin():
        for commit in commits_in(remote_sha, local_sha):
            if commit not in seen:
                seen.add(commit)
                problems.extend(scan([commit], identifiers))
    if not problems:
        return 0
    print("push refused: %d thing(s) that must not be published\n"
          % len(problems), file=sys.stderr)
    for problem in dict.fromkeys(problems):
        print("  - %s" % problem, file=sys.stderr)
    print("\nFix the commits and push again. To override once (you had "
          "better be sure):\n    git push --no-verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
