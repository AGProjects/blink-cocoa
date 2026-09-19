#!/bin/bash
# Build and install python3-msrplib from the local checkout at
# ~/work/python3-msrplib into Blink's venv, replacing the commit tarball
# pinned in requirements-sipsimple.txt.
# 03-install-python-deps.sh calls this script after requirements-sipsimple.txt.
# Override the checkout location with PY3MSRPLIB_DIR.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY3MSRPLIB_DIR="${PY3MSRPLIB_DIR:-$HOME/work/python3-msrplib}"

if [ -z "$PY3MSRPLIB_DIR" ] || { [ ! -f "$PY3MSRPLIB_DIR/pyproject.toml" ] && [ ! -f "$PY3MSRPLIB_DIR/setup.py" ]; } || [ ! -d "$PY3MSRPLIB_DIR/msrplib" ]; then
    echo
    echo "Cannot find python3-msrplib checkout."
    echo "Expected at ${PY3MSRPLIB_DIR} (override with PY3MSRPLIB_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$PY3MSRPLIB_DIR"

echo "Installing python3-msrplib from $PY3MSRPLIB_DIR ..."

# Wipe stale build artifacts so distutils cannot reuse old sources and ship
# an outdated copy.
rm -rf build dist python3_msrplib.egg-info

pip3 install --force-reinstall --no-deps --no-build-isolation --no-cache-dir .

# Verify the installed copy is the one in use and imports cleanly.
echo
echo "Verifying installed msrplib ..."
cd /  # keep CWD off sys.path so we test the installed copy, not the source tree
python3 - <<'EOF'
import sys
import msrplib
from msrplib import connect, protocol, session, transport

print("  package:  %s" % msrplib.__file__)
print("  version:  %s" % getattr(msrplib, '__version__', 'unknown'))
print("  import:   OK (python %d.%d)" % sys.version_info[:2])
EOF

echo
echo "python3-msrplib installed into ${VIRTUAL_ENV}."

# ---------------------------------------------------------------------------
# Copy the installed package into the Distribution tree (same destination as
# 06-copy-python-packages.sh), so an already-staged bundle picks up the new
# version without re-running the full 06 copy. Pure Python — no .so files,
# so no change_lib_paths.sh / codesign pass is needed.
#
# Skipped (with a note) if Resources/lib does not exist yet; in that case
# 06-copy-python-packages.sh will stage it from site-packages anyway.
# ---------------------------------------------------------------------------
cd "$SCRIPT_DIR"
site_packages_folder=$(./get_site_packages_folder.sh)
dist_lib="$SCRIPT_DIR/../Distribution/Resources/lib"

if [ -d "$dist_lib" ]; then
    echo
    echo "Copying msrplib package to Distribution ..."
    rm -rf "$dist_lib/msrplib" "$dist_lib"/python3_msrplib-*.dist-info "$dist_lib"/python3_msrplib-*.egg-info
    cp -a "$site_packages_folder/msrplib" "$dist_lib/"
    cp -a "$site_packages_folder"/python3_msrplib-*.dist-info "$dist_lib/" 2>/dev/null || true
    # Match 06's convention: don't ship bytecode caches.
    find "$dist_lib/msrplib" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
    find "$dist_lib/msrplib" -name '*.pyc' -delete 2>/dev/null
    echo "  copied to $dist_lib/msrplib"
else
    echo
    echo "Distribution/Resources/lib not found — skipping Distribution copy."
    echo "(06-copy-python-packages.sh will stage it on the next full copy.)"
fi
