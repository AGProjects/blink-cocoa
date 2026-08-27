# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""The recording bar that takes the composer's place while a voice note
is being made, and again while it is being listened to before it is sent.

Telegram's arrangement, and mobile's: the microphone sits at the right of
the input bar, pressing it turns the bar itself into the recorder, and
nothing opens, floats or covers the transcript. A voice note is a message
being written, so it is written where messages are written.

Two states, one view. Recording draws a live level strip and a clock that
counts up, with a cross to throw the take away and a stop key to end it.
Stopping puts the same bar into preview: the finished waveform, a play
key, a scrub, a bin and a send key. There is no third state -- a take is
either being made, being judged, or gone -- and the whole point of the
preview is that nothing is sent until the user has heard what they said.

The drawing is deliberately the bubble's own: the same key painter, the
same waveform bars in the same colour, the same clock. What is previewed
here becomes a bubble a moment later, and the two looking different would
make the preview a lie about what is being sent.
"""

__all__ = ['AudioRecorderView', 'RECORDER_BAR_HEIGHT']

from AppKit import (NSBezierPath,
                    NSColor,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSView)
from Foundation import NSAttributedString, NSMakeRect, NSZeroRect

import objc

from AudioPlayback import AudioPlayback, format_clock
from AudioRecorder import AudioRecorder, MAX_RECORDING_SECONDS
from BlinkLogger import BlinkLogger
from MessageBubbleView import (fill_key, COLOR_AUDIO_LOCAL, COLOR_KEY,
                               COLOR_KEY_GLYPH)


# The bar is the height of one line of composer, which is what it
# replaces. Taller and it shoves the transcript up every time somebody
# presses record.
RECORDER_BAR_HEIGHT = 34.0

KEY_SIZE = 26.0                 # the play key
# The key at the right end is the one that acts -- stop, then send -- and
# it is drawn bigger than the play key beside it. Two discs of the same
# size at the same moment read as a pair of equals, and these are not:
# one auditions the take, the other puts it in front of somebody.
ACTION_KEY_SIZE = 30.0
GLYPH_SIZE = 22.0               # the cross and the bin beside them
GAP = 8.0
EDGE = 6.0
CLOCK_W = 46.0
STRIP_H = 18.0
BAR_MIN_W = 2.0
BAR_GAP = 1.0

# The dot beside the clock while recording. It blinks, because a
# recording bar that looks the same whether or not it is recording is the
# one thing this control must never be.
DOT_SIZE = 8.0
BLINK_SECONDS = 0.6

COLOR_RECORD = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.86, 0.24, 0.24, 1.0)
# Send is green, not the play key's blue. The two sit a few points apart
# and one of them is irreversible: the same colour and the same size made
# "hear it again" and "it is gone" the same gesture at a glance. Green is
# the waveform's own green taken down a step, so it belongs to this bar
# rather than arriving from somewhere else. That leaves the bar reading
# red while recording, blue for playback, green to send.
COLOR_SEND = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.68, 0.38, 1.0)

CROSS_GLYPH = chr(10005)        # a multiplication sign, not a letter x
BIN_GLYPH = chr(128465)         # wastebasket


class AudioRecorderView(NSView):
    """The composer, while there is a recording in it.

    Owns no state about the take itself: what is being recorded is
    AudioRecorder's business and what is being played is AudioPlayback's,
    and this view asks them both at draw time exactly as a bubble does.
    Everything it does hold is about the bar -- which key is under the
    mouse, and which of the two states it is in.
    """

    def initWithFrame_(self, frame):
        self = objc.super(AudioRecorderView, self).initWithFrame_(frame)
        if self:
            self.delegate = None
            self.recording = True
            self.preview_path = None
            self.preview_peaks = None
            self.preview_duration = 0.0
            self._blink = True
            self._down = None
            self._scrubbing = False
            self._key_rect = NSZeroRect
            self._left_rect = NSZeroRect
            self._play_rect = NSZeroRect
            self._strip_rect = NSZeroRect
        return self

    def isFlipped(self):
        # Flipped, so the shadow the shared key painter casts falls
        # downwards here exactly as it does inside a bubble.
        return True

    def acceptsFirstResponder(self):
        # The composer had the keyboard when record was pressed and the
        # bar has no text in it: taking focus would only mean the user
        # cannot type the moment they discard the take.
        return False

    # -- state -------------------------------------------------------------

    @objc.python_method
    def startRecording(self):
        self.recording = True
        self.preview_path = None
        self.preview_peaks = None
        self.preview_duration = 0.0
        self._down = None
        self.setNeedsDisplay_(True)

    @objc.python_method
    def showPreview(self, path, peaks, duration):
        self.recording = False
        self.preview_path = path
        self.preview_peaks = peaks
        self.preview_duration = duration or 0.0
        self._down = None
        self.setNeedsDisplay_(True)

    @objc.python_method
    def tick(self, blink):
        """Redraw for the timer. `blink` drives the recording dot."""
        self._blink = blink
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _playing(self):
        if self.recording or not self.preview_path:
            return False
        return AudioPlayback().is_playing(self.preview_path)

    @objc.python_method
    def _progress(self):
        if self.recording or not self.preview_path:
            return 0.0
        return AudioPlayback().progress(self.preview_path)

    @objc.python_method
    def _clock(self):
        """What the clock reads: elapsed while recording, position after.

        Counting the position rather than the length during playback, and
        the length when stopped, is what every player does -- the number
        beside a moving bar has to be about the moment being played.
        """
        if self.recording:
            return AudioRecorder().elapsed()
        playback = AudioPlayback()
        if playback.is_current(self.preview_path) and playback.is_playing(self.preview_path):
            return playback.position(self.preview_path)
        return self.preview_duration

    # -- drawing -----------------------------------------------------------

    def drawRect_(self, rect):
        bounds = self.bounds()
        if bounds.size.width <= 0 or bounds.size.height <= 0:
            return

        middle = bounds.size.height / 2.0
        left = NSMakeRect(EDGE, middle - GLYPH_SIZE / 2.0, GLYPH_SIZE, GLYPH_SIZE)
        key = NSMakeRect(bounds.size.width - EDGE - ACTION_KEY_SIZE,
                         middle - ACTION_KEY_SIZE / 2.0,
                         ACTION_KEY_SIZE, ACTION_KEY_SIZE)
        self._left_rect = left
        self._key_rect = key

        self._drawLeftGlyph(left)
        self._drawActionKey(key)

        # Between the two: the clock, then whatever is left for the
        # waveform. The clock is given its width first because a number
        # that changes width as it counts drags the strip about beside it.
        x = left.origin.x + GLYPH_SIZE + GAP
        right = key.origin.x - GAP

        if self.recording:
            dot = NSMakeRect(x, middle - DOT_SIZE / 2.0, DOT_SIZE, DOT_SIZE)
            if self._blink:
                COLOR_RECORD.set()
                NSBezierPath.bezierPathWithOvalInRect_(dot).fill()
            x += DOT_SIZE + 6.0
        else:
            play = NSMakeRect(x, middle - KEY_SIZE / 2.0, KEY_SIZE, KEY_SIZE)
            self._drawPlayKey(play)
            x += KEY_SIZE + GAP

        clock_x = x
        self._drawClock(NSMakeRect(clock_x, middle - 8.0, CLOCK_W, 16.0))
        x += CLOCK_W + GAP

        strip = NSMakeRect(x, middle - STRIP_H / 2.0, max(right - x, 0.0), STRIP_H)
        self._strip_rect = strip
        if strip.size.width > 8.0:
            self._drawStrip(strip)

    @objc.python_method
    def _drawClock(self, rect):
        seconds = self._clock()
        colour = NSColor.secondaryLabelColor()
        if self.recording and seconds >= MAX_RECORDING_SECONDS - 30.0:
            # The last half minute of the cap, said in the one place the
            # user is already looking. A take that stops by itself with
            # no warning reads as a crash.
            colour = COLOR_RECORD
        text = NSAttributedString.alloc().initWithString_attributes_(
            format_clock(seconds),
            {NSFontAttributeName: NSFont.monospacedDigitSystemFontOfSize_weight_(11.0, 0.0)
                if hasattr(NSFont, 'monospacedDigitSystemFontOfSize_weight_')
                else NSFont.systemFontOfSize_(11.0),
             NSForegroundColorAttributeName: colour})
        size = text.size()
        text.drawAtPoint_((rect.origin.x,
                           rect.origin.y + (rect.size.height - size.height) / 2.0))

    @objc.python_method
    def _drawLeftGlyph(self, rect):
        """The cross while recording, the bin in preview.

        Two different words for the same outcome, and they are different
        on purpose: a take being recorded has never existed, and one in
        preview has been listened to. "Throw away what I am saying" and
        "delete that" are not the same gesture even though both end with
        an empty composer.
        """
        glyph = CROSS_GLYPH if self.recording else BIN_GLYPH
        colour = NSColor.secondaryLabelColor()
        if self._down == 'left':
            colour = NSColor.labelColor()
        text = NSAttributedString.alloc().initWithString_attributes_(
            glyph,
            {NSFontAttributeName: NSFont.systemFontOfSize_(13.0),
             NSForegroundColorAttributeName: colour})
        size = text.size()
        text.drawAtPoint_((rect.origin.x + (rect.size.width - size.width) / 2.0,
                           rect.origin.y + (rect.size.height - size.height) / 2.0))

    @objc.python_method
    def _drawActionKey(self, rect):
        """Stop while recording, send in preview.

        Never blue: blue is the play key's, here and in every bubble in
        the transcript, and it has to stay the colour of the thing that
        can be pressed twice with nothing lost.
        """
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(rect.origin.x + 0.5, rect.origin.y + 0.5,
                       max(rect.size.width - 1.0, 1.0),
                       max(rect.size.height - 1.0, 1.0)))
        fill_key(disc, COLOR_RECORD if self.recording else COLOR_SEND,
                 self._down == 'key')

        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        COLOR_KEY_GLYPH.set()
        if self.recording:
            # A square, which is what stop has meant on every recorder
            # since tape.
            side = rect.size.width * 0.34
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(centre_x - side / 2.0, centre_y - side / 2.0, side, side),
                1.5, 1.5).fill()
        else:
            # A paper plane, drawn rather than typed for the reason the
            # play triangle is: a glyph centres on its line box and lands
            # low in a circle.
            span = rect.size.width * 0.30
            plane = NSBezierPath.bezierPath()
            plane.moveToPoint_((centre_x - span, centre_y - span * 0.78))
            plane.lineToPoint_((centre_x + span * 1.05, centre_y))
            plane.lineToPoint_((centre_x - span, centre_y + span * 0.78))
            plane.lineToPoint_((centre_x - span * 0.55, centre_y))
            plane.closePath()
            plane.setLineJoinStyle_(1)           # NSRoundLineJoinStyle
            plane.setLineWidth_(1.0)
            plane.fill()
            plane.stroke()

    @objc.python_method
    def _drawPlayKey(self, rect):
        """The preview's own play key -- the bubble's, at the same size."""
        self._play_rect = rect
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(rect.origin.x + 0.5, rect.origin.y + 0.5,
                       max(rect.size.width - 1.0, 1.0),
                       max(rect.size.height - 1.0, 1.0)))
        fill_key(disc, COLOR_KEY, self._down == 'play')

        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        span = rect.size.width * 0.28
        COLOR_KEY_GLYPH.set()
        if self._playing():
            bar_w = max(span * 0.40, 2.0)
            gap = span * 0.42
            for sign in (-1.0, 1.0):
                x = centre_x + sign * (gap / 2.0 + bar_w / 2.0) - bar_w / 2.0
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x, centre_y - span, bar_w, span * 2.0),
                    bar_w / 2.0, bar_w / 2.0).fill()
        else:
            tip = centre_x + span * 0.92
            back = centre_x - span * 0.58
            head = NSBezierPath.bezierPath()
            head.moveToPoint_((tip, centre_y))
            head.lineToPoint_((back, centre_y - span))
            head.lineToPoint_((back, centre_y + span))
            head.closePath()
            head.setLineJoinStyle_(1)
            head.setLineWidth_(1.2)
            head.fill()
            head.stroke()

    @objc.python_method
    def _drawStrip(self, rect):
        """The signal: running past while recording, whole in preview.

        While recording the strip shows the last few seconds scrolling
        by, because the take has no end yet and a waveform squeezed to
        fit a growing recording shrinks under the user as they speak. In
        preview it is the whole take, played bars solid and the rest
        faded, which is the bubble's own reading of "how far in am I".
        """
        slots = max(int(rect.size.width / (BAR_MIN_W + BAR_GAP)), 8)
        if self.recording:
            bars = AudioRecorder().live_peaks(slots)
            played = 1.0
        else:
            bars = self._previewBars(slots)
            played = self._progress()
        if not bars:
            self._drawPlainTrack(rect, played)
            return

        slot = rect.size.width / float(len(bars))
        bar_w = max(slot - BAR_GAP, 1.0)
        edge = rect.origin.x + rect.size.width * played
        baseline = rect.origin.y + rect.size.height
        colour = COLOR_RECORD if self.recording else COLOR_AUDIO_LOCAL
        for index, value in enumerate(bars):
            x = rect.origin.x + index * slot + (slot - bar_w) / 2.0
            # At least a tick: silence is part of a recording, and a gap
            # in the strip reads as the meter having stopped.
            height = max((value / 255.0) * rect.size.height, 1.5)
            if x + bar_w / 2.0 <= edge:
                colour.set()
            else:
                colour.colorWithAlphaComponent_(0.25).set()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(x, baseline - height, bar_w, height)).fill()

    @objc.python_method
    def _previewBars(self, count):
        """The take's own peaks, reduced to `count` bars."""
        samples = (self.preview_peaks or {}).get('l') or []
        if not samples:
            return []
        if len(samples) <= count:
            return list(samples)
        span = len(samples) / float(count)
        bars = []
        for index in range(count):
            start = int(index * span)
            end = max(min(len(samples), int((index + 1) * span)), start + 1)
            bars.append(max(samples[start:end]))
        return bars

    @objc.python_method
    def _drawPlainTrack(self, rect, played):
        """A scrub bar, for a take that metered nothing.

        The control still works and the absence is visible, which is the
        same answer the bubble gives for a recording with no waveform.
        """
        radius = rect.size.height / 4.0
        track = NSMakeRect(rect.origin.x, rect.origin.y + rect.size.height / 2.0 - radius,
                           rect.size.width, radius * 2.0)
        NSColor.tertiaryLabelColor().set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            track, radius, radius).fill()
        if played > 0:
            COLOR_KEY.set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(track.origin.x, track.origin.y,
                           track.size.width * played, track.size.height),
                radius, radius).fill()

    # -- the mouse ---------------------------------------------------------

    @objc.python_method
    def _hit(self, point):
        def inside(rect):
            return (rect.size.width > 0
                    and rect.origin.x <= point.x <= rect.origin.x + rect.size.width
                    and rect.origin.y <= point.y <= rect.origin.y + rect.size.height)
        if inside(self._key_rect):
            return 'key'
        if inside(self._left_rect):
            return 'left'
        if not self.recording:
            if inside(self._play_rect):
                return 'play'
            if inside(self._strip_rect):
                return 'strip'
        return None

    def mouseDown_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        target = self._hit(point)
        if target == 'strip':
            # Scrubbing starts on the press and tracks the drag, so a
            # preview is dragged through rather than clicked at.
            self._scrubbing = True
            self._seekTo(point)
            return
        self._down = target
        if target:
            self.setNeedsDisplay_(True)

    def mouseDragged_(self, event):
        if not self._scrubbing:
            return
        self._seekTo(self.convertPoint_fromView_(event.locationInWindow(), None))

    def mouseUp_(self, event):
        if self._scrubbing:
            self._scrubbing = False
            return
        target = self._down
        self._down = None
        self.setNeedsDisplay_(True)
        if target is None:
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if self._hit(point) != target:
            # Pressed on a key and released off it: the gesture every
            # button on this platform reads as "no".
            return
        self._act(target)

    @objc.python_method
    def _seekTo(self, point):
        rect = self._strip_rect
        if rect.size.width <= 0:
            return
        fraction = (point.x - rect.origin.x) / rect.size.width
        self._call('audioBarSeek', min(max(fraction, 0.0), 1.0))
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _act(self, target):
        if target == 'key':
            self._call('audioBarStop' if self.recording else 'audioBarSend')
        elif target == 'left':
            self._call('audioBarCancel' if self.recording else 'audioBarDiscard')
        elif target == 'play':
            self._call('audioBarToggle')

    @objc.python_method
    def _call(self, name, *args):
        handler = getattr(self.delegate, name, None)
        if handler is None:
            return
        try:
            handler(*args)
        except Exception as e:
            BlinkLogger().log_error('The recording bar cannot %s: %s' % (name, e))
