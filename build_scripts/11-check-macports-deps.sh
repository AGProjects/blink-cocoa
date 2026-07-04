#!/bin/bash
#
# 11-check-macports-deps.sh — verify every /opt/local (MacPorts) library that
# the sipsimple _core links is a UNIVERSAL binary (x86_64 + arm64).
#
# This is the LINK-TIME companion to 10-check-universal-libs.sh (which checks
# the already-bundled output under Distribution/). MacPorts libs at /opt/local
# periodically get rebuilt arm64-only (an OS/Xcode bump triggers single-arch
# rebuilds). Because the x86_64 cross-build links extensions with
# -undefined dynamic_lookup, a single-arch dependency is SILENTLY dropped from
# the x86_64 slice — and the Intel app then crashes at startup with e.g.:
#     dlopen(_core…): symbol not found in flat namespace '_av_codec_next'
#     dlopen(_core…): symbol not found in flat namespace '_vpx_codec_vp8_cx'
# Run this BEFORE building/shipping to catch it while it's a one-line port fix.
#
# Usage:
#   ./11-check-macports-deps.sh [path/to/_core.cpython-XXX-darwin.so]
# With no argument it inspects the venv build, then falls back to the bundled
# copy under ../Distribution.
#
# Exit status: 0 if every /opt/local dependency is universal; non-zero if any
# is single-arch.
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REQUIRED="x86_64 arm64"

command -v lipo >/dev/null 2>&1 || { echo "error: lipo not found (run on macOS)." >&2; exit 1; }
[ -x ./get_deps_recurrent.py ] || { echo "error: get_deps_recurrent.py missing/not executable." >&2; exit 1; }

# --- locate a _core to inspect ---------------------------------------------
core="${1:-}"
if [ -z "$core" ]; then
    sp="$(./get_site_packages_folder.sh 2>/dev/null)"
    cver="$(./get_python_version.sh 2>/dev/null | sed -r 's/\.//g')"
    for cand in \
        "$sp/sipsimple/core/_core.cpython-$cver-darwin.so" \
        "../Distribution/Resources/lib/sipsimple/core/_core.cpython-$cver-darwin.so"
    do
        [ -f "$cand" ] && { core="$cand"; break; }
    done
fi

if [ -z "$core" ] || [ ! -f "$core" ]; then
    echo "error: sipsimple _core extension not found." >&2
    echo "       Build the SDK first (04-install_sipsimple.sh) or pass the path." >&2
    exit 1
fi

echo "Auditing /opt/local libraries linked by:"
echo "  $core"
echo "Required architectures: $REQUIRED"
echo

# --- check each /opt/local dependency --------------------------------------
bad=0
checked=0
for l in $(./get_deps_recurrent.py "$core"); do
    [ -f "$l" ] || continue
    checked=$((checked + 1))
    archs="$(lipo -archs "$l" 2>/dev/null)"
    if echo "$archs" | grep -q x86_64 && echo "$archs" | grep -q arm64; then
        :
    else
        printf '  SINGLE-ARCH  %-46s [%s]\n' "$l" "$archs"
        bad=$((bad + 1))
    fi
done

echo
echo "Checked $checked /opt/local dependencies."
if [ "$bad" -eq 0 ]; then
    echo "OK — all MacPorts dependencies are universal ($REQUIRED)."
    exit 0
fi

cat >&2 <<'EOF'

FAILED — the MacPorts dependencies listed above are single-arch. The x86_64
cross-build links with -undefined dynamic_lookup, so it will SILENTLY drop them
and the Intel build will crash at startup ("symbol not found in flat namespace").

Fix each offending port by reinstalling it +universal:

    port provides /opt/local/lib/<lib>.dylib        # find the port name
    sudo port -N -f uninstall <port>
    sudo port -N install      <port> +universal

(For ffmpeg use the custom Portfile in build_scripts/ffmpeg/, and rebuild its
deps x264 + libopus +universal FIRST.) Then re-run 04-install_sipsimple.sh and
05-copy-libraries.sh, and re-run this check until it passes.
EOF
exit 1
