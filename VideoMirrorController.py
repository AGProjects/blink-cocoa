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
from Quartz import kCAGravityResizeAspectFill

from VideoWindowController import VideoWidget


class PreviewVideoWidget(VideoWidget):
    """A VideoWidget that stays at a FIXED frame size and ALWAYS uses
    the CGImage + CALayer.contentsGravity rendering path (i.e. the
    "CG fallback" the base VideoWidget falls back to when Metal isn't
    available).  Metal is intentionally bypassed here even when it's
    initialised process-wide.

    Why bypass Metal for the preview only:
    --------------------------------------
    The Metal path renders the texture into a CAMetalLayer's drawable
    with a per-frame quad scale (qx/qy) that mathematically letterboxes
    correctly when the drawable and the camera frame have steady
    dimensions.  But the preview's lifecycle in Preferences is hostile
    to that steady state:
       * setProducer(None) / setProducer(new) is called every time the
         user changes capture resolution.
       * pjsip's avf_dev emits 1-2 frames at the PREVIOUS resolution
         while the camera is mid-reconfigure.
       * The CAMetalLayer's drawableSize is resized each frame to the
         view bounds * backingScaleFactor; when bounds and frame size
         disagree by even one update cycle, the next quad scale we
         compute is wrong for the frame we're about to draw, and the
         picture briefly looks stretched / compressed before settling.
    The CGImage fallback dodges all of this: pjsip's BGRA frame is
    wrapped in a CGImage and assigned to a plain CALayer's contents;
    Core Animation then composites it into the layer's bounds with
    kCAGravityResizeAspect, which is computed entirely by the
    compositor based on the layer's current pixel dimensions vs the
    contents' pixel dimensions, every frame, atomically.  No transient
    mismatch is possible.

    The full-screen call window keeps Metal because it gets steady
    same-resolution frames once the call is established and benefits
    from Metal's lower per-frame CPU cost; only the preferences
    preview uses the CG path."""

    def makeBackingLayer(self):
        # Force a plain CALayer; the base class's makeBackingLayer
        # returns a CAMetalLayer when Metal is available, but we want
        # the CG fallback path so the layer's contentsGravity handles
        # scaling atomically per composited frame.
        # objc.super(VideoWidget, self) reaches NSView, whose default
        # makeBackingLayer returns a plain CALayer.
        return objc.super(VideoWidget, self).makeBackingLayer()

    @objc.python_method
    def _setup_video_layer(self):
        # Let the base class wire up the CALayer + CGImage rendering
        # path (it will set kCAGravityResizeAspect because makeBackingLayer
        # above returned a non-Metal CALayer).  Then override the layer's
        # contentsGravity to kCAGravityResizeAspectFill: the preview
        # thumbnail should show the camera feed FILLING the entire 4:3
        # box with the source's aspect preserved by cropping the
        # overflow, the same way FaceTime / Photo Booth / macOS Camera
        # render their own previews.  This eliminates the black bars
        # that kCAGravityResizeAspect would otherwise letterbox /
        # pillarbox inside the box whenever the source aspect differs
        # from 4:3 (i.e. when the user picks 720p or 1080p, both 16:9).
        objc.super(PreviewVideoWidget, self)._setup_video_layer()
        try:
            layer = self.layer()
            if layer is not None:
                layer.setContentsGravity_(kCAGravityResizeAspectFill)
                # Mask any source pixels that overflow the layer bounds
                # (we're cropping rather than letterboxing).
                layer.setMasksToBounds_(True)
        except Exception:
            pass

    @objc.python_method
    def setProducer(self, producer):
        # Reset the aspect_ratio cache so the FIRST frame from the new
        # producer triggers a redraw.  Without this, switching capture
        # resolution while the preview is visible can leave a stale
        # bitmap from the previous producer painted into the widget
        # until the very next frame whose aspect happens to differ
        # from the cached value.  Clearing the cache forces
        # _on_aspect_ratio_detected to fire on the next valid frame
        # regardless of whether the new producer's aspect ratio
        # matches the old one.
        self.aspect_ratio = None
        # Wipe the previous CGImage off the layer when the producer
        # detaches.  Without this the previous resolution's bitmap
        # stays painted into the (correctly-fitted) layer while pjsip
        # reconfigures the camera, and if avf_dev briefly emits a
        # frame at the OLD resolution before settling at the NEW one,
        # CALayer's kCAGravityResizeAspect dutifully composites that
        # old-aspect content with letterbox bars inside the new-size
        # box - which the user sees as "black bands inside the video"
        # that only go away when the entire section is rebuilt on a
        # tab switch.  Clearing contents to None means the layer
        # shows its background colour (transparent / parent black)
        # during the gap rather than a stale image.
        if producer is None:
            try:
                layer = self.layer()
                if layer is not None:
                    layer.setContents_(None)
                    self.setNeedsDisplay_(True)
            except Exception:
                pass
        return objc.super(PreviewVideoWidget, self).setProducer(producer)

    @objc.python_method
    def _on_aspect_ratio_detected(self, width, height):
        # Intentionally do NOT call super (which would propagate to a
        # delegate.init_aspect_ratio() callback) and do NOT touch
        # self.frame().  We want a stable widget size; the renderer
        # fits the video into the existing bounds with letterbox bars.
        # The redraw nudge ensures any frame that landed during the
        # cache-stale window (between setProducer(None) and
        # setProducer(new)) gets repainted at the right scale.
        self.setNeedsDisplay_(True)


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

    # Last container width we centered the preview at.  Used to skip
    # redundant setFrame_ calls when AppKit cascades layout updates
    # without actually changing our width (typical when a popup
    # expands, a checkbox toggles, or the section's vertical box
    # re-flows for an unrelated reason).  Without this, the
    # centering math (cf.size.width - width)/2 rounds to a slightly
    # different integer each time and the preview visibly shifts.
    _last_centered_width = -1.0

    @objc.python_method
    def _reposition(self, force=False):
        if self.preview is None:
            return
        cf = self.frame()
        # No-op when the container width hasn't actually changed.
        if not force and abs(cf.size.width - self._last_centered_width) < 0.5:
            return
        pf = self.preview.frame()
        # Re-assert the original fixed width on every reposition. If
        # anything has stretched the preview to the container width
        # (which is what causes the "preview jumps to the left after
        # a settings change" symptom), this snaps it back.
        width = self.fixed_preview_width
        height = pf.size.height if pf.size.height > 0 else self.fixed_preview_width
        new_x = max(0, (cf.size.width - width) / 2)
        self.preview.setFrame_(((new_x, 0), (width, height)))
        self._last_centered_width = cf.size.width

    def viewDidMoveToWindow(self):
        # The preview's autoresizing mask
        # (NSViewMinXMargin | NSViewMaxXMargin) is supposed to keep it
        # horizontally centered, but AppKit applies autoresizing only
        # when the SUPERVIEW changes size.  Tab toggles inside the
        # Preferences window often cascade a layout pass that doesn't
        # change OUR width but does call setFrame_ on the preview
        # itself with a slightly different origin (typically zero,
        # which snaps the preview to the left edge).  Force a fresh
        # _reposition once we're actually in a window so the preview
        # locks at the centered x BEFORE any of those cascades hit.
        objc.super(CenteredPreviewContainer, self).viewDidMoveToWindow()
        self._reposition(force=True)

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
