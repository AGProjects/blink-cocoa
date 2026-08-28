#!/bin/bash
#
# 12-check-ldap.sh — verify the LDAP address book search is actually alive in
# this build.
#
# WHY THIS EXISTS
#
# ContactWindowController.py opens with:
#
#     try:
#         import ldap
#     except ImportError:
#         from application.python import Null
#         ldap = Null
#
# Null answers every call with itself and never raises, so when python-ldap is
# missing the failure is not an ImportError anywhere near the user. The app
# logs a perfectly convincing
#
#     Connected to LDAP server ldaps://ldap.sipthor.net:636
#
# (simple_bind_s on Null "succeeds"), and only when a search result is
# unpacked does anything surface — as
#
#     LDAP error: not enough values to unpack (expected 2, got 0)
#
# printed to stdout, where a shipped app has no stdout. Directory search is
# then dead in the field with no usable signal. The same silence covers the
# subtler failure: the module imports on the build machine but its dylibs were
# never bundled, so _ldap fails to load on every other Mac.
#
# Neither 10-check-universal-libs.sh nor publish/check-universal.sh catches
# that second case: a load command pointing at /opt/local is present in BOTH
# slices, so the parity check passes and the arch check passes. It is a
# dangling reference, not a mismatched one.
#
# WHAT IT CHECKS
#
#   1. MacPorts openldap is installed and universal        (build machine)
#   2. python-ldap imports in the venv                     (build machine)
#   3. _ldap.cpython-*-darwin.so is in the bundle, universal
#   4. every non-OS library _ldap loads is under Frameworks/libs
#   5. libldap / liblber are in Frameworks/libs and universal
#
# Run it after 06-copy-python-packages.sh, alongside 10 and 11.
#
# Usage:   ./12-check-ldap.sh
# Exit:    0 = LDAP search will work in the shipped app
#          1 = it will not, or it will only work on this machine

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DIST="../Distribution"
LIBS="$DIST/Frameworks/libs"
cver="$(./get_python_version.sh 2>/dev/null | sed -r 's/\.//g')"

fail=0
note() { printf '  %-8s %s\n' "$1" "$2"; }
bad()  { note "FAIL" "$1"; fail=1; }
ok()   { note "ok" "$1"; }
warn() { note "WARN" "$1"; }

is_universal() {
    local a
    a="$(lipo -archs "$1" 2>/dev/null)" || return 1
    case " $a " in *" x86_64 "*) ;; *) return 1 ;; esac
    case " $a " in *" arm64 "*)  ;; *) return 1 ;; esac
    return 0
}

# On an Intel host a single-arch build is expected; only require both slices
# when building on Apple Silicon.
NEED_UNIVERSAL=0
[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ] && NEED_UNIVERSAL=1

echo "==> Build machine"

if command -v port >/dev/null 2>&1; then
    if port -q installed openldap 2>/dev/null | grep -q '@'; then
        ok "openldap port installed"
    else
        bad "openldap port is NOT installed — run ./02-install-c-deps.sh"
    fi
fi

for l in /opt/local/lib/libldap.[0-9]*.dylib /opt/local/lib/liblber.[0-9]*.dylib; do
    [ -f "$l" ] || continue
    if [ "$NEED_UNIVERSAL" -eq 1 ] && ! is_universal "$l"; then
        bad "$l is single-arch [$(lipo -archs "$l" 2>/dev/null)]"
    else
        ok "$(basename "$l") $(lipo -archs "$l" 2>/dev/null)"
    fi
done

if source ./activate_venv.sh 2>/dev/null && python3 -c "import ldap" 2>/dev/null; then
    ok "venv: import ldap ($(python3 -c 'import ldap; print(ldap.__version__)' 2>/dev/null))"
else
    bad "venv: import ldap FAILED — run ./03-install-python-deps.sh"
fi

echo
echo "==> Bundle"

ext="$DIST/Resources/lib/_ldap.cpython-$cver-darwin.so"
if [ ! -f "$ext" ]; then
    bad "$ext missing — run ./06-copy-python-packages.sh"
elif [ ! -d "$DIST/Resources/lib/ldap" ]; then
    bad "$DIST/Resources/lib/ldap/ missing — the Python package was not copied"
else
    ok "_ldap extension and ldap package present"

    if [ "$NEED_UNIVERSAL" -eq 1 ] && ! is_universal "$ext"; then
        bad "_ldap is single-arch [$(lipo -archs "$ext" 2>/dev/null)] — run ./install_python-deps-universal.sh"
    else
        ok "_ldap $(lipo -archs "$ext" 2>/dev/null)"
    fi

    # Every load command that is not an OS library must resolve inside the
    # bundle. This is the check that catches "works here, dies everywhere".
    #
    # Dependency lines are the indented ones. On a fat binary otool prints a
    # header line per architecture ("<file> (architecture arm64):"), not just
    # one at the top, so `tail -n +2` is not enough to drop them — the second
    # header reads back as a dangling dependency. Slices carry the same load
    # commands (publish/check-universal.sh is what verifies that), so the list
    # is deduplicated.
    otool -L "$ext" 2>/dev/null | grep -E '^[[:space:]]' | awk '{print $1}' | sort -u | while read -r dep; do
        case "$dep" in
            /usr/lib/*|/System/*) continue ;;
            @executable_path/../Frameworks/libs/*)
                fn="$(basename "$dep")"
                [ -f "$LIBS/$fn" ] \
                    && echo "  ok       bundled  $fn" \
                    || echo "  FAIL     MISSING from Frameworks/libs: $fn"
                ;;
            *)
                echo "  FAIL     dangling dependency: $dep"
                echo "           (change_lib_paths.sh did not rewrite it, or"
                echo "            05-copy-libraries.sh did not bundle it)"
                ;;
        esac
    done > "$SCRIPT_DIR/.ldap-deps.$$"
    cat "$SCRIPT_DIR/.ldap-deps.$$"
    grep -q FAIL "$SCRIPT_DIR/.ldap-deps.$$" && fail=1
    rm -f "$SCRIPT_DIR/.ldap-deps.$$"
fi

found_ldap_lib=0
for l in "$LIBS"/libldap.*.dylib "$LIBS"/liblber.*.dylib; do
    [ -f "$l" ] || continue
    found_ldap_lib=1
    if [ "$NEED_UNIVERSAL" -eq 1 ] && ! is_universal "$l"; then
        bad "$(basename "$l") is single-arch in Frameworks/libs"
    else
        ok "bundled $(basename "$l") $(lipo -archs "$l" 2>/dev/null)"
    fi
done
[ "$found_ldap_lib" -eq 0 ] && bad "no libldap/liblber in $LIBS — run ./05-copy-libraries.sh"

echo
if [ "$fail" -eq 0 ]; then
    echo "OK — LDAP directory search is wired up end to end."
    exit 0
fi

cat >&2 <<'MSG'
FAILED — the shipped app will fall back to `ldap = Null` and silently disable
LDAP directory search (logging a fake "Connected to LDAP server" line, then
"LDAP error: not enough values to unpack (expected 2, got 0)" on first
search).

Build order for LDAP:
    ./02-install-c-deps.sh              openldap + cyrus-sasl2, +universal
    ./03-install-python-deps.sh         python-ldap into the venv
    ./05-copy-libraries.sh              libldap/liblber/libsasl2 -> Frameworks/libs
    ./06-copy-python-packages.sh        ldap/ + _ldap.so -> Resources/lib
                                        (also runs install_python-deps-universal.sh)
    ./12-check-ldap.sh                  this script

If only the two Bundle checks failed and the rest of Distribution/ is already
built, ./05b-copy-ldap.sh does just the LDAP part of steps 05 and 06 without
wiping and re-signing the whole bundle.
MSG
exit 1
