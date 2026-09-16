# Copyright (C) 2026 AG Projects. See LICENSE for details.
#
"""Remote pointer on a shared screen -- the call side.

Wire format and geometry are in ScreenPointer.py. This module:

  * advertises our capabilities when a call starts, and records the peer's;
  * tells the peer when we start/stop sending a screen ("My screen" capture
    device on a call with video) and records when the peer does;
  * as the SHARER, draws the pointers the peer sends over the display being
    captured (PointerOverlay) and acks them;
  * as the VIEWER, sends pointers for VideoWindowController and reports the
    acks back to it.

Per-call state lives here, keyed by the sipsimple Session, not in the video
window, so it survives the window being closed and reopened mid-call.
VideoWindowController listens for BlinkScreenPointerDidChange and
BlinkScreenPointerGotAck, both posted with the Session as sender.
"""

from AppKit import (NSBackingStoreBuffered,
                    NSColor,
                    NSScreen,
                    NSScreenSaverWindowLevel,
                    NSView,
                    NSWindow,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowStyleMaskBorderless)
from Foundation import NSBezierPath, NSMakeRect, NSProcessInfo
from Quartz import (CABasicAnimation,
                    CACurrentMediaTime,
                    CAAnimationGroup,
                    CALayer,
                    CAMediaTimingFunction,
                    CATransaction,
                    CGColorCreateGenericRGB,
                    CGDisplayMirrorsDisplay,
                    CGGetActiveDisplayList,
                    CGMainDisplayID,
                    kCAFillModeForwards,
                    kCAMediaTimingFunctionEaseOut)

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from sipsimple.application import SIPApplication
from sipsimple.threading import run_in_thread
from zope.interface import implementer

import ScreenPointer
from BlinkLogger import BlinkLogger
from util import call_later, checkValidPhoneNumber, run_in_gui_thread


__all__ = ['ScreenPointerManager', 'PointerEchoView']


MARKER_RADIUS = 26.0
DOT_RADIUS = 9.0
PULSE_SECONDS = 0.38
PULSES = 2
FADE_SECONDS = 0.2
PENDING_CLICK_TTL = 5.0         # seconds a sent click waits for its ack


class _CallState(object):
    def __init__(self):
        self.peer_capabilities = None       # None: the peer never advertised
        self.peer_sharing = False
        self.peer_in_app = True
        self.capabilities_sent = False
        self.sharing_signalled = False      # we told the peer 'start'
        self.pending_clicks = {}            # t -> (nx, ny, sent_at)
        self.last_drawn_click = None


def _content_of(data):
    """(content_type, body) of an in-dialog MESSAGE, looking inside CPIM."""
    headers = data.headers or {}
    header = headers.get('Content-Type')
    if header is None:
        return None, None
    content_type = (getattr(header, 'content_type', None) or str(header)).lower()
    body = data.body
    if content_type == 'message/cpim':
        try:
            from sipsimple.streams.msrp.chat import CPIMPayload
            payload = CPIMPayload.decode(body)
        except Exception:
            return None, None
        return (payload.content_type or '').lower(), payload.content
    return content_type, body


def _video_established(session):
    return any(stream.type == 'video' and getattr(stream, 'state', None) == 'ESTABLISHED'
               for stream in (session.streams or []))


def _local_screen_index():
    device = getattr(SIPApplication, 'video_device', None)
    if device is None:
        return None
    try:
        return ScreenPointer.screen_index(device.real_name)
    except Exception:
        return None


def _capture_letterboxes():
    # avf_dev.m asks ScreenCaptureKit to keep the display's aspect ratio,
    # which it only can from macOS 14 on; before that the display is scaled
    # to the frame.
    try:
        return NSProcessInfo.processInfo().operatingSystemVersion().majorVersion >= 14
    except Exception:
        return False


def _screen_for_index(index):
    """The NSScreen avf_dev.m names "My screen <index+1>": main display
    first, then the other active displays that do not mirror another."""
    try:
        error, displays, count = CGGetActiveDisplayList(16, None, None)
    except Exception:
        return None
    if error or not displays:
        return None
    displays = list(displays[:count])
    main = CGMainDisplayID()
    ordered = [display for display in displays if display == main]
    ordered += [display for display in displays if display != main and not CGDisplayMirrorsDisplay(display)]
    if index >= len(ordered):
        return None
    for screen in NSScreen.screens():
        try:
            if int(screen.deviceDescription()['NSScreenNumber']) == ordered[index]:
                return screen
        except Exception:
            continue
    return None


class PointerOverlayView(NSView):
    def hitTest_(self, point):
        return None

    def isOpaque(self):
        return False


class PointerOverlay(object):
    """Pulsing marker drawn above everything on the display we share, the
    same marker sylk-mobile's PointerGuideOverlay draws: a blue dot with a
    ring that grows and fades twice, then the whole marker fades out.

    It is a borderless, click-through window, so it is also in the captured
    screen and the viewer sees the marker in the video too.
    """

    def __init__(self):
        self.window = None
        self.marker = None
        self.generation = 0

    def _window_for(self, screen):
        window = self.window
        if window is None:
            window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 100, 100), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
            window.setReleasedWhenClosed_(False)
            window.setOpaque_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            window.setHasShadow_(False)
            window.setIgnoresMouseEvents_(True)
            window.setLevel_(NSScreenSaverWindowLevel)
            window.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                                          NSWindowCollectionBehaviorStationary |
                                          NSWindowCollectionBehaviorFullScreenAuxiliary |
                                          NSWindowCollectionBehaviorIgnoresCycle)
            view = PointerOverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100))
            view.setWantsLayer_(True)
            window.setContentView_(view)
            self.window = window
        if window.frame() != screen.frame():
            window.setFrame_display_(screen.frame(), False)
        return window

    def show(self, screen, sx, sy):
        """sx/sy normalized on the screen, origin top-left."""
        window = self._window_for(screen)
        root = window.contentView().layer()
        if root is None:
            return
        size = screen.frame().size
        x = sx * size.width
        y = (1.0 - sy) * size.height

        blue = CGColorCreateGenericRGB(33 / 255.0, 150 / 255.0, 243 / 255.0, 1.0)
        blue_fill = CGColorCreateGenericRGB(33 / 255.0, 150 / 255.0, 243 / 255.0, 0.92)
        diameter = 2 * MARKER_RADIUS

        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        try:
            if self.marker is not None:
                self.marker.removeFromSuperlayer()
            marker = CALayer.layer()
            marker.setFrame_(((x - MARKER_RADIUS, y - MARKER_RADIUS), (diameter, diameter)))
            ring = CALayer.layer()
            ring.setFrame_(((0, 0), (diameter, diameter)))
            ring.setCornerRadius_(MARKER_RADIUS)
            ring.setBorderWidth_(3.0)
            ring.setBorderColor_(blue)
            ring.setOpacity_(0.0)       # model value once the pulses are over
            dot = CALayer.layer()
            dot.setFrame_(((MARKER_RADIUS - DOT_RADIUS, MARKER_RADIUS - DOT_RADIUS), (2 * DOT_RADIUS, 2 * DOT_RADIUS)))
            dot.setCornerRadius_(DOT_RADIUS)
            dot.setBackgroundColor_(blue_fill)
            marker.addSublayer_(ring)
            marker.addSublayer_(dot)
            root.addSublayer_(marker)
            self.marker = marker
        finally:
            CATransaction.commit()

        grow = CABasicAnimation.animationWithKeyPath_('transform.scale')
        grow.setFromValue_(0.6)
        grow.setToValue_(2.3)
        fade = CABasicAnimation.animationWithKeyPath_('opacity')
        fade.setFromValue_(0.9)
        fade.setToValue_(0.0)
        pulse = CAAnimationGroup.animation()
        pulse.setAnimations_([grow, fade])
        pulse.setDuration_(PULSE_SECONDS)
        pulse.setRepeatCount_(PULSES)
        pulse.setTimingFunction_(CAMediaTimingFunction.functionWithName_(kCAMediaTimingFunctionEaseOut))
        ring.addAnimation_forKey_(pulse, 'pulse')

        vanish = CABasicAnimation.animationWithKeyPath_('opacity')
        vanish.setFromValue_(1.0)
        vanish.setToValue_(0.0)
        vanish.setBeginTime_(marker.convertTime_fromLayer_(CACurrentMediaTime(), None) + PULSE_SECONDS * PULSES)
        vanish.setDuration_(FADE_SECONDS)
        vanish.setFillMode_(kCAFillModeForwards)
        vanish.setRemovedOnCompletion_(False)
        marker.addAnimation_forKey_(vanish, 'vanish')

        window.orderFrontRegardless()

        self.generation += 1
        generation = self.generation
        call_later(PULSE_SECONDS * PULSES + FADE_SECONDS + 0.1, self._expire, generation)

    def _expire(self, generation):
        if generation != self.generation:
            return      # a newer click took over
        self.hide()

    def hide(self):
        if self.marker is not None:
            self.marker.removeFromSuperlayer()
            self.marker = None
        if self.window is not None:
            self.window.orderOut_(None)


class PointerEchoView(NSView):
    """The viewer's confirmation: a green ring where the click the sharer
    acked landed. Laid over the remote video view, click-through."""

    point = None

    def hitTest_(self, point):
        return None

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        if self.point is None:
            return
        x, y = self.point
        radius = 11.0
        oval = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - radius, y - radius, 2 * radius, 2 * radius))
        NSColor.colorWithCalibratedRed_green_blue_alpha_(76 / 255.0, 175 / 255.0, 80 / 255.0, 0.35).setFill()
        oval.fill()
        oval.setLineWidth_(3.0)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(76 / 255.0, 175 / 255.0, 80 / 255.0, 1.0).setStroke()
        oval.stroke()


@implementer(IObserver)
class ScreenPointerManager(object, metaclass=Singleton):

    def __init__(self):
        self.calls = {}
        self.overlay = None
        self.notification_center = NotificationCenter()
        for name in ('SIPSessionDidStart', 'SIPSessionDidEnd', 'SIPSessionDidFail',
                     'SIPSessionGotMessage', 'MediaStreamDidStart', 'MediaStreamDidEnd',
                     'VideoDeviceDidChangeCamera'):
            self.notification_center.add_observer(self, name=name)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        try:
            handler(notification.sender, notification.data)
        except Exception as e:
            BlinkLogger().log_error('[pointer] %s handler failed: %s' % (notification.name, e))

    # --- queries for the video window ------------------------------------------

    def can_point(self, session):
        """The peer is sending its screen and draws the pointers we send."""
        state = self.calls.get(session)
        return bool(state is not None and state.peer_sharing and state.peer_capabilities
                    and ScreenPointer.CAP_POINTER in state.peer_capabilities)

    def peer_in_app(self, session):
        state = self.calls.get(session)
        return state is None or state.peer_in_app

    def send_pointer(self, session, nx, ny):
        state = self.calls.get(session)
        if state is None or not self.can_point(session) or not state.peer_in_app:
            return None
        t = ScreenPointer.new_click_id()
        now = t / 1000.0
        state.pending_clicks = dict((key, value) for key, value in state.pending_clicks.items()
                                    if now - value[2] < PENDING_CLICK_TTL)
        state.pending_clicks[t] = (nx, ny, now)
        self._send(session, ScreenPointer.POINTER_CONTENT_TYPE, ScreenPointer.build_pointer(nx, ny, t))
        return t

    # --- sending ------------------------------------------------------------------

    @run_in_thread('network-io')
    def _send(self, session, content_type, content):
        try:
            session.send_message(content_type, content)
        except Exception as e:
            BlinkLogger().log_info('[pointer] cannot send %s: %s' % (content_type, e))

    def _eligible(self, session):
        """Not for conferences (the mixer is no client) nor for phone
        numbers (a gateway, which has no use for these), as on sylk-mobile."""
        if getattr(session, 'remote_focus', False):
            return False
        try:
            return not checkValidPhoneNumber(session.remote_identity.uri.user)
        except Exception:
            return True

    def _state(self, session):
        state = self.calls.get(session)
        if state is None:
            state = self.calls[session] = _CallState()
        return state

    def _changed(self, session):
        self.notification_center.post_notification('BlinkScreenPointerDidChange', sender=session)

    def _update_sharing(self, session):
        state = self.calls.get(session)
        if state is None or not self._eligible(session):
            return
        sharing = (session.state == 'connected' and _video_established(session)
                   and _local_screen_index() is not None)
        if sharing == state.sharing_signalled:
            return
        state.sharing_signalled = sharing
        state.last_drawn_click = None
        self._send(session, ScreenPointer.SCREEN_SHARING_CONTENT_TYPE,
                   ScreenPointer.build_sharing('start' if sharing else 'stop'))
        BlinkLogger().log_info('[pointer] told %s we %s sharing the screen'
                               % (session.remote_identity.uri, 'started' if sharing else 'stopped'))
        if not self._anyone_sharing() and self.overlay is not None:
            self.overlay.hide()

    def _anyone_sharing(self):
        return any(state.sharing_signalled for state in self.calls.values())

    # --- notifications ----------------------------------------------------------------

    def _NH_SIPSessionDidStart(self, session, data):
        state = self._state(session)
        if self._eligible(session) and not state.capabilities_sent:
            state.capabilities_sent = True
            self._send(session, ScreenPointer.CAPABILITIES_CONTENT_TYPE, ScreenPointer.build_capabilities())
        self._update_sharing(session)

    def _NH_SIPSessionDidEnd(self, session, data):
        if self.calls.pop(session, None) is not None:
            self._changed(session)
        if not self._anyone_sharing() and self.overlay is not None:
            self.overlay.hide()

    _NH_SIPSessionDidFail = _NH_SIPSessionDidEnd

    def _NH_MediaStreamDidStart(self, stream, data):
        if stream.type == 'video' and stream.session is not None and stream.session in self.calls:
            self._update_sharing(stream.session)

    def _NH_MediaStreamDidEnd(self, stream, data):
        session = getattr(stream, 'session', None)
        if stream.type != 'video' or session not in self.calls:
            return
        state = self.calls[session]
        if state.peer_sharing:
            # No video, no screen: the peer may never get to send 'stop'.
            state.peer_sharing = False
            self._changed(session)
        if session.state == 'connected':
            self._update_sharing(session)
        else:
            state.sharing_signalled = False

    def _NH_VideoDeviceDidChangeCamera(self, sender, data):
        for session in list(self.calls):
            self._update_sharing(session)

    def _NH_SIPSessionGotMessage(self, session, data):
        content_type, body = _content_of(data)
        if content_type not in ScreenPointer.CONTENT_TYPES:
            return
        state = self._state(session)
        peer = session.remote_identity.uri

        if content_type == ScreenPointer.CAPABILITIES_CONTENT_TYPE:
            capabilities = ScreenPointer.parse_capabilities(body)
            if capabilities is not None:
                state.peer_capabilities = capabilities
                BlinkLogger().log_info('[pointer] %s advertised: %s' % (peer, ', '.join(capabilities) or '(none)'))
                self._changed(session)

        elif content_type == ScreenPointer.SCREEN_SHARING_CONTENT_TYPE:
            action = ScreenPointer.parse_sharing(body)
            if action is not None:
                state.peer_sharing = (action == 'start')
                if state.peer_sharing:
                    state.peer_in_app = True
                BlinkLogger().log_info('[pointer] %s %sed sharing the screen' % (peer, action))
                self._changed(session)

        elif content_type == ScreenPointer.POINTER_CONTENT_TYPE:
            pointer = ScreenPointer.parse_pointer(body)
            if pointer is not None and state.sharing_signalled:
                x, y, t = pointer
                if t is None or t != state.last_drawn_click:
                    state.last_drawn_click = t
                    self._draw(x, y)
                if t is not None:
                    self._send(session, ScreenPointer.POINTER_ACK_CONTENT_TYPE, ScreenPointer.build_ack(t))

        elif content_type == ScreenPointer.POINTER_ACK_CONTENT_TYPE:
            t = ScreenPointer.parse_ack(body)
            click = state.pending_clicks.pop(t, None) if t is not None else None
            if click is not None:
                self.notification_center.post_notification('BlinkScreenPointerGotAck', sender=session,
                                                           data=NotificationData(x=click[0], y=click[1]))

        elif content_type == ScreenPointer.POINTER_VISIBILITY_CONTENT_TYPE:
            in_app = ScreenPointer.parse_visibility(body)
            if in_app is not None:
                state.peer_in_app = in_app
                self._changed(session)

    # --- drawing -----------------------------------------------------------------

    def _draw(self, x, y):
        index = _local_screen_index()
        if index is None:
            return
        screen = _screen_for_index(index)
        if screen is None:
            BlinkLogger().log_info('[pointer] no display for screen %d' % (index + 1))
            return
        frame_w, frame_h = -1, -1
        try:
            frame_w, frame_h = SIPApplication.video_device.producer.framesize
        except Exception:
            pass
        size = screen.frame().size
        point = ScreenPointer.frame_to_screen(x, y, frame_w, frame_h, size.width, size.height,
                                              _capture_letterboxes() and frame_w > 0 and frame_h > 0)
        if point is None:
            return      # on the letterbox bars
        if self.overlay is None:
            self.overlay = PointerOverlay()
        self.overlay.show(screen, point[0], point[1])
