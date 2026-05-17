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
                    NSWindowDocumentIconButton
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
import unicodedata

from math import floor
from dateutil.tz import tzlocal


from application.notification import NotificationCenter
from resources import ApplicationData
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import VideoCamera, Engine, FrameBufferVideoRenderer
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
                    CGAffineTransformMakeScale,
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

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', b'diI')])

IDLE_TIME = 5


RecordingImages = []
def loadRecordingImages():
    if not RecordingImages:
        RecordingImages.append(NSImage.imageNamed_("recording1"))
        RecordingImages.append(NSImage.imageNamed_("recording2"))
        RecordingImages.append(NSImage.imageNamed_("recording3"))


class VideoWidget(NSView):
    _frame = None
    renderer = None
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
                # Tag the drawable as sRGB-encoded so CoreAnimation maps
                # the bytes correctly to the display's profile.
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
            # Opt INTO EDR. On macOS 26 Tahoe with Apple Silicon, the
            # call window's fullScreenPrimary collection-behaviour puts
            # it into the EDR compositor whether we like it or not.
            # When the layer is tagged EDR=False inside an EDR-active
            # window, the compositor tone-maps our SDR pixels to a
            # fraction of SDR-reference white — visibly dim against
            # the surrounding AppKit chrome and against the Preferences
            # preview (whose host window stays out of EDR mode entirely
            # because it isn't fullScreenPrimary). EDR=True keeps the
            # layer at the display's SDR-reference white level, which
            # is the brightness the user perceives as "normal". Our
            # pixels are in [0, 1] so we never actually emit values
            # above SDR-reference — EDR=True only changes the compositor
            # behaviour, not the visual range we produce.
            try:
                layer.setWantsExtendedDynamicRangeContent_(True)
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

        try:
            BlinkLogger().log_info(
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
        #BlinkLogger().log_debug("%s setProducer %s" % (self, producer))
        # Detect whether this widget is displaying the local camera so we
        # can mirror it horizontally like FaceTime / Zoom / Meet. This is
        # purely a display transform on our own layer; the remote side
        # always sees the un-mirrored frame.
        try:
            local_producer = SIPApplication.video_device.producer
        except Exception:
            local_producer = None
        self._is_self_view = (producer is not None and producer is local_producer)
        self._apply_self_view_mirror()

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
        # Applies (or removes) a horizontal flip on the backing layer so the
        # user sees themselves the way a mirror would. Called from both
        # setProducer (when we learn which producer we have) and from
        # _setup_video_layer (in case the layer was created after
        # setProducer ran). Safe to call repeatedly.
        layer = self.layer()
        if layer is None:
            return
        try:
            if getattr(self, '_is_self_view', False):
                layer.setAffineTransform_(CGAffineTransformMakeScale(-1.0, 1.0))
            else:
                layer.setAffineTransform_(CGAffineTransformIdentity)
        except Exception as e:
            BlinkLogger().log_info(
                "VideoWidget mirror toggle failed: %s" % e)

    def close(self):
        BlinkLogger().log_debug("Close %s" % self)
        self.setProducer(None)
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

class myVideoWidget(VideoWidget):
    initialLocation = None
    initialOrigin = None
    auto_rotate_menu_enabled = True

    start_origin = None
    final_origin = None
    temp_origin = None
    is_dragging = False
    allow_drag = True

    # Reference width for the corner thumbnail. The thumbnail's height
    # is computed from this width + the camera's actual aspect ratio,
    # so a 4:3 camera shows as ~150x113 and a 16:9 camera as ~150x84.
    _thumbnail_width = 150.0

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

    @objc.python_method
    def _on_aspect_ratio_detected(self, width, height):
        # The corner thumbnail must not poke the call window's
        # init_aspect_ratio (that's reserved for the *remote* video
        # stream). Instead, resize ourselves so the thumbnail's outer
        # bounds match the local camera aspect, then re-snap to the
        # corner we're currently anchored to.
        if width <= 0 or height <= 0:
            return
        aspect = float(width) / float(height)
        target_w = float(self._thumbnail_width)
        target_h = max(40.0, target_w / aspect)
        cur = self.frame()
        if abs(cur.size.width - target_w) < 0.5 and \
                abs(cur.size.height - target_h) < 0.5:
            return
        # Keep the current top-left anchor consistent — snapToCorner
        # will fix the position below based on the chosen corner.
        new_frame = ((cur.origin.x, cur.origin.y), (target_w, target_h))
        self.setFrame_(new_frame)
        try:
            self.snapToCorner()
        except Exception as e:
            BlinkLogger().log_debug(
                "myVideoWidget snapToCorner after resize failed: %s" % e)

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

    def mouseDown_(self, event):
        if not self.allow_drag:
            return

        self.initialLocation = event.locationInWindow()
        self.initialOrigin = self.frame().origin
        self.final_origin = None

    def mouseUp_(self, event):
        if not self.allow_drag:
            return

        self.is_dragging = False
        self.goToFinalOrigin()

    @objc.python_method
    def goToFinalOrigin(self):
        if not self.allow_drag:
            return

        self.setFrameOrigin_(self.final_origin)
        self.start_origin = None
        self.final_origin = None

    def performDrag(self):
        if not self.allow_drag:
            return
        
        if not self.currentLocation:
            return

        newOrigin = self.frame().origin
        offset_x =  self.initialLocation.x - self.initialOrigin.x
        offset_y =  self.initialLocation.y - self.initialOrigin.y
        newOrigin.x = self.currentLocation.x - offset_x
        newOrigin.y = self.currentLocation.y - offset_y

        if newOrigin.x < 10:
            newOrigin.x = 10

        if newOrigin.y < 10:
            newOrigin.y = 10

        if self.window().delegate().full_screen:
            parentFrame = NSScreen.mainScreen().visibleFrame()
        else:
            parentFrame = self.window().frame()

        if newOrigin.x > parentFrame.size.width - 10 - self.frame().size.width:
            newOrigin.x = parentFrame.size.width - 10 - self.frame().size.width

        if newOrigin.y > parentFrame.size.height - 30 - self.frame().size.height:
            newOrigin.y = parentFrame.size.height - 30 - self.frame().size.height

        if ((newOrigin.y + self.frame().size.height) > (parentFrame.origin.y + parentFrame.size.height)):
            newOrigin.y = parentFrame.origin.y + (parentFrame.size.height - self.frame().size.height)

        if abs(newOrigin.x - self.window().delegate().myVideoViewTL.frame().origin.x) > abs(newOrigin.x - self.window().delegate().myVideoViewTR.frame().origin.x):
            letter2 = "R"
        else:
            letter2 = "L"

        if abs(newOrigin.y - self.window().delegate().myVideoViewTL.frame().origin.y) > abs(newOrigin.y - self.window().delegate().myVideoViewBL.frame().origin.y):
            letter1 = "B"
        else:
            letter1 = "T"

        finalFrame = "myVideoView" + letter1 + letter2
        self.start_origin = newOrigin
        self.final_origin = getattr(self.window().delegate(), finalFrame).frame().origin
        NSUserDefaults.standardUserDefaults().setValue_forKey_(letter1 + letter2, "MyVideoCorner")
        self.setFrameOrigin_(newOrigin)

    @objc.python_method
    def snapToCorner(self):
        delegate = self.window().delegate() if self.window() else None
        if delegate is None:
            return

        newOrigin = self.frame().origin
        if abs(newOrigin.x - delegate.myVideoViewTL.frame().origin.x) > abs(newOrigin.x - delegate.myVideoViewTR.frame().origin.x):
            letter2 = "R"
        else:
            letter2 = "L"

        if abs(newOrigin.y - delegate.myVideoViewTL.frame().origin.y) > abs(newOrigin.y - delegate.myVideoViewBL.frame().origin.y):
            letter1 = "B"
        else:
            letter1 = "T"

        # The four corner placeholder views in the xib are sized for the
        # default 150x84 thumbnail. If the thumbnail has been resized to
        # match the local camera's aspect ratio (Phase 1 follow-up), snap
        # by the *outer* edge of the placeholder that corresponds to the
        # window corner, so the thumb still hugs the window edge instead
        # of slipping inward or overshooting.
        placeholder = getattr(delegate, "myVideoView" + letter1 + letter2)
        placeholder_frame = placeholder.frame()
        size = self.frame().size

        if letter2 == "L":
            new_x = placeholder_frame.origin.x
        else:  # right side: align right edges
            new_x = placeholder_frame.origin.x + placeholder_frame.size.width - size.width

        if letter1 == "B":
            new_y = placeholder_frame.origin.y
        else:  # top side: align top edges
            new_y = placeholder_frame.origin.y + placeholder_frame.size.height - size.height

        self.setFrameOrigin_((new_x, new_y))
        NSUserDefaults.standardUserDefaults().setValue_forKey_(letter1 + letter2, "MyVideoCorner")

    def mouseDragged_(self, event):
        self.is_dragging = True
        self.currentLocation = event.locationInWindow()
        self.performDrag()


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
        self.title = self.sessionController.titleShort
        self.flipWnd = mbFlipWindow.alloc().init()
        self.flipWnd.setFlipRight_(True)
        self.flipWnd.setDuration_(2.4)
        
        self.notification_center = NotificationCenter()

        loadRecordingImages()

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
        self.myVideoView.setProducer(data.new_camera)

    @property
    def media_received(self):
        return self.streamController.media_received
    
    @objc.python_method
    def updateMuteButton(self):
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

    @objc.python_method
    def updateHoldButton(self):
        audio_stream = self.sessionController.streamHandlerOfType("audio")
        if audio_stream:
            if audio_stream.status == STREAM_CONNECTED:
                if audio_stream.holdByLocal:
                    self.holdButton.setToolTip_(NSLocalizedString("Unhold", "Label"))
                else:
                    self.holdButton.setToolTip_(NSLocalizedString("Hold", "Label"))

                if audio_stream.holdByLocal or audio_stream.holdByRemote:
                    self.holdButton.setImage_(NSImage.imageNamed_("paused-red"))
                else:
                    self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
            else:
                self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
        else:
            self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))

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

        self.hangupButton.setToolTip_(NSLocalizedString("Hangup", "Label"))
        self.chatButton.setToolTip_(NSLocalizedString("Chat", "Label"))
        self.infoButton.setToolTip_(NSLocalizedString("Show Session Information", "Label"))
        self.muteButton.setToolTip_(NSLocalizedString("Mute", "Label"))
        self.aspectButton.setToolTip_(NSLocalizedString("Aspect", "Label"))
        self.screenshotButton.setToolTip_(NSLocalizedString("Screenshot", "Label"))
        self.recordButton.setToolTip_(NSLocalizedString("Start Recording", "Label"))
        self.fullScreenButton.setToolTip_(NSLocalizedString("Full Screen", "Label"))

        self.disconnectLabel.superview().hide()
        self.recordButton.setEnabled_(False)

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
    def _setupStatsOverlay(self):
        """Create a small translucent overlay in the top-right of the
        video view showing codec, resolution, fps and RTT. The label is
        a sibling of videoView in the content view so it composes on top
        of the GPU drawable without being part of the Metal render pass.

        The overlay follows the same auto-hide rules as buttonsView:
        visible when the user is interacting with the window, hidden
        when the buttons fade out."""
        content = self.window().contentView()
        if content is None:
            return

        # Top-right corner, fixed pixel size. Pinned to the top-right
        # so it slides with the window's right edge during a resize.
        cb = content.bounds()
        overlay_w = 230.0
        overlay_h = 20.0
        margin = 12.0
        frame = NSMakeRect(
            cb.size.width - overlay_w - margin,
            cb.size.height - overlay_h - margin,
            overlay_w,
            overlay_h)
        overlay = NSTextField.alloc().initWithFrame_(frame)
        overlay.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
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
        overlay.setAlignment_(2)  # NSTextAlignmentRight
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

        parts = []
        if codec:
            parts.append(codec)
        if resolution:
            parts.append(resolution)
        if fps > 0:
            parts.append("%d fps" % fps)
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
        if self.streamController:
            return self.streamController.sessionController
        else:
            return None

    @objc.python_method
    def init_aspect_ratio(self, width, height):
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
        # NB: do NOT call self.window().setColorSpace_(sRGB) here.
        # Pinning the window to sRGB on an Apple Silicon Mac with a
        # Display P3 screen forces the whole window's compositing to
        # the smaller sRGB gamut, which visibly dims the Metal-rendered
        # video. Leaving the window at its default (screen) profile,
        # while the CAMetalLayer tags itself sRGB, lets CoreAnimation
        # do a normal sRGB->screen-gamut mapping which renders the
        # camera bytes at full brightness.
        self.sessionController.log_debug('Init %s in %s' % (self.window(), self))
        self.window().makeFirstResponder_(self.videoView)
        self.window().setAcceptsMouseMovedEvents_(True)
        self.window().setTitle_(title)
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
        self.initialLocation = event.locationInWindow()

    def mouseUp_(self, event):
        if self.closed:
            return
        if self.streamController.ended:
            return

        if self.myVideoView and self.myVideoView.is_dragging:
            self.myVideoView.goToFinalOrigin()

    def mouseDragged_(self, event):
        if self.closed:
            return
        if self.streamController.ended:
            return

        if self.myVideoView and self.myVideoView.is_dragging:
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
        if self.streamController:
            return self.streamController.sessionController
        else:
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
                    self.myVideoView.setHidden_(False)
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
        userdef = NSUserDefaults.standardUserDefaults()
        last_corner = userdef.stringForKey_("MyVideoCorner")
        if last_corner == "TL":
            self.moveMyVideoView(self.myVideoViewTL)
        elif last_corner == "TR":
            self.moveMyVideoView(self.myVideoViewTR)
        elif last_corner == "BR":
            self.moveMyVideoView(self.myVideoViewBR)
        elif last_corner == "BL":
            self.moveMyVideoView(self.myVideoViewBL)

    @objc.python_method
    def moveMyVideoView(self, view):
        if self.closed:
            return

        self.myVideoView.setFrame_(view.frame())
        self.myVideoView.setAutoresizingMask_(view.autoresizingMask())
        self.myVideoView.setFrameOrigin_(view.frame().origin)

    def windowWillResize_toSize_(self, window, frameSize):
        if self.closed:
            return frameSize

        if self.full_screen_in_progress or self.full_screen:
            return frameSize

        scaledSize = frameSize
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / self.aspect_ratio or 1.77
        if self.myVideoView:
            self.myVideoView.snapToCorner()
        
        if scaledSize.width < 665:
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

        origin = self.buttonsView.frame().origin
        origin.x = (NSWidth(self.buttonsView.superview().bounds()) - NSWidth(self.buttonsView.frame())) / 2
        self.buttonsView.setFrameOrigin_(origin)
        self.buttonsView.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin)

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
        self.sessionController.log_debug('windowDidEnterFullScreen_ %s' % self)
        if self.streamController.ended:
            self.window().orderOut_(self)
            return

        self.full_screen_in_progress = False
        self.full_screen = True
        self.stopMouseOutTimer()
        self.fullScreenButton.setImage_(NSImage.imageNamed_("restore"))

        self.repositionMyVideo()

        self.showButtons()

        if self.window():
            self.window().setLevel_(NSNormalWindowLevel)

    def windowDidExitFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidExitFullScreen %s' % self)
        self.fullScreenButton.setImage_(NSImage.imageNamed_("fullscreen"))

        self.full_screen_in_progress = False
        self.full_screen = False

        self.recordButton.setEnabled_(False)

        if self.streamController.videoRecorder:
            if self.streamController.videoRecorder.isRecording():
                self.streamController.videoRecorder.pause()

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
        self.sessionController.log_debug('windowWillClose %s' % self)
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
        self.sessionController.log_debug('windowShouldClose_ %s' % self)
        return True

    @objc.python_method
    @run_in_gui_thread
    def close(self):
        if self.closed:
            return

        self.sessionController.log_debug('Close remote %s' % self)
        self.closed = True
        self.notification_center.discard_observer(self, sender=self.streamController.videoRecorder)
        self.notification_center.discard_observer(self, name='BlinkMuteChangedState')
        self.notification_center.discard_observer(self, name='BlinkAudioStreamChangedHoldState')
        self.notification_center.discard_observer(self, name='VideoDeviceDidChangeCamera')
        self.notification_center = None

        # Release BOTH video views' producers before anything else can
        # raise. Any exception here would leave the AVCaptureSession (and
        # the camera LED) running until the next call. We release the PiP
        # thumbnail's local-camera consumer first, then the main view's
        # consumer (which may be the remote stream's producer when the
        # call was connected, or the local camera when we were still in
        # preview mode), each wrapped in its own try/except so a failure
        # in one path cannot prevent the other from running.
        if self.myVideoView:
            try:
                self.myVideoView.setProducer(None)
            except Exception as e:
                BlinkLogger().log_debug(
                    "myVideoView.setProducer(None) during cleanup ignored: %s" % e)
            try:
                self.myVideoView.close()
            except Exception as e:
                BlinkLogger().log_debug(
                    "myVideoView.close() during cleanup ignored: %s" % e)

        if self.videoView:
            try:
                self.videoView.setProducer(None)
            except Exception as e:
                BlinkLogger().log_debug(
                    "videoView.setProducer(None) during cleanup ignored: %s" % e)
            try:
                self.videoView.close()
            except Exception as e:
                BlinkLogger().log_debug(
                    "videoView.close() during cleanup ignored: %s" % e)

        if self.zrtp_controller:
            self.zrtp_controller.close()
            self.zrtp_controller = None

        self.hideButtons()
        self.stopRecordingTimer()
        self.stopStatsOverlayTimer()
        self.goToWindowMode()
        self.stopIdleTimer()
        self.stopMouseOutTimer()
        self.closeTrackingAreas()
        if self.localVideoWindow:
            self.localVideoWindow.close()

        if self.window():
            self.window().close()

    def dealloc(self):
        self.sessionController.log_debug("Dealloc %s" % self)
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
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

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
        if self.full_screen:
            self.toggleFullScreen()
        else:
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
        if self.sessionController:
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
        filename = self.screenshot_filename()
        screenshot_task = NSTask.alloc().init()
        screenshot_task.setLaunchPath_('/usr/sbin/screencapture')
        screenshot_task.setArguments_(['-tpng', filename])
        screenshot_task.launch()
        NSSound.soundNamed_("Grab").play()
        self.sessionController.log_info("Screenshot saved in %s" % filename)

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
        if status == 0 and self.sessionController and os.path.exists(self.screencapture_file):
            self.sendFiles([str(self.screencapture_file)])
        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, NSTaskDidTerminateNotification, self.screenshot_task)
        self.screenshot_task = None
        self.screencapture_file = None

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
        # abrupt hidden-flag flip. The recording indicator stays at
        # full opacity if we're currently recording, so the user always
        # has a visible "REC" cue.
        recording = bool(self.streamController
                         and self.streamController.videoRecorder
                         and self.streamController.videoRecorder.isRecording())

        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.25)
            self.buttonsView.animator().setAlphaValue_(0.0)
            if self.fullScreenButton is not None:
                self.fullScreenButton.animator().setAlphaValue_(0.0)
            if recording and self.recordButton is not None:
                self.recordButton.animator().setAlphaValue_(1.0)
        finally:
            NSAnimationContext.endGrouping()

        self.visible_buttons = False

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
        try:
            self.buttonsView.setHidden_(False)
            for btn in (self.fullScreenButton, self.holdButton,
                        self.hangupButton, self.chatButton,
                        self.infoButton, self.muteButton,
                        self.aspectButton, self.screenshotButton,
                        self.recordButton):
                if btn is not None:
                    btn.setHidden_(False)
        except Exception:
            pass

        NSAnimationContext.beginGrouping()
        try:
            NSAnimationContext.currentContext().setDuration_(0.18)
            self.buttonsView.animator().setAlphaValue_(1.0)
            if self.fullScreenButton is not None:
                self.fullScreenButton.animator().setAlphaValue_(1.0)
        finally:
            NSAnimationContext.endGrouping()

        self.visible_buttons = True

    def updateRecordingTimer_(self, timer):
        self.recordButton.setEnabled_(self.full_screen)
        if not self.streamController.videoRecorder:
            return

        if self.streamController.videoRecorder.isRecording():
            self.recordButton.setToolTip_(NSLocalizedString("Stop Recording", "Label"))
            self.recordingImage += 1
            if self.recordingImage >= len(RecordingImages):
                self.recordingImage = 0
            self.recordButton.setImage_(RecordingImages[self.recordingImage])
        else:
            self.recordButton.setToolTip_(NSLocalizedString("Start Recording", "Label"))
            self.recordButton.setImage_(RecordingImages[0])

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
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(dirtyRect, 7.0, 7.0)
        path.addClip()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0, 0, 0, 0.3).setFill()
        NSRectFillUsingOperation(dirtyRect, NSCompositeSourceOver)
        objc.super(RoundedCornersView, self).drawRect_(dirtyRect)


class BlackView(NSView):
    def drawRect_(self, dirtyRect):
        NSColor.blackColor().set()
        NSRectFill(dirtyRect)
