# Copyright (C) 2010-2026 AG Projects. See LICENSE for details.
#
# A standalone "Video Mirror" window: shows the local camera feed,
# mirrored horizontally (handled automatically by VideoWidget when its
# producer is SIPApplication.video_device.producer). Reuses the Metal
# renderer from VideoWindowController.VideoWidget so we get the same
# QuickTime-fluid playback as the call window.
#
# Exposed via Devices -> Video Mirror in the main menu.

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSClosableWindowMask,
                    NSMiniaturizableWindowMask,
                    NSResizableWindowMask,
                    NSTitledWindowMask,
                    NSView,
                    NSViewHeightSizable,
                    NSViewWidthSizable,
                    NSWindow,
                    NSWindowController)

from Foundation import (NSLocalizedString,
                        NSMakeRect,
                        NSMakeSize,
                        NSNotificationCenter,
                        NSObject)

import objc

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from sipsimple.application import SIPApplication
from util import run_in_gui_thread

# Module-level import: PreviewVideoWidget below subclasses VideoWidget, so
# we need it resolvable at class-definition time. VideoWindowController
# does not import this module back, so there is no circular dependency.
from VideoWindowController import VideoWidget


class PreviewVideoWidget(VideoWidget):
    """A VideoWidget that resizes its own frame height to match the
    detected camera aspect ratio. Used for the in-preferences preview
    so the picture fills the view exactly — no letterbox or pillarbox
    bars inside a fixed-shape box."""

    @objc.python_method
    def _on_aspect_ratio_detected(self, width, height):
        if width <= 0 or height <= 0:
            return
        aspect = float(width) / float(height)
        cur = self.frame()
        if cur.size.width <= 0:
            return
        target_h = max(60.0, cur.size.width / aspect)
        if abs(cur.size.height - target_h) < 0.5:
            return
        new_frame = ((cur.origin.x, cur.origin.y),
                     (cur.size.width, target_h))
        self.setFrame_(new_frame)
        # If our enclosing centered container exists, ask it to
        # re-fit so the VerticalBoxView picks up the new height.
        sv = self.superview()
        if sv is not None and hasattr(sv, '_fit_to_preview'):
            try:
                sv._fit_to_preview()
            except Exception:
                pass


class CenteredPreviewContainer(NSView):
    """Holds a PreviewVideoWidget at a fixed pixel width, centered
    horizontally inside whatever container width the parent layout
    gives us. Also propagates the preview's aspect-driven height
    changes upward so the surrounding VerticalBoxView re-flows."""

    preview = None
    # The intended preview width. Captured at setPreview() time so we
    # can re-assert it in _reposition() -- otherwise the surrounding
    # VerticalBoxView's layout cycles can occasionally stretch the
    # preview to the full container width, which then makes our
    # centering math compute origin.x = 0 and the preview snaps to the
    # left of the section.
    fixed_preview_width = 160.0

    def isFlipped(self):
        return True

    @objc.python_method
    def setPreview(self, preview):
        self.preview = preview
        self.fixed_preview_width = float(preview.frame().size.width) or 160.0
        self.addSubview_(preview)
        # Match our height to the preview's so the VerticalBoxView
        # parent doesn't reserve more vertical space than needed.
        cur = self.frame()
        pf = preview.frame()
        if abs(cur.size.height - pf.size.height) > 0.5:
            cur.size.height = pf.size.height
            self.setFrame_(cur)
        self._reposition()

    @objc.python_method
    def _reposition(self):
        if self.preview is None:
            return
        cf = self.frame()
        pf = self.preview.frame()
        # Re-assert the original fixed width on every reposition. If
        # anything has stretched the preview to the container width
        # (which is what causes the "preview jumps to the left after
        # a settings change" symptom), this snaps it back.
        width = self.fixed_preview_width
        height = pf.size.height if pf.size.height > 0 else self.fixed_preview_width
        new_x = max(0, (cf.size.width - width) / 2)
        self.preview.setFrame_(((new_x, 0), (width, height)))

    @objc.python_method
    def _fit_to_preview(self):
        """Called by PreviewVideoWidget when it has changed height in
        response to a new camera aspect ratio. Adjusts our own height
        to match and notifies the VerticalBoxView parent to re-lay-out."""
        if self.preview is None:
            return
        cf = self.frame()
        pf = self.preview.frame()
        if abs(cf.size.height - pf.size.height) > 0.5:
            cf.size.height = pf.size.height
            self.setFrame_(cf)
            sv = self.superview()
            if sv is not None and hasattr(sv, 'relayout'):
                try:
                    sv.relayout()
                except Exception:
                    pass
        self._reposition()

    def resizeSubviewsWithOldSize_(self, oldSize):
        # VerticalBoxView gives us the full section width; re-center the
        # preview each time.
        self._reposition()

    def setFrame_(self, frame):
        # AppKit may resize us (e.g. when VerticalBoxView relayouts on
        # any settings change); after the size lands, re-center the
        # preview unconditionally instead of trusting the autoresizing
        # mask to keep it put across all the indirect paths that touch
        # frames during a settings notification.
        objc.super(CenteredPreviewContainer, self).setFrame_(frame)
        self._reposition()


@implementer(IObserver)
class VideoMirrorController(NSWindowController):
    """A singleton-ish floating window that mirrors the local camera.

    Lifecycle:
        - ``toggle()`` shows the window if hidden, hides it if visible.
        - When shown, the widget's producer is set to the local camera so
          frames flow. Mirroring is automatic in VideoWidget.
        - When hidden (window closed), the producer is unset so the
          camera is released for other consumers.
    """

    video_widget = None
    _notification_center_attached = False

    def init(self):
        self = objc.super(VideoMirrorController, self).init()
        if self is None:
            return None

        # 16:9 default; the user can resize, and VideoWidget letterboxes
        # whatever the actual camera produces.
        initial_rect = NSMakeRect(0, 0, 480, 270)
        style = (NSTitledWindowMask | NSClosableWindowMask |
                 NSMiniaturizableWindowMask | NSResizableWindowMask)

        window = NSWindow.alloc().\
            initWithContentRect_styleMask_backing_defer_(
                initial_rect, style, NSBackingStoreBuffered, False)
        window.setTitle_(NSLocalizedString("Video Mirror", "Window title"))
        window.setReleasedWhenClosed_(False)
        window.setMinSize_(NSMakeSize(160, 90))
        window.setDelegate_(self)
        window.center()

        content = window.contentView()
        bounds = content.bounds()
        widget = VideoWidget.alloc().initWithFrame_(bounds)
        widget.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(widget)
        self.video_widget = widget

        self.setWindow_(window)

        # Pick up camera switches so the mirror always reflects the
        # currently-selected video device.
        try:
            NotificationCenter().add_observer(self, name="VideoDeviceDidChangeCamera")
            self._notification_center_attached = True
        except Exception:
            pass

        return self

    # ----- public API -----

    @objc.python_method
    def show(self):
        if self.video_widget is not None:
            try:
                self.video_widget.setProducer(SIPApplication.video_device.producer)
            except Exception as e:
                BlinkLogger().log_info(
                    "VideoMirrorController: could not start camera: %s" % e)
        win = self.window()
        if win is not None:
            win.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)

    @objc.python_method
    def hide(self):
        if self.video_widget is not None:
            try:
                self.video_widget.setProducer(None)
            except Exception:
                pass
        win = self.window()
        if win is not None and win.isVisible():
            win.orderOut_(None)

    @objc.python_method
    def toggle(self):
        win = self.window()
        if win is not None and win.isVisible():
            self.hide()
        else:
            self.show()

    @objc.python_method
    def is_visible(self):
        win = self.window()
        return bool(win is not None and win.isVisible())

    # ----- NSWindowDelegate -----

    def windowWillClose_(self, notification):
        # Releasing the producer when the user closes the window stops
        # the camera if no other consumer is using it.
        if self.video_widget is not None:
            try:
                self.video_widget.setProducer(None)
            except Exception:
                pass

    # ----- notification handler -----

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_VideoDeviceDidChangeCamera(self, notification):
        if self.video_widget is None or not self.is_visible():
            return
        try:
            new_producer = notification.data.new_camera
        except Exception:
            new_producer = SIPApplication.video_device.producer
        try:
            self.video_widget.setProducer(new_producer)
        except Exception as e:
            BlinkLogger().log_info(
                "VideoMirrorController: failed to switch camera: %s" % e)

    def dealloc(self):
        if self._notification_center_attached:
            try:
                NotificationCenter().discard_observer(self, name="VideoDeviceDidChangeCamera")
            except Exception:
                pass
        objc.super(VideoMirrorController, self).dealloc()
