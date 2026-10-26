# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Extra videos of ours in a video room: a second camera, or an RTSP camera (CCTV).

Each is a publisher of its own in the room, video only, joined over the room's
sylkRTC connection with parent (our own publisher's session id) and label
("Camera 2"): the gateway (feature multiple-video-feeds) names it "<our name>
<label>", shows it under us rather than as another participant and gives it no
chat leg; it ends it with us. At most 4 per participant (the server's limit).

One sendonly RTCPeerConnection each, with an RTCVideoSource of its own:

  camera (RTCCameraVideoCapturer, AVFoundation) ─┬─► RTCVideoSource (adapted to 640x360, 10 fps) ─► track ─► sender
  RTSP camera (RtspSource: libavformat, ffmpeg) ─┘

An RTSP camera that worked is remembered with its name, address, user, password
and resolution (user defaults VideoroomVideoSources, newest first, 20) and offered
again.

The same track is its tile here (its Participant's video_track), so the window
shows it like any other video. A camera of this computer must not be the SDK's
(the one our own publisher sends): local_cameras() leaves it out.

Resolutions are 16:9 at 10 fps; changing one while sending adapts the source and
the sender's bitrate, without renegotiating.

The session posts, with itself as sender,

  VideoroomSessionExtraVideoDidChangeState   extra, state (connecting, established, ended), reason
"""

__all__ = ['ExtraVideo', 'RESOLUTIONS', 'DEFAULT_RESOLUTION', 'MAX_EXTRA_VIDEOS', 'FPS', 'local_cameras', 'extra_videos_available',
           'saved_sources', 'remember_source', 'rename_source', 'set_source_resolution', 'forget_source']

import json
import uuid

import objc

from Foundation import NSLocalizedString, NSUserDefaults

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from WebRTCSupport import webrtc_classes
from util import call_in_gui_thread, run_in_gui_thread


FPS = 10
# what an extra video is sent at: (name, width, height, kbit/s), 16:9; the source is scaled to it
RESOLUTIONS = [('1280x720', 1280, 720, 1200), ('640x360', 640, 360, 500), ('320x180', 320, 180, 200)]
DEFAULT_RESOLUTION = '640x360'
MAX_EXTRA_VIDEOS = 4            # per participant, the gateway's limit
JOIN_TIMEOUT = 25               # seconds from the offer to established
CAPTURE_WIDTH, CAPTURE_HEIGHT = 1280, 720       # the camera is captured at least this large, so any resolution can be chosen later

SOURCES_KEY = 'VideoroomVideoSources'      # user defaults: the RTSP cameras that worked, JSON
MAX_SOURCES = 20

DIRECTION_SEND_ONLY = 1         # RTCRtpTransceiverDirection
SDP_ANSWER = 2                  # RTCSdpType


def _resolution(name):
    return next((resolution for resolution in RESOLUTIONS if resolution[0] == name), None) or \
           next(resolution for resolution in RESOLUTIONS if resolution[0] == DEFAULT_RESOLUTION)


def _cls(name):
    return webrtc_classes().get(name)


# Remembered RTSP cameras

def saved_sources():
    """The RTSP cameras that worked, newest first: [{label, url, user, password, resolution}]."""
    try:
        sources = json.loads(NSUserDefaults.standardUserDefaults().stringForKey_(SOURCES_KEY) or '[]')
    except (TypeError, ValueError):
        sources = []
    return [source for source in sources if isinstance(source, dict) and source.get('url')]


def _save_sources(sources):
    NSUserDefaults.standardUserDefaults().setObject_forKey_(json.dumps(sources[:MAX_SOURCES]), SOURCES_KEY)


def remember_source(source):
    previous = next((other for other in saved_sources() if other.get('url') == source.get('url')), {})
    sources = [other for other in saved_sources() if other.get('url') != source.get('url')]
    sources.insert(0, dict(label=source.get('label') or '', url=source['url'], user=source.get('user') or '', password=source.get('password') or '',
                           resolution=source.get('resolution') or previous.get('resolution') or DEFAULT_RESOLUTION))
    _save_sources(sources)


def rename_source(url, label):
    sources = saved_sources()
    for source in sources:
        if source.get('url') == url:
            source['label'] = label
    _save_sources(sources)


def set_source_resolution(url, resolution):
    sources = saved_sources()
    for source in sources:
        if source.get('url') == url:
            source['resolution'] = resolution
    _save_sources(sources)


def forget_source(url):
    _save_sources([source for source in saved_sources() if source.get('url') != url])


def _error_text(error):
    return str(error.localizedDescription() or error) if error is not None else None


_metadata_registered = False


def _register_metadata():
    global _metadata_registered
    if _metadata_registered:
        return
    _metadata_registered = True
    error_only = {'callable': {'retval': {'type': b'v'}, 'arguments': {0: {'type': b'^v'}, 1: {'type': b'@'}}}}
    nothing = {'callable': {'retval': {'type': b'v'}, 'arguments': {0: {'type': b'^v'}}}}
    # startCaptureWithDevice:format:fps:completionHandler: (self, _cmd, device, format, fps, block)
    objc.registerMetaDataForSelector(b'RTCCameraVideoCapturer', b'startCaptureWithDevice:format:fps:completionHandler:', {'arguments': {5: dict(error_only, type=b'@?')}})
    objc.registerMetaDataForSelector(b'RTCCameraVideoCapturer', b'stopCaptureWithCompletionHandler:', {'arguments': {2: dict(nothing, type=b'@?')}})


def extra_videos_available():
    """Whether extra videos can be sent from here (WebRTC.framework has the camera capturer)."""
    return _cls('RTCCameraVideoCapturer') is not None


def _sdk_camera_name():
    """The name of the camera the SDK uses (our own publisher's), or None when it is the screen or none."""
    try:
        from sipsimple.application import SIPApplication
        from ScreenPointer import is_screen_device
        name = SIPApplication.video_device.real_name
        return None if not name or is_screen_device(name) else str(name)
    except Exception:
        return None


def local_cameras():
    """[(name, unique id)] of the cameras of this computer that can be an extra video: all but the SDK's."""
    capturer = _cls('RTCCameraVideoCapturer')
    if capturer is None:
        return []
    sdk = _sdk_camera_name()
    cameras = []
    for device in capturer.captureDevices() or []:
        name = str(device.localizedName())
        if sdk is not None and name == sdk:
            continue
        cameras.append((name, str(device.uniqueID())))
    return cameras


def _capture_device(unique_id):
    capturer = _cls('RTCCameraVideoCapturer')
    if capturer is None:
        return None
    return next((device for device in capturer.captureDevices() or [] if str(device.uniqueID()) == unique_id), None)


def _capture_format(device):
    """The smallest format of the camera at least CAPTURE_WIDTH x CAPTURE_HEIGHT (else the largest), and its fps."""
    from AVFoundation import CMVideoFormatDescriptionGetDimensions
    best, best_size, largest, largest_size = None, None, None, None
    for format in _cls('RTCCameraVideoCapturer').supportedFormatsForDevice_(device) or []:
        dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription())
        size = (int(dimensions.width), int(dimensions.height))
        if largest_size is None or size[0] * size[1] > largest_size[0] * largest_size[1]:
            largest, largest_size = format, size
        if size[0] >= CAPTURE_WIDTH and size[1] >= CAPTURE_HEIGHT and (best_size is None or size[0] * size[1] < best_size[0] * best_size[1]):
            best, best_size = format, size
    format, size = (best, best_size) if best is not None else (largest, largest_size)
    if format is None:
        return None, None, 0
    rates = [float(rate.maxFrameRate()) for rate in format.videoSupportedFrameRateRanges() or []]
    fps = min(FPS, int(max(rates))) if rates else FPS
    return format, size, max(1, fps)


@implementer(IObserver)
class ExtraVideo(object):
    """One extra video of ours: source is dict(kind='camera', name, unique_id) or dict(kind='rtsp', url, user, password)."""

    def __init__(self, session, source, label):
        from Videoroom import Participant
        self.session = session
        self.source = dict(source)
        self.label = label
        self.id = str(uuid.uuid4())
        self.state = 'idle'         # idle, connecting, established, ended
        self.reason = ''
        self.resolution = _resolution(source.get('resolution') or DEFAULT_RESOLUTION)[0]
        self.peer_connection = None
        self._delegate = None
        self._video_source = None
        self._capturer = None
        self._transceiver = None
        self._join_sent = False
        self._join_timer = None
        self._reader = None         # an RTSP camera's RtspReader
        self.capture_size = None    # what the camera gives
        # its tile here: a participant of the window, its picture our own track
        self.participant = Participant(dict(id=self.id, uri=session.identity, display_name=label, type='webrtc', parent=session.id, label=label))
        self.participant.session = session
        self.participant.extra_video = self
        self.participant.video = True
        self.participant.state = 'connecting'

    def __repr__(self):
        return '<ExtraVideo %s %s %s>' % (self.label, self.describe(), self.state)

    def describe(self):
        if self.source.get('kind') == 'rtsp':
            from RtspSource import RtspReader
            return RtspReader(self.source['url']).describe()      # without user and password
        return self.source.get('name') or 'camera'

    def log(self, text, level='info'):
        getattr(BlinkLogger(), 'log_%s' % level)('[videoroom] %s: %s: %s' % (self.session.room, self.label, text))

    @property
    def size(self):
        name, width, height, bitrate = _resolution(self.resolution)
        return width, height

    @property
    def bitrate(self):
        return _resolution(self.resolution)[3]

    # Start and stop

    def start(self):
        from Videoroom import VideoroomPeerConnectionDelegate, peer_connection_factory, _timer
        session = self.session
        if session.connection is None or session.state != 'established':
            raise RuntimeError(NSLocalizedString("the room is not connected yet", "Label"))
        kind = self.source.get('kind')
        if kind == 'rtsp':
            from RtspSource import rtsp_missing
            reason = rtsp_missing()
            if reason is not None:
                raise RuntimeError(reason)
            device = format = None
        elif kind == 'camera':
            if not extra_videos_available():
                raise RuntimeError(NSLocalizedString("this WebRTC.framework cannot capture cameras", "Label"))
            device = _capture_device(self.source.get('unique_id'))
            if device is None:
                raise RuntimeError(NSLocalizedString("the camera is not connected", "Label"))
            format, self.capture_size, fps = _capture_format(device)
            if format is None:
                raise RuntimeError(NSLocalizedString("the camera has no usable format", "Label"))
            _register_metadata()
        else:
            raise RuntimeError('%s sources are not supported' % kind)
        factory = peer_connection_factory()
        self._delegate = VideoroomPeerConnectionDelegate.alloc().initWithSession_label_(session, self.id)
        self.peer_connection = factory.peerConnectionWithConfiguration_constraints_delegate_(session._configuration(), session._constraints(), self._delegate)
        if self.peer_connection is None:
            raise RuntimeError('cannot make a PeerConnection')
        self._video_source = factory.videoSource()
        name, width, height, bitrate = _resolution(self.resolution)
        self._video_source.adaptOutputFormatToWidth_height_fps_(width, height, FPS)
        track = factory.videoTrackWithSource_trackId_(self._video_source, 'video-%s' % self.id)
        init = _cls('RTCRtpTransceiverInit').alloc().init()
        init.setDirection_(DIRECTION_SEND_ONLY)
        init.setStreamIds_(['blink-%s' % self.id])
        self._transceiver = self.peer_connection.addTransceiverWithTrack_init_(track, init)
        self.participant.video_track = track

        self.state = 'connecting'
        session.extra_videos[self.id] = self
        NotificationCenter().add_observer(self, sender=session.connection, name='SylkRTCConnectionGotEvent')

        if kind == 'rtsp':
            from RtspSource import RtspReader
            self._reader = RtspReader(self.source['url'], self.source.get('user'), self.source.get('password'), source=self._video_source, fps=FPS,
                                      on_started=self._rtsp_started, on_failed=lambda reason: self.stop(reason))
            self._reader.start()
        else:
            self._capturer = _cls('RTCCameraVideoCapturer').alloc().initWithDelegate_(self._video_source)

            def started(error):
                if error is not None:
                    call_in_gui_thread(self.stop, 'the camera cannot be opened: %s' % _error_text(error))

            self._capturer.startCaptureWithDevice_format_fps_completionHandler_(device, format, fps, started)

        def offered(description, error):
            call_in_gui_thread(self._offer_created, description, _error_text(error))

        self.peer_connection.offerForConstraints_completionHandler_(session._constraints(), offered)
        self._join_timer = _timer(JOIN_TIMEOUT, lambda: self.stop('no media connection'))
        captured = ', captured at %dx%d' % self.capture_size if self.capture_size else ''
        self.log('adding %s (sent at %dx%d %d fps, %d kbit/s%s)' % (self.describe(), width, height, FPS, bitrate, captured))
        self._post_state('connecting')

    def stop(self, reason='stopped'):
        if self.state in ('idle', 'ended'):
            return
        connection = self.session.connection
        if connection is not None and connection.state == 'ready' and self._join_sent and reason != 'left':
            connection.request(dict(sylkrtc='videoroom-leave', session=self.id))
        self._end(reason)

    def _end(self, reason):
        if self.state == 'ended':
            return
        self.state = 'ended'
        self.reason = reason
        if self._join_timer is not None:
            self._join_timer.invalidate()
            self._join_timer = None
        session = self.session
        if session.connection is not None:
            NotificationCenter().discard_observer(self, sender=session.connection, name='SylkRTCConnectionGotEvent')
        session.extra_videos.pop(self.id, None)
        session._trickle_ready.discard(self.id)
        session._trickle_pending.pop(self.id, None)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.stop()
        capturer, self._capturer = self._capturer, None
        if capturer is not None:
            try:
                capturer.stopCaptureWithCompletionHandler_(lambda: None)
            except Exception:
                pass
        if self._delegate is not None:
            self._delegate.session = None
        if self.peer_connection is not None:
            self.peer_connection.close()
        self.peer_connection = self._delegate = self._transceiver = self._video_source = None
        self.participant.video_track = None
        self.participant.state = 'ended' if reason in ('stopped', 'left') else 'failed'
        self.log('ended: %s' % reason)
        self._post_state('ended', reason)

    def _post_state(self, state, reason=''):
        NotificationCenter().post_notification('VideoroomSessionExtraVideoDidChangeState', sender=self.session,
                                               data=NotificationData(extra=self, state=state, reason=reason))

    # Signalling

    def _offer_created(self, description, error):
        if self.state != 'connecting' or self.peer_connection is None:
            return
        if description is None:
            self.stop('cannot create the offer: %s' % error)
            return
        sdp = str(description.sdp())

        def local_set(error):
            if error is not None:
                call_in_gui_thread(self.stop, 'cannot use our offer: %s' % _error_text(error))

        self.peer_connection.setLocalDescription_completionHandler_(description, local_set)
        session = self.session
        session.connection.request(dict(sylkrtc='videoroom-join', account=session.identity, session=self.id, uri=session.room,
                                        sdp=sdp, audio=False, video=True, parent=session.id, label=self.label), self._join_answered)
        self._join_sent = True
        session._flush_trickle(self.id)

    def _join_answered(self, error):
        if error is not None:
            self._join_sent = False     # nothing to leave
            self._end('the server refused it: %s' % error)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SylkRTCConnectionGotEvent(self, notification):
        message = notification.data.event
        if message.get('sylkrtc') != 'videoroom-event' or message.get('session') != self.id or message.get('event') != 'session-state':
            return      # the room's publishers and the rest: our own session has them
        state = message.get('state')
        if state == 'accepted':
            answer = _cls('RTCSessionDescription').alloc().initWithType_sdp_(SDP_ANSWER, message.get('sdp') or '')

            def remote_set(error):
                call_in_gui_thread(self._answer_set, _error_text(error))

            self.peer_connection.setRemoteDescription_completionHandler_(answer, remote_set)
        elif state == 'established':
            if self._join_timer is not None:
                self._join_timer.invalidate()
                self._join_timer = None
            self.state = 'established'
            self.participant.state = 'established'
            self.log('sending')
            if self.source.get('kind') == 'rtsp':
                remember_source(dict(label=self.label, url=self.source['url'], user=self.source.get('user'), password=self.source.get('password'), resolution=self.resolution))
            self._post_changed()
            self._post_state('established')
        elif state == 'terminated':
            self._join_sent = False     # nothing to leave
            self._end(message.get('reason') or 'ended by the server')

    def _rtsp_started(self, width, height, codec):
        if self.state == 'ended':
            return
        self.capture_size = (width, height)
        self.log('RTSP %s %dx%d' % (codec.upper(), width, height))

    def _answer_set(self, error):
        if self.peer_connection is None:
            return
        if error is not None:
            self.stop('cannot use the answer: %s' % error)
            return
        self._limit_bitrate()

    def _limit_bitrate(self):
        if self._transceiver is None:
            return
        try:
            sender = self._transceiver.sender()
            parameters = sender.parameters()
            for encoding in parameters.encodings():
                encoding.setMaxBitrateBps_(self.bitrate * 1000)
                encoding.setMaxFramerate_(FPS)
            sender.setParameters_(parameters)
        except Exception as e:
            self.log('cannot limit the bitrate: %s' % e, 'debug')

    def _post_changed(self):
        NotificationCenter().post_notification('VideoroomSessionParticipantDidChange', sender=self.session, data=NotificationData(participant=self.participant))

    # While sending

    def set_resolution(self, resolution):
        """Send at another resolution (one of RESOLUTIONS): the source adapts and the bitrate follows, no renegotiation."""
        name, width, height, bitrate = _resolution(resolution)
        if name == self.resolution:
            return
        self.resolution = name
        if self.source.get('kind') == 'rtsp':
            self.source['resolution'] = name
            set_source_resolution(self.source['url'], name)
        if self._video_source is not None:
            self._video_source.adaptOutputFormatToWidth_height_fps_(width, height, FPS)
            self._limit_bitrate()
        self.log('sending at %dx%d, %d kbit/s' % (width, height, bitrate))
        self._post_changed()

    def ice_failed(self):
        self.stop('the media connection failed (ICE)')
