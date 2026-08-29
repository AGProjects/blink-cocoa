#!/bin/bash
#
# Verify the build environment before anything long starts compiling, and
# print (or apply) the exact configuration changes needed to fix it.
#
# Every check here corresponds to a failure that has already cost hours:
#
#   macosx_deployment_target unset   libraries stamped with the build host's
#                                    macOS; 05-copy-libraries.sh rejects them
#   deployment target drift          the .app claims one minimum while the
#                                    Xcode targets or Info plists say another
#   buildfromsource not 'always'     MacPorts installs prebuilt archives that
#                                    ignore the deployment target entirely
#   revupgrade_autorun yes           rebuilding one port cascades into
#                                    unrelated software (cairo, wget, python)
#   +universal in variants.conf      every port, build tools included, goes
#                                    through the muniversal merge
#   clobbered headers                muniversal replaced gmp.h / ffi.h with
#                                    an empty ar archive; every later build
#                                    against them fails with a parse error
#
# Run it before 02-install-c-deps.sh, before bundled_ports_rebuild.sh (which
# calls it automatically), and after any macOS or Xcode upgrade. It is also
# what you run first when setting up a second build machine -- --fix writes
# the MacPorts configuration that machine needs.
#
# Usage:
#     ./bundled_ports_preflight.sh          # check and report
#     ./bundled_ports_preflight.sh --fix    # also apply the config changes
#
# Exit status:
#     0  environment is sane (warnings may still be printed)
#     1  at least one error -- do not start a build
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=bundled_ports_helpers.sh
source ./bundled_ports_helpers.sh

MACPORTS_CONF=/opt/local/etc/macports/macports.conf
VARIANTS_CONF=/opt/local/etc/macports/variants.conf
PBXPROJ=../Blink.xcodeproj/project.pbxproj
PLISTS="../Info.plist ../Info-pro.plist ../Info-lite.plist ../Info-sip2sip.plist"

APPLY=0
case "${1:-}" in
    --fix) APPLY=1 ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    "") ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

errors=0
warnings=0
conf_fixes=""          # newline-separated "key value" for macports.conf
fix_variants=0         # comment out a global +universal
port_cmds=""           # newline-separated port commands to run
source_fixes=""        # newline-separated notes about the Blink source tree
header_fixes=""        # how to repair headers muniversal clobbered
overlay_fixes=""       # stale /opt/local/ports copies to remove

err()  { echo "  ERROR   $*"; errors=$((errors + 1)); }
warn() { echo "  warning $*"; warnings=$((warnings + 1)); }
ok()   { echo "  ok      $*"; }

echo "Blink minimum macOS: $BLINK_MIN_OS  (blink_min_os.sh)"
echo

# ------------------------------------------------------------------- tools --
echo "Tools"
for tool in port otool lipo; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool found"
    else
        err "$tool not found"
    fi
done
echo

# ---------------------------------------------------------- macports.conf --
echo "MacPorts configuration"
target="$(awk '$1 == "macosx_deployment_target" {print $2}' "$MACPORTS_CONF" 2>/dev/null)"
if [ -z "$target" ]; then
    err "macosx_deployment_target is not set (want $BLINK_MIN_OS)"
    conf_fixes="$conf_fixes
macosx_deployment_target $BLINK_MIN_OS"
elif [ "$target" != "$BLINK_MIN_OS" ]; then
    err "macosx_deployment_target is $target, blink_min_os.sh says $BLINK_MIN_OS"
    conf_fixes="$conf_fixes
macosx_deployment_target $BLINK_MIN_OS"
else
    ok "macosx_deployment_target $target"
fi

if grep -qE '^[[:space:]]*buildfromsource[[:space:]]+always' "$MACPORTS_CONF" 2>/dev/null; then
    ok "buildfromsource always"
else
    err "buildfromsource is not 'always' -- MacPorts may install prebuilt
          archives built for the wrong deployment target"
    conf_fixes="$conf_fixes
buildfromsource always"
fi

if grep -qE '^[[:space:]]*revupgrade_autorun[[:space:]]+no' "$MACPORTS_CONF" 2>/dev/null; then
    ok "revupgrade_autorun no"
else
    warn "revupgrade_autorun is not 'no' -- rebuilding a bundled port can
          cascade into unrelated software. Set it to no while working through
          the bundled set, then run 'sudo port rev-upgrade' once at the end."
    conf_fixes="$conf_fixes
revupgrade_autorun no"
fi

if grep -qE '^[[:space:]]*\+universal' "$VARIANTS_CONF" 2>/dev/null; then
    warn "$VARIANTS_CONF sets +universal globally -- every port, including
          build tools, is built universal and run through the muniversal
          header merge. bundled_ports_rebuild.sh requests +universal per
          port, so this is not needed."
    fix_variants=1
else
    ok "no global +universal in variants.conf"
fi

if port_is_installed diffutils-for-muniversal; then
    ok "diffutils-for-muniversal installed"
else
    warn "diffutils-for-muniversal is not installed -- muniversal needs a GNU
          diff to merge headers that differ per architecture. Without it the
          merge falls through to libtool and writes an empty ar archive over
          the header. See merge_universal_header.py."
    port_cmds="$port_cmds
sudo port -N install diffutils-for-muniversal"
fi
echo

# ---------------------------------------------------------- local overlays --
# add_clang_workaround in 02-install-c-deps.sh copies a Portfile into
# /opt/local/ports so it can append -Wno-error=implicit-function-declaration.
# Those copies are frozen at the revision they were taken and shadow upstream
# forever. Worse, a copy made before the cp -R fix contains no files/ dir, so
# any patch the Portfile lists cannot be found locally and MacPorts tries to
# fetch it as a distfile -- 404 on every mirror (gnutls' CRAU_MAYBE_UNUSED.patch).
echo "Local port overlays"
LOCALPORTS=/opt/local/ports
overlay_found=0
if [ -d "$LOCALPORTS" ]; then
    while IFS= read -r pf; do
        [ -f "$pf" ] || continue
        d="$(dirname "$pf")"
        name="$(basename "$d")"
        # ffmpeg's overlay is maintained on purpose.
        [ "$name" = "ffmpeg" ] && continue
        grep -q 'Added by 02-install-c-deps.sh' "$pf" 2>/dev/null || continue
        overlay_found=1
        if grep -q '^[[:space:]]*patchfiles' "$pf" && [ ! -d "$d/files" ]; then
            err "overlay $d has patchfiles but no files/ -- fetches will 404"
        else
            warn "overlay $d shadows the upstream Portfile"
        fi
        overlay_fixes="$overlay_fixes
sudo rm -rf $d"
    done < <(find "$LOCALPORTS" -mindepth 3 -maxdepth 3 -name Portfile 2>/dev/null)
fi
[ "$overlay_found" -eq 0 ] && ok "no stale clang-workaround overlays"
if [ -n "$overlay_fixes" ]; then
    overlay_fixes="$overlay_fixes
sudo portindex $LOCALPORTS"
fi
echo

# ------------------------------------------------------ deployment targets --
echo "Deployment targets"
max_allowed="$(ver_num "$BLINK_MIN_OS")"

if [ -f "$PBXPROJ" ]; then
    bad=0
    while IFS= read -r v; do
        if [ "$(ver_num "$v")" -gt "$max_allowed" ]; then
            err "project.pbxproj has MACOSX_DEPLOYMENT_TARGET = $v (newer than $BLINK_MIN_OS)"
            source_fixes="$source_fixes
Blink.xcodeproj/project.pbxproj: set MACOSX_DEPLOYMENT_TARGET = $BLINK_MIN_OS (found $v)"
            bad=1
        fi
    done < <(grep -o 'MACOSX_DEPLOYMENT_TARGET = [0-9.]*' "$PBXPROJ" \
             | awk '{print $3}' | sort -u)
    [ "$bad" -eq 0 ] && ok "project.pbxproj targets are $BLINK_MIN_OS or older"
else
    warn "$PBXPROJ not found"
fi

for plist in $PLISTS; do
    [ -f "$plist" ] || continue
    v="$(grep -A1 'LSMinimumSystemVersion' "$plist" \
         | sed -n 's/.*<string>\(.*\)<\/string>.*/\1/p' | head -1)"
    [ -z "$v" ] && continue
    if [ "$(ver_num "$v")" -gt "$max_allowed" ]; then
        err "$(basename "$plist") LSMinimumSystemVersion is $v (newer than $BLINK_MIN_OS)"
        source_fixes="$source_fixes
$(basename "$plist"): set LSMinimumSystemVersion to $BLINK_MIN_OS (found $v)"
    else
        ok "$(basename "$plist") LSMinimumSystemVersion $v"
    fi
done
echo

# ------------------------------------------------------------------ headers --
echo "Headers"
if corrupt_out="$(corrupt_headers)"; then
    while IFS= read -r h; do
        err "clobbered by the universal merge: $h"
    done <<< "$corrupt_out"
    header_fixes="rebuild the owning port with 'sudo port -k -s -N install <port> +universal',
then ./merge_universal_header.py on its work/destroot-arm64 and destroot-x86_64 copies.
bundled_ports_rebuild.sh does this automatically for the ports it builds."
else
    ok "no clobbered headers under /opt/local/include"
fi
echo

# ------------------------------------------------------------ apply / print --
apply_conf() {
    # Set each "key value" in a MacPorts config file: replace the existing
    # line (commented or not), otherwise append. Done in python rather than
    # sed because BSD sed -i needs an argument and would drop file modes.
    local file="$1" pairs="$2" tmp
    tmp="$(mktemp)" || return 1
    KEYVALS="$pairs" python3 - "$file" "$tmp" <<'PYEOF'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
pairs = [l.split(None, 1) for l in os.environ["KEYVALS"].splitlines() if l.strip()]
try:
    lines = open(src, encoding="utf-8").read().splitlines(True)
except FileNotFoundError:
    lines = []
for key, value in pairs:
    pat = re.compile(r'^\s*#?\s*%s\b' % re.escape(key))
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = "%-26s%s\n" % (key, value)
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("%-26s%s\n" % (key, value))
open(dst, "w", encoding="utf-8").writelines(lines)
PYEOF
    [ -s "$tmp" ] || { rm -f "$tmp"; return 1; }
    sudo cp "$file" "$file.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null
    sudo cp "$tmp" "$file" && rm -f "$tmp"
}

if [ -n "$conf_fixes" ] || [ "$fix_variants" -eq 1 ] || [ -n "$port_cmds" ] || [ -n "$source_fixes" ] || [ -n "$header_fixes" ] || [ -n "$overlay_fixes" ]; then
    echo "Required changes"
    echo "----------------"
    if [ -n "$conf_fixes" ]; then
        echo "  $MACPORTS_CONF"
        printf '%s\n' "$conf_fixes" | sed '/^$/d' | awk '{printf "      %-26s%s\n", $1, $2}'
    fi
    if [ "$fix_variants" -eq 1 ]; then
        echo "  $VARIANTS_CONF"
        echo "      comment out the global +universal line"
    fi
    if [ -n "$port_cmds" ]; then
        echo "  ports"
        printf '%s\n' "$port_cmds" | sed '/^$/d;s/^/      /'
    fi
    if [ -n "$overlay_fixes" ]; then
        echo "  stale local port overlays (remove, then rebuild from upstream)"
        printf '%s\n' "$overlay_fixes" | sed '/^$/d;s/^/      /'
    fi
    if [ -n "$source_fixes" ]; then
        echo "  Blink source tree (edit by hand -- these are under version control)"
        printf '%s\n' "$source_fixes" | sed '/^$/d;s/^/      /'
    fi
    if [ -n "$header_fixes" ]; then
        echo "  clobbered headers"
        printf '%s\n' "$header_fixes" | sed '/^$/d;s/^/      /'
    fi
    echo

    if [ "$APPLY" -eq 1 ]; then
        echo "Applying configuration changes (originals backed up alongside) ..."
        if [ -n "$conf_fixes" ]; then
            apply_conf "$MACPORTS_CONF" "$conf_fixes" \
                && echo "  updated $MACPORTS_CONF" \
                || echo "  FAILED to update $MACPORTS_CONF" >&2
        fi
        if [ "$fix_variants" -eq 1 ]; then
            sudo cp "$VARIANTS_CONF" "$VARIANTS_CONF.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null
            sudo sed -i.tmpbak 's/^[[:space:]]*+universal/#+universal/' "$VARIANTS_CONF" \
                && sudo rm -f "$VARIANTS_CONF.tmpbak" \
                && echo "  updated $VARIANTS_CONF"
        fi
        [ -n "$port_cmds" ] && echo "  port commands above are NOT run automatically."
        [ -n "$overlay_fixes" ] && echo "  overlay removals above are NOT run automatically."
        [ -n "$source_fixes" ] && echo "  source-tree changes above are NOT applied automatically."
        [ -n "$header_fixes" ] && echo "  header repairs above are NOT applied automatically."
        echo
        echo "Re-run ./bundled_ports_preflight.sh to confirm."
        exit 0
    elif [ -n "$conf_fixes" ] || [ "$fix_variants" -eq 1 ]; then
        # --fix only writes the MacPorts config files; do not advertise it
        # when the only findings are things it will not touch.
        echo "Apply the configuration changes automatically with:"
        echo "    ./bundled_ports_preflight.sh --fix"
        echo
    fi
fi

if [ "$errors" -ne 0 ]; then
    echo "$errors error(s), $warnings warning(s) -- fix the errors before building." >&2
    exit 1
fi
echo "Environment OK ($warnings warning(s))."
