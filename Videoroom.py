# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Joining a SylkServer video conference room over WebRTC.

SylkServer rooms are a Janus videoroom (an SFU) behind the webrtcgateway: every
participant publishes its camera and microphone on one PeerConnection and
subscribes to each other publisher on one PeerConnection per publisher (a feed).
The signalling is sylkRTC (SylkRTC.py): videoroom-join with our offer, the answer
in session-state accepted, the publishers in initial-publishers and
publishers-joined/left, then per publisher videoroom-feed-attach, the server's
offer in feed-attached and our videoroom-feed-answer.

The media are WebRTC.framework's (libwebrtc), one RTCPeerConnectionFactory for
the process:

  - the microphone and the speakers are its audio device (the system's default
    devices), with its own echo canceller; every feed's audio is played by it;
  - the camera is the SDK's (SIPApplication.video_device), read through a
    FrameBufferVideoRenderer like any other view of it and handed to an
    RTCVideoSource as 32ARGB pixel buffers, so the camera chosen in Blink, the
    screen included, is what the room sees. The source scales it to 640x480 at
    24 fps. Camera off disables the track, which then sends black;
  - the codecs are negotiated by it: the room's codec is set on the server (Janus
    videocodec) and the answer picks one of those offered;
  - the video orientation a mobile sends (urn:3gpp:video-orientation) is applied
    by it: the frames of a feed's track come upright to their renderer.

A feed's video is its participant's video_track (an RTCVideoTrack), which the
video conference window renders. Whether a feed carries video at all is read
from its offer (an m=video that is rejected, inactive or recvonly has none):
those participants are audio only, as the SIP callers are.

While the screen is the video device (ScreenPointer.is_screen_device) it goes
at 1280x720, 15 fps and 1800 kbit/s, the camera at 640x480, 24 fps and 1024
kbit/s; the source is adapted in place when the device changes.

The PeerConnections' statistics are read a few times a second: the audio level
of our microphone and of every feed (who speaks), the video bytes received of
every feed (the window's video loss check) and its codec. SIP callers speak by
the room's conference-audio-levels.

The room's controls are Sylk Mobile's (react-native-sylkrtc): the speakers
(videoroom-configure, the configure event), raised hands, muting everyone or one
participant, removing SIP participants, inviting (with invite-status back).

Publishers of type sip are callers behind the room's audio bridge: they have no
feed, they are shown by name only. The bridge itself (type bridge) carries their
mixed audio: it is subscribed to, but not shown.

Threads: libwebrtc calls its delegates and completion handlers on its signalling
thread, the camera frames come on pjsip's video thread; anything that touches the
signalling or the user interface is handed to the GUI thread.

Notifications (sender the VideoroomSession):

  VideoroomSessionDidChangeState          state, reason, room
  VideoroomSessionAudioMutedDidChange     muted, room
  VideoroomSessionVideoMutedDidChange     muted, room
  VideoroomSessionParticipantDidJoin      participant
  VideoroomSessionParticipantDidLeave     participant
  VideoroomSessionParticipantDidChange    participant
  VideoroomSessionParticipantsDidChange   -
  VideoroomSessionGotMessage              type, content, content_type, sender_uri, sender_name, timestamp
  VideoroomSessionDidDeliverMessage       message_id, delivered, code, reason
  VideoroomSessionSpeakersDidChange       speakers (publisher ids, ours is the session id), originator (a name, or '')
  VideoroomSessionRaisedHandsDidChange    raised_hands (publisher ids, ours is the session id)
  VideoroomSessionWasMutedByOther         originator (a name)
  VideoroomSessionGotInviteStatus         uri, state (progress, success, failed), code, reason
  VideoroomSessionSpeakingDidChange       speaking (our own microphone, after mute)
  VideoroomSessionParticipantSpeakingDidChange  participant
  VideoroomSessionLocalVideoDidChange     track (our video track was replaced: the screen or the camera)
  VideoroomSessionExtraVideoDidChangeState  extra, state, reason (ExtraVideo.py: one of our extra videos)

Several videos per participant (gateway feature multiple-video-feeds): a participant
publishes one audio and video, and up to 4 extra videos, each a publisher of its own
with parent (the owner's publisher id) and label. Someone else's are participants
named "<owner> · <label>" (no microphone, no hand of their own); ours are ExtraVideo
objects (session.extra_videos), shown from their own track, and the room's publishers
event for them is skipped.
"""

__all__ = ['VideoroomManager', 'VideoroomSession', 'Participant']

import os
import re
import time
import uuid

from hashlib import md5

import objc

from AppKit import NSApp
from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
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
                    kCVPixelFormatType_32ARGB)

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from SylkDiscovery import SylkServerDiscovery
from SylkRTC import SylkRTCConnections
from WebRTCSupport import media_missing, webrtc_bundle, webrtc_classes, ws_server
from util import call_in_gui_thread, run_in_gui_thread


VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS = 640, 480, 24     # as the SIP video calls (VGA, 24 fps)
VIDEO_BITRATE = 1024 * 1000     # bit/s
# the screen: 720p is the largest frame of H264 level 3.1 (42e01f), what the rooms negotiate
SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS = 1280, 720, 15
SCREEN_BITRATE = 1800 * 1000    # bit/s
PUBLISHER_TIMEOUT = 20          # seconds from join to established
STATS_INTERVAL = 0.3            # seconds between reads of the PeerConnections' statistics
SPEAKING_LEVEL = 10 ** (-38 / 20.0)     # audioLevel (0-1, linear) above which someone speaks: -38 dBFS
SPEAKING_SIP_LEVEL = 25         # the same for the room's audio levels of SIP participants (the mixer's 0-255 scale)
SPEAKING_HOLD = 0.6             # seconds speaking lasts after the last loud level, so a tile does not flicker between words

# libwebrtc's enums (RTCPeerConnection.h, RTCConfiguration.h, RTCRtpTransceiver.h, ...)
SDP_OFFER, SDP_ANSWER = 0, 2                            # RTCSdpType
BUNDLE_MAX_BUNDLE = 2                                   # RTCBundlePolicy
RTCP_MUX_REQUIRE = 1                                    # RTCRtcpMuxPolicy
SDP_SEMANTICS_UNIFIED_PLAN = 1                          # RTCSdpSemantics
DIRECTION_SEND_ONLY = 1                                 # RTCRtpTransceiverDirection
MEDIA_AUDIO, MEDIA_VIDEO = 0, 1                         # RTCRtpMediaType
ICE_GATHERING_COMPLETE = 2                              # RTCIceGatheringState
ICE_STATES = ['new', 'checking', 'connected', 'completed', 'failed', 'disconnected', 'closed']
ICE_FAILED = 4
ROTATION_0 = 0                                          # RTCVideoRotation
DEGRADATION_MAINTAIN_RESOLUTION = 2                     # RTCDegradationPreference


def _log(text, level='info'):
    getattr(BlinkLogger(), 'log_%s' % level)(text)


def _timer(interval, function, repeats=False):
    """A timer on the main run loop calling function(), once or repeatedly; invalidate() it to cancel."""
    timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, repeats, lambda timer: function())
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return timer


def _contact(uri):
    """The contact (not a passer-by) that has uri in the contacts list, or None."""
    if not uri or '@' not in uri:
        return None
    try:
        return NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(uri)
    except Exception:
        return None


def sending_screen_device():
    """Whether Blink's video device is the screen ("My screen", ScreenPointer)."""
    from sipsimple.application import SIPApplication
    try:
        from ScreenPointer import is_screen_device
        return bool(is_screen_device(SIPApplication.video_device.real_name))
    except Exception:
        return False


def _error_text(error):
    return str(error.localizedDescription() or error) if error is not None else None


# The framework: loaded by WebRTCSupport, set up here once

_factory = None


def _cls(name):
    return webrtc_classes()[name]


def _register_block_metadata():
    """The completion handlers of RTCPeerConnection are blocks: without their signatures PyObjC cannot make them.
    Argument indexes count self and _cmd; the block's own first argument is the block."""
    session_description = {'callable': {'retval': {'type': b'v'}, 'arguments': {0: {'type': b'^v'}, 1: {'type': b'@'}, 2: {'type': b'@'}}}}
    error_only = {'callable': {'retval': {'type': b'v'}, 'arguments': {0: {'type': b'^v'}, 1: {'type': b'@'}}}}
    for selector, block in ((b'offerForConstraints:completionHandler:', session_description),
                            (b'answerForConstraints:completionHandler:', session_description),
                            (b'setLocalDescription:completionHandler:', error_only),
                            (b'setRemoteDescription:completionHandler:', error_only),
                            (b'addIceCandidate:completionHandler:', error_only)):
        objc.registerMetaDataForSelector(b'RTCPeerConnection', selector, {'arguments': {3: block}})
    # statisticsWithCompletionHandler: has the block as its only argument
    objc.registerMetaDataForSelector(b'RTCPeerConnection', b'statisticsWithCompletionHandler:', {'arguments': {2: error_only}})


def peer_connection_factory():
    """The process's RTCPeerConnectionFactory (made on first use)."""
    global _factory
    if _factory is not None:
        return _factory
    missing = media_missing()
    if missing:
        raise RuntimeError(', '.join(missing))
    _register_block_metadata()
    functions = {}
    objc.loadBundleFunctions(webrtc_bundle(), functions, [('RTCInitializeSSL', b'Z')])
    if not functions['RTCInitializeSSL']():
        _log('[videoroom] RTCInitializeSSL failed', 'warning')
    _factory = _cls('RTCPeerConnectionFactory').alloc().initWithEncoderFactory_decoderFactory_(
        _cls('RTCDefaultVideoEncoderFactory').alloc().init(), _cls('RTCDefaultVideoDecoderFactory').alloc().init())
    formats = set(int(number) for number in _cls('RTCCVPixelBuffer').supportedPixelFormats())
    if int(kCVPixelFormatType_32ARGB) not in formats:
        _log('[videoroom] RTCCVPixelBuffer does not take 32ARGB pixel buffers: the camera cannot be sent', 'warning')
    return _factory


# SDP, for the Activity log

def _media_summary(sdp):
    """'audio OPUS/48000/2, video VP9/90000' from an SDP."""
    parts = []
    for match in re.finditer(r'^m=(\w+) (\d+) [^ ]+ ([^\r\n]*)', sdp, re.M):
        media, port, payloads = match.group(1), match.group(2), match.group(3).split()
        section_end = re.search(r'^m=', sdp[match.end():], re.M)
        section = sdp[match.end():match.end() + section_end.start()] if section_end else sdp[match.end():]
        if port == '0' and 'a=bundle-only' not in section:
            parts.append('%s rejected' % media)
            continue
        codec = None
        if payloads:
            rtpmap = re.search(r'^a=rtpmap:%s ([^\r\n]+)' % re.escape(payloads[0]), section, re.M)
            codec = rtpmap.group(1) if rtpmap else payloads[0]
        parts.append('%s %s' % (media, codec) if codec else media)
    return ', '.join(parts) or 'no media'


def _offers_video(sdp):
    """Whether a feed's offer carries video: an m=video that is not rejected nor inactive."""
    for match in re.finditer(r'^m=video (\d+) [^\r\n]*', sdp, re.M):
        section_end = re.search(r'^m=', sdp[match.end():], re.M)
        section = sdp[match.end():match.end() + section_end.start()] if section_end else sdp[match.end():]
        if match.group(1) == '0' and 'a=bundle-only' not in section:
            continue
        if re.search(r'^a=(inactive|recvonly)\s*$', section, re.M):
            continue
        return True
    return False


def _stats_entries(report):
    """(type, id, values) of every statistic of an RTCStatisticsReport, values a dict."""
    entries = []
    try:
        statistics = report.statistics()
        for key in statistics:
            statistic = statistics[key]
            values = statistic.values()
            entries.append((str(statistic.type()), str(statistic.id()), {str(name): values[name] for name in values}))
    except Exception:
        pass
    return entries


def _stats_by_kind(entries):
    """{(type, kind): values} of the RTP statistics (inbound-rtp, outbound-rtp, remote-inbound-rtp, media-source)
    of a report, the codec's mimeType added as codec; the first of a kind when there are several."""
    codecs = dict((id, values) for kind, id, values in entries if kind == 'codec')
    result = {}
    for kind, id, values in entries:
        if kind not in ('inbound-rtp', 'outbound-rtp', 'remote-inbound-rtp', 'media-source'):
            continue
        media = values.get('kind') or values.get('mediaType')
        if media is None or (kind, str(media)) in result:
            continue
        values = dict(values)
        codec = codecs.get(str(values.get('codecId')))
        if codec is not None and codec.get('mimeType'):
            values['codec'] = str(codec['mimeType']).rpartition('/')[2].upper()
        result[(kind, str(media))] = values
    return result


def _chosen_video_codec(sdp):
    """The encoding name of the first codec of m=video in an answer, or None."""
    match = re.search(r'^m=video (\d+) [^ ]+ ([^\r\n]*)', sdp, re.M)
    if match is None or match.group(1) == '0':
        return None
    rest = sdp[match.end():]
    next_media = re.search(r'^m=', rest, re.M)
    if next_media is not None:
        rest = rest[:next_media.start()]
    for pt in match.group(2).split():
        rtpmap = re.search(r'^a=rtpmap:%s ([^/\r\n]+)' % re.escape(pt), rest, re.M)
        if rtpmap is not None and rtpmap.group(1).upper() not in ('RTX', 'RED', 'ULPFEC', 'FLEXFEC-03'):
            return rtpmap.group(1).upper()
    return None


def _ice_servers(ice_servers):
    """RTCIceServer objects from a WebRTC iceServers list (the SylkServer configuration's)."""
    result = []
    for server in ice_servers or []:
        if not isinstance(server, dict):
            continue
        urls = server.get('urls') or server.get('url') or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [url for url in urls if isinstance(url, str) and url.split(':', 1)[0].lower() in ('stun', 'stuns', 'turn', 'turns')]
        if not urls:
            continue
        username = server.get('username')
        credential = server.get('credential')
        result.append(_cls('RTCIceServer').alloc().initWithURLStrings_username_credential_(
            urls, str(username) if username else None, str(credential) if credential else None))
    return result


# The delegate of a PeerConnection

class VideoroomPeerConnectionDelegate(NSObject):
    """What a PeerConnection says, handed to its session in the GUI thread. label is the session id for the
    publisher, the feed id for a feed. The PeerConnection keeps its delegate weakly: the session keeps it."""

    def initWithSession_label_(self, session, label):
        self = objc.super(VideoroomPeerConnectionDelegate, self).init()
        if self is not None:
            self.session = session
            self.label = label
        return self

    @objc.python_method
    def _hand(self, name, *args):
        session = self.session
        if session is not None:
            call_in_gui_thread(getattr(session, name), self.label, *args)

    @objc.typedSelector(b'v@:@q')
    def peerConnection_didChangeSignalingState_(self, peer_connection, state):
        pass

    @objc.typedSelector(b'v@:@@')
    def peerConnection_didAddStream_(self, peer_connection, stream):
        pass

    @objc.typedSelector(b'v@:@@')
    def peerConnection_didRemoveStream_(self, peer_connection, stream):
        pass

    @objc.typedSelector(b'v@:@')
    def peerConnectionShouldNegotiate_(self, peer_connection):
        pass        # the offer and the answers are made when the signalling needs them

    @objc.typedSelector(b'v@:@q')
    def peerConnection_didChangeIceConnectionState_(self, peer_connection, state):
        self._hand('_ice_connection_state', int(state))

    @objc.typedSelector(b'v@:@q')
    def peerConnection_didChangeIceGatheringState_(self, peer_connection, state):
        if int(state) == ICE_GATHERING_COMPLETE:
            self._hand('_end_of_candidates')

    @objc.typedSelector(b'v@:@@')
    def peerConnection_didGenerateIceCandidate_(self, peer_connection, candidate):
        self._hand('_local_candidate', str(candidate.sdp()), int(candidate.sdpMLineIndex()), str(candidate.sdpMid()) if candidate.sdpMid() else None)

    @objc.typedSelector(b'v@:@@')
    def peerConnection_didRemoveIceCandidates_(self, peer_connection, candidates):
        pass

    @objc.typedSelector(b'v@:@@')
    def peerConnection_didOpenDataChannel_(self, peer_connection, channel):
        pass

    @objc.typedSelector(b'v@:@q')
    def peerConnection_didChangeConnectionState_(self, peer_connection, state):
        pass


# Participants

class Participant(object):
    """Someone in the room: a WebRTC publisher (with a feed we subscribe to) or a SIP caller behind the bridge."""

    def __init__(self, publisher):
        self.id = publisher.get('id')
        self.uri = (publisher.get('uri') or '').replace('sip:', '', 1)
        self.display_name = publisher.get('display_name') or ''
        self.type = publisher.get('type') or 'webrtc'
        self.parent = publisher.get('parent') or None   # an extra video of another participant: that participant's publisher id
        self.label = publisher.get('label') or None     # and its name ("Camera 2")
        self.session = None                             # the room session it is in (set when added), for its parent's name
        self.extra_video = None                         # one of our own extra videos: its ExtraVideo
        self.muted = bool(publisher.get('muted'))
        self._contact = None        # looked up once, on first use
        self._contact_looked_up = False
        # whether the feed carries video: None until its offer comes; a SIP caller never has any
        self.video = False if self.type == 'sip' else None
        self.video_bytes = 0        # video bytes received (statistics), for the bitrate shown on the tile
        self.video_frames = None    # video frames decoded (statistics), whether video flows; None until known
        self.video_size = None      # (width, height) of the frames received (statistics)
        self.video_codec = None     # the feed's video codec, for the tile's statistics
        self.stats = {}             # (type, kind) -> values, the last statistics of its PeerConnection (Conference info)
        self.speaking = False
        self._speaking_until = 0.0
        self.feed = None            # feed id once attached
        self.state = None           # None, attaching, connecting, established, failed
        self.peer_connection = None
        self.delegate = None
        self.video_track = None     # its RTCVideoTrack, once the offer is answered
        self.is_self = False        # our own SIP leg into the room (the MSRP chat), seen behind the bridge

    def __repr__(self):
        return '<Participant %s %s %s>' % (self.uri, self.type, self.state)

    @property
    def contact(self):
        """The contact with this address in our contacts list, or None."""
        if not self._contact_looked_up:
            self._contact_looked_up = True
            self._contact = _contact(self.uri)
        return self._contact

    @property
    def name(self):
        """The name of the contact with this address in our contacts, else the one the room gives, else the address.
        An extra video: its participant's name and its label, "Adrian · Camera 2"; one of ours: its label."""
        if self.parent:
            owner = self.session.participants.get(self.parent) if self.session is not None else None
            if owner is not None:
                return '%s · %s' % (owner.name, self.label or 'Camera')
            return self.display_name or '%s · %s' % (self.uri, self.label or 'Camera')
        name = getattr(self.contact, 'name', None)
        return str(name) if name else (self.display_name or self.uri)

    @property
    def icon(self):
        """The contact's photograph, or None: the stand-in pictures (the grey person, a group) are not one, and
        without a photograph the avatar is the initials on the name's colour, as in the contact list."""
        contact = self.contact
        if contact is None:
            return None
        avatar = getattr(contact, 'avatar', None)
        if avatar is not None:
            from Avatars import is_placeholder_avatar
            if is_placeholder_avatar(getattr(avatar, 'path', None)):
                return None
            icon = getattr(avatar, 'icon', None)
            if icon is not None:
                return icon
        return getattr(contact, 'icon', None)

    @property
    def has_video(self):
        return self.video_track is not None

    def set_level(self, loud):
        """loud is whether this level counts as speech; speaking holds for SPEAKING_HOLD after the
        last loud one. Whether speaking changed."""
        now = time.monotonic()
        if loud:
            self._speaking_until = now + SPEAKING_HOLD
        speaking = now < self._speaking_until
        if speaking == self.speaking:
            return False
        self.speaking = speaking
        return True

    @property
    def subscribable(self):
        return self.type != 'sip'

    @property
    def visible(self):
        return self.type != 'bridge' and not self.is_self

    def update(self, publisher):
        """Take what changed; whether anything did."""
        changed = False
        if publisher.get('muted') is not None and bool(publisher['muted']) != self.muted:
            self.muted = bool(publisher['muted'])
            changed = True
        if publisher.get('display_name') and publisher['display_name'] != self.display_name:
            self.display_name = publisher['display_name']
            changed = True
        return changed


# A room

@implementer(IObserver)
class VideoroomSession(object):
    """One room joined from one account."""

    def __init__(self, account, room, audio=True, video=True, target=None):
        self.account = account
        self.room = room.replace('sip:', '', 1)
        # a room of another SylkServer (WebRTCSupport.RoomTarget): its server, its ICE servers, its guests
        self.target = target if target is not None and target.remote else None
        # the address of the same room's MSRP switch, which the MSRP window has
        self.sip_room = target.sip_uri if target is not None else self.room
        self.identity = str(account.id)     # who we are in the room: ourselves, or a guest of the other server
        self._guest = False
        self.id = str(uuid.uuid4())
        self.state = 'idle'         # idle, connecting, accepted, established, terminated
        self.reason = ''
        self.duration = None        # seconds the room had existed when we joined
        self.participants = {}      # publisher id -> Participant
        self.feeds = {}             # feed id -> Participant
        self.audio_muted = not audio    # joined without audio: the microphone starts muted
        self.video_muted = not video    # joined without video: the camera starts off (black is sent)
        self.connection = None
        self.ice_servers = []
        self.peer_connection = None     # the publisher's
        self.local_video_track = None   # what we send, for the "Me" tile
        self.video_codec = None         # the video codec the answer chose for us
        self.extra_videos = {}          # session id -> ExtraVideo: our extra videos in the room (ExtraVideo.py)
        self.stats = {}                 # (type, kind) -> values, the publisher's last statistics (Conference info)
        self.pending_extra_sources = [] # [(source, label)] from Join Conference, started by the window once joined
        self.pending_screen = None      # the screen device Join Conference chose, shared once joined
        self._delegate = None
        self._audio_track = None
        self._video_source = None
        self._video_transceiver = None
        self._capturer = None
        self._camera_renderer = None
        self._pixel_pool = None
        self._pixel_pool_size = None
        self._establish_timer = None
        # candidates wait until the request that creates their session on the server
        # (videoroom-join, videoroom-feed-answer) has been sent
        self._trickle_ready = set()
        self._trickle_pending = {}
        self._join_sent = False
        self._invitees = []         # to invite once the room is joined
        self.video_paused = False   # the feeds' video is not wanted (the window cannot be seen): audio only
        self.speakers = []          # publisher ids the room shows large, for everyone (videoroom-configure); [] is the grid
        self.raised_hands = []      # publisher ids with a raised hand, in the order raised
        self.speaking = False       # our own microphone (after mute)
        self._speaking_until = 0.0
        self._muting_everyone = False   # we asked: the room's mute-audio is not for us
        self._sending_screen = None     # the video profile in use: the screen's or the camera's
        self._stats_timer = None
        self._stats_pending = set()     # labels whose statistics are being collected
        self._stats_failed = False
        self._levels_at = None          # when the room last sent its audio levels (SIP participants)

    def __repr__(self):
        return '<VideoroomSession %s %s>' % (self.room, self.state)

    @property
    def features(self):
        """What the room's gateway announced (SylkRTC: sip-instance, multiple-video-feeds)."""
        return set(getattr(self.connection, 'features', ()) or ())

    @property
    def multiple_video_feeds(self):
        """Whether the room's gateway takes more videos of ours (child publishers)."""
        from SylkRTC import FEATURE_MULTIPLE_VIDEO_FEEDS
        return FEATURE_MULTIPLE_VIDEO_FEEDS in self.features

    def log(self, text, level='info'):
        _log('[videoroom] %s: %s' % (self.room, text), level)

    def _post(self, name, **data):
        NotificationCenter().post_notification(name, sender=self, data=NotificationData(**data))

    # Joining and leaving

    def join(self):
        if self.state != 'idle':
            return
        if self.target is not None:
            url = self.target.ws_server
            self.ice_servers = self.target.ice_servers
        else:
            url = ws_server(self.account)
            if url is None:
                self._terminate('the account has no SylkServer WebSocket server')
                return
            try:
                self.ice_servers = (SylkServerDiscovery().configuration(self.account.id.domain) or {}).get('iceServers') or []
            except Exception:
                self.ice_servers = []
        self._set_state('connecting')
        self.log('joining from %s through %s%s' % (self.account.id, url, ' (the SylkServer of %s)' % self.target.domain if self.target is not None else ''))
        self.connection = SylkRTCConnections().get(url)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.connection, name='SylkRTCConnectionGotEvent')
        notification_center.add_observer(self, sender=self.connection, name='SylkRTCConnectionDidEnd')
        self.connection.add_account(self.account, self._account_added)

    def _join_as_guest(self):
        """Another server that does not take our domain: join as a guest of it (its defaultGuestDomain)."""
        name = re.sub(r'[^a-z0-9]+', '', (self.account.display_name or self.account.id.username or 'blink').lower()) or 'blink'
        self._guest = True
        self.identity = '%s%s@%s' % (name, os.urandom(2).hex(), self.target.guest_domain)
        username, _, domain = self.identity.partition('@')
        password = md5(('%s:%s:%s' % (username, domain, os.urandom(8).hex())).encode()).hexdigest()    # never checked: guests do not register
        self.log('%s does not take %s: joining as the guest %s' % (self.target.domain, self.account.id.domain, self.identity))
        self.connection.add_identity(self.identity, password, self.account.display_name or str(self.account.id), self._account_added)

    def _account_added(self, error):
        if self.state != 'connecting':
            return
        if error is not None and self.target is not None and not self._guest and 'not allowed' in str(error).lower():
            self._join_as_guest()
            return
        if error is not None:
            self._terminate('the server refused the account: %s' % error)
            return
        try:
            self._start_publisher()
        except Exception as e:
            self.log('cannot start the media: %s' % e, 'error')
            self._terminate('cannot start the media: %s' % e)
            return
        self._establish_timer = _timer(PUBLISHER_TIMEOUT, lambda: self._terminate('no media connection'))

    def leave(self):
        if self.state in ('idle', 'terminated'):
            return
        self.log('leaving')
        if self.connection is not None and self.connection.state == 'ready' and self._join_sent:
            self.connection.request(dict(sylkrtc='videoroom-leave', session=self.id))
        self._terminate('left')

    def _terminate(self, reason):
        if self.state == 'terminated':
            return
        self.reason = reason
        for extra in list(self.extra_videos.values()):
            extra.stop('left')      # our extra videos go with us (the server ends them too)
        if self._establish_timer is not None:
            self._establish_timer.invalidate()
            self._establish_timer = None
        if self._stats_timer is not None:
            self._stats_timer.invalidate()
            self._stats_timer = None
        self._stop_camera()
        for participant in list(self.participants.values()):
            self._close_feed(participant)
        if self._delegate is not None:
            self._delegate.session = None
        if self.peer_connection is not None:
            self.peer_connection.close()
        self.peer_connection = None
        self._delegate = None
        self._audio_track = self._video_source = self._video_transceiver = self._capturer = None
        self.local_video_track = None
        self._pixel_pool = None
        if self.connection is not None:
            notification_center = NotificationCenter()
            notification_center.remove_observer(self, sender=self.connection, name='SylkRTCConnectionGotEvent')
            notification_center.remove_observer(self, sender=self.connection, name='SylkRTCConnectionDidEnd')
            connection, self.connection = self.connection, None
            if not any(session.connection is connection for session in VideoroomManager().sessions if session is not self):
                _timer(1, lambda: SylkRTCConnections().release(connection))
        self.log('ended: %s' % reason)
        self._set_state('terminated', reason)
        VideoroomManager()._session_ended(self)

    def _set_state(self, state, reason=''):
        self.state = state
        # also for the MSRP window of the same room: no audio there while the room has ours
        self._post('VideoroomSessionDidChangeState', state=state, reason=reason, room=self.room)

    # The publisher

    def _configuration(self):
        configuration = _cls('RTCConfiguration').alloc().init()
        configuration.setIceServers_(_ice_servers(self.ice_servers))
        configuration.setBundlePolicy_(BUNDLE_MAX_BUNDLE)
        configuration.setRtcpMuxPolicy_(RTCP_MUX_REQUIRE)
        configuration.setSdpSemantics_(SDP_SEMANTICS_UNIFIED_PLAN)
        return configuration

    @staticmethod
    def _constraints():
        return _cls('RTCMediaConstraints').alloc().initWithMandatoryConstraints_optionalConstraints_(None, None)

    def _start_publisher(self):
        factory = peer_connection_factory()
        self._delegate = VideoroomPeerConnectionDelegate.alloc().initWithSession_label_(self, self.id)
        self.peer_connection = factory.peerConnectionWithConfiguration_constraints_delegate_(self._configuration(), self._constraints(), self._delegate)
        if self.peer_connection is None:
            raise RuntimeError('cannot make a PeerConnection')
        stream_id = 'blink-%s' % self.id

        # microphone: the audio device's; muted is a disabled track (silence is sent)
        audio_source = factory.audioSourceWithConstraints_(None)
        self._audio_track = factory.audioTrackWithSource_trackId_(audio_source, 'audio-%s' % self.id)
        self._audio_track.setIsEnabled_(not self.audio_muted)
        init = _cls('RTCRtpTransceiverInit').alloc().init()
        init.setDirection_(DIRECTION_SEND_ONLY)
        init.setStreamIds_([stream_id])
        self.peer_connection.addTransceiverWithTrack_init_(self._audio_track, init)

        # camera: the SDK's frames go into the source, which scales them to what the room gets
        self._video_source = self._make_video_source(sending_screen_device())
        self._apply_video_profile()
        self._capturer = _cls('RTCVideoCapturer').alloc().initWithDelegate_(self._video_source)
        self.local_video_track = factory.videoTrackWithSource_trackId_(self._video_source, 'video-%s' % self.id)
        self.local_video_track.setIsEnabled_(not self.video_muted)
        init = _cls('RTCRtpTransceiverInit').alloc().init()
        init.setDirection_(DIRECTION_SEND_ONLY)
        init.setStreamIds_([stream_id])
        self._video_transceiver = self.peer_connection.addTransceiverWithTrack_init_(self.local_video_track, init)

        def offered(description, error):
            call_in_gui_thread(self._offer_created, description, _error_text(error))

        self.peer_connection.offerForConstraints_completionHandler_(self._constraints(), offered)
        if not self.video_muted:
            self._attach_camera()

    def _offer_created(self, description, error):
        if self.state != 'connecting' or self.peer_connection is None:
            return
        if description is None:
            self._terminate('cannot create the offer: %s' % error)
            return
        sdp = str(description.sdp())

        def local_set(error):
            if error is not None:
                call_in_gui_thread(self._terminate, 'cannot use our offer: %s' % _error_text(error))

        self.peer_connection.setLocalDescription_completionHandler_(description, local_set)
        self.log('sending the offer (%s)' % _media_summary(sdp))
        _log('[videoroom] %s: offer\n%s' % (self.room, sdp), 'debug')
        request = dict(sylkrtc='videoroom-join', account=self.identity, session=self.id, uri=self.room, sdp=sdp, audio=True, video=True)
        if not self._guest:
            # our MSRP chat leg to the room carries the same +sip.instance (SessionController): the gateway
            # then knows that endpoint of the focus is this device, not another participant
            try:
                from sipsimple.configuration.settings import SIPSimpleSettings
                instance_id = SIPSimpleSettings().instance_id
            except Exception:
                instance_id = None
            if instance_id:
                request['sip_instance'] = str(instance_id)
        self.connection.request(request, self._join_answered)
        self._join_sent = True
        self._flush_trickle(self.id)

    def _join_answered(self, error):
        if error is not None:
            self._terminate('cannot join: %s' % error)

    def _on_answer(self, sdp):
        self.log('answer: %s' % _media_summary(sdp))
        _log('[videoroom] %s: answer\n%s' % (self.room, sdp), 'debug')
        codec = _chosen_video_codec(sdp)
        if codec is None:
            self.log('the room takes no video from us', 'warning')
        answer = _cls('RTCSessionDescription').alloc().initWithType_sdp_(SDP_ANSWER, sdp)

        def remote_set(error):
            call_in_gui_thread(self._answer_set, codec, _error_text(error))

        self.peer_connection.setRemoteDescription_completionHandler_(answer, remote_set)

    def _answer_set(self, codec, error):
        if self.peer_connection is None:
            return
        if error is not None:
            self._terminate('cannot use the answer: %s' % error)
            return
        if codec is not None:
            self.log('sending video %s' % codec)
        self.video_codec = codec
        self._limit_video_bitrate()

    def _limit_video_bitrate(self):
        if self._video_transceiver is None:
            return
        bitrate, fps = (SCREEN_BITRATE, SCREEN_FPS) if self._sending_screen else (VIDEO_BITRATE, VIDEO_FPS)
        try:
            sender = self._video_transceiver.sender()
            parameters = sender.parameters()
            for encoding in parameters.encodings():
                encoding.setMaxBitrateBps_(bitrate)
                encoding.setMaxFramerate_(fps)
            try:
                # the screen keeps its resolution (readable text) and gives up frames instead; a camera the default
                parameters.setDegradationPreference_(DEGRADATION_MAINTAIN_RESOLUTION if self._sending_screen else None)
            except Exception:
                pass
            sender.setParameters_(parameters)
        except Exception as e:
            self.log('cannot limit the video bitrate: %s' % e, 'debug')

    @staticmethod
    def _make_video_source(screen):
        """A video source; for the screen a screencast one (libwebrtc treats it as screen content)."""
        factory = peer_connection_factory()
        if screen and factory.respondsToSelector_(b'videoSourceForScreenCast:'):
            return factory.videoSourceForScreenCast_(True)
        return factory.videoSource()

    def _replace_video_source(self, screen):
        """The video device went to or from the screen: a new source of the right kind, with its track
        on the same sender (no renegotiation)."""
        if self._video_transceiver is None or self.peer_connection is None:
            return
        factory = peer_connection_factory()
        source = self._make_video_source(screen)
        capturer = _cls('RTCVideoCapturer').alloc().initWithDelegate_(source)
        self._track_serial = getattr(self, '_track_serial', 0) + 1
        track = factory.videoTrackWithSource_trackId_(source, 'video-%s-%d' % (self.id, self._track_serial))
        track.setIsEnabled_(not self.video_muted)
        try:
            self._video_transceiver.sender().setTrack_(track)
        except Exception as e:
            self.log('cannot change the video source: %s' % e, 'warning')
            return
        self._capturer, self._video_source, self.local_video_track = capturer, source, track
        self._post('VideoroomSessionLocalVideoDidChange', track=track)

    def _apply_video_profile(self):
        """The screen goes at 720p, 15 fps and a higher bitrate, the camera at VGA, 24 fps (adapted in place)."""
        if self._video_source is None:
            return
        screen = sending_screen_device()
        if screen == self._sending_screen:
            return
        first = self._sending_screen is None
        self._sending_screen = screen
        width, height, fps, bitrate = (SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS, SCREEN_BITRATE) if screen else (VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_BITRATE)
        if not first:
            self._replace_video_source(screen)
        self._video_source.adaptOutputFormatToWidth_height_fps_(width, height, fps)
        if not first:
            self._limit_video_bitrate()
        if screen or not first:
            self.log('sending the %s at %dx%d, %d fps, %d kbit/s' % ('screen' if screen else 'camera', width, height, fps, bitrate // 1000))

    # The camera

    def _attach_camera(self):
        from sipsimple.application import SIPApplication
        from sipsimple.core import FrameBufferVideoRenderer
        if self._camera_renderer is not None or self._video_source is None:
            return
        try:
            producer = SIPApplication.video_device.producer
        except AttributeError:
            producer = None
        if producer is None:
            self.log('no camera: sending black', 'warning')
            return
        try:
            self._camera_renderer = FrameBufferVideoRenderer(self._on_camera_frame)
            self._camera_renderer.producer = producer
        except Exception as e:
            self.log('cannot read the camera (%s): sending black' % e, 'warning')
            self._camera_renderer = None

    def _stop_camera(self):
        renderer, self._camera_renderer = self._camera_renderer, None
        if renderer is not None:
            try:
                renderer.close()
            except Exception:
                pass

    def camera_changed(self):
        """The camera device was switched in Blink (the screen included): read the new one."""
        if self.peer_connection is None:
            return
        self._apply_video_profile()
        if self.video_muted:
            return
        self._stop_camera()
        self._attach_camera()

    def _pixel_buffer(self, width, height):
        if self._pixel_pool is None or self._pixel_pool_size != (width, height):
            attributes = {kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32ARGB,
                          kCVPixelBufferWidthKey: width,
                          kCVPixelBufferHeightKey: height,
                          kCVPixelBufferIOSurfacePropertiesKey: {}}
            result, pool = CVPixelBufferPoolCreate(None, None, attributes, None)
            if result != 0 or pool is None:
                return None
            self._pixel_pool, self._pixel_pool_size = pool, (width, height)
        result, buffer = CVPixelBufferPoolCreatePixelBuffer(None, self._pixel_pool, None)
        return buffer if result == 0 else None

    def _on_camera_frame(self, frame):
        # pjsip's video thread: hand the frame to the video source and return. The framebuffer
        # device produces ARGB (A, R, G, B in memory), rows packed at width * 4.
        source, capturer = self._video_source, self._capturer
        if source is None or capturer is None or self.video_muted:
            return
        width, height = frame.width, frame.height
        data = frame.data
        row = width * 4
        if width <= 0 or height <= 0 or len(data) < row * height:
            return
        with objc.autorelease_pool():
            buffer = self._pixel_buffer(width, height)
            if buffer is None:
                return
            if CVPixelBufferLockBaseAddress(buffer, 0) != 0:
                return
            try:
                base = CVPixelBufferGetBaseAddress(buffer)
                if base is None:
                    return
                try:
                    base = base.as_buffer(CVPixelBufferGetDataSize(buffer))
                except AttributeError:
                    base = memoryview(base)
                stride = CVPixelBufferGetBytesPerRow(buffer)
                if stride == row:
                    base[:row * height] = data[:row * height]
                else:
                    for line in range(height):
                        base[line * stride:line * stride + row] = data[line * row:line * row + row]
            finally:
                CVPixelBufferUnlockBaseAddress(buffer, 0)
            video_frame = _cls('RTCVideoFrame').alloc().initWithBuffer_rotation_timeStampNs_(
                _cls('RTCCVPixelBuffer').alloc().initWithPixelBuffer_(buffer), ROTATION_0, time.monotonic_ns())
            source.capturer_didCaptureVideoFrame_(capturer, video_frame)

    # Controls

    def set_speakers(self, ids):
        """Make these publishers (ours is self.id; none, one or two) the room's speakers: shown large to
        everyone, as Sylk desktop's and Sylk Mobile's speaker selection does. The room's configure event
        changes the layout, for us too."""
        ids = [str(id) for id in ids][:2]
        if self.connection is None or self.state != 'established':
            return
        self.log('setting the speakers to %s' % (', '.join(self.speaker_name(id) for id in ids) or 'nobody (grid)'))

        def configured(error):
            if error is not None:
                self.log('setting the speakers failed: %s' % error, 'warning')

        self.connection.request(dict(sylkrtc='videoroom-configure', session=self.id, active_participants=ids), configured)

    # Moderation, as Sylk Mobile does it (react-native-sylkrtc Conference)

    def toggle_hand(self, session_id=None):
        """Raise or lower our hand; with session_id, lower someone else's."""
        if self.connection is None or self.state != 'established':
            return
        request = dict(sylkrtc='videoroom-toggle-hand', session=self.id)
        if session_id is not None:
            request['session_id'] = session_id
            self.log('lowering the hand of %s' % self.speaker_name(session_id))
        else:
            self.log('lowering our hand' if self.id in self.raised_hands else 'raising our hand')
        self.connection.request(request)

    def mute_everyone(self):
        """Ask everyone in the room to mute their microphone (not us)."""
        if self.connection is None or self.state != 'established':
            return
        self.log('muting everyone')
        self._muting_everyone = True
        _timer(3.0, lambda: setattr(self, '_muting_everyone', False))
        self.connection.request(dict(sylkrtc='videoroom-mute-audio-participants', session=self.id))

    def mute_participant(self, participant, muted=True):
        """Mute or unmute one participant: a WebRTC one is asked (mute-request), a SIP one is muted at the mix."""
        if self.connection is None or self.state != 'established':
            return
        self.log('%s %s' % ('muting' if muted else 'unmuting', participant.uri))
        self.connection.request(dict(sylkrtc='videoroom-mute-participant', session=self.id, participant_id=participant.id, muted=bool(muted)))

    def remove_participants(self, uris):
        """Remove SIP participants from the room (the server asks the focus to end their sessions)."""
        uris = [uri if uri.startswith('sip:') else 'sip:' + uri for uri in uris]
        if not uris or self.connection is None or self.state != 'established':
            return
        self.log('removing %s' % ', '.join(uris))
        self.connection.request(dict(sylkrtc='videoroom-remove', session=self.id, participants=uris))

    def speaker_name(self, id):
        if id == self.id:
            return 'you'
        extra = self.extra_videos.get(id)
        if extra is not None:
            return 'your %s' % extra.label
        participant = self.participants.get(id)
        return participant.name if participant is not None else str(id)

    def refresh_video(self, participant):
        """Pause and resume a feed's video, as Sylk Mobile does on a stall: Janus then sends a new keyframe."""
        feed = participant.feed
        if self.connection is None or feed not in self.feeds:
            return
        self.log('%s video stalled, asking for it again' % participant.uri)

        def resume():
            if self.connection is not None and participant.feed == feed and not self.video_paused:
                self.connection.request(dict(sylkrtc='videoroom-session-update', session=feed, options=dict(video=True)))

        self.connection.request(dict(sylkrtc='videoroom-session-update', session=feed, options=dict(video=False)))
        _timer(0.3, resume)

    def invite(self, uris):
        """Invite people to the room (the server calls them)."""
        uris = [uri.replace('sip:', '', 1) for uri in uris]
        if self.state != 'established':
            self._invitees.extend(uris)
            return
        self.log('inviting %s' % ', '.join(uris))
        self.connection.request(dict(sylkrtc='videoroom-invite', session=self.id, participants=uris))

    def set_audio_muted(self, muted):
        self.audio_muted = bool(muted)
        if self._audio_track is not None:
            self._audio_track.setIsEnabled_(not self.audio_muted)
        self.log('microphone muted' if self.audio_muted else 'microphone unmuted')
        self._post('VideoroomSessionAudioMutedDidChange', muted=self.audio_muted, room=self.room)
        if self.audio_muted:
            self._own_level(False)

    # Statistics: who speaks (audio levels), how much video comes (the window's video loss check)

    def _start_stats(self):
        if self._stats_timer is None and not self._stats_failed:
            self._stats_timer = _timer(STATS_INTERVAL, self._poll_stats, repeats=True)

    def _poll_stats(self):
        if self.state != 'established' or self._stats_failed:
            return
        if self.peer_connection is not None:
            self._request_stats(self.id, self.peer_connection)
        for participant in list(self.feeds.values()):
            if participant.peer_connection is not None:
                self._request_stats(participant.feed, participant.peer_connection)
        for extra in list(self.extra_videos.values()):
            if extra.peer_connection is not None:
                self._request_stats(extra.id, extra.peer_connection)
        # SIP callers speak by the room's audio levels: when they stop coming, nobody of them speaks
        if self._levels_at is not None and time.monotonic() - self._levels_at > 2.0:
            self._levels_at = None
            for participant in list(self.participants.values()):
                if participant.type == 'sip' and participant.speaking:
                    participant._speaking_until = 0.0
                    if participant.set_level(False):
                        self._post('VideoroomSessionParticipantSpeakingDidChange', participant=participant)
        if self._speaking_until and time.monotonic() >= self._speaking_until:
            self._own_level(False)

    def _request_stats(self, label, peer_connection):
        if label in self._stats_pending:
            return
        self._stats_pending.add(label)

        def collected(report):
            entries = _stats_entries(report)
            call_in_gui_thread(self._stats_collected, label, entries)

        try:
            peer_connection.statisticsWithCompletionHandler_(collected)
        except Exception as e:
            self._stats_pending.discard(label)
            self._stats_failed = True
            self.log('cannot read the media statistics: %s (no speaking highlight, no video loss check)' % e, 'warning')

    def _stats_collected(self, label, entries):
        self._stats_pending.discard(label)
        if self.state == 'terminated':
            return
        stats = _stats_by_kind(entries)
        if label == self.id:
            self.stats = stats
        elif label in self.extra_videos:
            self.extra_videos[label].participant.stats = stats
            return
        elif label in self.feeds:
            self.feeds[label].stats = stats
        if label == self.id:
            levels = [values.get('audioLevel') for kind, id, values in entries if kind == 'media-source' and values.get('kind') == 'audio']
            levels = [float(level) for level in levels if level is not None]
            if levels:
                self._own_level(max(levels) > SPEAKING_LEVEL and not self.audio_muted)
            return
        participant = self.feeds.get(label)
        if participant is None:
            return
        codecs = dict((id, values) for kind, id, values in entries if kind == 'codec')
        for kind, id, values in entries:
            if kind != 'inbound-rtp':
                continue
            media = values.get('kind') or values.get('mediaType')
            if media == 'audio' and values.get('audioLevel') is not None:
                if participant.set_level(float(values['audioLevel']) > SPEAKING_LEVEL):
                    self._post('VideoroomSessionParticipantSpeakingDidChange', participant=participant)
            elif media == 'video':
                participant.video_bytes = int(values.get('bytesReceived') or 0)
                if values.get('framesDecoded') is not None:
                    participant.video_frames = int(values['framesDecoded'])
                if values.get('frameWidth') and values.get('frameHeight'):
                    participant.video_size = (int(values['frameWidth']), int(values['frameHeight']))
                codec = codecs.get(str(values.get('codecId')))
                if codec is not None and codec.get('mimeType'):
                    participant.video_codec = str(codec['mimeType']).rpartition('/')[2].upper()

    def _own_level(self, loud):
        now = time.monotonic()
        if loud:
            self._speaking_until = now + SPEAKING_HOLD
        speaking = now < self._speaking_until
        if speaking != self.speaking:
            self.speaking = speaking
            self._post('VideoroomSessionSpeakingDidChange', speaking=speaking)

    def set_video_muted(self, muted):
        self.video_muted = bool(muted)
        if self.local_video_track is not None:
            self.local_video_track.setIsEnabled_(not self.video_muted)      # a disabled track sends black
        if self.video_muted:
            self._stop_camera()
        else:
            self._attach_camera()
        self.log('camera off' if self.video_muted else 'camera on')
        self._post('VideoroomSessionVideoMutedDidChange', muted=self.video_muted, room=self.room)

    def set_video_paused(self, paused):
        """Stop or start receiving the video of every feed (the server stops sending it; audio goes on), as
        Sylk Mobile's pauseVideo/resumeVideo: for when the window showing it cannot be seen."""
        paused = bool(paused)
        if paused == self.video_paused:
            return
        self.video_paused = paused
        if self.connection is None or self.state == 'terminated':
            return
        feeds = [participant for participant in self.feeds.values() if participant.peer_connection is not None]
        self.log('%s the video of %d participant(s): the window is %s' % ('pausing' if paused else 'resuming', len(feeds), 'not visible' if paused else 'visible'))
        for participant in feeds:
            self._update_feed(participant, video=not paused)

    def _update_feed(self, participant, video):
        if self.connection is None or participant.feed is None:
            return

        def answered(error, participant=participant):
            if error is not None:
                self.log('cannot %s the video of %s: %s' % ('resume' if video else 'pause', participant.uri, error), 'warning')

        self.connection.request(dict(sylkrtc='videoroom-session-update', session=participant.feed, options=dict(video=bool(video))), answered)

    # ICE (from the delegates, in the GUI thread)

    def _local_candidate(self, label, candidate, mline, mid):
        if self.connection is None or self.state == 'terminated':
            return
        if candidate.startswith('a='):
            candidate = candidate[2:]
        self._trickle(label, [dict(candidate=candidate, sdpMLineIndex=mline, sdpMid=mid if mid is not None else str(mline))])

    def _end_of_candidates(self, label):
        if self.connection is not None and self.state != 'terminated':
            self._trickle(label, [])

    def _trickle(self, label, candidates):
        if label not in self._trickle_ready:
            self._trickle_pending.setdefault(label, []).append(candidates)
            return
        self.connection.request(dict(sylkrtc='videoroom-session-trickle', session=label, candidates=candidates))

    def _flush_trickle(self, label):
        self._trickle_ready.add(label)
        for candidates in self._trickle_pending.pop(label, []):
            self._trickle(label, candidates)

    def _ice_connection_state(self, label, state):
        name = ICE_STATES[state] if 0 <= state < len(ICE_STATES) else str(state)
        self.log('ICE %s: %s' % ('publisher' if label == self.id else label, name), 'debug')
        if state != ICE_FAILED or self.state == 'terminated':
            return
        if label == self.id:
            self._terminate('the media connection failed (ICE)')
            return
        extra = self.extra_videos.get(label)
        if extra is not None:
            extra.ice_failed()
            return
        participant = self.feeds.get(label)
        if participant is not None:
            participant.state = 'failed'
            self.log('media from %s failed (ICE)' % participant.uri, 'warning')
            self._post('VideoroomSessionParticipantDidChange', participant=participant)

    # Signalling events

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SylkRTCConnectionDidEnd(self, notification):
        if self.state != 'terminated':
            self._terminate('connection to the server lost: %s' % notification.data.reason)

    def _NH_SylkRTCConnectionGotEvent(self, notification):
        message = notification.data.event
        if message.get('sylkrtc') != 'videoroom-event' or message.get('session') != self.id:
            return
        event = message.get('event')
        handler = getattr(self, '_EH_' + (event or '').replace('-', '_'), None)
        if handler is not None:
            try:
                handler(message)
            except Exception as e:
                self.log('handling %s failed: %s' % (event, e), 'error')
                _log('[videoroom] %s' % message, 'debug')

    def _EH_session_state(self, message):
        state = message.get('state')
        if state == 'progress':
            self.log('the server is joining the room')
        elif state == 'accepted':
            if isinstance(message.get('duration'), int):
                self.duration = message['duration']
            self._set_state('accepted')
            self._on_answer(message.get('sdp') or '')
        elif state == 'established':
            if self._establish_timer is not None:
                self._establish_timer.invalidate()
                self._establish_timer = None
            self.log('media established')
            self._set_state('established')
            self._start_stats()
            invitees, self._invitees = self._invitees, []
            if invitees:
                self.invite(invitees)
        elif state == 'terminated':
            self._terminate(message.get('reason') or 'ended by the server')

    def _EH_initial_publishers(self, message):
        for publisher in message.get('publishers') or []:
            self._add_publisher(publisher)
        self._post('VideoroomSessionParticipantsDidChange')

    _EH_publishers_joined = _EH_initial_publishers

    def _EH_publishers_left(self, message):
        for publisher_id in message.get('publishers') or []:
            participant = self.participants.pop(publisher_id, None)
            if participant is None:
                continue
            self.log('%s left' % participant.uri)
            self._remove_feed(participant)
            self._post('VideoroomSessionParticipantDidLeave', participant=participant)
        self._post('VideoroomSessionParticipantsDidChange')

    def _EH_configure(self, message):
        ids = [str(id) for id in message.get('active_participants') or []]
        originator = message.get('originator')
        if originator == 'videoroom':
            who = 'the room'
        elif originator:
            who = self.speaker_name(str(originator))
        else:
            who = ''
        self.speakers = ids
        self.log('speakers set to %s%s' % (', '.join(self.speaker_name(id) for id in ids) or 'nobody (grid)', ' by %s' % who if who else ''))
        self._post('VideoroomSessionSpeakersDidChange', speakers=list(ids), originator=who)

    def _EH_invite_status(self, message):
        # the outcome of the REFER the server sent the focus for an invite of ours
        uri = str(message.get('participant') or '').replace('sip:', '', 1)
        state = str(message.get('state') or '')
        try:
            code = int(message.get('code') or 0)
        except (TypeError, ValueError):
            code = 0
        reason = str(message.get('reason') or '')
        self.log('invite of %s: %s%s' % (uri, state, (' (%d %s)' % (code, reason)).rstrip() if code else ''), 'warning' if state == 'failed' else 'info')
        self._post('VideoroomSessionGotInviteStatus', uri=uri, state=state, code=code, reason=reason)

    def _EH_raised_hands(self, message):
        ids = [str(id) for id in message.get('raised_hands') or []]
        if ids == self.raised_hands:
            return
        for id in ids:
            if id not in self.raised_hands:
                self.log('%s raised %s hand' % (self.speaker_name(id), 'our' if id == self.id else 'a'))
        self.raised_hands = ids
        self._post('VideoroomSessionRaisedHandsDidChange', raised_hands=list(ids))

    def _EH_mute_audio(self, message):
        originator = str(message.get('originator') or '')
        if self._muting_everyone or originator == self.id:
            self._muting_everyone = False
            return      # our own mute everyone
        who = self.speaker_name(originator) if originator else 'someone'
        self.log('%s muted everyone' % who)
        if not self.audio_muted:
            self.set_audio_muted(True)
            self._post('VideoroomSessionWasMutedByOther', originator=who)

    def _EH_conference_audio_levels(self, message):
        # SIP participants: the mixer's levels, rx being what comes from them (their speech)
        levels = message.get('levels') or []
        self._levels_at = time.monotonic()
        if isinstance(levels, dict):
            levels = [dict(entry, participant_id=pid) for pid, entry in levels.items()]
        for entry in levels:
            if not isinstance(entry, dict):
                continue
            participant = self.participants.get(str(entry.get('participant_id') or ''))
            if participant is None:
                continue
            level = entry.get('rx_peak') if entry.get('rx_peak') is not None else entry.get('rx', 0)
            try:
                loud = float(level) > SPEAKING_SIP_LEVEL
            except (TypeError, ValueError):
                continue
            if participant.set_level(loud):
                self._post('VideoroomSessionParticipantSpeakingDidChange', participant=participant)

    def _EH_publisher_updated(self, message):
        participant = self.participants.get(message.get('publisher'))
        if participant is not None and participant.update(dict(muted=message.get('muted'))):
            self._post('VideoroomSessionParticipantDidChange', participant=participant)

    def _EH_feed_attached(self, message):
        participant = self.feeds.get(message.get('feed'))
        if participant is None:
            return
        try:
            self._answer_feed(participant, message.get('sdp') or '')
        except Exception as e:
            self.log('cannot receive %s: %s' % (participant.uri, e), 'error')
            participant.state = 'failed'
            self._post('VideoroomSessionParticipantDidChange', participant=participant)

    def _EH_feed_established(self, message):
        participant = self.feeds.get(message.get('feed'))
        if participant is not None:
            participant.state = 'established'
            self.log('receiving %s' % participant.uri)
            self._post('VideoroomSessionParticipantDidChange', participant=participant)

    # The room's chat over the WebRTC connection: the gateway relays it to the room's MSRP switch.
    # The MSRP window uses it when its own MSRP session to the room cannot connect.

    def send_chat_message(self, content, content_type='text/plain'):
        """Send a message to the room's chat; its id (the delivery report comes with it)."""
        message_id = uuid.uuid4().hex
        if self.connection is None or self.state == 'terminated':
            raise RuntimeError('not in the room')
        if isinstance(content, bytes):
            content = content.decode('latin1')      # binary goes as latin1 over JSON, the gateway turns it back
        self.connection.request(dict(sylkrtc='videoroom-message', session=self.id, message_id=message_id, content=content, content_type=content_type))
        return message_id

    def _EH_message(self, message):
        sender = message.get('sender') or {}
        self._post('VideoroomSessionGotMessage', type=message.get('type') or 'normal', content=message.get('content') or '',
                   content_type=message.get('content_type') or 'text/plain', sender_uri=(sender.get('uri') or '').replace('sip:', '', 1),
                   sender_name=sender.get('display_name') or '', timestamp=message.get('timestamp'))

    def _EH_message_delivery(self, message):
        self._post('VideoroomSessionDidDeliverMessage', message_id=message.get('message_id'), delivered=bool(message.get('delivered')),
                   code=message.get('code'), reason=message.get('reason'))

    def _EH_mute_request(self, message):
        self.log('muted by a moderator' if message.get('muted') else 'unmuted by a moderator')
        self.set_audio_muted(bool(message.get('muted')))

    def _add_publisher(self, publisher):
        publisher_id = publisher.get('id')
        if not publisher_id:
            return
        participant = self.participants.get(publisher_id)
        if participant is not None:
            if participant.update(publisher):
                self._post('VideoroomSessionParticipantDidChange', participant=participant)
            return
        if publisher_id == self.id or publisher_id in self.extra_videos or publisher.get('parent') == self.id:
            return      # ourselves, or one of our extra videos (shown from its own track)
        participant = Participant(publisher)
        participant.session = self
        participant.is_self = participant.type == 'sip' and participant.uri.lower() in (str(self.account.id).lower(), self.identity.lower())
        self.participants[publisher_id] = participant
        self.log('%s is in the room (%s)' % (participant.uri, participant.type))
        self._post('VideoroomSessionParticipantDidJoin', participant=participant)
        if participant.subscribable:
            self._attach_feed(participant)

    # Feeds: the other publishers

    def _attach_feed(self, participant):
        participant.feed = str(uuid.uuid4())
        participant.state = 'attaching'
        self.feeds[participant.feed] = participant

        def attached(error):
            if error is not None:
                participant.state = 'failed'
                self._post('VideoroomSessionParticipantDidChange', participant=participant)

        self.connection.request(dict(sylkrtc='videoroom-feed-attach', session=self.id, publisher=participant.id, feed=participant.feed), attached)

    def _answer_feed(self, participant, sdp):
        participant.state = 'connecting'
        participant.video = _offers_video(sdp)      # without video: audio only, in the carousel
        self._post('VideoroomSessionParticipantDidChange', participant=participant)
        self.log('offer from %s: %s' % (participant.uri, _media_summary(sdp)))
        _log('[videoroom] %s: offer from %s\n%s' % (self.room, participant.uri, sdp), 'debug')
        factory = peer_connection_factory()
        feed = participant.feed
        participant.delegate = VideoroomPeerConnectionDelegate.alloc().initWithSession_label_(self, feed)
        participant.peer_connection = peer_connection = factory.peerConnectionWithConfiguration_constraints_delegate_(
            self._configuration(), self._constraints(), participant.delegate)
        if peer_connection is None:
            raise RuntimeError('cannot make a PeerConnection')
        offer = _cls('RTCSessionDescription').alloc().initWithType_sdp_(SDP_OFFER, sdp)

        def remote_set(error):
            call_in_gui_thread(self._feed_offer_set, participant, feed, _error_text(error))

        peer_connection.setRemoteDescription_completionHandler_(offer, remote_set)

    def _feed_offer_set(self, participant, feed, error):
        if participant.feed != feed or participant.peer_connection is None:
            return
        if error is not None:
            self.log('cannot take the offer of %s: %s' % (participant.uri, error), 'error')
            participant.state = 'failed'
            self._post('VideoroomSessionParticipantDidChange', participant=participant)
            return
        for transceiver in participant.peer_connection.transceivers():
            if transceiver.mediaType() == MEDIA_VIDEO and transceiver.receiver().track() is not None:
                participant.video_track = transceiver.receiver().track()
                break

        def answered(description, error):
            call_in_gui_thread(self._feed_answer_created, participant, feed, description, _error_text(error))

        participant.peer_connection.answerForConstraints_completionHandler_(self._constraints(), answered)

    def _feed_answer_created(self, participant, feed, description, error):
        if participant.feed != feed or participant.peer_connection is None or self.connection is None:
            return
        if description is None:
            self.log('cannot answer %s: %s' % (participant.uri, error), 'error')
            participant.state = 'failed'
            self._post('VideoroomSessionParticipantDidChange', participant=participant)
            return

        def local_set(error):
            if error is not None:
                call_in_gui_thread(self.log, 'cannot use our answer to %s: %s' % (participant.uri, _error_text(error)), 'error')

        participant.peer_connection.setLocalDescription_completionHandler_(description, local_set)
        sdp = str(description.sdp())
        participant.video_codec = participant.video_codec or _chosen_video_codec(sdp)
        self.connection.request(dict(sylkrtc='videoroom-feed-answer', session=self.id, feed=feed, sdp=sdp))
        self._flush_trickle(feed)
        if self.video_paused:
            self._update_feed(participant, video=False)
        self._post('VideoroomSessionParticipantDidChange', participant=participant)

    def _close_feed(self, participant):
        if participant.delegate is not None:
            participant.delegate.session = None
        if participant.peer_connection is not None:
            participant.peer_connection.close()
        participant.peer_connection = None
        participant.delegate = None
        participant.video_track = None

    def _remove_feed(self, participant):
        feed = participant.feed
        if feed is None:
            return
        self.feeds.pop(feed, None)
        self._trickle_ready.discard(feed)
        self._trickle_pending.pop(feed, None)
        if self.connection is not None and self.state != 'terminated':
            self.connection.request(dict(sylkrtc='videoroom-feed-detach', session=self.id, feed=feed))
        self._close_feed(participant)
        participant.feed = None


@implementer(IObserver)
class VideoroomManager(object, metaclass=Singleton):
    """The rooms joined over WebRTC."""

    def __init__(self):
        self.sessions = []
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='VideoDeviceDidChangeCamera')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')

    def find(self, room):
        """The session in room: its video room address or its MSRP switch address (another SylkServer's differ)."""
        room = room.replace('sip:', '', 1).lower()
        return next((session for session in self.sessions if room in (session.room.lower(), session.sip_room.lower()) and session.state != 'terminated'), None)

    def join(self, account, room, audio=True, video=True, target=None, extra_sources=None, screen=None):
        """Join room from account (its window shown), or bring forward the session already in it. extra_sources:
        [(source, label)] published as our extra videos once the room is joined (ExtraVideo.py); screen: a
        screen device shared once joined (the window starts both)."""
        from VideoroomWindowController import window_for_session
        session = self.find(room)
        if session is not None:
            window_for_session(session).show()
            return session
        session = VideoroomSession(account, room, audio=audio, video=video, target=target)
        session.pending_extra_sources = list(extra_sources or [])
        session.pending_screen = screen
        self.sessions.append(session)
        NotificationCenter().post_notification('VideoroomManagerDidAddSession', sender=self, data=NotificationData(session=session))
        window_for_session(session).show()
        session.join()
        return session

    def leave_all(self):
        for session in list(self.sessions):
            session.leave()

    def _session_ended(self, session):
        if session in self.sessions:
            self.sessions.remove(session)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_VideoDeviceDidChangeCamera(self, notification):
        for session in self.sessions:
            session.camera_changed()

    def _NH_SIPApplicationWillEnd(self, notification):
        self.leave_all()
