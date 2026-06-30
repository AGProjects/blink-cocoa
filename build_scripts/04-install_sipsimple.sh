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

chmod +x ./get_dependencies*
# Export so setup_pjsip.py / any sub-script also sees the choice.
export PJSIP_VERSION

# Make sure the build can find MacPorts headers/libs in this shell.
# (The per-pass target arch is injected via SIPSIMPLE_TARGET_ARCH in
# build_sipsimple(), which setup_pjsip.py turns into -arch.)
export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# Force PJSIP to link libvpx via --with-vpx=/opt/local instead of relying on
# auto-detection. Auto-detect succeeds for the native (arm64) build but FAILS
# in the x86_64 cross build, so -lvpx never lands in PJ_LDLIBS and the x86_64
# _core.so ends up with undefined _vpx_codec_* symbols (dlopen fails on Intel
# with "symbol not found in flat namespace '_vpx_codec_vp8_cx'"). Setting this
# env var makes setup_pjsip.py pass --with-vpx for every arch. /opt/local is
# the MacPorts prefix (has include/ and lib/).
export SIPSIMPLE_LIBVPX_PATH="${SIPSIMPLE_LIBVPX_PATH:-/opt/local}"

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

# Where the freshly built extensions land, and where we stage per-arch slices.
cver="$(cd "$SCRIPT_DIR" && ./get_python_version.sh | sed -r 's/\.//g')"
SP="$(cd "$SCRIPT_DIR" && ./get_site_packages_folder.sh)"
DIST="$SCRIPT_DIR/../Distribution"

# A clean source/deps tree is required for every (re)build. Without this:
#   - the stale build/ tree (old pjsip + old _core.so) is reused
#   - pip sees the same version already installed and skips reinstalling
#   - a leftover in-tree _core.so shadows the freshly installed wheel when
#     CWD is on sys.path
# Wiping also guarantees pjsip is recompiled for the architecture we're about
# to target (objects are not reused across arches).
clean_tree() {
    echo "Cleaning previous build artifacts in $SIPSIMPLE_DIR ..."
    rm -rf deps/pjsip deps/ZRTPCPP deps/pjproject-* 2>/dev/null || true
    rm -rf build/ build_inplace/ python3_sipsimple.egg-info/
    find sipsimple -name "_core*.so" -print -delete 2>/dev/null || true
    pip3 uninstall -y python3-sipsimple >/dev/null 2>&1 || true
}

# Build pjsip + the _core/_sha1 extensions for one architecture, then verify
# the produced _core.so really contains that slice. Pass "native" to build
# without an `arch` wrapper (Intel / single-arch path).
# --no-build-isolation so the build sees the venv's Cython, setuptools, etc.
build_sipsimple() {
    local arch="$1" runner=""
    if [ "$arch" != "native" ]; then
        runner="arch -$arch"
        # setup_pjsip.py hardcodes arch_flags WITHOUT -arch and then forces
        # os.environ['ARCHFLAGS'] to it, so neither the `arch -X` wrapper nor a
        # plain ARCHFLAGS/CFLAGS export can change the produced slice — the
        # compiler defaults to the host's native arch (arm64). setup_pjsip.py
        # has been patched to honor SIPSIMPLE_TARGET_ARCH; set it so pjproject
        # AND the _core/_sha1 extension are built (and link libvpx) for the
        # slice we're actually producing.
        export SIPSIMPLE_TARGET_ARCH="$arch"
    else
        unset SIPSIMPLE_TARGET_ARCH
    fi
    echo
    echo ">>> Building sipsimple (${arch}) ..."
    clean_tree
    $runner ./get_dependencies.sh --version "$PJSIP_VERSION"
    # --no-cache-dir is essential: pip caches the built wheel, and on a second
    # run --force-reinstall happily reinstalls the CACHED wheel instead of
    # recompiling — so source/flag changes (and the per-arch slice itself)
    # silently don't take effect. Always build fresh.
    $runner pip3 install --force-reinstall --no-deps --no-build-isolation --no-cache-dir .
    if [ "$arch" != "native" ]; then
        local core="$SP/sipsimple/core/_core.cpython-$cver-darwin.so"
        local got; got="$(lipo -archs "$core" 2>/dev/null)"
        case " $got " in
            *" $arch "*) echo "    _core.so arch OK ($got)" ;;
            *) echo "ERROR: built _core.so for $arch but lipo reports '$got'." >&2
               exit 1 ;;
        esac
    fi
}

# Stage the just-built _core/_sha1 into Resources/lib-<arch>/ so
# make-universal-sipsimple.sh can lipo them together at the end.
stage_slice() {
    local arch="$1"
    mkdir -p "$DIST/Resources/lib-$arch"
    cp "$SP/sipsimple/core/_core.cpython-$cver-darwin.so" "$DIST/Resources/lib-$arch/"
    cp "$SP/sipsimple/util/_sha1.cpython-$cver-darwin.so" "$DIST/Resources/lib-$arch/"
    # Rewrite /opt/local install names to @executable_path/../Frameworks/libs/
    # BEFORE the slices are lipo'd together. Otherwise make-universal-sipsimple.sh
    # would merge these raw slices over the path-fixed copy 04b produced, leaving
    # the final _core/_sha1 pointing back at /opt/local. The merge codesigns the
    # result afterwards, so the fixed paths end up in a valid signature.
    "$SCRIPT_DIR/change_lib_paths.sh" \
        "$DIST/Resources/lib-$arch/_core.cpython-$cver-darwin.so" \
        "$DIST/Resources/lib-$arch/_sha1.cpython-$cver-darwin.so"
}

# On Apple Silicon build a universal SDK: compile x86_64 first, stage it, then
# compile arm64 LAST so the venv is left with the native (arm64) install. The
# two staged slices are merged into the bundle by make-universal-sipsimple.sh
# at the very end. Set SIPSIMPLE_UNIVERSAL=0 to force a single-arch build.
BUILD_UNIVERSAL=0
if [ "$(uname -m)" = "arm64" ] && [ "${SIPSIMPLE_UNIVERSAL:-1}" != "0" ]; then
    BUILD_UNIVERSAL=1
fi

if [ "$BUILD_UNIVERSAL" = "1" ]; then
    build_sipsimple x86_64
    stage_slice    x86_64
    build_sipsimple arm64
    stage_slice    arm64
else
    build_sipsimple native
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

    # On Apple Silicon, 04b just copied the single-arch (arm64) _core/_sha1.
    # Replace them with a universal binary lipo'd from the two staged slices.
    if [ "$BUILD_UNIVERSAL" = "1" ]; then
        echo
        echo "Merging arm64 + x86_64 slices into universal _core/_sha1 ..."
        ( cd "$SCRIPT_DIR" && ./make-universal-sipsimple.sh )
    fi
fi
