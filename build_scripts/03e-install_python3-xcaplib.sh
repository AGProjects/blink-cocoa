#!/bin/bash
# Build and install python3-xcaplib from the local checkout at
# ~/work/python3-xcaplib into Blink's venv, replacing the release tarball
# pinned in requirements-sipsimple.txt (whose green XCAP client blocks the
# twisted reactor during HTTPS requests).
# 03-install-python-deps.sh calls this script after requirements-sipsimple.txt.
# Override the checkout location with PY3XCAPLIB_DIR.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY3XCAPLIB_DIR="${PY3XCAPLIB_DIR:-$HOME/work/python3-xcaplib}"

if [ -z "$PY3XCAPLIB_DIR" ] || { [ ! -f "$PY3XCAPLIB_DIR/pyproject.toml" ] && [ ! -f "$PY3XCAPLIB_DIR/setup.py" ]; } || [ ! -d "$PY3XCAPLIB_DIR/xcaplib" ]; then
    echo
    echo "Cannot find python3-xcaplib checkout."
    echo "Expected at ${PY3XCAPLIB_DIR} (override with PY3XCAPLIB_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$PY3XCAPLIB_DIR"

echo "Installing python3-xcaplib from $PY3XCAPLIB_DIR ..."

# Wipe stale build artifacts so distutils cannot reuse old sources and ship
# an outdated copy.
rm -rf build dist python3_xcaplib.egg-info

pip3 install --force-reinstall --no-deps --no-build-isolation --no-cache-dir .

# Verify the version actually in use runs green requests in worker threads.
echo
echo "Verifying installed xcaplib ..."
cd /  # keep CWD off sys.path so we test the installed copy, not the source tree
python3 - <<'EOF'
import sys
import xcaplib
from xcaplib import green, httpclient

assert hasattr(green, '_call_in_thread'), 'xcaplib.green does not run requests in worker threads'
assert hasattr(httpclient.HostCache, 'lock'), 'xcaplib.httpclient.HostCache has no lock'
client = green.HTTPClient('https://xcap.example.com/xcap-root', 'alice', 'example.com', 'secret')
assert hasattr(client, '_request_lock'), 'xcaplib.green.HTTPClient does not serialize requests'

print("  package:      %s" % xcaplib.__file__)
print("  version:      %s" % xcaplib.__version__)
print("  green client: OK, requests in worker threads (python %d.%d)" % sys.version_info[:2])
EOF

echo
echo "python3-xcaplib installed into ${VIRTUAL_ENV}."

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
    echo "Copying xcaplib package to Distribution ..."
    rm -rf "$dist_lib/xcaplib" "$dist_lib"/python3_xcaplib-*.dist-info "$dist_lib"/python3_xcaplib-*.egg-info
    cp -a "$site_packages_folder/xcaplib" "$dist_lib/"
    cp -a "$site_packages_folder"/python3_xcaplib-*.dist-info "$dist_lib/" 2>/dev/null || true
    # Match 06's convention: don't ship bytecode caches.
    find "$dist_lib/xcaplib" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
    find "$dist_lib/xcaplib" -name '*.pyc' -delete 2>/dev/null
    echo "  copied to $dist_lib/xcaplib"
else
    echo
    echo "Distribution/Resources/lib not found — skipping Distribution copy."
    echo "(06-copy-python-packages.sh will stage it on the next full copy.)"
fi
