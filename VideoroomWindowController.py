# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The window of a video conference room joined over WebRTC (Videoroom.py).

The participants are tiles in a grid sized to the window, our own camera among
them (mirrored, as "Me"); the call bar at the bottom is the video call window's
(VideoCallBar), and fades out after 5 seconds without the mouse moving. A tile
shows its participant's RTCVideoTrack in an RTCMTLNSVideoView fitted inside it,
or the initial when there is no picture, with the name (and a muted microphone)
in a pill.

The title is "Room <name> · <n> participants · mm:ss" once connected; a status
pill says what is happening while connecting and why the room ended. The window
is listed in the Window menu while it is open (AppKit lists titled windows).
"""

__all__ = ['VideoroomWindowController', 'window_for_session', 'room_title']

import time

from math import ceil, floor

import objc

from AppKit import (NSApp,
                    NSAnimationContext,
                    NSBackingStoreBuffered,
                    NSBezierPath,
                    NSColor,
                    NSFont,
                    NSFontAttributeName,
                    NSFontWeightMedium,
                    NSFontWeightSemibold,
                    NSForegroundColorAttributeName,
                    NSMakePoint,
                    NSMakeRect,
                    NSMakeSize,
                    NSScreen,
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
from VideoWindowController import VideoCallBar, make_call_bar_segment, CALL_BAR_BOTTOM, CALL_BAR_HEIGHT
from WebRTCSupport import webrtc_classes
from util import call_in_gui_thread, run_in_gui_thread


TILE_GAP = 6.0
TILE_RADIUS = 8.0
TILE_ASPECT = 16.0 / 9.0
NAME_PILL_HEIGHT = 22.0
ENDED_LINGER = 2.5          # seconds the window stays to show why the room ended
CONTROLS_IDLE_TIME = 5.0    # seconds without the mouse moving before the call bar hides
WINDOW_SIZE = (960.0, 600.0)
WINDOW_MINIMUM = (360.0, 240.0)

SESSION_NOTIFICATIONS = ('VideoroomSessionDidChangeState', 'VideoroomSessionAudioMutedDidChange', 'VideoroomSessionVideoMutedDidChange',
                         'VideoroomSessionParticipantDidJoin', 'VideoroomSessionParticipantDidLeave', 'VideoroomSessionParticipantDidChange',
                         'VideoroomSessionParticipantsDidChange')

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


def _timer(interval, function, repeats=False):
    timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, repeats, lambda timer: function())
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return timer


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


def _draw_name_pill(bounds, text, muted):
    font = NSFont.systemFontOfSize_weight_(11.0, NSFontWeightMedium)
    attributes = {NSFontAttributeName: font, NSForegroundColorAttributeName: _white(0.95)}
    icon_width = 18.0 if muted else 0.0
    available = bounds.size.width - 24.0 - icon_width
    label = NSAttributedString.alloc().initWithString_attributes_(text, attributes)
    while label.size().width > available and len(text) > 1:
        text = text[:-2] + '…'
        label = NSAttributedString.alloc().initWithString_attributes_(text, attributes)
    width = label.size().width + 16.0 + icon_width
    pill = NSMakeRect(bounds.origin.x + 8.0, bounds.origin.y + 8.0, width, NAME_PILL_HEIGHT)
    NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.55).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(pill, NAME_PILL_HEIGHT / 2, NAME_PILL_HEIGHT / 2).fill()
    x = pill.origin.x + 8.0
    if muted:
        mark = NSAttributedString.alloc().initWithString_attributes_('●', {NSFontAttributeName: NSFont.systemFontOfSize_(9.0),
                                                                                NSForegroundColorAttributeName: NSColor.systemRedColor()})
        mark.drawAtPoint_(NSMakePoint(x, pill.origin.y + (NAME_PILL_HEIGHT - mark.size().height) / 2))
        x += icon_width
    label.drawAtPoint_(NSMakePoint(x, pill.origin.y + (NAME_PILL_HEIGHT - label.size().height) / 2))


def _draw_placeholder(bounds, name, detail=None):
    """The initial in a circle, for a tile without a picture."""
    diameter = min(bounds.size.width, bounds.size.height) * 0.36
    center_x = bounds.origin.x + bounds.size.width / 2
    center_y = bounds.origin.y + bounds.size.height / 2 + 6.0
    circle = NSMakeRect(center_x - diameter / 2, center_y - diameter / 2, diameter, diameter)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(70 / 255.0, 74 / 255.0, 82 / 255.0, 1.0).setFill()
    NSBezierPath.bezierPathWithOvalInRect_(circle).fill()
    initial = (name.strip()[:1] or '?').upper()
    text = NSAttributedString.alloc().initWithString_attributes_(initial, {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(max(10.0, floor(diameter * 0.45)), NSFontWeightSemibold),
        NSForegroundColorAttributeName: _white(0.9)})
    size = text.size()
    text.drawAtPoint_(NSMakePoint(center_x - size.width / 2, center_y - size.height / 2))
    if detail:
        small = NSAttributedString.alloc().initWithString_attributes_(detail, {
            NSFontAttributeName: NSFont.systemFontOfSize_(11.0), NSForegroundColorAttributeName: _white(0.6)})
        small.drawAtPoint_(NSMakePoint(center_x - small.size().width / 2, circle.origin.y - 6.0 - small.size().height))


class VideoroomTileOverlay(NSView):
    """What is drawn over a tile: the placeholder when there is no picture, the name pill."""

    tile = None

    def isOpaque(self):
        return False

    def hitTest_(self, point):
        return None

    def drawRect_(self, rect):
        tile = self.tile
        if tile is None:
            return
        bounds = self.bounds()
        if not tile.showingVideo():
            _draw_placeholder(bounds, tile.name(), tile.detail())
        _draw_name_pill(bounds, tile.label(), tile.muted())


class VideoroomTileView(NSView):
    """A participant (or ourselves): its video track in an RTCMTLNSVideoView fitted inside, or the initial."""

    participant = None      # None for our own tile
    session = None
    track = None
    videoView = None
    videoContainer = None
    overlay = None
    videoSize = None
    mirrored = False

    @objc.python_method
    def setup(self, session, participant=None):
        self.session = session
        self.participant = participant
        self.mirrored = participant is None
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(TILE_RADIUS)
        self.layer().setMasksToBounds_(True)
        self.layer().setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(32 / 255.0, 34 / 255.0, 38 / 255.0, 1.0).CGColor())
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
        self.update()

    def isFlipped(self):
        return False

    def hitTest_(self, point):
        return None         # clicks and the mouse go to the window's content view

    # What is shown

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
    def detail(self):
        if self.participant is None:
            if self.session.video_muted:
                return _t("camera off")
            return _t("no camera") if self.videoView is None else None
        if self.participant.type == 'sip':
            return _t("audio")
        if self.participant.state in ('attaching', 'connecting'):
            return _t("connecting…")
        if self.participant.state == 'failed':
            return _t("no media")
        return None

    @objc.python_method
    def showingVideo(self):
        return self.videoView is not None and self.track is not None and self.videoSize is not None

    @objc.python_method
    def wantedTrack(self):
        if self.participant is not None:
            return self.participant.video_track
        return None if self.session.video_muted else self.session.local_video_track

    @objc.python_method
    def update(self):
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
        self.overlay.setNeedsDisplay_(True)

    @objc.python_method
    def release(self):
        if self.track is not None and self.videoView is not None:
            self.track.removeRenderer_(self.videoView)
        self.track = None
        if self.videoView is not None:
            self.videoView.setDelegate_(None)

    # Layout: the picture fitted, not cropped

    @objc.typedSelector(b'v@:@{CGSize=dd}')
    def videoView_didChangeVideoSize_(self, view, size):
        call_in_gui_thread(self._videoSizeChanged, size.width, size.height)

    @objc.python_method
    def _videoSizeChanged(self, width, height):
        self.videoSize = (width, height) if width > 0 and height > 0 else None
        self.layoutVideo()
        self.overlay.setNeedsDisplay_(True)

    @objc.python_method
    def layoutVideo(self):
        if self.videoView is None:
            return
        bounds = self.bounds()
        if not self.showingVideo():
            self.videoView.setHidden_(True)
            return
        width, height = self.videoSize
        scale = min(bounds.size.width / width, bounds.size.height / height)
        fitted_w, fitted_h = floor(width * scale), floor(height * scale)
        self.videoView.setFrame_(NSMakeRect(floor((bounds.size.width - fitted_w) / 2), floor((bounds.size.height - fitted_h) / 2), fitted_w, fitted_h))
        self.videoView.setHidden_(False)
        layer = self.videoContainer.layer()
        if layer is not None:
            if self.mirrored:
                layer.setSublayerTransform_(CATransform3DTranslate(CATransform3DMakeScale(-1.0, 1.0, 1.0), -bounds.size.width, 0.0, 0.0))
            else:
                layer.setSublayerTransform_(CATransform3DIdentity)

    def setFrameSize_(self, size):
        objc.super(VideoroomTileView, self).setFrameSize_(size)
        self.layoutVideo()


class VideoroomContentView(NSView):
    """The window's content: the tiles in the grid that makes them largest at 16:9, under the call bar.
    Moving the mouse brings the call bar back; a double click toggles full screen."""

    controller = None
    tiles = None
    trackingArea = None

    def initWithFrame_(self, frame):
        self = objc.super(VideoroomContentView, self).initWithFrame_(frame)
        if self is not None:
            self.tiles = []
            self.setWantsLayer_(True)
            self.layer().setBackgroundColor_(NSColor.blackColor().CGColor())
        return self

    def isOpaque(self):
        return True

    def acceptsFirstResponder(self):
        return True

    @objc.python_method
    def addTile(self, tile):
        self.tiles.append(tile)
        # the tiles go under the status pill and the call bar
        below = self.controller.statusLabel if self.controller is not None else None
        if below is not None and below.superview() is self:
            self.addSubview_positioned_relativeTo_(tile, NSWindowBelow, below)
        else:
            self.addSubview_(tile)
        self.layoutTiles()

    @objc.python_method
    def removeTile(self, tile):
        if tile in self.tiles:
            self.tiles.remove(tile)
            tile.removeFromSuperview()
            self.layoutTiles()

    @objc.python_method
    def layoutTiles(self):
        count = len(self.tiles)
        if not count:
            return
        bounds = self.bounds()
        width, height = bounds.size.width - 2 * TILE_GAP, bounds.size.height - 2 * TILE_GAP
        best = None
        for columns in range(1, count + 1):
            rows = int(ceil(count / float(columns)))
            cell_w = (width - (columns - 1) * TILE_GAP) / columns
            cell_h = (height - (rows - 1) * TILE_GAP) / rows
            tile_w = min(cell_w, cell_h * TILE_ASPECT)
            tile_h = tile_w / TILE_ASPECT
            if best is None or tile_w * tile_h > best[0]:
                best = (tile_w * tile_h, columns, rows, cell_w, cell_h)
        area, columns, rows, cell_w, cell_h = best
        # tiles take the whole cell: the pictures are fitted inside them; the first row is at the top
        for index, tile in enumerate(self.tiles):
            row, column = divmod(index, columns)
            in_row = min(columns, count - row * columns)
            offset = (width - in_row * cell_w - (in_row - 1) * TILE_GAP) / 2      # a last, shorter row is centred
            x = TILE_GAP + offset + column * (cell_w + TILE_GAP)
            y = bounds.size.height - TILE_GAP - (row + 1) * cell_h - row * TILE_GAP
            tile.setFrame_(NSMakeRect(floor(x), floor(y), floor(cell_w), floor(cell_h)))

    def resizeSubviewsWithOldSize_(self, old_size):
        objc.super(VideoroomContentView, self).resizeSubviewsWithOldSize_(old_size)
        self.layoutTiles()
        if self.controller is not None:
            self.controller.placeStatus()

    def updateTrackingAreas(self):
        if self.trackingArea is not None:
            self.removeTrackingArea_(self.trackingArea)
        self.trackingArea = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            NSMakeRect(0, 0, 0, 0), NSTrackingMouseMoved | NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect, self, None)
        self.addTrackingArea_(self.trackingArea)
        objc.super(VideoroomContentView, self).updateTrackingAreas()

    def mouseMoved_(self, event):
        if self.controller is not None:
            self.controller.showControls()

    def mouseEntered_(self, event):
        self.mouseMoved_(event)

    def mouseDown_(self, event):
        if self.controller is None:
            return
        if event.clickCount() == 2:
            self.controller.toggleFullScreen()
        else:
            self.controller.showControls()

    def keyDown_(self, event):
        if event.keyCode() == 53 and self.window() is not None and self.window().styleMask() & NSWindowStyleMaskFullScreen:     # Esc
            self.window().toggleFullScreen_(None)
        else:
            objc.super(VideoroomContentView, self).keyDown_(event)


@implementer(IObserver)
class VideoroomWindowController(NSObject):
    """The window of one VideoroomSession."""

    window = None
    contentView = None
    callBar = None
    statusLabel = None

    def initWithSession_(self, session):
        self = objc.super(VideoroomWindowController, self).init()
        if self is None:
            return None
        self.session = session
        self.tiles = {}             # participant -> tile
        self.joined_at = None
        self.closing = False
        self.idle_timer = None
        self.clock = None
        self.title = room_title(session.room)
        self._build()
        notification_center = NotificationCenter()
        for name in SESSION_NOTIFICATIONS:
            notification_center.add_observer(self, sender=session, name=name)
        for participant in list(session.participants.values()):
            self.addParticipant(participant)
        self.updateMuteButton()
        self.updateCameraButton()
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

        self.localTile = VideoroomTileView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 180))
        self.localTile.setup(self.session)
        content.addTile(self.localTile)

        # the status pill, above the call bar
        self.statusLabel = NSTextField.labelWithString_('')
        self.statusLabel.setFont_(NSFont.systemFontOfSize_weight_(12.0, NSFontWeightMedium))
        self.statusLabel.setTextColor_(_white(0.95))
        self.statusLabel.setDrawsBackground_(False)
        self.statusLabel.setWantsLayer_(True)
        self.statusLabel.layer().setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.55).CGColor())
        self.statusLabel.layer().setCornerRadius_(11.0)
        self.statusLabel.setAlignment_(1)       # NSTextAlignmentCenter
        content.addSubview_(self.statusLabel)

        bar = VideoCallBar.alloc().initWithFrame_(NSMakeRect(0, CALL_BAR_BOTTOM, 0, CALL_BAR_HEIGHT))
        content.addSubview_(bar)
        self.callBar = bar

        def add(symbol, label, action, destructive=False):
            return bar.addSegment(make_call_bar_segment(symbol, label, action, self, destructive))

        self.muteButton = add('mic.fill', _t("Mute"), 'userClickedMuteButton:')
        self.cameraButton = add('video.fill', _t("Camera off"), 'userClickedCameraButton:')
        self.shareButton = add('square.and.arrow.up', _t("Share"), 'userClickedShareButton:')
        self.shareButton.setToolTip_(_t("The addresses others can join this conference at"))
        from ConferenceShareDialog import share_available
        self.shareButton.setHidden_(not share_available(self.session.sip_room, self.session.account, self.session.target))
        self.shareDialog = None
        self.fullScreenButton = add('arrow.up.left.and.arrow.down.right', _t("Full screen"), 'userClickedFullScreenButton:')
        self.hangupButton = add('phone.down.fill', _t("Leave"), 'userClickedHangupButton:', destructive=True)
        bar.tile()
        self.placeStatus()

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
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.contentView)
        self.showControls()

    def windowWillClose_(self, notification):
        if self.session.state not in ('idle', 'terminated'):
            self.session.leave()
        self.release()

    @objc.python_method
    def close(self):
        if self.window is not None and self.window.isVisible():
            self.window.close()
        else:
            self.release()

    @objc.python_method
    def release(self):
        if self.closing:
            return
        self.closing = True
        for timer in (self.idle_timer, self.clock):
            if timer is not None:
                timer.invalidate()
        self.idle_timer = self.clock = None
        notification_center = NotificationCenter()
        for name in SESSION_NOTIFICATIONS:
            notification_center.discard_observer(self, sender=self.session, name=name)
        self.localTile.release()
        for tile in self.tiles.values():
            tile.release()
        _windows.pop(self.session, None)

    # The call bar fades out when the mouse rests

    @objc.python_method
    def showControls(self):
        if self.callBar.alphaValue() < 1.0:
            NSAnimationContext.beginGrouping()
            NSAnimationContext.currentContext().setDuration_(0.2)
            self.callBar.animator().setAlphaValue_(1.0)
            NSAnimationContext.endGrouping()
        if self.idle_timer is not None:
            self.idle_timer.invalidate()
        self.idle_timer = _timer(CONTROLS_IDLE_TIME, self.hideControls)

    @objc.python_method
    def hideControls(self):
        self.idle_timer = None
        if self.session.state == 'terminated' or self.closing:
            return
        location = self.contentView.convertPoint_fromView_(self.window.mouseLocationOutsideOfEventStream(), None)
        if not self.callBar.isHidden() and self.callBar.alphaValue() > 0 and _contains(self.callBar.frame(), location):
            self.idle_timer = _timer(CONTROLS_IDLE_TIME, self.hideControls)
            return
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.4)
        self.callBar.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

    @objc.python_method
    def toggleFullScreen(self):
        self.window.toggleFullScreen_(None)

    def windowDidEnterFullScreen_(self, notification):
        self.fullScreenButton.configure('arrow.down.right.and.arrow.up.left', _t("Exit full screen"))

    def windowDidExitFullScreen_(self, notification):
        self.fullScreenButton.configure('arrow.up.left.and.arrow.down.right', _t("Full screen"))

    # Status

    @objc.python_method
    def setStatus(self, text):
        self.statusLabel.setStringValue_(text or '')
        self.statusLabel.setHidden_(not text)
        self.placeStatus()

    @objc.python_method
    def placeStatus(self):
        if self.statusLabel is None or self.contentView is None:
            return
        size = self.statusLabel.intrinsicContentSize()
        width, height = ceil(size.width) + 28.0, 22.0
        bounds = self.contentView.bounds()
        self.statusLabel.setFrame_(NSMakeRect(floor((bounds.size.width - width) / 2), CALL_BAR_BOTTOM + CALL_BAR_HEIGHT + 10.0, width, height))

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

    # Controls

    def userClickedMuteButton_(self, sender):
        self.session.set_audio_muted(not self.session.audio_muted)

    def userClickedCameraButton_(self, sender):
        self.session.set_video_muted(not self.session.video_muted)

    def userClickedShareButton_(self, sender):
        from ConferenceShareDialog import ConferenceShareDialog
        self.shareDialog = ConferenceShareDialog.alloc().initWithRoom_account_target_title_(self.session.sip_room, self.session.account,
                                                                                            self.session.target, self.title)
        self.shareDialog.runSheetForWindow(self.window)
        self.showControls()

    def userClickedFullScreenButton_(self, sender):
        self.toggleFullScreen()

    def userClickedHangupButton_(self, sender):
        self.close()

    @objc.python_method
    def updateMuteButton(self):
        muted = self.session.audio_muted
        self.muteButton.configure('mic.slash.fill' if muted else 'mic.fill', _t("Unmute") if muted else _t("Mute"),
                                  tint=NSColor.systemRedColor() if muted else None)
        self.localTile.update()

    @objc.python_method
    def updateCameraButton(self):
        off = self.session.video_muted
        self.cameraButton.configure('video.slash.fill' if off else 'video.fill', _t("Camera on") if off else _t("Camera off"),
                                    tint=NSColor.systemRedColor() if off else None)
        self.localTile.update()

    # Participants

    @objc.python_method
    def addParticipant(self, participant):
        if not participant.visible or participant in self.tiles:
            return
        tile = VideoroomTileView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 180))
        tile.setup(self.session, participant)
        self.tiles[participant] = tile
        self.contentView.addTile(tile)
        self.updateTitle()

    @objc.python_method
    def removeParticipant(self, participant):
        tile = self.tiles.pop(participant, None)
        if tile is not None:
            tile.release()
            self.contentView.removeTile(tile)
        self.updateTitle()

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
        self.localTile.update()
        if state == 'accepted':
            self.setStatus(_t("Joining…"))
        elif state == 'established':
            if self.joined_at is None:
                self.joined_at = time.monotonic()
            if self.clock is None:
                self.clock = _timer(1.0, self.updateTitle, repeats=True)
            self.setStatus(None)     # once connected the title says it; the pill is for connecting and ending
            self.updateTitle()
            self.showControls()
        elif state == 'terminated':
            if self.clock is not None:
                self.clock.invalidate()
                self.clock = None
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
    def _NH_VideoroomSessionParticipantDidJoin(self, notification):
        self.addParticipant(notification.data.participant)

    @objc.python_method
    def _NH_VideoroomSessionParticipantDidLeave(self, notification):
        self.removeParticipant(notification.data.participant)

    @objc.python_method
    def _NH_VideoroomSessionParticipantDidChange(self, notification):
        tile = self.tiles.get(notification.data.participant)
        if tile is not None:
            tile.update()

    @objc.python_method
    def _NH_VideoroomSessionParticipantsDidChange(self, notification):
        self.updateTitle()


def _contains(rect, point):
    return (rect.origin.x <= point.x <= rect.origin.x + rect.size.width
            and rect.origin.y <= point.y <= rect.origin.y + rect.size.height)
