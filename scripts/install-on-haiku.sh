#!/bin/sh
# Install the haikode CLI, BKeyStore helper, and native desktop app for this user.
#
# THIS IS THE DEVELOPER INSTALL. It copies a source checkout into
# /boot/home/haikode and installs unmanaged binaries under
# /boot/home/config/non-packaged, which the package manager knows nothing about
# and cannot upgrade or remove.
#
# The preferred install is the package:
#
#     scripts/build-hpkg.sh                       # -> build/haikode-<version>-<arch>.hpkg
#     pkgman install build/haikode-*-x86_64.hpkg
#     pkgman uninstall haikode                    # clean removal
#
# The package installs into /boot/system, puts the Python tree in
# lib/python3.10/vendor-packages so no PYTHONPATH is needed, and registers a
# Deskbar entry. Use this script instead when you are editing the source and
# want the tree you edit to be the tree that runs.
set -eu

case "$(uname -s)" in
	Haiku) ;;
	*) echo "This installer must run on Haiku OS." >&2; exit 1 ;;
esac

echo "Developer (non-packaged) install."
echo "For a normal install build a package instead: scripts/build-hpkg.sh"
echo

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
EXPECTED_DIR=/boot/home/haikode

if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
	echo "Installing source tree at $EXPECTED_DIR ..."
	mkdir -p "$EXPECTED_DIR"
	# Copy source without copying compiler output or the session database.
	cp -R "$PROJECT_DIR/haikode" "$EXPECTED_DIR/"
	cp -R "$PROJECT_DIR/desktop" "$EXPECTED_DIR/"
	cp -R "$PROJECT_DIR/tools" "$EXPECTED_DIR/"
	cp -R "$PROJECT_DIR/scripts" "$EXPECTED_DIR/"
	PROJECT_DIR=$EXPECTED_DIR
fi

# macOS archives can contain AppleDouble resource-fork sidecars. They are not
# project sources, and Python otherwise tries to compile `._*.py` on Haiku.
find "$PROJECT_DIR" -name '._*' -type f -delete

BIN_DIR=/boot/home/config/non-packaged/bin
APP_DIR=/boot/home/config/non-packaged/apps/haikode
mkdir -p "$BIN_DIR" "$APP_DIR"

# Haiku nightlies ship `python3.10` with no unversioned `python3` command.
PYTHON=
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
	if command -v "$candidate" >/dev/null 2>&1; then
		PYTHON=$candidate
		break
	fi
done
if [ -z "$PYTHON" ]; then
	echo "Python 3 is required. Install it with: pkgman install python3.10" >&2
	exit 1
fi

# The helper binary keeps the name "hai-keystore" on purpose. Haiku ties a
# keyring grant to the app signature AND the binary path, so renaming it would
# re-trigger the "Application keyring access" dialog on the machine's physical
# screen and orphan every key the approved binary already stored.
echo "Building BKeyStore helper (binary name stays hai-keystore) ..."
# The modern compiler on the 32-bit gcc2 hybrid lives behind `setarch x86`;
# gcc2 itself cannot build this C++.
MAKE=make
if command -v getarch >/dev/null 2>&1 \
		&& [ "$(getarch)" = "x86_gcc2" ] \
		&& command -v setarch >/dev/null 2>&1; then
	MAKE="setarch x86 make"
fi
$MAKE -C "$PROJECT_DIR/tools/hai-keystore" clean
$MAKE -C "$PROJECT_DIR/tools/hai-keystore"
cp "$PROJECT_DIR/tools/hai-keystore/hai-keystore" "$BIN_DIR/hai-keystore"
chmod 755 "$BIN_DIR/hai-keystore"

echo "Building native desktop app ..."
$MAKE -C "$PROJECT_DIR/desktop" clean
$MAKE -C "$PROJECT_DIR/desktop"
DESKTOP_BIN=$(find "$PROJECT_DIR/desktop" -type f -name haikode -perm -100 \
	| grep '/objects' | head -n 1)
if [ -z "$DESKTOP_BIN" ]; then
	echo "Build finished but no desktop binary was found." >&2
	exit 1
fi
cp "$DESKTOP_BIN" "$APP_DIR/haikode"
chmod 755 "$APP_DIR/haikode"

cp "$PROJECT_DIR/scripts/haikode-launcher" "$BIN_DIR/haikode"
chmod 755 "$BIN_DIR/haikode"
# Backwards compatibility: the command used to be called "hai".
ln -sf "$BIN_DIR/haikode" "$BIN_DIR/hai"
ln -sf "$APP_DIR/haikode" "$BIN_DIR/haikode-desktop"
ln -sf "$APP_DIR/haikode" "$BIN_DIR/hai-desktop"

# The package's deskbar entry comes from its data/ directory; a source
# install has to place its own, or the app exists on disk and nowhere a
# user actually looks. The user deskbar menu is a plain directory of links.
DESKBAR_DIR=/boot/home/config/settings/deskbar/menu/Applications
mkdir -p "$DESKBAR_DIR"
ln -sf "$APP_DIR/haikode" "$DESKBAR_DIR/haikode"

if command -v mimeset >/dev/null 2>&1; then
	mimeset -f "$APP_DIR/haikode" >/dev/null 2>&1 || true
fi

echo "Running CLI and worker smoke checks ..."
"$BIN_DIR/haikode" doctor
printf '%s' smoke | HAI_DESKTOP_TEST_REPLY=worker-ok \
	PYTHONPATH="$PROJECT_DIR" "$PYTHON" -m haikode.desktop_worker \
	| grep '"event":"completed"' >/dev/null

echo
echo "Installed:"
echo "  CLI:      $BIN_DIR/haikode"
echo "            (compatibility symlink kept: $BIN_DIR/hai -> haikode)"
echo "  Desktop:  $APP_DIR/haikode"
echo "            (run: haikode-desktop, or the old name hai-desktop)"
echo "  Keystore: $BIN_DIR/hai-keystore"
echo "            (name kept so the existing Haiku keyring approval and the"
echo "             keys already stored under it stay valid)"
echo "Open Settings in the desktop app to choose provider, model, API key,"
echo "Ollama LAN/Tailscale URL, or ChatGPT/SuperGrok subscription login."
echo
echo "This install is not managed by pkgman. To replace it with a real package:"
echo "  $PROJECT_DIR/scripts/build-hpkg.sh"
echo "  pkgman install $PROJECT_DIR/build/haikode-<version>-\$(uname -m).hpkg"
echo "Remove the non-packaged copies first so they do not shadow /boot/system/bin:"
echo "  rm -f $BIN_DIR/haikode $BIN_DIR/hai $BIN_DIR/haikode-desktop \\"
echo "        $BIN_DIR/hai-desktop $BIN_DIR/hai-keystore"
echo "  rm -rf $APP_DIR"
