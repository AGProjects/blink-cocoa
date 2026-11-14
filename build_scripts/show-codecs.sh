#!/bin/bash
#
# Show the audio + video codecs that PJSIP actually registered in the
# sipsimple build currently installed in Blink's venv. Useful for verifying
# that optional codecs (G.729, opus, AMR-WB, etc.) made it into the
# compiled _core.so before / after running 04-install_sipsimple.sh,
# and whether the SDK supports stereo audio calls.
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

# Stereo audio (python3-sipsimple 5.7): the audio.stereo setting (stereo
# mixer, Opus in stereo, no echo canceller) and AudioStream(stereo=True)
# offering only the stereo codecs. Stereo calls need both, and opus.
import inspect

try:
    from sipsimple.configuration.settings import AudioSettings
    stereo_setting = 'stereo' in dir(AudioSettings)
except Exception:
    stereo_setting = False
try:
    from sipsimple.streams.rtp.audio import AudioStream
    stereo_stream = 'stereo' in inspect.signature(AudioStream.__init__).parameters
except Exception:
    stereo_stream = False
stereo = stereo_setting and stereo_stream and 'opus' in audio

print()
print("Stereo audio:")
print(f"  audio.stereo setting          {'yes' if stereo_setting else 'no'}")
print(f"  AudioStream(stereo=True)      {'yes' if stereo_stream else 'no'}")
print(f"  stereo calls (Opus stereo)    {'yes' if stereo else 'no'}")

e.stop()
e.join(timeout=5)
PY

exit $?
