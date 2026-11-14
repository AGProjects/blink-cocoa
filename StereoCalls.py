# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Stereo audio calls (Preferences, Audio Calls: Enable Stereo Mode; the contact menus' Start Stereo Audio Call).

A stereo call needs the SDK in stereo (python3-sipsimple 5.7, audio.stereo): a stereo mixer, Opus in stereo, and no
echo canceller (it is mono and made for speech). The SDK is switched to stereo for the call and back to mono when no
stereo call is left, and only if it was switched here.

Outgoing: the call's audio stream is AudioStream(stereo=True), which offers only the codecs that carry stereo (Opus).
The session starts after the stereo mixer is made (MIXER_DELAY).

Incoming: an offer asking for stereo Opus (AudioStream.offered_stereo) switches the SDK to stereo while the call
rings, so that it is answered in stereo; the stream moves to the stereo mixer when it is accepted (the SDK). Only
when no other audio call is in progress: making the mixer again under a call in progress would take its audio away.
"""

__all__ = ['StereoCalls', 'stereo_available', 'stereo_sdk_available']

from application.notification import IObserver, NotificationCenter
from application.python.types import Singleton
from sipsimple.configuration.settings import AudioSettings, SIPSimpleSettings
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from util import call_later, run_in_gui_thread


def stereo_sdk_available():
    """Whether the SDK has stereo audio (python3-sipsimple 5.7: audio.stereo)."""
    return 'stereo' in dir(AudioSettings)


def stereo_available():
    """Whether stereo calls are offered: enabled in the preferences, and an SDK that has stereo audio."""
    return stereo_sdk_available() and bool(getattr(SIPSimpleSettings().audio, 'enable_stereo', False))


@implementer(IObserver)
class StereoCalls(object, metaclass=Singleton):
    """Starts and answers stereo calls and puts the SDK back in mono after the last one."""

    MIXER_DELAY = 0.6       # s: the session made after the stereo mixer

    def __init__(self):
        self._calls = {}            # the stereo calls (SessionControllers going out, sipsimple Sessions coming in): their notifications
        self._engaged = False       # the SDK put in stereo here (to be put back)

    def _engage(self):
        """The SDK in stereo; the delay before a stream is made on the new mixer."""
        settings = SIPSimpleSettings()
        if settings.audio.stereo:
            return 0
        settings.audio.stereo = True
        settings.save()
        self._engaged = True
        BlinkLogger().log_info('Stereo call: the audio in stereo (Opus stereo, no echo cancellation)')
        return self.MIXER_DELAY

    def _add(self, call, *names):
        if call in self._calls:
            return
        self._calls[call] = names
        notification_center = NotificationCenter()
        for name in names:
            notification_center.add_observer(self, sender=call, name=name)

    # Outgoing

    def start(self, session_controller, stream_types=(('audio', {'stereo': True}),)):
        """A stereo call with a SessionController (startSessionWithTarget or joinConference, stereo=True); stream_types
        as startCompositeSessionWithStreamsOfTypes takes them, the audio as ('audio', {'stereo': True})."""
        types = ', '.join((stype[0] if isinstance(stype, tuple) else stype) for stype in stream_types)
        BlinkLogger().log_info('Stereo call requested to %s (%s): offering Opus only, with stereo=1; sprop-stereo=1' % (session_controller.target_uri, types))
        delay = self._engage()
        self._add(session_controller, 'BlinkSessionDidEnd', 'BlinkSessionDidFail', 'BlinkSessionCancelledBeforeDNSLookup')

        def call():
            try:
                started = session_controller.startCompositeSessionWithStreamsOfTypes(stream_types)
            except Exception as e:
                BlinkLogger().log_error('Failed to start stereo audio session: %s' % e)
                self._ended(session_controller)
                raise
            if not started:
                BlinkLogger().log_error('Failed to start stereo audio session')
                self._ended(session_controller)
        if delay:
            call_later(delay, call)
        else:
            call()
        return True

    # Incoming

    def incoming(self, session, streams):
        """A new incoming session: answered in stereo if it asks for stereo Opus and stereo is enabled."""
        if not stereo_available():
            return
        audio = next((stream for stream in streams if stream.type == 'audio'), None)
        if audio is None or not getattr(audio, 'offered_stereo', False):
            return
        from SessionController import SessionControllersManager
        from MediaStream import STATE_CONNECTED, STATE_CONNECTING, STATE_DNS_LOOKUP
        others = [controller for controller in SessionControllersManager().sessionControllers
                  if controller.session is not session and controller.hasStreamOfType('audio')
                  and controller.state in (STATE_CONNECTED, STATE_CONNECTING, STATE_DNS_LOOKUP)]
        if others and not SIPSimpleSettings().audio.stereo:
            BlinkLogger().log_info('Stereo offer answered in mono: another audio call is in progress')
            return
        BlinkLogger().log_info('Incoming stereo offer: answered in stereo')
        self._engage()
        self._add(session, 'SIPSessionDidEnd', 'SIPSessionDidFail')

    # Ending

    @run_in_gui_thread
    def handle_notification(self, notification):
        self._ended(notification.sender)

    def _ended(self, call):
        if call not in self._calls:
            return
        notification_center = NotificationCenter()
        for name in self._calls.pop(call):
            notification_center.discard_observer(self, sender=call, name=name)
        if self._calls or not self._engaged:
            return
        self._engaged = False
        from CuteMusic import selected
        if selected():      # AG CuteMusic's mixer as the input: its music mode keeps stereo
            return
        settings = SIPSimpleSettings()
        if settings.audio.stereo:
            settings.audio.stereo = False
            settings.save()
            BlinkLogger().log_info('No stereo call left: the audio in mono again')
