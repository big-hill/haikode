---
status: accepted
date: 2026-08-15
decision: Let an explicit slash-update verify and install the matching Haiku package
---

# Explicit one-command package update

## Context and problem

The packaged `/update` path downloaded the matching HPKG but stopped after
printing a `pkgman install` command. The user had already explicitly requested
the update, yet had to find the file, start a second installation flow, and
return to haikode. Automatically mutating the package from the passive startup
check would be a different and unsafe contract.

## Alternatives considered

1. Keep download and installation as two manual operations.
2. Launch HaikuDepot after downloading and require its second confirmation.
3. After an explicit `/update`, verify the release asset and invoke
   `pkgman install -y` before asking the user to restart haikode.
4. Download and install releases automatically during startup.

## Decision

Choose option 3 for packaged installations. `/update` selects the current
architecture's HPKG, requires and verifies the SHA-256 digest in GitHub's
release response, verifies the HPKG's own name and version metadata, and passes
the private temporary file to `pkgman install -y`. The passive check remains
read-only. A source checkout continues to use a fast-forward-only Git pull.

The TUI runs the explicit update off its drawing thread. Once package
activation starts, that worker is presented as non-cancellable because hiding
its result cannot cancel pkgman's transaction. The current process is not
restarted automatically; after success the user closes and reopens haikode.

## Rationale

One explicit command is a clear consent boundary, while digest verification
prevents a truncated or substituted download from reaching the system package
manager. `pkgman -y` is Haiku's supported non-interactive transaction mode and
packagefs handles activation. Restarting only after pkgman returns avoids
executing a mixture of old loaded modules and newly activated package files.

## Consequences

- Release assets need a valid GitHub SHA-256 digest and a package for each
  supported architecture.
- A checksum or download failure removes the temporary file and never invokes
  pkgman.
- A package with an unexpected name or version is removed and never invokes
  pkgman.
- An installation failure keeps the verified HPKG in `/tmp` and reports its
  path for manual diagnosis or recovery.
- A successful transaction removes the temporary file and asks for a close and
  reopen. Configuration, OAuth state, keys and sessions remain outside the
  replaced package.
- Package update behavior requires physical-Haiku lifecycle validation; mocks
  only prove command construction and failure handling.

## Reversal conditions

Replace this flow if Haiku removes non-interactive local-package installation,
if releases move to a signed repository with a stronger native update path, or
if packagefs can no longer guarantee safe activation while the old process is
still running. Any replacement must retain explicit consent, architecture
selection, integrity verification, state preservation, failure recovery and a
tested restart boundary.
