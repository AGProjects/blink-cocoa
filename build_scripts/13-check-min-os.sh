#!/bin/bash
#
# Check that every compiled library produced / installed by the other build
# scripts was built for BLINK_MIN_OS or older.
#
# MacPorts -- and any ./configure or cmake run without an explicit deployment
# target -- stamps LC_BUILD_VERSION with the *build host's* macOS version. A
# dylib built on macOS 26 inside an app whose deployment target is 13.0 is an
# unsupported combination: dyld reports "built for macOS 26.0 which is newer
# than running OS", and, worse, the port's configure step probed the host libc,
# so references to symbols the deployment target does not have get baked in
# (05-copy-libraries.sh has a companion guard for the symbols we know about).
#
# By default it scans everything the build installs under ../Distribution:
#   * ../Distribution/Frameworks   (bundled dylibs, Python.framework, Sparkle)
#   * ../Distribution/Resources    (compiled .so extensions, e.g. sipsimple
#                                   _core and the Crypto/WebKit modules)
#
# You can also pass one or more extra files/directories to scan, which is how
# you audit a finished bundle or the MacPorts prefix:
#     ./13-check-min-os.sh ~/Applications/Blink.app
#     ./13-check-min-os.sh /opt/local/lib
#
# The target defaults to 13.0 (macOS Ventura) and must be kept in sync with
# MACOSX_DEPLOYMENT_TARGET in Blink.xcodeproj, LSMinimumSystemVersion in the
# Info plists, and BLINK_MIN_OS in 05-copy-libraries.sh / 02b-install-bcg729.sh.
# Override for a one-off check with:
#     BLINK_MIN_OS=14.0 ./13-check-min-os.sh
#
# Exit status:
#     0  every Mach-O library targets BLINK_MIN_OS or older
#     1  at least one library was built too new (or none were found)
#
# Usage:
#     ./13-check-min-os.sh [extra-path ...]
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# The version itself lives in blink_min_os.sh so it cannot drift from the
# Xcode targets, the Info plists and macports.conf.
# shellcheck source=blink_min_os.sh
source "$SCRIPT_DIR/blink_min_os.sh"

for tool in lipo otool; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: '$tool' not found — this script must run on macOS." >&2
        exit 1
    fi
done

# Minimum OS of one architecture slice: LC_BUILD_VERSION on modern toolchains,
# LC_VERSION_MIN_MACOSX on older ones (e.g. the python.org framework).
minos_of() {
    otool -arch "$2" -l "$1" 2>/dev/null | awk '
        $1 == "cmd" && $2 == "LC_BUILD_VERSION"      { want = "minos";   next }
        $1 == "cmd" && $2 == "LC_VERSION_MIN_MACOSX" { want = "version"; next }
        want != "" && $1 == want                     { print $2; exit }'
}

# ---------------------------------------------------------------------------
# Work out which locations to scan.
# ---------------------------------------------------------------------------
scan_paths=()
scan_paths+=( "../Distribution/Frameworks" )
scan_paths+=( "../Distribution/Resources" )

for arg in "$@"; do
    scan_paths+=( "$arg" )
done

# ---------------------------------------------------------------------------
# Collect candidate Mach-O files from the scan paths.
# ---------------------------------------------------------------------------
# Unlike the universal check, the per-arch staging directories are scanned too:
# a wrong deployment target in one of them is a real defect, and they get
# lipo-merged into the shipped libs.
libs=()
for p in "${scan_paths[@]}"; do
    if [ -f "$p" ]; then
        libs+=( "$p" )
    elif [ -d "$p" ]; then
        while IFS= read -r f; do
            libs+=( "$f" )
        done < <(find "$p" -type f \( -name '*.dylib' -o -name '*.so' -o \
                    \( -perm -111 ! -name '*.*' \) \) 2>/dev/null)
    fi
done

if [ ${#libs[@]} -eq 0 ]; then
    echo "No Mach-O libraries found in:"
    for p in "${scan_paths[@]}"; do echo "  $p"; done
    echo
    echo "Run the build scripts first (e.g. 04-install_sipsimple.sh, 05-copy-libraries.sh)."
    exit 1
fi

libs=($(printf '%s\n' "${libs[@]}" | sort -u))

# ---------------------------------------------------------------------------
# Inspect each slice of each library.
# ---------------------------------------------------------------------------
min_allowed="$(ver_num "$BLINK_MIN_OS")"
ok=0
bad=0             # libraries with at least one too-new slice
too_new=()        # "path :: arch minos X"
unstamped=()      # no LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX at all
total=0

for lib in "${libs[@]}"; do
    real="$(readlink -f "$lib" 2>/dev/null || echo "$lib")"
    [ -f "$real" ] || continue

    archs="$(lipo -archs "$real" 2>/dev/null)"
    # Not a Mach-O file (text stub, shell script, etc.) — skip silently.
    [ -z "$archs" ] && continue

    total=$((total + 1))

    lib_bad=0
    lib_stamped=0
    for arch in $archs; do
        v="$(minos_of "$real" "$arch")"
        if [ -z "$v" ]; then
            continue
        fi
        lib_stamped=1
        if [ "$(ver_num "$v")" -gt "$min_allowed" ]; then
            too_new+=( "$lib :: $arch built for macOS $v" )
            lib_bad=1
        fi
    done

    if [ "$lib_stamped" -eq 0 ]; then
        unstamped+=( "$lib :: [$archs]" )
    elif [ "$lib_bad" -eq 0 ]; then
        ok=$((ok + 1))
    else
        bad=$((bad + 1))
    fi
done

# ---------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------
echo "Checked $total Mach-O libraries (deployment target: macOS $BLINK_MIN_OS)."
echo "  ok         : $ok"
echo "  built too new: $bad"
[ ${#unstamped[@]} -gt 0 ] && echo "  unstamped  : ${#unstamped[@]}"
echo

if [ ${#unstamped[@]} -gt 0 ]; then
    echo "No LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX (informational — very old"
    echo "toolchains omit it; such binaries impose no minimum):"
    echo
    for entry in "${unstamped[@]}"; do
        echo "  $entry"
    done
    echo
fi

if [ ${#too_new[@]} -eq 0 ]; then
    echo "OK — every library targets macOS $BLINK_MIN_OS or older."
    exit 0
fi

echo "The following libraries were built for a NEWER macOS than $BLINK_MIN_OS:"
echo
for entry in "${too_new[@]}"; do
    echo "  $entry"
done
echo
echo "For MacPorts deps, set in /opt/local/etc/macports/macports.conf:"
echo
echo "    macosx_deployment_target  $BLINK_MIN_OS"
echo
echo "then, for each port above:"
echo
echo "    sudo port -f uninstall <port>"
echo "    sudo port clean <port>"
echo "    sudo port install <port>"
echo
echo "and re-run 05-copy-libraries.sh. bcg729 is not a MacPorts port — it picks"
echo "the target up from BLINK_MIN_OS in 02b-install-bcg729.sh. pjsip and the"
echo "sipsimple extensions come from min_osx_version in setup_pjsip.py."
exit 1
