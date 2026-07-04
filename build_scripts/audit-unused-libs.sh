#!/bin/bash
#
# Audit unused Python packages / extension modules in Resources/lib.
# Referenced by 06b-prune-python-packages.sh — keep them in sync.
#
# Usage:
#   ./audit-unused-libs.sh                     static report
#   ./audit-unused-libs.sh --emit-prune        also print prune lines for 06b
#   ./audit-unused-libs.sh --runtime <dump>    merge a runtime modules dump
#                                              (repeatable)
#   ./audit-unused-libs.sh --install-tracer    install sitecustomize.py tracer
#                                              into Resources/lib, then run the
#                                              app and exercise all features;
#                                              dumps land in
#                                              ~/Library/Logs/Blink/runtime-modules-*.txt
#   ./audit-unused-libs.sh --remove-tracer     remove the tracer (MUST be done
#                                              before shipping)
#
# Typical flow:
#   1. ./audit-unused-libs.sh > report-static.txt
#   2. ./audit-unused-libs.sh --install-tracer
#      (run Blink, use every feature, quit)
#      ./audit-unused-libs.sh --remove-tracer
#   3. ./audit-unused-libs.sh --runtime ~/Library/Logs/Blink/runtime-modules-*.txt \
#          --emit-prune > report-final.txt
#   4. Review report-final.txt; move approved prune lines into
#      06b-prune-python-packages.sh with a one-line justification each.

set -e
cd "$(dirname "$0")"

SRC_ROOT="$(cd .. && pwd)"
LIB="$SRC_ROOT/Distribution/Resources/lib"

if [ ! -d "$LIB" ]; then
    echo "error: $LIB not found; run 06-copy-python-packages.sh first" >&2
    exit 1
fi

case "$1" in
    --install-tracer)
        if [ -e "$LIB/sitecustomize.py" ]; then
            echo "error: $LIB/sitecustomize.py already exists" >&2
            exit 1
        fi
        cp runtime_import_tracer.py "$LIB/sitecustomize.py"
        echo "Tracer installed at Resources/lib/sitecustomize.py"
        echo "Run the app, exercise all features, quit normally."
        echo "Dumps: ~/Library/Logs/Blink/runtime-modules-<pid>.txt"
        echo "Then: $0 --remove-tracer   (required before shipping!)"
        exit 0
        ;;
    --remove-tracer)
        rm -f "$LIB/sitecustomize.py"
        rm -rf "$LIB/__pycache__"
        echo "Tracer removed."
        exit 0
        ;;
esac

exec python3 audit_unused_libs.py --lib "$LIB" --src "$SRC_ROOT" "$@"
