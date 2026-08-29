#!/bin/bash
#
# Report, per bundled port, whether its installed libraries already satisfy
# the minimum macOS and the universal-architecture requirement -- i.e. what
# bundled_ports_rebuild.sh still has left to do.
#
# 13-check-min-os.sh and 10-check-universal-libs.sh answer the same question
# per *library*; this answers it per *port*, which is the unit you rebuild.
# It ends with a ready-to-paste command for whatever is outstanding.
#
# Read-only -- safe to run in a second terminal while a rebuild is going.
#
# Usage:
#     ./bundled_ports_status.sh              # every bundled port
#     ./bundled_ports_status.sh gnutls x264  # just these
#
# Exit status:
#     0  every port is ok
#     1  at least one port still needs rebuilding
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=bundled_ports_helpers.sh
source ./bundled_ports_helpers.sh
# shellcheck source=bundled_ports.sh
source ./bundled_ports.sh

for tool in port lipo otool; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: '$tool' not found -- this script must run on macOS." >&2
        exit 1
    }
done

ports="${*:-$BUNDLED_PORTS}"

echo "Minimum macOS: $BLINK_MIN_OS   required arches: $REQUIRED_ARCHS"
echo

todo=""
ok_count=0

for p in $ports; do
    reason="$(port_needs_rebuild "$p")"
    if [ $? -eq 0 ]; then
        printf '  %-18s REBUILD -- %s\n' "$p" "$reason"
        todo="$todo $p"
    else
        printf '  %-18s %s\n' "$p" "$reason"
        ok_count=$((ok_count + 1))
    fi
done

total=$(echo $ports | wc -w | tr -d ' ')
echo
echo "$ok_count of $total ports ok."

# Bundled libraries that are not MacPorts ports never show up in `port
# installed`, so the loop above cannot see them. bcg729 is the reason this
# section exists: it is built by cmake and was silently left at the build
# host's deployment target while every port read as ok.
lib_todo=""
if [ $# -eq 0 ] && [ -n "${BUNDLED_NON_PORT_LIBS:-}" ]; then
    echo
    echo "Non-port bundled libraries"
    for entry in $BUNDLED_NON_PORT_LIBS; do
        libpath="${entry%%|*}"
        builder="${entry##*|}"
        reason="$(lib_needs_rebuild "$libpath")"
        if [ $? -eq 0 ]; then
            printf '  %-18s REBUILD -- %s\n' "$(basename "$libpath")" "$reason"
            lib_todo="$lib_todo
    $builder"
        else
            printf '  %-18s %s\n' "$(basename "$libpath")" "$reason"
        fi
    done
fi

if [ -z "$todo" ] && [ -z "$lib_todo" ]; then
    echo
    echo "Nothing left to rebuild."
    exit 0
fi

if [ -n "$todo" ]; then
    echo
    echo "Still to rebuild:$todo"
    echo
    echo "    ./bundled_ports_rebuild.sh$todo"
fi
if [ -n "$lib_todo" ]; then
    echo
    echo "Non-port libraries to rebuild:"
    printf '%s\n' "$lib_todo" | sed '/^$/d'
fi
exit 1
