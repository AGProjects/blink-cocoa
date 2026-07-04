#!/bin/bash
#
# Show the audio + video codecs that PJSIP actually registered in the
# sipsimple build currently installed in Blink's venv. Useful for verifying
# that optional codecs (G.729, opus, AMR-WB, etc.) made it into the
# compiled _core.so before / after running 04-install_sipsimple.sh.
#
# Usage:
#     ./show-codecs.sh
#

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source ./activate_venv.sh

# IMPORTANT: cd to a neutral directory before invoking Python so any stray
# in-tree sipsimple/core/_core*.so cannot shadow the venv install via
# CWD-on-sys.path. We always want to inspect what the venv actually has.
cd /

python3 - <<'PY'
import sys, time

try:
    import sipsimple
    from sipsimple.core import Engine
    from sipsimple.core._core import __file__ as _core_file
except Exception as exc:
    print("Could not import sipsimple.core:", exc, file=sys.stderr)
    sys.exit(1)

print(f"sipsimple {sipsimple.__version__}")
print(f"_core.so:  {_core_file}")

e = Engine()
e.start(codecs=[], video_codecs=[])
for _ in range(100):                  # up to ~10s for the worker thread
    if e.is_running:
        break
    time.sleep(0.1)
else:
    print("Engine never came up.", file=sys.stderr)
    sys.exit(2)


def decode(items):
    return sorted({c.decode() if isinstance(c, (bytes, bytearray)) else str(c)
                   for c in items})


audio = decode(e.available_codecs)
try:
    video = decode(e.available_video_codecs)
except AttributeError:
    video = []

print()
print(f"Audio codecs ({len(audio)}):")
for c in audio:
    print(f"  - {c}")

print()
print(f"Video codecs ({len(video)}):")
for c in video:
    print(f"  - {c}")

# Highlight optional codecs the SDK build can flip on/off, so a missing
# one is obvious at a glance.
print()
print("Optional codec status:")
for name in ("G729", "opus", "AMR-WB", "GSM", "iLBC", "speex"):
    mark = "yes" if name in audio else "no"
    print(f"  {name:<8} {mark}")

e.stop()
e.join(timeout=5)
PY

exit $?
