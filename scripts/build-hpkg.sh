#!/bin/sh
# Build an installable haikode .hpkg.
#
# The .hpkg is the Haiku-native way to ship: `pkgman install ./haikode-*.hpkg`
# drops the whole thing into /boot/system, the package manager can uninstall it
# again cleanly, and the Deskbar entry appears without anybody editing a menu.
# scripts/install-on-haiku.sh remains for hacking on a source checkout.
#
# Layout produced (paths are relative to the package root, i.e. /boot/system):
#
#   apps/haikode/haikode                              native BeAPI desktop app
#   bin/haikode                                       CLI launcher
#   bin/hai                          -> haikode       compatibility name
#   bin/haikode-desktop              -> ../apps/...   desktop app on $PATH
#   bin/hai-desktop                  -> ../apps/...   compatibility name
#   bin/hai-keystore                                  BKeyStore helper
#   lib/python3.10/vendor-packages/haikode/           the Python package tree
#   data/deskbar/menu/Applications/haikode -> ...     Deskbar entry
#   documentation/packages/haikode/README.md
#
# vendor-packages is already on the stock python3.10 sys.path, so the packaged
# launcher needs no PYTHONPATH at all.
#
# Usage:  scripts/build-hpkg.sh [output-directory]
set -eu

case "$(uname -s)" in
	Haiku) ;;
	*)
		echo "build-hpkg.sh must run on Haiku: it needs package, rc, xres," >&2
		echo "mimeset and the BeAPI build of the desktop app." >&2
		exit 1
		;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUT_DIR=${1:-$PROJECT_DIR/build}

# Package architecture. Not `uname -m`: on 32-bit Haiku that answers "BePC"
# (a BeOS inheritance), which is not a valid hpkg architecture; `getarch`
# answers the real one. On the gcc2 hybrid the architecture FIELD stays
# x86_gcc2 — pkgman refuses anything else — while the C++ inside is still
# built by the modern compiler via `setarch x86`. That is the same shape
# HaikuPorts ships secondary-arch packages in (falkon_x86-…-x86_gcc2.hpkg):
# the secondary arch lives in a package's *name*, never in its architecture.
# Measured on real hardware: an `architecture x86` package is simply
# "not installable" there.
if command -v getarch >/dev/null 2>&1; then
	ARCH=$(getarch)
else
	ARCH=$(uname -m)
fi
MAKE=make
case "$ARCH" in
	BePC)
		ARCH=x86_gcc2
		;;
esac
if [ "$ARCH" = "x86_gcc2" ] && command -v setarch >/dev/null 2>&1; then
	MAKE="setarch x86 make"
fi
PYTHON_LIB_DIR=lib/python3.10/vendor-packages
APP_SIGNATURE=application/x-vnd.haikode
PACKAGER=${HAIKODE_PACKAGER:-"haikode maintainers <haikode@localhost>"}
VENDOR=${HAIKODE_VENDOR:-"haikode"}

# Haiku nightlies ship `python3.10` with no unversioned `python3` command
# (hrev59917 provides only `cmd:python3.10`), so resolve the interpreter
# instead of requiring a name that may not exist. Plain name first.
PYTHON=
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
	if command -v "$candidate" >/dev/null 2>&1; then
		PYTHON=$candidate
		break
	fi
done

missing=
for tool in package rc xres mimeset make findpaths; do
	command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
[ -n "$PYTHON" ] || missing="$missing python3"
if [ -n "$missing" ]; then
	echo "Missing required Haiku tools:$missing" >&2
	echo "Install them with: pkgman install haiku_devel" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------
# hpkg versions are <major>.<minor>.<micro>-<revision>. The Python
# pre-release suffix ("0.1.0-m0m1") is dropped deliberately, not carried as
# an hpkg ~label: a pre-release sorts BELOW the plain version, so adopting
# it now would turn every field upgrade into a refused downgrade.
#
# The revision is the part that must differ between two builds: packagefs
# refuses to activate a same-name-same-version file with "Name in use",
# while pkgman's transcript still reads like a successful upgrade — a
# silent no-op, found in the field by comparing installed files against
# the checkout. The commit count is monotonic and needs no bookkeeping;
# HAIKODE_PACKAGE_REVISION still overrides, and a git-less tree falls
# back to a timestamp.
FULL_VERSION=$("$PYTHON" - "$PROJECT_DIR" <<'PY'
import re, sys, pathlib
text = (pathlib.Path(sys.argv[1]) / "haikode" / "__init__.py").read_text()
match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
print(match.group(1) if match else "0.0.0")
PY
)
BASE_VERSION=${FULL_VERSION%%-*}
if [ -n "${HAIKODE_PACKAGE_REVISION:-}" ]; then
	REVISION=$HAIKODE_PACKAGE_REVISION
elif REVISION=$(git -C "$PROJECT_DIR" rev-list --count HEAD 2>/dev/null) \
		&& [ -n "$REVISION" ]; then
	:
else
	REVISION=$(date +%Y%m%d%H%M)
fi
PKG_VERSION="$BASE_VERSION-$REVISION"
PKG_FILE="$OUT_DIR/haikode-$PKG_VERSION-$ARCH.hpkg"

echo "haikode $FULL_VERSION -> package version $PKG_VERSION ($ARCH)"

# ---------------------------------------------------------------------------
# native parts
# ---------------------------------------------------------------------------
echo "Building BKeyStore helper ..."
$MAKE -C "$PROJECT_DIR/tools/hai-keystore" clean >/dev/null
$MAKE -C "$PROJECT_DIR/tools/hai-keystore"
KEYSTORE_BIN="$PROJECT_DIR/tools/hai-keystore/hai-keystore"
[ -x "$KEYSTORE_BIN" ] || { echo "hai-keystore was not produced." >&2; exit 1; }

echo "Building native desktop app ..."
$MAKE -C "$PROJECT_DIR/desktop" clean >/dev/null
$MAKE -C "$PROJECT_DIR/desktop"
DESKTOP_BIN=$(find "$PROJECT_DIR/desktop" -type f -name haikode -perm -100 \
	| grep '/objects' | head -n 1)
[ -n "$DESKTOP_BIN" ] || { echo "Desktop app binary was not produced." >&2; exit 1; }

# ---------------------------------------------------------------------------
# staging tree
# ---------------------------------------------------------------------------
STAGE=$(mktemp -d /tmp/haikode-hpkg.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT INT TERM

mkdir -p "$STAGE/apps/haikode" \
         "$STAGE/bin" \
         "$STAGE/$PYTHON_LIB_DIR" \
         "$STAGE/data/deskbar/menu/Applications" \
         "$STAGE/documentation/packages/haikode"

echo "Staging Python package ..."
cp -R "$PROJECT_DIR/haikode" "$STAGE/$PYTHON_LIB_DIR/haikode"
# macOS archives leave AppleDouble sidecars behind; Python would try to compile
# them and pkgman would ship them. Byte-compiled output is rebuilt on demand.
find "$STAGE/$PYTHON_LIB_DIR/haikode" -name '._*' -type f -delete
find "$STAGE/$PYTHON_LIB_DIR/haikode" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/$PYTHON_LIB_DIR/haikode" -name '*.pyc' -type f -delete

cp "$DESKTOP_BIN" "$STAGE/apps/haikode/haikode"
chmod 755 "$STAGE/apps/haikode/haikode"
cp "$KEYSTORE_BIN" "$STAGE/bin/hai-keystore"
chmod 755 "$STAGE/bin/hai-keystore"
cp "$PROJECT_DIR/README.md" "$STAGE/documentation/packages/haikode/README.md"

# The packaged launcher differs from scripts/haikode-launcher on purpose: a
# packaged install imports haikode from vendor-packages, so setting PYTHONPATH
# unconditionally would let a stale source checkout shadow the package.
cat > "$STAGE/bin/haikode" <<'LAUNCHER'
#!/bin/sh
# Installed from the haikode package as /boot/system/bin/haikode.
# haikode lives in lib/python3.10/vendor-packages and is importable already;
# HAIKODE_HOME (or the older HAI_HOME) is honoured only so a developer can
# deliberately shadow the packaged tree with a working copy.
set -eu

DEV_TREE="${HAIKODE_HOME:-${HAI_HOME:-}}"
if [ -n "$DEV_TREE" ]; then
	export PYTHONPATH="$DEV_TREE${PYTHONPATH:+:$PYTHONPATH}"
fi
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
	if command -v "$candidate" >/dev/null 2>&1; then
		exec "$candidate" -m haikode "$@"
	fi
done
echo "haikode: no Python 3 interpreter found" >&2
exit 127
LAUNCHER
chmod 755 "$STAGE/bin/haikode"

# Compatibility and convenience names. hai-keystore keeps its own name because
# Haiku ties a keyring grant to the signature AND the binary path.
ln -s haikode "$STAGE/bin/hai"
ln -s ../apps/haikode/haikode "$STAGE/bin/haikode-desktop"
ln -s ../apps/haikode/haikode "$STAGE/bin/hai-desktop"
# Deskbar reads this directory; four levels up from it is the package root.
ln -s ../../../../apps/haikode/haikode "$STAGE/data/deskbar/menu/Applications/haikode"

# ---------------------------------------------------------------------------
# resources, signature and MIME
# ---------------------------------------------------------------------------
echo "Applying application resources ..."
rc -o "$STAGE/haikode.rsrc" "$PROJECT_DIR/desktop/resources/haikode.rdef"
xres -o "$STAGE/apps/haikode/haikode" "$STAGE/haikode.rsrc"
rm -f "$STAGE/haikode.rsrc"

# mimeset turns the app_signature resource into the BEOS:APP_SIG attribute that
# Deskbar, the roster and "open" all look at. hpkg stores file attributes, so
# doing it here means the installed copy is already registered.
mimeset -f "$STAGE/apps/haikode/haikode"
mimeset -f "$STAGE/bin/haikode"
mimeset -f "$STAGE/bin/hai-keystore"

SIG=$(catattr -d BEOS:APP_SIG "$STAGE/apps/haikode/haikode" 2>/dev/null || true)
if [ "$SIG" != "$APP_SIGNATURE" ]; then
	echo "Warning: desktop app signature is '$SIG', expected '$APP_SIGNATURE'." >&2
fi

# ---------------------------------------------------------------------------
# .PackageInfo
# ---------------------------------------------------------------------------
cat > "$STAGE/.PackageInfo" <<INFO
name			haikode
version			$PKG_VERSION
architecture		$ARCH
summary			"AI coding agent that runs natively on Haiku"
description		"haikode is an AI coding agent that runs on Haiku and talks
directly to cloud providers over HTTPS. There is no server to run beside it, no
SSH tunnel, no Node and no second computer in the loop: the agent loop, the
provider clients, OAuth refresh, the tools, permissions, sessions and both
front-ends are Python 3 standard library only.

It ships two front-ends over one engine: a curses TUI with a plain REPL
fallback, and a native BeAPI desktop application. API keys are kept in the
Haiku keystore through the bundled hai-keystore helper, the same BKeyStore
mechanism WebPositive uses. Sessions are stored locally and can be reverted
file by file.

Commands installed: haikode (CLI, also available as hai), haikode-desktop
(native application, also available as hai-desktop) and hai-keystore."
packager		"$PACKAGER"
vendor			"$VENDOR"
licenses {
	"MIT"
}
copyrights {
	"2026 haikode contributors"
}
provides {
	haikode = $BASE_VERSION
	cmd:haikode = $BASE_VERSION
	cmd:hai = $BASE_VERSION
	cmd:hai_keystore = $BASE_VERSION
	app:haikode = $BASE_VERSION
}
requires {
	haiku >= r1~beta5
	# The command, not the package name: on the 32-bit hybrid the package is
	# called python3.10_x86, but both spellings provide cmd:python3.10.
	cmd:python3.10 >= 3.10
}
INFO

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
mkdir -p "$OUT_DIR"
rm -f "$PKG_FILE"
echo "Creating $PKG_FILE ..."
package create -C "$STAGE" -i "$STAGE/.PackageInfo" "$PKG_FILE"

[ -s "$PKG_FILE" ] || { echo "package create produced nothing." >&2; exit 1; }

echo
echo "Built: $PKG_FILE"
package list "$PKG_FILE" | head -n 40
echo
echo "Install with:  pkgman install $PKG_FILE"
echo "Uninstall with: pkgman uninstall haikode"
