#!/bin/bash
# Verify the build prerequisites for Blink on macOS.

set -e

env python3 -V | grep -E "3\.(9|10|11|12|13)" > /dev/null
if [ $? -ne 0 ]; then
    echo
    echo "Python 3.9, 3.10, 3.11, 3.12 or 3.13 is required."
    echo "Install from https://www.python.org/"
    echo "  detected: $(python3 -V 2>&1)"
    echo
    exit 1
fi

if ! command -v port >/dev/null; then
    echo
    echo "Please install MacPorts from https://www.macports.org"
    echo
    exit 1
fi

if ! command -v darcs >/dev/null; then
    echo
    echo "AG Projects repositories are managed using darcs."
    echo "Install with one of:"
    echo "    sudo port install darcs"
    echo "    brew install darcs"
    echo "    http://darcs.net"
    echo
    exit 1
fi

# 03-install-python-deps.sh creates the venv with `python3 -m venv` (stdlib),
# so we no longer need the third-party `virtualenv` package. Sanity-check
# that the stdlib venv module can actually be imported.
if ! python3 -c "import venv" 2>/dev/null; then
    echo
    echo "Python's stdlib venv module is missing."
    echo "Re-install Python 3 from https://www.python.org/ (the python.org"
    echo "installer ships venv) or install your distro's python3-venv package."
    echo
    exit 1
fi

echo "All build prerequisites OK."
echo "  python3:  $(command -v python3)  ($(python3 -V 2>&1))"
echo "  port:     $(command -v port)"
echo "  darcs:    $(command -v darcs)"
