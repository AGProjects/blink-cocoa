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
           'NOISE_FLOOR_DB', 'MAX_RECORDING_SECONDS',
           'wave_is_finalised', 'wait_for_wave', 'wave_duration',
           'finalise_wave', 'to_recording_format']

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


# ---------------------------------------------------------------------------
# Call recordings
#
# The call recorder is not this class: a call is recorded by sipsimple's
# RecordingWaveFile, straight to PCM, because the mixer writes what it
# hears rather than encoding it. What follows turns one of those into the
# same thing a voice note is -- an AAC .m4a at mobile's settings -- so a
# recording plays in the transcript, on a phone, and anywhere else with no
# decoder to ship and a tenth of the bytes.
# ---------------------------------------------------------------------------

# afconvert is part of macOS (CoreAudio's own command line tools) and has
# been since the beginning. Named rather than found on PATH: a PATH lookup
# is a thing that can be poisoned, and this is the only binary this module
# will ever run.
AFCONVERT = '/usr/bin/afconvert'

# How long to wait for the recorder to finish writing before giving up.
# sipsimple stops a recording by handing its WAV port to the mixer to be
# freed on the mixer's own thread (RecordingWaveFile._stop, "Defer freeing
# port + pool until the async REMOVE_PORT completes"), and pjmedia writes
# the RIFF and data sizes into the header from that free. So at the moment
# AudioStreamDidStopRecording is posted the file on disc still declares a
# length of zero, and anything that opens it gets
# kAudioFileInvalidFileError -- which is what "cannot play the recording"
# was.
# Short, because it is not really a wait for anything any more: the
# destroy that would write the header does not arrive on this path, so
# this is only the courtesy pause that lets a build where it DOES arrive
# take the fast road. finalise_wave handles the rest.
FINALISE_TIMEOUT = 3.0
FINALISE_POLL = 0.2


def _wave_layout(path):
    """(declared data bytes, byte rate, where the samples start), or Nones.

    Only the header is read: enough to say whether the writer has been
    round to patch it, enough to work out how long the recording is
    without opening an audio framework to ask, and enough to write the
    sizes in ourselves when nothing else will.
    """
    import struct
    try:
        with open(path, 'rb') as handle:
            riff = handle.read(12)
            if len(riff) < 12 or riff[:4] != b'RIFF' or riff[8:12] != b'WAVE':
                return None, None, None
            byte_rate = None
            while True:
                header = handle.read(8)
                if len(header) < 8:
                    return None, byte_rate, None
                name, size = struct.unpack('<4sI', header)
                if name == b'fmt ':
                    fmt = handle.read(size)
                    if len(fmt) >= 12:
                        byte_rate = struct.unpack('<I', fmt[8:12])[0]
                    continue
                if name == b'data':
                    return size, byte_rate, handle.tell()
                handle.seek(size + (size & 1), 1)
    except (OSError, struct.error) as e:
        BlinkLogger().log_debug('Cannot read the WAV header of %s: %s' % (path, e))
        return None, None, None


def _wave_actual_bytes(path):
    """How many bytes of samples are really there, or None."""
    _, _, offset = _wave_layout(path)
    if offset is None:
        return None
    try:
        return max(os.path.getsize(path) - offset, 0)
    except OSError:
        return None


def wave_is_finalised(path):
    """Whether the recorder has been back to write the sizes in.

    A data chunk of zero is a file still being written -- or one whose
    writer was never destroyed, which looks exactly the same and is just
    as unplayable.
    """
    declared, _, offset = _wave_layout(path)
    if not declared or offset is None:
        return False
    try:
        return os.path.getsize(path) >= offset + declared
    except OSError:
        return False


def finalise_wave(path):
    """A playable copy of a recording whose header says it is empty.

    Returns `path` itself when the header is already right, a new path
    beside it in the temporary directory when it had to be repaired, and
    None when the file is not a WAV this can make sense of.

    Why this exists rather than a longer wait: pjmedia writes the RIFF and
    data lengths from pjmedia_port_destroy, sipsimple defers that destroy
    to its mixer (RecordingWaveFile._stop -> AudioMixer
    ._remove_port_deferred), and on this path it does not arrive -- ten
    seconds after the stop notification the header still declares zero
    bytes of audio, which is kAudioFileInvalidFileError to every player on
    the system. The samples are all on disc; only the two numbers in front
    of them are wrong, and the file length says what they should be.

    Repaired IN PLACE where the file can be written, and that is
    deliberate: the recording in the user's history is the one they will
    go back to, nothing else is ever going to finish it, and a folder of
    call recordings that no player will open is the actual bug. Only the
    two length fields are written -- eight bytes, no sample touched -- so
    a writer that does come back later simply writes the same numbers
    again. A copy is the fallback for a file we may not write.
    """
    declared, _, offset = _wave_layout(path)
    if offset is None:
        return None
    try:
        actual = max(os.path.getsize(path) - offset, 0)
    except OSError:
        return None
    if not actual:
        return None                     # a header and nothing else
    if declared == actual:
        return path                     # nothing to repair

    import struct

    def patch(handle):
        handle.seek(4)
        handle.write(struct.pack('<I', offset + actual - 8))
        handle.seek(offset - 4)
        handle.write(struct.pack('<I', actual))

    try:
        with open(path, 'r+b') as handle:
            patch(handle)
    except OSError as e:
        BlinkLogger().log_debug('Cannot write the sizes into %s (%s); using a copy'
                                % (path, e))
    except struct.error as e:
        BlinkLogger().log_error('Cannot finalise %s: %s' % (path, e))
        return None
    else:
        BlinkLogger().log_info('Wrote the missing sizes into %s: %d bytes of audio, '
                               'a header that said %d'
                               % (os.path.basename(path), actual, declared or 0))
        return path

    target = os.path.join(tempfile.gettempdir(),
                          'finalised-%s' % os.path.basename(path))
    try:
        with open(path, 'rb') as source, open(target, 'wb') as handle:
            handle.write(source.read())
        with open(target, 'r+b') as handle:
            patch(handle)
    except (OSError, struct.error) as e:
        BlinkLogger().log_error('Cannot finalise %s: %s' % (path, e))
        try:
            os.remove(target)
        except OSError:
            pass
        return None
    BlinkLogger().log_info('Wrote the missing sizes into a copy of %s: %d bytes'
                           % (os.path.basename(path), actual))
    return target


def wait_for_wave(path, timeout=FINALISE_TIMEOUT):
    """Block until a recording is complete on disc. True if it got there.

    BLOCKING -- call it off the GUI thread. Returning False is not a
    failure: it means the sizes have to be written in here instead, which
    is what finalise_wave is for.
    """
    deadline = time.time() + max(float(timeout), 0.0)
    while True:
        if wave_is_finalised(path):
            return True
        if time.time() >= deadline:
            BlinkLogger().log_info('%s was not finalised by the recorder; '
                                   'writing its sizes in here' % os.path.basename(path))
            return False
        time.sleep(FINALISE_POLL)


def wave_duration(path):
    """How long a WAV runs, in seconds, or None.

    Measured from the file rather than from the header when the two
    disagree, so this answers for a recording whose sizes were never
    written in as well as for one whose were.
    """
    declared, byte_rate, offset = _wave_layout(path)
    if not byte_rate or offset is None:
        return None
    actual = _wave_actual_bytes(path)
    size = declared if declared and (actual is None or declared <= actual) else actual
    if not size:
        return None
    return round(float(size) / float(byte_rate), 2)


def to_recording_format(path):
    """A voice-note-shaped copy of a WAV: AAC, 16 kHz, mono, 32 kbit/s.

    The same numbers RECORDING_SETTINGS gives AVAudioRecorder, so a call
    recording and a voice note are the same kind of file -- and mobile
    records exactly this, which is what makes either of them playable
    there. A minute of call goes from about 3.8 MB of PCM to 240 KB.

    Returns the new path, or None -- and None is not a failure the caller
    has to treat as one: the WAV is still a perfectly good recording, it
    is merely fifteen times the size.

    BLOCKING. afconvert rather than an AVAssetExportSession because the
    export presets do not take settings: the preset picks its own sample
    rate and bitrate, and "the same format as a voice note" then depends
    on what version of macOS is running.
    """
    if not os.path.isfile(AFCONVERT):
        BlinkLogger().log_info('No %s here; sharing the recording as PCM' % AFCONVERT)
        return None
    target = os.path.join(tempfile.gettempdir(),
                          '%s.m4a' % os.path.splitext(os.path.basename(path))[0])
    command = [AFCONVERT, '-f', 'm4af',
               '-d', 'aac@%d' % int(RECORDING_SETTINGS[AVSampleRateKey]),
               '-c', str(int(RECORDING_SETTINGS[AVNumberOfChannelsKey])),
               '-b', str(int(RECORDING_SETTINGS[AVEncoderBitRateKey])),
               path, target]
    try:
        import subprocess
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=120)
    except Exception as e:
        BlinkLogger().log_error('Cannot convert %s: %s' % (path, e))
        return None
    if result.returncode != 0 or not os.path.isfile(target) or not os.path.getsize(target):
        BlinkLogger().log_error('afconvert refused %s: %s'
                                % (os.path.basename(path),
                                   (result.stdout or b'').decode('utf-8', 'replace').strip()
                                   or 'exit %d' % result.returncode))
        try:
            os.remove(target)
        except OSError:
            pass
        return None
    try:
        BlinkLogger().log_info('Converted %s to %s (%d -> %d bytes)'
                               % (os.path.basename(path), os.path.basename(target),
                                  os.path.getsize(path), os.path.getsize(target)))
    except OSError:
        pass
    return target


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
