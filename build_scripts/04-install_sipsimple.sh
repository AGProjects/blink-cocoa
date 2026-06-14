#!/bin/bash
# Build python3-sipsimple from the sibling checkout and install it into
# Blink's venv. Assumes the python3-sipsimple/ checkout sits next to blink/,
# i.e. at ../../python3-sipsimple relative to this script.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow override via env var; default to ../../python3-sipsimple
SIPSIMPLE_DIR="${SIPSIMPLE_DIR:-$(cd "$SCRIPT_DIR/../../python3-sipsimple" 2>/dev/null && pwd)}"

if [ -z "$SIPSIMPLE_DIR" ] || [ ! -f "$SIPSIMPLE_DIR/setup.py" ] || [ ! -f "$SIPSIMPLE_DIR/setup_pjsip.py" ]; then
    echo
    echo "Cannot find python3-sipsimple checkout."
    echo "Expected at \$SIPSIMPLE_DIR or at ../../python3-sipsimple"
    echo "(relative to $SCRIPT_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$SIPSIMPLE_DIR"

# PJSIP version selection.
#   - PJSIP_VERSION env var wins (CI / non-interactive runs)
#   - $1 (positional) honored as a fallback
#   - otherwise prompt interactively; default 2.12
#   - if stdin isn't a tty (piped, redirected), use the default silently
# Supported: 2.12 (legacy, stable) and 2.17 (in-progress migration target).
PJSIP_VERSION="${PJSIP_VERSION:-${1:-}}"
if [ -z "$PJSIP_VERSION" ]; then
    if [ -t 0 ]; then
        echo
        echo "Select PJSIP version to build against:"
        echo "  1) 2.12  (legacy, stable — fully patched)"
        echo "  2) 2.17  (in-progress migration target — see PJSIP_217_MIGRATION.md)"
        echo
        read -r -p "Choice [1]: " choice
        case "${choice:-1}" in
            1|2.12)  PJSIP_VERSION="2.12" ;;
            2|2.17)  PJSIP_VERSION="2.17" ;;
            *)       echo "Unrecognized choice '${choice}', defaulting to 2.12."
                     PJSIP_VERSION="2.12" ;;
        esac
    else
        PJSIP_VERSION="2.12"
    fi
fi

case "$PJSIP_VERSION" in
    2.12|2.17) ;;
    *) echo "Unsupported PJSIP_VERSION='$PJSIP_VERSION' (allowed: 2.12, 2.17)." >&2
       exit 1 ;;
esac

echo "Building SIP SIMPLE SDK from $SIPSIMPLE_DIR against PJSIP $PJSIP_VERSION ..."

# Re-running needs a clean deps tree; get_dependencies.sh fails otherwise.
rm -rf deps/pjsip deps/ZRTPCPP deps/pjproject-* 2>/dev/null || true

chmod +x ./get_dependencies*
# Export so setup_pjsip.py / any sub-script also sees the choice.
export PJSIP_VERSION
./get_dependencies.sh --version "$PJSIP_VERSION"

if [ $? -ne 0 ]; then
    echo
    echo "Failed to install all SIP SIMPLE SDK dependencies"
    echo
    exit 1
fi

# Make sure the build can find MacPorts headers/libs in this shell.
export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# Optional codec probe — bcg729 (G.729) is built+installed by
# 02b-install-bcg729.sh.  It's not in MacPorts/Homebrew, so just warn (don't
# fail) if it isn't present; setup_pjsip.py will then build PJSIP without
# G.729 support and 05-copy-libraries.sh has nothing extra to bundle.
if [ -f "/opt/local/include/bcg729/encoder.h" ] && [ -f "/opt/local/lib/libbcg729.dylib" ]; then
    echo "G.729 codec: bcg729 found at /opt/local — will be built into PJSIP and bundled by 05."
else
    echo
    echo "NOTE: bcg729 not found at /opt/local — G.729 codec will be DISABLED."
    echo "      Run '$SCRIPT_DIR/02b-install-bcg729.sh' first if you want G.729."
    echo
fi

# Force a clean rebuild every time this script runs. Without this:
#   - the stale build/ tree (old pjsip + old _core.so) is reused
#   - pip sees the same version already installed and skips reinstalling
#   - any in-tree _core.so left over from a previous `build_ext --inplace`
#     run will shadow the freshly installed wheel when CWD is on sys.path
# Wipe everything that could shadow or corrupt the new install.
echo "Cleaning previous build artifacts in $SIPSIMPLE_DIR ..."
rm -rf build/ build_inplace/ python3_sipsimple.egg-info/
find sipsimple -name "_core*.so" -print -delete 2>/dev/null || true
pip3 uninstall -y python3-sipsimple >/dev/null 2>&1 || true

# --no-build-isolation so the build sees the venv's Cython, setuptools, etc.
pip3 install --force-reinstall --no-deps --no-build-isolation .

if [ $? -ne 0 ]; then
    echo
    echo "Failed to build SIP SIMPLE SDK"
    echo
    exit 1
fi

# Confirm the freshly built extension actually picked up bcg729 (if it was
# present). IMPORTANT: cd out of $SIPSIMPLE_DIR before importing. Otherwise
# CWD is on sys.path and any stray in-tree sipsimple/core/_core*.so will
# shadow the freshly installed wheel.
echo
echo "Verifying installed _core extension ..."
INSTALLED_SO="$(cd / && python3 -c 'import sipsimple.core._core; print(sipsimple.core._core.__file__)' || true)"
if [ -z "$INSTALLED_SO" ]; then
    echo "  (could not import sipsimple.core._core to verify)"
    echo "  Skipping 04b-copy_sipsimple.sh — fix the import error first."
else
    if [ -f "/opt/local/lib/libbcg729.dylib" ]; then
        echo "  extension: $INSTALLED_SO"
        if otool -L "$INSTALLED_SO" 2>/dev/null | grep -qi bcg729; then
            BCG729_LINE=$(otool -L "$INSTALLED_SO" 2>/dev/null | grep -i bcg729 | head -1 | awk '{print $1}')
            echo "  G.729 codec: verified — _core.so links $BCG729_LINE"
            echo "               (05-copy-libraries.sh will discover and bundle this dylib)."
        else
            echo "  WARNING: bcg729 was present at build time but _core.so does not link it."
            echo "           Check setup_pjsip.py output above for 'Found bcg729 at ...'."
        fi
    else
        echo "  G.729: bcg729 not installed, skipping codec verification."
    fi

    # _core imported cleanly — propagate the fresh build into Blink's
    # bundled Resources/lib/sipsimple/ so the next Xcode build picks it up.
    echo
    if [ -x "$SCRIPT_DIR/04b-copy_sipsimple.sh" ]; then
        echo "Running 04b-copy_sipsimple.sh to refresh Blink.app's bundled copy ..."
        ( cd "$SCRIPT_DIR" && ./04b-copy_sipsimple.sh )
    else
        echo "NOTE: $SCRIPT_DIR/04b-copy_sipsimple.sh not executable — skipping bundle refresh."
    fi
fi
