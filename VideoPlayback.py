# Copyright (C) 2009-2026 AG Projects. See LICENSE for details.
#

"""Playback for the movies that arrive as file transfers.

One player for the whole application, for the same reason AudioPlayback
is one: a transcript can hold dozens of clips, a person watches one at a
time, and pressing play on a second must stop the first. The two players
drive the same transport the bubble already draws, so a recording and a
movie behave identically under the same key.

What is different is where the pixels go. AVAudioPlayer needs nowhere to
put them; a movie needs a layer, and that layer has to sit in the bubble
that owns the clip. So this player also lends out its AVPlayerLayer --
`attach` puts it in a view, `detach` takes it back, and only one view
ever holds it, which is the player's own rule expressed in geometry.
Keeping ONE layer rather than one per bubble is what stops a transcript
of twenty movies from holding twenty decoders open.

The bubble owns none of this. It asks what is playing and how far along
it is at draw time, and says "toggle this file"; nothing about
AVFoundation reaches the drawing code.
"""

__all__ = ['VideoPlayback', 'poster_image', 'movie_duration', 'is_playable',
           'forget_movie']

import math
import os

from AVFoundation import (AVPlayer,
                          AVPlayerLayer,
                          AVURLAsset,
                          AVAssetImageGenerator,
                          AVLayerVideoGravityResizeAspect)
from CoreMedia import CMTimeGetSeconds, CMTimeMakeWithSeconds
from Foundation import NSURL, NSMakeSize, NSZeroSize
from AppKit import NSImage

from BlinkLogger import BlinkLogger


# The timescale every CMTime here is built on. 600 is the conventional
# one: it divides evenly by every common frame rate (24, 25, 30, 60), so
# a seek expressed in it lands on a frame boundary rather than between
# two of them.
TIMESCALE = 600

# Where the poster frame is taken from. Not zero: the first frame of a
# clip recorded on a phone is very often black or a blurred smear from
# the camera still settling, and a wall of black rectangles is not a
# picture of anything. A second in, the shot has usually resolved.
POSTER_AT_SECONDS = 1.0

# The largest poster asked for, in pixels. The bubble draws it at a few
# hundred points at most and the grid crops it into a cell; anything
# past this is memory spent on detail that is scaled away. Generous
# enough for a Retina bubble at full width.
POSTER_MAX_PIXELS = 1400.0

# How near a seek has to land. Tight enough that dragging the bar tracks
# the picture, loose enough that AVFoundation can use a nearby frame
# rather than decoding forward from the last keyframe on every drag
# event -- exact seeking on a long clip makes scrubbing stutter.
SEEK_TOLERANCE = 0.12

# What the extensions say. Kept here rather than imported from
# MessageHost so this module stays free of the message layer; the two
# lists answer different questions -- that one classifies an envelope
# for a filter chip, this one asks whether AVFoundation will open a file
# we already hold.
# Deliberately short. AVFoundation plays the QuickTime family and little
# else: .mkv, .avi and .webm are common enough to be tempting and macOS
# opens none of them, and claiming them here would replace a bubble that
# opens in VLC with a play key that can never do anything. They keep the
# old behaviour -- a click hands them to whatever owns the extension.
PLAYABLE_EXTENSIONS = ('.mp4', '.mov', '.m4v', '.3gp')


def is_playable(path):
    """Whether this is a movie worth handing to AVFoundation.

    Asked of the file on disc rather than of the envelope, and only of
    its extension: an .mp4 whose codec this machine has no decoder for
    still answers True here. That case is caught later and properly --
    the renderer asks for a poster and a length before it commits to
    showing a player, and a file that yields neither is handed back to
    the ordinary open-it-outside rule.
    """
    if not path:
        return False
    return os.path.splitext(str(path))[1].lower() in PLAYABLE_EXTENSIONS


# Posters are held so a bubble rebuilt by a scroll or a filter does not
# re-decode a frame it has already seen. Kept SMALL on purpose: each one
# is a bitmap up to POSTER_MAX_PIXELS on its long side, so a generous
# cache is tens of megabytes of pictures nothing is looking at. The
# bubble keeps the poster it is actually showing in `media_image`; this
# is only the shortcut for building the next one.
_posters = {}                           # path -> NSImage or None once tried
_POSTER_CACHE_MAX = 16

# Durations are two floats and a string, so there is no reason to be
# frugal -- and they are asked for on every timer tick while a movie
# plays, which is the one lookup here that has to stay free.
_durations = {}                         # path -> seconds or None once tried
_DURATION_CACHE_MAX = 256


def _asset(path):
    """The file as an AVURLAsset, or None."""
    try:
        return AVURLAsset.URLAssetWithURL_options_(
            NSURL.fileURLWithPath_(str(path)), None)
    except Exception as e:
        BlinkLogger().log_error('Cannot open %s as a movie: %s'
                                % (os.path.basename(str(path)), e))
        return None


def _remember(cache, key, value, limit):
    """Cache a result, negative ones included.

    A file that yields no poster must not be re-decoded on every redraw
    -- the miss costs exactly as much as the hit did.
    """
    if len(cache) >= limit:
        cache.clear()
    cache[key] = value
    return value


def movie_duration(path):
    """Length in seconds, straight from the container. Blocking.

    Read from the asset rather than from the player, because the bubble
    needs a length before anything has been played: the scrub bar is a
    fraction OF the duration, and a bar that cannot be dragged until
    after the clip has started is a bar that appears broken.
    """
    key = str(path or '')
    if not key:
        return None
    if key in _durations:
        return _durations[key]
    asset = _asset(key)
    if asset is None:
        return _remember(_durations, key, None, _DURATION_CACHE_MAX)
    try:
        seconds = CMTimeGetSeconds(asset.duration())
    except Exception as e:
        BlinkLogger().log_info('Cannot measure %s: %s'
                               % (os.path.basename(key), e))
        return _remember(_durations, key, None, _DURATION_CACHE_MAX)
    if seconds is None or math.isnan(seconds) or seconds <= 0:
        # A stream with an indefinite duration, or a file that arrived
        # truncated. Either way there is no timeline to scrub.
        return _remember(_durations, key, None, _DURATION_CACHE_MAX)
    return _remember(_durations, key, float(seconds), _DURATION_CACHE_MAX)


def poster_image(path):
    """A still from the movie, as an NSImage, or None. Blocking.

    This is what makes a video bubble a picture rather than a filename:
    the poster goes into the bubble's `media_image`, so every rule the
    transcript already has about sizing a photograph, fitting it to the
    bubble, cropping it into a grid cell and dragging it to the Finder
    applies to a movie without being written a second time.

    `appliesPreferredTrackTransform` is not optional. Video shot in
    portrait on a phone is stored landscape with a rotation in its track
    metadata; without honouring it every clip from a phone posters on
    its side.
    """
    key = str(path or '')
    if not key:
        return None
    if key in _posters:
        return _posters[key]

    asset = _asset(key)
    if asset is None:
        return _remember(_posters, key, None, _POSTER_CACHE_MAX)

    image = None
    try:
        generator = AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
        generator.setAppliesPreferredTrackTransform_(True)
        generator.setMaximumSize_(NSMakeSize(POSTER_MAX_PIXELS,
                                             POSTER_MAX_PIXELS))
        # A clip shorter than the offset would otherwise be asked for a
        # frame past its end, which yields nothing at all.
        length = movie_duration(key) or 0.0
        at = POSTER_AT_SECONDS if length > POSTER_AT_SECONDS * 2 else 0.0
        result = generator.copyCGImageAtTime_actualTime_error_(
            CMTimeMakeWithSeconds(at, TIMESCALE), None, None)
        # A PAIR, not a triple. PyObjC annotates `error` as an
        # out-argument and `actualTime` as a plain input pointer, so the
        # actual time is not among the results -- unpacking three names
        # raises, the exception below swallows it, and every movie in the
        # transcript silently has no picture. Taken by position, and from
        # the ends, so an SDK that ever marks actualTime as an
        # out-argument too keeps working.
        cgimage, error = result[0], result[-1]
        if cgimage is None:
            BlinkLogger().log_info(
                'No poster for %s: %s'
                % (os.path.basename(key),
                   error.localizedDescription() if error else 'no frame'))
        else:
            # NSZeroSize means "the size the pixels actually are", which
            # is what the bubble's own scaling wants to start from.
            image = NSImage.alloc().initWithCGImage_size_(cgimage, NSZeroSize)
            BlinkLogger().log_info('Poster for %s at %.1fs (%.0fx%.0f)'
                                   % (os.path.basename(key), at,
                                      image.size().width, image.size().height))
    except Exception as e:
        BlinkLogger().log_info('Cannot poster %s: %s'
                               % (os.path.basename(key), e))
    return _remember(_posters, key, image, _POSTER_CACHE_MAX)


def forget_movie(path):
    """Drop what was learned about a file, so it can be asked again.

    A movie that is still downloading is on disc under its final name
    and answers every question wrongly: no frame, no length. Those
    answers are cached like any other, so the retry once the rest of it
    lands would be served the same nothing from memory.
    """
    key = str(path or '')
    _posters.pop(key, None)
    _durations.pop(key, None)


def _announce_playback():
    """Tell the shared monitor that the playing state may have moved.

    Imported here rather than at the top of the module: the monitor reads
    both players, so a module-level import would be a circle.
    """
    try:
        from PlaybackMonitor import notify_playback_change
        notify_playback_change()
    except Exception as e:
        BlinkLogger().log_error('Cannot announce the playback state: %s' % e)


class VideoPlayback(object):
    """The one player. `VideoPlayback()` always returns it."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(VideoPlayback, cls).__new__(cls)
            cls._instance._player = None
            cls._instance._layer = None
            cls._instance._host = None
            cls._instance._path = None
            cls._instance._key = None
            # Where a seek is heading while AVFoundation gets there. The
            # scrub bar is redrawn the instant the mouse moves and the
            # player's own clock still reads the old time for a frame or
            # two, so without this the thumb springs back under the
            # pointer -- and on a PAUSED clip nothing ticks afterwards to
            # correct it, so it springs back and stays there.
            cls._instance._seek_target = None
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
            # Rate rather than a status enum: it is the one thing that is
            # true the instant play() is called, so the key flips to
            # pause on the press rather than on the next timer tick.
            return abs(float(player.rate())) > 0.001
        except Exception:
            return False

    def duration(self, key=None):
        """Length in seconds of the loaded clip, or 0."""
        if key is not None and not self.is_current(key):
            return 0.0
        if self._path:
            return float(movie_duration(self._path) or 0.0)
        return 0.0

    def position(self, key=None):
        """How far into the clip we are, in seconds."""
        if key is not None and not self.is_current(key):
            return 0.0
        player = self._player
        if player is None:
            return 0.0
        try:
            seconds = CMTimeGetSeconds(player.currentTime())
        except Exception:
            return 0.0
        if seconds is None or math.isnan(seconds):
            return 0.0
        pending = self._seek_target
        if pending is not None:
            if abs(float(seconds) - pending) <= SEEK_TOLERANCE * 2.0:
                self._seek_target = None        # the player caught up
            else:
                return pending
        return max(float(seconds), 0.0)

    def progress(self, key=None):
        """Position as a fraction of the whole, 0..1."""
        length = self.duration(key)
        if length <= 0:
            return 0.0
        return min(max(self.position(key) / length, 0.0), 1.0)

    def finished(self, key):
        """True when this clip has played to its end and stopped there.

        Distinguished from "not playing" for the same reason it is in
        AudioPlayback: the bar stays full with the play key back, rather
        than snapping to zero the moment the picture stops.
        """
        if not self.is_current(key) or self.is_playing(key):
            return False
        length = self.duration(key)
        return length > 0 and self.position(key) >= length - 0.05

    # -- the picture -------------------------------------------------------

    def attach(self, view):
        """Put the picture in this view, taking it off whatever had it.

        Called on every layout as well as on the press, so the layer
        follows a bubble that reflows -- a window resize moves the
        poster, and a layer left at the old frame would be a rectangle
        of video sitting somewhere the picture no longer is.
        """
        layer = self._layer
        if layer is None or view is None:
            return False
        try:
            host = view.layer()
            if host is None:
                # The view has not been told to be layer-backed yet, or
                # AppKit has not built the layer. Nothing to add to, and
                # asking again next layout costs nothing.
                return False
            if self._host is not view:
                self.detach()
                host.addSublayer_(layer)
                self._host = view
            # Undo what detach did to this view, whenever that was. detach
            # HIDES the view it takes the layer off -- deliberately, so the
            # bubble behind it goes back to painting its poster -- and
            # nothing here ever put it back. So a view that has hosted the
            # picture once is hidden for the rest of its life, and the
            # second press of play on it plays a movie into a hidden view:
            # sound, a running clock, and no picture. Every play after the
            # first one in a session went that way, because by then some
            # earlier attach had detached from it -- the preview window
            # closing after a movie was sent is exactly such a moment.
            if view.isHidden():
                view.setHidden_(False)
            bounds = view.bounds()
            # No implicit animation: the layer would otherwise slide into
            # place over a quarter of a second every time the transcript
            # relaid out, which reads as the video drifting.
            from Quartz import CATransaction
            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            layer.setFrame_(bounds)
            CATransaction.commit()
            return True
        except Exception as e:
            BlinkLogger().log_error('Cannot show the picture: %s' % e)
            return False

    def detach(self):
        """Take the layer back, and tell the view that was holding it.

        The telling is not a courtesy. Whether a bubble paints its poster
        is decided in its own drawRect from who owns the player, and this
        player is global: it can be stopped from a conversation the
        holding bubble is not in -- another window pressing play, a
        recording starting -- and that conversation's refresh loop walks
        its own transcript and never reaches it. Left untold, the bubble
        keeps the empty frame it drew while the picture was over it.
        """
        layer = self._layer
        host = self._host
        self._host = None
        if layer is not None:
            try:
                layer.removeFromSuperlayer()
            except Exception as e:
                BlinkLogger().log_debug('Cannot remove the video layer: %s' % e)
        if host is None:
            return
        try:
            host.setHidden_(True)
            bubble = host.superview()
            if bubble is not None:
                bubble.setNeedsDisplay_(True)
        except Exception as e:
            BlinkLogger().log_debug('Cannot hide the video host: %s' % e)

    def host(self):
        """The view currently showing the picture, or None."""
        return self._host

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
            player = AVPlayer.playerWithURL_(url)
        except Exception as e:
            BlinkLogger().log_error('Cannot open %s for playback: %s' % (path, e))
            return False
        if player is None:
            BlinkLogger().log_error('Cannot play %s: unsupported movie'
                                    % os.path.basename(str(path)))
            return False
        try:
            layer = AVPlayerLayer.layer()
            layer.setPlayer_(player)
            layer.setVideoGravity_(AVLayerVideoGravityResizeAspect)
            # The bubble draws a rounded frame under the poster and the
            # layer sits inside it. This is MAP_RADIUS, kept as a number
            # rather than imported: this module must not depend on the
            # view that happens to borrow it. If one moves, move both --
            # the symptom is corners squaring up the instant play is
            # pressed.
            layer.setCornerRadius_(8.0)
            layer.setMasksToBounds_(True)
        except Exception as e:
            BlinkLogger().log_error('Cannot build a video layer: %s' % e)
            return False
        self._player = player
        self._layer = layer
        self._path = str(path)
        self._key = key
        BlinkLogger().log_info('Playing %s (%.1fs)'
                               % (os.path.basename(self._path), self.duration()))
        return True

    def toggle(self, path, key):
        """Play this movie, or pause it if it is the one playing.

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
            player.play()
        except Exception as e:
            BlinkLogger().log_error('Cannot start playback: %s' % e)
            return False
        # Announced here rather than by the bubble that pressed play: a
        # control that can stop playback from another window has to hear
        # about a clip started anywhere.
        _announce_playback()
        return True

    def pause(self):
        player = self._player
        if player is None:
            return
        try:
            player.pause()
        except Exception as e:
            BlinkLogger().log_error('Cannot pause playback: %s' % e)
        _announce_playback()

    def stop(self):
        """Give up the player entirely, and the layer with it."""
        player = self._player
        self.detach()
        self._player = None
        self._layer = None
        self._path = None
        self._key = None
        self._seek_target = None
        if player is None:
            return
        try:
            player.pause()
            # Dropping the item as well as the player: an AVPlayer still
            # holding a decoded item keeps the file open, and a movie
            # whose message has just been deleted must let go of it.
            player.replaceCurrentItemWithPlayerItem_(None)
        except Exception as e:
            BlinkLogger().log_error('Cannot stop playback: %s' % e)
        _announce_playback()

    def stop_for_key(self, key):
        """Stop only if this bubble is the one playing.

        Used when a conversation closes or a message is deleted: another
        conversation's movie must keep playing.
        """
        if self.is_current(key):
            self.stop()

    def seek(self, fraction, key=None, fallback=0.0):
        """Jump to a point in the clip, given as 0..1.

        `fallback` is the length the envelope claims, used when the
        container reports none of its own -- the target time is a
        fraction OF the duration, so without it such a clip could only
        ever be scrubbed to its very beginning.
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
        self._seek_target = wanted

        def landed(finished):
            # Called on one of AVFoundation's own queues, so it does the
            # one thing that is safe from there: forget the pending
            # position. Nothing here touches AppKit, and the next timer
            # tick or the next draw reads the player's real clock.
            if self._seek_target == wanted:
                self._seek_target = None

        try:
            tolerance = CMTimeMakeWithSeconds(SEEK_TOLERANCE, TIMESCALE)
            player.seekToTime_toleranceBefore_toleranceAfter_completionHandler_(
                CMTimeMakeWithSeconds(wanted, TIMESCALE), tolerance, tolerance,
                landed)
        except Exception as e:
            self._seek_target = None
            BlinkLogger().log_error('Cannot seek: %s' % e)
            return False
        BlinkLogger().log_debug('Seek %.1fs -> %.1fs of %.1fs'
                                % (before, wanted, length))
        return True
