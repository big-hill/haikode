---
name: haiku-packages
description: Installing, building and debugging hpkg packages on Haiku, including the 32-bit x86_gcc2 hybrid
when to use: Whenever installing software with pkgman, building an .hpkg, or debugging why a package will not install on Haiku
---

# Working with Haiku packages

Verified on real hardware (hrev57937 x86_64 and hrev59866 x86_gcc2), not
from documentation.

## pkgman basics

- `pkgman install <name>` / `pkgman uninstall <name>` / `pkgman search <name>`
- `pkgman full-sync` upgrades the whole system to the repositories' state.
  If it proposes *downgrades* or fails to resolve, check the repo files
  first — see below.
- Repository definitions live in
  `/boot/system/settings/package-repositories/` (`Haiku`, `HaikuPorts`).
  A machine upgraded across releases (beta5 → beta6) may still point at the
  old release's URLs; repoint these files before blaming the resolver.

## The 32-bit hybrid (x86_gcc2)

- `uname -m` says `BePC` and is useless for this; use `getarch` /
  `listarch`. The primary architecture on 32-bit Haiku is `x86_gcc2`
  (gcc2-built system), with `x86` as the secondary architecture.
- An `.hpkg`'s **architecture field must be the primary architecture**
  (`x86_gcc2`), even when every binary in it is built for the secondary
  one. The secondary arch belongs in the package *name* suffix instead.
  HaikuPorts' own packages prove the rule: `falkon_x86-...-x86_gcc2.hpkg`.
  A package whose field says `x86` is simply "not installable" on the
  hybrid, with no better error message.
- Build for the secondary architecture inside `setarch x86` — it puts the
  x86 toolchain and libraries first. Secondary-arch interpreters carry the
  suffix in the package name (`python3.10_x86`) but still provide the
  plain command (`cmd:python3.10`).

## When an uninstall takes friends with it

pkgman removes dependents of what you remove. Uninstalling a pinned Qt5 or
KDE-frameworks stack can silently take applications (this is how Calligra
disappeared in the field). Read the transaction summary before confirming,
and reinstall by name afterwards if something went missing.

## When a package is truly uninstallable

If resolution fails on a library soname (for example a `libxml2` bump that
an application has not been rebuilt against), the fix is upstream — check
HaikuPorts issues before filing a duplicate. Pick an alternative
application in the meantime rather than pinning the whole system to old
libraries.

## Non-packaged software

Anything installed by hand belongs under `~/config/non-packaged/bin` (per
user); it survives system updates and needs no package at all.
