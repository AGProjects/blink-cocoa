#!/bin/bash
#
# Shared helpers for inspecting installed MacPorts libraries: architecture
# slices, minimum-OS stamps, and the headers muniversal clobbers.
#
# Sourced by bundled_ports_preflight.sh, bundled_ports_rebuild.sh and
# bundled_ports_status.sh so they cannot drift apart.
#

# shellcheck source=blink_min_os.sh
source "$(dirname "${BASH_SOURCE[0]}")/blink_min_os.sh"

REQUIRED_ARCHS="${REQUIRED_ARCHS:-x86_64 arm64}"

port_is_installed() { port -q installed "$1" 2>/dev/null | grep -q '@'; }

# Installed files belonging to a port, one absolute path per line.
port_files() {
    port -q contents "$1" 2>/dev/null | sed 's/^[[:space:]]*//' | grep '^/'
}

# Minimum OS of one architecture slice: LC_BUILD_VERSION on modern
# toolchains, LC_VERSION_MIN_MACOSX on older ones.
minos_of() {
    otool -arch "$2" -l "$1" 2>/dev/null | awk '
        $1 == "cmd" && $2 == "LC_BUILD_VERSION"      { want = "minos";   next }
        $1 == "cmd" && $2 == "LC_VERSION_MIN_MACOSX" { want = "version"; next }
        want != "" && $1 == want                     { print $2; exit }'
}

# Echoes a human-readable reason and returns 0 when the port's installed
# libraries do NOT satisfy the target; returns 1 when they are fine.
port_needs_rebuild() {
    local p="$1" max_allowed worst worst_n thin nlibs f archs want arch v n
    max_allowed="$(ver_num "$BLINK_MIN_OS")"
    worst=""; worst_n=0; thin=""; nlibs=0

    if ! port_is_installed "$p"; then
        echo "not installed"
        return 0
    fi

    while IFS= read -r f; do
        case "$f" in
            *.dylib|*.so) ;;
            *) continue ;;
        esac
        [ -f "$f" ] || continue
        archs="$(lipo -archs "$f" 2>/dev/null)"
        [ -z "$archs" ] && continue
        nlibs=$((nlibs + 1))

        for want in $REQUIRED_ARCHS; do
            case " $archs " in
                *" $want "*) ;;
                *) [ -z "$thin" ] && thin="$(basename "$f") [$archs]" ;;
            esac
        done

        for arch in $archs; do
            v="$(minos_of "$f" "$arch")"
            [ -z "$v" ] && continue
            n="$(ver_num "$v")"
            if [ "$n" -gt "$worst_n" ]; then worst_n="$n"; worst="$v"; fi
        done
    done < <(port_files "$p")

    if [ "$nlibs" -eq 0 ]; then
        echo "ok (no libraries -- build tool only)"
        return 1
    fi

    local problems=""
    [ "$worst_n" -gt "$max_allowed" ] && problems="built for macOS $worst"
    if [ -n "$thin" ]; then
        [ -n "$problems" ] && problems="$problems; "
        problems="${problems}not universal: $thin"
    fi

    if [ -z "$problems" ]; then
        echo "ok ($nlibs libs, minos ${worst:-none})"
        return 1
    fi
    echo "$problems"
    return 0
}

# Echoes a human-readable reason and returns 0 when a single library file
# does NOT satisfy the target; returns 1 when it is fine. Used for bundled
# libraries that are not MacPorts ports (see BUNDLED_NON_PORT_LIBS).
lib_needs_rebuild() {
    local f="$1" archs want arch v n worst worst_n thin max_allowed
    max_allowed="$(ver_num "$BLINK_MIN_OS")"

    [ -f "$f" ] || { echo "not built"; return 0; }

    archs="$(lipo -archs "$f" 2>/dev/null)"
    [ -z "$archs" ] && { echo "not a Mach-O file"; return 0; }

    worst=""; worst_n=0; thin=""
    for want in $REQUIRED_ARCHS; do
        case " $archs " in
            *" $want "*) ;;
            *) thin="[$archs]" ;;
        esac
    done
    for arch in $archs; do
        v="$(minos_of "$f" "$arch")"
        [ -z "$v" ] && continue
        n="$(ver_num "$v")"
        if [ "$n" -gt "$worst_n" ]; then worst_n="$n"; worst="$v"; fi
    done

    local problems=""
    [ "$worst_n" -gt "$max_allowed" ] && problems="built for macOS $worst"
    if [ -n "$thin" ]; then
        [ -n "$problems" ] && problems="$problems; "
        problems="${problems}not universal: $thin"
    fi
    if [ -z "$problems" ]; then
        echo "ok (minos ${worst:-none})"
        return 1
    fi
    echo "$problems"
    return 0
}

# Prints every header under /opt/local/include that muniversal replaced with
# an empty ar archive. Returns 0 if any were found.
corrupt_headers() {
    local found=1 h
    while IFS= read -r h; do
        if head -c 7 "$h" 2>/dev/null | grep -q '!<arch>'; then
            echo "$h"
            found=0
        fi
    done < <(find /opt/local/include -name '*.h' -type f 2>/dev/null)
    return $found
}
