#!/bin/bash
#
# Rebuild the ports whose libraries ship inside Blink.app, for the current
# minimum macOS (blink_min_os.sh).
#
# Run this after changing BLINK_MIN_OS, after an OS or Xcode upgrade, or when
# 13-check-min-os.sh reports libraries built for a newer macOS than the .app
# targets.
#
# Why per-port verbs rather than one port command: `port upgrade --force` is
# right for an installed port -- the current version stays active until the
# replacement is staged, so ports rebuilt later in the run can still find
# their build dependencies -- but it errors with "<port> is not installed" on
# anything missing. A mixed set needs both verbs, chosen per port.
#
# Never use `port -f uninstall` across the whole set to force a rebuild. It
# strands the build-time dependencies of everything later in the run: libffi
# and gmp go missing halfway through and python314, mpfr and libmpc then fail
# to configure against them.
#
# Ports are installed with -k so the per-architecture destroots survive. That
# lets this script repair, immediately, any header muniversal clobbered with
# an empty ar archive (gmp.h, ffi.h, ffitarget.h are the known casualties);
# without it the damage is found hours later when something fails to compile.
# Work directories are cleaned once the port is verified.
#
# Usage:
#     ./bundled_ports_rebuild.sh                  # every bundled port
#     ./bundled_ports_rebuild.sh --skip-ok        # only those needing work
#     ./bundled_ports_rebuild.sh gnutls p11-kit   # just these
#     ./bundled_ports_rebuild.sh --no-repair ...  # do not touch headers
#     ./bundled_ports_rebuild.sh --skip-preflight # ignore preflight failures
#
# Exit status:
#     0  every port rebuilt (or already ok, with --skip-ok)
#     1  at least one port failed (all failures listed at the end)
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=bundled_ports_helpers.sh
source ./bundled_ports_helpers.sh
# shellcheck source=bundled_ports.sh
source ./bundled_ports.sh

HEADER_OUT=../universal-headers

SKIP_OK=0
REPAIR=1
PREFLIGHT=1
ports=""

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-ok)        SKIP_OK=1 ;;
        --no-repair)      REPAIR=0 ;;
        --skip-preflight) PREFLIGHT=0 ;;
        -h|--help)        sed -n '2,40p' "$0"; exit 0 ;;
        -*)               echo "unknown option: $1" >&2; exit 2 ;;
        *)                ports="$ports $1" ;;
    esac
    shift
done
explicit_ports=1
[ -z "$ports" ] && explicit_ports=0
ports="${ports:-$BUNDLED_PORTS}"

command -v port >/dev/null 2>&1 || {
    echo "error: MacPorts not found (https://www.macports.org)" >&2
    exit 1
}

# ---------------------------------------------------------------- preflight --
if [ "$PREFLIGHT" -eq 1 ]; then
    if ! ./bundled_ports_preflight.sh; then
        cat >&2 <<'EOF'

Preflight failed. Fix the errors above first -- building now wastes the time
either way. If the only errors are clobbered headers and you intend this run
to rebuild the owning ports, re-run with --skip-preflight.
EOF
        exit 1
    fi
    echo
fi

echo "Rebuilding for macOS $BLINK_MIN_OS."

# ------------------------------------------------------------ header repair --
# Regenerate any header the universal merge replaced with an ar archive,
# using the per-arch destroots the just-built port left behind.
repair_headers() {
    local p="$1" work h name arm x86 repaired=0 unrepaired=0
    local corrupt_list
    corrupt_list="$(corrupt_headers)" || return 0     # none -> nothing to do

    work="$(echo /opt/local/var/macports/build/"$p"-*/work)"
    [ -d "$work" ] || {
        echo "    headers clobbered but no work dir for $p -- repair by hand:"
        printf '      %s\n' $corrupt_list
        return 1
    }

    mkdir -p "$HEADER_OUT"
    while IFS= read -r h; do
        name="$(basename "$h")"
        arm="$(find "$work/destroot-arm64"  -name "$name" -type f 2>/dev/null | head -1)"
        x86="$(find "$work/destroot-x86_64" -name "$name" -type f 2>/dev/null | head -1)"
        if [ -z "$arm" ] || [ -z "$x86" ]; then
            echo "    $name: not produced by $p, leaving for its owning port"
            unrepaired=$((unrepaired + 1))
            continue
        fi
        if ./merge_universal_header.py "$arm" "$x86" "$HEADER_OUT" >/dev/null; then
            sudo cp "$HEADER_OUT"/*.h /opt/local/include/ && {
                echo "    repaired $name"
                repaired=$((repaired + 1))
            }
        else
            echo "    $name: merge_universal_header.py failed"
            unrepaired=$((unrepaired + 1))
        fi
    done <<< "$corrupt_list"

    [ "$repaired" -gt 0 ] && echo "    $repaired header(s) repaired from $p"
    [ "$unrepaired" -gt 0 ] && return 1
    return 0
}

# ------------------------------------------------------------------ rebuild --
failed=""
skipped=0
built=0
total=$(echo $ports | wc -w | tr -d ' ')
n=0

for p in $ports; do
    n=$((n + 1))

    if [ "$SKIP_OK" -eq 1 ]; then
        reason="$(port_needs_rebuild "$p")" || {
            printf '==> [%d/%d] %-18s skipped -- %s\n' "$n" "$total" "$p" "$reason"
            skipped=$((skipped + 1))
            continue
        }
        echo
        echo "==> [$n/$total] $p  ($reason)"
    else
        echo
        echo "==> [$n/$total] $p"
    fi

    if port_is_installed "$p"; then
        sudo port -k -s -N upgrade --force "$p" || { failed="$failed $p"; continue; }
    else
        sudo port -k -s -N install "$p" +universal || { failed="$failed $p"; continue; }
    fi
    built=$((built + 1))

    if [ "$REPAIR" -eq 1 ]; then
        if repair_headers "$p"; then
            sudo port clean --work "$p" >/dev/null 2>&1
        else
            echo "    keeping $p work dir for manual header repair"
        fi
    else
        sudo port clean --work "$p" >/dev/null 2>&1
    fi
done

# ------------------------------------------------- non-port bundled libraries --
# These never appear in `port installed`, so the loop above cannot reach them.
# bcg729 is built by cmake and takes its deployment target from BLINK_MIN_OS
# in its own script; without this it stays stamped with the build host's macOS
# while every port reads as ok.
if [ "$explicit_ports" -eq 0 ] && [ -n "${BUNDLED_NON_PORT_LIBS:-}" ]; then
    for entry in $BUNDLED_NON_PORT_LIBS; do
        libpath="${entry%%|*}"
        builder="${entry##*|}"
        name="$(basename "$libpath")"
        reason="$(lib_needs_rebuild "$libpath")"
        needs=$?
        if [ "$SKIP_OK" -eq 1 ] && [ "$needs" -ne 0 ]; then
            echo
            echo "==> $name  skipped -- $reason"
            continue
        fi
        echo
        echo "==> $name  ($reason)"
        if [ -x "$builder" ]; then
            "$builder" || failed="$failed $name"
        else
            echo "    $builder not found or not executable" >&2
            failed="$failed $name"
        fi
    done
fi

# ------------------------------------------------------------------- report --
echo
echo "Built $built of $total ports (skipped $skipped already ok)."

echo
echo "Checking for headers clobbered by the universal merge ..."
if corrupt_out="$(corrupt_headers)"; then
    printf '  CORRUPT: %s\n' $corrupt_out
    echo "  Rebuild the owning port with -k, then run merge_universal_header.py."
else
    echo "  OK -- no clobbered headers."
fi

if [ -n "$failed" ]; then
    echo
    echo "FAILED:$failed"
    echo
    echo "Logs are under /opt/local/var/macports/logs/. Re-run with just the"
    echo "failing ports once each is sorted out:"
    echo "    ./bundled_ports_rebuild.sh$failed"
    exit 1
fi

echo
echo "Now verify:"
echo "    ./bundled_ports_status.sh"
echo "    ./13-check-min-os.sh /opt/local/lib"
echo "    ./10-check-universal-libs.sh /opt/local/lib"
