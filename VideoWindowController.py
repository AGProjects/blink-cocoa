# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import (NSAnimationContext,
                    NSApp,
                    NSRectFillUsingOperation,
                    NSCompositeSourceOver,
                    NSApplication,
                    NSGraphicsContext,
                    NSCalibratedRGBColorSpace,
                    NSAlphaFirstBitmapFormat,
                    NSWindow,
                    NSView,
                    NSOpenGLView,
                    NSOnState,
                    NSOffState,
                    NSMenu,
                    NSCursor,
                    NSMenuItem,
                    NSWindowController,
                    NSEventTrackingRunLoopMode,
                    NSFloatingWindowLevel,
                    NSNormalWindowLevel,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingMouseMoved,
                    NSTrackingActiveAlways,
                    NSFilenamesPboardType,
                    NSDragOperationNone,
                    NSDragOperationCopy,
                    NSDeviceIsScreen,
                    NSZeroPoint,
                    NSRectFill,
                    NSRightMouseUp,
                    NSSound,
                    NSViewMinXMargin,
                    NSViewMaxXMargin,
                    NSViewMinYMargin,
                    NSViewMaxYMargin,
                    NSViewHeightSizable,
                    NSViewMaxXMargin,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable,
                    NSWindowDocumentIconButton,
                    NSButton,
                    NSColorSpace,
                    NSFont,
                    NSFontAttributeName,
                    NSFontWeightMedium,
                    NSForegroundColorAttributeName,
                    NSImageSymbolConfiguration,
                    NSImageView,
                    NSImageScaleProportionallyUpOrDown,
                    NSImageAlignCenter,
                    NSTrackingInVisibleRect,
                    NSWindowAbove
                    )

from Foundation import (NSAttributedString,
                        NSBundle,
                        NSURL,
                        NSBezierPath,
                        NSUserDefaults,
                        NSData,
                        NSObject,
                        NSColor,
                        NSDictionary,
                        NSArray,
                        NSImage,
                        NSDate,
                        NSEvent,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSNotificationCenter,
                        NSLocalizedString,
                        NSTrackingArea,
                        NSZeroRect,
                        NSScreen,
                        NSMakeSize,
                        NSMakeRect,
                        NSPopUpButton,
                        NSTextField,
                        NSTask,
                        NSTaskDidTerminateNotification,
                        NSMakePoint,
                        NSPointInRect,
                        NSInsetRect,
                        NSWidth,
                        NSHeight,
                        NSDownloadsDirectory,
                        NSSearchPathForDirectoriesInDomains,
                        NSUserDomainMask,
                        NSWorkspace
                        )

from Foundation import mbFlipWindow

import datetime
import os
import objc
import time
import traceback
import unicodedata

from math import ceil, floor
from dateutil.tz import tzlocal


from application.notification import NotificationCenter
from resources import ApplicationData
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import VideoCamera, Engine, FrameBufferVideoRenderer

import VideoFrameSource
from sipsimple.threading import run_in_thread
from util import allocate_autorelease_pool, format_identity_to_string, call_in_gui_thread

from Quartz import CIImage, CIContext, kCIFormatARGB8, NSOpenGLPFAWindow, NSOpenGLPFAAccelerated, NSOpenGLPFADoubleBuffer, NSOpenGLPixelFormat, kCGEventMouseMoved, kCGEventSourceStateHIDSystemState, CGColorCreateGenericRGB
# Phase 1 renderer: CGImage + CALayer.contents (kept as a fallback).
from Quartz import (CGImageCreate,
                    CGColorSpaceCreateDeviceRGB,
                    CGDataProviderCreateWithCFData,
                    kCGBitmapByteOrder32Big,
                    kCGImageAlphaNoneSkipFirst,
                    kCGRenderingIntentDefault,
                    kCAGravityResizeAspect,
                    CGAffineTransformMake,
                    CGAffineTransformIdentity)

# Metal renderer: CAMetalLayer + a tiny vertex/fragment shader that samples
# the camera bytes (uploaded into a Metal texture) onto an aspect-fit quad.
# This gives us explicit, deterministic control over every step: byte
# interpretation (handled via shader swizzle), aspect math (computed
# CPU-side, applied in the vertex shader), drawable resolution
# (set explicitly from view bounds * backingScaleFactor). No reliance on
# CALayer.contentsGravity or any other "do the right thing" black box.
import struct as _struct

_METAL_IMPORT_ERROR = None
try:
    import Metal
    try:
        from Quartz import CAMetalLayer
    except ImportError:
        from QuartzCore import CAMetalLayer
    _HAS_METAL = True
except ImportError as _e:
    Metal = None
    CAMetalLayer = None
    _HAS_METAL = False
    _METAL_IMPORT_ERROR = str(_e)
except Exception as _e:
    Metal = None
    CAMetalLayer = None
    _HAS_METAL = False
    _METAL_IMPORT_ERROR = str(_e)


_METAL_SHADER_SOURCE = """
#include <metal_stdlib>
using namespace metal;

struct VertexUniforms {
    float quad_x_scale;     // 1.0 = fills width; <1.0 = pillarbox on sides
    float quad_y_scale;     // 1.0 = fills height; <1.0 = letterbox top/bottom
};

struct VertexOut {
    float4 position [[position]];
    float2 uv;
};

vertex VertexOut vertex_main(uint vid [[vertex_id]],
                              constant VertexUniforms &u [[buffer(0)]]) {
    // Triangle strip: BL, BR, TL, TR in clip space.
    const float2 positions[4] = {
        float2(-1.0, -1.0), float2( 1.0, -1.0),
        float2(-1.0,  1.0), float2( 1.0,  1.0)
    };
    // Flip V so that the top of the texture maps to the top of the quad.
    const float2 uvs[4] = {
        float2(0.0, 1.0), float2(1.0, 1.0),
        float2(0.0, 0.0), float2(1.0, 0.0)
    };
    VertexOut out;
    out.position = float4(positions[vid].x * u.quad_x_scale,
                          positions[vid].y * u.quad_y_scale,
                          0.0, 1.0);
    out.uv = uvs[vid];
    return out;
}

fragment float4 fragment_main(VertexOut in [[stage_in]],
                              texture2d<float> tex [[texture(0)]]) {
    constexpr sampler s(filter::linear, address::clamp_to_edge);
    // Bytes in the texture were uploaded as BGRA8Unorm, but pjsip
    // actually produces them in ARGB order (A,R,G,B in memory). When
    // Metal samples BGRA8Unorm it returns float4(byte2, byte1, byte0,
    // byte3) = (G_actual, R_actual, A_actual, B_actual). We swizzle
    // here to undo that and discard the meaningless alpha byte.
    float4 c = tex.sample(s, in.uv);
    return float4(c.g, c.r, c.a, 1.0);
}
"""

# Lazily-initialised, process-wide Metal state. Many VideoWidgets can
# share one device/queue/pipeline; only the per-widget texture is unique.
_METAL_DEVICE = None
_METAL_COMMAND_QUEUE = None
_METAL_PIPELINE_STATE = None
_METAL_INIT_FAILED = False


def _init_metal_once():
    """Returns True if Metal is ready, False otherwise. Idempotent."""
    global _METAL_DEVICE, _METAL_COMMAND_QUEUE, _METAL_PIPELINE_STATE, _METAL_INIT_FAILED
    if _METAL_INIT_FAILED:
        return False
    if _METAL_DEVICE is not None:
        return True
    if not _HAS_METAL:
        _METAL_INIT_FAILED = True
        BlinkLogger().log_info(
            "Metal init skipped: Python Metal/CAMetalLayer module not "
            "available (%s); video falls back to the CGImage path." % (
                _METAL_IMPORT_ERROR or "unknown ImportError"))
        return False
    try:
        device = Metal.MTLCreateSystemDefaultDevice()
        if device is None:
            raise RuntimeError("no default Metal device")
        queue = device.newCommandQueue()
        if queue is None:
            raise RuntimeError("could not create Metal command queue")
        library, err = device.newLibraryWithSource_options_error_(
            _METAL_SHADER_SOURCE, None, None)
        if library is None:
            raise RuntimeError("shader compile failed: %s" % err)
        vfn = library.newFunctionWithName_("vertex_main")
        ffn = library.newFunctionWithName_("fragment_main")
        if vfn is None or ffn is None:
            raise RuntimeError("could not resolve shader functions")
        pdesc = Metal.MTLRenderPipelineDescriptor.alloc().init()
        pdesc.setVertexFunction_(vfn)
        pdesc.setFragmentFunction_(ffn)
        pdesc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(
            Metal.MTLPixelFormatBGRA8Unorm)
        pstate, err = device.newRenderPipelineStateWithDescriptor_error_(
            pdesc, None)
        if pstate is None:
            raise RuntimeError("pipeline state failed: %s" % err)
        _METAL_DEVICE = device
        _METAL_COMMAND_QUEUE = queue
        _METAL_PIPELINE_STATE = pstate
        return True
    except Exception as e:
        BlinkLogger().log_info("Metal init failed: %s; falling back to CGImage" % e)
        _METAL_INIT_FAILED = True
        return False

from MediaStream import STREAM_CONNECTED, STREAM_IDLE, STREAM_FAILED
from VideoLocalWindowController import VideoLocalWindowController
from SIPManager import SIPManager
from ZRTPAuthentication import ZRTPAuthentication

from util import run_in_gui_thread
from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer
from BlinkLogger import BlinkLogger

try:
    import ScreenPointer
    from ScreenPointerController import ScreenPointerManager, PointerEchoView
except ImportError:     # the remote pointer is not part of every target
    ScreenPointer = None
    ScreenPointerManager = None
    PointerEchoView = None

# How long the viewer's green "the peer drew it" ring stays up.
POINTER_ECHO_SECONDS = 0.9

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', b'diI')])

IDLE_TIME = 5

# NSWindowStyleMaskFullScreen
FULL_SCREEN_STYLE_MASK = 1 << 14
# How long a closing window waits for a full screen transition to report
# back before it is closed anyway.
FULL_SCREEN_TRANSITION_TIMEOUT = 5.0



class VideoWidget(NSView):
    _frame = None
    renderer = None
    # Live VideoFrameSource.Subscription when this widget is showing a
    # remote stream; None when it is showing the local camera (which
    # uses the private renderer above) or nothing at all.
    frame_subscription = None
    aspect_ratio = None
    # FPS tracker: count handle_frame() calls over a rolling 1-second
    # window. current_fps is the value the stats overlay reads.
    _fps_window_start = 0.0
    _fps_window_count = 0
    current_fps = 0

    def awakeFromNib(self):
        self.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
        self._setup_video_layer()

    def initWithFrame_(self, frameRect):
        self = objc.super(VideoWidget, self).initWithFrame_(frameRect)
        if self is None:
            return None
        self._setup_video_layer()
        return self

    def wantsUpdateLayer(self):
        # We drive the backing layer's contents (or its sample-buffer queue)
        # directly from handle_frame() rather than drawing in drawRect_.
        # Telling AppKit we want updateLayer keeps it from calling drawRect_.
        return True

    def updateLayer(self):
        # No-op: contents are set explicitly when a new frame arrives.
        pass

    def makeBackingLayer(self):
        # Use a CAMetalLayer if Metal is available. CAMetalLayer is the
        # right surface for direct GPU video rendering — it gives us
        # nextDrawable (display-synced) and lets us composite anything
        # into the layer via a render pipeline.
        if _HAS_METAL and _init_metal_once():
            try:
                layer = CAMetalLayer.alloc().init()
                layer.setDevice_(_METAL_DEVICE)
                layer.setPixelFormat_(Metal.MTLPixelFormatBGRA8Unorm)
                # We only render to the drawable; we never read it back.
                layer.setFramebufferOnly_(True)
                layer.setOpaque_(True)
                # Tag the drawable as sRGB-encoded.
                try:
                    from Quartz import CGColorSpaceCreateWithName
                    srgb = CGColorSpaceCreateWithName('kCGColorSpaceSRGB')
                    if srgb is not None:
                        layer.setColorspace_(srgb)
                except Exception as cs_err:
                    BlinkLogger().log_debug(
                        "CAMetalLayer colorspace not set: %s" % cs_err)
                return layer
            except Exception as e:
                BlinkLogger().log_info(
                    "CAMetalLayer creation failed (%s); falling back to "
                    "default CALayer." % e)
        return objc.super(VideoWidget, self).makeBackingLayer()

    @objc.python_method
    def _setup_video_layer(self):
        self.setWantsLayer_(True)
        layer = self.layer()
        if layer is None:
            return

        # Black background on the unfilled area, no implicit redisplay
        # on bounds change (next frame will refresh us).
        layer.setBackgroundColor_(CGColorCreateGenericRGB(0.0, 0.0, 0.0, 1.0))
        layer.setNeedsDisplayOnBoundsChange_(False)

        if _HAS_METAL and CAMetalLayer is not None and \
                isinstance(layer, CAMetalLayer) and _init_metal_once():
            # Metal path.
            self._metal_layer = layer
            self._metal_texture = None       # lazy-created sized to frame
            self._metal_tex_width = 0
            self._metal_tex_height = 0
            # Keep the drawable size in sync with the layer's pixel size.
            # The CAMetalLayer auto-resizes drawableSize when contentsScale
            # and bounds change, but only via setNeedsDisplay; we set it
            # explicitly each frame for safety.
            try:
                bsf = self.window().backingScaleFactor() if self.window() else 1.0
            except Exception:
                bsf = 1.0
            layer.setContentsScale_(bsf)
            # Re-assert colorspace AFTER the layer has been adopted by
            # the view (AppKit's adoption can replace properties set in
            # makeBackingLayer alone).
            try:
                from Quartz import CGColorSpaceCreateWithName
                srgb = CGColorSpaceCreateWithName('kCGColorSpaceSRGB')
                if srgb is not None:
                    layer.setColorspace_(srgb)
            except Exception as cs_err:
                BlinkLogger().log_debug(
                    "CAMetalLayer colorspace re-assert failed: %s" % cs_err)
            # Explicitly opt OUT of EDR.
            #
            # An earlier iteration set this to True on the theory that
            # the call window's fullScreenPrimary behaviour was forcing
            # the layer into the EDR compositor and tone-mapping our
            # SDR pixels down. macOS 26 Tahoe behaves the opposite
            # way: setting wantsEDR=True makes the compositor RESERVE
            # display headroom for HDR content we never produce, then
            # squeeze our [0,1] sRGB pixels into a fraction of SDR-
            # reference white to make room for that headroom — visibly
            # dim against the Preferences preview (whose host window
            # isn't fullScreenPrimary so it isn't EDR-tagged at all).
            # Setting wantsEDR=False explicitly tells the compositor
            # "this layer is SDR-only", which keeps our pixels at full
            # SDR-reference brightness regardless of the surrounding
            # window's EDR behaviour. Our BGRA8Unorm pixel format
            # can't carry HDR-range values anyway, so opting OUT loses
            # nothing and restores brightness parity with Preferences.
            try:
                layer.setWantsExtendedDynamicRangeContent_(False)
            except Exception:
                pass
            self._renderer_kind = 'metal'
        else:
            # CGImage fallback path. Letterbox via contentsGravity and
            # disable the implicit ~0.25s contents crossfade that would
            # otherwise smear video at 30 fps.
            layer.setContentsGravity_(kCAGravityResizeAspect)
            layer.setActions_({"contents": None})
            self._renderer_kind = 'cg'

        # If setProducer already ran (and figured out we're a self-view),
        # apply the mirror now that the layer is finally available.
        self._apply_self_view_mirror()

        # Renderer-kind trace demoted to debug — the info-log version
        # produced one line per widget mount (Preferences preview,
        # myVideoWidget, remoteVideoWidget) and cluttered every call
        # / settings visit. Still useful when investigating "video is
        # black on this machine" reports (tells you whether we picked
        # Metal or fell back to the CGImage path), so we keep it as
        # log_debug rather than removing it outright.
        try:
            BlinkLogger().log_debug(
                "VideoWidget %s renderer=%s" % (
                    type(self).__name__, self._renderer_kind))
        except Exception:
            pass

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse(self):
        return True

    def canBecomeKeyView(self):
        return True
    
    @objc.python_method
    def setProducer(self, producer):
        # Camera lifecycle trace. Logged at DEBUG level so it
        # doesn't clutter the normal log but is available when
        # tracking down "camera LED still on" reports by enabling
        # debug logging. Each acquire/release tells you exactly
        # which widget is holding the shared
        # SIPApplication.video_device producer; the LED turns off
        # once every widget has released it.
        try:
            widget_name = type(self).__name__
            if producer is None:
                BlinkLogger().log_debug(
                    "[camera] release: widget=%s id=0x%x"
                    % (widget_name, id(self)))
            else:
                BlinkLogger().log_debug(
                    "[camera] acquire: widget=%s id=0x%x producer=0x%x"
                    % (widget_name, id(self), id(producer)))
        except Exception:
            pass
        # Detect whether this widget is displaying the local camera so we
        # can mirror it horizontally like FaceTime / Zoom / Meet. This is
        # purely a display transform on our own layer; the remote side
        # always sees the un-mirrored frame.
        try:
            local_producer = SIPApplication.video_device.producer
        except Exception:
            local_producer = None
        self._is_self_view = (producer is not None and producer is local_producer)
        if self._is_self_view and ScreenPointer is not None:
            # A shared screen is shown the way it is sent: text must read.
            try:
                if ScreenPointer.is_screen_device(SIPApplication.video_device.real_name):
                    self._is_self_view = False
            except Exception:
                pass
        self._apply_self_view_mirror()

        # A remote stream is shared through VideoFrameSource rather than
        # rendered by a renderer this widget owns. sipsimple allows
        # exactly one consumer on a RemoteVideoStream, so that renderer
        # has to outlive any single widget: otherwise hiding, closing or
        # swapping this window takes the call recorder's frame feed down
        # with it. Local camera producers keep the per-widget renderer
        # below -- VideoCamera has a video tee, several renderers on it
        # are fine, and the preferences / mirror widgets rely on closing
        # their own.
        released_subscription = False
        if self.frame_subscription is not None and self.frame_subscription.producer is not producer:
            self.frame_subscription.release()
            self.frame_subscription = None
            released_subscription = True

        if producer is not None and not isinstance(producer, VideoCamera):
            if self.frame_subscription is not None:
                return False        # already subscribed to this producer
            if self.renderer is not None:
                # Was showing the local camera on its own renderer.
                try:
                    self.renderer.close()
                except Exception as e:
                    BlinkLogger().log_debug(
                        "VideoWidget.setProducer close() ignored: %s" % e)
                self.renderer = None
            subscription = VideoFrameSource.subscribe(producer, self.handle_frame)
            if subscription is None:
                return False
            self.frame_subscription = subscription
            return True

        if producer is None:
            if self.renderer is not None:
                # The underlying video device may already be torn down
                # (pjsip closes it briefly when settings.video.* changes).
                # Closing a renderer attached to a closed device raises
                # SIPCoreError; we don't care, the renderer is going
                # away anyway.
                try:
                    self.renderer.close()
                except Exception as e:
                    BlinkLogger().log_debug(
                        "VideoWidget.setProducer close() ignored: %s" % e)
                self.renderer = None
                return True
            return released_subscription
        else:
            if self.renderer is None:
                try:
                    self.renderer = FrameBufferVideoRenderer(self.handle_frame)
                except Exception as e:
                    # Camera is in an unusable state right now (e.g. mid
                    # resolution-change). Bail; we'll be called again when
                    # VideoDeviceDidChangeCamera fires.
                    BlinkLogger().log_info(
                        "VideoWidget.setProducer: cannot create renderer "
                        "right now (%s); will retry when the camera "
                        "stabilises." % e)
                    return False

        if self.renderer is not None and self.renderer.producer != producer:
            try:
                self.renderer.producer = producer
            except Exception as e:
                BlinkLogger().log_info(
                    "VideoWidget.setProducer: pjsip rejected the producer "
                    "(%s); dropping the renderer and waiting for the "
                    "camera to come back." % e)
                try:
                    self.renderer.close()
                except Exception:
                    pass
                self.renderer = None
                return False
            return True

        return False

    @objc.python_method
    def _on_aspect_ratio_detected(self, width, height):
        """Hook called when the camera's aspect ratio is first detected
        (or changes). Default implementation notifies the host delegate's
        ``init_aspect_ratio`` so the call window can resize itself to
        match the remote video. Subclasses can override to do something
        else entirely — the preferences-panel preview, for instance,
        resizes its own view to the camera aspect instead of touching
        any window."""
        if self.delegate and hasattr(self.delegate, 'init_aspect_ratio'):
            try:
                self.delegate.init_aspect_ratio(width, height)
            except Exception:
                pass

    @objc.python_method
    def _apply_self_view_mirror(self):
        # The user sees themselves the way a mirror would. Called from
        # setProducer (when we learn which producer we have), from
        # _setup_video_layer (in case the layer was created after
        # setProducer ran) and on every resize. Safe to call repeatedly.
        #
        # The Metal path mirrors the PIXELS, in the vertex shader (see
        # _render_metal_frame), and leaves the layer alone. Scaling the
        # layer itself by -1 is what threw the thumbnail out of the window:
        # a layer-backed view's layer is anchored at its bottom-left
        # corner, so the flip swings the whole picture one width to the
        # left of where the view is -- off the window entirely in a left
        # corner. The CGImage fallback has no shader, so there the flip is
        # kept and translated back by the width, which is why it has to be
        # redone whenever the size changes.
        layer = self.layer()
        if layer is None:
            return
        try:
            mirrored = getattr(self, '_is_self_view', False)
            if mirrored and getattr(self, '_renderer_kind', 'cg') != 'metal':
                width = self.bounds().size.width
                layer.setAffineTransform_(CGAffineTransformMake(-1.0, 0.0, 0.0, 1.0, width, 0.0))
            else:
                layer.setAffineTransform_(CGAffineTransformIdentity)
        except Exception as e:
            BlinkLogger().log_info(
                "VideoWidget mirror toggle failed: %s" % e)

    def setFrameSize_(self, size):
        objc.super(VideoWidget, self).setFrameSize_(size)
        if getattr(self, '_is_self_view', False):
            self._apply_self_view_mirror()

    def close(self):
        BlinkLogger().log_debug("Close %s" % self)
        self.setProducer(None)
        if self.frame_subscription is not None:
            self.frame_subscription.release()
            self.frame_subscription = None
        if self.renderer is not None:
            self.renderer.close()
            self.renderer = None
        self.removeFromSuperview()

    def dealloc(self):
        BlinkLogger().log_debug("Dealloc %s" % self)
        objc.super(VideoWidget, self).dealloc()

    def mouseDown_(self, event):
        if hasattr(self.delegate, "mouseDown_"):
            self.delegate.mouseDown_(event)

    def resetCursorRects(self):
        # Pointing at the peer's shared screen: say so with the cursor.
        try:
            delegate = self.delegate
        except Exception:
            delegate = None
        if getattr(delegate, 'pointer_mode', False) and getattr(delegate, 'videoView', None) is self:
            self.addCursorRect_cursor_(self.visibleRect(), NSCursor.pointingHandCursor())

    @property
    def delegate(self):
        if not self.window():
            return
    
        if NSApp.delegate().contactsWindowController.drawer.contentView().window() == self.window():
            delegate = NSApp.delegate().contactsWindowController.drawer.parentWindow().delegate()
        elif NSApp.delegate().chatWindowController.drawer.contentView().window() == self.window():
            delegate = NSApp.delegate().chatWindowController.drawer.parentWindow().delegate()
        else:
            delegate = self.window().delegate()
        return delegate

    def rightMouseDown_(self, event):
        if hasattr(self.delegate, "rightMouseDown_"):
            self.delegate.rightMouseDown_(event)

    def keyDown_(self, event):
        if hasattr(self.delegate, "keyDown_"):
            self.delegate.keyDown_(event)

    def mouseUp_(self, event):
        if hasattr(self.delegate, "mouseUp_"):
            self.delegate.mouseUp_(event)

    def mouseDragged_(self, event):
        if hasattr(self.delegate, "mouseDraggedView_"):
            self.delegate.mouseDraggedView_(event)

    @objc.python_method
    @run_in_gui_thread
    def handle_frame(self, frame):
        # Always count the frame for FPS, even if we're hidden or the
        # layer isn't ready yet — that way the overlay shows the true
        # receive rate, not the render rate.
        now = time.time()
        if self._fps_window_start == 0.0:
            self._fps_window_start = now
        self._fps_window_count += 1
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            self.current_fps = int(round(self._fps_window_count / elapsed))
            self._fps_window_start = now
            self._fps_window_count = 0

        if self.isHidden():
            return

        self._frame = frame

        aspect_ratio = floor((float(frame.width) / frame.height) * 100)/100
        if self.aspect_ratio != aspect_ratio:
            self.aspect_ratio = aspect_ratio
            self._on_aspect_ratio_detected(frame.width, frame.height)

        # During a fullscreen transition the window geometry is in flux;
        # skipping the frame avoids flicker while the system animates.
        if self.delegate and getattr(self.delegate, 'full_screen_in_progress', False):
            return

        layer = self.layer()
        if layer is None:
            return

        # Dispatch to whichever renderer this widget was set up with.
        kind = getattr(self, '_renderer_kind', 'cg')

        if kind == 'metal' and self._metal_layer is not None:
            try:
                self._render_metal_frame(frame)
            except Exception as e:
                # Drop this frame; do NOT switch _renderer_kind. The
                # backing layer is CAMetalLayer and calling setContents_
                # on it (which the CG fallback path would do) is
                # undefined behaviour — likely showing garbage. Retry
                # the Metal path on the next frame; one frame dropped
                # at 30 fps is invisible.
                BlinkLogger().log_info(
                    "Metal render failed (frame dropped): %s" % e)
            return

        # Fallback: CGImage on CALayer.
        cgimage = self._cgimage_from_frame(frame)
        if cgimage is not None:
            layer.setContents_(cgimage)

    # ----- Metal path ------------------------------------------------------

    @objc.python_method
    def _render_metal_frame(self, frame):
        device = _METAL_DEVICE
        queue = _METAL_COMMAND_QUEUE
        pipeline = _METAL_PIPELINE_STATE
        layer = self._metal_layer
        if device is None or queue is None or pipeline is None or layer is None:
            return

        # Keep the drawable in lockstep with the view's pixel size. Doing
        # this every frame is cheap and avoids any "drawable too small /
        # too large" mismatch when the window resizes.
        try:
            bsf = self.window().backingScaleFactor() if self.window() else 1.0
        except Exception:
            bsf = 1.0
        bounds = self.bounds()
        target_w = max(1.0, float(bounds.size.width) * bsf)
        target_h = max(1.0, float(bounds.size.height) * bsf)
        cur = layer.drawableSize()
        if abs(cur.width - target_w) > 0.5 or abs(cur.height - target_h) > 0.5:
            layer.setDrawableSize_((target_w, target_h))

        # (Re)create the source texture only when the camera resolution
        # changes — typically once per call, never per-frame.
        if (self._metal_texture is None
                or self._metal_tex_width != frame.width
                or self._metal_tex_height != frame.height):
            desc = Metal.MTLTextureDescriptor.\
                texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                    Metal.MTLPixelFormatBGRA8Unorm,
                    frame.width, frame.height, False)
            desc.setUsage_(Metal.MTLTextureUsageShaderRead)
            tex = device.newTextureWithDescriptor_(desc)
            if tex is None:
                BlinkLogger().log_info(
                    "Metal newTextureWithDescriptor returned nil")
                return
            self._metal_texture = tex
            self._metal_tex_width = frame.width
            self._metal_tex_height = frame.height

        # Upload this frame's pixels into the texture. pjsip's frame
        # buffers are tightly packed (width * 4 bytes per row) for
        # both the local camera (via avf_dev) and the remote decoder
        # output. Using len(data) // height as the stride is wrong if
        # the buffer has *tail* padding (data_len > width*4*height with
        # tight rows) — we'd overshoot by ~20 bytes per row and the
        # image becomes diagonally sheared with broken lines. Stick to
        # the tight assumption and log a warning if data_len doesn't
        # match, so any real row-padded case is at least visible.
        data_len = len(frame.data)
        bpr = frame.width * 4
        expected = bpr * frame.height
        if data_len != expected and not getattr(
                self, '_stride_warning_logged', False):
            self._stride_warning_logged = True
            BlinkLogger().log_info(
                "VideoWidget %s stride note: %dx%d frame, "
                "data_len=%d, expected %d (using bpr=%d)" % (
                    type(self).__name__,
                    frame.width, frame.height, data_len, expected, bpr))
        region = Metal.MTLRegionMake2D(0, 0, frame.width, frame.height)
        self._metal_texture.\
            replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
                region, 0, frame.data, bpr)

        # Compute the aspect-fit quad scale: how much of the drawable the
        # texture's natural shape should cover, with the rest left as the
        # clear color (black bars).
        dw = float(layer.drawableSize().width)
        dh = float(layer.drawableSize().height)
        if dw <= 0 or dh <= 0:
            return
        view_aspect = dw / dh
        tex_aspect = float(frame.width) / float(frame.height)
        if tex_aspect > view_aspect:
            qx, qy = 1.0, view_aspect / tex_aspect    # bars top/bottom
        else:
            qx, qy = tex_aspect / view_aspect, 1.0    # bars left/right

        drawable = layer.nextDrawable()
        if drawable is None:
            return

        # Build the render pass: clear to opaque black, then draw the quad.
        pass_desc = Metal.MTLRenderPassDescriptor.alloc().init()
        att = pass_desc.colorAttachments().objectAtIndexedSubscript_(0)
        att.setTexture_(drawable.texture())
        att.setLoadAction_(Metal.MTLLoadActionClear)
        att.setStoreAction_(Metal.MTLStoreActionStore)
        att.setClearColor_(Metal.MTLClearColorMake(0.0, 0.0, 0.0, 1.0))

        cmd = queue.commandBuffer()
        enc = cmd.renderCommandEncoderWithDescriptor_(pass_desc)
        enc.setRenderPipelineState_(pipeline)
        enc.setFragmentTexture_atIndex_(self._metal_texture, 0)

        if getattr(self, '_is_self_view', False):
            qx = -qx        # mirror the self view; the layer is not flipped
        uniforms = _struct.pack('ff', qx, qy)
        enc.setVertexBytes_length_atIndex_(uniforms, len(uniforms), 0)

        enc.drawPrimitives_vertexStart_vertexCount_(
            Metal.MTLPrimitiveTypeTriangleStrip, 0, 4)
        enc.endEncoding()
        cmd.presentDrawable_(drawable)
        cmd.commit()

    # ----- CALayer + CGImage fallback path ---------------------------------

    @objc.python_method
    def _cgimage_from_frame(self, frame):
        # pjsip's framebuffer device produces PJMEDIA_FORMAT_ARGB on Darwin:
        # byte order is A, R, G, B per pixel. The alpha byte carries no real
        # alpha data, so we mark it as "skip first" and let CGImage interpret
        # the remaining three bytes as RGB.
        #
        # avf_dev.m sets frame.size = bytesPerRow * height, where bytesPerRow
        # comes from CVPixelBufferGetBytesPerRow(). CoreVideo can pad rows
        # for alignment, so deriving bytesPerRow from the actual buffer
        # length is safer than assuming width * 4.
        if frame.height <= 0:
            return None
        data_len = len(frame.data)
        # Tight packing — see _render_metal_frame for the reasoning.
        bytes_per_row = frame.width * 4

        nsdata = NSData.dataWithBytes_length_(frame.data, data_len)
        provider = CGDataProviderCreateWithCFData(nsdata)
        if provider is None:
            return None
        # pjsip delivers sRGB-encoded BGRA bytes. Tagging the CGImage
        # with CGColorSpaceCreateDeviceRGB() makes CoreGraphics treat
        # them as device-native (Display P3 on Apple Silicon), which
        # then triggers an sRGB->P3 mapping at display time and the
        # whole picture renders perceptibly dim / desaturated on
        # wide-gamut displays. Pin to sRGB explicitly so CoreGraphics
        # knows the bytes are sRGB-encoded and maps to the display
        # gamut correctly.
        try:
            from Quartz import CGColorSpaceCreateWithName
            colorspace = CGColorSpaceCreateWithName('kCGColorSpaceSRGB')
            if colorspace is None:
                colorspace = CGColorSpaceCreateDeviceRGB()
        except Exception:
            colorspace = CGColorSpaceCreateDeviceRGB()
        bitmap_info = kCGBitmapByteOrder32Big | kCGImageAlphaNoneSkipFirst
        return CGImageCreate(
            frame.width,
            frame.height,
            8,                  # bits per component
            32,                 # bits per pixel
            bytes_per_row,
            colorspace,
            bitmap_info,
            provider,
            None,               # decode array
            False,              # shouldInterpolate
            kCGRenderingIntentDefault,
        )

    @objc.python_method
    def show(self):
        BlinkLogger().log_debug('Show %s' % self)
        self.setHidden_(False)

    @objc.python_method
    def toggle(self):
        if not self.isHidden():
            self.hide()
        else:
            self.show()
    
    @objc.python_method
    def hide(self):
        BlinkLogger().log_debug('Hide %s' % self)
        self.setHidden_(True)


class remoteVideoWidget(VideoWidget):
    def draggingEntered_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            pboard = sender.draggingPasteboard()
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        return NSDragOperationNone
    
    def prepareForDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return False
            return True
        return False
    
    def performDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            filenames = pboard.propertyListForType_(NSFilenamesPboardType)
            return self.sendFiles(filenames)
        return False
    
    @objc.python_method
    def sendFiles(self, fnames):
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file) or os.path.isdir(file)]
        if filenames and hasattr(self.delegate, "sessionController"):
            self.delegate.sessionController.sessionControllersManager.send_files_to_contact(self.delegate.sessionController.account, self.delegate.sessionController.target_uri, filenames)
            return True
        return False

# The local camera thumbnail over the remote picture.
MY_VIDEO_CORNER_KEY = "MyVideoCorner"
MY_VIDEO_SCALE_KEY = "MyVideoScale"
MY_VIDEO_MARGIN = 10.0
# Width as a fraction of the window's, until the user drags it to another.
MY_VIDEO_DEFAULT_SCALE = 0.22
MY_VIDEO_MIN_W = 96.0
# Never more than this share of the window in either direction.
MY_VIDEO_MAX_FRACTION = 0.5
MY_VIDEO_RESIZE_GRIP = 18.0
# Frame shapes a real camera produces; anything else is start-up noise.
MY_VIDEO_MIN_ASPECT = 0.5
MY_VIDEO_MAX_ASPECT = 2.5


class myVideoWidget(VideoWidget):
    auto_rotate_menu_enabled = True
    is_dragging = False
    allow_drag = True

    @objc.python_method
    def _setup_video_layer(self):
        # Inherit the base VideoWidget renderer setup, then layer on the
        # PiP look: rounded corners, a hairline border to read against
        # the remote video behind it.
        VideoWidget._setup_video_layer(self)
        layer = self.layer()
        if layer is None:
            return
        try:
            layer.setCornerRadius_(10.0)
            layer.setMasksToBounds_(True)
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(
                CGColorCreateGenericRGB(1.0, 1.0, 1.0, 0.35))
        except Exception as e:
            BlinkLogger().log_debug(
                "myVideoWidget decoration setup failed: %s" % e)

    # What the camera last reported, width over height. Until the first frame
    # the thumbnail is laid out as 16:9.
    camera_aspect = 16.0 / 9.0
    # None, 'move' or 'resize' while the mouse is down on the thumbnail.
    drag_mode = None
    drag_start_point = None
    drag_start_frame = None

    @objc.python_method
    def _on_aspect_ratio_detected(self, width, height):
        """Re-shape the thumbnail to the local camera.

        Never the call window's init_aspect_ratio: that belongs to the
        remote picture. A camera switch reports a few frames of whatever
        the device is doing while it starts, and a thumbnail sized off a
        1x1 or a 2000x10 frame is how it used to end up off screen, so
        anything implausible is ignored and the next real frame decides.
        """
        if width < 16 or height < 16:
            return
        aspect = float(width) / float(height)
        if not (MY_VIDEO_MIN_ASPECT <= aspect <= MY_VIDEO_MAX_ASPECT):
            return
        self.camera_aspect = aspect
        self.layoutInSuperview()

    # -- placement -----------------------------------------------------------
    #
    # The thumbnail has no position of its own. It is always derived from
    # three things -- the corner the user chose, the size they chose (as a
    # fraction of the window's width) and the camera's shape -- and laid
    # out again whenever any of them or the window changes. The old way
    # kept an origin and nudged it towards four placeholder views; every
    # path that moved it without re-deriving (a camera switch, a click
    # without a drag, full screen) could leave it outside the window.

    @objc.python_method
    def corner(self):
        corner = NSUserDefaults.standardUserDefaults().stringForKey_(MY_VIDEO_CORNER_KEY)
        return corner if corner in ('TL', 'TR', 'BL', 'BR') else 'TR'

    @objc.python_method
    def scale(self):
        value = NSUserDefaults.standardUserDefaults().floatForKey_(MY_VIDEO_SCALE_KEY)
        return value if value > 0 else MY_VIDEO_DEFAULT_SCALE

    @objc.python_method
    def targetFrame(self, corner=None, scale=None):
        container = self.superview()
        if container is None:
            return self.frame()
        bounds = container.bounds()
        W, H = bounds.size.width, bounds.size.height
        corner = corner or self.corner()
        scale = scale if scale is not None else self.scale()
        aspect = self.camera_aspect or (16.0 / 9.0)

        w = min(max(W * scale, MY_VIDEO_MIN_W), W * MY_VIDEO_MAX_FRACTION)
        h = w / aspect
        if h > H * MY_VIDEO_MAX_FRACTION:
            h = H * MY_VIDEO_MAX_FRACTION
            w = h * aspect

        delegate = self.window().delegate() if self.window() else None
        top = MY_VIDEO_MARGIN
        if delegate is not None and hasattr(delegate, 'myVideoTopInset'):
            top = delegate.myVideoTopInset()

        x = MY_VIDEO_MARGIN if corner[1] == 'L' else W - w - MY_VIDEO_MARGIN
        if corner[0] == 'T':
            y = H - h - top
        else:
            y = MY_VIDEO_MARGIN
            if delegate is not None and hasattr(delegate, 'myVideoBottomInset'):
                y = delegate.myVideoBottomInset(x, w)
        # Whatever the insets asked for, the thumbnail stays inside.
        x = min(max(x, 0.0), max(W - w, 0.0))
        y = min(max(y, 0.0), max(H - h, 0.0))
        return NSMakeRect(floor(x), floor(y), floor(w), floor(h))

    @objc.python_method
    def layoutInSuperview(self, animate=False):
        if self.drag_mode is not None:
            return          # the mouse owns the frame until it lets go
        frame = self.targetFrame()
        if animate:
            NSAnimationContext.beginGrouping()
            try:
                NSAnimationContext.currentContext().setDuration_(0.18)
                self.animator().setFrame_(frame)
            finally:
                NSAnimationContext.endGrouping()
        else:
            self.setFrame_(frame)

    def resizeWithOldSuperviewSize_(self, old_size):
        # Scales and re-anchors with the window, full screen included.
        self.layoutInSuperview()

    # -- moving and resizing -------------------------------------------------

    @objc.python_method
    def _gripRect(self):
        """The corner that points into the picture: drag it to resize."""
        bounds = self.bounds()
        g = MY_VIDEO_RESIZE_GRIP
        corner = self.corner()
        # Opposite the anchored corner. Not flipped: y grows upwards.
        x = bounds.size.width - g if corner[1] == 'L' else 0.0
        y = 0.0 if corner[0] == 'T' else bounds.size.height - g
        return NSMakeRect(x, y, g, g)

    def resetCursorRects(self):
        try:
            self.addCursorRect_cursor_(self._gripRect(), NSCursor.crosshairCursor())
        except Exception:
            pass

    def mouseDown_(self, event):
        if not self.allow_drag:
            return
        container = self.superview()
        if container is None:
            return
        local = self.convertPoint_fromView_(event.locationInWindow(), None)
        grip = self._gripRect()
        in_grip = (grip.origin.x <= local.x <= grip.origin.x + grip.size.width
                   and grip.origin.y <= local.y <= grip.origin.y + grip.size.height)
        self.drag_mode = 'resize' if in_grip else 'move'
        self.drag_start_point = container.convertPoint_fromView_(event.locationInWindow(), None)
        self.drag_start_frame = self.frame()
        self.is_dragging = False

    def mouseDragged_(self, event):
        container = self.superview()
        if self.drag_mode is None or container is None:
            return
        self.is_dragging = True
        point = container.convertPoint_fromView_(event.locationInWindow(), None)
        start = self.drag_start_frame
        bounds = container.bounds()

        if self.drag_mode == 'move':
            x = start.origin.x + (point.x - self.drag_start_point.x)
            y = start.origin.y + (point.y - self.drag_start_point.y)
            x = min(max(x, 0.0), max(bounds.size.width - start.size.width, 0.0))
            y = min(max(y, 0.0), max(bounds.size.height - start.size.height, 0.0))
            self.setFrameOrigin_(NSMakePoint(x, y))
            return

        # Resize about the anchored corner: the one in the window's corner.
        corner = self.corner()
        anchor_x = start.origin.x if corner[1] == 'L' else start.origin.x + start.size.width
        anchor_y = start.origin.y if corner[0] == 'B' else start.origin.y + start.size.height
        aspect = self.camera_aspect or (16.0 / 9.0)
        wanted = max(abs(point.x - anchor_x), abs(point.y - anchor_y) * aspect)
        width = bounds.size.width or 1.0
        self.setFrame_(self.targetFrame(corner=corner, scale=wanted / width))

    def mouseUp_(self, event):
        self.endDrag()

    @objc.python_method
    def endDrag(self):
        """Settle the thumbnail after the mouse lets go.

        A move lands in whichever corner the thumbnail's centre is nearest;
        a resize keeps its size as a fraction of the window. A click that
        never dragged changes nothing -- it used to send the thumbnail to
        an origin that had never been set.
        """
        mode, dragged = self.drag_mode, self.is_dragging
        self.drag_mode = None
        self.is_dragging = False
        container = self.superview()
        if not dragged or container is None:
            return
        defaults = NSUserDefaults.standardUserDefaults()
        bounds = container.bounds()
        frame = self.frame()
        if mode == 'move':
            cx = frame.origin.x + frame.size.width / 2.0
            cy = frame.origin.y + frame.size.height / 2.0
            corner = ('B' if cy < bounds.size.height / 2.0 else 'T') + \
                     ('L' if cx < bounds.size.width / 2.0 else 'R')
            defaults.setValue_forKey_(corner, MY_VIDEO_CORNER_KEY)
        elif mode == 'resize' and bounds.size.width > 0:
            defaults.setFloat_forKey_(frame.size.width / bounds.size.width, MY_VIDEO_SCALE_KEY)
        self.window().invalidateCursorRectsForView_(self)
        self.layoutInSuperview(animate=True)

    def rightMouseDown_(self, event):
        if self.isHidden():
            return

        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
            self.window().graphicsContext(), 0, 1, 0)

        videoDevicesMenu = NSMenu.alloc().init()
        lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Select Video Camera", "Menu item"), "", "")
        lastItem.setEnabled_(False)
        videoDevicesMenu.addItem_(NSMenuItem.separatorItem())

        i = 0
        for item in NSApp.delegate().video_devices:
            if item not in (None, 'system_default'):
                i += 1
            lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(item, "changeVideoDevice:", "")
            lastItem.setRepresentedObject_(item)
            if SIPApplication.video_device.real_name == item:
                lastItem.setState_(NSOnState)

        if i > 1 and self.auto_rotate_menu_enabled:
            videoDevicesMenu.addItem_(NSMenuItem.separatorItem())
            settings = SIPSimpleSettings()
            lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Auto Rotate Cameras", "Menu item"), "toggleAutoRotate:", "")
            lastItem.setState_(NSOnState if settings.video.auto_rotate_cameras else NSOffState)

        NSMenu.popUpContextMenu_withEvent_forView_(videoDevicesMenu, event, self)

    def toggleAutoRotate_(self, sender):
        settings = SIPSimpleSettings()
        settings.video.auto_rotate_cameras = not settings.video.auto_rotate_cameras
        settings.save()

    def changeVideoDevice_(self, sender):
        settings = SIPSimpleSettings()
        BlinkLogger().log_info('Switching to %s video camera' % sender.representedObject())
        settings.video.device = sender.representedObject()
        settings.save()


# The call bar floating over the video: one translucent capsule split into
# segments, each an icon over a short label -- the same row of call controls
# the contacts window has at its foot, but drawn to sit on top of a picture
# rather than on window chrome.
#
# Built in code, not in the xib. Its buttons come and go with what the call
# can do (chat with the MSRP setting, hold never for video), and a segmented
# row with a hole where a hidden button was is exactly what fixed xib frames
# produce. The bar re-packs itself whenever a segment is shown or hidden,
# and drops the labels when the window is too narrow to carry them.
#
# The icons are SF Symbols drawn white, so they read against any picture;
# a segment is tinted only when its colour means something -- muted, held,
# recording -- and hanging up is the one segment with a red fill.
CALL_BAR_BOTTOM = 20.0
CALL_BAR_HEIGHT = 50.0
CALL_BAR_COMPACT_HEIGHT = 38.0
CALL_BAR_PADDING = 4.0
CALL_BAR_RADIUS = 14.0
CALL_BAR_SEGMENT_MIN_W = 58.0
CALL_BAR_SEGMENT_COMPACT_W = 40.0
CALL_BAR_ICON_PT = 16.0
CALL_BAR_LABEL_PT = 10.5
# Whatever else is in the window keeps this much air from the bar's ends
# before the bar gives up its labels.
CALL_BAR_MARGIN = 24.0


def _call_bar_white(alpha):
    return NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha)


_call_bar_symbol_cache = {}

# The box an icon is fitted into, whatever size the symbol comes out at.
CALL_BAR_ICON_BOX_W = 24.0
CALL_BAR_ICON_BOX_H = 18.0
CALL_BAR_ICON_GAP = 2.0


def call_bar_symbol(name):
    """The SF Symbol as a template image, or None if there is no such symbol.

    Left a template on purpose and coloured by the NSImageView that shows
    it (contentTintColor). Rendering the colour into the image by hand came
    out as a speck on the bar; the image view is the path AppKit itself
    uses for tinted symbols.
    """
    if name in _call_bar_symbol_cache:
        return _call_bar_symbol_cache[name]
    image = None
    try:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if image is None:
            BlinkLogger().log_info('Call bar: there is no SF Symbol named %s' % name)
        else:
            config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                CALL_BAR_ICON_PT, NSFontWeightMedium)
            image = image.imageWithSymbolConfiguration_(config) or image
            image.setTemplate_(True)
    except Exception as e:
        BlinkLogger().log_info('Call bar: cannot load symbol %s: %s' % (name, e))
        image = None
    _call_bar_symbol_cache[name] = image
    return image


class VideoCallBarSegment(NSButton):
    """One control on the call bar: an icon over a label.

    The well (hover, press, the red of End) and the label are drawn here;
    the icon is an NSImageView subview, which is what tints a symbol
    reliably. The cell's own rendering has no idea what a translucent bar
    looks like and would put a light bezel on a dark video.
    """
    symbol_name = None
    label = ''
    tint = None
    destructive = False
    hovering = False
    hover_area = None
    iconView = None

    @objc.python_method
    def configure(self, symbol_name, label, tint=None):
        """Change what the segment shows. The bar re-packs if the width moved."""
        changed = (label or '') != self.label
        self.symbol_name = symbol_name
        self.label = label or ''
        self.tint = tint
        self.setAccessibilityLabel_(self.label)
        if self.iconView is not None:
            self.iconView.setImage_(call_bar_symbol(symbol_name) if symbol_name else None)
        self._applyTint()
        self.layoutContent()
        self.setNeedsDisplay_(True)
        if changed:
            self._retile()

    @objc.python_method
    def _applyTint(self):
        if self.iconView is None:
            return
        alpha = 1.0 if self.isEnabled() else 0.35
        self.iconView.setContentTintColor_(
            (self.tint or NSColor.whiteColor()).colorWithAlphaComponent_(alpha))

    @objc.python_method
    def _compact(self):
        bar = self.superview()
        return isinstance(bar, VideoCallBar) and bar.compact

    @objc.python_method
    def _labelFont(self):
        return NSFont.systemFontOfSize_weight_(CALL_BAR_LABEL_PT, NSFontWeightMedium)

    @objc.python_method
    def _labelHeight(self):
        font = self._labelFont()
        return float(ceil(font.ascender() - font.descender()))

    @objc.python_method
    def _geometry(self):
        """(icon frame, label baseline-box y) for the current bounds.

        Icon on top, label under it, the pair centred. NSButton is a flipped
        view -- y grows DOWN -- so which end is "top" is asked, not assumed.
        """
        bounds = self.bounds()
        width, height = bounds.size.width, bounds.size.height
        icon_x = floor((width - CALL_BAR_ICON_BOX_W) / 2.0)
        if self._compact():
            icon_y = floor((height - CALL_BAR_ICON_BOX_H) / 2.0)
            return NSMakeRect(icon_x, icon_y, CALL_BAR_ICON_BOX_W, CALL_BAR_ICON_BOX_H), None
        label_h = self._labelHeight()
        block = CALL_BAR_ICON_BOX_H + CALL_BAR_ICON_GAP + label_h
        margin = floor((height - block) / 2.0)
        if self.isFlipped():
            icon_y = margin
            label_y = margin + CALL_BAR_ICON_BOX_H + CALL_BAR_ICON_GAP
        else:
            label_y = margin
            icon_y = margin + label_h + CALL_BAR_ICON_GAP
        return NSMakeRect(icon_x, icon_y, CALL_BAR_ICON_BOX_W, CALL_BAR_ICON_BOX_H), label_y

    @objc.python_method
    def layoutContent(self):
        if self.iconView is not None:
            self.iconView.setFrame_(self._geometry()[0])

    @objc.python_method
    def preferredWidth(self, compact):
        if compact:
            return CALL_BAR_SEGMENT_COMPACT_W
        text = NSAttributedString.alloc().initWithString_attributes_(
            self.label, {NSFontAttributeName: self._labelFont()})
        return max(CALL_BAR_SEGMENT_MIN_W, floor(text.size().width) + 18.0)

    @objc.python_method
    def _retile(self):
        bar = self.superview()
        if isinstance(bar, VideoCallBar):
            bar.tile()

    def setFrameSize_(self, size):
        objc.super(VideoCallBarSegment, self).setFrameSize_(size)
        self.layoutContent()

    def setHidden_(self, flag):
        was = bool(self.isHidden())
        objc.super(VideoCallBarSegment, self).setHidden_(flag)
        if was != bool(flag):
            self._retile()

    def setEnabled_(self, flag):
        objc.super(VideoCallBarSegment, self).setEnabled_(flag)
        self._applyTint()
        self.setNeedsDisplay_(True)

    def hitTest_(self, point):
        # The icon view must not take the click: the whole segment is the button.
        hit = objc.super(VideoCallBarSegment, self).hitTest_(point)
        return self if hit is not None else None

    def mouseDownCanMoveWindow(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        if self.hover_area is not None:
            self.removeTrackingArea_(self.hover_area)
        self.hover_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            NSZeroRect, NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect,
            self, None)
        self.addTrackingArea_(self.hover_area)
        objc.super(VideoCallBarSegment, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.hovering = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hovering = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        enabled = bool(self.isEnabled())
        pressed = enabled and bool(self.isHighlighted())

        well = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(bounds, 2.0, 2.0), CALL_BAR_RADIUS - 5.0, CALL_BAR_RADIUS - 5.0)
        if self.destructive:
            red = NSColor.systemRedColor()
            red.colorWithAlphaComponent_(0.65 if pressed else (0.95 if self.hovering else 0.85)).setFill()
            well.fill()
        elif pressed:
            _call_bar_white(0.22).setFill()
            well.fill()
        elif self.hovering and enabled:
            _call_bar_white(0.10).setFill()
            well.fill()

        _icon_frame, label_y = self._geometry()
        if label_y is None:
            return
        alpha = 1.0 if enabled else 0.35
        label_color = (self.tint or _call_bar_white(0.92)).colorWithAlphaComponent_(0.92 * alpha)
        text = NSAttributedString.alloc().initWithString_attributes_(
            self.label, {NSFontAttributeName: self._labelFont(),
                         NSForegroundColorAttributeName: label_color})
        text_w = text.size().width
        text.drawAtPoint_(NSMakePoint(floor((bounds.size.width - text_w) / 2.0), label_y))


def make_call_bar_segment(symbol_name, label, action, target, destructive=False):
    segment = VideoCallBarSegment.alloc().initWithFrame_(
        NSMakeRect(0, 0, CALL_BAR_SEGMENT_MIN_W, CALL_BAR_HEIGHT))
    segment.setBordered_(False)
    segment.setTitle_('')
    segment.setImagePosition_(0)        # NSNoImage: the icon view shows the icon
    # Clicking the bar must not take focus off the video view.
    segment.setRefusesFirstResponder_(True)
    segment.setTarget_(target)
    segment.setAction_(action)
    segment.setToolTip_(label)
    segment.destructive = destructive
    icon = NSImageView.alloc().initWithFrame_(
        NSMakeRect(0, 0, CALL_BAR_ICON_BOX_W, CALL_BAR_ICON_BOX_H))
    icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    icon.setImageAlignment_(NSImageAlignCenter)
    icon.setEditable_(False)
    segment.addSubview_(icon)
    segment.iconView = icon
    segment.configure(symbol_name, label)
    return segment


class VideoCallBar(NSView):
    """The translucent capsule the segments sit in, centred at the bottom."""
    segments = ()
    compact = False

    def initWithFrame_(self, frame):
        self = objc.super(VideoCallBar, self).initWithFrame_(frame)
        if self is None:
            return None
        self.segments = []
        self.setWantsLayer_(True)
        self.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin | NSViewMaxYMargin)
        return self

    @objc.python_method
    def addSegment(self, segment):
        self.segments.append(segment)
        self.addSubview_(segment)
        self.tile()
        return segment

    @objc.python_method
    def _visible(self):
        return [segment for segment in self.segments if not segment.isHidden()]

    @objc.python_method
    def widthFor(self, compact):
        visible = self._visible()
        return (2 * CALL_BAR_PADDING
                + sum(segment.preferredWidth(compact) for segment in visible))

    @objc.python_method
    def minimumWindowWidth(self):
        """Narrower than this and not even the icons fit: hide the bar."""
        return self.widthFor(True) + CALL_BAR_MARGIN

    @objc.python_method
    def tile(self):
        container = self.superview()
        available = NSWidth(container.bounds()) if container is not None else None
        self.compact = bool(available is not None
                            and self.widthFor(False) + CALL_BAR_MARGIN > available)
        height = CALL_BAR_COMPACT_HEIGHT if self.compact else CALL_BAR_HEIGHT
        x = CALL_BAR_PADDING
        for segment in self._visible():
            width = segment.preferredWidth(self.compact)
            segment.setFrame_(NSMakeRect(x, CALL_BAR_PADDING, width, height - 2 * CALL_BAR_PADDING))
            segment.layoutContent()
            segment.setNeedsDisplay_(True)
            x += width
        width = x + CALL_BAR_PADDING
        origin_x = self.frame().origin.x
        if available is not None:
            origin_x = floor((available - width) / 2.0)
        self.setFrame_(NSMakeRect(origin_x, CALL_BAR_BOTTOM, width, height))
        self.setNeedsDisplay_(True)

    def resizeWithOldSuperviewSize_(self, old_size):
        # Every window resize, including the ones windowDidResize_ returns
        # early from: re-centre, and trade labels for room when narrow.
        self.tile()

    def mouseDownCanMoveWindow(self):
        return False

    def hitTest_(self, point):
        # Faded out is gone: a click where the invisible bar sits belongs to
        # the video underneath, which is what brings the bar back.
        if self.isHidden() or self.alphaValue() < 0.05:
            return None
        return objc.super(VideoCallBar, self).hitTest_(point)

    def drawRect_(self, rect):
        bounds = self.bounds()
        capsule = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(bounds, 0.5, 0.5), CALL_BAR_RADIUS, CALL_BAR_RADIUS)
        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.45).setFill()
        capsule.fill()
        _call_bar_white(0.16).setStroke()
        capsule.setLineWidth_(1.0)
        capsule.stroke()

        # Hairlines between neighbours, the segmented-control look -- but not
        # against the red segment, whose own fill already separates it.
        _call_bar_white(0.14).setFill()
        visible = self._visible()
        inset = 10.0 if not self.compact else 8.0
        for left, right in zip(visible, visible[1:]):
            if left.destructive or right.destructive:
                continue
            x = floor(right.frame().origin.x)
            NSRectFill(NSMakeRect(x, inset, 1.0, bounds.size.height - 2 * inset))

# A short note over the video that something happened -- "Screenshot saved" --
# with at most one thing to do about it. Sits just above the call bar, in the
# same translucent style, and goes away on its own.
TOAST_HEIGHT = 30.0
TOAST_PADDING = 14.0
TOAST_GAP = 12.0
TOAST_SECONDS = 6.0


class VideoToast(NSView):
    label = None
    actionButton = None
    callback = None

    def initWithFrame_(self, frame):
        self = objc.super(VideoToast, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        self.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin | NSViewMaxYMargin)

        label = NSTextField.labelWithString_('')
        label.setFont_(NSFont.systemFontOfSize_weight_(12.0, NSFontWeightMedium))
        label.setTextColor_(NSColor.whiteColor())
        label.setLineBreakMode_(5)          # NSLineBreakByTruncatingMiddle
        self.addSubview_(label)
        self.label = label

        button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        button.setBordered_(False)
        button.setRefusesFirstResponder_(True)
        button.setTarget_(self)
        button.setAction_('actionClicked:')
        self.addSubview_(button)
        self.actionButton = button
        return self

    @objc.python_method
    def configure(self, text, action_title=None, callback=None):
        self.label.setStringValue_(text)
        self.callback = callback
        if action_title:
            link = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.78, 1.0, 1.0)
            self.actionButton.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
                action_title, {NSFontAttributeName: NSFont.systemFontOfSize_weight_(12.0, NSFontWeightMedium),
                               NSForegroundColorAttributeName: link}))
            self.actionButton.setHidden_(False)
        else:
            self.actionButton.setHidden_(True)
        self.tile()

    @objc.python_method
    def tile(self):
        label_size = self.label.fittingSize()
        width = TOAST_PADDING + label_size.width
        button_size = None
        if not self.actionButton.isHidden():
            button_size = self.actionButton.fittingSize()
            width += TOAST_GAP + button_size.width
        width += TOAST_PADDING

        container = self.superview()
        if container is not None:
            available = NSWidth(container.bounds()) - 2 * CALL_BAR_MARGIN
            width = min(width, max(available, 120.0))
            bar = getattr(self.window().delegate(), 'buttonsView', None) if self.window() else None
            bottom = CALL_BAR_BOTTOM + CALL_BAR_HEIGHT + 10.0
            if bar is not None:
                bottom = bar.frame().origin.y + bar.frame().size.height + 10.0
            x = floor((NSWidth(container.bounds()) - width) / 2.0)
            self.setFrame_(NSMakeRect(x, bottom, width, TOAST_HEIGHT))

        label_w = width - 2 * TOAST_PADDING
        if button_size is not None:
            label_w -= TOAST_GAP + button_size.width
        self.label.setFrame_(NSMakeRect(TOAST_PADDING, floor((TOAST_HEIGHT - label_size.height) / 2.0),
                                        max(label_w, 0.0), label_size.height))
        if button_size is not None:
            self.actionButton.setFrame_(NSMakeRect(TOAST_PADDING + label_w + TOAST_GAP,
                                                   floor((TOAST_HEIGHT - button_size.height) / 2.0),
                                                   button_size.width, button_size.height))
        self.setNeedsDisplay_(True)

    def actionClicked_(self, sender):
        if self.callback is not None:
            self.callback()

    def resizeWithOldSuperviewSize_(self, old_size):
        self.tile()

    def mouseDownCanMoveWindow(self):
        return False

    def hitTest_(self, point):
        if self.isHidden() or self.alphaValue() < 0.05:
            return None
        return objc.super(VideoToast, self).hitTest_(point)

    def drawRect_(self, rect):
        bounds = self.bounds()
        radius = bounds.size.height / 2.0
        capsule = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(bounds, 0.5, 0.5), radius, radius)
        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.6).setFill()
        capsule.fill()
        _call_bar_white(0.16).setStroke()
        capsule.setLineWidth_(1.0)
        capsule.stroke()


@implementer(IObserver)
class VideoWindowController(NSWindowController):

    valid_aspect_ratios = [None, 1.33, 1.77]
    aspect_ratio_descriptions = {1.33: '4/3', 1.77: '16/9'}
    initial_aspect_ratio = None
    full_screen = False
    initialLocation = None
    always_on_top = False
    localVideoWindow = None
    full_screen_in_progress = False
    mouse_in_window = True
    mouse_timer = None
    title = None
    disconnectedPanel = None
    show_window_after_full_screen_ends = None
    tracking_area = None
    flipped = False
    aspect_ratio = None
    titleBarView = None
    initialLocation = None
    is_key_window = False
    updating_aspect_ratio = False
    dragMyVideoViewWithinWindow = True
    closed = False
    window_too_small = False
    zrtp_controller = None
    local_video_hidden = False

    holdButton = objc.IBOutlet()
    cameraButton = None
    pointerButton = None
    pointer_mode = False
    pointerEchoView = None
    pointer_echo_timer = None
    video_swapped = False
    remote_widget_aspect = None
    hangupButton = objc.IBOutlet()
    chatButton = objc.IBOutlet()
    infoButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    fullScreenButton = objc.IBOutlet()
    aspectButton = objc.IBOutlet()
    screenshotButton = objc.IBOutlet()
    recordButton = objc.IBOutlet()

    buttonsView = objc.IBOutlet()
    videoView = objc.IBOutlet()
    myVideoView = objc.IBOutlet()
    myVideoViewTL = objc.IBOutlet()
    myVideoViewTR = objc.IBOutlet()
    myVideoViewBL = objc.IBOutlet()
    myVideoViewBR = objc.IBOutlet()

    disconnectLabel = objc.IBOutlet()
    last_label = None
    screenshot_task = None
    screencapture_file = None
    toastView = None
    toast_timer = None
    close_after_exit_full_screen = False
    close_timer = None
    close_attempts = 0

    recordingImage = 0
    recording_timer = 0
    idle_timer = None
    is_idle = False
    show_time = None
    mouse_in_window = False
    is_key_window = False
    visible_buttons = True
    recording_timer = None
    must_hide_after_exit_full_screen = False
    will_close = False
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController
        self.sessionController.log_debug('Init %s' % self)
        self.title = self.sessionController.displayTitleShort
        self.flipWnd = mbFlipWindow.alloc().init()
        self.flipWnd.setFlipRight_(True)
        self.flipWnd.setDuration_(2.4)
        
        self.notification_center = NotificationCenter()


    @objc.python_method
    def initLocalVideoWindow(self):
        # Single-window flow: the legacy separate-local-preview-window
        # (VideoLocalWindowController) is no longer used. The main
        # video window opens directly in "preview" mode during call
        # setup and transitions to the connected layout when the
        # remote video stream starts. Kept as a no-op so any
        # downstream code that still calls this doesn't break.
        return

    @objc.python_method
    def _NH_BlinkMuteChangedState(self, sender, data):
        self.updateMuteButton()

    @objc.python_method
    def _NH_BlinkAudioStreamChangedHoldState(self, sender, data):
        self.updateHoldButton()

    @objc.python_method
    def _NH_VideoDeviceDidChangeCamera(self, sender, data):
        # Whichever view is showing the local camera -- the thumbnail, or
        # the main view once the pictures have been swapped.
        view = self.videoView if self.video_swapped else self.myVideoView
        view.setProducer(data.new_camera)
        if not self.video_swapped:
            # Whatever the new camera's shape, the thumbnail is re-derived
            # from its corner when the first real frame reports it.
            self.myVideoView.aspect_ratio = None


    @property
    def media_received(self):
        return self.streamController.media_received

    # --- remote pointer on the peer's shared screen ----------------------------------

    @objc.python_method
    def _pointerSession(self):
        sc = self.sessionController
        return sc.session if sc is not None else None

    @objc.python_method
    def _NH_BlinkScreenPointerDidChange(self, sender, data):
        if sender is self._pointerSession():
            self.updatePointerButton()

    @objc.python_method
    def _NH_BlinkScreenPointerGotAck(self, sender, data):
        if sender is self._pointerSession():
            self.showPointerEcho(data.x, data.y)

    @objc.python_method
    def pointerAvailable(self):
        if ScreenPointerManager is None or self.closed or self.video_swapped:
            return False
        session = self._pointerSession()
        return session is not None and ScreenPointerManager().can_point(session)

    @objc.python_method
    def updatePointerButton(self):
        if self.pointerButton is None:
            return
        available = self.pointerAvailable()
        if not available and self.pointer_mode:
            self.setPointerMode(False)
        self.pointerButton.setHidden_(not available)
        self.pointerButton.configure('hand.point.up.left.fill', NSLocalizedString("Pointer", "Video call bar"),
                                     tint=NSColor.systemGreenColor() if self.pointer_mode else None)

    @objc.python_method
    def setPointerMode(self, enabled):
        enabled = bool(enabled)
        if enabled == self.pointer_mode:
            return
        self.pointer_mode = enabled
        sc = self.sessionController
        if sc is not None:
            sc.log_info('Remote pointer %s' % ('on' if enabled else 'off'))
        if self.pointerButton is not None:
            self.pointerButton.configure('hand.point.up.left.fill', NSLocalizedString("Pointer", "Video call bar"),
                                         tint=NSColor.systemGreenColor() if enabled else None)
        window = self.window()
        if window is not None and self.videoView is not None:
            window.invalidateCursorRectsForView_(self.videoView)
        if enabled:
            self.showToast(NSLocalizedString("Click on the shared screen to point", "Label"))

    @objc.IBAction
    def userClickedPointerButton_(self, sender):
        if self.pointerAvailable():
            self.setPointerMode(not self.pointer_mode)

    @objc.python_method
    def handlePointerClick(self, event):
        """Send a click on the remote video as a pointer. True when the click
        was meant for pointing and must not do anything else."""
        view = self.videoView
        if view is None or not self.pointerAvailable():
            return False
        location = event.locationInWindow()
        for other in (self.myVideoView, self.buttonsView):
            if other is None or other.window() is None or other.isHidden() or other.alphaValue() < 0.05:
                continue
            if NSPointInRect(other.convertPoint_fromView_(location, None), other.bounds()):
                return False
        point = view.convertPoint_fromView_(location, None)
        bounds = view.bounds()
        if not NSPointInRect(point, bounds):
            return False
        frame = view._frame
        if frame is None or not frame.width or not frame.height:
            return True
        y = bounds.size.height - point.y if view.isFlipped() else point.y
        normalized = ScreenPointer.view_to_frame(point.x, y, bounds.size.width, bounds.size.height,
                                                 frame.width, frame.height)
        if normalized is None:
            return True     # on the black bars around the picture
        manager = ScreenPointerManager()
        session = self._pointerSession()
        if not manager.peer_in_app(session):
            self.showToast(NSLocalizedString("%s left Sylk, the pointer cannot be shown right now", "Label") % self.title)
            return True
        manager.send_pointer(session, normalized[0], normalized[1])
        return True

    @objc.python_method
    def showPointerEcho(self, nx, ny):
        view = self.videoView
        if view is None or PointerEchoView is None or not self.pointer_mode:
            return
        frame = view._frame
        container = view.superview()
        if frame is None or container is None or not frame.width or not frame.height:
            return
        echo = self.pointerEchoView
        if echo is None or echo.superview() is not container:
            if echo is not None:
                echo.removeFromSuperview()
            echo = PointerEchoView.alloc().initWithFrame_(view.frame())
            container.addSubview_positioned_relativeTo_(echo, NSWindowAbove, view)
            self.pointerEchoView = echo
        echo.setFrame_(view.frame())
        bounds = view.bounds()
        x, y = ScreenPointer.frame_to_view(nx, ny, bounds.size.width, bounds.size.height, frame.width, frame.height)
        if view.isFlipped():
            y = bounds.size.height - y
        echo.point = (x, y)
        echo.setHidden_(False)
        echo.setNeedsDisplay_(True)
        if self.pointer_echo_timer is not None and self.pointer_echo_timer.isValid():
            self.pointer_echo_timer.invalidate()
        self.pointer_echo_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            POINTER_ECHO_SECONDS, self, "pointerEchoTimerFired:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.pointer_echo_timer, NSRunLoopCommonModes)

    def pointerEchoTimerFired_(self, timer):
        self.pointer_echo_timer = None
        if self.pointerEchoView is not None:
            self.pointerEchoView.setHidden_(True)

    @objc.python_method
    def closePointer(self):
        self.pointer_mode = False
        if self.pointer_echo_timer is not None and self.pointer_echo_timer.isValid():
            self.pointer_echo_timer.invalidate()
        self.pointer_echo_timer = None
        if self.pointerEchoView is not None:
            self.pointerEchoView.removeFromSuperview()
            self.pointerEchoView = None
    
    @objc.python_method
    def updateMuteButton(self):
        muted = SIPManager().is_muted()
        if muted:
            self.muteButton.configure('mic.slash.fill', NSLocalizedString("Unmute", "Video call bar"),
                                      tint=NSColor.systemRedColor())
        else:
            self.muteButton.configure('mic.fill', NSLocalizedString("Mute", "Video call bar"))

    @objc.python_method
    def updateHoldButton(self):
        audio_stream = self.sessionController.streamHandlerOfType("audio")
        connected = bool(audio_stream and audio_stream.status == STREAM_CONNECTED)
        held_here = connected and bool(audio_stream.holdByLocal)
        held = held_here or (connected and bool(audio_stream.holdByRemote))
        label = NSLocalizedString("Unhold", "Label") if held_here else NSLocalizedString("Hold", "Label")
        self.holdButton.setToolTip_(label)
        self.holdButton.configure('pause.fill', label,
                                  tint=NSColor.systemRedColor() if held else None)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def awakeFromNib(self):
        self.notification_center.add_observer(self,sender=self.streamController.videoRecorder)
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')
        self.notification_center.add_observer(self, name='BlinkAudioStreamChangedHoldState')
        self.notification_center.add_observer(self, name='VideoDeviceDidChangeCamera')
        if ScreenPointerManager is not None:
            self.notification_center.add_observer(self, name='BlinkScreenPointerDidChange')
            self.notification_center.add_observer(self, name='BlinkScreenPointerGotAck')

        self._buildCallBar()
        self.updatePointerButton()

        self.hangupButton.setToolTip_(NSLocalizedString("Hangup", "Label"))
        self.chatButton.setToolTip_(NSLocalizedString("Chat", "Label"))
        self.infoButton.setToolTip_(NSLocalizedString("Show Session Information", "Label"))
        self.muteButton.setToolTip_(NSLocalizedString("Mute", "Label"))
        self.screenshotButton.setToolTip_(NSLocalizedString("Screenshot", "Label"))
        self.recordButton.setToolTip_(NSLocalizedString("Start Recording", "Label"))
        self.fullScreenButton.setToolTip_(NSLocalizedString("Full Screen", "Label"))

        self.disconnectLabel.superview().hide()

        # TEMPORARILY HIDDEN: the Full Screen / Chat / Info / Record
        # buttons on the video call bar are hidden, not removed.  The
        # IBOutlets and all driving logic (recording timer, fullscreen
        # toggle, chat opener, session-info panel) are intact - this
        # only flips setHidden_(True) so the buttons stay out of the
        # layout/alpha-fade animation.  Restore by deleting this block.
        for _btn in (self.fullScreenButton, self.chatButton,
                     self.infoButton):
            if _btn is not None:
                _btn.setHidden_(True)

        self.updateMuteButton()
        self.updateHoldButton()
        self._setupStatsOverlay()

        self.recording_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "updateRecordingTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.recording_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.recording_timer, NSEventTrackingRunLoopMode)

        # Refresh the stats overlay once per second, in lockstep with
        # VideoController.updateStatisticsTimer_ which recomputes the
        # underlying RTT/codec/etc. on the same cadence.
        self.stats_overlay_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "updateStatsOverlayTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self.stats_overlay_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self.stats_overlay_timer, NSEventTrackingRunLoopMode)

    @objc.python_method
    def availableCameras(self):
        """The cameras the user can pick, as the Devices menu lists them."""
        try:
            return [device for device in NSApp.delegate().video_devices
                    if device not in (None, 'system_default')]
        except Exception:
            return []

    @objc.IBAction
    def userClickedCameraButton_(self, sender):
        """The camera segment's menu: what the video does, then which camera.

        Offered with a single camera too -- only the device list at the
        bottom depends on how many there are. Picking a camera sends the
        contacts window's selectVideoDevice:, the very action Devices >
        Video Camera sends, so both places change it the same way. There is
        no None entry: turning the camera off mid-call is Stop Video.
        """
        connected = bool(self.flipped and self.streamController is not None
                         and self.streamController.stream is not None)
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        def add(title, action, target=self, enabled=True, state=NSOffState):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, action, '')
            item.setTarget_(target)
            item.setEnabled_(bool(enabled))
            item.setState_(state)
            return item

        add(NSLocalizedString("Show Preview", "Menu item") if self.local_video_hidden
            else NSLocalizedString("Hide Preview", "Menu item"),
            'userClickedLocalVideo:', enabled=connected)
        add(NSLocalizedString("Swap Video", "Menu item"), 'userClickedSwapVideo:',
            enabled=connected, state=NSOnState if self.video_swapped else NSOffState)
        add(NSLocalizedString("Stop Video", "Menu item"), 'removeVideo:',
            enabled=self.sessionController is not None)

        ratios = [ratio for ratio in self.valid_aspect_ratios if ratio is not None]
        aspect_item = add(NSLocalizedString("Aspect Ratio", "Menu item"), None,
                          enabled=bool(ratios))
        submenu = NSMenu.alloc().init()
        submenu.setAutoenablesItems_(False)
        for ratio in ratios:
            title = self.aspect_ratio_descriptions.get(ratio, "%.2f" % ratio)
            item = submenu.addItemWithTitle_action_keyEquivalent_(title, 'userSelectedAspectRatio:', '')
            item.setTarget_(self)
            item.setRepresentedObject_(ratio)
            current = self.aspect_ratio is not None and abs(ratio - self.aspect_ratio) < 0.005
            item.setState_(NSOnState if current else NSOffState)
        aspect_item.setSubmenu_(submenu)

        menu.addItem_(NSMenuItem.separatorItem())
        cameras = self.availableCameras()
        try:
            current = SIPApplication.video_device.real_name
        except AttributeError:
            current = None
        if not cameras:
            add(NSLocalizedString("No Camera", "Menu item"), None, enabled=False)
        devices = NSApp.delegate().contactsWindowController
        for camera in sorted(cameras, key=lambda name: name.lower()):
            item = add(camera, 'selectVideoDevice:', target=devices,
                       state=NSOnState if camera == current else NSOffState)
            item.setRepresentedObject_(camera)

        # Under the segment; AppKit moves it up if that would leave the screen.
        bounds = sender.bounds()
        below = bounds.size.height if sender.isFlipped() else 0.0
        menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(0.0, below), sender)

    @objc.IBAction
    def userSelectedAspectRatio_(self, sender):
        ratio = sender.representedObject()
        if ratio is None:
            return
        self.aspect_ratio = float(ratio)
        self.sessionController.log_info(
            "Aspect ratio set to %s" % self.aspect_ratio_descriptions.get(self.aspect_ratio, "%.2f" % self.aspect_ratio))
        self.updateAspectRatio()

    @objc.IBAction
    def userClickedSwapVideo_(self, sender):
        """Put the local camera in the main view and the remote in the thumbnail, or back.

        Both views are detached first and attached again after the run loop
        has drained, the same two-step show() uses on connect: switching a
        widget's producer in place races pjsip's converter thread.

        The main view's shape normally comes from the remote picture and
        resizes the window; while swapped the local camera must not do
        that, and on the way back the remote's known shape is put back
        rather than re-detected, which would also re-centre the window.
        """
        stream = self.streamController.stream if self.streamController is not None else None
        if not self.flipped or stream is None:
            return
        try:
            local = SIPApplication.video_device.producer
        except AttributeError:
            return
        remote = stream.producer
        swapping = not self.video_swapped
        if swapping:
            self.remote_widget_aspect = self.videoView.aspect_ratio
        main, thumb = (local, remote) if swapping else (remote, local)
        self.video_swapped = swapping
        self.updatePointerButton()
        self.sessionController.log_info('Video %s' % ('swapped: local camera in the main view'
                                                      if swapping else 'restored: remote in the main view'))
        for view in (self.videoView, self.myVideoView):
            try:
                view.setProducer(None)
            except Exception:
                pass

        from util import call_later

        def attach():
            if self.closed or self.will_close:
                return
            try:
                self.videoView.aspect_ratio = None if swapping else self.remote_widget_aspect
                self.myVideoView.aspect_ratio = None
                self.videoView.setProducer(main)
                self.myVideoView.setProducer(thumb)
            except Exception as exc:
                BlinkLogger().log_info("Swapping video failed: %s" % exc)
        call_later(0.25, attach)

    @objc.python_method
    def _buildCallBar(self):
        """Put the call bar where the xib's row of loose buttons was.

        The outlets are pointed at the new segments, so everything that
        already drives them -- hiding, enabling, tooltips -- goes on working
        against the bar. Left to right: what changes the call, what the
        window does, and hanging up last and in red.
        """
        old = self.buttonsView
        content = old.superview() if old is not None else None
        if content is None:
            content = self.window().contentView()
        bar = VideoCallBar.alloc().initWithFrame_(
            NSMakeRect(0, CALL_BAR_BOTTOM, 0, CALL_BAR_HEIGHT))
        if old is not None and old.superview() is not None:
            content.addSubview_positioned_relativeTo_(bar, NSWindowAbove, old)
            old.removeFromSuperview()
        else:
            content.addSubview_(bar)
        self.buttonsView = bar

        def add(symbol, label, action, destructive=False):
            return bar.addSegment(make_call_bar_segment(symbol, label, action, self, destructive))

        self.muteButton = add('mic.fill', NSLocalizedString("Mute", "Video call bar"),
                              'userClickedMuteButton:')
        self.cameraButton = add('camera', NSLocalizedString("Camera", "Video call bar"),
                                'userClickedCameraButton:')
        self.cameraButton.setToolTip_(NSLocalizedString("Video and camera", "Label"))
        if ScreenPointerManager is not None:
            # Only while the peer shares a screen it can draw our pointer on;
            # updatePointerButton() shows it.
            self.pointerButton = add('hand.point.up.left.fill', NSLocalizedString("Pointer", "Video call bar"),
                                     'userClickedPointerButton:')
            self.pointerButton.setToolTip_(NSLocalizedString("Point at the shared screen", "Label"))
            self.pointerButton.setHidden_(True)
        self.holdButton = add('pause.fill', NSLocalizedString("Hold", "Video call bar"),
                              'userClickedHoldButton:')
        self.chatButton = add('message.fill', NSLocalizedString("Chat", "Video call bar"),
                              'userClickedChatButton:')
        self.screenshotButton = add('camera.viewfinder', NSLocalizedString("Screenshot", "Video call bar"),
                                    'userClickedScreenshotButton:')
        self.recordButton = add('record.circle', NSLocalizedString("Record", "Video call bar"),
                                'userClickedRecordButton:')
        self.fullScreenButton = add('arrow.up.left.and.arrow.down.right',
                                    NSLocalizedString("Full", "Video call bar"),
                                    'userClickedFullScreenButton:')
        self.infoButton = add('info.circle', NSLocalizedString("Info", "Video call bar"),
                              'userClickedInfoButton:')
        self.hangupButton = add('phone.down.fill', NSLocalizedString("End", "Video call bar"),
                                'userClickedHangupButton:', destructive=True)

        # The status pill sat just above the old, shorter row; keep it clear
        # of the bar.
        status = self.disconnectLabel.superview() if self.disconnectLabel is not None else None
        if status is not None:
            origin = status.frame().origin
            origin.y = CALL_BAR_BOTTOM + CALL_BAR_HEIGHT + 8.0
            status.setFrameOrigin_(origin)

    @objc.python_method
    def _setupStatsOverlay(self):
        """Create a small translucent overlay in the bottom strip of the
        video window (under the call-control buttons) showing codec,
        resolution, fps and RTT. The label is a sibling of videoView in
        the content view so it composes on top of the GPU drawable
        without being part of the Metal render pass.

        Positioning: centered horizontally, with its bottom edge fixed
        10 px above the window's bottom margin. This sits cleanly under
        the buttons row (whose own y starts above this strip) and does
        NOT overlap with the disconnectLabel that lives in the middle
        of the window during pre-connect / disconnect states.

        The overlay follows the same auto-hide rules as buttonsView:
        visible when the user is interacting with the window, hidden
        when the buttons fade out."""
        content = self.window().contentView()
        if content is None:
            return

        cb = content.bounds()
        overlay_w = 260.0
        # Height shrunk 20 → 15 and bottom_margin 10 → 4 so the
        # overlay's TOP edge ends at y=19 — strictly below the
        # buttonsView whose bottom edge sits at y=20 in the xib.
        # Earlier values placed the overlay's upper half INSIDE the
        # buttonsView's vertical span, which read as an overlap.
        overlay_h = 15.0
        bottom_margin = 4.0   # fixed gap from the window's lower edge

        frame = NSMakeRect(
            (cb.size.width - overlay_w) / 2.0,
            bottom_margin,
            overlay_w,
            overlay_h)

        overlay = NSTextField.alloc().initWithFrame_(frame)
        # flexibleMinX + flexibleMaxX keeps it centred on horizontal
        # resize; flexibleMaxY anchors the bottom so it doesn't drift up
        # when the window grows taller.
        overlay.setAutoresizingMask_(
            NSViewMinXMargin | NSViewMaxXMargin | NSViewMaxYMargin)
        overlay.setEditable_(False)
        overlay.setSelectable_(False)
        overlay.setBezeled_(False)
        overlay.setDrawsBackground_(True)
        overlay.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.0, 0.0, 0.55))
        overlay.setTextColor_(NSColor.whiteColor())
        try:
            from AppKit import NSFont
            overlay.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11.0, 0))
        except Exception:
            from AppKit import NSFont
            overlay.setFont_(NSFont.systemFontOfSize_(11.0))
        overlay.setAlignment_(1)  # NSTextAlignmentCenter.
                                  # On modern macOS (10.12+) the
                                  # NSTextAlignment* constants match
                                  # the iOS naming, NOT the old
                                  # NSLeftTextAlignment / NSRight... /
                                  # NSCenter... order:
                                  # 0=Left, 1=Center, 2=Right,
                                  # 3=Justified, 4=Natural.
        overlay.setStringValue_("")
        overlay.setHidden_(True)
        # Round the corners via the backing layer.
        try:
            overlay.setWantsLayer_(True)
            overlay.layer().setCornerRadius_(4.0)
            overlay.layer().setMasksToBounds_(True)
        except Exception:
            pass
        content.addSubview_(overlay)
        self.statsOverlay = overlay

    @objc.python_method
    def _formatStatsLine(self):
        """Compose the single-line label content from the most recent
        stats. Returns None if we don't have enough information yet
        (e.g. before the first frame arrives)."""
        sc = self.streamController
        if sc is None:
            return None

        # Codec — comes from the negotiated SIP stream once media is
        # established. Before connect, fall back to "—".
        codec = ""
        try:
            if sc.stream is not None and sc.stream.codec:
                codec = sc.stream.codec
                if isinstance(codec, (bytes, bytearray)):
                    codec = codec.decode('ascii', errors='replace')
                codec = codec.upper()
        except Exception:
            codec = ""

        # Resolution — taken from the actual remote frame so it tracks
        # mid-call resolution changes (camera rotation, bandwidth-driven
        # downscale, etc.) without us having to listen for separate
        # events.
        resolution = ""
        try:
            f = getattr(self.videoView, '_frame', None)
            if f is not None and f.width and f.height:
                resolution = "%dx%d" % (int(f.width), int(f.height))
        except Exception:
            resolution = ""

        # FPS is whatever VideoWidget computed over its last 1-second
        # window from handle_frame() calls — the actual receive rate.
        fps = 0
        try:
            fps = int(getattr(self.videoView, 'current_fps', 0) or 0)
        except Exception:
            fps = 0

        # RTT — sc.statistics['rtt'] is already halved (one-way) in
        # VideoController.updateStatisticsTimer_; show round-trip as
        # double for a more useful "what the user feels" number.
        rtt_ms = 0
        try:
            stats = getattr(sc, 'statistics', None) or {}
            rtt_one_way = stats.get('rtt', 0) or 0
            rtt_ms = int(round(float(rtt_one_way) * 2))
        except Exception:
            rtt_ms = 0

        # RX / TX bitrate, taken from VideoController.statistics which
        # samples the underlying sipsimple stream once per second.
        # rx_bytes / tx_bytes are bytes-per-second; convert to kbps
        # (×8/1000) for display. Round to nearest kbps so the number
        # doesn't twitch on minor jitter.
        rx_kbps = 0
        tx_kbps = 0
        try:
            stats = getattr(sc, 'statistics', None) or {}
            rx_bps = (stats.get('rx_bytes', 0) or 0) * 8
            tx_bps = (stats.get('tx_bytes', 0) or 0) * 8
            rx_kbps = int(round(rx_bps / 1000.0))
            tx_kbps = int(round(tx_bps / 1000.0))
        except Exception:
            rx_kbps = tx_kbps = 0

        parts = []
        if codec:
            parts.append(codec)
        if resolution:
            parts.append(resolution)
        if fps > 0:
            parts.append("%d fps" % fps)
        # Format speeds as a single combined "↓X ↑Y kbps" segment
        # rather than two parts so the overlay stays compact. Show
        # the segment whenever EITHER direction has data, so the
        # user sees TX even before the remote sends frames back.
        if rx_kbps > 0 or tx_kbps > 0:
            parts.append("↓%d ↑%d kbps" % (rx_kbps, tx_kbps))
        if rtt_ms > 0:
            parts.append("RTT %d ms" % rtt_ms)
        if not parts:
            return None
        return "  ".join(parts)

    def updateStatsOverlayTimer_(self, timer):
        try:
            overlay = getattr(self, 'statsOverlay', None)
            if overlay is None or self.closed or self.will_close:
                return
            # TEMPORARILY DISABLED: the codec/speed band that sits under
            # the call-control buttons is force-hidden for now.  To
            # re-enable, remove this early-return block; the full
            # formatting / visibility logic below is preserved as-is.
            overlay.setHidden_(True)
            return
            text = self._formatStatsLine()
            if not text:
                overlay.setHidden_(True)
                return
            overlay.setStringValue_(text)
            # Tie the overlay's visibility to the buttons' visibility:
            # if the user has chosen to keep the chrome hidden (auto-fade
            # has finished), hide the stats too. Otherwise show it.
            buttons = getattr(self, 'buttonsView', None)
            if buttons is not None:
                overlay.setHidden_(buttons.isHidden() or buttons.alphaValue() <= 0.01)
            else:
                overlay.setHidden_(False)
        except Exception as e:
            BlinkLogger().log_debug("stats overlay refresh ignored: %s" % e)

    @property
    def sessionController(self):
        # ``self.streamController`` is a VideoController IBOutlet/ref.
        # Once the VideoController has been ObjC-deallocated, this
        # attribute can hold a NIL'd PyObjC proxy that is truthy in
        # Python but raises AttributeError ("cannot access attribute X
        # of NIL 'VideoController' object") on any attribute lookup.
        # Wrap the chain to return None instead of crashing — that
        # error path was observed at app exit after a video call,
        # taking the whole app down with a dispatch-main-queue
        # block_invoke during NSObject_dealloc_block_invoke.
        sc = self.streamController
        if sc is None:
            return None
        try:
            return sc.sessionController
        except AttributeError:
            return None

    @objc.python_method
    def init_aspect_ratio(self, width, height):
        if self.video_swapped:
            return      # the main view is showing the local camera
        self.sessionController.log_info('Remote video stream at %0.fx%0.f resolution' % (width, height))
        self.aspect_ratio = floor((float(width) / height) * 100)/100
        self.sessionController.log_info('Remote aspect ratio is %s' % self.aspect_ratio)

        found = False
        for ratio in self.valid_aspect_ratios:
            if ratio is None:
                continue
            diff = ratio - self.aspect_ratio
            if diff < 0:
                diff = diff * -1
            if self.aspect_ratio > 0.95 * ratio and self.aspect_ratio < 1.05 * ratio:
                found = True
                break
    
        if self.aspect_ratio == 1:
            self.aspect_ratio = 1.77
            found = True

        if not found:
            self.valid_aspect_ratios.append(self.aspect_ratio)
        
        frame = self.window().frame()
        frame.size.height = frame.size.width / self.aspect_ratio
        self.window().setFrame_display_(frame, True)
        self.window().center()
        if self.initial_aspect_ratio is None:
            self.initial_aspect_ratio = self.aspect_ratio

    @objc.python_method
    def init_window(self):
        if self.window() is not None:
            return

        if self.streamController.stream is None:
            return

        NSBundle.loadNibNamed_owner_("VideoWindow", self)
        title = NSLocalizedString("Video with %s", "Window title") % self.title
        NSApplication.sharedApplication().addWindowsItem_title_filename_(self.window(), title, False)
        self.window().center()
        self.window().setDelegate_(self)
        # Pin the window's colorspace to sRGB. This OPTS THE WINDOW
        # OUT of the EDR / auto-tone-mapping pool that macOS 26 Tahoe
        # applies to any fullScreenPrimary window by default.
        #
        # Symptom that drove this: with the window in its default
        # (screen-inherited) colorspace, the Metal-rendered video
        # brightness fluctuates 70%↔90% depending on focus and
        # ambient sensors — the WindowServer is dynamically
        # reserving/releasing EDR headroom and squeezing our SDR
        # pixels to whatever fraction of SDR-reference is left.
        # That auto-headroom kicks in at the WINDOW level (because
        # the window is EDR-eligible); flipping the layer's
        # wantsExtendedDynamicRangeContent to False isn't enough.
        # Setting the window's colorSpace to a fixed sRGB profile
        # removes it from the EDR pool, so the compositor maps our
        # bytes 1:1 to SDR-reference with no dynamic adjustment.
        #
        # An earlier comment here warned against doing this on the
        # theory that it shrinks the gamut to sRGB on a Display P3
        # screen and dims the picture. That observation was from
        # before the auto-EDR behaviour landed on Tahoe; the current
        # tradeoff is the opposite — pinned sRGB is FULL brightness
        # and stable, free-floating is fluctuating-and-dim.
        try:
            from AppKit import NSColorSpace
            srgb_ns = NSColorSpace.sRGBColorSpace()
            if srgb_ns is not None:
                self.window().setColorSpace_(srgb_ns)
        except Exception as cs_err:
            self.sessionController.log_debug(
                'window().setColorSpace_(sRGB) failed: %s' % cs_err)
        self.sessionController.log_debug('Init %s in %s' % (self.window(), self))
        self.window().makeFirstResponder_(self.videoView)
        self.window().setAcceptsMouseMovedEvents_(True)
        self.window().setTitle_(title)
        # Hide the Hold button on the video call bar at setup time.
        # See the matching exclusion in showButtons(). Audio calls
        # still get the hold affordance via the audio bar.
        try:
            if self.holdButton is not None:
                self.holdButton.setHidden_(True)
        except Exception:
            pass
        self.updateTrackingAreas()

        if SIPSimpleSettings().video.keep_window_on_top:
            self.toogleAlwaysOnTop()

        self.startIdleTimer()

    def rightMouseDown_(self, event):
        if self.closed:
            return
        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
          NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
          self.window().graphicsContext(), 0, 1, 0)

        menu = NSMenu.alloc().init()
        if self.streamController.zrtp_active:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Encrypted using ZRTP", "Menu item"), "", "")
            lastItem.setEnabled_(False)
            
            lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Verify Peer...", "Menu item"), "userClickedVerifyPeer:", "")
            lastItem.setIndentationLevel_(1)
            menu.addItem_(NSMenuItem.separatorItem())

        menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Remove Video", "Menu item"), "removeVideo:", "")
        menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hangup", "Menu item"), "hangup:", "")
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hold", "Menu item"), "userClickedHoldButton:", "")
        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream and audio_stream.status == STREAM_CONNECTED and not self.sessionController.inProposal:
                if audio_stream.holdByLocal:
                    lastItem.setTitle_(NSLocalizedString("Unhold", "Label"))
                else:
                    lastItem.setTitle_(NSLocalizedString("Hold", "Label"))

        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Mute", "Menu item"), "userClickedMuteButton:", "")
        lastItem.setState_(NSOnState if SIPManager().is_muted() else NSOffState)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Always On Top", "Menu item"), "toogleAlwaysOnTop:", "")
        lastItem.setEnabled_(not self.full_screen)
        lastItem.setState_(NSOnState if self.always_on_top else NSOffState)
        if self.sessionController.hasStreamOfType("chat"):
            menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Attach To Chat Drawer", "Menu item"), "userClickedAttachToChatMenuItem:", "")
        if self.sessionController.hasStreamOfType("audio"):
            menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Attach To Audio Drawer", "Menu item"), "userClickedAttachToAudioMenuItem:", "")
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Full Screen", "Menu item"), "userClickedFullScreenButton:", "")
        menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Aspect", "Menu item"), "userClickedAspectButton:", "")
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Screenshot", "Menu item"), "userClickedScreenshotButton:", "")
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send Screenshot", "Menu item"), "userClickedSendScreenshotButton:", "")
        lastItem.setEnabled_(not bool(self.screencapture_file))
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Open Screenshots Folder", "Menu item"), "userClickedOpenScreenshotFolder:", "")
        lastItem.setRepresentedObject_(ApplicationData.get('screenshots'))
        lastItem.setEnabled_(True)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Info", "Menu item"), "userClickedInfoButton:", "")
        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Local Video", "Menu item"), "userClickedLocalVideo:", "")
        lastItem.setState_(NSOffState if self.local_video_hidden else NSOnState)
        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self.window().contentView())

    def removeVideo_(self, sender):
        self.will_close = True
        self.removeVideo()

    def hangup_(self, sender):
        if self.sessionController:
            self.sessionController.end()

    def mouseDown_(self, event):
        if self.closed:
            return

        if self.streamController.ended:
            return
        if self.pointer_mode and self.handlePointerClick(event):
            self.initialLocation = None     # a click to point, not a window drag
            return
        self.initialLocation = event.locationInWindow()
        # ANY click on the video window resurrects the chrome.
        # Previously the buttons + PIP fade-out was tied to mouse
        # idle, and the only way to bring them back was to move the
        # mouse. Users who left the pointer parked over the window
        # could end up with a video-only canvas and no way to find
        # hangup / mute without first wiggling the mouse to trigger
        # the idle reset. Showing the chrome on mouseDown gives an
        # always-discoverable way back. Also reset the idle counter
        # bookkeeping so the auto-hide timer gives the user the full
        # IDLE_TIME window before fading the chrome out again.
        try:
            self.show_time = time.time()
            self.is_idle = False
            self.showButtons()
        except Exception:
            pass

    def mouseUp_(self, event):
        if self.closed:
            return
        if self.streamController.ended:
            return

        if self.myVideoView and self.myVideoView.drag_mode is not None:
            self.myVideoView.endDrag()

    def mouseDragged_(self, event):
        if self.closed:
            return
        if self.streamController.ended:
            return

        if self.myVideoView and self.myVideoView.drag_mode is not None:
            self.myVideoView.mouseDragged_(event)

    def mouseDraggedView_(self, event):
        if self.closed:
            return
        if self.streamController.ended:
            return

        if not self.initialLocation:
            return

        if self.full_screen or self.full_screen_in_progress:
            return

        screenVisibleFrame = NSScreen.mainScreen().visibleFrame()
        windowFrame = self.window().frame()
        newOrigin = windowFrame.origin

        currentLocation = event.locationInWindow()

        newOrigin.x += (currentLocation.x - self.initialLocation.x)
        newOrigin.y += (currentLocation.y - self.initialLocation.y)

        if ((newOrigin.y + windowFrame.size.height) > (screenVisibleFrame.origin.y + screenVisibleFrame.size.height)):
            newOrigin.y = screenVisibleFrame.origin.y + (screenVisibleFrame.size.height - windowFrame.size.height)

        self.window().setFrameOrigin_(newOrigin)

    def updateTrackingAreas(self):
        if self.closed:
            return

        self.closeTrackingAreas()

        rect = NSZeroRect
        rect.size = self.window().contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                                                                                         NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window().contentView().addTrackingArea_(self.tracking_area)

    def closeTrackingAreas(self):
        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

    @property
    def sessionController(self):
        # ``self.streamController`` is a VideoController IBOutlet/ref.
        # Once the VideoController has been ObjC-deallocated, this
        # attribute can hold a NIL'd PyObjC proxy that is truthy in
        # Python but raises AttributeError ("cannot access attribute X
        # of NIL 'VideoController' object") on any attribute lookup.
        # Wrap the chain to return None instead of crashing — that
        # error path was observed at app exit after a video call,
        # taking the whole app down with a dispatch-main-queue
        # block_invoke during NSObject_dealloc_block_invoke.
        sc = self.streamController
        if sc is None:
            return None
        try:
            return sc.sessionController
        except AttributeError:
            return None

    def windowDidResignKey_(self, notification):
        self.is_key_window = False

    def windowDidBecomeKey_(self, notification):
        self.is_key_window = True

    def keyDown_(self, event):
        if self.closed:
            return

        if event.keyCode() == 53:
            if self.full_screen:
                self.toggleFullScreen()
            else:
                if self.sessionController:
                    self.sessionController.removeVideoFromSession()

    def mouseEntered_(self, event):
        if self.closed:
            return

        if self.streamController.ended:
            return
        self.mouse_in_window = True
        self.stopMouseOutTimer()
        self.showButtons()

    def mouseExited_(self, event):
        if self.closed:
            return

        if self.streamController.ended:
            return
        if self.full_screen or self.full_screen_in_progress:
            return
        self.mouse_in_window = False
        self.startMouseOutTimer()

    @objc.python_method
    def hideStatusLabel(self):
        if self.disconnectLabel:
            self.disconnectLabel.setStringValue_("")
            self.disconnectLabel.superview().hide()

    @objc.python_method
    def showStatusLabel(self, label):
        self.last_label = label
        if self.window():
            self.disconnectLabel.superview().show()
            self.disconnectLabel.setStringValue_(label)
            self.disconnectLabel.setHidden_(False)

        if self.localVideoWindow and self.localVideoWindow.window():
            self.localVideoWindow.window().delegate().disconnectLabel.setStringValue_(label)
            self.localVideoWindow.window().delegate().disconnectLabel.superview().show()
            self.localVideoWindow.window().delegate().disconnectLabel.setHidden_(False)

    @objc.python_method
    def startIdleTimer(self):
        if self.idle_timer is None:
            self.idle_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "updateIdleTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.idle_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.idle_timer, NSEventTrackingRunLoopMode)

    @objc.python_method
    def stopIdleTimer(self):
        if self.idle_timer is not None and self.idle_timer.isValid():
            self.idle_timer.invalidate()
            self.idle_timer = None

    @objc.python_method
    def changeAspectRatio(self):
        try:
            idx = self.valid_aspect_ratios.index(self.aspect_ratio)
        except ValueError:
            self.aspect_ratio = None
        else:
            try:
                self.aspect_ratio = self.valid_aspect_ratios[idx+1]
            except IndexError:
                self.aspect_ratio = self.valid_aspect_ratios[1]

        if self.aspect_ratio:
            try:
                desc = self.aspect_ratio_descriptions[self.aspect_ratio]
            except KeyError:
                desc = "%.2f" % self.aspect_ratio

            self.sessionController.log_info("Aspect ratio set to %s" % desc)

        self.updateAspectRatio()

    @objc.python_method
    def updateAspectRatio(self):
        if self.closed:
            return

        if not self.window():
            return
        
        if self.aspect_ratio is not None:
            self.updating_aspect_ratio = True
            
            frame = self.window().frame()
            currentSize = frame.size
            scaledSize = currentSize
            scaledSize.height = scaledSize.width / self.aspect_ratio
            frame.size = scaledSize
            self.window().setFrame_display_animate_(frame, True, False)

            mask = self.videoView.autoresizingMask()
            frame = self.videoView.superview().frame()
            currentSize = frame.size
            scaledSize = currentSize
            scaledSize.height = scaledSize.width / self.aspect_ratio
            if abs(scaledSize.height - self.window().frame().size.height) < 1:
                scaledSize.height = self.window().frame().size.height
    
            if scaledSize.height > self.window().frame().size.height:
                scaledSize.height = self.window().frame().size.height
                scaledSize.width = scaledSize.height * self.aspect_ratio
            frame.size = scaledSize
            
            self.videoView.setFrame_(frame)
            origin = NSMakePoint(
                                 (NSWidth(self.videoView.superview().bounds()) - NSWidth(self.videoView.frame())) / 2,
                                 (NSHeight(self.videoView.superview().bounds()) - NSHeight(self.videoView.frame())) / 2)
            self.videoView.setFrameOrigin_(origin)
            self.videoView.setAutoresizingMask_(mask)

        self.updating_aspect_ratio = False

    @objc.python_method
    @run_in_gui_thread
    def show(self):
        if self.closed:
            return

        if self.will_close:
            return

        self.sessionController.log_debug("Show %s" % self)
        self.init_window()

        if self.window() is None:
            # init_window() bailed because the stream isn't materialised
            # yet. Nothing we can do until that lands.
            return

        if self.sessionController.video_consumer == "standalone":
            is_connected = (self.streamController.status == STREAM_CONNECTED)

            if is_connected:
                # Connected layout: main view = remote video, corner
                # thumbnail = local camera.
                first_time = not self.flipped
                if first_time:
                    # CRITICAL: switching videoView's producer
                    # in-place from LOCAL to REMOTE races with pjsip's
                    # libswscale worker thread, which can dereference
                    # the converter's sws_ctx after we've torn the old
                    # one down but before the new one exists
                    # (EXC_BAD_ACCESS at 0x40 in
                    # libswscale_conv_convert). Doing it as a clean
                    # two-step (detach now, attach after the GUI runloop
                    # has drained) gives pjsip a chance to serialize the
                    # converter teardown so the worker thread isn't
                    # mid-conversion when we re-init.
                    try:
                        self.videoView.setProducer(None)
                    except Exception:
                        pass
                    self.myVideoView.setProducer(SIPApplication.video_device.producer)
                    self._crossfade_into_connected_layout()
                    from util import call_later

                    def _attach_remote():
                        if self.closed or self.will_close:
                            return
                        if not self.streamController or \
                                not self.streamController.stream:
                            return
                        try:
                            self.videoView.setProducer(
                                self.streamController.stream.producer)
                        except Exception as exc:
                            BlinkLogger().log_info(
                                "Late videoView attach failed: %s" % exc)
                    call_later(0.25, _attach_remote)
                else:
                    # Subsequent show() passes: producers are already
                    # set the way we want. Don't re-call setProducer
                    # (no-op anyway, but safer not to touch pjsip).
                    self.myVideoView.setAlphaValue_(1.0)
                    self.myVideoView.setHidden_(self.local_video_hidden)
                self.flipped = True
            else:
                # Preview layout (used during outgoing call setup): the
                # main view IS the local camera, full window. No PiP
                # thumbnail yet — there's nothing to compare it against.
                self.videoView.setProducer(SIPApplication.video_device.producer)
                try:
                    self.myVideoView.setProducer(None)
                except Exception:
                    pass
                self.myVideoView.setHidden_(True)
                self.myVideoView.setAlphaValue_(0.0)
        else:
            self.flipped = True

        self.updateAspectRatio()
        self.showButtons()
        self.repositionMyVideo()

        if not self.window().isVisible():
            self.window().makeKeyAndOrderFront_(self)

        self.update_encryption_icon()

    @objc.python_method
    def _crossfade_into_connected_layout(self):
        """Animate the transition from the full-window preview (local
        camera) to the connected layout (remote video + corner thumb).
        The producer swap on the main view has already happened by the
        time this runs; we just fade the corner thumb in so its
        appearance isn't a hard pop."""
        # Position the thumb in the user's chosen corner with alpha 0
        # so it can fade up cleanly.
        if self.local_video_hidden:
            return
        try:
            self.myVideoView.setHidden_(False)
            self.myVideoView.setAlphaValue_(0.0)
        except Exception:
            pass
        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.45)
            self.myVideoView.animator().setAlphaValue_(1.0)
        finally:
            NSAnimationContext.endGrouping()

    def windowDidBecomeKey_(self, notification):
        if self.closed:
            return
        self.repositionMyVideo()

    @objc.python_method
    def repositionMyVideo(self):
        if self.closed or self.myVideoView is None:
            return
        self.myVideoView.layoutInSuperview()

    @objc.python_method
    def myVideoTopInset(self):
        """How far below the top edge the thumbnail keeps.

        In full screen the menu bar and the title bar slide down over the
        top of the picture when the pointer reaches it, and a thumbnail
        parked in a top corner disappears under them; the notch takes a
        strip as well. Windowed, the title bar is outside the content.
        """
        if not self.full_screen or self.window() is None:
            return MY_VIDEO_MARGIN
        titlebar = 28.0
        try:
            probe = NSMakeRect(0, 0, 100, 100)
            titlebar = NSWindow.frameRectForContentRect_styleMask_(probe, 1).size.height - 100.0
        except Exception:
            pass
        menubar = 24.0
        try:
            menubar = max(menubar, NSApp.mainMenu().menuBarHeight())
        except Exception:
            pass
        try:
            menubar = max(menubar, self.window().screen().safeAreaInsets().top)
        except Exception:
            pass
        return MY_VIDEO_MARGIN + titlebar + menubar

    @objc.python_method
    def myVideoBottomInset(self, x, width):
        """Keep a bottom-corner thumbnail clear of the call bar it would cover."""
        bar = self.buttonsView
        if bar is None or bar.isHidden():
            return MY_VIDEO_MARGIN
        frame = bar.frame()
        if x + width <= frame.origin.x or x >= frame.origin.x + frame.size.width:
            return MY_VIDEO_MARGIN
        return frame.origin.y + frame.size.height + MY_VIDEO_MARGIN

    def windowWillResize_toSize_(self, window, frameSize):
        if self.closed:
            return frameSize

        if self.full_screen_in_progress or self.full_screen:
            return frameSize

        scaledSize = frameSize
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / self.aspect_ratio or 1.77
        
        # Width below which the call bar + PIP get auto-hidden because
        # they no longer fit. The bar drops its labels first (see
        # VideoCallBar.tile); only when not even its icons fit does it go.
        #
        # Previous value 665 was the old fixed-size minimum, and
        # combined with the auto-hide it produced the "I shrank the
        # window and now the buttons are gone for good and I can't
        # bring them back" trap reported in the field. The xib's
        # minSize is now 200x150; the new 290 threshold means the
        # chrome stays available across most of the usable resize
        # range, and even when it does auto-hide the on-mouseDown
        # showButtons() fallback in mouseDown_ at least gives a
        # visible toggle.
        if scaledSize.width < self.buttonsView.minimumWindowWidth():
            self.window_too_small = True
            self.hideButtons()
            self.myVideoView.hide()
        else:
            self.window_too_small = False
            self.showButtons()
            if not self.local_video_hidden:
                self.myVideoView.show()
        return scaledSize

    def windowDidResize_(self, notification):
        if self.closed:
            return

        if not self.streamController.stream:
            return

        if self.updating_aspect_ratio:
            return

        self.updateTrackingAreas()

        # Re-centres the bar, and trades its labels for room when narrow.
        self.buttonsView.tile()

        # Keep the status-label pill horizontally centered on resize but
        # let the xib's anchor-to-bottom autoresizing keep it just above
        # the buttons bar — the legacy code here recentered it
        # vertically too, which pinned the label to the middle of the
        # video and overrode the xib position.
        status_view = self.disconnectLabel.superview()
        mask = status_view.autoresizingMask()
        sf = status_view.frame()
        sf.origin.x = (NSWidth(status_view.superview().bounds())
                       - NSWidth(sf)) / 2
        status_view.setFrameOrigin_(sf.origin)
        status_view.setAutoresizingMask_(mask)
    
    @objc.python_method
    @run_in_gui_thread
    def hide(self):
        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if self.window():
            self.window().orderOut_(self)
            if self.myVideoView:
                self.myVideoView.hide()

        self.hideButtons()

    @objc.python_method
    def removeVideo(self):
        self.window().orderOut_(None)
        if self.sessionController:
            self.sessionController.removeVideoFromSession()
        NSApp.delegate().contactsWindowController.showAudioDrawer()
    
    @objc.python_method
    @run_in_gui_thread
    def goToFullScreen(self):
        if self.closed or self.will_close:
            return
        self.sessionController.log_debug('goToFullScreen %s' % self)
        if not self.full_screen:
            self.window().toggleFullScreen_(None)

    @objc.python_method
    @run_in_gui_thread
    def goToWindowMode(self, window=None):
        if self.full_screen:
            self.show_window_after_full_screen_ends = window
            self.window().toggleFullScreen_(None)

    @objc.python_method
    @run_in_gui_thread
    def toggleFullScreen(self):
        self.sessionController.log_debug('toggleFullScreen %s' % self)

        if self.full_screen_in_progress:
            return

        self.full_screen_in_progress = True

        if self.full_screen:
            self.goToWindowMode()
        else:
            self.goToFullScreen()

    def windowWillEnterFullScreen_(self, notification):
        self.full_screen_in_progress = True

    def windowWillExitFullScreen_(self, notification):
        self.full_screen_in_progress = True

    def windowDidEnterFullScreen_(self, notification):
        sc = self.sessionController
        if sc is not None:
            sc.log_debug('windowDidEnterFullScreen_ %s' % self)
        if self.closed or self.streamController is None or self.streamController.ended:
            self.full_screen = True
            self.full_screen_in_progress = False
            if self.closed:
                # Leaves full screen first, then closes -- from the next
                # run loop pass, not from inside AppKit's transition callback.
                self._scheduleCloseTimer(0.1)
            else:
                self.window().orderOut_(self)
            return

        self.full_screen_in_progress = False
        self.full_screen = True
        self.stopMouseOutTimer()
        self.fullScreenButton.configure('arrow.down.right.and.arrow.up.left',
                                        NSLocalizedString("Exit", "Video call bar"))

        self.repositionMyVideo()

        self.showButtons()

        if self.window():
            self.window().setLevel_(NSNormalWindowLevel)

    def windowDidExitFullScreen_(self, notification):
        sc = self.sessionController
        if sc is not None:
            sc.log_debug('windowDidExitFullScreen %s' % self)
        if self.closed:
            # The call ended while in full screen: the close was waiting for
            # this. It is finished from the next run loop pass; closing the
            # window from inside the transition callback can leave it frozen.
            self.full_screen = False
            self.full_screen_in_progress = False
            self._scheduleCloseTimer(0.1)
            return
        self.fullScreenButton.configure('arrow.up.left.and.arrow.down.right',
                                        NSLocalizedString("Full", "Video call bar"))

        self.full_screen_in_progress = False
        self.full_screen = False
        # The top inset for the menu and title bars no longer applies.
        self.repositionMyVideo()

        # Recording is not tied to full screen any more: the recorder
        # takes decoded frames off the stream, not pixels off the
        # screen, so leaving full screen is none of its business.

        if self.show_window_after_full_screen_ends is not None:
            self.show_window_after_full_screen_ends.makeKeyAndOrderFront_(None)
            self.show_window_after_full_screen_ends = None
        else:
            if self.window():
                if self.streamController.ended or self.must_hide_after_exit_full_screen:
                    self.must_hide_after_exit_full_screen = False
                    self.window().orderOut_(self)
                else:
                    self.window().orderFront_(self)
                    self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

        self.updateAspectRatio()

    def windowWillClose_(self, sender):
        # A raise in here aborts -[NSWindow close] and leaves the window on
        # screen, and every later attempt to close it raises again -- a
        # window nobody can get rid of. Whatever goes wrong is logged instead.
        try:
            self._windowWillClose()
        except Exception:
            BlinkLogger().log_error('Video window windowWillClose failed: %s' % traceback.format_exc())

    @objc.python_method
    def _windowWillClose(self):
        # `self.sessionController` is now a property that legitimately
        # returns None once the underlying VideoController has been
        # ObjC-deallocated (e.g. at app exit, after the call ended and
        # the 5-second deallocTimer has fired). AppKit still delivers
        # windowWillClose_ during the app's shutdown drain, and the
        # old `self.sessionController.log_debug(...)` then crashed with
        # 'NoneType' object has no attribute 'log_debug'. Use a local
        # binding + None-check so the late-fire path is harmless.
        sc = self.sessionController
        if sc is not None:
            sc.log_debug('windowWillClose %s' % self)
        self.will_close = True
        # Release the camera consumers immediately so the Mac camera
        # LED turns off the moment the window starts closing — without
        # waiting for the SIP teardown / VideoController.end() chain to
        # finish. The full close() pass below (when it eventually runs)
        # is idempotent: setProducer(None) on an already-released view
        # is a no-op.
        if self.myVideoView:
            try:
                self.myVideoView.setProducer(None)
            except Exception as e:
                BlinkLogger().log_debug(
                    "windowWillClose myVideoView.setProducer(None) ignored: %s" % e)
        if self.videoView:
            try:
                self.videoView.setProducer(None)
            except Exception as e:
                BlinkLogger().log_debug(
                    "windowWillClose videoView.setProducer(None) ignored: %s" % e)
        if self.sessionController:
            self.sessionController.removeVideoFromSession()
            if not self.sessionController.hasStreamOfType("chat"):
                NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

    def windowShouldClose_(self, sender):
        # See windowWillClose_ — same late-fire guard.
        sc = self.sessionController
        if sc is not None:
            sc.log_debug('windowShouldClose_ %s' % self)
        return True

    @objc.python_method
    @run_in_gui_thread
    def close(self):
        """Tear the window down at the end of the video stream.

        Every step is on its own: one that raises is logged with its
        traceback and the rest still run, and the window is always taken
        off screen at the end. A single raise in here used to leave a
        window that was marked closed -- so every handler ignored input --
        but was still showing, and could not be closed by hand either.
        """
        if self.closed:
            # Torn down already. If the window is somehow still up, take it away.
            self._closeWindow()
            return
        self.closed = True

        sc = self.sessionController
        if sc is not None:
            sc.log_debug('Close remote %s' % self)

        def step(name, function):
            try:
                function()
            except Exception:
                BlinkLogger().log_error('Video window close: %s failed: %s' % (name, traceback.format_exc()))

        def discard_observers():
            nc = self.notification_center
            self.notification_center = None
            if nc is None:
                return
            recorder = self.streamController.videoRecorder if self.streamController is not None else None
            if recorder is not None:
                nc.discard_observer(self, sender=recorder)
            for name in ('BlinkMuteChangedState', 'BlinkAudioStreamChangedHoldState',
                         'VideoDeviceDidChangeCamera', 'BlinkScreenPointerDidChange',
                         'BlinkScreenPointerGotAck'):
                nc.discard_observer(self, name=name)

        # The camera first: anything that goes wrong later must not leave
        # the AVCaptureSession, and the camera LED, running.
        #
        # The outlets are cleared before the views are closed. An IBOutlet
        # does not retain: the superview is the only owner, and
        # VideoWidget.close() takes the view out of it, so once the Python
        # references below are gone the view is freed and an outlet still
        # pointing at it is a dangling pointer. The next read of the outlet
        # then dies in objc_msgSend -- windowWillClose_ does exactly that
        # when the window close is finished later, by forceCloseWindow_ or
        # windowDidExitFullScreen_ after a call that ended in full screen.
        views = [view for view in (self.myVideoView, self.videoView) if view]
        self.myVideoView = None
        self.videoView = None
        for view in views:
            step('releasing a video producer', lambda view=view: view.setProducer(None))
            step('closing a video view', lambda view=view: view.close())
        del views
        step('discarding observers', discard_observers)

        def close_zrtp():
            if self.zrtp_controller:
                self.zrtp_controller.close()
                self.zrtp_controller = None
        step('closing ZRTP', close_zrtp)

        step('hiding the call bar', self.hideButtons)
        step('stopping the recording timer', self.stopRecordingTimer)
        step('stopping the stats timer', self.stopStatsOverlayTimer)
        step('hiding the note', lambda: self.hideToast(animate=False))
        step('leaving pointer mode', self.closePointer)
        step('stopping the idle timer', self.stopIdleTimer)
        step('stopping the mouse timer', self.stopMouseOutTimer)
        step('removing tracking areas', self.closeTrackingAreas)

        def close_local_window():
            if self.localVideoWindow:
                self.localVideoWindow.close()
        step('closing the local video window', close_local_window)

        self._closeWindow()

    @objc.python_method
    def _windowIsFullScreen(self):
        window = self.window()
        try:
            return window is not None and bool(window.styleMask() & FULL_SCREEN_STYLE_MASK)
        except Exception:
            return False

    @objc.python_method
    def _scheduleCloseTimer(self, interval):
        # One pending close step at a time: a timer left over from an earlier
        # transition must not fire in the middle of a later one and close the
        # window while it is on its way out of full screen.
        self._stopCloseTimer()
        self.close_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, "forceCloseWindow:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_timer, NSRunLoopCommonModes)

    @objc.python_method
    def _stopCloseTimer(self):
        timer = self.close_timer
        self.close_timer = None
        if timer is not None and timer.isValid():
            timer.invalidate()

    @objc.python_method
    def _closeWindow(self):
        """Take the window off screen and close it, full screen or not.

        Closing or ordering out a window while it is in, or on its way into
        or out of, full screen leaves a frozen window behind. In that case
        the window is first taken out of full screen and the close is
        finished once the transition reports back (windowDidEnterFullScreen_,
        windowDidExitFullScreen_, or one of the failure callbacks), with a
        timer in case none of them comes.
        """
        window = self.window()
        if window is None:
            return
        sc = self.sessionController
        in_full_screen = self.full_screen or self._windowIsFullScreen()
        if self.full_screen_in_progress or in_full_screen:
            self.close_after_exit_full_screen = True
            if self.full_screen_in_progress:
                if sc is not None:
                    sc.log_info('Video window close: waiting for the full screen transition to finish')
            else:
                if sc is not None:
                    sc.log_info('Video window close: leaving full screen first')
                self.full_screen_in_progress = True
                try:
                    window.toggleFullScreen_(None)
                except Exception:
                    self.full_screen_in_progress = False
                    BlinkLogger().log_error('Video window close: leaving full screen failed: %s'
                                            % traceback.format_exc())
            self._scheduleCloseTimer(FULL_SCREEN_TRANSITION_TIMEOUT)
            return
        self._stopCloseTimer()
        self.close_after_exit_full_screen = False
        try:
            window.orderOut_(None)
        except Exception:
            BlinkLogger().log_error('Video window close: orderOut failed: %s' % traceback.format_exc())
        try:
            window.close()
        except Exception:
            BlinkLogger().log_error('Video window close: close failed: %s' % traceback.format_exc())

    def forceCloseWindow_(self, timer):
        self.close_timer = None
        if not self.closed or self.window() is None:
            return
        self.close_attempts += 1
        if self.close_attempts > 4:
            # Full screen never let go. Close it regardless rather than keep
            # a dead window around forever.
            sc = self.sessionController
            if sc is not None:
                sc.log_info('Video window close: full screen did not end, closing anyway')
            self.full_screen = False
            self.full_screen_in_progress = False
            self._forceWindowOut()
            return
        # Either a transition finished and the close continues from here, or
        # the transition never reported back. In both cases the window's own
        # style mask says whether it is still in full screen.
        self.full_screen_in_progress = False
        self.full_screen = self._windowIsFullScreen()
        self._closeWindow()

    @objc.python_method
    def _forceWindowOut(self):
        self._stopCloseTimer()
        self.close_after_exit_full_screen = False
        window = self.window()
        if window is None:
            return
        try:
            window.orderOut_(None)
            window.close()
        except Exception:
            BlinkLogger().log_error('Video window close: forced close failed: %s' % traceback.format_exc())

    def windowDidFailToEnterFullScreen_(self, window):
        self.full_screen_in_progress = False
        self.full_screen = self._windowIsFullScreen()
        if self.closed:
            self._scheduleCloseTimer(0.1)

    def windowDidFailToExitFullScreen_(self, window):
        self.full_screen_in_progress = False
        self.full_screen = self._windowIsFullScreen()
        if self.closed:
            self._scheduleCloseTimer(0.1)

    def dealloc(self):
        # sessionController property can legitimately be None at app
        # exit (VideoController dealloced first). Guard the log call.
        sc = self.sessionController
        if sc is not None:
            sc.log_debug("Dealloc %s" % self)
        self.flipWnd = None

        self.tracking_area = None
        self.streamController = None
        self.localVideoWindow = None
        objc.super(VideoWindowController, self).dealloc()

    @objc.python_method
    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

    def toogleAlwaysOnTop_(self, sender):
        self.toogleAlwaysOnTop()

    @objc.python_method
    def stopRecordingTimer(self):
        if self.recording_timer is not None and self.recording_timer.isValid():
            self.recording_timer.invalidate()
        self.recording_timer = None

    @objc.python_method
    def stopStatsOverlayTimer(self):
        timer = getattr(self, 'stats_overlay_timer', None)
        if timer is not None and timer.isValid():
            timer.invalidate()
        self.stats_overlay_timer = None

    @objc.python_method
    def stopMouseOutTimer(self):
        if self.mouse_timer is not None:
            if self.mouse_timer.isValid():
                self.mouse_timer.invalidate()
            self.mouse_timer = None

    @objc.python_method
    def startMouseOutTimer(self):
        if self.mouse_timer is None:
            self.mouse_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3, self, "mouseOutTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.mouse_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.mouse_timer, NSEventTrackingRunLoopMode)

    def mouseOutTimer_(self, timer):
        self.hideButtons()
        self.mouse_timer = None

    @objc.python_method
    def getSecondaryScreen(self):
        try:
            secondaryScreen = next((screen for screen in NSScreen.screens() if screen != NSScreen.mainScreen() and screen.deviceDescription()[NSDeviceIsScreen] == 'YES'))
        except (StopIteration, KeyError):
            secondaryScreen = None
        return secondaryScreen

    @objc.IBAction
    def userClickedLocalVideo_(self, sender):
        if self.closed or not self.myVideoView:
            return
        self.local_video_hidden = not self.local_video_hidden
        if self.local_video_hidden:
            self.myVideoView.hide()
        else:
            self.myVideoView.show()

    @objc.IBAction
    def userClickedFullScreenButton_(self, sender):
        self.toggleFullScreen()

    @objc.IBAction
    def userClickedOpenScreenshotFolder_(self, sender):
        NSWorkspace.sharedWorkspace().openFile_(sender.representedObject())

    @objc.IBAction
    def userClickedAspectButton_(self, sender):
        self.changeAspectRatio()

    @objc.IBAction
    def userClickedMuteButton_(self, sender):
        SIPManager().mute(not SIPManager().is_muted())
        self.updateMuteButton()

    @objc.IBAction
    def userClickedRecordButton_(self, sender):
        if self.streamController.videoRecorder:
            self.streamController.videoRecorder.toggleRecording()

    @objc.IBAction
    def userClickedInfoButton_(self, sender):
        if self.sessionController and self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    @objc.IBAction
    def userClickedVerifyPeer_(self, sender):
        if not self.streamController.zrtp_active:
            return

        if self.zrtp_controller is None:
            self.zrtp_controller = ZRTPAuthentication(self.streamController)
        self.zrtp_controller.open()

    @objc.IBAction
    def userClickedHoldButton_(self, sender):
        if not self.sessionController:
            return

        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream and audio_stream.status == STREAM_CONNECTED and not self.sessionController.inProposal:
                if audio_stream.holdByLocal:
                    audio_stream.unhold()
                    audio_stream.view.setSelected_(True)
                else:
                    audio_stream.hold()

    @objc.IBAction
    def userClickedHangupButton_(self, sender):
        # Ordering out a window that is in, or entering or leaving, full
        # screen freezes it; close() takes it out of full screen instead.
        if not (self.full_screen or self.full_screen_in_progress or self._windowIsFullScreen()):
            self.window().orderOut_(None)
        if self.sessionController:
            self.sessionController.end()

    @objc.IBAction
    def userClickedContactsButton_(self, sender):
        if self.full_screen:
            self.toggleFullScreen()
        NSApp.delegate().contactsWindowController.focusSearchTextField()

    @objc.IBAction
    def userClickedChatButton_(self, sender):
        if self.sessionController and self.chatButtonAllowed():
            self.sessionController.addChatToSession()

    @objc.IBAction
    def userClickedAttachToChatMenuItem_(self, sender):
        self.sessionController.setVideoConsumer("chat")

    @objc.IBAction
    def userClickedAttachToAudioMenuItem_(self, sender):
        self.sessionController.setVideoConsumer("audio")
        NSApp.delegate().contactsWindowController.showAudioDrawer()

    @objc.IBAction
    def userClickedInfoButton_(self, sender):
        if self.sessionController and self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    @objc.IBAction
    def userClickedPauseButton_(self, sender):
        self.pauseButton.setImage_(NSImage.imageNamed_("video-paused" if not self.streamController.paused else "video"))
        self.streamController.togglePause()

    @objc.IBAction
    def userClickedScreenshotButton_(self, sender):
        """Take a screenshot, and say so where the user is looking.

        The sound and the log line were all the feedback there was, and
        neither says where the file went. The note appears once screencapture
        has actually finished, with a way to the file.
        """
        if self.screenshot_task is not None:
            return          # one at a time: the previous one is still being written
        filename = self.screenshot_filename()
        self.screencapture_file = filename
        self.screenshot_task = NSTask.alloc().init()
        self.screenshot_task.setLaunchPath_('/usr/sbin/screencapture')
        self.screenshot_task.setArguments_(['-tpng', filename])
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "localScreenshotDidFinish:", NSTaskDidTerminateNotification, self.screenshot_task)
        self.screenshot_task.launch()

    def localScreenshotDidFinish_(self, notification):
        task = notification.object()
        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, NSTaskDidTerminateNotification, task)
        filename = self.screencapture_file
        self.screenshot_task = None
        self.screencapture_file = None
        if self.closed:
            return
        if task.terminationStatus() == 0 and filename and os.path.exists(filename):
            NSSound.soundNamed_("Grab").play()
            if self.sessionController:
                self.sessionController.log_info("Screenshot saved in %s" % filename)
            self.showToast(NSLocalizedString("Screenshot saved", "Video window note"),
                           NSLocalizedString("Show in Finder", "Button title"),
                           lambda: self.revealInFinder(filename))
        else:
            if self.sessionController:
                self.sessionController.log_info("Screenshot failed (status %d)" % task.terminationStatus())
            self.showToast(NSLocalizedString("Screenshot failed", "Video window note"))

    @objc.python_method
    def revealInFinder(self, filename):
        if os.path.exists(filename):
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(filename)])
        else:
            NSWorkspace.sharedWorkspace().openFile_(os.path.dirname(filename))

    @objc.python_method
    def showToast(self, text, action_title=None, callback=None, seconds=TOAST_SECONDS):
        window = self.window()
        if window is None:
            return
        content = window.contentView()
        if self.toastView is None:
            self.toastView = VideoToast.alloc().initWithFrame_(NSMakeRect(0, 0, 200, TOAST_HEIGHT))
            if self.buttonsView is not None and self.buttonsView.superview() is content:
                content.addSubview_positioned_relativeTo_(self.toastView, NSWindowAbove, self.buttonsView)
            else:
                content.addSubview_(self.toastView)
        toast = self.toastView
        toast.configure(text, action_title, callback)
        toast.setHidden_(False)
        toast.setAlphaValue_(0.0)
        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.18)
            toast.animator().setAlphaValue_(1.0)
        finally:
            NSAnimationContext.endGrouping()

        if self.toast_timer is not None and self.toast_timer.isValid():
            self.toast_timer.invalidate()
        self.toast_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            seconds, self, "toastTimerFired:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.toast_timer, NSRunLoopCommonModes)

    def toastTimerFired_(self, timer):
        self.toast_timer = None
        self.hideToast()

    @objc.python_method
    def hideToast(self, animate=True):
        if self.toast_timer is not None and self.toast_timer.isValid():
            self.toast_timer.invalidate()
        self.toast_timer = None
        toast = self.toastView
        if toast is None:
            return
        if not animate:
            toast.setAlphaValue_(0.0)
            return
        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.25)
            toast.animator().setAlphaValue_(0.0)
        finally:
            NSAnimationContext.endGrouping()

    @objc.python_method
    def screenshot_filename(self, for_remote=False):
        screenshots_folder = ApplicationData.get('screenshots')
        if not os.path.exists(screenshots_folder):
           os.mkdir(screenshots_folder, 0o700)

        label = format_identity_to_string(self.sessionController.target_uri) if not for_remote else self.sessionController.account.id
        filename = '%s/%s_screencapture_%s.png' % (screenshots_folder, datetime.datetime.now(tzlocal()).strftime("%Y-%m-%d_%H-%M"), label)
        basename, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(filename):
            filename = '%s_%d%s' % (basename, i, ext)
            i += 1
        return filename

    @objc.IBAction
    def userClickedSendScreenshotButton_(self, sender):
        if self.screenshot_task is not None:
            return          # shares the task slot with Screenshot; one at a time
        filename = self.screenshot_filename(True)
        self.screencapture_file = filename
        self.screenshot_task = NSTask.alloc().init()
        self.screenshot_task.setLaunchPath_('/usr/sbin/screencapture')
        self.screenshot_task.setArguments_(['-tpng', filename])
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "checkScreenshotTaskStatus:", NSTaskDidTerminateNotification, self.screenshot_task)
        
        self.screenshot_task.launch()
        NSSound.soundNamed_("Grab").play()
        self.sessionController.log_info("Screenshot saved in %s" % filename)

    def checkScreenshotTaskStatus_(self, notification):
        status = notification.object().terminationStatus()
        filename = self.screencapture_file
        sent = False
        if status == 0 and self.sessionController and filename and os.path.exists(filename):
            sent = self.sendFiles([str(filename)])
        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, NSTaskDidTerminateNotification, self.screenshot_task)
        self.screenshot_task = None
        self.screencapture_file = None
        if self.closed:
            return
        if sent:
            self.showToast(NSLocalizedString("Screenshot sent", "Video window note"),
                           NSLocalizedString("Show in Finder", "Button title"),
                           lambda: self.revealInFinder(filename))
        else:
            self.showToast(NSLocalizedString("Screenshot could not be sent", "Video window note"))

    @objc.python_method
    def sendFiles(self, fnames):
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file) or os.path.isdir(file)]
        if filenames:
            self.sessionController.sessionControllersManager.send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, filenames)
            return True
        return False

    def updateIdleTimer_(self, timer):
        if not self.sessionController:
            return
        last_idle_counter = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventMouseMoved)
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if not chat_stream:
            if self.show_time is not None and time.time() - self.show_time < IDLE_TIME:
                return
        
        if last_idle_counter > IDLE_TIME:
            self.show_time = None
            if not self.is_idle:
                if self.visible_buttons:
                    self.hideButtons()
                self.is_idle = True
        else:
            if not self.visible_buttons:
                self.showButtons()
            self.is_idle = False

    @objc.python_method
    def hideButtons(self):
        if not self.window():
            return

        if not self.window().isVisible():
            return

        # Smooth ~250 ms fade-out via Core Animation, instead of an
        # abrupt hidden-flag flip. A faded bar stops taking clicks
        # (VideoCallBar.hitTest_), so they reach the video and bring it back.
        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.25)
            self.buttonsView.animator().setAlphaValue_(0.0)
        finally:
            NSAnimationContext.endGrouping()

        self.visible_buttons = False

    @objc.python_method
    def chatButtonAllowed(self):
        """Whether the Chat button on the call bar has anything to offer.

        The button proposes an MSRP chat stream on this session, so it follows
        chat.enable_msrp_chat -- except for a conference, whose chat is part of
        the room rather than a one-to-one conversation. allowsStreamOfType()
        applies exactly that rule.
        """
        if self.sessionController is None:
            return False
        try:
            return self.sessionController.allowsStreamOfType('chat')
        except AttributeError:
            return False

    @objc.python_method
    def showButtons(self):
        if not self.window():
            return

        if not self.window().isVisible():
            return

        if self.window_too_small:
            self.hideButtons()
            return

        # Make sure nothing's set hidden from a previous code path
        # (legacy behavior used setHidden_ extensively); alpha 0 is
        # what we use to hide now.
        #
        # NOTE: holdButton is deliberately excluded — putting a video
        # call on hold doesn't currently work cleanly and the button
        # has been a source of "why is the picture frozen" tickets,
        # so the affordance is removed from the call bar entirely
        # for video. (Audio calls still have hold via the audio bar.)
        # The xib still defines the button as an IBOutlet so legacy
        # state-update code in show() can run without crashing; we
        # just keep it permanently hidden.
        try:
            self.buttonsView.setHidden_(False)
            for btn in (self.fullScreenButton,
                        self.hangupButton, self.chatButton,
                        self.infoButton, self.muteButton,
                        self.cameraButton, self.screenshotButton,
                        self.recordButton):
                if btn is not None:
                    btn.setHidden_(False)
            if self.holdButton is not None:
                self.holdButton.setHidden_(True)
            # With MSRP chat switched off the Chat button would propose a
            # stream that gets refused, so it stays out of the call bar. A
            # conference keeps it -- see chatButtonAllowed().
            if self.chatButton is not None and not self.chatButtonAllowed():
                self.chatButton.setHidden_(True)
        except Exception:
            pass

        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.18)
            self.buttonsView.animator().setAlphaValue_(1.0)
        finally:
            NSAnimationContext.endGrouping()

        self.visible_buttons = True

    def updateRecordingTimer_(self, timer):
        if not self.streamController or not self.streamController.videoRecorder:
            return

        if self.streamController.videoRecorder.isRecording():
            self.recordButton.setToolTip_(NSLocalizedString("Stop Recording", "Label"))
            # Blinks red on the timer's half-second beat.
            self.recordingImage = (self.recordingImage + 1) % 2
            tint = NSColor.systemRedColor() if self.recordingImage == 0 else _call_bar_white(0.5)
            self.recordButton.configure('record.circle.fill',
                                        NSLocalizedString("Stop", "Video call bar"), tint=tint)
        else:
            self.recordButton.setToolTip_(NSLocalizedString("Start Recording", "Label"))
            self.recordButton.configure('record.circle', NSLocalizedString("Record", "Video call bar"))

    @objc.python_method
    def update_encryption_icon(self):
        if not self.window():
            return

        if not self.streamController:
            return

        if not self.streamController.stream:
            return

        if self.streamController.zrtp_active:
            if self.streamController.zrtp_verified:
                image = 'locked-green'
            else:
                image = 'locked-orange'
        elif self.streamController.srtp_active:
            image = 'locked-orange'
        else:
            image = 'unlocked-darkgray'

        title = NSLocalizedString("Video with %s", "Window title") % self.title
        self.window().setRepresentedURL_(NSURL.fileURLWithPath_(title))
        self.window().standardWindowButton_(NSWindowDocumentIconButton).setImage_(NSImage.imageNamed_(image))

class RoundedCornersView(NSView):
    def hide(self):
        self.setHidden_(True)
    
    def show(self):
        self.setHidden_(False)
    
    def drawRect_(self, dirtyRect):
        # Deliberately do NOT fill a translucent black background here.
        # The previous 30%-black SourceOver fill dimmed the Metal video
        # underneath this view to ~70% brightness whenever the bar was
        # visible. Keep the chrome transparent so the video stays crisp;
        # the button images already carry enough contrast on their own.
        objc.super(RoundedCornersView, self).drawRect_(dirtyRect)


class BlackView(NSView):
    def drawRect_(self, dirtyRect):
        NSColor.blackColor().set()
        NSRectFill(dirtyRect)
