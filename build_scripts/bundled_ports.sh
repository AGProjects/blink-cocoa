#!/bin/bash
#
# The MacPorts ports whose libraries end up inside the shipped Blink.app.
#
# This is deliberately NOT the same list as REQUIRED_PORTS in
# 02-install-c-deps.sh. That list is what has to be *installed* to build
# Blink, and includes build-only tools (pkgconfig, yasm) and ports whose
# libraries are never bundled (lzo2). This list is what 05-copy-libraries.sh
# actually copies into Distribution/Frameworks/libs, walked out of the
# LC_LOAD_DYLIB graph of the sipsimple core plus the extra_libs in that
# script -- so it includes transitive dependencies that never appear in
# REQUIRED_PORTS at all (nettle, p11-kit, libtasn1, brotli, zstd, ...).
#
# Use it whenever every bundled library has to be rebuilt: after changing
# macosx_deployment_target, after an OS or Xcode upgrade, or when
# 13-check-min-os.sh / 10-check-universal-libs.sh reports offenders.
#
#     source ./bundled_ports.sh
#     sudo port -s -N upgrade --force $BUNDLED_PORTS
#     ./13-check-min-os.sh /opt/local/lib
#
# Prefer `upgrade --force` over `port -f uninstall` + `port install`: the
# installed version stays active until the replacement is staged, so ports
# being rebuilt later in the run can still find their build dependencies.
# Force-uninstalling the whole set at once strands them halfway through.
#
# Running this file directly prints the list one port per line.
#

# ffmpeg pulls in libavcodec/avdevice/avformat/avutil/swresample/swscale.
BUNDLED_FFMPEG="ffmpeg libvpx x264 libopus"

# TLS and crypto. gnutls drags in the whole nettle/p11-kit/idn2 family.
BUNDLED_TLS="openssl gnutls nettle p11-kit libtasn1 libidn2 libunistring"

# LDAP address book search (see 05b-copy-ldap.sh, 12-check-ldap.sh).
# libfetch is pulled in by libldap/liblber.
BUNDLED_LDAP="openldap cyrus-sasl2 libfetch"

# gmp is needed by gnutls and hogweed; mpfr and libmpc are linked by gmpy2.
BUNDLED_MATH="gmp mpfr libmpc"

# General support libraries linked by the above or by the sipsimple core.
BUNDLED_SUPPORT="sqlite3 bzip2 libiconv gettext-runtime zlib zstd brotli \
libffi libuuid"

BUNDLED_PORTS="$BUNDLED_FFMPEG $BUNDLED_TLS $BUNDLED_LDAP $BUNDLED_MATH \
$BUNDLED_SUPPORT"

# Collapse the line continuations to single spaces.
BUNDLED_PORTS="$(echo $BUNDLED_PORTS)"

# Convenience alias.
BUNDLED="$BUNDLED_PORTS"

# Libraries that ship in Blink.app but are NOT MacPorts ports, so they never
# appear in `port installed` and the port checks above cannot see them. Each
# entry is <installed path>|<script that builds it>.
#
# bcg729 is built from source by cmake, which macports.conf cannot reach --
# it takes the deployment target from BLINK_MIN_OS in its own script.
BUNDLED_NON_PORT_LIBS="/opt/local/lib/libbcg729.0.dylib|./02b-install-bcg729.sh"

# Executed rather than sourced: print the list.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    printf '%s\n' $BUNDLED_PORTS
fi
