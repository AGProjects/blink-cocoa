#!/bin/bash
# Build and install python3-eventlib from the local checkout at
# ~/work/python3-eventlib into Blink's venv, replacing the release tarball
# pinned in requirements-sipsimple.txt (which lacks eventlib.watchdog).
# 03-install-python-deps.sh calls this script after requirements-sipsimple.txt.
# Override the checkout location with PY3EVENTLIB_DIR.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY3EVENTLIB_DIR="${PY3EVENTLIB_DIR:-$HOME/work/python3-eventlib}"

if [ -z "$PY3EVENTLIB_DIR" ] || { [ ! -f "$PY3EVENTLIB_DIR/pyproject.toml" ] && [ ! -f "$PY3EVENTLIB_DIR/setup.py" ]; } || [ ! -d "$PY3EVENTLIB_DIR/eventlib" ]; then
    echo
    echo "Cannot find python3-eventlib checkout."
    echo "Expected at ${PY3EVENTLIB_DIR} (override with PY3EVENTLIB_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$PY3EVENTLIB_DIR"

echo "Installing python3-eventlib from $PY3EVENTLIB_DIR ..."

# Wipe stale build artifacts so distutils cannot reuse old sources and ship
# an outdated copy.
rm -rf build dist python3_eventlib.egg-info

pip3 install --force-reinstall --no-deps --no-build-isolation --no-cache-dir .

# Verify the version actually in use has the reactor watchdog.
echo
echo "Verifying installed eventlib ..."
cd /  # keep CWD off sys.path so we test the installed copy, not the source tree
python3 - <<'EOF'
import sys
import eventlib
from eventlib import api, watchdog
from eventlib.hubs import twistedr
from eventlib.twistedutil import block_on

for name in ('start', 'stop', 'is_running'):
    assert callable(getattr(watchdog, name, None)), 'eventlib.watchdog.%s is missing' % name

print("  package:  %s" % eventlib.__file__)
print("  version:  %s" % eventlib.__version__)
print("  watchdog: OK (python %d.%d)" % sys.version_info[:2])
EOF

echo
echo "python3-eventlib installed into ${VIRTUAL_ENV}."

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
    echo "Copying eventlib package to Distribution ..."
    rm -rf "$dist_lib/eventlib" "$dist_lib"/python3_eventlib-*.dist-info "$dist_lib"/python3_eventlib-*.egg-info
    cp -a "$site_packages_folder/eventlib" "$dist_lib/"
    cp -a "$site_packages_folder"/python3_eventlib-*.dist-info "$dist_lib/" 2>/dev/null || true
    # Match 06's convention: don't ship bytecode caches.
    find "$dist_lib/eventlib" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
    find "$dist_lib/eventlib" -name '*.pyc' -delete 2>/dev/null
    echo "  copied to $dist_lib/eventlib"
else
    echo
    echo "Distribution/Resources/lib not found — skipping Distribution copy."
    echo "(06-copy-python-packages.sh will stage it on the next full copy.)"
fi
