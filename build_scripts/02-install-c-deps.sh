#!/bin/bash
# Install MacPorts C dependencies for Blink.
# On Apple Silicon we install +universal so the resulting libs are fat
# (arm64+x86_64) for the universal Blink build.

set -e

echo "Installing port dependencies..."

if uname -v | grep ARM64 | grep Darwin >/dev/null; then
    # Apple Silicon: build universal so we can lipo with the x86_64 slice later.
    sudo port install \
        pkgconfig +universal \
        yasm +universal x264 +universal \
        gnutls +universal openssl +universal sqlite3 +universal \
        libuuid +universal libopus +universal \
        mpfr +universal libmpc +universal libvpx +universal \
        gmp +universal \
        fdk-aac +universal
    # AV1 (libaom / libdav1d / libsvt-av1) intentionally not pulled in here.
    # PJSIP can only consume AV1 via ffmpeg, and switching MacPorts ffmpeg to
    # a version that supports AV1 currently fails on Apple Silicon (Tahoe)
    # because of a meson/glib2 cross-build issue. Revisit when MacPorts ships
    # a working ffmpeg-devel +universal build.
else
    # Intel: same set without +universal.
    sudo port install \
        pkgconfig yasm x264 gnutls openssl sqlite3 \
        libuuid libopus mpfr libmpc libvpx gmp \
        fdk-aac
fi

sudo port install create-dmg

# On Apple Silicon, audit the libs Blink links against and force-rebuild any
# port that was previously installed without +universal. `port install foo
# +universal` is a *requested* variant — if foo was already installed without
# it, MacPorts keeps the single-arch copy and silently ignores the request.
# Blink's universal build then fails at 05-copy-libraries.sh with:
#   Non-fat file: Frameworks/libs//libuuid.1.dylib is architecture: arm64
# This loop catches that case and fixes it idempotently.
if uname -v | grep ARM64 | grep Darwin >/dev/null; then
    echo
    echo "Auditing MacPorts deps for non-universal slices ..."
    # Map: port-name → representative dylib under /opt/local/lib used to
    # check arch. (The dylib soname varies by version — globbed below.)
    ports_to_check=(
        "pkgconfig"   "" \
        "yasm"        "" \
        "x264"        "/opt/local/lib/libx264.*.dylib" \
        "gnutls"      "/opt/local/lib/libgnutls.*.dylib" \
        "openssl"     "/opt/local/lib/libssl.*.dylib" \
        "sqlite3"     "/opt/local/lib/libsqlite3.*.dylib" \
        "libuuid"     "/opt/local/lib/libuuid.*.dylib" \
        "libopus"     "/opt/local/lib/libopus.*.dylib" \
        "mpfr"        "/opt/local/lib/libmpfr.*.dylib" \
        "libmpc"      "/opt/local/lib/libmpc.*.dylib" \
        "libvpx"      "/opt/local/lib/libvpx.*.dylib" \
        "gmp"         "/opt/local/lib/libgmp.*.dylib" \
        "fdk-aac"     "/opt/local/lib/libfdk-aac.*.dylib" \
    )
    ports_to_fix=()
    i=0
    while [ $i -lt ${#ports_to_check[@]} ]; do
        port_name="${ports_to_check[$i]}"
        glob="${ports_to_check[$((i+1))]}"
        i=$((i+2))
        [ -z "$glob" ] && continue
        for dylib in $glob; do
            [ -f "$dylib" ] || continue
            [ -L "$dylib" ] && continue
            archs=$(lipo -archs "$dylib" 2>/dev/null || true)
            case "$archs" in
                *arm64*x86_64*|*x86_64*arm64*)
                    ;;
                *)
                    echo "  $port_name: $dylib has archs '$archs' (need universal)"
                    ports_to_fix+=( "$port_name" )
                    break
                    ;;
            esac
        done
    done

    if [ ${#ports_to_fix[@]} -gt 0 ]; then
        echo
        echo "The following ports are single-arch and will be rebuilt +universal:"
        for p in "${ports_to_fix[@]}"; do echo "  - $p"; done
        echo
        for p in "${ports_to_fix[@]}"; do
            echo "Reinstalling $p +universal ..."
            sudo port -N uninstall "$p" || true
            sudo port -N install   "$p" +universal
        done
        echo "Universal rebuild complete."
    else
        echo "  All audited ports are universal."
    fi
fi

# MacPorts' libuuid uuid.h conflicts with the macOS system header; rename it.
# Always check after any libuuid (re)install — even if uuid.h.old already
# exists from a previous run, a fresh libuuid install will recreate uuid.h
# and we need to rename it again.
if [ -f /opt/local/include/uuid/uuid.h ]; then
    if [ -f /opt/local/include/uuid/uuid.h.old ]; then
        # Both present — fresh libuuid install put uuid.h back. Replace the
        # old backup so we don't leak stale headers from a previous version.
        echo "Refreshing /opt/local/include/uuid/uuid.h.old (libuuid was reinstalled)..."
        sudo mv -f /opt/local/include/uuid/uuid.h /opt/local/include/uuid/uuid.h.old
    else
        echo "Renaming /opt/local/include/uuid/uuid.h to avoid conflict with system header..."
        sudo mv /opt/local/include/uuid/uuid.h /opt/local/include/uuid/uuid.h.old
    fi
fi

echo
echo "C dependencies installed. Next: install ffmpeg as described in ffmpeg/readme.txt"
echo "(macOS ships curl system-wide; wget is no longer required by these scripts.)"
