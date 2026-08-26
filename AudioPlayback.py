# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""Playback for the voice recordings that arrive as file transfers.

One player for the whole application, deliberately. A transcript can hold
dozens of recordings and each has its own play key, but a person listens
to one thing at a time: pressing play on a second recording stops the
first, which is what Sylk Mobile does (its `audioRecordingStatus` names a
single current clip) and what every messaging client does. The
alternative -- a player per bubble -- is several recordings talking over
each other and no way to tell which key stops which.

The bubble owns none of this. It asks what is playing and how far along
it is at draw time, and says "toggle this file"; nothing about AVFoundation
reaches the drawing code.
"""

__all__ = ['AudioPlayback', 'format_clock', 'AUDIO_CHANNELS',
           'envelope_peaks', 'peak_samples', 'has_peaks', 'channel_peaks',
           'level_at', 'derive_peaks', 'SPECTRUM_BANDS', 'spectrum_frame',
           'has_spectrum']

import base64
import os

from AVFoundation import AVAudioPlayer
from Foundation import NSURL

from BlinkLogger import BlinkLogger


def format_clock(seconds):
    """Seconds as m:ss, or h:mm:ss once there is an hour of it.

    The player's own clock, not the caption's: the caption says "23m 14s"
    because it is describing a recording, and a clock counting up beside a
    progress bar has to be readable at a glance while it moves.
    """
    try:
        total = int(max(float(seconds or 0.0), 0.0))
    except (TypeError, ValueError):
        return '0:00'
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return '%d:%02d:%02d' % (hours, minutes, secs)
    return '%d:%02d' % (minutes, secs)


AUDIO_CHANNELS = ('r', 'l')             # remote above local, as mobile stacks them


def envelope_peaks(meta):
    """The ``peaks`` the sender put in the transfer envelope, or None.

    Sylk's CALL recorder writes a peak per 100ms bin while it encodes and
    ships them as ``{l: [...], r: [...]}`` -- `l` your side, `r` theirs.
    A plain voice memo arrives with none of this, which is why every
    reader below takes a peaks dict rather than the envelope: a waveform
    measured from the file itself substitutes for it unchanged.
    """
    if not isinstance(meta, dict):
        return None
    peaks = meta.get('peaks')
    return peaks if isinstance(peaks, dict) else None


def peak_samples(peaks, channel):
    """The raw 0..255 samples for one channel, or None.

    On the iOS mic-only path `r` is deliberately empty, which is why each
    channel is asked for separately rather than assuming a pair.
    """
    if not isinstance(peaks, dict):
        return None
    samples = peaks.get(channel)
    if not isinstance(samples, (list, tuple)) or not samples:
        return None
    return samples


def has_peaks(peaks):
    """Whether there is any waveform here at all."""
    return any(peak_samples(peaks, channel) for channel in AUDIO_CHANNELS)


def channel_peaks(peaks, channel, count):
    """`count` bar heights in 0..1 for one channel, or None.

    Max-pooled, never averaged: most of any utterance is near silence, and
    averaging a recording of speech flattens it into a uniform sausage.
    A source shorter than the bar grid is stretched by repeating rather
    than left padded with zeroes, so a very short clip fills its strip.

    Normalised per channel so the loudest moment of THAT side fills the
    strip. Without it a quietly recorded voice memo (peaks topping out
    around a quarter of full scale) draws as a flat line with one taller
    tick, and the two sides of a call -- recorded at quite different
    levels -- cannot be compared at all.
    """
    samples = peak_samples(peaks, channel)
    if samples is None or count <= 0:
        return None

    values = []
    for raw in samples:
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            values.append(0.0)

    bars = []
    ratio = float(len(values)) / float(count)
    if ratio <= 1.0:
        for index in range(count):
            bars.append(values[min(len(values) - 1, int(index * ratio))] / 255.0)
    else:
        for index in range(count):
            start = int(index * ratio)
            end = max(min(len(values), int((index + 1) * ratio)), start + 1)
            bars.append(max(values[start:end] or [0.0]) / 255.0)

    ceiling = max(bars)
    if ceiling > 0.001:
        bars = [min(value / ceiling, 1.0) for value in bars]
    return bars


def level_at(peaks, channel, fraction):
    """The level of one channel at a point in the recording, 0..1.

    An exact index, not a window: the meter shows the amplitude AT the
    playhead, so it tracks a scrub in real time and freezes on the right
    value when paused rather than drifting to a regional maximum.
    """
    samples = peak_samples(peaks, channel)
    if not samples:
        return 0.0
    position = min(max(float(fraction or 0.0), 0.0), 1.0)
    index = min(len(samples) - 1, int(position * len(samples)))
    try:
        return min(max(float(samples[index]) / 255.0, 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


# The recorded spectrogram, as Sylk Mobile's spectrumCodec writes it:
#
#   {v: 1, rate: 10, bands: 16, count: N, lo: -100, hi: 0, data: <base64>}
#
# `data` is N frames of `bands` bytes, row-major. A byte is a dB value
# quantised over [lo, hi], so it decodes as lo + (b/255) * (hi - lo).
SPECTRUM_BANDS = 16
SPECTRUM_RATE = 10                      # frames per second stored
SPECTRUM_DB_LO = -100.0                 # what byte 0 means
SPECTRUM_DB_HI = 0.0                    # what byte 255 means

# The window actually mapped to bar height, which is NOT the quantisation
# range: mobile's SpectrumBarsView draws -90..-20 dB. Everything below the
# floor is silence and everything above it is clipping, so spending the
# strip on the full 100 dB would leave every bar in the bottom tenth.
SPECTRUM_VIEW_LO = -90.0
SPECTRUM_VIEW_HI = -20.0

_spectrum_cache = {}
_SPECTRUM_CACHE_MAX = 8


def _spectrum_bytes(encoded):
    """base64 -> bytes, memoised on the string itself.

    A redraw happens ten times a second while a recording plays and the
    payload can be tens of kilobytes; decoding it per frame would be the
    most expensive thing in the transcript. The string is the key because
    it is exactly what identifies one recording's spectrogram.
    """
    if not encoded:
        return b''
    cached = _spectrum_cache.get(encoded)
    if cached is not None:
        return cached
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        raw = b''
    if len(_spectrum_cache) >= _SPECTRUM_CACHE_MAX:
        _spectrum_cache.clear()         # a transcript shows few at once
    _spectrum_cache[encoded] = raw
    return raw


def _spectrum_meta(spectrum):
    """The spectrum envelope as a dict, or None.

    Takes the spectrum itself, or anything carrying one under a
    ``spectrum`` key, so the caller does not have to know whether it came
    from a transfer envelope or from the metadata message that actually
    delivers it.
    """
    if isinstance(spectrum, dict) and 'data' not in spectrum:
        spectrum = spectrum.get('spectrum')
    if isinstance(spectrum, str):
        # The field survives a round trip through JSON as a string in
        # some paths; mobile's own reader parses it the same way.
        import json
        try:
            spectrum = json.loads(spectrum)
        except (TypeError, ValueError):
            return None
    if not isinstance(spectrum, dict) or not spectrum.get('data'):
        return None
    return spectrum


def has_spectrum(source):
    """Whether there is a spectrogram here worth drawing."""
    spectrum = _spectrum_meta(source)
    if spectrum is None:
        return False
    bands = int(spectrum.get('bands') or SPECTRUM_BANDS)
    return bands > 0 and len(_spectrum_bytes(spectrum.get('data'))) >= bands


def spectrum_frame(source, position, duration=0.0):
    """The band energies at a moment, as `bands` values in 0..1, or None.

    `position` is in seconds. Frames were stored at a fixed rate, so the
    frame for a moment is a straight index -- which is what lets the
    bars follow a scrub exactly rather than being smoothed towards it.
    """
    spectrum = _spectrum_meta(source)
    if spectrum is None:
        return None

    bands = int(spectrum.get('bands') or SPECTRUM_BANDS)
    rate = float(spectrum.get('rate') or SPECTRUM_RATE)
    low = float(spectrum.get('lo', SPECTRUM_DB_LO))
    high = float(spectrum.get('hi', SPECTRUM_DB_HI))
    raw = _spectrum_bytes(spectrum.get('data'))
    if bands <= 0 or not raw:
        return None
    count = int(spectrum.get('count') or (len(raw) // bands))
    count = min(count, len(raw) // bands)
    if count <= 0:
        return None

    if rate > 0:
        index = int(float(position or 0.0) * rate)
    elif duration > 0:
        index = int((float(position or 0.0) / duration) * (count - 1))
    else:
        index = 0
    index = min(max(index, 0), count - 1)

    span = (high - low) or 1.0
    view = (SPECTRUM_VIEW_HI - SPECTRUM_VIEW_LO) or 1.0
    base = index * bands
    frame = []
    for band in range(bands):
        db = low + (raw[base + band] / 255.0) * span
        frame.append(min(max((db - SPECTRUM_VIEW_LO) / view, 0.0), 1.0))
    return frame


# How many bins a locally measured waveform is reduced to. The envelope's
# own peaks are one per 100ms; a fixed count is used here instead because
# the strip only ever draws a few dozen bars and a five-minute recording
# has no need to carry three thousand numbers to do it.
DERIVED_BINS = 400

_derived_peaks = {}                     # path -> {'l': [...]} or None once tried


def derived_peaks(path):
    """A waveform measured from the file, if one has been measured."""
    return _derived_peaks.get(str(path or ''))


def derive_peaks(path, bins=DERIVED_BINS):
    """Measure a waveform from the audio itself. Blocking; call off the GUI.

    A plain voice memo arrives with no `peaks` in its envelope -- only the
    call recorder writes those -- so without this the bubble draws a bare
    scrub bar for exactly the recordings a person is most likely to want
    to look at. The file is already decrypted on disc by the time anything
    can be played, so the shape is there for the taking.

    Decoded through afconvert, which ships with macOS and understands
    whatever Core Audio does, into 8 kHz mono 16-bit PCM: far more
    resolution than a strip a few dozen bars wide can show, and small
    enough that a long recording is still a fraction of a second's work.
    A format afconvert cannot open (Ogg, notably) yields None, which is
    the same answer as before and draws the same plain bar.

    Returns a peaks dict shaped exactly like the envelope's, so every
    reader above takes it without knowing where it came from.
    """
    import struct
    import subprocess
    import tempfile
    import wave

    key = str(path or '')
    if not key:
        return None
    if key in _derived_peaks:
        return _derived_peaks[key]

    result = None
    handle = None
    try:
        handle = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        handle.close()
        completed = subprocess.run(
            ['/usr/bin/afconvert', '-f', 'WAVE', '-d', 'LEI16@8000',
             '-c', '1', key, handle.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if completed.returncode != 0:
            BlinkLogger().log_info(
                'Cannot measure %s: afconvert said %s'
                % (os.path.basename(key),
                   (completed.stderr or b'').decode('utf-8', 'replace').strip()[:120]))
        else:
            with wave.open(handle.name, 'rb') as source:
                frames = source.getnframes()
                raw = source.readframes(frames)
            count = len(raw) // 2
            if count:
                samples = struct.unpack('<%dh' % count, raw[:count * 2])
                span = max(count / float(bins), 1.0)
                bars = []
                for index in range(bins):
                    start = int(index * span)
                    end = max(min(count, int((index + 1) * span)), start + 1)
                    loudest = 0
                    for value in samples[start:end]:
                        value = -value if value < 0 else value
                        if value > loudest:
                            loudest = value
                    bars.append(min(int(loudest * 255 / 32768), 255))
                result = {'l': bars, 'r': []}
                BlinkLogger().log_info('Measured a waveform from %s (%d bins)'
                                       % (os.path.basename(key), len(bars)))
    except Exception as e:
        BlinkLogger().log_info('Cannot measure %s: %s' % (os.path.basename(key), e))
    finally:
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    # Cached either way: a file that cannot be measured must not be
    # re-decoded on every redraw.
    _derived_peaks[key] = result
    return result


class AudioPlayback(object):
    """The one player. `AudioPlayback()` always returns it."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AudioPlayback, cls).__new__(cls)
            cls._instance._player = None
            cls._instance._path = None
            cls._instance._key = None
        return cls._instance

    # -- state -------------------------------------------------------------

    def current_key(self):
        """Which bubble owns the player right now, or None."""
        return self._key

    def is_current(self, key):
        return bool(key) and self._key == key

    def is_playing(self, key=None):
        if key is not None and not self.is_current(key):
            return False
        player = self._player
        if player is None:
            return False
        try:
            return bool(player.isPlaying())
        except Exception:
            return False

    def duration(self, key=None):
        """Length in seconds of the loaded clip, or 0."""
        if key is not None and not self.is_current(key):
            return 0.0
        player = self._player
        if player is None:
            return 0.0
        try:
            return float(player.duration())
        except Exception:
            return 0.0

    def position(self, key=None):
        """How far into the clip we are, in seconds."""
        if key is not None and not self.is_current(key):
            return 0.0
        player = self._player
        if player is None:
            return 0.0
        try:
            return float(player.currentTime())
        except Exception:
            return 0.0

    def progress(self, key=None):
        """Position as a fraction of the whole, 0..1."""
        length = self.duration(key)
        if length <= 0:
            return 0.0
        return min(max(self.position(key) / length, 0.0), 1.0)

    def finished(self, key):
        """True when this clip has played to its end and stopped there.

        Distinguished from "not playing" so the bubble can show a full
        bar with the play key back, rather than snapping to zero the
        instant the sound ends.
        """
        if not self.is_current(key) or self.is_playing(key):
            return False
        length = self.duration(key)
        return length > 0 and self.position(key) >= length - 0.05

    # -- control -----------------------------------------------------------

    def load(self, path, key):
        """Point the player at a file. True if it is ready to play."""
        if self.is_current(key) and self._player is not None:
            return True
        if not path or not os.path.exists(path):
            BlinkLogger().log_error('Cannot play %s: the file is not here' % path)
            return False
        self.stop()
        try:
            url = NSURL.fileURLWithPath_(str(path))
            player, error = AVAudioPlayer.alloc().initWithContentsOfURL_error_(url, None)
        except Exception as e:
            BlinkLogger().log_error('Cannot open %s for playback: %s' % (path, e))
            return False
        if player is None:
            # A recording in a format this machine has no decoder for, or
            # a file that arrived truncated. Either way it is the user's
            # answer to why the key does nothing.
            BlinkLogger().log_error('Cannot play %s: %s'
                                    % (os.path.basename(str(path)),
                                       error.localizedDescription() if error else 'unsupported audio'))
            return False
        try:
            player.prepareToPlay()
        except Exception:
            pass
        self._player = player
        self._path = str(path)
        self._key = key
        BlinkLogger().log_info('Playing %s (%.1fs)'
                               % (os.path.basename(self._path), self.duration()))
        return True

    def toggle(self, path, key):
        """Play this recording, or pause it if it is the one playing.

        Returns True when something is playing afterwards. Pressing play
        on a finished clip starts it again from the beginning -- the
        alternative is a key that appears to do nothing.
        """
        if self.is_current(key) and self._player is not None:
            if self.is_playing(key):
                self.pause()
                return False
            if self.finished(key):
                self.seek(0.0, key)
            return self._start()
        if not self.load(path, key):
            return False
        return self._start()

    def _start(self):
        player = self._player
        if player is None:
            return False
        try:
            return bool(player.play())
        except Exception as e:
            BlinkLogger().log_error('Cannot start playback: %s' % e)
            return False

    def pause(self):
        player = self._player
        if player is None:
            return
        try:
            player.pause()
        except Exception as e:
            BlinkLogger().log_error('Cannot pause playback: %s' % e)

    def stop(self):
        """Give up the player entirely."""
        player = self._player
        self._player = None
        self._path = None
        self._key = None
        if player is None:
            return
        try:
            player.stop()
        except Exception as e:
            BlinkLogger().log_error('Cannot stop playback: %s' % e)

    def stop_for_key(self, key):
        """Stop only if this bubble is the one playing.

        Used when a conversation closes or a message is deleted: another
        conversation's recording must keep playing.
        """
        if self.is_current(key):
            self.stop()

    def seek(self, fraction, key=None, fallback=0.0):
        """Jump to a point in the clip, given as 0..1.

        `fallback` is the length the envelope claims, used when
        AVAudioPlayer reports none of its own. Some containers play
        perfectly while returning a duration of zero, and a fraction of
        zero is zero -- so without this, scrubbing such a recording could
        only ever seek to the very beginning, which looks exactly like
        scrubbing not working at all.
        """
        if key is not None and not self.is_current(key):
            BlinkLogger().log_debug('Seek ignored: %s is not the current clip' % key)
            return False
        player = self._player
        if player is None:
            return False
        length = self.duration() or float(fallback or 0.0)
        if length <= 0:
            BlinkLogger().log_info('Cannot seek: the clip reports no duration')
            return False
        wanted = min(max(float(fraction), 0.0), 1.0) * length
        before = self.position()
        was_playing = self.is_playing()
        try:
            player.setCurrentTime_(wanted)
        except Exception as e:
            BlinkLogger().log_error('Cannot seek: %s' % e)
            return False

        # Read back rather than trusting the write. A seek that silently
        # does not take is indistinguishable from a click that never
        # arrived, and AVAudioPlayer is documented to accept currentTime
        # at any time but does not always honour it mid-playback: the
        # audio queue is already primed with the old position and keeps
        # reporting it. Stopping the queue, setting the time and starting
        # again is what actually moves it.
        landed = self.position()
        if abs(landed - wanted) > 0.5:
            try:
                player.pause()
                player.setCurrentTime_(wanted)
                if was_playing:
                    player.play()
            except Exception as e:
                BlinkLogger().log_error('Cannot re-seek: %s' % e)
                return False
            landed = self.position()

        if abs(landed - wanted) > 0.5:
            BlinkLogger().log_info('Seek to %.1fs of %.1fs did not take (still %.1fs)'
                                   % (wanted, length, landed))
            return False
        BlinkLogger().log_debug('Seek %.1fs -> %.1fs of %.1fs'
                                % (before, landed, length))
        return True
