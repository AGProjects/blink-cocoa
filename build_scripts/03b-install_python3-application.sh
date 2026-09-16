#!/bin/bash
# Build and install python3-application from the local checkout at
# ~/work/python3-application into Blink's venv. This is the only source of
# python3-application for Blink (it is not in requirements-python.txt);
# 03-install-python-deps.sh calls this script. Override with PY3APP_DIR.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY3APP_DIR="${PY3APP_DIR:-$HOME/work/python3-application}"

if [ -z "$PY3APP_DIR" ] || { [ ! -f "$PY3APP_DIR/pyproject.toml" ] && [ ! -f "$PY3APP_DIR/setup.py" ]; } || [ ! -d "$PY3APP_DIR/application" ]; then
    echo
    echo "Cannot find python3-application checkout."
    echo "Expected at ${PY3APP_DIR} (override with PY3APP_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$PY3APP_DIR"

echo "Installing python3-application from $PY3APP_DIR ..."

# The stale build/ tree may contain pre-fix sources (e.g. the broken
# decorator.py from before the execute_once fix); distutils can reuse it
# and ship the old copy. Wipe it to force a clean build.
rm -rf build python3_application.egg-info

pip3 install --force-reinstall --no-deps --no-build-isolation .

# Verify the execute_once and NotificationCenter deadlock fixes are in the version actually in use.
echo
echo "Verifying installed application.python.decorator ..."
cd /  # keep CWD off sys.path so we test the installed copy, not the source tree
python3 - <<'EOF'
import application, sys
from application.python.decorator import execute_once

class A(object):
    @execute_once
    def load(self): pass

assert A.load.called is False
a = A(); a.load(); a.load()
assert a.load.called is True and A.load.called is True
with A.load.lock:
    pass

# python3-application#7: NotificationCenter.lock must be reentrant (weakref cleanup under GC)
from application.notification import NotificationCenter
lock = NotificationCenter('__build_check__').lock
with lock:
    assert lock.acquire(blocking=False), "NotificationCenter.lock is not reentrant (issue #7 fix missing)"
    lock.release()

print("  package:  %s" % application.__file__)
print("  version:  %s" % application.__version__)
print("  notification lock: OK (reentrant)")
print("  execute_once: OK (lock + called work, %s)" % ("python %d.%d" % sys.version_info[:2]))
EOF

echo
echo "python3-application installed into ${VIRTUAL_ENV}."

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
    echo "Copying application package to Distribution ..."
    rm -rf "$dist_lib/application" "$dist_lib"/python3_application-*.dist-info
    cp -a "$site_packages_folder/application" "$dist_lib/"
    cp -a "$site_packages_folder"/python3_application-*.dist-info "$dist_lib/" 2>/dev/null || true
    # Match 06's convention: don't ship bytecode caches.
    find "$dist_lib/application" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
    find "$dist_lib/application" -name '*.pyc' -delete 2>/dev/null
    echo "  copied to $dist_lib/application"
else
    echo
    echo "Distribution/Resources/lib not found — skipping Distribution copy."
    echo "(06-copy-python-packages.sh will stage it on the next full copy.)"
fi
