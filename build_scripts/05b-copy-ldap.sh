#!/bin/bash
#
# 05b-copy-ldap.sh — put python-ldap and its libraries into the bundle,
# and nothing else.
#
# This is the LDAP-only slice of three scripts that otherwise have to be run
# in full:
#
#     05-copy-libraries.sh              libldap / liblber / libsasl2 + deps
#     install_python-deps-universal.sh  the universal _ldap extension
#     06-copy-python-packages.sh        ldap/ + _ldap.so into Resources/lib
#
# Running those three costs a wipe of Resources/lib and a re-sign of every
# dylib and .so in the bundle — several minutes — when all that changed is
# one package. This script touches only the LDAP files. Everything already
# in Distribution/ is left exactly as it is.
#
# It is not a replacement for 05/06 in a clean build. Use it when the bundle
# is already built and 12-check-ldap.sh reports the LDAP parts missing, or
# after upgrading the python-ldap or openldap version.
#
# What it does:
#   1. copies the versioned libldap/liblber/libsasl2 dylibs and their whole
#      /opt/local dependency closure into Frameworks/libs, re-paths them to
#      @executable_path/../Frameworks/libs/ and signs them
#   2. on Apple Silicon, rebuilds _ldap for both arches and lipo-merges it
#      (skip with --no-universal)
#   3. copies _ldap.cpython-*-darwin.so, the ldap package, ldif.py, ldapurl.py
#      and the dist-info into Resources/lib, re-paths and signs the extension
#
# Usage:
#   ./05b-copy-ldap.sh                 everything above
#   ./05b-copy-ldap.sh --libs-only     step 1 only (dylibs)
#   ./05b-copy-ldap.sh --ext-only      steps 2-3 only (no dylibs); this is how
#                                      install_python-deps-universal.sh builds
#                                      the universal _ldap during a full build
#   ./05b-copy-ldap.sh --no-universal  skip the per-arch rebuild (arm64 only)
#
# Env:
#   CODESIGN_ID   signing identity, default "Developer ID Application"
#
# Then run ./12-check-ldap.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LIBS_ONLY=0
EXT_ONLY=0
UNIVERSAL=1
while [ $# -gt 0 ]; do
    case "$1" in
        --libs-only)    LIBS_ONLY=1 ;;
        --ext-only)     EXT_ONLY=1 ;;
        --no-universal) UNIVERSAL=0 ;;
        -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

CODESIGN_ID="${CODESIGN_ID:-Developer ID Application}"
DIST="../Distribution"
LIBS="$DIST/Frameworks/libs"
PYLIB="$DIST/Resources/lib"

pver="$(./get_python_version.sh)"
cver="$(echo "$pver" | sed -r 's/\.//g')"
site_packages_folder="$(./get_site_packages_folder.sh)"

[ -d "$DIST" ] || { echo "error: $DIST not found — run this from build_scripts/." >&2; exit 1; }
[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ] || UNIVERSAL=0

sign() {
    codesign -f -o runtime --timestamp -s "$CODESIGN_ID" "$1" 2>/dev/null \
        || echo "    WARNING: could not sign $(basename "$1") with identity '$CODESIGN_ID'"
}

# --------------------------------------------------------------- dylibs ----

if [ "$EXT_ONLY" -eq 1 ]; then
    echo "==> Frameworks/libs (skipped: --ext-only)"
else

echo "==> Frameworks/libs"
mkdir -p "$LIBS"

ldap_libs=""
for l in /opt/local/lib/libldap.[0-9]*.dylib \
         /opt/local/lib/liblber.[0-9]*.dylib \
         /opt/local/lib/libsasl2.[0-9]*.dylib; do
    [ -f "$l" ] || continue
    ldap_libs="$ldap_libs $l $(./get_deps_recurrent.py "$l")"
done

if [ -z "$ldap_libs" ]; then
    echo "    error: no libldap/liblber under /opt/local/lib." >&2
    echo "           sudo port install openldap cyrus-sasl2   (or ./02-install-c-deps.sh)" >&2
    exit 1
fi

# The closure is deduplicated here rather than in the loop: libsasl2 and
# libldap share most of their dependencies.
for l in $(printf '%s\n' $ldap_libs | sort -u); do
    [ -f "$l" ] || continue
    fn="$(basename "$l")"
    archs="$(lipo -archs "$l" 2>/dev/null)"
    if [ "$UNIVERSAL" -eq 1 ]; then
        case " $archs " in
            *" x86_64 "*) ;;
            *) echo "    WARNING: $fn is $archs — the Intel build will not load it."
               echo "             Fix with ./02-install-c-deps.sh --only $(port provides "$l" 2>/dev/null | awk '{print $NF}')" ;;
        esac
    fi
    echo "    cp $l"
    cp -f "$l" "$LIBS/$fn"
    chmod u+w "$LIBS/$fn"
    ./change_lib_paths.sh "$LIBS/$fn"
    sign "$LIBS/$fn"
done

[ "$LIBS_ONLY" -eq 1 ] && { echo; echo "Done (libs only). Now: ./12-check-ldap.sh"; exit 0; }

fi

# ----------------------------------------------------- universal _ldap -----

ext_name="_ldap.cpython-$cver-darwin.so"

if ! (source ./activate_venv.sh && python3 -c "import ldap" >/dev/null 2>&1); then
    echo
    echo "error: python-ldap is not installed in the venv." >&2
    echo "       Run ./03-install-python-deps.sh first." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Building _ldap for both architectures.
#
# ARCHFLAGS is the knob that decides the output slices: _osx_support's
# compiler_fixup() strips every -arch the interpreter's sysconfig baked in and
# substitutes ARCHFLAGS. Running the build under `arch -x86_64` does NOT do
# this — the compiler still gets whatever sysconfig says — which is why the
# per-arch loop is driven by ARCHFLAGS and not by `arch`.
#
# --no-cache-dir is not optional. pip caches wheels it builds itself, keyed by
# the platform tag, and a universal2 interpreter reports the SAME tag
# (macosx-*-universal2) whichever architecture it is running as. Without it,
# the second pass silently reinstalls the wheel the first pass built and both
# "slices" come out as the same architecture — lipo then refuses them with
#     have the same architectures (x86_64) and can't be in the same fat file
#
# The extension links with -undefined dynamic_lookup and never links
# libpython, so the x86_64 slice builds even from an arm64-only interpreter,
# as long as the MacPorts libraries it links are universal.
# ---------------------------------------------------------------------------

source ./activate_venv.sh
ldap_ver="$(python3 -c "import importlib.metadata as m; print(m.version('python-ldap'))" 2>/dev/null)"

# Same flags 03-install-python-deps.sh uses: MacPorts openldap headers, plus
# cyrus-sasl2's sasl.h, which python-ldap includes as <sasl.h>.
export CFLAGS="-I/opt/local/include -I/opt/local/include/sasl"
export LDFLAGS="-L/opt/local/lib"

# build_ext <archflags> <destination> — compile python-ldap into the venv and
# copy the resulting extension out. Fails if pip fails or nothing was built.
build_ext() {
    ARCHFLAGS="$1" pip3 install --force-reinstall --no-deps --no-binary python-ldap \
        --no-cache-dir "python-ldap==$ldap_ver" > /dev/null 2>&1 || return 1
    [ -f "$site_packages_folder/$ext_name" ] || return 1
    cp -f "$site_packages_folder/$ext_name" "$2" || return 1
    chmod u+w "$2"
}

has_arch() {
    case " $(lipo -archs "$1" 2>/dev/null) " in *" $2 "*) return 0 ;; *) return 1 ;; esac
}

built=0

if [ "$UNIVERSAL" -eq 1 ]; then
    echo
    echo "==> Building $ext_name (python-ldap $ldap_ver)"

    # One pass, both slices. The MacPorts libraries are universal, so clang
    # can emit a fat extension directly and there is nothing to lipo.
    echo "    universal ..."
    if build_ext "-arch arm64 -arch x86_64" "$PYLIB/$ext_name" \
       && has_arch "$PYLIB/$ext_name" arm64 && has_arch "$PYLIB/$ext_name" x86_64; then
        echo "    $(lipo -info "$PYLIB/$ext_name")"
        built=1
    else
        # Fall back to one build per architecture. x86_64 first, so the venv
        # is left holding the arm64 build the build machine can import.
        echo "    single fat build did not produce both slices, building per arch"
        ok=1
        for arch in x86_64 arm64; do
            mkdir -p "$DIST/Resources/lib-$arch"
            dst="$DIST/Resources/lib-$arch/$ext_name"
            echo "    $arch ..."
            if build_ext "-arch $arch" "$dst" && has_arch "$dst" "$arch"; then
                :
            else
                echo "    WARNING: $arch build failed or produced [$(lipo -archs "$dst" 2>/dev/null)]"
                ok=0
            fi
        done

        if [ "$ok" -eq 1 ]; then
            lipo -create -output "$PYLIB/$ext_name" \
                "$DIST/Resources/lib-arm64/$ext_name" \
                "$DIST/Resources/lib-x86_64/$ext_name" \
                && built=1
            [ "$built" -eq 1 ] && echo "    $(lipo -info "$PYLIB/$ext_name")"
        fi
    fi
fi

# Whatever happened above, the bundle must not be left without an extension:
# a missing _ldap.so is the `ldap = Null` failure this whole exercise exists
# to prevent. A single-arch one at least works on this machine, and
# 12-check-ldap.sh reports it as not shippable.
if [ "$built" -eq 0 ]; then
    [ "$UNIVERSAL" -eq 1 ] && echo "    WARNING: falling back to the single-arch extension from the venv"
    cp -f "$site_packages_folder/$ext_name" "$PYLIB/$ext_name" \
        || { echo "error: no $ext_name in $site_packages_folder" >&2; exit 1; }
    chmod u+w "$PYLIB/$ext_name"
fi

# ------------------------------------------------------- python package ----

echo
echo "==> Resources/lib"
mkdir -p "$PYLIB"

for item in ldap ldif.py ldapurl.py ldap_dn.py; do
    src="$site_packages_folder/$item"
    [ -e "$src" ] || continue
    echo "    cp $item"
    chmod -R u+w "$PYLIB/$item" 2>/dev/null || true
    rm -rf "${PYLIB:?}/$item"
    cp -a "$src" "$PYLIB/$item"
done

for d in "$site_packages_folder"/python_ldap-*.dist-info; do
    [ -d "$d" ] || continue
    echo "    cp $(basename "$d")"
    rm -rf "$PYLIB/$(basename "$d")"
    cp -a "$d" "$PYLIB/"
done

# python-ldap imports pyasn1 / pyasn1_modules for the paged-results and
# extended-operation controls. They are pulled in as dependencies elsewhere,
# but a bundle built before python-ldap existed may not have them.
for m in pyasn1 pyasn1_modules; do
    if [ ! -d "$PYLIB/$m" ] && [ -d "$site_packages_folder/$m" ]; then
        echo "    cp $m (python-ldap dependency, missing from the bundle)"
        cp -a "$site_packages_folder/$m" "$PYLIB/$m"
    fi
done

# Shipping the build machine's bytecode just adds bulk; Python regenerates it.
find "$PYLIB/ldap" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
find "$PYLIB/ldap" -name '*.py[co]' -delete 2>/dev/null

./change_lib_paths.sh "$PYLIB/$ext_name"
sign "$PYLIB/$ext_name"

echo
echo "Done. Now: ./12-check-ldap.sh"
