# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""RTSP cameras (CCTV) as a video source, through the ffmpeg that is bundled for the SDK.

libavformat opens the stream (RTSP over TCP, user and password in the URL),
libavcodec decodes its video (H264, HEVC, MJPEG, ...), libswscale converts the
picture to BGRA, which goes into an RTCVideoSource as CVPixelBuffers, at most
FPS a second: the source then adapts it to what the room is sent.

  rtsp:// ─► avformat (demux, video stream only) ─► avcodec ─► swscale (BGRA) ─► CVPixelBuffer ─► RTCVideoSource

The libraries are called through ctypes. The few struct fields read are those of
ffmpeg 4.4 (libavformat 58, libavcodec 58, libavutil 56: build_scripts/ffmpeg),
taken from its headers (offsetof); _check_versions() refuses other majors rather
than read the wrong fields.

The reading happens in a thread of its own; it ends when stopped (the interrupt
callback aborts a blocking read), at the end of the stream or on an error.
Callbacks (on_started, on_failed, on_ended) are called in the GUI thread.
"""

__all__ = ['RtspReader', 'rtsp_available', 'rtsp_missing', 'rtsp_url']

import ctypes
import os
import threading
import time

from urllib.parse import quote, urlsplit, urlunsplit

import objc

from Foundation import NSBundle
from Quartz import (CVPixelBufferGetBaseAddress,
                    CVPixelBufferGetBytesPerRow,
                    CVPixelBufferGetDataSize,
                    CVPixelBufferLockBaseAddress,
                    CVPixelBufferPoolCreate,
                    CVPixelBufferPoolCreatePixelBuffer,
                    CVPixelBufferUnlockBaseAddress,
                    kCVPixelBufferHeightKey,
                    kCVPixelBufferIOSurfacePropertiesKey,
                    kCVPixelBufferPixelFormatTypeKey,
                    kCVPixelBufferWidthKey,
                    kCVPixelFormatType_32BGRA)

from BlinkLogger import BlinkLogger
from WebRTCSupport import webrtc_classes
from util import call_in_gui_thread


LIBRARIES = (('avutil', 'libavutil.56.dylib'), ('swscale', 'libswscale.5.dylib'), ('avcodec', 'libavcodec.58.dylib'), ('avformat', 'libavformat.58.dylib'))
MAJORS = dict(avutil=56, avcodec=58, avformat=58, swscale=5)
MAX_WIDTH, MAX_HEIGHT = 1280, 720       # what is handed on at most: the largest resolution an extra video is sent at
OPEN_TIMEOUT = 10                       # seconds to open the stream and find its video
READ_TIMEOUT = 10                       # seconds without data before the stream counts as lost

# ffmpeg 4.4, from its headers (offsetof, LP64)
FORMAT_STREAMS = 48                     # AVFormatContext.streams (AVStream **)
FORMAT_INTERRUPT_CALLBACK = 1224        # AVFormatContext.interrupt_callback (AVIOInterruptCB: callback, opaque)
STREAM_CODECPAR = 208                   # AVStream.codecpar
CODECPAR_CODEC_ID = 4                   # AVCodecParameters.codec_id
CODEC_ID = 20                           # AVCodec.id
PACKET_STREAM_INDEX = 36                # AVPacket.stream_index
FRAME_LINESIZE = 64                     # AVFrame.data at 0, linesize[8] here
FRAME_WIDTH, FRAME_HEIGHT, FRAME_FORMAT = 104, 108, 116

AVMEDIA_TYPE_VIDEO = 0
AV_PIX_FMT_BGRA = 28
AVERROR_EAGAIN = -35                    # AVERROR(EAGAIN): EAGAIN is 35 on macOS
AVERROR_EOF = -541478725                # FFERRTAG('E','O','F',' ')
SWS_BILINEAR = 2


_libs = None
_missing = None


def _libraries_path():
    return os.path.join(NSBundle.mainBundle().privateFrameworksPath() or '', 'libs')


def _int(lib, name, *argtypes, restype=ctypes.c_int):
    function = getattr(lib, name)
    function.argtypes = list(argtypes)
    function.restype = restype
    return function


def _load():
    """The four libraries with the functions used declared, or the reason they cannot be."""
    global _libs
    if _libs is not None:
        return None
    path = _libraries_path()
    libs = {}
    for name, filename in LIBRARIES:
        full = os.path.join(path, filename)
        if not os.path.exists(full):
            return '%s is not in the application' % filename
        try:
            libs[name] = ctypes.CDLL(full, mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            return 'cannot load %s: %s' % (filename, e)
    p, i, c = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    pp = ctypes.POINTER(ctypes.c_void_p)
    util, swscale, codec, format = libs['avutil'], libs['swscale'], libs['avcodec'], libs['avformat']
    for lib, name in ((util, 'avutil_version'), (codec, 'avcodec_version'), (format, 'avformat_version'), (swscale, 'swscale_version')):
        _int(lib, name, restype=ctypes.c_uint)
    _int(util, 'av_strerror', i, ctypes.c_char_p, ctypes.c_size_t)
    _int(util, 'av_dict_set', pp, c, c, i)
    _int(util, 'av_dict_free', pp, restype=None)
    _int(util, 'av_frame_alloc', restype=p)
    _int(util, 'av_frame_free', pp, restype=None)
    _int(util, 'av_frame_unref', p, restype=None)
    _int(format, 'avformat_network_init')
    _int(format, 'avformat_alloc_context', restype=p)
    _int(format, 'avformat_open_input', pp, c, p, pp)
    _int(format, 'avformat_find_stream_info', p, pp)
    _int(format, 'av_find_best_stream', p, i, i, i, pp, i)
    _int(format, 'av_read_frame', p, p)
    _int(format, 'avformat_close_input', pp, restype=None)
    _int(codec, 'avcodec_alloc_context3', p, restype=p)
    _int(codec, 'avcodec_parameters_to_context', p, p)
    _int(codec, 'avcodec_open2', p, p, pp)
    _int(codec, 'avcodec_send_packet', p, p)
    _int(codec, 'avcodec_receive_frame', p, p)
    _int(codec, 'avcodec_free_context', pp, restype=None)
    _int(codec, 'av_packet_alloc', restype=p)
    _int(codec, 'av_packet_unref', p, restype=None)
    _int(codec, 'av_packet_free', pp, restype=None)
    _int(swscale, 'sws_getCachedContext', p, i, i, i, i, i, i, i, p, p, p, restype=p)
    _int(swscale, 'sws_scale', p, p, p, i, i, p, p)
    _int(swscale, 'sws_freeContext', p, restype=None)
    libs['network'] = format.avformat_network_init()
    _libs = libs
    return None


def _check_versions():
    for name, function in (('avutil', 'avutil_version'), ('avcodec', 'avcodec_version'), ('avformat', 'avformat_version'), ('swscale', 'swscale_version')):
        major = getattr(_libs[name], function)() >> 16
        if major != MAJORS[name]:
            return 'lib%s %d, this code reads the structures of %d (ffmpeg 4.4)' % (name, major, MAJORS[name])
    return None


def rtsp_missing():
    """Why RTSP cameras cannot be used here, or None. Loads the libraries (checked once a run)."""
    global _missing
    if _missing is None:
        _missing = _load() or _check_versions() or ''
        if _missing:
            BlinkLogger().log_warning('[videoroom] RTSP cameras not available: %s' % _missing)
    return _missing or None


def rtsp_available():
    return rtsp_missing() is None


def rtsp_url(url, user=None, password=None):
    """The address with the user and password in it (rtsp://user:password@host/...), as libavformat takes them."""
    if not user:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ''
    if ':' in host:
        host = '[%s]' % host
    netloc = '%s:%s@%s' % (quote(user, safe=''), quote(password or '', safe=''), host) + (':%d' % parts.port if parts.port else '')
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _error(code):
    buffer = ctypes.create_string_buffer(256)
    if _libs['avutil'].av_strerror(code, buffer, len(buffer)) < 0:
        return 'error %d' % code
    return buffer.value.decode('utf-8', 'replace')


def _ptr(address, offset):
    """The pointer stored at address + offset."""
    return ctypes.c_void_p.from_address(address + offset).value


def _int_at(address, offset):
    return ctypes.c_int.from_address(address + offset).value


_InterruptCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


class RtspReader(object):
    """Reads one RTSP stream into an RTCVideoSource (source) until stopped.

    on_started(width, height, codec name): the first picture came; on_failed(reason): it could not be
    opened or it broke; on_ended(): stopped (by us) or the stream ended. All in the GUI thread.
    """

    def __init__(self, url, user=None, password=None, source=None, fps=10, on_started=None, on_failed=None, on_ended=None, on_frame=None):
        self.url = url
        self.user = user
        self.password = password
        self.source = source
        self.fps = fps
        self.on_started = on_started
        self.on_failed = on_failed
        self.on_ended = on_ended
        self.on_frame = on_frame        # on_frame(pixel buffer), in the reading thread: a test's picture
        self.size = None                # (width, height) of the stream
        self.codec = None
        self._stop = False
        self._deadline = None           # the interrupt callback aborts what blocks past it
        self._thread = None
        self._capturer = None
        self._pool = None
        self._pool_size = None
        self._interrupt = _InterruptCallback(self._interrupted)    # kept: ffmpeg holds the pointer

    def describe(self):
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.hostname + (':%d' % parts.port if parts.port else '') if parts.hostname else parts.netloc, parts.path, parts.query, ''))

    def start(self):
        reason = rtsp_missing()
        if reason is not None:
            raise RuntimeError(reason)
        if self.source is not None:
            self._capturer = webrtc_classes()['RTCVideoCapturer'].alloc().initWithDelegate_(self.source)
        self._thread = threading.Thread(target=self._run, name='RTSP %s' % self.describe(), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    # The reading thread

    def _interrupted(self, opaque):
        return 1 if self._stop or (self._deadline is not None and time.monotonic() > self._deadline) else 0

    def _run(self):
        format_context = ctypes.c_void_p(None)
        codec_context = ctypes.c_void_p(None)
        packet = ctypes.c_void_p(None)
        frame = ctypes.c_void_p(None)
        scaler = None
        started = False
        failure = None
        lib = _libs
        try:
            format_context.value = lib['avformat'].avformat_alloc_context()
            if not format_context.value:
                raise RuntimeError('cannot allocate the stream')
            callback = ctypes.cast(self._interrupt, ctypes.c_void_p).value
            ctypes.c_void_p.from_address(format_context.value + FORMAT_INTERRUPT_CALLBACK).value = callback
            ctypes.c_void_p.from_address(format_context.value + FORMAT_INTERRUPT_CALLBACK + 8).value = None
            options = ctypes.c_void_p(None)
            for key, value in (('rtsp_transport', 'tcp'), ('stimeout', str(READ_TIMEOUT * 1000000)), ('fflags', 'nobuffer'),
                               ('max_delay', '500000'), ('allowed_media_types', 'video')):
                lib['avutil'].av_dict_set(ctypes.byref(options), key.encode(), value.encode(), 0)
            self._deadline = time.monotonic() + OPEN_TIMEOUT
            # avformat_open_input frees the context on failure and sets the pointer to NULL
            result = lib['avformat'].avformat_open_input(ctypes.byref(format_context), rtsp_url(self.url, self.user, self.password).encode(), None, ctypes.byref(options))
            lib['avutil'].av_dict_free(ctypes.byref(options))
            if result < 0:
                raise RuntimeError(self._reason(result, 'cannot open the stream'))
            result = lib['avformat'].avformat_find_stream_info(format_context, None)
            if result < 0:
                raise RuntimeError(self._reason(result, 'cannot read the stream'))
            decoder = ctypes.c_void_p(None)
            index = lib['avformat'].av_find_best_stream(format_context, AVMEDIA_TYPE_VIDEO, -1, -1, ctypes.byref(decoder), 0)
            if index < 0 or not decoder.value:
                raise RuntimeError('the stream has no video that can be decoded here' if index < 0 else 'no decoder for its video')
            stream = _ptr(_ptr(format_context.value, FORMAT_STREAMS), index * 8)
            parameters = _ptr(stream, STREAM_CODECPAR)
            if not parameters or _int_at(parameters, CODECPAR_CODEC_ID) != _int_at(decoder.value, CODEC_ID):
                raise RuntimeError('unexpected ffmpeg structures (not 4.4?)')
            self.codec = ctypes.c_char_p(_ptr(decoder.value, 0)).value.decode('ascii', 'replace')
            codec_context.value = lib['avcodec'].avcodec_alloc_context3(decoder)
            if not codec_context.value or lib['avcodec'].avcodec_parameters_to_context(codec_context, parameters) < 0:
                raise RuntimeError('cannot set up the decoder')
            result = lib['avcodec'].avcodec_open2(codec_context, decoder, None)
            if result < 0:
                raise RuntimeError(self._reason(result, 'cannot open the decoder'))
            packet.value = lib['avcodec'].av_packet_alloc()
            frame.value = lib['avutil'].av_frame_alloc()
            interval = 1.0 / max(1, self.fps)
            next_frame = 0.0
            while not self._stop:
                self._deadline = time.monotonic() + READ_TIMEOUT
                result = lib['avformat'].av_read_frame(format_context, packet)
                if result == AVERROR_EOF:
                    break
                if result < 0:
                    raise RuntimeError(self._reason(result, 'the stream broke'))
                try:
                    if _int_at(packet.value, PACKET_STREAM_INDEX) != index:
                        continue
                    if lib['avcodec'].avcodec_send_packet(codec_context, packet) < 0:
                        continue        # a broken packet: the next keyframe recovers
                finally:
                    lib['avcodec'].av_packet_unref(packet)
                while not self._stop:
                    result = lib['avcodec'].avcodec_receive_frame(codec_context, frame)
                    if result in (AVERROR_EAGAIN, AVERROR_EOF):
                        break
                    if result < 0:
                        break
                    try:
                        now = time.monotonic()
                        if now < next_frame:
                            continue        # FPS at most: the others are dropped decoded
                        next_frame = max(now, next_frame + interval)
                        scaler = self._deliver(frame.value, scaler)
                        if not started and scaler is not None:
                            started = True
                            call_in_gui_thread(self._started)
                    finally:
                        lib['avutil'].av_frame_unref(frame)
        except Exception as e:
            failure = str(e)
        finally:
            if scaler is not None:
                lib['swscale'].sws_freeContext(scaler)
            if frame.value:
                lib['avutil'].av_frame_free(ctypes.byref(frame))
            if packet.value:
                lib['avcodec'].av_packet_free(ctypes.byref(packet))
            if codec_context.value:
                lib['avcodec'].avcodec_free_context(ctypes.byref(codec_context))
            if format_context.value:
                lib['avformat'].avformat_close_input(ctypes.byref(format_context))
        if self._stop:
            call_in_gui_thread(self._ended)
        elif failure is not None:
            call_in_gui_thread(self._failed, failure)
        else:
            call_in_gui_thread(self._failed, 'the stream ended')

    def _reason(self, code, text):
        if self._stop:
            return 'stopped'
        if self._deadline is not None and time.monotonic() > self._deadline:
            return '%s: no answer within %d s' % (text, OPEN_TIMEOUT)
        reason = _error(code)
        if '401' in reason or 'nauthori' in reason:
            reason += ' (check the user and password)'
        elif '404' in reason or 'not found' in reason.lower():
            reason += ' (check the address)'
        return '%s: %s' % (text, reason)

    def _pixel_buffer(self, width, height):
        if self._pool is None or self._pool_size != (width, height):
            attributes = {kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
                          kCVPixelBufferWidthKey: width,
                          kCVPixelBufferHeightKey: height,
                          kCVPixelBufferIOSurfacePropertiesKey: {}}
            result, pool = CVPixelBufferPoolCreate(None, None, attributes, None)
            if result != 0 or pool is None:
                return None
            self._pool, self._pool_size = pool, (width, height)
        result, buffer = CVPixelBufferPoolCreatePixelBuffer(None, self._pool, None)
        return buffer if result == 0 else None

    def _deliver(self, frame, scaler):
        """Convert the decoded picture to BGRA (scaled down to MAX_WIDTH x MAX_HEIGHT at most, the aspect kept) and
        hand it to the source; the scaler (kept between frames: sws_getCachedContext)."""
        lib = _libs
        width, height, pixel_format = _int_at(frame, FRAME_WIDTH), _int_at(frame, FRAME_HEIGHT), _int_at(frame, FRAME_FORMAT)
        if width <= 0 or height <= 0 or pixel_format < 0:
            return scaler
        self.size = (width, height)
        scale = min(1.0, MAX_WIDTH / width, MAX_HEIGHT / height)
        out_w, out_h = max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1)
        scaler = lib['swscale'].sws_getCachedContext(scaler, width, height, pixel_format, out_w, out_h, AV_PIX_FMT_BGRA, SWS_BILINEAR, None, None, None)
        if not scaler:
            return None
        with objc.autorelease_pool():
            buffer = self._pixel_buffer(out_w, out_h)
            if buffer is None or CVPixelBufferLockBaseAddress(buffer, 0) != 0:
                return scaler
            try:
                base = CVPixelBufferGetBaseAddress(buffer)
                if base is None:
                    return scaler
                size = CVPixelBufferGetDataSize(buffer)
                try:
                    view = base.as_buffer(size)
                except AttributeError:
                    view = memoryview(base)
                try:
                    target, copy = (ctypes.c_char * size).from_buffer(view), False
                except TypeError:
                    target, copy = ctypes.create_string_buffer(size), True     # read-only from PyObjC: converted aside, then copied in
                destination = (ctypes.c_void_p * 4)(ctypes.addressof(target), None, None, None)
                strides = (ctypes.c_int * 4)(CVPixelBufferGetBytesPerRow(buffer), 0, 0, 0)
                lib['swscale'].sws_scale(scaler, ctypes.c_void_p(frame), ctypes.c_void_p(frame + FRAME_LINESIZE), 0, height, destination, strides)
                if copy:
                    view[:size] = target.raw
                del target
            finally:
                CVPixelBufferUnlockBaseAddress(buffer, 0)
            if self.on_frame is not None:
                self.on_frame(buffer)
            if self.source is not None and self._capturer is not None:
                classes = webrtc_classes()
                video_frame = classes['RTCVideoFrame'].alloc().initWithBuffer_rotation_timeStampNs_(
                    classes['RTCCVPixelBuffer'].alloc().initWithPixelBuffer_(buffer), 0, time.monotonic_ns())
                self.source.capturer_didCaptureVideoFrame_(self._capturer, video_frame)
        return scaler

    # The GUI thread's

    def _started(self):
        if not self._stop and self.on_started is not None:
            self.on_started(self.size[0] if self.size else 0, self.size[1] if self.size else 0, self.codec or '')

    def _failed(self, reason):
        if self.on_failed is not None:
            self.on_failed(reason)

    def _ended(self):
        if self.on_ended is not None:
            self.on_ended()
