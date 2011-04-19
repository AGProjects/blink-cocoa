# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import time
from itertools import chain

from zope.interface import implements
from application.notification import NotificationCenter, IObserver

from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import AccountManager

from sipsimple.audio import WavePlayer
from sipsimple.application import SIPApplication

from resources import Resources
from util import allocate_autorelease_pool


HANGUP_TONE_THROTLE_DELAY = 2.0
CHAT_TONE_THROTLE_DELAY = 3.0


class Ringer(object):
    implements(IObserver)

    """
    Manages ringtone playing for incoming sessions.

    - updates ringtone when settings or default account change
    - plays ringtone when an incoming session arrives
    - plays ringtone when an outgoing audio session gets SIPSessionGotRingIndication
    - selects between user ringtone and "discrete", tone based ringtone according to active sessions
    - stops playing ringtones when appropriate
    - "beeps" when call ends
    """
    owner = None
    incoming_audio_sessions = {}
    ringing_sessions = set()
    chat_sessions = {}
    ds_sessions = {}
    filesend_sessions = {}
    filerecv_sessions = {}
    active_sessions = set()
    inbound_ringtone = None
    tone_ringtone = None
    initial_hold_tone = None
    hold_tone = None
    chat_ringtone = None
    tone_chat_ringtone = None
    on_hold_session_count = 0

    msg_out_sound = None
    msg_in_sound = None

    file_out_sound = None
    file_in_sound = None

    last_hangup_tone_time = 0
    chat_beep_time = 0

    def __init__(self, owner):
        self.owner = owner

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="BlinkFileTransferDidEnd")
        notification_center.add_observer(self, name="AudioStreamDidChangeHoldState")
        notification_center.add_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.add_observer(self, name="ChatViewControllerDidDisplayMessage")

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name="BlinkFileTransferDidEnd")
        notification_center.remove_observer(self, name="AudioStreamDidChangeHoldState")
        notification_center.remove_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.remove_observer(self, name="ChatViewControllerDidDisplayMessage")

    def update_playing_ringtones(self):
        should_play_incoming = False
        should_play_incoming_tone = False
        should_play_chat = False
        should_play_chat_tone = False

        if 'audio' in (stream.type for stream in chain(*(session.streams for session in self.active_sessions if session.streams))):
            # play only secondary ringtones when there's active audio sessions
            if self.incoming_audio_sessions:
                should_play_incoming_tone = True
            if self.chat_sessions or self.filerecv_sessions or self.ds_sessions:
                should_play_chat_tone = True
        else:
            # play ringtone
            if self.incoming_audio_sessions:
                should_play_incoming = True
            if self.chat_sessions or self.filerecv_sessions or self.ds_sessions:
                should_play_chat = True

        if self.ringing_sessions:
            # proposal for adding new streams to an ongoing session
            proposed_stream_types = [stream.type for stream in chain(*(session.proposed_streams or [] for session in self.ringing_sessions))]
            has_chat_proposed = 'chat' in proposed_stream_types
            has_ds_proposed = 'desktop-sharing' in proposed_stream_types
            has_file_proposed = 'file-transfer' in proposed_stream_types
            has_audio_ongoing = 'audio' in (stream.type for stream in chain(*(session.streams for session in self.ringing_sessions if session.streams)))
            should_play_chat_tone = (has_chat_proposed or has_file_proposed or has_ds_proposed) and not has_audio_ongoing

        if self.inbound_ringtone:
            if should_play_incoming and not self.inbound_ringtone.is_active:
                self.inbound_ringtone.start()
            elif not should_play_incoming and self.inbound_ringtone.is_active:
                self.inbound_ringtone.stop()

        if self.tone_ringtone:
            if should_play_incoming_tone and not self.tone_ringtone.is_active:
                self.tone_ringtone.start()
            elif not should_play_incoming_tone and self.tone_ringtone.is_active:
                self.tone_ringtone.stop()

        if self.chat_ringtone:
            if should_play_chat and not self.chat_ringtone.is_active:
                self.chat_ringtone.start()
            elif not should_play_chat and self.chat_ringtone.is_active:
                self.chat_ringtone.stop()

        if self.tone_chat_ringtone:
            if should_play_chat_tone and not self.tone_chat_ringtone.is_active:
                self.tone_chat_ringtone.start()
            elif not should_play_chat_tone and self.tone_chat_ringtone.is_active:
                self.tone_chat_ringtone.stop()

    def update_ringtones(self):
        account = AccountManager().default_account
        settings = SIPSimpleSettings()
        app = SIPApplication()

        def change_tone(name, new_tone):
            current = getattr(self, name)
            if current:
                if current.is_active:
                    current.stop()
                    if new_tone:
                        new_tone.start()
            setattr(self, name, new_tone)

        change_tone("initial_hold_tone", WavePlayer(app.voice_audio_mixer, Resources.get('hold_tone.wav'), volume=10))
        app.voice_audio_bridge.add(self.initial_hold_tone)
        change_tone("hold_tone", WavePlayer(app.voice_audio_mixer, Resources.get('hold_tone.wav'), loop_count=0, pause_time=45, volume=10, initial_play=False))
        app.voice_audio_bridge.add(self.hold_tone)

        if account:
            inbound_ringtone = account.sounds.audio_inbound.sound_file if account.sounds.audio_inbound is not None else None
        else:
            inbound_ringtone = None

        if inbound_ringtone and not settings.audio.silent:
            # Workaround not to use same device from two bridges. -Saul
            if settings.audio.alert_device is not None and app.alert_audio_mixer.real_output_device == app.voice_audio_mixer.real_output_device:
                new_tone = WavePlayer(app.voice_audio_mixer, inbound_ringtone.path, volume=inbound_ringtone.volume, loop_count=0, pause_time=6)
                app.voice_audio_bridge.add(new_tone)
            else:
                new_tone = WavePlayer(app.alert_audio_mixer, inbound_ringtone.path, volume=inbound_ringtone.volume, loop_count=0, pause_time=6)
                app.alert_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("inbound_ringtone", new_tone)

        if inbound_ringtone and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6, volume=inbound_ringtone.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("tone_ringtone", new_tone)

        if inbound_ringtone and not settings.audio.silent:
            # Workaround not to use same device from two bridges. -Saul
            if settings.audio.alert_device is not None and app.alert_audio_mixer.real_output_device == app.voice_audio_mixer.real_output_device:
                new_tone = WavePlayer(app.voice_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6, volume=inbound_ringtone.volume)
                app.voice_audio_bridge.add(new_tone)
            else:
                new_tone = WavePlayer(app.alert_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6, volume=inbound_ringtone.volume)
                app.alert_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("chat_ringtone", new_tone)

        if inbound_ringtone and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6, volume=inbound_ringtone.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("tone_chat_ringtone", new_tone)

        msg_out_sound = settings.sounds.message_sent
        if msg_out_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, msg_out_sound.path, volume=msg_out_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("msg_out_sound", new_tone)

        msg_in_sound = settings.sounds.message_received
        if msg_in_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, msg_in_sound.path, volume=msg_in_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("msg_in_sound", new_tone)

        file_out_sound = settings.sounds.file_sent
        if file_out_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, file_out_sound.path, volume=file_out_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("file_out_sound", new_tone)

        file_in_sound = settings.sounds.file_received
        if file_in_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, file_in_sound.path, volume=file_in_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("file_in_sound", new_tone)

    def add_incoming(self, session, streams):
        stream_types = [s.type for s in streams]
        if 'audio' in stream_types:
            self.incoming_audio_sessions[session] = streams
        else:
            if 'chat' in stream_types:
                self.chat_sessions[session] = streams
            if 'file-transfer' in stream_types:
                self.filerecv_sessions[session] = streams
        NotificationCenter().add_observer(self, sender=session)
        self.update_playing_ringtones()

    def add_outgoing(self, session, streams):
        NotificationCenter().add_observer(self, sender=session)


    def stop_ringing(self, session):
        self.ringing_sessions.discard(session)
        self.incoming_audio_sessions.pop(session, None)
        self.chat_sessions.pop(session, None)
        self.ds_sessions.pop(session, None)
        self.filerecv_sessions.pop(session, None)
        self.update_playing_ringtones()

    def play_hangup(self):
        if time.time() - self.last_hangup_tone_time > HANGUP_TONE_THROTLE_DELAY:
            hangup_tone = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get('hangup_tone.wav'), volume=30)
            NotificationCenter().add_observer(self, sender=hangup_tone, name="WavePlayerDidEnd")
            SIPApplication.voice_audio_bridge.add(hangup_tone)
            hangup_tone.start()
            self.last_hangup_tone_time = time.time()

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        session = notification.sender
        name = notification.name
        data = notification.data
        settings = SIPSimpleSettings()

        if name == "SIPSessionWillStart":
            self.active_sessions.add(session)
            self.stop_ringing(session)
            #if session.direction == "incoming":
            #    if self.inbound_ringtone:
            #        self.inbound_ringtone.stop()
            #    if len(self.incoming_audio_sessions) <= 1 and self.tone_ringtone:
            #        self.tone_ringtone.stop()
        elif name == "SIPSessionDidStart":
            pass
        elif name in ("SIPSessionWillEnd", "SIPSessionDidFail"):
            streams = session.streams or session.proposed_streams
            if not streams:
                # not connected
                has_audio = session in self.incoming_audio_sessions
            else:
                stream_types = [s.type for s in streams]
                has_audio = 'audio' in stream_types
            # play hangup tone
            if has_audio:
                self.play_hangup()
            NotificationCenter().remove_observer(self, sender=session)
            self.active_sessions.discard(session)
            self.stop_ringing(session)
        elif name in ("SIPSessionGotAcceptProposal", "SIPSessionGotRejectProposal", "SIPSessionHadProposalFailure"):
            self.stop_ringing(session)
        elif name == "SIPSessionGotProposal":
            stream_types = [stream.type for stream in data.streams]
            if 'audio' in stream_types:
                if data.originator == "local":
                    self.ringing_sessions.add(session)
                else:
                    self.incoming_audio_sessions[session] = data.streams
            elif 'chat' in stream_types:
                self.chat_sessions[session] = data.streams
            elif 'desktop-sharing' in stream_types:
                self.ds_sessions[session] = data.streams
            elif 'file-transfer' in stream_types:
                self.filerecv_sessions[session] = data.streams
            self.update_playing_ringtones()
        elif name == "SIPSessionDidRenegotiateStreams":
            if data.action == "remove":
                for stream in (s for s in data.streams if s.type=='audio'):
                    self.play_hangup()
        elif name == "SIPSessionGotRingIndication":
            self.ringing_sessions.add(session)
            self.update_playing_ringtones()
        elif name == "AudioStreamDidChangeHoldState":
            if data.on_hold:
                self.on_hold_session_count += 1
            else:
                self.on_hold_session_count -= 1
            if self.hold_tone:
                if self.on_hold_session_count == 1:
                    self.hold_tone.start()
                elif self.on_hold_session_count == 0:
                    self.hold_tone.stop()
            if data.on_hold and data.originator == 'remote' and self.initial_hold_tone and not self.initial_hold_tone.is_active:
                self.initial_hold_tone.start()
        elif name == "ChatViewControllerDidDisplayMessage":
            now = time.time()
            if not settings.audio.silent and self.msg_out_sound and now - self.chat_beep_time > CHAT_TONE_THROTLE_DELAY and not data.history_entry:
                if data.direction == 'outgoing':
                    self.msg_out_sound.stop()
                    self.msg_out_sound.start()
                else:
                    self.msg_in_sound.stop()
                    self.msg_in_sound.start()
                self.chat_beep_time = now
        elif name == "BlinkFileTransferDidEnd":
            if not settings.audio.silent:
                if data == "send":
                    if self.file_out_sound:
                        self.file_out_sound.start()
                else:
                    if self.file_in_sound:
                        self.file_in_sound.start()
        elif name == "CFGSettingsObjectDidChange":
            sound_attributes = ['audio.silent', 'audio.alert_device', 'audio.output_device', 'sounds.audio_inbound', 'sounds.message_sent', 'sounds.message_received', 'sounds.file_sent', 'sounds.file_received']
            if set(sound_attributes).intersection(data.modified):
                self.update_ringtones()
            if 'audio.silent' in data.modified:
                self.update_playing_ringtones()
        elif name == "WavePlayerDidEnd":
            NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        on_hold_streams = [stream for stream in chain(*(session.streams for session in self.active_sessions if session.streams)) if stream.on_hold]
        if not on_hold_streams and self.hold_tone is not None and self.hold_tone.is_active:
            self.hold_tone.stop()


