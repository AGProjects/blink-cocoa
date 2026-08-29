# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""One watcher over the two players, for controls that outlive a bubble.

A recording plays out of a single application-wide player, and the bubble
that started it draws its clock from a timer belonging to the conversation
it sits in. A control meant to stop playback from anywhere -- a toolbar
button in a window showing a different conversation, the header of the
messages pane after the user has clicked another contact -- cannot use that
timer: the transcript that owns it may be in another window, scrolled out
of sight, or closed.

So this ticks instead, only while something is playing, and posts
BlinkPlaybackStateChanged whenever the answer to "is anything playing"
changes. A control is then a plain observer: show or enable while playing,
go away when the clip ends, call stop() when clicked.

The tick is needed because a clip that reaches its end stops the player
without anybody calling stop(); nothing announces that on its own.
"""

from Foundation import NSObject, NSTimer

import objc

from application.notification import NotificationCenter, NotificationData

from AudioPlayback import AudioPlayback
from BlinkLogger import BlinkLogger
from VideoPlayback import VideoPlayback

# Four times a second. What this drives is a button with two states, not a
# clock, so it does not need the ten a second the bubbles run at.
TICK_SECONDS = 0.25

PLAYBACK_STATE_CHANGED = 'BlinkPlaybackStateChanged'


class PlaybackMonitor(NSObject):

    def init(self):
        self = objc.super(PlaybackMonitor, self).init()
        if self:
            self._timer = None
            self._playing = False
        return self

    # -- state -------------------------------------------------------------

    @objc.python_method
    def is_playing(self):
        """Is either player making sound right now."""
        return self._playing

    @objc.python_method
    def _reading(self):
        try:
            return bool(AudioPlayback().is_playing()
                        or VideoPlayback().is_playing())
        except Exception as e:
            BlinkLogger().log_error('Cannot read the playback state: %s' % e)
            return False

    # -- what the players call ---------------------------------------------

    @objc.python_method
    def poke(self):
        """Something may have started or stopped: look, and keep looking.

        Called by the players themselves rather than by their callers, so
        that every route into them -- a bubble, the recorder's preview, a
        conversation closing -- is covered without each one having to
        remember to announce anything.
        """
        self._announce()
        if self._playing:
            self._startTimer()

    # -- what the controls call --------------------------------------------

    @objc.python_method
    def stop(self):
        """Stop whatever is playing, from wherever the user happens to be."""
        AudioPlayback().stop()
        VideoPlayback().stop()
        self.poke()

    # -- the tick ----------------------------------------------------------

    @objc.python_method
    def _startTimer(self):
        if self._timer is not None:
            return
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            TICK_SECONDS, self, 'playbackTimer:', None, True)

    @objc.python_method
    def _stopTimer(self):
        if self._timer is None:
            return
        try:
            self._timer.invalidate()
        except Exception:
            pass
        self._timer = None

    def playbackTimer_(self, timer):
        self._announce()
        if not self._playing:
            # Nothing left to watch. The next play pokes it back to life.
            self._stopTimer()

    @objc.python_method
    def _announce(self):
        playing = self._reading()
        if playing == self._playing:
            return
        self._playing = playing
        NotificationCenter().post_notification(
            PLAYBACK_STATE_CHANGED, sender=self,
            data=NotificationData(playing=playing))


_monitor = None


def playback_monitor():
    """The one monitor. Built on first use, then kept for the run."""
    global _monitor
    if _monitor is None:
        _monitor = PlaybackMonitor.alloc().init()
    return _monitor


def notify_playback_change():
    """Poke the monitor, from inside the players.

    Imported where it is called rather than at the top of the player
    modules: the monitor reads both of them, so a module-level import
    would be a circle.
    """
    try:
        playback_monitor().poke()
    except Exception as e:
        BlinkLogger().log_error('Cannot announce the playback state: %s' % e)
