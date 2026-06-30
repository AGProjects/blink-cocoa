#!/bin/bash
#
# Check that every compiled library produced / installed by the other build
# scripts is universal (contains BOTH the Intel x86_64 slice and the Apple
# Silicon arm64 slice). Anything that ships only one architecture would make
# the universal Blink build crash on the missing platform, so this script
# audits the bundled dylibs (and the compiled Python extensions) and prints
# a report of which libraries are single-arch.
#
# By default it scans everything the build installs under ../Distribution:
#   * ../Distribution/Frameworks   (bundled dylibs, Python.framework, Sparkle)
#   * ../Distribution/Resources    (compiled .so extensions, e.g. sipsimple
#                                   _core and the Crypto/WebKit modules)
#
# You can also pass one or more extra files/directories to scan:
#     ./10-check-universal-libs.sh /opt/local/lib
#
# Exit status:
#     0  every Mach-O library is universal (arm64 + x86_64)
#     1  at least one library is single-arch (or no libraries were found)
#
# Usage:
#     ./10-check-universal-libs.sh [extra-path ...]
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REQUIRED_ARCHS=(x86_64 arm64)

if ! command -v lipo >/dev/null 2>&1; then
    echo "error: 'lipo' not found — this script must run on macOS." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Work out which locations to scan.
# ---------------------------------------------------------------------------
scan_paths=()

# Everything the build installs lives under Distribution/Frameworks (bundled
# dylibs, Python.framework, Sparkle) and Distribution/Resources (the compiled
# .so Python extensions).
scan_paths+=( "../Distribution/Frameworks" )
scan_paths+=( "../Distribution/Resources" )

# Any extra paths the caller passed on the command line.
for arg in "$@"; do
    scan_paths+=( "$arg" )
done

# ---------------------------------------------------------------------------
# Collect candidate Mach-O libraries (.dylib and .so) from the scan paths.
# ---------------------------------------------------------------------------
# Per-arch staging directories are single-arch on purpose (they feed
# make-universal-libs.sh), so skip them to avoid false positives.
prune_dirs=(libs-arm64 libs-x86_64 lib-arm64 lib-x86_64 lib-)

libs=()
for p in "${scan_paths[@]}"; do
    if [ -f "$p" ]; then
        libs+=( "$p" )
    elif [ -d "$p" ]; then
        prune_expr=()
        for d in "${prune_dirs[@]}"; do
            prune_expr+=( -name "$d" -o )
        done
        while IFS= read -r f; do
            libs+=( "$f" )
        done < <(find "$p" \( -type d \( "${prune_expr[@]}" -false \) -prune \) -o \
                          \( -type f \( -name '*.dylib' -o -name '*.so' \) -print \) 2>/dev/null)
    fi
done

if [ ${#libs[@]} -eq 0 ]; then
    echo "No .dylib/.so libraries found in:"
    for p in "${scan_paths[@]}"; do echo "  $p"; done
    echo
    echo "Run the build scripts first (e.g. 04-install_sipsimple.sh, 05-copy-libraries.sh)."
    exit 1
fi

# De-duplicate (a symlink and its target, repeated paths, etc.).
libs=($(printf '%s\n' "${libs[@]}" | sort -u))

# ---------------------------------------------------------------------------
# Inspect each library with lipo.
# ---------------------------------------------------------------------------
universal=()      # has every required arch
single=()         # missing at least one required arch  -> "path :: archs"
total=0

for lib in "${libs[@]}"; do
    # Resolve symlinks so we don't report the same physical file twice with a
    # confusing arch (and skip dangling links).
    real="$(readlink -f "$lib" 2>/dev/null || echo "$lib")"
    [ -f "$real" ] || continue

    archs="$(lipo -archs "$real" 2>/dev/null)"
    # Not a Mach-O file (text stub, script, etc.) — skip silently.
    [ -z "$archs" ] && continue

    total=$((total + 1))

    missing=()
    for want in "${REQUIRED_ARCHS[@]}"; do
        case " $archs " in
            *" $want "*) ;;
            *) missing+=( "$want" ) ;;
        esac
    done

    if [ ${#missing[@]} -eq 0 ]; then
        universal+=( "$lib" )
    else
        single+=( "$lib :: [$archs]  missing: ${missing[*]}" )
    fi
done

# ---------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------
echo "Checked $total Mach-O libraries (required arches: ${REQUIRED_ARCHS[*]})."
echo "  universal : ${#universal[@]}"
echo "  single-arch: ${#single[@]}"
echo

if [ ${#single[@]} -eq 0 ]; then
    echo "OK — every library is universal (${REQUIRED_ARCHS[*]})."
    exit 0
fi

echo "The following libraries are NOT universal:"
echo
for entry in "${single[@]}"; do
    echo "  $entry"
done
echo
echo "Rebuild these for both architectures. For MacPorts deps, install the"
echo "port +universal (see 02-install-c-deps.sh); for lipo-merged libs see"
echo "make-universal-libs.sh / make-universal-sipsimple.sh."
exit 1
