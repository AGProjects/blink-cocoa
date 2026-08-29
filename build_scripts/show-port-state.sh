#!/bin/bash
#
# SYNC NOTE — this file exists in two repositories and they are kept
# independent on purpose. The copies must stay IDENTICAL:
#
#     blink/build_scripts/show-port-state.sh
#     python3-sipsimple/mac/show-port-state.sh
#
# Edit one, copy it to the other, and record both. To check:
#
#     diff ~/work/blink/build_scripts/show-port-state.sh \
#          ~/work/python3-sipsimple/mac/show-port-state.sh
#
# show-port-state.sh — what MacPorts actually holds for every port Blink links.
# Read-only. Run it before and after any port surgery.
#
#   ARCH      universal / SINGLE-ARCH / absent, from the installed dylibs
#   VERSIONS  every version in the registry; duplicates are what make
#             `port uninstall <name>` fail with "specify the full version"

set -u

PORTS="pkgconfig| yasm| x264|/opt/local/lib/libx264.*.dylib gnutls|/opt/local/lib/libgnutls.*.dylib
openssl|/opt/local/lib/libssl.*.dylib sqlite3|/opt/local/lib/libsqlite3.*.dylib
libuuid|/opt/local/lib/libuuid.*.dylib libopus|/opt/local/lib/libopus.*.dylib
mpfr|/opt/local/lib/libmpfr.*.dylib libmpc|/opt/local/lib/libmpc.*.dylib
libvpx|/opt/local/lib/libvpx.*.dylib gmp|/opt/local/lib/libgmp.*.dylib
fdk-aac|/opt/local/lib/libfdk-aac.*.dylib ffmpeg|/opt/local/lib/libavcodec.*.dylib"

bad=0
for spec in $PORTS; do
    p="${spec%%|*}"
    glob="${spec#*|}"

    state="(no dylib to probe)"
    if [ -n "$glob" ]; then
        state="absent"
        for f in $glob; do
            [ -f "$f" ] || continue
            [ -L "$f" ] && continue
            a="$(lipo -archs "$f" 2>/dev/null)"
            case " $a " in
                *" x86_64 "*) case " $a " in *" arm64 "*) state="universal" ;;
                                             *) state="SINGLE-ARCH [$a]"; bad=1 ;; esac ;;
                *) state="SINGLE-ARCH [$a]"; bad=1 ;;
            esac
            [ "${state#SINGLE}" != "$state" ] && break
        done
    fi

    vers="$(port -q installed "$p" 2>/dev/null | sed 's/^[[:space:]]*//' | tr '\n' ';' | sed 's/;$//')"
    [ -z "$vers" ] && vers="not installed"

    printf "%-12s %-22s %s\n" "$p" "$state" "$vers"
done

echo
if [ "$bad" -eq 0 ]; then
    echo "All probed ports are universal."
else
    echo "Ports marked SINGLE-ARCH must be rebuilt +universal before 04/05."
fi
