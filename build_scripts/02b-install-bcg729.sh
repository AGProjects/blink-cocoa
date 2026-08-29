#!/bin/bash
#
# Install bcg729 (Belledonne G.729) into the MacPorts prefix so that the
# python3-sipsimple build (step 04) auto-detects it and PJSIP enables the
# G.729 codec. The resulting libbcg729.0.dylib is then auto-discovered by
# 05-copy-libraries.sh, copied into Blink.app/Contents/Frameworks/libs/
# via get_deps_recurrent.py, and re-targeted by change_lib_paths.sh.
#
# bcg729 is NOT in MacPorts or Homebrew, so we build it from upstream.
#
# Apple Silicon: build a universal (arm64 + x86_64) dylib so it works for
# both slices of the universal Blink build — matches the "+universal"
# MacPorts ports installed in 02-install-c-deps.sh.
#
# Fail-soft semantics:
#   - If anything goes wrong (no network, cmake missing, build error,
#     sudo declined, tag moved, etc.) we print a clear warning and exit 0.
#     Subsequent build steps then proceed and simply omit G.729 support.
#
# Re-running is safe: if bcg729 is already installed at the target prefix,
# we skip the build and just re-verify the dylib's install_name.
#
# Knobs (env vars):
#   BCG729_PREFIX   install prefix              (default: /opt/local)
#   BCG729_TAG      git tag to build            (default: 1.1.1)
#   BCG729_REPO     git repo URL                (default: github mirror)
#

set -u

PREFIX="${BCG729_PREFIX:-/opt/local}"
TAG="${BCG729_TAG:-1.1.1}"
REPO="${BCG729_REPO:-https://github.com/BelledonneCommunications/bcg729.git}"
WORK="${TMPDIR:-/tmp}/bcg729-build-$$"

skip() {
    echo
    echo "WARNING: bcg729 install skipped — $*"
    echo "         Blink will be built without G.729 codec support."
    echo "         Re-run this script after fixing the issue to add G.729 later."
    echo
    rm -rf "$WORK" 2>/dev/null || true
    exit 0
}

fix_install_name() {
    # MacPorts convention: every dylib under /opt/local/lib must have its
    # install_name (LC_ID_DYLIB) set to its own absolute path. bcg729's
    # CMake build defaults to @rpath/libbcg729.0.dylib, which fails dlopen
    # from any consumer that doesn't carry an LC_RPATH for /opt/local/lib —
    # and PJSIP / sipsimple's _core.so don't add one. Patch each concrete
    # dylib so it advertises an absolute path; consumers linked against it
    # from this point forward record the absolute path and dlopen works.
    #
    # On Apple Silicon any LC_ID_DYLIB edit invalidates the ad-hoc signature,
    # so we re-sign immediately afterwards.
    [ "$(uname -s)" = "Darwin" ] || return 0

    command -v otool             >/dev/null 2>&1 || skip "otool not found (install Xcode command line tools: xcode-select --install)"
    command -v install_name_tool >/dev/null 2>&1 || skip "install_name_tool not found (xcode-select --install)"
    command -v codesign          >/dev/null 2>&1 || skip "codesign not found (xcode-select --install)"

    local dylib id fixed_any=0
    for dylib in "$PREFIX"/lib/libbcg729*.dylib; do
        [ -f "$dylib" ] || continue
        [ -L "$dylib" ] && continue          # skip symlinks; patch the real file
        id=$(otool -D "$dylib" 2>/dev/null | tail -n 1 | xargs)
        case "$id" in
            "$dylib")
                ;;
            @rpath/*|@loader_path/*|@executable_path/*|"")
                echo "  patching install_name on $dylib (was: ${id:-<unset>}) ..."
                sudo install_name_tool -id "$dylib" "$dylib" \
                    || skip "install_name_tool -id failed on $dylib"
                sudo codesign --force --sign - "$dylib" >/dev/null 2>&1 \
                    || skip "codesign --force --sign - failed on $dylib (Apple Silicon requires re-signing)"
                fixed_any=1
                ;;
            /*)
                # Some other absolute path — leave it alone.
                ;;
        esac

        local now
        now=$(otool -D "$dylib" 2>/dev/null | tail -n 1 | xargs)
        if [ "$now" != "$dylib" ] && [ "${now#/}" = "$now" ]; then
            skip "install_name on $dylib is '$now' after patching; expected '$dylib'"
        fi
    done

    if [ "$fixed_any" = "1" ]; then
        echo "  install_name normalised; any consumer linked against bcg729 from now on"
        echo "  will record the absolute path (no LC_RPATH needed)."
    fi
}

verify_arch() {
    # On Apple Silicon, Blink builds universal. Make sure the dylib is fat
    # (arm64 + x86_64); warn if not, since the x86 slice (04-...-x86.sh)
    # would fail to link otherwise.
    [ "$(uname -s)" = "Darwin" ] || return 0
    if ! uname -v | grep -q ARM64; then
        return 0
    fi

    local dylib
    for dylib in "$PREFIX"/lib/libbcg729*.dylib; do
        [ -f "$dylib" ] || continue
        [ -L "$dylib" ] && continue
        local archs
        archs=$(lipo -archs "$dylib" 2>/dev/null || true)
        echo "  $dylib  archs: ${archs:-<unknown>}"
        case "$archs" in
            *arm64*x86_64*|*x86_64*arm64*)
                ;;
            *)
                echo "  NOTE: $dylib is not universal — the x86_64 Blink slice"
                echo "        (04-install_sipsimple-x86.sh under Rosetta) will fail to link."
                echo "        Re-run this script (delete and rebuild) to get a universal dylib."
                ;;
        esac
    done
}

# On Apple Silicon we need a universal dylib. If the already-installed lib
# is single-arch (i.e. was built by an older version of this script that
# predated the universal flag), report it and return 1 so the caller can
# force a rebuild. Returns 0 if everything is fine (or non-Darwin, or non-AS).
needs_universal_rebuild() {
    [ "$(uname -s)" = "Darwin" ] || return 1
    uname -v | grep -q ARM64       || return 1
    command -v lipo >/dev/null 2>&1 || return 1

    local dylib archs
    for dylib in "$PREFIX"/lib/libbcg729*.dylib; do
        [ -f "$dylib" ] || continue
        [ -L "$dylib" ] && continue
        archs=$(lipo -archs "$dylib" 2>/dev/null || true)
        case "$archs" in
            *arm64*x86_64*|*x86_64*arm64*)
                ;;
            *)
                return 0   # found a non-universal dylib → rebuild needed
                ;;
        esac
    done
    return 1
}

# Force a clean wipe of the existing install so the build path runs with
# fresh state. Used when the existing install is single-arch on AS.
wipe_existing_install() {
    echo "Removing existing single-arch bcg729 install from $PREFIX ..."
    sudo rm -f  "$PREFIX"/lib/libbcg729* \
                "$PREFIX"/lib/pkgconfig/libbcg729.pc \
                2>/dev/null || true
    sudo rm -rf "$PREFIX"/include/bcg729 2>/dev/null || true
}

# 0) Already installed? (probe the same files setup_pjsip.py probes)
if [ -f "$PREFIX/include/bcg729/encoder.h" ] && \
   { [ -f "$PREFIX/lib/libbcg729.dylib" ] || \
     [ -f "$PREFIX/lib/libbcg729.a" ]    || \
     [ -f "$PREFIX/lib/libbcg729.so" ]; }; then
    echo "bcg729 already installed at $PREFIX."
    if needs_universal_rebuild; then
        echo "Existing install is single-arch on Apple Silicon — forcing universal rebuild."
        wipe_existing_install
        # fall through to the build path below
    else
        echo "Verifying dylib install_name (macOS only) ..."
        fix_install_name
        verify_arch
        echo "bcg729 ready at $PREFIX — setup_pjsip.py will pick it up automatically."
        exit 0
    fi
fi

# 1) Tooling
command -v cmake >/dev/null 2>&1 || skip "cmake not found (sudo port install cmake)"
command -v git   >/dev/null 2>&1 || skip "git not found"
command -v sudo  >/dev/null 2>&1 || skip "sudo not found (install needs root for $PREFIX)"

# 2) Fetch
mkdir -p "$WORK" || skip "cannot create $WORK"
cd "$WORK"       || skip "cannot enter $WORK"

echo "Cloning bcg729 $TAG from $REPO ..."
if ! git clone --depth 1 --branch "$TAG" "$REPO" src 2>/dev/null; then
    echo "Tag $TAG not reachable; falling back to default branch ..."
    git clone --depth 1 "$REPO" src || skip "git clone of bcg729 failed (no network?)"
fi

# 3) Configure + build
cd src || skip "cloned src directory missing"

JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 2)"

# On Apple Silicon: build universal (matches the +universal MacPorts ports
# installed in 02-install-c-deps.sh so the x86_64 Blink slice can link too).
CMAKE_ARCH_ARGS=()
if [ "$(uname -s)" = "Darwin" ] && uname -v | grep -q ARM64; then
    CMAKE_ARCH_ARGS+=( -DCMAKE_OSX_ARCHITECTURES="arm64;x86_64" )
    echo "Apple Silicon detected — building universal (arm64;x86_64)."
fi

# bcg729 is not a MacPorts port, so macports.conf's macosx_deployment_target
# does not reach it: without this, cmake stamps LC_BUILD_VERSION with the
# build host's macOS and 05-copy-libraries.sh's minOS guard rejects the dylib.
# Keep in sync with BLINK_MIN_OS there.
BLINK_MIN_OS="${BLINK_MIN_OS:-13.0}"
echo "Building bcg729 for macOS $BLINK_MIN_OS or later."

cmake -B build -S . \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON \
    -DENABLE_TESTS=NO \
    -DCMAKE_INSTALL_NAME_DIR="$PREFIX/lib" \
    -DCMAKE_MACOSX_RPATH=OFF \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="$BLINK_MIN_OS" \
    "${CMAKE_ARCH_ARGS[@]}" \
    >/dev/null || skip "cmake configure failed"

cmake --build build -j"$JOBS" || skip "cmake build failed"

# 4) Install (needs sudo for /opt/local — same pattern as 02-install-c-deps.sh)
echo "Installing bcg729 to $PREFIX (sudo) ..."
sudo cmake --install build || skip "sudo cmake --install failed"

# 5) Verify the files setup_pjsip.py looks for actually landed
if [ ! -f "$PREFIX/include/bcg729/encoder.h" ]; then
    skip "headers not found at $PREFIX/include/bcg729/ after install"
fi
if [ ! -f "$PREFIX/lib/libbcg729.dylib" ] && \
   [ ! -f "$PREFIX/lib/libbcg729.a" ]    && \
   [ ! -f "$PREFIX/lib/libbcg729.so" ]; then
    skip "library not found at $PREFIX/lib/ after install"
fi

# Normalise install_name + verify architecture.
fix_install_name
verify_arch

echo
echo "bcg729 $TAG installed at $PREFIX."
echo "setup_pjsip.py will auto-detect it and enable PJMEDIA_HAS_BCG729=1."
echo "05-copy-libraries.sh will auto-bundle libbcg729 into Blink.app."
echo

rm -rf "$WORK"
exit 0
