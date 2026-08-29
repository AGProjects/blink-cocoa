#!/bin/bash
#
# The single source of truth for Blink's minimum supported macOS.
#
# This value has to agree in six places or the app and its bundled libraries
# disagree about what they run on:
#
#   * macosx_deployment_target in /opt/local/etc/macports/macports.conf
#   * MACOSX_DEPLOYMENT_TARGET in Blink.xcodeproj/project.pbxproj
#   * LSMinimumSystemVersion in Info.plist and Info-pro.plist
#   * BLINK_MIN_OS in 05-copy-libraries.sh and 02b-install-bcg729.sh
#   * the default in 13-check-min-os.sh
#
# bundled_ports_preflight.sh verifies they all still agree. Change the value
# here, then run that script to find what else needs updating.
#
# Override for a one-off build with:  BLINK_MIN_OS=14.0 ./<script>
#
BLINK_MIN_OS="${BLINK_MIN_OS:-13.0}"
# Exported so scripts invoked from here (02b-install-bcg729.sh, and anything
# else that reads BLINK_MIN_OS) inherit the configured value instead of
# falling back to their own default.
export BLINK_MIN_OS

# "13.0" -> 13000000, so versions compare as plain integers.
ver_num() {
    echo "$1" | awk -F. '{printf "%d%03d%03d\n", $1, ($2=="" ? 0 : $2), ($3=="" ? 0 : $3)}'
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "$BLINK_MIN_OS"
fi
