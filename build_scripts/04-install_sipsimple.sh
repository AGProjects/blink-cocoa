#!/bin/bash
# Build python3-sipsimple from the sibling checkout and install it into
# Blink's venv. Assumes the python3-sipsimple/ checkout sits next to blink/,
# i.e. at ../../python3-sipsimple relative to this script.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow override via env var; default to ../../python3-sipsimple
SIPSIMPLE_DIR="${SIPSIMPLE_DIR:-$(cd "$SCRIPT_DIR/../../python3-sipsimple" 2>/dev/null && pwd)}"

if [ -z "$SIPSIMPLE_DIR" ] || [ ! -f "$SIPSIMPLE_DIR/setup.py" ] || [ ! -f "$SIPSIMPLE_DIR/setup_pjsip.py" ]; then
    echo
    echo "Cannot find python3-sipsimple checkout."
    echo "Expected at \$SIPSIMPLE_DIR or at ../../python3-sipsimple"
    echo "(relative to $SCRIPT_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$SIPSIMPLE_DIR"

echo "Building SIP SIMPLE SDK from $SIPSIMPLE_DIR ..."

# Re-running needs a clean deps tree; get_dependencies.sh fails otherwise.
rm -rf deps/pjsip deps/ZRTPCPP deps/pjproject-* 2>/dev/null || true

chmod +x ./get_dependencies*
./get_dependencies.sh 2.12

if [ $? -ne 0 ]; then
    echo
    echo "Failed to install all SIP SIMPLE SDK dependencies"
    echo
    exit 1
fi

# Make sure the build can find MacPorts headers/libs in this shell.
export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# --no-build-isolation so the build sees the venv's Cython, setuptools, etc.
pip3 install --no-build-isolation .

if [ $? -ne 0 ]; then
    echo
    echo "Failed to build SIP SIMPLE SDK"
    echo
    exit 1
fi
