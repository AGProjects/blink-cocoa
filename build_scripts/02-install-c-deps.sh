#!/bin/bash
#
# 02-install-c-deps.sh — bring /opt/local to the state Blink needs, from ANY
# starting state.
#
# This script is idempotent by design: run it on a fresh MacPorts install, on
# a half-universal tree, or after MacPorts has upgraded something behind your
# back, and it converges on the same result — every port Blink links present,
# active, and universal (arm64 + x86_64) on Apple Silicon.
#
# It exists because "port install <p> +universal" does NOT guarantee that:
#
#   * +universal is a REQUESTED variant. If the port is already installed
#     without it, MacPorts keeps the single-arch copy and ignores the request.
#   * "port uninstall <p>" FAILS when two versions are registered
#     ("Please specify the full version as recorded in the port registry"),
#     so a naive uninstall+install silently turns into an upgrade.
#   * An upgrade can DEACTIVATE a working universal build in favour of a newer
#     single-arch one and still report success. (Seen live: libuuid
#     1.0.3_0+universal -> 2.42.1_0 arm64-only.)
#   * A +universal build can fail outright on current toolchains. clang 16+
#     turns implicit function declarations into hard errors, which breaks
#     older autotools/meson sources — and meson's feature probes can
#     mis-detect under dual -arch flags, producing exactly that.
#
# So every port is handled as: inspect what is really on disk (lipo, not the
# registry) -> purge EVERY registered version if it is wrong -> install
# +universal -> VERIFY with lipo again -> on a build failure, retry once with
# -Wno-error=implicit-function-declaration via a local Portfile override.
#
# ffmpeg is built here too, from build_scripts/ffmpeg/Portfile, rather than
# being a manual procedure in ffmpeg/readme.txt. A build step that lives
# outside the numbered sequence is a step that drifts.
#
# Usage:
#   ./02-install-c-deps.sh                 show the plan, then ask to proceed
#   ./02-install-c-deps.sh --dry-run       show the plan and stop
#   ./02-install-c-deps.sh --yes           no prompt (CI / unattended)
#   ./02-install-c-deps.sh --only ffmpeg   act on one port
#
# Exit: 0 = every port present, active and universal. Non-zero otherwise.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCALPORTS="/opt/local/ports"
SOURCES_CONF="/opt/local/etc/macports/sources.conf"
CLANG_WORKAROUND="-Wno-error=implicit-function-declaration"

DRY=0
ASSUME_YES=0
ONLY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --only)    shift; ONLY="${1:-}" ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

command -v port >/dev/null 2>&1 || {
    echo "error: MacPorts not found. Install from https://www.macports.org" >&2
    exit 1
}

# Apple Silicon builds universal; Intel builds native only.
# Detect the HARDWARE, not the current process: `uname -m` says x86_64 when
# this shell is itself running under Rosetta, which would silently turn the
# universal build off. hw.optional.arm64 is 1 on Apple Silicon either way.
UNIVERSAL=0
VARIANT=""
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then
    UNIVERSAL=1
    VARIANT="+universal"
fi

# port | representative dylib glob (empty = tool-only port, nothing to probe)
# ffmpeg LAST: it links x264 / opus / vpx, so those must be universal first.
PORTS_LIST="
pkgconfig|
yasm|
gmp|/opt/local/lib/libgmp.*.dylib
mpfr|/opt/local/lib/libmpfr.*.dylib
libmpc|/opt/local/lib/libmpc.*.dylib
libuuid|/opt/local/lib/libuuid.*.dylib
openssl|/opt/local/lib/libssl.*.dylib
sqlite3|/opt/local/lib/libsqlite3.*.dylib
gnutls|/opt/local/lib/libgnutls.*.dylib
libopus|/opt/local/lib/libopus.*.dylib
libvpx|/opt/local/lib/libvpx.*.dylib
x264|/opt/local/lib/libx264.*.dylib
fdk-aac|/opt/local/lib/libfdk-aac.*.dylib
ffmpeg|/opt/local/lib/libavcodec.*.dylib
"

# --------------------------------------------------------------- helpers --

# Every version of $1 in the registry, one "@version+variants" per line.
port_versions() {
    port -q installed "$1" 2>/dev/null \
        | sed 's/^[[:space:]]*//' \
        | awk '$2 ~ /^@/ {print $2}'
}

# The active version of $1, or empty.
port_active_version() {
    port -q installed "$1" 2>/dev/null \
        | sed 's/^[[:space:]]*//' \
        | awk '/\(active\)/ && $2 ~ /^@/ {print $2}'
}

# Arch state of the dylibs matching glob $1: universal | single | absent.
glob_arch_state() {
    local glob="$1" f a found=0
    [ -z "$glob" ] && { echo "n/a"; return; }
    for f in $glob; do
        [ -f "$f" ] || continue
        [ -L "$f" ] && continue
        found=1
        a="$(lipo -archs "$f" 2>/dev/null)"
        if [ "$UNIVERSAL" -eq 1 ]; then
            case " $a " in
                *" x86_64 "*) case " $a " in *" arm64 "*) ;; *) echo "single"; return ;; esac ;;
                *) echo "single"; return ;;
            esac
        fi
    done
    [ "$found" -eq 1 ] && echo "universal" || echo "absent"
}

# Which of the four states a port is in, and therefore what has to happen.
#   ok             present, active, universal, no duplicate versions
#   purge_inactive present and universal, but stale versions clutter the
#                  registry (these break `port uninstall <name>`)
#   rebuild        installed but single-arch, or inactive
#   install        not installed at all
plan_for() {
    local p="$1" glob="$2"
    local vers count active state
    vers="$(port_versions "$p")"
    if [ -z "$vers" ]; then
        count=0
    else
        count="$(printf '%s\n' "$vers" | grep -c '@')"
    fi
    active="$(port_active_version "$p")"
    state="$(glob_arch_state "$glob")"

    if [ "$count" -eq 0 ]; then echo "install"; return; fi
    if [ -z "$active" ];      then echo "rebuild"; return; fi
    if [ "$state" = "single" ]; then echo "rebuild"; return; fi
    if [ "$state" = "absent" ] && [ -n "$glob" ]; then echo "rebuild"; return; fi
    if [ "$count" -gt 1 ];    then echo "purge_inactive"; return; fi
    echo "ok"
}

run() {
    echo "    \$ $*"
    "$@"
}

# Remove EVERY registered version, addressing each by its full version string
# (a bare `port uninstall <name>` fails when more than one is registered).
purge_all_versions() {
    local p="$1" v rc=0
    for v in $(port_versions "$p"); do
        run sudo port -N -f uninstall "$p" "$v" || rc=1
    done
    run sudo port clean --all "$p" >/dev/null 2>&1 || true
    return $rc
}

purge_inactive_versions() {
    local p="$1" v active
    active="$(port_active_version "$p")"
    for v in $(port_versions "$p"); do
        [ "$v" = "$active" ] && continue
        run sudo port -N -f uninstall "$p" "$v" || true
    done
}

# Copy the upstream Portfile into the local ports tree and append the clang
# workaround, so a port whose sources predate clang 16 can still build.
# Never applied to ffmpeg — that one already has our own Portfile.
add_clang_workaround() {
    local p="$1" pf cat dest
    [ "$p" = "ffmpeg" ] && return 1
    pf="$(port file "$p" 2>/dev/null)"
    [ -f "$pf" ] || return 1
    cat="$(port info --category --line "$p" 2>/dev/null | awk '{print $1}')"
    [ -n "$cat" ] || return 1
    dest="$LOCALPORTS/$cat/$p"
    echo "    applying clang workaround via local Portfile: $dest/Portfile"
    sudo mkdir -p "$dest" || return 1
    sudo cp "$pf" "$dest/Portfile" || return 1
    printf '\n# Added by 02-install-c-deps.sh: clang 16+ makes implicit function\n# declarations a hard error, which these sources predate.\nconfigure.cflags-append  %s\n' \
        "$CLANG_WORKAROUND" | sudo tee -a "$dest/Portfile" >/dev/null || return 1
    sudo portindex "$LOCALPORTS" >/dev/null 2>&1 || return 1
    return 0
}

install_port() {
    local p="$1"
    if [ -n "$VARIANT" ]; then
        run sudo port -N install "$p" "$VARIANT"
    else
        run sudo port -N install "$p"
    fi
}

# ------------------------------------------------- local ports repository --

ensure_local_ports_repo() {
    local tmp
    echo "==> Local ports repository (for the Blink ffmpeg Portfile)"

    if [ ! -f "$SCRIPT_DIR/ffmpeg/Portfile" ]; then
        echo "    error: $SCRIPT_DIR/ffmpeg/Portfile is missing." >&2
        return 1
    fi

    run sudo mkdir -p "$LOCALPORTS/multimedia/ffmpeg" || return 1
    run sudo cp "$SCRIPT_DIR/ffmpeg/Portfile" "$LOCALPORTS/multimedia/ffmpeg/Portfile" || return 1

    if ! grep -qE '^[[:space:]]*file://'"$LOCALPORTS" "$SOURCES_CONF" 2>/dev/null; then
        echo "    prepending file://$LOCALPORTS to $SOURCES_CONF"
        tmp="$(mktemp)" || return 1
        { echo "file://$LOCALPORTS"; cat "$SOURCES_CONF"; } > "$tmp"
        run sudo cp "$tmp" "$SOURCES_CONF" || { rm -f "$tmp"; return 1; }
        rm -f "$tmp"
    else
        echo "    $SOURCES_CONF already references the local repository"
    fi

    run sudo portindex "$LOCALPORTS" >/dev/null || return 1
    echo
}

# ------------------------------------------------------------------ plan --

echo "==> Inspecting /opt/local"
[ "$UNIVERSAL" -eq 1 ] \
    && echo "    Apple Silicon: every probed port must be universal (arm64 + x86_64)" \
    || echo "    Intel host: single-arch builds are expected, no +universal"
echo

plan=""
todo=0
printf "    %-12s %-15s %s\n" "PORT" "ACTION" "CURRENT"
for spec in $PORTS_LIST; do
    p="${spec%%|*}"
    glob="${spec#*|}"
    [ -n "$ONLY" ] && [ "$p" != "$ONLY" ] && continue

    action="$(plan_for "$p" "$glob")"
    cur="$(port_versions "$p" | tr '\n' ' ')"
    [ -z "$cur" ] && cur="not installed"
    printf "    %-12s %-15s %s\n" "$p" "$action" "$cur"
    if [ "$action" != "ok" ]; then
        plan="$plan $p|$glob|$action"
        todo=$((todo + 1))
    fi
done
echo

if [ -n "$ONLY" ] && [ -z "$plan" ] && [ "$todo" -eq 0 ]; then
    case "$PORTS_LIST" in
        *"$ONLY|"*) ;;
        *) echo "error: --only '$ONLY' is not one of the Blink ports." >&2; exit 2 ;;
    esac
fi

if [ "$todo" -eq 0 ]; then
    echo "==> Nothing to do — every port is present, active and correct."
    echo
else
    echo "==> $todo port(s) need work."
    echo "    rebuild        = purge every registered version, then install $VARIANT"
    echo "    purge_inactive = drop stale versions that break 'port uninstall'"
    echo "    install        = not installed yet"
    echo
    if [ "$DRY" -eq 1 ]; then
        echo "==> --dry-run: stopping before any change."
        exit 0
    fi
    if [ "$ASSUME_YES" -ne 1 ]; then
        if [ -t 0 ]; then
            printf "Proceed? [y/N] "
            read -r answer
            case "$answer" in
                y|Y|yes|YES) ;;
                *) echo "Aborted."; exit 1 ;;
            esac
        else
            echo "error: not a tty and --yes not given; refusing to modify ports." >&2
            exit 1
        fi
        echo
    fi
fi

# --------------------------------------------------------------- execute --

[ "$todo" -gt 0 ] && { ensure_local_ports_repo || exit 1; }

failed=""
for item in $plan; do
    p="${item%%|*}"
    rest="${item#*|}"
    glob="${rest%%|*}"
    action="${rest#*|}"

    echo "==> $p ($action)"
    case "$action" in
        purge_inactive)
            purge_inactive_versions "$p"
            ;;
        rebuild|install)
            [ "$action" = "rebuild" ] && purge_all_versions "$p"
            if ! install_port "$p"; then
                echo "    build failed — retrying once with $CLANG_WORKAROUND"
                if add_clang_workaround "$p"; then
                    purge_all_versions "$p"
                    install_port "$p" || failed="$failed $p"
                else
                    failed="$failed $p"
                fi
            fi
            ;;
    esac

    # Trust lipo, not the registry: an "successful" upgrade can have
    # deactivated a universal build in favour of a single-arch one.
    state="$(glob_arch_state "$glob")"
    case "$state" in
        universal|n/a) echo "    verified: $p is $state" ;;
        *)
            echo "    VERIFY FAILED: $p is '$state' after install" >&2
            case " $failed " in *" $p "*) ;; *) failed="$failed $p" ;; esac
            ;;
    esac
    echo
done

# create-dmg is a build tool, not linked into the app — no variant needed.
if [ -z "$ONLY" ]; then
    if ! port -q installed create-dmg 2>/dev/null | grep -q '@'; then
        echo "==> create-dmg (packaging tool)"
        run sudo port -N install create-dmg || echo "    warning: create-dmg install failed (06-dmg.sh needs it)" >&2
        echo
    fi
fi

# MacPorts' libuuid ships a uuid.h that shadows the macOS system header and
# breaks the sipsimple build. Do this LAST — any libuuid (re)install above
# puts the header back.
if [ -f /opt/local/include/uuid/uuid.h ]; then
    echo "==> Moving MacPorts uuid.h aside (conflicts with the system header)"
    run sudo mv -f /opt/local/include/uuid/uuid.h /opt/local/include/uuid/uuid.h.old
    echo
fi

# ---------------------------------------------------------------- report --

echo "==> Final audit"
bad=0
for spec in $PORTS_LIST; do
    p="${spec%%|*}"
    glob="${spec#*|}"
    [ -n "$ONLY" ] && [ "$p" != "$ONLY" ] && continue
    state="$(glob_arch_state "$glob")"
    printf "    %-12s %s\n" "$p" "$state"
    case "$state" in universal|n/a) ;; *) bad=1 ;; esac
done
echo

if [ "$bad" -eq 0 ] && [ -z "$failed" ]; then
    echo "==> OK — C dependencies are installed and universal."
    echo "    Next: ./02b-install-bcg729.sh, then ./03-install-python-deps.sh"
    exit 0
fi

[ -n "$failed" ] && echo "    ports that failed to build:$failed" >&2
cat >&2 <<'EOF'

==> FAILED — some ports are not usable for a universal build.

For a port that will not build +universal, read the MacPorts log named in the
error above. The usual cause on current toolchains is clang 16+ rejecting
implicit function declarations; this script already retries once with
-Wno-error=implicit-function-declaration via a local Portfile in
/opt/local/ports. If that was not enough, the port needs a real fix — check
whether an older version builds, or build it single-arch for each arch and
lipo the results by hand.

Do NOT continue to 04-install_sipsimple.sh while any port is single-arch: the
x86_64 cross-build links with -undefined dynamic_lookup and will silently drop
it, producing an Intel build that crashes at startup.
EOF
exit 1
