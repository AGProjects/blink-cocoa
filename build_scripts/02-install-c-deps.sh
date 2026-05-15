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

# MacPorts' libuuid uuid.h conflicts with the macOS system header; rename it.
# Idempotent: skip if we've already done it.
if [ -f /opt/local/include/uuid/uuid.h ] && [ ! -f /opt/local/include/uuid/uuid.h.old ]; then
    echo "Renaming /opt/local/include/uuid/uuid.h to avoid conflict with system header..."
    sudo mv /opt/local/include/uuid/uuid.h /opt/local/include/uuid/uuid.h.old
fi

echo
echo "C dependencies installed. Next: install ffmpeg as described in ffmpeg/readme.txt"
echo "(macOS ships curl system-wide; wget is no longer required by these scripts.)"
