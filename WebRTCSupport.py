# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""WebRTC conferencing with SylkServer (the webrtcgateway's video rooms): what can be used, and where rooms are.

A SylkServer room is two things that share one name: the video room (a Janus
videoroom behind the webrtcgateway, reached over a WebSocket with the sylkRTC
protocol) and the MSRP switch (the conference application, reached over SIP,
carrying the room's chat and participants). Blink joins the video room for audio
and video, and the MSRP switch for chat only.

The media are done by WebRTC.framework (libwebrtc, Objective-C API), bundled by
build_scripts/05c_install_webrtc.sh and loaded here with objc.loadBundle, only once
an account's domain is found to publish a wsServer in its SylkServer configuration
(SylkDiscovery): nothing of it is loaded before that.

Rooms of other SylkServer domains are given as a link (https://host/conference/room,
as the Sylk web app shares them) or as room@host. The host's SylkServer
configuration says where its server is: wsServer, iceServers,
defaultConferenceDomain (the video rooms, at videoconference.<domain>) and
defaultGuestDomain. The same room's MSRP switch is room@conference.<domain>.
"""

__all__ = ['ws_server', 'media_missing', 'media_available', 'media_report', 'conference_video_available', 'join_video_conference',
           'active_videoroom', 'drop_sip_audio', 'room_chat_session', 'show_videoroom_window', 'webrtc_classes', 'webrtc_bundle',
           'parse_room', 'split_uri', 'local_room_host', 'RoomTarget', 'room_target', 'room_video', 'resolve_room',
           'VideoConferenceAvailability', 'DISCOVERY_TIMEOUT']

import os
import re
import sys

from urllib.parse import unquote, urlsplit

import objc

from AppKit import NSApp, NSBundle

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import AccountManager, BonjourAccount

from BlinkLogger import BlinkLogger
from SylkDiscovery import SylkServerDiscovery
from util import call_later, run_in_gui_thread


FRAMEWORK_NAME = 'WebRTC.framework'
DISCOVERY_TIMEOUT = 12              # seconds to wait for the SylkServer of a room's domain
DISCOVERY_POLL = 0.2                # seconds

# The classes the conferencing code needs from the framework (and Foundation, for the WebSocket)
REQUIRED_CLASSES = ('RTCPeerConnectionFactory', 'RTCPeerConnection', 'RTCConfiguration', 'RTCIceServer', 'RTCMediaConstraints',
                    'RTCSessionDescription', 'RTCIceCandidate', 'RTCRtpTransceiverInit', 'RTCVideoSource', 'RTCVideoFrame',
                    'RTCCVPixelBuffer', 'RTCMTLNSVideoView', 'RTCDefaultVideoEncoderFactory', 'RTCDefaultVideoDecoderFactory')


# The SylkServer of an account

def ws_server(account):
    """The wsServer of the account's domain (SylkServer discovery), or None."""
    if account is None or account is BonjourAccount():
        return None
    try:
        configuration = SylkServerDiscovery().configuration(account.id.domain) or {}
    except Exception:
        return None
    server = configuration.get('wsServer')
    if not isinstance(server, str) or not server.strip().startswith(('wss://', 'ws://')):
        return None
    return server.strip()


# WebRTC.framework

_framework = None           # the NSBundle, once loaded
_classes = {}               # what objc.loadBundle put in, by name
_missing = None             # media_missing(), once checked


def _framework_path():
    """Where the framework is: the app's Frameworks folder (Copy Frameworks of Blink and Blink Pro)."""
    path = os.path.join(NSBundle.mainBundle().privateFrameworksPath() or '', FRAMEWORK_NAME)
    return path if os.path.isdir(path) else None


def _load():
    """Load WebRTC.framework (once); the reason it cannot be, or None."""
    global _framework
    if _framework is not None:
        return None
    path = _framework_path()
    if path is None:
        return '%s is not in the application (build_scripts/05c_install_webrtc.sh)' % FRAMEWORK_NAME
    try:
        objc.loadBundle('WebRTC', _classes, bundle_path=path)
    except Exception as e:
        return 'cannot load %s: %s' % (path, e)
    _framework = NSBundle.bundleWithPath_(path)
    return None


def media_missing():
    """What is missing for WebRTC media, as a list of reasons; empty when everything is there. Loads the
    framework (checked once a run)."""
    global _missing
    if _missing is not None:
        return _missing
    reason = _load()
    if reason is not None:
        _missing = [reason]
        return _missing
    missing = [name for name in REQUIRED_CLASSES if _classes.get(name) is None]
    try:
        objc.lookUpClass('NSURLSessionWebSocketTask')
    except objc.nosuchclass_error:
        missing.append('NSURLSessionWebSocketTask')
    _missing = missing
    return _missing


def media_available():
    """Whether the WebRTC media can be done here (loads the framework; checked once a run)."""
    return not media_missing()


def webrtc_classes():
    """The classes of WebRTC.framework by name (empty until media_missing() has loaded it)."""
    return _classes


def webrtc_bundle():
    """The NSBundle of WebRTC.framework (None until media_missing() has loaded it), for its C functions."""
    return _framework


def _video_codecs():
    factory = _classes.get('RTCDefaultVideoEncoderFactory')
    if factory is None:
        return []
    names = []
    try:
        for info in factory.supportedCodecs():
            name = str(info.name())
            if name not in names:
                names.append(name)
    except Exception:
        pass
    return names


def media_report():
    """One line about the WebRTC media that can be done here, for the Activity log."""
    if media_missing():
        return 'not available (%s)' % ', '.join(media_missing())
    info = _framework.infoDictionary() or {}
    version = info.get('CFBundleShortVersionString') or info.get('CFBundleVersion') or 'unknown version'
    milestone = info.get('WebRTCMilestone')     # set by build_scripts/05c_install_webrtc.sh
    parts = ['%s %s%s' % (FRAMEWORK_NAME, version, ' (%s)' % milestone if milestone else ''),
             'video %s' % (', '.join(_video_codecs()) or 'none'),
             'audio OPUS',
             'echo cancellation built in',
             'WebSockets NSURLSession']
    return ', '.join(parts)


def join_video_conference(account, room, audio=True, video=True, target=None, extra_sources=None, screen=None):
    """Join room (user@host) over WebRTC from account, its window shown, the microphone muted without audio
    and the camera off without video (both can be turned on in the room); the VideoroomSession, or None
    when that cannot be done. target (a RoomTarget) is a room of another SylkServer: joined through its server."""
    if target is not None and target.remote:
        if not media_available():
            return None
    elif not conference_video_available(account):
        return None
    from Videoroom import VideoroomManager
    video_room = VideoroomManager().join(account, room, audio=audio, video=video, target=target, extra_sources=extra_sources, screen=screen)
    if video_room is not None:
        drop_sip_audio(video_room)
    return video_room


def active_videoroom(uri):
    """The video room joined to uri (a room address in any form: video room or MSRP switch), or None;
    never loads the WebRTC code."""
    module = sys.modules.get('Videoroom')
    if module is None or uri is None:
        return None
    user, host = split_uri(uri)
    if not user or not host:
        return None
    return module.VideoroomManager().find('%s@%s' % (user, host))


def _room_sessions(video_room):
    """The SIP sessions (SessionControllers) to the room of video_room, in any of its address forms, not ending."""
    from MediaStream import STATE_FAILED, STATE_FINISHED
    try:
        controllers = list(NSApp.delegate().contactsWindowController.sessionControllersManager.sessionControllers)
    except AttributeError:
        return
    for controller in controllers:
        if getattr(controller, 'finished', False) or controller.state in (STATE_FAILED, STATE_FINISHED) or controller.target_uri is None:
            continue
        if active_videoroom(controller.target_uri) is video_room:
            yield controller


def _silent_media_end(controller):
    """The audio moved to the video room, it did not end: no hangup tone (SessionRinger)."""
    controller.silent_media_end = True
    if controller.session is not None:
        controller.session.blink_silent_media_end = True


def drop_sip_audio(video_room):
    """The room's audio and video go over WebRTC now: SIP audio (and video) to the same room would loop
    through it, so it is removed from the sessions that have it; a session with nothing else is ended."""
    from MediaStream import STATE_CONNECTED
    for controller in _room_sessions(video_room):
        media = [kind for kind in ('audio', 'video') if controller.hasStreamOfType(kind)]
        if not media:
            continue
        what = ' and '.join(media)
        if not controller.hasStreamOfType('chat'):
            BlinkLogger().log_info('[conference] %s: hanging up the SIP %s, the room has it over WebRTC now' % (controller.remoteAOR, what))
            _silent_media_end(controller)
            controller.end()
        elif controller.state == STATE_CONNECTED:
            BlinkLogger().log_info('[conference] %s: removing SIP %s, the room has it over WebRTC now' % (controller.remoteAOR, what))
            _silent_media_end(controller)
            if 'video' in media:
                controller.removeVideoFromSession()
            if 'audio' in media:
                controller.removeAudioFromSession()
        else:
            BlinkLogger().log_warning('[conference] %s: SIP %s kept, the session is %s' % (controller.remoteAOR, what, controller.state))


def room_chat_session(video_room):
    """A SIP session to the room of video_room that has (or is getting) its chat, or None."""
    return next((controller for controller in _room_sessions(video_room) if controller.hasStreamOfType('chat')), None)


def show_videoroom_window(video_room):
    """Bring the window of a video room in front."""
    from VideoroomWindowController import window_for_session
    window_for_session(video_room).show()


def conference_video_available(account=None):
    """Whether account (by default the default account) can join video rooms: its domain runs SylkServer
    with a wsServer and the media can be done here. The framework is loaded only after the first check."""
    if account is None:
        account = AccountManager().default_account
    if ws_server(account) is None:
        return False
    return media_available()


# Room addresses

def split_uri(uri):
    """(user, host) of a URI given as a string, bytes or SIPURI; host lowercased, without port or parameters."""
    if uri is None:
        return None, None
    if isinstance(uri, bytes):
        uri = uri.decode(errors='replace')
    if not isinstance(uri, str):
        user, host = getattr(uri, 'user', None), getattr(uri, 'host', None)
        user = user.decode(errors='replace') if isinstance(user, bytes) else user
        host = host.decode(errors='replace') if isinstance(host, bytes) else host
        return user, (host or '').lower()
    uri = uri.strip()
    if uri.lower().startswith(('sip:', 'sips:')):
        uri = uri.partition(':')[2]
    user, _, host = uri.partition('@')
    return user, host.partition(';')[0].partition(':')[0].lower()


def parse_room(text):
    """(room, host) of what was typed or pasted: a link https://host/conference/room, sip:room@host,
    room@host; host is None for a bare room name; None when there is nothing."""
    text = (text or '').strip()
    if not text:
        return None
    if re.match(r'^https?://', text, re.IGNORECASE):
        parts = urlsplit(text)
        segments = [segment for segment in parts.path.split('/') if segment]
        if not segments or not parts.hostname:
            return None
        room = segments[segments.index('conference') + 1] if 'conference' in segments[:-1] else segments[-1]
        return unquote(room).strip(), parts.hostname.lower()
    text = re.sub('^sips?:', '', text, flags=re.IGNORECASE)
    room, _, host = text.partition('@')
    host = host.partition(';')[0].partition(':')[0].strip().lower()
    return room.strip(), host or None


def _configuration_domain(host):
    """The domain whose SylkServer configuration covers a room host."""
    host = host.lower()
    for prefix in ('videoconference.', 'conference.'):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


class RoomTarget(object):
    """Where a room of a SylkServer domain is."""

    def __init__(self, room, domain, configuration, account):
        self.room = room
        self.domain = domain
        self.ws_server = configuration.get('wsServer')
        self.ice_servers = configuration.get('iceServers') or []
        video_domain = str(configuration.get('defaultConferenceDomain') or 'videoconference.%s' % domain).lower()
        self.video_uri = '%s@%s' % (room, video_domain)
        sip_domain = 'conference.' + video_domain[len('videoconference.'):] if video_domain.startswith('videoconference.') else 'conference.%s' % domain
        self.sip_uri = '%s@%s' % (room, sip_domain)       # the MSRP switch of the same room
        self.guest_domain = configuration.get('defaultGuestDomain') or 'guest.%s' % domain
        # another server than the account's own: joined there, as ourselves or as a guest
        self.remote = self.ws_server != ws_server(account)

    def __repr__(self):
        return '<RoomTarget %s / %s at %s%s>' % (self.video_uri, self.sip_uri, self.ws_server, ' (remote)' if self.remote else '')


def room_target(room, host, account):
    """The RoomTarget of room at host from the cached configuration of its domain, or None."""
    if not room or not host:
        return None
    domain = _configuration_domain(host)
    configuration = SylkServerDiscovery().configuration(domain)
    if not isinstance(configuration, dict):
        return None
    server = configuration.get('wsServer')
    if not isinstance(server, str) or not server.strip().startswith(('wss://', 'ws://')):
        return None
    return RoomTarget(room, domain, configuration, account)


def local_room_host(host, account):
    """Whether a room host is the account's own conference server (nothing to look up)."""
    if not host or account is None or account is BonjourAccount():
        return True
    host = host.lower()
    own = str(account.conference.server_address or '').lower()
    return host == own or _configuration_domain(host) == account.id.domain.lower()


def room_video(uri, account):
    """(the video room address, its RoomTarget or None) to add video to the room uri (its MSRP switch
    address) from account, from what is known now; None when there is no video for it."""
    user, host = split_uri(uri)
    if not user or not host:
        return None
    if not local_room_host(host, account):
        # a room of another SylkServer: its conference. or videoconference. host (not someone at that domain)
        if not host.lower().startswith(('conference.', 'videoconference.')):
            return None
        target = room_target(user, host, account)
        if target is not None and target.remote:
            return (target.video_uri, target) if media_available() else None
        return None
    if conference_video_available(account):
        return '%s@%s' % (user, host), None
    return None


def resolve_room(room, host, account, callback):
    """callback(RoomTarget or None) once the configuration of host's domain is known: at once when
    cached, else after discovery (in the GUI thread)."""
    target = room_target(room, host, account)
    if target is not None or not room or not host:
        callback(target)
        return
    domain = _configuration_domain(host)
    BlinkLogger().log_info('[webrtc] Looking up the SylkServer of %s for the room %s@%s' % (domain, room, host))
    discovery = SylkServerDiscovery()
    discovery.discover(domain, force=True)
    waited = [0.0]

    def check():
        waited[0] += DISCOVERY_POLL
        if domain in discovery.running and waited[0] < DISCOVERY_TIMEOUT:
            call_later(DISCOVERY_POLL, check)
            return
        result = room_target(room, host, account)
        BlinkLogger().log_info('[webrtc] %s: %s' % (domain, 'SylkServer at %s' % result.ws_server if result is not None else 'no SylkServer found'))
        callback(result)

    call_later(DISCOVERY_POLL, check)


# The Activity log

@implementer(IObserver)
class VideoConferenceAvailability(object, metaclass=Singleton):
    """Says in the Activity log whether video conferences can be joined from the default account,
    when that changes (account, its server, or what is missing)."""

    def __init__(self):
        self._state = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SylkServerConfigurationDidChange')
        notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')
        notification_center.add_observer(self, name='SIPAccountDidActivate')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SylkServerConfigurationDidChange(self, notification):
        self.update()

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        self.update()

    def _NH_SIPAccountDidActivate(self, notification):
        if notification.sender is AccountManager().default_account:
            self.update()

    def update(self):
        account = AccountManager().default_account
        if account is None:
            return
        server = ws_server(account)
        # only once SylkServer is there is the framework loaded, to see whether the media can be done
        missing = tuple(media_missing()) if server is not None else None
        state = (str(account.id), server, missing)
        if state == self._state:
            return
        self._state = state
        logger = BlinkLogger()
        if server is None:
            logger.log_info('[webrtc] Video conferences: %s has no SylkServer WebSocket server, conferences are joined without video' % account.id)
            return
        if missing:
            logger.log_warning('[webrtc] Video conferences for %s through %s are not available: %s' % (account.id, server, ', '.join(missing)))
        else:
            logger.log_info('[webrtc] Video conferences available for %s through %s' % (account.id, server))
        logger.log_info('[webrtc] WebRTC media: %s' % media_report())
