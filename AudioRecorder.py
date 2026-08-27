# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""The microphone side of the voice notes this client sends.

The mirror of AudioPlayback, and one recorder for the whole application
for the same reason there is one player: a person records one thing at a
time, and a recorder per conversation is several microphones fighting
over one device. Starting a take anywhere stops whatever was being
recorded elsewhere, which is the only behaviour that cannot surprise
anybody.

The format is not a choice made here. Sylk Mobile records
`sylk-audio-recording.m4a` -- AAC, 16 kHz, mono, 32 kbit/s -- on both iOS
and Android (app/components/AudioRecorder.js), and a voice note is only
worth sending if the other end can play it. Core Audio encodes and
decodes that natively, so the same file plays back in our own transcript
without a decoder anybody has to ship.

The waveform is measured while recording rather than from the file
afterwards. AVAudioRecorder meters the signal for free, and a peak per
50ms is exactly what the bubble's strip wants -- the same shape, the same
0..255 quantisation, and the same `{l: [...], r: []}` dict mobile ships,
so the recipient draws our recording with no special handling.
"""

__all__ = ['AudioRecorder', 'microphone_authorized', 'request_microphone',
           'NOISE_FLOOR_DB', 'MAX_RECORDING_SECONDS']

import os
import tempfile
import time

from AudioPlayback import DERIVED_BINS
from AVFoundation import (AVAudioRecorder,
                          AVCaptureDevice,
                          AVMediaTypeAudio,
                          AVEncoderAudioQualityKey,
                          AVEncoderBitRateKey,
                          AVFormatIDKey,
                          AVNumberOfChannelsKey,
                          AVSampleRateKey)
from Foundation import NSURL

from BlinkLogger import BlinkLogger


# 'aac ' as a four-character code. Named rather than imported: the
# constant lives in a different PyObjC framework wrapper depending on the
# version, and this module failing to import is the composer losing its
# record button over a number that has not changed since 2004.
K_AUDIO_FORMAT_MPEG4_AAC = 1633772320
AV_AUDIO_QUALITY_MEDIUM = 64

# What mobile records, field for field. 16 kHz is more than speech needs
# and a quarter of the bytes of 44.1; 32 kbit/s is about 240 KB a minute,
# against 1.9 MB for the 16-bit PCM voice notes used to ship as.
RECORDING_SETTINGS = {
    AVFormatIDKey: K_AUDIO_FORMAT_MPEG4_AAC,
    AVSampleRateKey: 16000.0,
    AVNumberOfChannelsKey: 1,
    AVEncoderBitRateKey: 32000,
    AVEncoderAudioQualityKey: AV_AUDIO_QUALITY_MEDIUM,
}

# Room tone is not part of a recording. Mobile folds anything below this
# to zero so an empty room draws a flat line rather than a hedge, and the
# two clients have to agree or the same take looks different on each.
NOISE_FLOOR_DB = -50.0

# One peak every 50ms, which is the grid mobile resamples onto. Finer
# than any strip can draw and coarse enough that ten minutes of speech is
# twelve thousand small integers rather than a megabyte of them.
PEAK_RATE_HZ = 20.0
PEAK_INTERVAL = 1.0 / PEAK_RATE_HZ

# A voice note is not a podcast. The cap is here so a record button left
# on by accident -- a window that lost focus, a laptop lid closed mid-take
# -- stops by itself instead of filling a disc.
MAX_RECORDING_SECONDS = 600.0

# The prefix recording_title() reads to show "Audio Recording" instead of
# the name a machine generated. Mobile writes a fixed
# sylk-audio-recording.m4a into its cache directory and overwrites it each
# time; the epoch is added here because these files sit beside each other
# in one folder until they are sent, and a second take must not land on
# the first one's bytes while it is still being uploaded.
RECORDING_PREFIX = 'sylk-audio-recording'


def microphone_authorized():
    """Whether this application may already open the microphone.

    Three answers, not two: granted, refused, and never asked. Only the
    last is worth putting a prompt up for, and only the middle one is
    worth explaining to the user.
    """
    try:
        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    except Exception as e:
        BlinkLogger().log_debug('Cannot read the microphone permission: %s' % e)
        return True                     # older systems do not gate this
    return int(status) == 3             # AVAuthorizationStatusAuthorized


def request_microphone(callback):
    """Ask for the microphone, then hand the answer to `callback`.

    The system prompt is shown once in the life of an installation; every
    call after that answers from what the user said then, and answers
    immediately.
    """
    try:
        status = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
    except Exception:
        callback(True)
        return
    if status == 3:
        callback(True)
        return
    if status in (1, 2):                # Restricted, Denied
        callback(False)
        return
    try:
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, lambda granted: callback(bool(granted)))
    except Exception as e:
        BlinkLogger().log_error('Cannot ask for the microphone: %s' % e)
        callback(False)


def _resample(samples, span, rate=PEAK_RATE_HZ):
    """Timestamped peaks onto a uniform grid over the whole take.

    The timer that collects them is a best effort -- AppKit coalesces
    timers under load and a slow redraw pushes the next tick out -- so the
    raw list is neither evenly spaced nor as long as the recording. Laid
    on a grid measured from the start of the take, a stretch of dropped
    ticks reads as what it was rather than silently shortening the
    waveform against the clock beside it.
    """
    if not samples:
        return []
    if span <= 0:
        return [value for _, value in samples]
    bins = max(int(span * rate), 1)
    grid = [0] * bins
    for stamp, value in samples:
        index = int(stamp * rate)
        if index < 0:
            continue
        if index >= bins:
            index = bins - 1
        if value > grid[index]:
            grid[index] = value
    return grid


def _reduce(bars, count=DERIVED_BINS):
    """A waveform at no more than `count` bars, loudest of each group kept.

    Max rather than mean: the strip is a record of how loud it got, and
    averaging a peak track flattens exactly the moments that give a
    recording its shape.
    """
    if len(bars) <= count:
        return bars
    span = len(bars) / float(count)
    reduced = []
    for index in range(count):
        start = int(index * span)
        end = max(min(len(bars), int((index + 1) * span)), start + 1)
        reduced.append(max(bars[start:end]))
    return reduced


class AudioRecorder(object):
    """The one recorder. `AudioRecorder()` always returns it."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AudioRecorder, cls).__new__(cls)
            cls._instance._recorder = None
            cls._instance._path = None
            cls._instance._samples = []
            cls._instance._started = 0.0
            cls._instance._elapsed = 0.0
        return cls._instance

    # -- state -------------------------------------------------------------

    def is_recording(self):
        recorder = self._recorder
        if recorder is None:
            return False
        try:
            return bool(recorder.isRecording())
        except Exception:
            return False

    def path(self):
        """Where the take is being written, or None."""
        return self._path

    def elapsed(self):
        """Seconds recorded so far.

        Taken from the recorder rather than the clock: it counts what has
        actually been written, and a take that never started -- a
        microphone another application is holding -- reads zero instead of
        counting up beside a file that is not growing.
        """
        recorder = self._recorder
        if recorder is None:
            return self._elapsed
        try:
            return float(recorder.currentTime())
        except Exception:
            return self._elapsed

    def peaks(self):
        """The waveform so far, shaped like the envelope's own.

        One channel: a microphone is one signal, and `r` is left empty
        exactly as mobile leaves it, which every reader downstream
        already handles.

        Reduced to the same number of bins a waveform measured from a
        file is reduced to, because this list is about to be JSON'd into
        a SIP MESSAGE: ten minutes at the collection rate is twelve
        thousand numbers, which is three times this client's own message
        length limit spent on resolution no strip can draw.
        """
        return {'l': _reduce(_resample(self._samples, self.elapsed())), 'r': []}

    def live_peaks(self, count):
        """The last `count` samples, for the meter that runs while recording.

        Not the resampled waveform: this is the tail of the signal moving
        past, and it is drawn a dozen times a second. Padded at the front
        so the strip fills from the right as a take starts rather than
        stretching a handful of values across the whole width.
        """
        count = max(int(count), 1)
        tail = [value for _, value in self._samples[-count:]]
        if len(tail) < count:
            tail = [0] * (count - len(tail)) + tail
        return tail

    def level(self):
        """The current input level, 0..1, for the VU meter."""
        if not self._samples:
            return 0.0
        return self._samples[-1][1] / 255.0

    # -- control -----------------------------------------------------------

    def start(self):
        """Begin a take. Returns the path being written, or None.

        Anything already recording is thrown away rather than kept: the
        user pressed record on a second conversation, and two takes with
        one microphone between them means the first one has been silent
        since the moment the second started.
        """
        self.cancel()

        folder = os.path.join(tempfile.gettempdir(), 'blink-recordings')
        try:
            if not os.path.isdir(folder):
                os.makedirs(folder)
        except OSError as e:
            BlinkLogger().log_error('Cannot make a place to record: %s' % e)
            return None
        path = os.path.join(folder, '%s-%d.m4a'
                            % (RECORDING_PREFIX, int(time.time() * 1000)))

        try:
            url = NSURL.fileURLWithPath_(path)
            recorder, error = AVAudioRecorder.alloc().initWithURL_settings_error_(
                url, RECORDING_SETTINGS, None)
        except Exception as e:
            BlinkLogger().log_error('Cannot open the microphone: %s' % e)
            return None
        if recorder is None:
            BlinkLogger().log_error(
                'Cannot open the microphone: %s'
                % (error.localizedDescription() if error else 'unsupported settings'))
            return None

        try:
            recorder.setMeteringEnabled_(True)
        except Exception as e:
            # A take with no waveform is still a take. Said out loud
            # because "the recording has no shape" otherwise looks like
            # a drawing bug rather than a microphone that will not meter.
            BlinkLogger().log_info('Cannot meter the microphone: %s' % e)
        try:
            started = bool(recorder.record())
        except Exception as e:
            BlinkLogger().log_error('Cannot start recording: %s' % e)
            started = False
        if not started:
            BlinkLogger().log_error('The microphone refused to start recording')
            return None

        self._recorder = recorder
        self._path = path
        self._samples = []
        self._started = time.monotonic()
        self._elapsed = 0.0
        BlinkLogger().log_info('Recording to %s' % os.path.basename(path))
        return path

    def tick(self):
        """Take one peak. Called on a timer while the take runs.

        Returns False once the take has run past the cap, which is the
        caller's cue to stop it -- the recorder itself has no opinion
        about how long a voice note should be.
        """
        recorder = self._recorder
        if recorder is None:
            return False
        try:
            recorder.updateMeters()
            db = float(recorder.averagePowerForChannel_(0))
        except Exception:
            db = -160.0
        level = (db - NOISE_FLOOR_DB) / -NOISE_FLOOR_DB
        level = min(max(level, 0.0), 1.0)
        self._samples.append((time.monotonic() - self._started, int(round(level * 255))))
        return self.elapsed() < MAX_RECORDING_SECONDS

    def stop(self):
        """End the take and keep it. Returns the finished path, or None."""
        recorder = self._recorder
        if recorder is None:
            return None
        self._elapsed = self.elapsed()
        try:
            recorder.stop()
        except Exception as e:
            BlinkLogger().log_error('Cannot stop recording: %s' % e)
        self._recorder = None
        path = self._path
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            # An encoder that wrote nothing. The user pressed stop and is
            # owed an answer, and a preview of an empty file would give
            # them a dead play key instead of one.
            BlinkLogger().log_error('The recording came out empty')
            # Removed here rather than left for cancel(): clearing _path
            # is what makes this take unreachable, so anything not
            # deleted now is never deleted.
            self.discard(path)
            self._path = None
            return None
        BlinkLogger().log_info('Recorded %s (%.1fs, %d bytes)'
                               % (os.path.basename(path), self._elapsed,
                                  os.path.getsize(path)))
        return path

    def cancel(self):
        """End the take and throw it away."""
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        self._recorder = None
        self.discard(self._path)
        self._path = None
        self._samples = []
        self._elapsed = 0.0

    def discard(self, path):
        """Delete a take that will not be sent.

        Separate from cancel() because the preview outlives the recorder:
        by the time the user presses the bin the take is a finished file
        and nothing is recording any more.
        """
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
                BlinkLogger().log_info('Discarded %s' % os.path.basename(path))
        except OSError as e:
            BlinkLogger().log_debug('Cannot remove %s: %s' % (path, e))
