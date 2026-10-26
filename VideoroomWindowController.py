# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The window of a video conference room joined over WebRTC (Videoroom.py).

The participants are tiles sized to the window: 1 fills it; 2 side by side, or
one over the other in a portrait window; 3 in a row, a column or 2 over 1 by
the window's shape; 4 to 8 in two rows (2x2, 3+2, 3x2, 4+3, 4x2). The pictures
are cropped to fill their tile (our own shared screen is fitted). Our own
camera, mirrored, as "Me", is in the grid, or floats over it as a thumbnail
when that keeps the grid even (or when the others fill it); a button on our
tile and menu items force it either way, and the thumbnail can be dragged to
any corner. What is not in the grid goes in the carousel above the call bar:
past the grid's 8, audio only participants (SIP callers, feeds without video,
as round avatars), those demoted after losing video, and, with speakers set,
everyone but the speakers. A carousel thumbnail is picked with a click and
replaces the grid tile clicked next.

Names are pills at the tiles' outer corners, moved above the call bar and our
thumbnail when under them. A raised hand is a yellow badge beside the name, a
participant speaking has a green border (a green ring as an avatar), and the
mouse on a tile shows its codec and bitrate. A tile that gets no new frames for
VIDEO_FROZEN_TIME keeps its last picture with a "weak video" mark; after
VIDEO_DEGRADE_GRACE it goes to the carousel (not a shared screen), and comes back
when the video does.

The call bar is the video call window's (VideoCallBar), at 75 % and lifted
above the bottom row's names; it fades out after 5 seconds without the mouse
moving, with the carousel and the button on our own tile that moves it between
the grid and the thumbnail. Camera and More are menus. Raising hands and muting
are there with three people or more, speaker selection with four or more
(distinct addresses, us included); Stop sharing while the screen is shared.

Our extra videos (ExtraVideo.py: a second camera or an RTSP camera, with a
gateway that takes several videos per participant) are tiles of their own, drawn from their own
track: More -> Add video…, and on their tile Send at and Stop this video. While
the screen is shared with nobody chosen as speaker, the speakers follow our
videos: the screen alone, or the screen and our one other video; with more
than two videos of ours, nobody.

The title is "Room <name> · <n> participants · mm:ss" once connected; a status
pill says what is happening while connecting, why the room ended, and short
notes ("X raised a hand", "Muted by X", "Calling X…"). The window is listed in
the Window menu while it is open. Contacts dragged onto it are invited.
"""

__all__ = ['VideoroomWindowController', 'window_for_session', 'room_title']

import sys
import time

from math import ceil, floor

import objc

from AppKit import (NSApp,
                    NSAnimationContext,
                    NSBackingStoreBuffered,
                    NSBezierPath,
                    NSButton,
                    NSColor,
                    NSDragOperationCopy,
                    NSDragOperationNone,
                    NSFont,
                    NSFontAttributeName,
                    NSFontWeightMedium,
                    NSFontWeightSemibold,
                    NSForegroundColorAttributeName,
                    NSImage,
                    NSImageSymbolConfiguration,
                    NSMakePoint,
                    NSMakeRect,
                    NSMakeSize,
                    NSMenu,
                    NSMenuItem,
                    NSOffState,
                    NSOnState,
                    NSPanel,
                    NSPopUpButton,
                    NSScreen,
                    NSSegmentedControl,
                    NSTextField,
                    NSTrackingActiveAlways,
                    NSTrackingArea,
                    NSTrackingInVisibleRect,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingMouseMoved,
                    NSView,
                    NSWindow,
                    NSWindowBelow,
                    NSWindowCollectionBehaviorFullScreenPrimary,
                    NSWindowOcclusionStateVisible,
                    NSWindowStyleMaskClosable,
                    NSWindowStyleMaskFullScreen,
                    NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled,
                    NSViewHeightSizable,
                    NSViewWidthSizable)
from Foundation import NSAttributedString, NSLocalizedString, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from Quartz import CATransform3DMakeScale, CATransform3DTranslate, CATransform3DIdentity

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from VideoWindowController import VideoCallBar, make_call_bar_segment
from WebRTCSupport import split_uri, webrtc_classes
from util import call_in_gui_thread, run_in_gui_thread


TILE_GAP = 6.0
TILE_RADIUS = 8.0
GRID_MAX = 8                    # tiles in the grid (4x2); the others go in the carousel
THREE_IN_A_ROW_ASPECT = 2.2     # 3 tiles: in a row when the window is at least this wide for its height
THREE_IN_A_COLUMN_ASPECT = 0.75 # and in a column when it is at most this (portrait)
TWO_IN_A_COLUMN_ASPECT = 1.0    # 2 tiles: one over the other when the window is at most this wide for its height
FLOATING_MARGIN = 12.0          # the floating thumbnail of our own video, from the window's edges
FLOATING_MIN_W, FLOATING_MAX_W = 160.0, 320.0
CAROUSEL_THUMB_W = 80.0
CAROUSEL_THUMB_H = 45.0
CAROUSEL_PADDING = 4.0
CAROUSEL_GAP = 8.0              # between the carousel and the call bar
CAROUSEL_ARROW_W = 24.0
COMPACT_TILE_HEIGHT = 60.0      # smaller (the carousel's): no name pill, the name is the tooltip
NAME_PILL_HEIGHT = 22.0
NAME_PILL_MARGIN = 8.0
THUMBNAIL_BUTTON_SIZE = 26.0
CALL_BAR_SCALE = 0.75
CALL_BAR_LIFT = TILE_GAP + NAME_PILL_MARGIN + NAME_PILL_HEIGHT + 6.0     # the call bar's bottom, from the window's
ENDED_LINGER = 2.5              # seconds the window stays to show why the room ended
CONTROLS_IDLE_TIME = 5.0        # seconds without the mouse moving before the call bar hides
FLASH_TIME = 3.0                # seconds a note stays in the status pill
WINDOW_SIZE = (960.0, 600.0)
WINDOW_MINIMUM = (360.0, 240.0)
VIDEO_PAUSE_DELAY = 2.0         # seconds out of sight before the feeds' video is paused
# Video loss. The bitrate alone says nothing: a shared screen that does not change encodes to
# almost nothing and is still fine. A tile is degraded when no new frames are decoded for
# VIDEO_FROZEN_TIME (frames decoded from the statistics; video bytes when those are not known):
# it keeps its place and its last picture, with a "weak video" mark. Degraded for
# VIDEO_DEGRADE_GRACE (Sylk Mobile) it is demoted to the carousel, unless the user put it in the
# grid, it is a speaker or it is sharing a screen. New frames recover it, and a demoted tile
# returns to the grid. No new frames for VIDEO_STALL_TIME asks the server once for the video again
# (Sylk Mobile), not for a screen. Until the room says who shares a screen, a feed of 1280x720 or
# more (SCREEN_GUESS_PIXELS) is taken as one: cameras come at VGA from Blink.
VIDEO_BITRATE_ALPHA = 0.4           # the average bitrate, for the stats pill
VIDEO_WARMUP_TIME = 5               # seconds after the feed comes up before it can degrade
VIDEO_FROZEN_TIME = 4               # seconds without new frames before the tile is degraded
VIDEO_DEGRADE_GRACE = 30            # seconds degraded before the tile leaves the grid
VIDEO_STALL_TIME = 20               # seconds without new frames before asking for the video again
SCREEN_GUESS_PIXELS = 1280 * 720
# The room's controls by how many people (distinct addresses, us included) are in it
ROOM_CONTROLS_AORS = 3              # raise hand, mute (everyone or one)
SPEAKER_CONTROLS_AORS = 4           # speaker selection and the speaker items
PENDING_CAMERA_WAIT = 30.0          # seconds the cameras chosen in Join Conference wait for the screen to free the SDK's camera

SESSION_NOTIFICATIONS = ('VideoroomSessionDidChangeState', 'VideoroomSessionAudioMutedDidChange', 'VideoroomSessionVideoMutedDidChange',
                         'VideoroomSessionParticipantDidJoin', 'VideoroomSessionParticipantDidLeave', 'VideoroomSessionParticipantDidChange',
                         'VideoroomSessionParticipantsDidChange', 'VideoroomSessionSpeakersDidChange', 'VideoroomSessionRaisedHandsDidChange',
                         'VideoroomSessionWasMutedByOther', 'VideoroomSessionGotInviteStatus', 'VideoroomSessionSpeakingDidChange',
                         'VideoroomSessionParticipantSpeakingDidChange', 'VideoroomSessionLocalVideoDidChange',
                         'VideoroomSessionExtraVideoDidChangeState')

_windows = {}       # session -> VideoroomWindowController


def window_for_session(session):
    """The window of a session, made on first use."""
    controller = _windows.get(session)
    if controller is None:
        controller = _windows[session] = VideoroomWindowController.alloc().initWithSession_(session)
    return controller


def _t(text):
    return NSLocalizedString(text, "Video conference window")


def _white(alpha):
    return NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha)


def _black(alpha):
    return NSColor.colorWithCalibratedWhite_alpha_(0.0, alpha)


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)


def _hand_color():
    return _rgb(255, 196, 0)


def _speaking_color():
    return _rgb(52, 199, 89)


def _pick_color():
    return _rgb(10, 132, 255)


def _timer(interval, function, repeats=False):
    timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, repeats, lambda timer: function())
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return timer


def _sending_screen():
    from Videoroom import sending_screen_device
    return sending_screen_device()


def room_title(room):
    """The room's display name when its contact has one, else Room <user part>."""
    user = room.partition('@')[0]
    name = None
    try:
        contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(room)
        name = getattr(contact, 'name', None)
    except Exception:
        pass
    if name and name.partition('@')[0] != user and name != room:
        return str(name)
    return _t("Room %s") % user


# Geometry: the views are flipped (y grows down), rects are NSRects

def _rect(x, y, width, height):
    return NSMakeRect(x, y, width, height)


def _inset(rect, dx, dy):
    return NSMakeRect(rect.origin.x + dx, rect.origin.y + dy, rect.size.width - 2 * dx, rect.size.height - 2 * dy)


def _intersects(a, b):
    return (a.origin.x < b.origin.x + b.size.width and b.origin.x < a.origin.x + a.size.width
            and a.origin.y < b.origin.y + b.size.height and b.origin.y < a.origin.y + a.size.height)


def _contains(rect, point):
    return (rect.origin.x <= point.x <= rect.origin.x + rect.size.width
            and rect.origin.y <= point.y <= rect.origin.y + rect.size.height)


# Drawing

_symbol_cache = {}
_TONES = {'white': lambda: _white(0.95), 'red': NSColor.systemRedColor, 'dark': lambda: _rgb(40, 30, 0)}


def _symbol(name, tone, size):
    """An SF Symbol drawn in a colour (not a template), or None."""
    key = (name, tone, size)
    if key in _symbol_cache:
        return _symbol_cache[key]
    image = None
    try:
        base = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if base is not None:
            config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, NSFontWeightSemibold)
            config = config.configurationByApplyingConfiguration_(NSImageSymbolConfiguration.configurationWithPaletteColors_([_TONES[tone]()]))
            image = base.imageWithSymbolConfiguration_(config)
    except Exception:
        image = None
    _symbol_cache[key] = image
    return image


def _draw_symbol(name, tone, rect, size=11.0):
    image = _symbol(name, tone, size)
    if image is None:
        return False
    image_size = image.size()
    if not image_size.width or not image_size.height:
        return False
    scale = min(1.0, rect.size.width / image_size.width, rect.size.height / image_size.height)
    width, height = image_size.width * scale, image_size.height * scale
    target = _rect(rect.origin.x + (rect.size.width - width) / 2, rect.origin.y + (rect.size.height - height) / 2, width, height)
    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(target, NSMakeRect(0, 0, 0, 0), 2, 1.0, True, None)     # NSCompositingOperationSourceOver
    return True


def _text(text, size, color, weight=None):
    font = NSFont.systemFontOfSize_weight_(size, weight) if weight is not None else NSFont.systemFontOfSize_(size)
    return NSAttributedString.alloc().initWithString_attributes_(text, {NSFontAttributeName: font, NSForegroundColorAttributeName: color})


def _draw_text_centered(label, rect):
    size = label.size()
    label.drawAtPoint_(NSMakePoint(rect.origin.x + (rect.size.width - size.width) / 2, rect.origin.y + (rect.size.height - size.height) / 2))


def _pill_label(text, available):
    label = _text(text, 11.0, _white(0.95), NSFontWeightMedium)
    while label.size().width > available and len(text) > 1:
        text = text[:-2] + '…'
        label = _text(text, 11.0, _white(0.95), NSFontWeightMedium)
    return label


def _name_pill_rect(rect, text, muted, corner=('bottom', 'left'), avoid=()):
    """(where the name pill goes, its label): at the tile's corner (vertical top/bottom, horizontal
    left/center/right), lifted above anything of avoid it would be under."""
    icon_width = 18.0 if muted else 0.0
    label = _pill_label(text, rect.size.width - 24.0 - icon_width)
    width = ceil(label.size().width) + 16.0 + icon_width
    vertical, horizontal = corner
    if horizontal == 'right':
        x = rect.origin.x + rect.size.width - NAME_PILL_MARGIN - width
    elif horizontal == 'center':
        x = rect.origin.x + (rect.size.width - width) / 2
    else:
        x = rect.origin.x + NAME_PILL_MARGIN
    y = rect.origin.y + NAME_PILL_MARGIN if vertical == 'top' else rect.origin.y + rect.size.height - NAME_PILL_HEIGHT - NAME_PILL_MARGIN
    pill = _rect(x, y, width, NAME_PILL_HEIGHT)
    for obstacle in avoid or ():
        if _intersects(pill, obstacle):
            pill = _rect(x, obstacle.origin.y - NAME_PILL_HEIGHT, width, NAME_PILL_HEIGHT)
            if pill.origin.y < rect.origin.y + NAME_PILL_MARGIN:
                pill = _rect(x, rect.origin.y + NAME_PILL_MARGIN, width, NAME_PILL_HEIGHT)     # a tile too short for both: at the top
    return pill, label


def _draw_name_pill(rect, text, muted, corner=('bottom', 'left'), avoid=()):
    pill, label = _name_pill_rect(rect, text, muted, corner, avoid)
    _black(0.55).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(pill, NAME_PILL_HEIGHT / 2, NAME_PILL_HEIGHT / 2).fill()
    x = pill.origin.x + 8.0
    if muted:
        if not _draw_symbol('mic.slash.fill', 'red', _rect(x - 2, pill.origin.y + 3, 18, 16)):
            mark = _text('●', 9.0, NSColor.systemRedColor())
            mark.drawAtPoint_(NSMakePoint(x, pill.origin.y + (NAME_PILL_HEIGHT - mark.size().height) / 2))
        x += 18.0
    label.drawAtPoint_(NSMakePoint(x, pill.origin.y + (NAME_PILL_HEIGHT - label.size().height) / 2))


def _draw_placeholder(rect, name, detail=None):
    """The initial in a circle, for a tile without a picture."""
    diameter = min(rect.size.width, rect.size.height) * 0.36
    center_x = rect.origin.x + rect.size.width / 2
    center_y = rect.origin.y + rect.size.height / 2 - 6.0
    circle = _rect(center_x - diameter / 2, center_y - diameter / 2, diameter, diameter)
    _rgb(70, 74, 82).setFill()
    NSBezierPath.bezierPathWithOvalInRect_(circle).fill()
    _draw_text_centered(_text((name.strip()[:1] or '?').upper(), max(10.0, floor(diameter * 0.45)), _white(0.9), NSFontWeightSemibold), circle)
    if detail:
        small = _text(detail, 11.0, _white(0.6))
        small.drawAtPoint_(NSMakePoint(center_x - small.size().width / 2, circle.origin.y + diameter + 6.0))


def _hand_rect(rect, corner, compact=False):
    """Where the raised hand badge goes: the corner beside the name pill's (the other side, same edge)."""
    size = 16.0 if compact else NAME_PILL_HEIGHT + 4.0
    margin = 3.0 if compact else NAME_PILL_MARGIN
    vertical, horizontal = ('top', 'left') if compact else corner
    x = rect.origin.x + margin if horizontal == 'right' or compact else rect.origin.x + rect.size.width - margin - size
    y = rect.origin.y + margin if vertical == 'top' else rect.origin.y + rect.size.height - margin - size
    return _rect(x, y, size, size)


def _draw_hand_badge(badge):
    _hand_color().setFill()
    NSBezierPath.bezierPathWithOvalInRect_(badge).fill()
    size = badge.size.width
    _draw_symbol('hand.raised.fill', 'dark', _inset(badge, size * 0.18, size * 0.18), size=size * 0.5)


def _draw_speaking(rect, compact=False):
    width = 2.0 if compact else 3.0
    _speaking_color().setStroke()
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(_inset(rect, width / 2, width / 2), TILE_RADIUS, TILE_RADIUS)
    path.setLineWidth_(width)
    path.stroke()


def _draw_stats(rect, text):
    """The feed's codec and bitrate, a small pill at the top middle of the tile (while the mouse is on it)."""
    label = _text(text, 10.0, _white(0.85))
    width = ceil(label.size().width) + 14.0
    pill = _rect(rect.origin.x + (rect.size.width - width) / 2, rect.origin.y + NAME_PILL_MARGIN, width, 18.0)
    _black(0.6).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(pill, 9.0, 9.0).fill()
    _draw_text_centered(label, pill)


def _draw_replace_hint(rect):
    label = _text(_t("Replace"), 12.0, _white(1.0), NSFontWeightSemibold)
    width = ceil(label.size().width) + 24.0
    pill = _rect(rect.origin.x + (rect.size.width - width) / 2, rect.origin.y + (rect.size.height - 28.0) / 2, width, 28.0)
    _pick_color().setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(pill, 14.0, 14.0).fill()
    _draw_text_centered(label, pill)


def _draw_thumbnail_button(button, floating, hovering):
    """On our own tile: out of the grid into the floating thumbnail, or back."""
    _black(0.75 if hovering else 0.55).setFill()
    NSBezierPath.bezierPathWithOvalInRect_(button).fill()
    frame = _rect(button.origin.x + 6.5, button.origin.y + 8.5, button.size.width - 13.0, button.size.height - 17.0)
    _white(0.92).setStroke()
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(frame, 1.5, 1.5)
    path.setLineWidth_(1.4)
    path.stroke()
    _white(0.92).setFill()
    if floating:
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(_inset(frame, 1.5, 1.5), 1.0, 1.0).fill()
    else:
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            _rect(frame.origin.x + frame.size.width - 6.0, frame.origin.y + frame.size.height - 4.5, 5.0, 3.5), 0.8, 0.8).fill()


# Tiles

class VideoroomTileOverlay(NSView):
    """What is drawn over a tile: the placeholder or the avatar, the name pill and the badges."""

    tile = None

    def isFlipped(self):
        return True

    def isOpaque(self):
        return False

    def hitTest_(self, point):
        return None

    def drawRect_(self, rect):
        tile = self.tile
        if tile is not None:
            tile.drawOverlay(self.bounds())


class VideoroomTileView(NSView):
    """A participant (or ourselves, participant None): its video track in an RTCMTLNSVideoView, cropped to
    fill the tile (our shared screen fitted), or the initial; as a round avatar in the carousel without video."""

    controller = None
    session = None
    participant = None
    track = None
    videoView = None
    videoContainer = None
    overlay = None
    videoSize = None

    @objc.python_method
    def setup(self, controller, participant=None):
        self.controller = controller
        self.session = controller.session
        self.participant = participant
        self.label_corner = ('bottom', 'left')
        self.hand = False
        self.picked = False         # in the carousel, chosen to go into the grid
        self.replaceable = False    # in the grid, while a carousel tile is picked: a click puts it here
        self.hovering = False

        # video loss (remote tiles)
        self.since = None           # when the feed's video was first sampled
        self.bitrate = 0.0          # the average received video bitrate, bit/s
        self._sample = None         # (time, video bytes) of the previous sample
        self.last_bytes = None      # when new frames (or bytes, without frame counts) last came
        self.refreshed = False      # the server was asked for the video again during this stall
        self.degraded = None        # since when too little video comes: the initial is shown in place
        self.demoted = False        # degraded too long: in the carousel, as audio only
        self.pinned = False         # put in the grid by the user: degraded, but never demoted
        # our own tile
        self.thumbnail_enabled = False
        self.floating = False
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(TILE_RADIUS)
        self.layer().setMasksToBounds_(True)
        # the video view sits in a container, which mirrors our own picture
        self.videoContainer = NSView.alloc().initWithFrame_(self.bounds())
        self.videoContainer.setWantsLayer_(True)
        self.videoContainer.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.addSubview_(self.videoContainer)
        view_class = webrtc_classes().get('RTCMTLNSVideoView')
        if view_class is not None and view_class.isMetalAvailable():
            self.videoView = view_class.alloc().initWithFrame_(self.bounds())
            self.videoView.setDelegate_(self)
            self.videoView.setHidden_(True)
            self.videoContainer.addSubview_(self.videoView)
        self.overlay = VideoroomTileOverlay.alloc().initWithFrame_(self.bounds())
        self.overlay.tile = self
        self.overlay.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.addSubview_(self.overlay)
        self.refresh()

    def isFlipped(self):
        return True

    def hitTest_(self, point):
        return None         # clicks and the mouse go to the window's content view

    # What it is

    @objc.python_method
    def name(self):
        if self.participant is not None:
            return self.participant.name
        account = self.session.account
        return getattr(account, 'display_name', None) or str(account.id)

    @objc.python_method
    def label(self):
        return self.participant.name if self.participant is not None else _t("Me")

    @objc.python_method
    def muted(self):
        return self.participant.muted if self.participant is not None else self.session.audio_muted

    @objc.python_method
    def speaking(self):
        if self.participant is not None:
            return self.participant.speaking
        # ourselves: only when someone else is there to hear it
        return self.session.speaking and any(participant.visible for participant in self.session.participants.values())

    @objc.python_method
    def in_grid(self):
        """Audio only participants, and those demoted after losing video, are not in the grid, only in the carousel."""
        return self.participant is None or (self.participant.video is not False and not self.demoted)

    @objc.python_method
    def compact(self):
        return self.bounds().size.height < COMPACT_TILE_HEIGHT

    @objc.python_method
    def avatar(self):
        """In the carousel without video (audio only, or demoted): a round avatar, nothing around it."""
        return self.compact() and not self.in_grid()

    @objc.python_method
    def screen(self):
        return self.participant is None and _sending_screen()

    @objc.python_method
    def detail(self):
        if self.participant is None:
            if self.session.video_muted:
                return _t("camera off")
            return _t("no camera") if self.videoView is None else None
        participant = self.participant
        if participant.video is False or self.demoted:
            return _t("audio")
        if self.degraded is not None:
            return _t("no video")
        if participant.state in ('attaching', 'connecting'):
            return _t("connecting…")
        if participant.state == 'failed':
            return _t("no media")
        return None

    @objc.python_method
    def showingVideo(self):
        # degraded, the last picture stays (a still shared screen is still worth seeing)
        return not (self.videoView is None or self.track is None or self.videoSize is None or self.avatar())

    @objc.python_method
    def wantedTrack(self):
        if self.participant is not None:
            return self.participant.video_track if self.participant.video is not False else None
        return None if self.session.video_muted else self.session.local_video_track

    @objc.python_method
    def refresh(self):
        """Follow the session: the track to render, the overlay."""
        track = self.wantedTrack()
        if track != self.track and self.videoView is not None:
            if self.track is not None:
                self.track.removeRenderer_(self.videoView)
            self.track = track
            self.videoSize = None
            if track is not None:
                track.addRenderer_(self.videoView)
        self.layoutVideo()
        self.redraw()

    @objc.python_method
    def redraw(self):
        if self.overlay is None:
            return
        avatar = self.avatar()
        self.layer().setBackgroundColor_((NSColor.clearColor() if avatar else _rgb(32, 34, 38)).CGColor())
        self.layer().setCornerRadius_(0.0 if avatar else TILE_RADIUS)
        self.overlay.setNeedsDisplay_(True)

    @objc.python_method
    def teardown(self):
        if self.track is not None and self.videoView is not None:
            self.track.removeRenderer_(self.videoView)
        self.track = None
        if self.videoView is not None:
            self.videoView.setDelegate_(None)

    # The picture: cropped to fill the tile, our shared screen fitted

    @objc.typedSelector(b'v@:@{CGSize=dd}')
    def videoView_didChangeVideoSize_(self, view, size):
        call_in_gui_thread(self._videoSizeChanged, size.width, size.height)

    @objc.python_method
    def _videoSizeChanged(self, width, height):
        self.videoSize = (width, height) if width > 0 and height > 0 else None
        self.layoutVideo()
        self.redraw()

    @objc.python_method
    def layoutVideo(self):
        if self.videoView is None or self.overlay is None:
            return
        bounds = self.bounds()
        if not self.showingVideo():
            self.videoView.setHidden_(True)
            return
        width, height = self.videoSize
        screen = self.screen()
        if screen:
            scale = min(bounds.size.width / width, bounds.size.height / height)
        else:
            scale = max(bounds.size.width / width, bounds.size.height / height)
        shown_w, shown_h = ceil(width * scale), ceil(height * scale)
        self.videoView.setFrame_(NSMakeRect(floor((bounds.size.width - shown_w) / 2), floor((bounds.size.height - shown_h) / 2), shown_w, shown_h))
        self.videoView.setHidden_(False)
        layer = self.videoContainer.layer()
        if layer is not None:
            if self.participant is None and not screen:
                layer.setSublayerTransform_(CATransform3DTranslate(CATransform3DMakeScale(-1.0, 1.0, 1.0), -bounds.size.width, 0.0, 0.0))
            else:
                layer.setSublayerTransform_(CATransform3DIdentity)

    def setFrameSize_(self, size):
        old = self.bounds().size
        objc.super(VideoroomTileView, self).setFrameSize_(size)
        self.layoutVideo()
        if (old.height < COMPACT_TILE_HEIGHT) != (size.height < COMPACT_TILE_HEIGHT):
            self.layoutVideo()
            self.redraw()

    # Video loss

    @objc.python_method
    def sharingScreen(self):
        """Whether the participant seems to share a screen (SCREEN_GUESS_PIXELS) rather than a camera."""
        size = self.participant.video_size if self.participant is not None else None
        return bool(size and size[0] * size[1] >= SCREEN_GUESS_PIXELS)

    @objc.python_method
    def check_video(self, now):
        """Sample the video, once a second, and degrade, demote or recover the tile."""
        participant = self.participant
        if participant is None or participant.extra_video is not None:
            return      # ours: its picture comes from here, not from a feed
        if participant.video is not True or participant.state != 'established':
            self.since = self._sample = self.last_bytes = None
            self.bitrate = 0.0
            return
        received, frames = participant.video_bytes, participant.video_frames
        if self._sample is None:
            self.since = self.last_bytes = now
        else:
            then, before, frames_before = self._sample
            if now > then:
                rate = max(0, received - before) * 8 / (now - then)
                self.bitrate = VIDEO_BITRATE_ALPHA * rate + (1 - VIDEO_BITRATE_ALPHA) * self.bitrate
            flowing = frames > frames_before if frames is not None and frames_before is not None else received > before
            if flowing:
                self.last_bytes = now       # the last time new frames came
                self.refreshed = False
        self._sample = (now, received, frames)
        if self.hovering:
            self.overlay.setNeedsDisplay_(True)     # the stats pill follows the bitrate
        if now - self.since < VIDEO_WARMUP_TIME:
            return
        screen = self.sharingScreen()
        frozen = now - self.last_bytes
        if frozen > VIDEO_STALL_TIME and not self.refreshed and not screen:
            self.refreshed = True
            self.session.refresh_video(participant)
        log = BlinkLogger().log_info
        if frozen > VIDEO_FROZEN_TIME:
            if self.degraded is None:
                self.degraded = now
                log('[videoroom] %s video lost (no new frames for %d s%s)' % (participant.uri, frozen, ', a shared screen' if screen else ''))
                self.redraw()
            elif (not self.demoted and not self.pinned and not screen and self not in self.controller.speaker_tiles
                  and now - self.degraded > VIDEO_DEGRADE_GRACE):
                self.demoted = True
                log('[videoroom] %s has no video for %d s, moved to the carousel' % (participant.uri, VIDEO_DEGRADE_GRACE))
                self.controller.layoutTiles()
        elif self.degraded is not None:
            self.degraded = None
            log('[videoroom] %s video back' % participant.uri)
            demoted, self.demoted = self.demoted, False
            self.redraw()
            if demoted:
                self.controller.layoutTiles()

    @objc.python_method
    def set_pick_state(self, picked=False, replaceable=False):
        if (picked, replaceable) != (self.picked, self.replaceable):
            self.picked, self.replaceable = picked, replaceable
            self.overlay.setNeedsDisplay_(True)

    # Our own tile's button: into the floating thumbnail, or back into the grid

    @objc.python_method
    def thumbnailButtonRect(self):
        """In the first corner clear of the name pill and the raised hand badge: the right on the edge away
        from them first, then the others."""
        margin = 6.0
        rect = self.bounds()
        pill = _name_pill_rect(rect, _t("Me"), self.muted(), self.label_corner, self.controller.labelAvoid(self))[0]
        taken = [_inset(pill, -4, -4), _inset(_hand_rect(rect, self.label_corner), -4, -4)]
        away = 'bottom' if self.label_corner[0] == 'top' else 'top'
        near = 'top' if away == 'bottom' else 'bottom'
        size = THUMBNAIL_BUTTON_SIZE
        spots = []
        for vertical, horizontal in ((away, 'right'), (away, 'left'), (near, 'right'), (near, 'left')):
            x = rect.size.width - margin - size if horizontal == 'right' else margin
            y = rect.size.height - margin - size if vertical == 'bottom' else margin
            spots.append(_rect(x, y, size, size))
        return next((spot for spot in spots if not any(_intersects(spot, other) for other in taken)), spots[0])

    # Drawing

    @objc.python_method
    def drawOverlay(self, rect):
        compact = self.compact()
        if self.avatar():
            self._drawAvatar(rect)
            return
        if not self.showingVideo():
            _draw_placeholder(rect, self.name(), None if compact else self.detail())
        if not compact:
            _draw_name_pill(rect, self.label(), self.muted(), self.label_corner, self.controller.labelAvoid(self))
        elif self.muted():
            _draw_symbol('mic.slash.fill', 'red', _rect(rect.size.width - 18, 3, 15, 12), size=9.0)
        if self.hand:
            _draw_hand_badge(_hand_rect(rect, self.label_corner, compact))
        if self.speaking() and not (self.picked or self.replaceable):
            _draw_speaking(rect, compact)
        participant = self.participant
        if participant is not None and participant.extra_video is not None:
            if not compact and self.hovering and not self.replaceable:
                extra = participant.extra_video
                _draw_stats(rect, '%dx%d · %d kbit/s' % (extra.size + (extra.bitrate,)))
        elif participant is not None and not compact and self.hovering and not self.replaceable and participant.video is True:
            parts = [participant.video_codec or '', '%d kbit/s' % round(self.bitrate / 1000) if self._sample is not None else '',
                     _t("weak video") if self.degraded is not None else '']
            text = ' · '.join(part for part in parts if part)
            if text:
                _draw_stats(rect, text)
        elif participant is not None and not compact and self.degraded is not None and self.showingVideo():
            _draw_stats(rect, _t("weak video"))     # the last picture, kept
        if self.picked or self.replaceable:
            width = (2.0 if compact else 3.0) if self.picked or self.hovering else 1.5
            _pick_color().setStroke()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(_inset(rect, 1.5, 1.5), TILE_RADIUS, TILE_RADIUS)
            path.setLineWidth_(width)
            path.stroke()
            if self.replaceable and self.hovering:
                _draw_replace_hint(rect)

    @objc.python_method
    def _drawAvatar(self, rect):
        from Avatars import draw_avatar
        participant = self.participant
        diameter = min(rect.size.width, rect.size.height) - 4
        circle = _rect(rect.origin.x + (rect.size.width - diameter) / 2, rect.origin.y + (rect.size.height - diameter) / 2, diameter, diameter)
        draw_avatar(circle, participant.icon, self.name())
        if self.speaking():
            _speaking_color().setStroke()
            ring = NSBezierPath.bezierPathWithOvalInRect_(_inset(circle, -1, -1))
            ring.setLineWidth_(2.0)
            ring.stroke()
        if self.muted():
            badge = _rect(circle.origin.x + diameter - 13, circle.origin.y + diameter - 13, 14, 14)
            _black(0.7).setFill()
            NSBezierPath.bezierPathWithOvalInRect_(badge).fill()
            _draw_symbol('mic.slash.fill', 'red', _inset(badge, 2, 2), size=7.0)
        if self.hand:
            _draw_hand_badge(_rect(circle.origin.x - 1, circle.origin.y - 1, 14, 14))
        if self.picked:
            _pick_color().setStroke()
            ring = NSBezierPath.bezierPathWithOvalInRect_(_inset(circle, -1, -1))
            ring.setLineWidth_(2.0)
            ring.stroke()


# Our own tile's button: into the floating thumbnail, or back into the grid

class VideoroomThumbnailButton(NSButton):
    """Over our own tile, shown and hidden with the call bar."""

    floating = False
    hovering = False
    hoverArea = None

    def isFlipped(self):
        return True

    def mouseDownCanMoveWindow(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        if self.hoverArea is not None:
            self.removeTrackingArea_(self.hoverArea)
        self.hoverArea = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            NSMakeRect(0, 0, 0, 0), NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect, self, None)
        self.addTrackingArea_(self.hoverArea)
        objc.super(VideoroomThumbnailButton, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.hovering = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hovering = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        _draw_thumbnail_button(self.bounds(), self.floating, self.hovering or self.isHighlighted())

    @objc.python_method
    def setFloating(self, floating):
        if floating != self.floating:
            self.floating = floating
            self.setNeedsDisplay_(True)
        self.setToolTip_(_t("Put me back in the grid") if floating else _t("Show me as a thumbnail"))


# The carousel

class VideoroomFlippedView(NSView):
    def isFlipped(self):
        return True

    def hitTest_(self, point):
        return None


class VideoroomCarouselArrow(NSView):
    """The caret at either end of the carousel when it can scroll that way."""

    direction = 1
    carousel = None

    def isFlipped(self):
        return True

    def mouseDown_(self, event):
        if self.carousel is not None:
            self.carousel.scrollBy(self.direction * (CAROUSEL_THUMB_W + TILE_GAP))

    def drawRect_(self, rect):
        bounds = self.bounds()
        cx, cy = bounds.size.width / 2, bounds.size.height / 2
        _black(0.55).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(_rect(cx - 11, cy - 11, 22, 22)).fill()
        _white(0.85).setFill()
        path = NSBezierPath.bezierPath()
        w, h = 7.0, 12.0
        if self.direction < 0:
            path.moveToPoint_(NSMakePoint(cx + w / 2, cy - h / 2))
            path.lineToPoint_(NSMakePoint(cx - w / 2, cy))
            path.lineToPoint_(NSMakePoint(cx + w / 2, cy + h / 2))
        else:
            path.moveToPoint_(NSMakePoint(cx - w / 2, cy - h / 2))
            path.lineToPoint_(NSMakePoint(cx + w / 2, cy))
            path.lineToPoint_(NSMakePoint(cx - w / 2, cy + h / 2))
        path.closePath()
        path.fill()


def _carousel_width(tile):
    return CAROUSEL_THUMB_H if not tile.in_grid() else CAROUSEL_THUMB_W     # an avatar is round


class VideoroomCarouselView(NSView):
    """The tiles that are not in the grid, as thumbnails in a strip above the call bar, no background. It
    shows and fades with the call bar; the wheel or the carets at its ends scroll it."""

    controller = None

    def initWithFrame_(self, frame):
        self = objc.super(VideoroomCarouselView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.tiles = []
        self.offset = 0.0
        self.shown = True
        self.setWantsLayer_(True)
        self.viewport = VideoroomFlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))     # clips the strip inside the padding
        self.viewport.setWantsLayer_(True)
        self.viewport.layer().setMasksToBounds_(True)
        self.addSubview_(self.viewport)
        self.content = VideoroomFlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        self.viewport.addSubview_(self.content)
        self.leftArrow = VideoroomCarouselArrow.alloc().initWithFrame_(NSMakeRect(0, 0, CAROUSEL_ARROW_W, CAROUSEL_THUMB_H))
        self.leftArrow.direction, self.leftArrow.carousel = -1, self
        self.rightArrow = VideoroomCarouselArrow.alloc().initWithFrame_(NSMakeRect(0, 0, CAROUSEL_ARROW_W, CAROUSEL_THUMB_H))
        self.rightArrow.direction, self.rightArrow.carousel = 1, self
        self.addSubview_(self.leftArrow)
        self.addSubview_(self.rightArrow)
        self.setHidden_(True)
        return self

    def isFlipped(self):
        return True

    def hitTest_(self, point):
        # only the carets take clicks: the tiles' go to the window's content view
        if self.isHidden() or self.alphaValue() < 0.05:
            return None
        local = self.convertPoint_fromView_(point, self.superview())
        for arrow in (self.leftArrow, self.rightArrow):
            if not arrow.isHidden() and _contains(arrow.frame(), local):
                return arrow
        return None

    @objc.python_method
    def contentWidth(self):
        return sum(_carousel_width(tile) + TILE_GAP for tile in self.tiles) - TILE_GAP if self.tiles else 0.0

    @objc.python_method
    def setTiles(self, tiles):
        for tile in tiles:
            if tile.superview() is not self.content:
                tile.removeFromSuperview()
                self.content.addSubview_(tile)
            tile.setToolTip_(tile.label())
            tile.set_pick_state(picked=tile.picked, replaceable=False)
        self.tiles = list(tiles)
        x = 0.0
        for tile in self.tiles:
            width = _carousel_width(tile)
            tile.setFrame_(NSMakeRect(x, 0, width, CAROUSEL_THUMB_H))
            tile.redraw()
            x += width + TILE_GAP
        self.setHidden_(not (self.tiles and self.shown))
        self.tile()

    @objc.python_method
    def tile(self):
        container = self.superview()
        bar = self.controller.callBar if self.controller is not None else None
        if container is None or bar is None:
            return
        bounds = container.bounds()
        height = CAROUSEL_THUMB_H + 2 * CAROUSEL_PADDING
        available = bounds.size.width - 2 * TILE_GAP
        scrolls = self.contentWidth() + 2 * CAROUSEL_PADDING > available
        inset = CAROUSEL_ARROW_W if scrolls else CAROUSEL_PADDING
        width = available if scrolls else self.contentWidth() + 2 * CAROUSEL_PADDING
        self.setFrame_(NSMakeRect(floor((bounds.size.width - width) / 2), bar.frame().origin.y - CAROUSEL_GAP - height, width, height))
        self.viewport.setFrame_(NSMakeRect(inset, CAROUSEL_PADDING, max(0.0, width - 2 * inset), CAROUSEL_THUMB_H))
        self.leftArrow.setFrame_(NSMakeRect(0, 0, CAROUSEL_ARROW_W, height))
        self.rightArrow.setFrame_(NSMakeRect(width - CAROUSEL_ARROW_W, 0, CAROUSEL_ARROW_W, height))
        self.scrollTo(self.offset)

    @objc.python_method
    def scrollBy(self, delta):
        self.scrollTo(self.offset + delta)

    @objc.python_method
    def scrollTo(self, offset):
        limit = max(0.0, self.contentWidth() - self.viewport.frame().size.width)
        self.offset = max(0.0, min(offset, limit))
        self.content.setFrame_(NSMakeRect(-self.offset, 0, self.contentWidth(), CAROUSEL_THUMB_H))
        self.leftArrow.setHidden_(self.offset <= 0)
        self.rightArrow.setHidden_(self.offset >= limit)

    @objc.python_method
    def tileAtPoint(self, point, view):
        """The carousel tile under point (in view's coordinates), or None."""
        if self.isHidden() or self.alphaValue() < 0.05:
            return None
        if not _contains(self.viewport.bounds(), self.viewport.convertPoint_fromView_(point, view)):
            return None
        local = self.content.convertPoint_fromView_(point, view)
        return next((tile for tile in self.tiles if _contains(tile.frame(), local)), None)

    @objc.python_method
    def fade(self, shown):
        self.shown = shown
        if shown and self.tiles:
            self.setHidden_(False)
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.2 if shown else 0.4)
        self.animator().setAlphaValue_(1.0 if shown else 0.0)
        NSAnimationContext.endGrouping()


# The window's content

class VideoroomContentView(NSView):
    """The window's content: the grid's tiles, our floating thumbnail, the carousel, the status pill and
    the call bar. It takes every click and the mouse for them (the tiles take none)."""

    controller = None
    trackingArea = None

    def initWithFrame_(self, frame):
        self = objc.super(VideoroomContentView, self).initWithFrame_(frame)
        if self is not None:
            self.setWantsLayer_(True)
            self.layer().setBackgroundColor_(NSColor.blackColor().CGColor())
            self.registerForDraggedTypes_(['x-blink-sip-uri'])     # contacts from the contact list: invited
        return self

    def isFlipped(self):
        return True

    def isOpaque(self):
        return True

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def resizeSubviewsWithOldSize_(self, old_size):
        objc.super(VideoroomContentView, self).resizeSubviewsWithOldSize_(old_size)
        if self.controller is not None:
            self.controller.contentResized()

    def updateTrackingAreas(self):
        if self.trackingArea is not None:
            self.removeTrackingArea_(self.trackingArea)
        self.trackingArea = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            NSMakeRect(0, 0, 0, 0), NSTrackingMouseMoved | NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect, self, None)
        self.addTrackingArea_(self.trackingArea)
        objc.super(VideoroomContentView, self).updateTrackingAreas()

    @objc.python_method
    def _point(self, event):
        return self.convertPoint_fromView_(event.locationInWindow(), None)

    def mouseMoved_(self, event):
        if self.controller is not None:
            self.controller.mouseMoved(self._point(event))

    def mouseEntered_(self, event):
        self.mouseMoved_(event)

    def mouseExited_(self, event):
        if self.controller is not None:
            self.controller.setHovered(None, None)

    def mouseDown_(self, event):
        if self.controller is not None:
            self.controller.mouseDown(self._point(event), event)

    def mouseDragged_(self, event):
        if self.controller is not None:
            self.controller.mouseDragged(self._point(event))

    def mouseUp_(self, event):
        if self.controller is not None:
            self.controller.mouseUp(self._point(event))

    def rightMouseDown_(self, event):
        if self.controller is not None:
            self.controller.rightMouseDown(self._point(event), event)

    def scrollWheel_(self, event):
        if self.controller is not None:
            self.controller.scrollWheel(self._point(event), event)

    def keyDown_(self, event):
        if event.keyCode() == 53 and self.controller is not None and self.controller.escape():     # Esc
            return
        objc.super(VideoroomContentView, self).keyDown_(event)

    # Contacts dropped from the contact list are invited

    def draggingEntered_(self, info):
        if self.controller is not None and info.draggingPasteboard().availableTypeFromArray_(['x-blink-sip-uri']):
            return NSDragOperationCopy
        return NSDragOperationNone

    def draggingUpdated_(self, info):
        return self.draggingEntered_(info)

    def performDragOperation_(self, info):
        pboard = info.draggingPasteboard()
        if self.controller is None or not pboard.availableTypeFromArray_(['x-blink-sip-uri']):
            return False
        uri = str(pboard.stringForType_('x-blink-sip-uri') or '').strip()
        if not uri:
            return False
        self.controller.inviteUris([uri])
        return True


class VideoroomMenuTarget(NSObject):
    """The target of menu items whose represented object is a Python callable."""

    def fire_(self, sender):
        function = sender.representedObject()
        if callable(function):
            try:
                function()
            except Exception as e:
                BlinkLogger().log_error('[videoroom] %s' % e)


# Speaker selection, as Sylk Mobile's SpeakerSelectionModal

class SpeakerSelectionPanel(NSObject):
    """Grid / 1 speaker / 2 speakers, and who: the room's layout, for everyone (videoroom-configure)."""

    @objc.python_method
    def run(self, window, candidates, speakers, apply):
        """candidates: (publisher id, name) of who can be a speaker, ours first ("Me"); apply(ids)."""
        self.parent = window
        self.apply = apply
        self.candidates = [candidates[0]] + sorted(candidates[1:], key=lambda item: item[1].lower()) if candidates else []
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 400, 214), NSWindowStyleMaskTitled, NSBackingStoreBuffered, True)
        content = self.panel.contentView()

        def label(text, x, y, width, bold=False, size=13.0):
            field = NSTextField.labelWithString_(text)
            field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
            field.setFrame_(NSMakeRect(x, y, width, 20))
            content.addSubview_(field)
            return field

        label(_t("Speaker selection"), 20, 178, 360, bold=True)
        note = label(_t("The room's layout: everyone sees the speakers large, the others in the carousel."), 20, 150, 360, size=11.0)
        note.setTextColor_(NSColor.secondaryLabelColor())
        self.mode = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [_t("Grid"), _t("1 speaker"), _t("2 speakers")], 0, self, 'modeChanged:')
        self.mode.setFrame_(NSMakeRect(20, 114, 360, 26))
        content.addSubview_(self.mode)
        label(_t("Speaker:"), 20, 80, 120)
        label(_t("Second speaker:"), 20, 50, 120)
        self.slots = []
        for y in (76, 46):
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(140, y, 240, 26), False)
            popup.setTarget_(self)
            popup.setAction_('slotChanged:')
            content.addSubview_(popup)
            self.slots.append(popup)
        cancel = NSButton.buttonWithTitle_target_action_(_t("Cancel"), self, 'cancel:')
        cancel.setFrame_(NSMakeRect(196, 10, 92, 30))
        cancel.setKeyEquivalent_('\x1b')
        content.addSubview_(cancel)
        ok = NSButton.buttonWithTitle_target_action_(_t("Apply"), self, 'ok:')
        ok.setFrame_(NSMakeRect(290, 10, 92, 30))
        ok.setKeyEquivalent_('\r')
        content.addSubview_(ok)
        self.picked = list(speakers[:2])
        self.mode.setSelectedSegment_(len(self.picked))
        self.fill()
        window.beginSheet_completionHandler_(self.panel, None)

    @objc.python_method
    def fill(self):
        mode = self.mode.selectedSegment()
        self.picked = [id for id in self.picked if any(candidate[0] == id for candidate in self.candidates)][:mode]
        for index, popup in enumerate(self.slots):
            popup.removeAllItems()
            popup.addItemWithTitle_(_t("Nobody"))
            popup.lastItem().setRepresentedObject_(None)
            other = self.picked[1 - index] if len(self.picked) > 1 - index else None
            for id, name in self.candidates:
                if id == other:
                    continue        # one person in one slot only
                popup.addItemWithTitle_(name)
                popup.lastItem().setRepresentedObject_(id)
            current = self.picked[index] if index < len(self.picked) else None
            for item_index in range(popup.numberOfItems()):
                if popup.itemAtIndex_(item_index).representedObject() == current:
                    popup.selectItemAtIndex_(item_index)
            popup.setEnabled_(index < mode)

    def modeChanged_(self, sender):
        mode = self.mode.selectedSegment()
        if mode and not self.picked and self.candidates:
            self.picked = [self.candidates[0][0]]
        if mode == 2 and len(self.picked) == 1:
            second = next((id for id, name in self.candidates if id not in self.picked), None)
            if second is not None:
                self.picked.append(second)
        self.fill()

    def slotChanged_(self, sender):
        picked = []
        for popup in self.slots[:self.mode.selectedSegment()]:
            item = popup.selectedItem()
            id = item.representedObject() if item is not None else None
            if id is not None and id not in picked:
                picked.append(id)
        self.picked = picked
        self.fill()

    def ok_(self, sender):
        ids = self.picked[:self.mode.selectedSegment()]
        self.close()
        self.apply(ids)

    def cancel_(self, sender):
        self.close()

    @objc.python_method
    def close(self):
        self.parent.endSheet_(self.panel)
        self.panel.orderOut_(None)


# The window

@implementer(IObserver)
class VideoroomWindowController(NSObject):
    """The window of one VideoroomSession."""

    window = None
    contentView = None
    callBar = None
    statusLabel = None
    carousel = None
    localTile = None

    def initWithSession_(self, session):
        self = objc.super(VideoroomWindowController, self).init()
        if self is None:
            return None
        self.session = session
        self.tiles = {}             # participant -> tile
        self.order = []             # the tiles, ours first, the others in joining order
        self.speaker_tiles = []     # the room's speakers' tiles: alone in the grid, the others in the carousel
        self.hide_local = False     # while the screen is shared: our picture would only show it back
        self.self_hidden = False    # we asked not to see ourselves (the others still do)
        self.local_placement = None     # None: as the layout has it; 'floating' or 'grid': as we asked
        self.floating = False
        self.floating_corner = ('bottom', 'right')     # where the thumbnail is, moved by dragging it
        self.picked = None          # the carousel tile picked to go into the grid
        self.hovered = None
        self.joined_at = None
        self.closing = False
        self.idle_timer = None
        self.clock = None
        self.pause_timer = None
        self.flash_until = 0.0
        self.drag = None            # the offset of the press in the floating thumbnail, while dragging it
        self.last_hands = []
        self.invite_names = {}
        self.speaker_for_screen = False     # we made our videos the speakers while sharing the screen
        self.auto_issued = []               # the speakers we set for it (the room's answer to one may come after the next)
        self.sharing_screen = _sending_screen()
        self.device_before_screen = None
        self.minimized_for_screen = False
        self.full_screen_before = False
        self.full_screen_after_deminiaturize = False
        self.minimize_after_full_screen = False
        self.menuTarget = VideoroomMenuTarget.alloc().init()
        self.speakerPanel = None
        self.shareDialog = None
        self.title = room_title(session.room)
        self._build()
        notification_center = NotificationCenter()
        for name in SESSION_NOTIFICATIONS:
            notification_center.add_observer(self, sender=session, name=name)
        notification_center.add_observer(self, name='VideoDeviceDidChangeCamera')
        for participant in list(session.participants.values()):
            self.addParticipant(participant)
        self.updateMuteButton()
        self.updateCameraButton()
        self.updateScreenState(initial=True)
        self.setStatus(_t("Connecting…"))
        return self

    def __repr__(self):
        return '<VideoroomWindowController %s>' % self.session.room

    # The window

    @objc.python_method
    def _build(self):
        frame = self._initialFrame()
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, style, NSBackingStoreBuffered, False)
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_(self.title)
        self.window.setDelegate_(self)
        self.window.setBackgroundColor_(NSColor.blackColor())
        self.window.setContentMinSize_(NSMakeSize(*WINDOW_MINIMUM))
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorFullScreenPrimary)
        self.window.setAcceptsMouseMovedEvents_(True)

        content = VideoroomContentView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
        content.controller = self
        content.setAutoresizesSubviews_(True)
        self.window.setContentView_(content)
        self.contentView = content

        # bottom to top: the tiles, the carousel, the status pill, the call bar
        self.carousel = VideoroomCarouselView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        self.carousel.controller = self
        content.addSubview_(self.carousel)

        self.statusLabel = NSTextField.labelWithString_('')
        self.statusLabel.setFont_(NSFont.systemFontOfSize_weight_(12.0, NSFontWeightMedium))
        self.statusLabel.setTextColor_(_white(0.95))
        self.statusLabel.setDrawsBackground_(False)
        self.statusLabel.setWantsLayer_(True)
        self.statusLabel.layer().setBackgroundColor_(_black(0.55).CGColor())
        self.statusLabel.layer().setCornerRadius_(11.0)
        self.statusLabel.setAlignment_(1)       # NSTextAlignmentCenter
        content.addSubview_(self.statusLabel)

        bar = VideoCallBar.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        bar.scale = CALL_BAR_SCALE
        bar.bottom = CALL_BAR_LIFT      # above the names at the bottom of the tiles: they stay visible
        content.addSubview_(bar)
        self.callBar = bar

        def add(symbol, label, action, destructive=False, tooltip=None):
            segment = bar.addSegment(make_call_bar_segment(symbol, label, action, self, destructive))
            if tooltip:
                segment.setToolTip_(tooltip)
            return segment

        self.muteButton = add('mic.fill', _t("Mute"), 'userClickedMuteButton:')
        self.cameraButton = add('video.fill', _t("Camera"), 'userClickedCameraButton:', tooltip=_t("The camera to send, the screen, or none"))
        self.stopScreenButton = add('rectangle.on.rectangle.slash', _t("Stop sharing"), 'userClickedStopScreenButton:', destructive=True,
                                    tooltip=_t("Stop sharing the screen: the camera comes back"))
        self.stopScreenButton.setHidden_(True)
        self.speakersButton = add('person.wave.2.fill', _t("Speakers"), 'userClickedSpeakersButton:', tooltip=_t("Speaker selection: the room's layout, for everyone"))
        self.speakersButton.setHidden_(True)
        self.chatButton = add('message.fill', _t("Chat"), 'userClickedChatButton:', tooltip=_t("The room's chat, in the chat window"))
        self.handButton = add('hand.raised.fill', _t("Raise hand"), 'userClickedHandButton:')
        self.handButton.setHidden_(True)      # with more than two of us (updateRoomButtons)
        self.moreButton = add('ellipsis.circle', _t("More"), 'userClickedMoreButton:')
        self.fullScreenButton = add('arrow.up.left.and.arrow.down.right', _t("Full screen"), 'userClickedFullScreenButton:')
        self.hangupButton = add('phone.down.fill', _t("Leave"), 'userClickedHangupButton:', destructive=True)
        bar.tile()

        # over our own tile, under the status pill and the call bar
        self.thumbnailButton = VideoroomThumbnailButton.alloc().initWithFrame_(NSMakeRect(0, 0, THUMBNAIL_BUTTON_SIZE, THUMBNAIL_BUTTON_SIZE))
        self.thumbnailButton.setBordered_(False)
        self.thumbnailButton.setTitle_('')
        self.thumbnailButton.setTarget_(self)
        self.thumbnailButton.setAction_('userClickedThumbnailButton:')
        self.thumbnailButton.setHidden_(True)
        content.addSubview_positioned_relativeTo_(self.thumbnailButton, NSWindowBelow, self.statusLabel)

        self.localTile = VideoroomTileView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 180))
        self.localTile.setup(self)
        self.order.append(self.localTile)
        self.layoutTiles()

    @objc.python_method
    def _initialFrame(self):
        """Centred on the screen of the main window."""
        screen = None
        try:
            main_window = NSApp.delegate().contactsWindowController.window()
            screen = main_window.screen() if main_window is not None else None
        except Exception:
            pass
        screen = screen or NSScreen.mainScreen()
        width, height = WINDOW_SIZE
        if screen is None:
            return NSMakeRect(100, 100, width, height)
        visible = screen.visibleFrame()
        width, height = min(width, visible.size.width), min(height, visible.size.height)
        return NSMakeRect(visible.origin.x + (visible.size.width - width) / 2, visible.origin.y + (visible.size.height - height) / 2, width, height)

    @objc.python_method
    def show(self):
        if self.window.isMiniaturized():
            self.window.deminiaturize_(None)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.contentView)
        self.showControls()

    def windowWillClose_(self, notification):
        if self.session.state not in ('idle', 'terminated'):
            self.endRoomChat()
            self.session.leave()
        self.teardown()

    @objc.python_method
    def close(self):
        if self.window is not None and self.window.isVisible():
            self.window.close()
        else:
            self.teardown()

    @objc.python_method
    def teardown(self):
        if self.closing:
            return
        self.closing = True
        for timer in (self.idle_timer, self.clock, self.pause_timer):
            if timer is not None:
                timer.invalidate()
        self.idle_timer = self.clock = self.pause_timer = None
        notification_center = NotificationCenter()
        for name in SESSION_NOTIFICATIONS:
            notification_center.discard_observer(self, sender=self.session, name=name)
        notification_center.discard_observer(self, name='VideoDeviceDidChangeCamera')
        if self.sharing_screen and self.device_before_screen is not None:
            self.stopSharingScreen()        # the room is over: the camera is the device again
        if 'ConferenceInfoPanel' in sys.modules:
            sys.modules['ConferenceInfoPanel'].close_conference_info(self)      # it closes with the room's window
        self.localTile.teardown()
        for tile in self.tiles.values():
            tile.teardown()
        _windows.pop(self.session, None)

    @objc.python_method
    def endRoomChat(self):
        """The MSRP chat to the room that was opened for this video room ends with it, or the room would keep
        us as a participant; a chat that was there before (we escalated to video) goes on. Found by the
        room's MSRP address: once the video room has ended WebRTCSupport.room_chat_session() no longer finds it."""
        from MediaStream import STATE_FAILED, STATE_FINISHED
        room = split_uri(self.session.sip_room)
        try:
            controllers = list(NSApp.delegate().contactsWindowController.sessionControllersManager.sessionControllers)
        except AttributeError:
            return
        for controller in controllers:
            if not getattr(controller, 'opened_for_videoroom', False) or controller.target_uri is None:
                continue
            if getattr(controller, 'finished', False) or controller.state in (STATE_FAILED, STATE_FINISHED):
                continue
            if split_uri(controller.target_uri) != room:
                continue
            controller.opened_for_videoroom = False
            BlinkLogger().log_info('[videoroom] %s: the video room is over, the chat opened with it ends too' % self.session.room)
            controller.end()

    # The call bar and the carousel fade out when the mouse rests

    @objc.python_method
    def showControls(self):
        if self.callBar.alphaValue() < 1.0:
            NSAnimationContext.beginGrouping()
            NSAnimationContext.currentContext().setDuration_(0.2)
            self.callBar.animator().setAlphaValue_(1.0)
            self.thumbnailButton.animator().setAlphaValue_(1.0)
            NSAnimationContext.endGrouping()
        if not self.carousel.shown or (self.carousel.alphaValue() < 1.0 and self.carousel.tiles):
            self.carousel.fade(True)
        if self.idle_timer is not None:
            self.idle_timer.invalidate()
        self.idle_timer = _timer(CONTROLS_IDLE_TIME, self.hideControls)

    @objc.python_method
    def hideControls(self):
        self.idle_timer = None
        if self.session.state == 'terminated' or self.closing:
            return
        location = self.contentView.convertPoint_fromView_(self.window.mouseLocationOutsideOfEventStream(), None)
        if _contains(self.callBar.frame(), location) or (not self.carousel.isHidden() and _contains(self.carousel.frame(), location)):
            self.idle_timer = _timer(CONTROLS_IDLE_TIME, self.hideControls)
            return
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.4)
        self.callBar.animator().setAlphaValue_(0.0)
        self.thumbnailButton.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()
        self.carousel.fade(False)       # hidden with the call bar
        self.setPicked(None)

    @objc.python_method
    def toggleFullScreen(self):
        self.window.toggleFullScreen_(None)

    @objc.python_method
    def isFullScreen(self):
        return bool(self.window.styleMask() & NSWindowStyleMaskFullScreen)

    # The feeds' video is received only while the window can be seen (any part of it on screen: not
    # minimized, not on another Space, not entirely covered); audio goes on regardless. Pausing waits a
    # moment so that a window passing behind another for an instant does not stop and restart the video.

    def windowDidChangeOcclusionState_(self, notification):
        if self.closing:
            return
        visible = bool(self.window.occlusionState() & NSWindowOcclusionStateVisible)
        if self.pause_timer is not None:
            self.pause_timer.invalidate()
            self.pause_timer = None
        if visible:
            self.session.set_video_paused(False)
        else:
            self.pause_timer = _timer(VIDEO_PAUSE_DELAY, self._pauseVideo)

    @objc.python_method
    def _pauseVideo(self):
        self.pause_timer = None
        if not self.closing and not (self.window.occlusionState() & NSWindowOcclusionStateVisible):
            self.session.set_video_paused(True)

    def windowDidEnterFullScreen_(self, notification):
        self.fullScreenButton.configure('arrow.down.right.and.arrow.up.left', _t("Window"))

    def windowDidExitFullScreen_(self, notification):
        self.fullScreenButton.configure('arrow.up.left.and.arrow.down.right', _t("Full screen"))
        if self.minimize_after_full_screen:
            self.minimize_after_full_screen = False
            self.window.miniaturize_(None)

    def windowDidDeminiaturize_(self, notification):
        if self.full_screen_after_deminiaturize:
            self.full_screen_after_deminiaturize = False
            if not self.isFullScreen():
                self.toggleFullScreen()

    # Layout

    @objc.python_method
    def contentResized(self):
        self.callBar.tile()
        self.layoutTiles()

    @objc.python_method
    def labelAvoid(self, tile):
        """What a tile's name pill must not be under, in the tile's coordinates: the call bar, and our
        floating thumbnail; a pill under one is moved above it."""
        content = self.contentView
        if content is None or tile.superview() is not content:
            return []
        rects = []
        bar = self.callBar.frame()
        rects.append(tile.convertRect_fromView_(NSMakeRect(bar.origin.x - 4, bar.origin.y - NAME_PILL_MARGIN, bar.size.width + 8, bar.size.height + NAME_PILL_MARGIN), content))
        local = self.localTile
        if self.floating and local is not tile and local.superview() is content:
            frame = local.frame()
            rects.append(tile.convertRect_fromView_(NSMakeRect(frame.origin.x - 4, frame.origin.y - NAME_PILL_MARGIN, frame.size.width + 8, frame.size.height + NAME_PILL_MARGIN + 4), content))
        return rects

    @objc.python_method
    def _putInContent(self, tile, top=False):
        """In the content view, under the carousel; top: right under it, above the other tiles (our thumbnail)."""
        content = self.contentView
        if tile.superview() is content:
            if not top:
                return
            subviews = list(content.subviews())
            index = subviews.index(tile)
            if index + 1 < len(subviews) and subviews[index + 1] is self.carousel:
                return
        tile.removeFromSuperview()
        content.addSubview_positioned_relativeTo_(tile, NSWindowBelow, self.carousel)
        tile.setToolTip_(None)

    @objc.python_method
    def layoutTiles(self):
        if self.contentView is None or self.localTile is None or self.closing:
            return
        local = self.localTile
        # hidden at our request only while there is someone else's video to show instead
        others_with_video = any(tile is not local and tile.in_grid() for tile in self.order)
        hidden = local if (self.hide_local or (self.self_hidden and others_with_video)) else None
        tiles = [tile for tile in self.order if tile is not hidden]
        speakers = [tile for tile in self.speaker_tiles if tile in tiles]
        others = [tile for tile in tiles if tile not in speakers]
        video = [tile for tile in others if tile.in_grid()]
        audio = [tile for tile in others if not tile.in_grid()]
        mine = local if local in tiles else None
        floating = False
        if speakers:
            shown, overflow = speakers, video + audio      # the speakers fill the window
        else:
            # our own video floats when that leaves an even grid of the others (3 of us: 2x1 and the
            # thumbnail, 5: 2x2 and the thumbnail, ...), or when the others fill the grid
            remotes = [tile for tile in video if tile is not mine]
            people = len(remotes) + (1 if mine is not None else 0)
            if self.local_placement == 'floating':
                floating = mine is not None and len(remotes) >= 1     # with one other: them full, us the thumbnail
            elif self.local_placement == 'grid':
                floating = mine is not None and len(remotes) >= GRID_MAX
            else:
                floating = mine is not None and ((people >= 3 and people % 2 == 1) or len(remotes) >= GRID_MAX)
            if floating:
                shown, overflow = remotes[:GRID_MAX], remotes[GRID_MAX:] + audio
            else:
                shown, overflow = video[:GRID_MAX], video[GRID_MAX:] + audio
        self.floating = floating
        local.floating = floating
        # the choice only means something with others in the grid (and not with the room's speakers)
        local.thumbnail_enabled = not speakers and len([tile for tile in video if tile is not mine]) >= 1
        for tile in self.order:
            if tile not in shown and tile not in overflow and not (floating and tile is local):
                tile.removeFromSuperview()      # our own, hidden
        for tile in shown:
            self._putInContent(tile)
        if floating:
            self._putInContent(local, top=True)
        self.carousel.setTiles(overflow)
        if self.picked is not None and self.picked not in overflow:
            self.setPicked(None)
        bounds = self.contentView.bounds()
        count = len(shown)
        if count:
            width, height = bounds.size.width - 2 * TILE_GAP, bounds.size.height - 2 * TILE_GAP
            aspect = width / max(1.0, height)
            if count == 1:
                columns, rows = 1, 1
            elif count == 2:
                # side by side, or one over the other in a portrait window
                columns, rows = (1, 2) if aspect <= TWO_IN_A_COLUMN_ASPECT else (2, 1)
            elif count == 3:
                # 3x1 in a wide window, 1x3 in a tall (portrait) one, 2 over 1 otherwise
                if aspect >= THREE_IN_A_ROW_ASPECT:
                    columns, rows = 3, 1
                elif aspect <= THREE_IN_A_COLUMN_ASPECT:
                    columns, rows = 1, 3
                else:
                    columns, rows = 2, 2
            else:
                columns, rows = int(ceil(count / 2.0)), 2      # 2x2, 3+2, 3x2, 4+3, 4x2 (a shorter bottom row centred)
            cell_w = (width - (columns - 1) * TILE_GAP) / columns
            cell_h = (height - (rows - 1) * TILE_GAP) / rows
            # tiles take the whole cell: the pictures are cropped to fill them
            for index, tile in enumerate(shown):
                row, column = divmod(index, columns)
                in_row = min(columns, count - row * columns)
                offset = (width - in_row * cell_w - (in_row - 1) * TILE_GAP) / 2      # a last, shorter row is centred
                x = TILE_GAP + offset + column * (cell_w + TILE_GAP)
                y = TILE_GAP + row * (cell_h + TILE_GAP)
                # the names at the outer corners: top for the top row, bottom for the others, left or right
                # by side (a middle column centred); one under the call bar is lifted above it when drawn
                vertical = 'top' if row == 0 else 'bottom'
                if in_row % 2 and column == in_row // 2 and in_row > 1:
                    horizontal = 'center'
                else:
                    horizontal = 'left' if column < in_row / 2.0 else 'right'
                tile.label_corner = (vertical, horizontal)
                tile.setFrame_(NSMakeRect(floor(x), floor(y), floor(cell_w), floor(cell_h)))
                tile.redraw()
        if floating:
            local.label_corner = ('bottom', 'left')
            local.setFrame_(self.floatingRect())
            local.redraw()
        self.updateRoomButtons()
        self.placeStatus()

    @objc.python_method
    def floatingRect(self, corner=None):
        vertical, horizontal = corner or self.floating_corner
        bounds = self.contentView.bounds()
        width = max(FLOATING_MIN_W, min(FLOATING_MAX_W, floor(bounds.size.width * 0.2)))
        height = floor(width * 9 / 16)
        x = FLOATING_MARGIN if horizontal == 'left' else bounds.size.width - FLOATING_MARGIN - width
        y = FLOATING_MARGIN if vertical == 'top' else bounds.size.height - FLOATING_MARGIN - height
        return NSMakeRect(x, y, width, height)

    @objc.python_method
    def snapFloating(self):
        """After a drag: the thumbnail goes to the corner nearest to where it was let go."""
        frame = self.localTile.frame()
        bounds = self.contentView.bounds()
        center_x, center_y = frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height / 2
        self.floating_corner = ('top' if center_y < bounds.size.height / 2 else 'bottom', 'left' if center_x < bounds.size.width / 2 else 'right')
        self.localTile.setFrame_(self.floatingRect())
        for tile in self.order:
            tile.overlay.setNeedsDisplay_(True)     # name pills that went around it
        self.placeThumbnailButton()

    @objc.python_method
    def placeThumbnailButton(self):
        """The button on our own tile: where its corner is clear of the name pill and the hand badge."""
        local = self.localTile
        show = bool(local.thumbnail_enabled and local.superview() is self.contentView and not local.compact())
        self.thumbnailButton.setHidden_(not show)
        if show:
            self.thumbnailButton.setFrame_(self.contentView.convertRect_fromView_(local.thumbnailButtonRect(), local))
            self.thumbnailButton.setFloating(self.floating)

    def userClickedThumbnailButton_(self, sender):
        self.toggleLocalPlacement()
        self.showControls()

    @objc.python_method
    def aorCount(self):
        """How many people are in the room: distinct addresses (devices of one person count once), us included."""
        aors = {split_uri(self.session.identity)}
        aors.update(split_uri(participant.uri) for participant in self.session.participants.values() if participant.visible)
        return len(aors)

    @objc.python_method
    def toggleLocalPlacement(self):
        self.local_placement = 'grid' if self.floating else 'floating'
        self.layoutTiles()

    @objc.python_method
    def canHideSelf(self):
        return any(tile is not self.localTile and tile.in_grid() for tile in self.order)

    @objc.python_method
    def setSelfHidden(self, hidden):
        self.self_hidden = hidden
        self.layoutTiles()
        self.flash(_t("Your video is hidden here; the others still see you") if hidden else _t("Your video is back"))

    # The mouse

    @objc.python_method
    def tileAtPoint(self, point):
        """The tile under point (content coordinates): the carousel's, our floating thumbnail, the grid's."""
        tile = self.carousel.tileAtPoint(point, self.contentView)
        if tile is not None:
            return tile
        if _contains(self.callBar.frame(), point) and self.callBar.alphaValue() > 0.05:
            return None
        local = self.localTile
        if self.floating and local.superview() is self.contentView and _contains(local.frame(), point):
            return local
        return next((tile for tile in self.order if tile.superview() is self.contentView and _contains(tile.frame(), point)), None)

    @objc.python_method
    def setHovered(self, tile, point):
        if tile is self.hovered:
            return
        previous, self.hovered = self.hovered, tile
        if previous is not None:
            previous.hovering = False
            previous.overlay.setNeedsDisplay_(True)
        if tile is not None:
            tile.hovering = True
            tile.overlay.setNeedsDisplay_(True)

    @objc.python_method
    def mouseMoved(self, point):
        self.showControls()
        self.setHovered(self.tileAtPoint(point), point)

    @objc.python_method
    def mouseDown(self, point, event):
        self.showControls()
        tile = self.tileAtPoint(point)
        if event.clickCount() == 2:
            if self.picked is None and tile is not self.localTile:
                self.toggleFullScreen()
            return
        if tile is self.localTile and self.floating:
            frame = tile.frame()
            self.drag = (point.x - frame.origin.x, point.y - frame.origin.y)
            return
        if tile is not None and tile in self.carousel.tiles:
            if self.speaker_tiles:
                return              # the speakers are the room's: changed with Speakers, not here
            if tile.in_grid():      # audio only participants cannot go into the grid
                self.setPicked(None if tile is self.picked else tile)
            return
        if self.picked is not None and tile is not None and tile.participant is not None and tile.superview() is self.contentView:
            picked = self.picked
            self.setPicked(None)
            picked.pinned = True        # the user wants it there: video loss does not take it out
            tile.pinned = False
            a, b = self.order.index(picked), self.order.index(tile)
            self.order[a], self.order[b] = self.order[b], self.order[a]
            self.layoutTiles()
            return
        if self.picked is not None:
            self.setPicked(None)        # a click anywhere else, our own tile included, cancels

    @objc.python_method
    def mouseDragged(self, point):
        if self.drag is None:
            return
        tile = self.localTile
        bounds = self.contentView.bounds()
        frame = tile.frame()
        x = max(0.0, min(point.x - self.drag[0], bounds.size.width - frame.size.width))
        y = max(0.0, min(point.y - self.drag[1], bounds.size.height - frame.size.height))
        tile.setFrameOrigin_(NSMakePoint(x, y))
        self.placeThumbnailButton()

    @objc.python_method
    def mouseUp(self, point):
        if self.drag is not None:
            self.drag = None
            self.snapFloating()

    @objc.python_method
    def scrollWheel(self, point, event):
        if not self.carousel.isHidden() and _contains(self.carousel.frame(), point):
            delta = event.scrollingDeltaX() or event.scrollingDeltaY()
            if not event.hasPreciseScrollingDeltas():
                delta *= 10
            self.carousel.scrollBy(-delta)

    @objc.python_method
    def escape(self):
        if self.picked is not None:
            self.setPicked(None)
            return True
        if self.isFullScreen():
            self.toggleFullScreen()
            return True
        return False

    @objc.python_method
    def setPicked(self, tile):
        """Pick a carousel tile; the remote tiles in the grid then show they can be replaced by it."""
        if tile is not None and tile not in self.carousel.tiles:
            tile = None
        self.picked = tile
        for candidate in self.order:
            if candidate.participant is not None:
                in_grid = candidate.superview() is self.contentView
                candidate.set_pick_state(picked=candidate is tile, replaceable=tile is not None and in_grid)

    @objc.python_method
    def rightMouseDown(self, point, event):
        tile = self.tileAtPoint(point)
        if tile is not None:
            NSMenu.popUpContextMenu_withEvent_forView_(self.tileMenu(tile), event, self.contentView)

    # Menus

    @objc.python_method
    def _menu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        return menu

    @objc.python_method
    def _item(self, menu, title, function=None, enabled=True, state=None, tooltip=None):
        item = menu.addItemWithTitle_action_keyEquivalent_(title, 'fire:' if function is not None else None, '')
        if function is not None:
            item.setTarget_(self.menuTarget)
            item.setRepresentedObject_(function)
        item.setEnabled_(bool(enabled) and function is not None)
        if state is not None:
            item.setState_(NSOnState if state else NSOffState)
        if tooltip:
            item.setToolTip_(tooltip)
        return item

    @objc.python_method
    def _popUpOverSegment(self, menu, segment):
        height = menu.size().height
        point = NSMakePoint(0.0, -height - 4.0) if segment.isFlipped() else NSMakePoint(0.0, segment.bounds().size.height + height + 4.0)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, point, segment)

    @objc.python_method
    def tileMenu(self, tile):
        session = self.session
        menu = self._menu()
        speakers = list(session.speakers)
        established = session.state == 'established'
        aors = self.aorCount()
        room_controls = aors >= ROOM_CONTROLS_AORS          # hands and muting
        speaker_controls = aors >= SPEAKER_CONTROLS_AORS    # promoting speakers
        if tile.participant is None:
            id = session.id
            if tile.thumbnail_enabled:
                self._item(menu, _t("Put me back in the grid") if self.floating else _t("Show me as a thumbnail"), self.toggleLocalPlacement)
            if established and self.canHideSelf():
                self._item(menu, _t("Hide myself"), lambda: self.setSelfHidden(True), tooltip=_t("Your own video goes from this window; the others still see you"))
            if room_controls or tile.hand:      # a hand still up can always be lowered
                menu.addItem_(NSMenuItem.separatorItem())
                self._item(menu, _t("Lower hand") if tile.hand else _t("Raise hand"), lambda: session.toggle_hand(), enabled=established)
            can_speak = True
        elif tile.participant.extra_video is not None:
            # one of our extra videos: what it is sent at, stopping it
            participant = tile.participant
            extra = participant.extra_video
            id = participant.id
            self._item(menu, participant.name)
            menu.addItem_(NSMenuItem.separatorItem())
            from ExtraVideo import RESOLUTIONS
            sizes = self._menu()
            for name, width, height, bitrate in RESOLUTIONS:
                self._item(sizes, _t("%dx%d, about %d kbit/s") % (width, height, bitrate), lambda name=name: extra.set_resolution(name), state=extra.resolution == name)
            menu.addItemWithTitle_action_keyEquivalent_(_t("Send at"), None, '').setSubmenu_(sizes)
            self._item(menu, _t("Stop this video"), lambda: extra.stop())
            can_speak = extra.state == 'established'
        else:
            participant = tile.participant
            id = participant.id
            self._item(menu, participant.name)
            own_controls = not participant.parent      # someone's extra video has no microphone nor hand of its own
            if (room_controls and own_controls) or tile.hand:
                menu.addItem_(NSMenuItem.separatorItem())
            if room_controls and own_controls:
                self._item(menu, _t("Unmute") if participant.muted else _t("Mute"), lambda: session.mute_participant(participant, not participant.muted), enabled=established)
            if tile.hand:
                self._item(menu, _t("Lower hand"), lambda: session.toggle_hand(id), enabled=established)
            can_speak = participant.video is True
        if can_speak and (speaker_controls or id in speakers):     # speakers still set can always be cleared
            menu.addItem_(NSMenuItem.separatorItem())
            if id in speakers:
                self._item(menu, _t("Stop showing as speaker"), lambda: session.set_speakers([s for s in speakers if s != id]), enabled=established)
            else:
                self._item(menu, _t("Show as the speaker"), lambda: session.set_speakers([id]), enabled=established)
                if len(speakers) == 1:
                    self._item(menu, _t("Show as second speaker"), lambda: session.set_speakers(speakers + [id]), enabled=established)
        if tile.participant is not None and tile.participant.type == 'sip':
            # whatever the number of people (a tile of someone else means we are not alone)
            menu.addItem_(NSMenuItem.separatorItem())
            uri = tile.participant.uri
            self._item(menu, _t("Remove from the conference"), lambda: session.remove_participants([uri]), enabled=established)
        return menu

    # Status

    @objc.python_method
    def setStatus(self, text):
        self.statusLabel.setStringValue_(text or '')
        self.statusLabel.setHidden_(not text)
        self.placeStatus()

    @objc.python_method
    def flash(self, text, duration=FLASH_TIME):
        """A note in the status pill for a few seconds (the window's title has the rest)."""
        self.flash_until = time.monotonic() + duration
        self.setStatus(text)
        _timer(duration + 0.05, self.updateTitle)

    @objc.python_method
    def placeStatus(self):
        if self.statusLabel is None or self.contentView is None:
            return
        size = self.statusLabel.intrinsicContentSize()
        width, height = ceil(size.width) + 28.0, 22.0
        bounds = self.contentView.bounds()
        above = self.carousel.frame().origin.y if not self.carousel.isHidden() else self.callBar.frame().origin.y
        self.statusLabel.setFrame_(NSMakeRect(floor((bounds.size.width - width) / 2), above - 10.0 - height, width, height))

    @objc.python_method
    def updateTitle(self):
        if self.session.state != 'established' or self.closing:
            return
        people = sum(1 for participant in self.session.participants.values() if participant.visible) + 1
        parts = [self.title, (_t("%d participant") if people == 1 else _t("%d participants")) % people]
        if self.joined_at is not None:
            elapsed = int(time.monotonic() - self.joined_at) + (self.session.duration or 0)
            hours, rest = divmod(elapsed, 3600)
            minutes, seconds = divmod(rest, 60)
            parts.append('%d:%02d:%02d' % (hours, minutes, seconds) if hours else '%02d:%02d' % (minutes, seconds))
        self.window.setTitle_('  ·  '.join(parts))
        if time.monotonic() >= self.flash_until and not self.statusLabel.isHidden():
            self.setStatus(None)     # once connected the title says it; the pill is for connecting, ending and notes

    @objc.python_method
    def tick(self):
        self.updateTitle()
        now = time.monotonic()
        for tile in list(self.tiles.values()):
            if self.session.video_paused:
                tile._sample = None         # no video on purpose: not a loss, and a new warm-up after
                continue
            tile.check_video(now)

    # Controls

    def userClickedMuteButton_(self, sender):
        self.session.set_audio_muted(not self.session.audio_muted)

    def userClickedCameraButton_(self, sender):
        """The video input: each camera, the screen (one entry per display), and turning the camera off
        or on, as the Devices menu has them."""
        from sipsimple.configuration.settings import SIPSimpleSettings
        devices = self.videoDevices()
        current = SIPSimpleSettings().video.device
        sharing = self.sharing_screen
        off = self.session.video_muted
        menu = self._menu()
        cameras = [('system_default', _t("System Default"))] + sorted(((device, device) for device in devices['cameras']), key=lambda item: item[1].lower())
        for device, label in cameras:
            self._item(menu, label, lambda device=device: self.useCamera(device), state=not off and not sharing and current == device)
        screens = devices['screens']
        if screens and (self.session.state == 'established' or sharing):      # sharing needs the room connected
            menu.addItem_(NSMenuItem.separatorItem())
            for device in screens:
                self._item(menu, _t("Share screen") if len(screens) == 1 else _t("Share %s") % device, lambda device=device: self.shareScreen(device),
                           state=not off and sharing and (len(screens) == 1 or current == device))
        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, _t("Turn camera on") if off else _t("Turn camera off"), self.toggleCamera)
        self._popUpOverSegment(menu, sender)

    @objc.python_method
    def videoDevices(self):
        """{'cameras': [...], 'screens': [...]}, as the Devices menu lists them, the screens by display."""
        try:
            from ScreenPointer import is_screen_device, screen_index
        except ImportError:
            is_screen_device, screen_index = (lambda device: False), (lambda device: 0)
        try:
            devices = [device for device in NSApp.delegate().video_devices if device not in (None, 'system_default')]
        except Exception:
            devices = []
        return dict(cameras=[device for device in devices if not is_screen_device(device)],
                    screens=sorted((device for device in devices if is_screen_device(device)), key=lambda device: screen_index(device) or 0))

    @objc.python_method
    def toggleCamera(self):
        self.session.set_video_muted(not self.session.video_muted)

    @objc.python_method
    def useCamera(self, device):
        from ScreenPointerController import select_video_device
        BlinkLogger().log_info('[videoroom] %s: camera %s' % (self.session.room, device))
        self.device_before_screen = None
        select_video_device(device)
        if self.session.video_muted:
            self.toggleCamera()

    @objc.python_method
    def shareScreen(self, device=None):
        """The screen is a video device ("My screen"), sent instead of the camera."""
        from sipsimple.configuration.settings import SIPSimpleSettings
        from ScreenPointerController import select_video_device
        if self.session.state != 'established':
            return
        screens = self.videoDevices()['screens']
        device = device or (screens[0] if screens else None)
        if device is None:
            self.flash(_t("The screen cannot be shared"))
            return
        if self.session.video_muted:
            self.toggleCamera()      # the screen goes where the camera would
        if not self.sharing_screen:
            self.device_before_screen = SIPSimpleSettings().video.device or 'system_default'
        BlinkLogger().log_info('[screen sharing] %s: sharing %s' % (self.session.room, device))
        select_video_device(device)

    @objc.python_method
    def stopSharingScreen(self):
        from ScreenPointerController import select_video_device
        device, self.device_before_screen = self.device_before_screen or 'system_default', None
        BlinkLogger().log_info('[screen sharing] %s: sharing stopped, the camera %s is back' % (self.session.room, device))
        select_video_device(device)

    def userClickedStopScreenButton_(self, sender):
        self.stopSharingScreen()

    @objc.python_method
    def updateScreenState(self, initial=False):
        """The video device changed: the screen shared (our tile hidden, the window out of the way, us the
        single speaker when nobody was chosen) or the camera back (all as it was), however it stopped."""
        screen = _sending_screen()
        self.localTile.refresh()
        if screen == self.sharing_screen and not initial:
            return
        self.sharing_screen = screen
        self.stopScreenButton.setHidden_(not screen)
        self.hide_local = screen
        self.layoutTiles()
        if initial:
            return
        self.flash(_t("Sharing your screen") if screen else _t("Screen sharing stopped"))
        # with nobody chosen as speaker, the screen is what everyone should look at: we are the speaker
        # while it is shared, and the grid comes back after (unless someone changed it)
        if screen and not self.session.speakers:
            self.speaker_for_screen = True
            self.auto_issued = []
            self.updateAutoSpeakers()
        elif not screen and self.speaker_for_screen:
            self.speaker_for_screen = False
            if self.session.speakers and list(self.session.speakers) in self.auto_issued:
                self.session.set_speakers([])
        if screen:
            # out of the way of what is shared; back as it was when the sharing ends
            self.minimized_for_screen = not self.window.isMiniaturized()
            self.full_screen_before = self.isFullScreen()
            if self.full_screen_before:
                self.minimize_after_full_screen = True
                self.toggleFullScreen()
            elif self.minimized_for_screen:
                self.window.miniaturize_(None)
        else:
            if self.device_before_screen is not None:
                self.device_before_screen = None    # stopped elsewhere (the Devices menu, the system)
            if self.minimized_for_screen:
                self.minimized_for_screen = False
                if self.window.isMiniaturized():
                    self.full_screen_after_deminiaturize = self.full_screen_before
                    self.window.deminiaturize_(None)
                    self.window.makeKeyAndOrderFront_(None)

    def userClickedSpeakersButton_(self, sender):
        candidates = [(self.session.id, _t("Me"))]
        candidates += [(participant.id, participant.name) for participant in self.tiles if participant.video is True and participant.visible]
        self.speakerPanel = SpeakerSelectionPanel.alloc().init()
        self.speakerPanel.run(self.window, candidates, list(self.session.speakers), self.session.set_speakers)

    def userClickedHandButton_(self, sender):
        self.session.toggle_hand()

    def userClickedMoreButton_(self, sender):
        session = self.session
        established = session.state == 'established'
        menu = self._menu()
        if self.sharing_screen:
            self._item(menu, _t("Stop sharing screen"), self.stopSharingScreen)
        elif established:      # sharing needs the room connected
            self._item(menu, _t("Share screen"), lambda: self.shareScreen(), enabled=bool(self.videoDevices()['screens']),
                       tooltip=_t("Send the screen instead of the camera"))
        menu.addItem_(NSMenuItem.separatorItem())
        if self.self_hidden:
            self._item(menu, _t("Show myself"), lambda: self.setSelfHidden(False))
        elif established and self.canHideSelf():
            self._item(menu, _t("Hide myself"), lambda: self.setSelfHidden(True), tooltip=_t("Your own video goes from this window; the others still see you"))
        if not self.self_hidden and self.localTile.thumbnail_enabled:
            self._item(menu, _t("Put me back in the grid") if self.floating else _t("Show me as a thumbnail"), self.toggleLocalPlacement)
        if established and session.multiple_video_feeds:
            self._addVideoMenu(menu)
        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, _t("Invite participants…"), self.inviteParticipants)
        self._item(menu, _t("Conference info…"), self.showInfo, tooltip=_t("Every participant with what comes from them and what we send"))
        from ConferenceShareDialog import share_available
        if share_available(session.sip_room, session.account, session.target):
            self._item(menu, _t("Share conference address…"), self.shareAddress, tooltip=_t("The addresses others can join this conference at"))
        menu.addItem_(NSMenuItem.separatorItem())
        if self.aorCount() >= ROOM_CONTROLS_AORS:
            self._item(menu, _t("Mute everyone"), session.mute_everyone, enabled=established, tooltip=_t("Everyone else's microphone is muted"))
        hands = [id for id in session.raised_hands if id != session.id]
        if hands:
            self._item(menu, _t("Lower all hands"), lambda: [session.toggle_hand(id) for id in hands], enabled=established)
        self._popUpOverSegment(menu, sender)

    # Our extra videos (ExtraVideo.py): a second camera of this computer, an RTSP camera

    @objc.python_method
    def _addVideoMenu(self, menu):
        from ExtraVideo import MAX_EXTRA_VIDEOS
        full = len(self.session.extra_videos) >= MAX_EXTRA_VIDEOS
        self._item(menu, _t("Add video…"), self.addVideo, enabled=not full,
                   tooltip=_t("At most %d videos besides your own") % MAX_EXTRA_VIDEOS if full else _t("Send another camera too, shown under your name"))

    @objc.python_method
    def addVideo(self):
        """More -> Add video…: a camera of this computer (not the one our own video sends) or a network camera."""
        from CameraSetup import AddVideoPanel
        number = len(self.session.extra_videos) + 1
        answer = AddVideoPanel.alloc().initWithLabel_(_t("Camera %d") % number).run()
        if answer is None or self.session.state != 'established':
            return
        source, label = answer
        key = source.get('unique_id') or source.get('url')
        if any((extra.source.get('unique_id') or extra.source.get('url')) == key for extra in self.session.extra_videos.values()):
            self.flash(_t("%s is sent already") % label)
            return
        self.startExtraVideo(source, label)

    @objc.python_method
    def startExtraVideo(self, source, label):
        from ExtraVideo import ExtraVideo
        extra = ExtraVideo(self.session, source, label)
        try:
            extra.start()
        except Exception as e:
            BlinkLogger().log_warning('[videoroom] %s: cannot add %s: %s' % (self.session.room, label, e))
            self.flash('%s: %s' % (label, e), duration=5.0)
            extra.stop(str(e))
            return None
        return extra

    @objc.python_method
    def startPendingVideos(self, waited=0.0):
        """What Join Conference chose besides our camera, once the room is joined: the screen to share, and our
        other videos. The cameras of this computer wait for the screen to take the SDK's camera's place (it is
        free then), at most PENDING_CAMERA_WAIT."""
        session = self.session
        if self.closing or session.state != 'established':
            return
        screen, session.pending_screen = session.pending_screen, None
        if screen:
            self.screen_pending = True
            self.shareScreen(screen)
        sources = list(session.pending_extra_sources or [])
        if not sources:
            return
        if not session.multiple_video_feeds:
            session.pending_extra_sources = []
            BlinkLogger().log_warning('[videoroom] %s: the server takes one video per participant, not publishing %s' % (session.room, ', '.join(label for source, label in sources)))
            self.flash(_t("This server takes one video per participant"), duration=5.0)
            return
        if getattr(self, 'screen_pending', False) and waited < PENDING_CAMERA_WAIT and not _sending_screen() and \
                any(source.get('kind') == 'camera' for source, label in sources):
            _timer(0.5, lambda: self.startPendingVideos(waited + 0.5))
            return
        self.screen_pending = False
        session.pending_extra_sources = []
        for source, label in sources:
            self.startExtraVideo(source, label)

    @objc.python_method
    def updateAutoSpeakers(self):
        """While our screen is shared with nobody chosen as speaker: the screen is the speaker, and with one
        more video of ours both are; with more than two videos of ours, nobody is made speaker. Followed as
        our extra videos come and go, until someone chooses the speakers."""
        if not self.speaker_for_screen or self.session.state != 'established':
            return
        current = list(self.session.speakers)
        if current and current not in self.auto_issued:
            self.speaker_for_screen = False     # chosen by someone: left alone from now on
            return
        extras = [extra.id for extra in self.session.extra_videos.values() if extra.state == 'established']
        feeds = [self.session.id] + extras
        wanted = feeds if len(feeds) <= 2 else []
        self.auto_issued.append(wanted)
        if wanted != current:
            self.session.set_speakers(wanted)

    @objc.python_method
    def showInfo(self):
        from ConferenceInfoPanel import show_conference_info
        show_conference_info(self)

    @objc.python_method
    def shareAddress(self):
        from ConferenceShareDialog import ConferenceShareDialog
        self.shareDialog = ConferenceShareDialog.alloc().initWithRoom_account_target_title_(self.session.sip_room, self.session.account,
                                                                                            self.session.target, self.title)
        self.shareDialog.runSheetForWindow(self.window)
        self.showControls()

    def userClickedChatButton_(self, sender):
        """The room's chat in the chat window in front, started if there is none (it ended, or was never there):
        MSRP only, the room's audio and video stay here, over WebRTC."""
        from WebRTCSupport import room_chat_session
        delegate = NSApp.delegate()
        controller = room_chat_session(self.session)
        chat_window = getattr(delegate, 'chatWindowController', None)
        if controller is not None and chat_window is not None and chat_window.hasSession_(controller):
            chat_window.selectSession_(controller)
            if chat_window.window().isMiniaturized():
                chat_window.window().deminiaturize_(None)
            chat_window.window().makeKeyAndOrderFront_(None)
            return
        BlinkLogger().log_info('[videoroom] %s: chat over MSRP to %s' % (self.session.room, self.session.sip_room))
        account = self.session.account
        controller = delegate.contactsWindowController.joinConference(self.session.sip_room, 'chat', nickname=account.conference.nickname)
        if controller is not None:
            controller.opened_for_videoroom = True      # leaving the video room ends it too

    def userClickedFullScreenButton_(self, sender):
        self.toggleFullScreen()

    def userClickedHangupButton_(self, sender):
        self.close()

    @objc.python_method
    def updateMuteButton(self):
        muted = self.session.audio_muted
        self.muteButton.configure('mic.slash.fill' if muted else 'mic.fill', _t("Unmute") if muted else _t("Mute"),
                                  tint=NSColor.systemRedColor() if muted else None)
        self.localTile.refresh()
        self.placeThumbnailButton()     # the name pill is wider with the muted microphone

    @objc.python_method
    def updateCameraButton(self):
        off = self.session.video_muted
        self.cameraButton.configure('video.slash.fill' if off else 'video.fill', _t("Camera off") if off else _t("Camera"),
                                    tint=NSColor.systemRedColor() if off else None)
        self.localTile.refresh()

    @objc.python_method
    def updateRoomButtons(self):
        aors = self.aorCount()
        # speakers with four people or more (or speakers still set, to clear them)
        self.speakersButton.setHidden_(not (aors >= SPEAKER_CONTROLS_AORS or bool(self.session.speakers)))
        # raising a hand with three people or more (or to lower one still up)
        self.handButton.setHidden_(not (aors >= ROOM_CONTROLS_AORS or self.session.id in self.session.raised_hands))
        if not self.carousel.isHidden():
            self.carousel.tile()
        self.placeThumbnailButton()

    # Inviting (the server calls them: videoroom-invite, then invite-status as it goes)

    @objc.python_method
    def inviteParticipants(self):
        account = self.session.account
        try:
            uris = NSApp.delegate().contactsWindowController.showAddParticipantsWindow(target=self.title, default_domain=account.id.domain)
        except Exception as e:
            BlinkLogger().log_error('[videoroom] cannot pick participants: %s' % e)
            return
        if uris:
            self.inviteUris(list(uris))

    @objc.python_method
    def inviteUris(self, uris):
        domain = self.session.account.id.domain
        names = []
        clean = []
        for uri in uris:
            uri = str(uri or '').strip()
            if uri.lower().startswith(('sip:', 'sips:')):
                uri = uri.partition(':')[2]
            if not uri:
                continue
            if '@' not in uri:
                uri = '%s@%s' % (uri, domain)
            contact = None
            try:
                contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(uri)
            except Exception:
                pass
            name = str(contact.name) if contact is not None and getattr(contact, 'name', None) else uri
            self.invite_names[uri] = name
            names.append(name)
            clean.append(uri)
        if not clean:
            return
        self.session.invite(clean)
        self.flash(_t("Inviting %s") % ', '.join(names))

    # Participants

    @objc.python_method
    def addParticipant(self, participant):
        if not participant.visible or participant in self.tiles:
            return
        tile = VideoroomTileView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 180))
        tile.setup(self, participant)
        tile.hand = participant.id in self.session.raised_hands
        self.tiles[participant] = tile
        self.order.append(tile)
        if self.session.speakers:
            self.applySpeakers(layout=False)    # a speaker the room named before we knew them
        self.layoutTiles()
        self.updateTitle()

    @objc.python_method
    def removeParticipant(self, participant):
        tile = self.tiles.pop(participant, None)
        if tile is not None:
            if tile is self.picked:
                self.setPicked(None)
            if tile is self.hovered:
                self.hovered = None
            tile.teardown()
            tile.removeFromSuperview()
            if tile in self.order:
                self.order.remove(tile)
            self.speaker_tiles = [speaker for speaker in self.speaker_tiles if speaker is not tile]
            self.layoutTiles()
        self.updateTitle()

    @objc.python_method
    def applySpeakers(self, layout=True):
        tiles = []
        for id in self.session.speakers:
            if id == self.session.id:
                tiles.append(self.localTile)
            else:
                # someone's video, or one of our extra videos (not among the room's participants: we publish it)
                extra = self.session.extra_videos.get(id)
                participant = extra.participant if extra is not None else self.session.participants.get(id)
                tile = self.tiles.get(participant) if participant is not None else None
                if tile is not None:
                    tiles.append(tile)
        if tiles != self.speaker_tiles:
            self.setPicked(None)
            self.speaker_tiles = tiles
            if layout:
                self.layoutTiles()

    # The session

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        if self.closing:
            return
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_VideoroomSessionDidChangeState(self, notification):
        state, reason = notification.data.state, notification.data.reason
        self.localTile.refresh()
        if state == 'accepted':
            self.setStatus(_t("Joining…"))
        elif state == 'established':
            if self.joined_at is None:
                self.joined_at = time.monotonic()
            if self.clock is None:
                self.clock = _timer(1.0, self.tick, repeats=True)
            self.setStatus(None)
            self.updateTitle()
            self.showControls()
            self.startPendingVideos()
        elif state == 'terminated':
            if self.clock is not None:
                self.clock.invalidate()
                self.clock = None
            self.endRoomChat()       # however it ended, the chat opened for it goes with it
            if reason == 'left':
                self.close()
                return
            self.setStatus(reason[:1].upper() + reason[1:] if reason else _t("Ended"))
            self.callBar.setAlphaValue_(1.0)
            _timer(ENDED_LINGER, self.close)

    @objc.python_method
    def _NH_VideoroomSessionAudioMutedDidChange(self, notification):
        self.updateMuteButton()

    @objc.python_method
    def _NH_VideoroomSessionVideoMutedDidChange(self, notification):
        self.updateCameraButton()

    @objc.python_method
    def _NH_VideoroomSessionLocalVideoDidChange(self, notification):
        self.localTile.refresh()        # our track was replaced (the screen or the camera)

    @objc.python_method
    def _NH_VideoDeviceDidChangeCamera(self, notification):
        self.updateScreenState()

    @objc.python_method
    def _NH_VideoroomSessionParticipantDidJoin(self, notification):
        self.addParticipant(notification.data.participant)

    @objc.python_method
    def _NH_VideoroomSessionParticipantDidLeave(self, notification):
        self.removeParticipant(notification.data.participant)

    @objc.python_method
    def _NH_VideoroomSessionParticipantDidChange(self, notification):
        tile = self.tiles.get(notification.data.participant)
        if tile is None:
            return
        tile.refresh()
        # the feed's offer says whether there is video: an audio only participant goes to the carousel
        if self.picked is not None and not self.picked.in_grid():
            self.setPicked(None)
        self.layoutTiles()

    @objc.python_method
    def _NH_VideoroomSessionParticipantsDidChange(self, notification):
        self.updateTitle()

    @objc.python_method
    def _NH_VideoroomSessionParticipantSpeakingDidChange(self, notification):
        tile = self.tiles.get(notification.data.participant)
        if tile is not None:
            tile.overlay.setNeedsDisplay_(True)

    @objc.python_method
    def _NH_VideoroomSessionSpeakingDidChange(self, notification):
        self.localTile.overlay.setNeedsDisplay_(True)

    @objc.python_method
    def _NH_VideoroomSessionSpeakersDidChange(self, notification):
        self.applySpeakers()
        self.updateRoomButtons()
        originator = notification.data.originator
        if originator and originator != 'you':
            self.flash((_t("Speakers set by %s") if self.session.speakers else _t("Grid set by %s")) % originator)

    @objc.python_method
    def _NH_VideoroomSessionRaisedHandsDidChange(self, notification):
        ids = list(notification.data.raised_hands)
        raised = set(ids)
        own = self.session.id in raised
        if own != self.localTile.hand:
            self.localTile.hand = own
            self.localTile.overlay.setNeedsDisplay_(True)
        for participant, tile in self.tiles.items():
            hand = participant.id in raised
            if hand != tile.hand:
                tile.hand = hand
                tile.overlay.setNeedsDisplay_(True)
        self.updateRoomButtons()
        self.handButton.configure('hand.raised.fill', _t("Lower hand") if own else _t("Raise hand"), tint=_hand_color() if own else None)
        newly = [id for id in ids if id not in self.last_hands]
        self.last_hands = ids
        others = [self.session.speaker_name(id) for id in newly if id != self.session.id]
        if others:
            self.flash(_t("%s raised a hand") % ', '.join(others))

    @objc.python_method
    def _NH_VideoroomSessionExtraVideoDidChangeState(self, notification):
        extra, state, reason = notification.data.extra, notification.data.state, notification.data.reason
        if state == 'connecting':
            self.addParticipant(extra.participant)
            self.flash(_t("Adding %s…") % extra.label)
        elif state == 'established':
            self.flash(_t("%s added") % extra.label)
            self.updateAutoSpeakers()
        elif state == 'ended':
            self.removeParticipant(extra.participant)
            self.updateAutoSpeakers()
            if reason not in ('stopped', 'left'):
                self.flash('%s: %s' % (extra.label, reason), duration=6.0)

    @objc.python_method
    def _NH_VideoroomSessionWasMutedByOther(self, notification):
        self.flash(_t("Muted by %s") % notification.data.originator)

    @objc.python_method
    def _NH_VideoroomSessionGotInviteStatus(self, notification):
        data = notification.data
        name = self.invite_names.get(data.uri, data.uri)
        if data.state == 'progress':
            self.flash(_t("Calling %s…") % name)
        elif data.state == 'success':
            self.flash(_t("%s answered") % name)
        elif data.state == 'failed':
            text = '%s: %s' % (name, data.reason or _t("not reached"))
            if data.code:
                text += ' (%d)' % data.code
            self.flash(text, duration=5.0)
