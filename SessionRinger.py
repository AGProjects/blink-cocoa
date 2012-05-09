# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import datetime
import time
from dateutil.tz import tzlocal
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
    on_hold_session_count = 0

    audio_primary_ringtone = None
    audio_secondary_ringtone = None
    initial_hold_tone = None
    secondary_hold_tone = None

    chat_primary_ringtone = None
    chat_secondary_ringtone = None

    chat_message_outgoing_sound = None
    chat_message_incoming_sound = None

    file_transfer_outgoing_sound = None
    file_transfer_incoming_sound = None

    last_hangup_tone_time = 0
    chat_beep_time = 0

    def __init__(self, owner):
        self.owner = owner
        self.started = False

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="BlinkFileTransferDidEnd")
        notification_center.add_observer(self, name="AudioStreamDidChangeHoldState")
        notification_center.add_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.add_observer(self, name="ChatViewControllerDidDisplayMessage")
        notification_center.add_observer(self, name="ConferenceHasAddedAudio")
        notification_center.add_observer(self, name="BlinkWillCancelProposal")

        self.started = True

    def stop(self):
        if not self.started:
            return
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name="BlinkFileTransferDidEnd")
        notification_center.remove_observer(self, name="AudioStreamDidChangeHoldState")
        notification_center.remove_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.remove_observer(self, name="ChatViewControllerDidDisplayMessage")
        notification_center.remove_observer(self, name="ConferenceHasAddedAudio")
        notification_center.remove_observer(self, name="BlinkWillCancelProposal")

    def update_playing_ringtones(self, account=None):
        settings = SIPSimpleSettings()

        should_play_audio_primary_ringtone = False
        should_play_audio_primary_ringtone_tone = False
        should_play_chat_primary_ringtone = False
        should_play_chat_secondary_ringtone = False

        if 'audio' in (stream.type for stream in chain(*(session.streams for session in self.active_sessions if session.streams))):
            # play only secondary ringtones when there's active audio sessions

            if self.incoming_audio_sessions:
                should_play_audio_primary_ringtone_tone = True
            if self.chat_sessions or self.filerecv_sessions or self.ds_sessions:
                should_play_chat_secondary_ringtone = True
        else:
            # play ringtone
            if self.incoming_audio_sessions:
                should_play_audio_primary_ringtone = True
            if self.chat_sessions or self.filerecv_sessions or self.ds_sessions:
                should_play_chat_primary_ringtone = True

        if self.ringing_sessions:
            # proposal for adding new streams to an ongoing session
            proposed_stream_types = [stream.type for stream in chain(*(session.proposed_streams or [] for session in self.ringing_sessions))]
            has_chat_proposed = 'chat' in proposed_stream_types
            has_ds_proposed = 'desktop-sharing' in proposed_stream_types
            has_file_proposed = 'file-transfer' in proposed_stream_types
            has_audio_ongoing = 'audio' in (stream.type for stream in chain(*(session.streams for session in self.ringing_sessions if session.streams)))
            should_play_chat_secondary_ringtone = (has_chat_proposed or has_file_proposed or has_ds_proposed) and not has_audio_ongoing

        if self.audio_primary_ringtone:
            if should_play_audio_primary_ringtone and not self.audio_primary_ringtone.is_active:
                self.update_ringtones(account)

                this_hour = int(datetime.datetime.now(tzlocal()).strftime("%H"))
                volume = None
                if settings.sounds.night_volume.start_hour < settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour and this_hour >= settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume
                elif settings.sounds.night_volume.start_hour > settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour:
                        volume = settings.sounds.night_volume.volume
                    elif this_hour >=  settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume

                if volume is None:
                    if account is not None:
                        if hasattr(account.sounds.audio_inbound.sound_file, 'volume'):
                            volume = account.sounds.audio_inbound.sound_file.volume
                        else:
                            volume = settings.sounds.audio_inbound.volume
                    else:
                        volume = settings.sounds.audio_inbound.volume

                self.audio_primary_ringtone.volume = volume
                self.audio_primary_ringtone.start()
            elif not should_play_audio_primary_ringtone and self.audio_primary_ringtone.is_active:
                self.audio_primary_ringtone.stop()

        if self.audio_secondary_ringtone:
            if should_play_audio_primary_ringtone_tone and not self.audio_secondary_ringtone.is_active:
                self.audio_secondary_ringtone.start()
            elif not should_play_audio_primary_ringtone_tone and self.audio_secondary_ringtone.is_active:
                self.audio_secondary_ringtone.stop()

        if self.chat_primary_ringtone:
            if should_play_chat_primary_ringtone and not self.chat_primary_ringtone.is_active:
                self.chat_primary_ringtone.start()
            elif not should_play_chat_primary_ringtone and self.chat_primary_ringtone.is_active:
                self.chat_primary_ringtone.stop()

        if self.chat_secondary_ringtone:
            if should_play_chat_secondary_ringtone and not self.chat_secondary_ringtone.is_active:
                self.chat_secondary_ringtone.start()
            elif not should_play_chat_secondary_ringtone and self.chat_secondary_ringtone.is_active:
                self.chat_secondary_ringtone.stop()

    def update_ringtones(self, account=None):
        settings = SIPSimpleSettings()

        if account is None:
            account = AccountManager().default_account

        app = SIPApplication()

        def change_tone(name, new_tone):
            current = getattr(self, name)
            if current and current.is_active:
                current.stop()
                if new_tone:
                    new_tone.start()
            setattr(self, name, new_tone)

        change_tone("initial_hold_tone", WavePlayer(app.voice_audio_mixer, Resources.get('hold_tone.wav'), volume=10))
        app.voice_audio_bridge.add(self.initial_hold_tone)
        change_tone("secondary_hold_tone", WavePlayer(app.voice_audio_mixer, Resources.get('hold_tone.wav'), loop_count=0, pause_time=45, volume=10, initial_play=False))
        app.voice_audio_bridge.add(self.secondary_hold_tone)

        if account:
            audio_primary_ringtone = account.sounds.audio_inbound.sound_file if account.sounds.audio_inbound is not None else None
        else:
            audio_primary_ringtone = None

        if audio_primary_ringtone and not settings.audio.silent:
            # Workaround not to use same device from two bridges. -Saul
            if settings.audio.alert_device is not None and app.alert_audio_mixer.real_output_device == app.voice_audio_mixer.real_output_device:
                new_tone = WavePlayer(app.voice_audio_mixer, audio_primary_ringtone.path, loop_count=0, pause_time=6)
                app.voice_audio_bridge.add(new_tone)
            else:
                new_tone = WavePlayer(app.alert_audio_mixer, audio_primary_ringtone.path, loop_count=0, pause_time=6)
                app.alert_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("audio_primary_ringtone", new_tone)

        if audio_primary_ringtone and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("audio_secondary_ringtone", new_tone)

        if audio_primary_ringtone and not settings.audio.silent:
            # Workaround not to use same device from two bridges. -Saul
            if settings.audio.alert_device is not None and app.alert_audio_mixer.real_output_device == app.voice_audio_mixer.real_output_device:
                new_tone = WavePlayer(app.voice_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6)
                app.voice_audio_bridge.add(new_tone)
            else:
                new_tone = WavePlayer(app.alert_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6)
                app.alert_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("chat_primary_ringtone", new_tone)

        if audio_primary_ringtone and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, Resources.get('ring_tone.wav'), loop_count=0, pause_time=6)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("chat_secondary_ringtone", new_tone)

        chat_message_outgoing_sound = settings.sounds.message_sent
        if chat_message_outgoing_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, chat_message_outgoing_sound.path, volume=chat_message_outgoing_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("chat_message_outgoing_sound", new_tone)

        chat_message_incoming_sound = settings.sounds.message_received
        if chat_message_incoming_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, chat_message_incoming_sound.path, volume=chat_message_incoming_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("chat_message_incoming_sound", new_tone)

        file_transfer_outgoing_sound = settings.sounds.file_sent
        if file_transfer_outgoing_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, file_transfer_outgoing_sound.path, volume=file_transfer_outgoing_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("file_transfer_outgoing_sound", new_tone)

        file_transfer_incoming_sound = settings.sounds.file_received
        if file_transfer_incoming_sound and not settings.audio.silent:
            new_tone = WavePlayer(app.voice_audio_mixer, file_transfer_incoming_sound.path, volume=file_transfer_incoming_sound.volume)
            app.voice_audio_bridge.add(new_tone)
        else:
            new_tone = None
        change_tone("file_transfer_incoming_sound", new_tone)

    def add_incoming(self, session, streams):
        settings = SIPSimpleSettings()
        stream_types = [s.type for s in streams]
        if 'audio' in stream_types and not settings.audio.silent:
            self.incoming_audio_sessions[session] = streams
        else:
            if 'chat' in stream_types:
                self.chat_sessions[session] = streams
            if 'file-transfer' in stream_types:
                self.filerecv_sessions[session] = streams
        NotificationCenter().add_observer(self, sender=session)
        self.update_playing_ringtones(session.account)

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
        settings = SIPSimpleSettings()

        if settings.audio.silent:
            return

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
        elif name in ("SIPSessionWillEnd", "SIPSessionDidEnd", "SIPSessionDidFail"):
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
        elif name in ("SIPSessionGotAcceptProposal", "SIPSessionGotRejectProposal", "SIPSessionHadProposalFailure", "BlinkWillCancelProposal"):
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
            self.update_playing_ringtones(session.account)
        elif name == "SIPSessionDidRenegotiateStreams":
            if data.action == "remove":
                for stream in (s for s in data.streams if s.type=='audio'):
                    self.play_hangup()
        elif name == "SIPSessionGotRingIndication":
            self.ringing_sessions.add(session)
            self.update_playing_ringtones()
        elif name == "AudioStreamDidChangeHoldState":
            if not settings.audio.silent:
                if data.on_hold:
                    self.on_hold_session_count += 1
                else:
                    self.on_hold_session_count -= 1
                if self.secondary_hold_tone:
                    if self.on_hold_session_count == 1:
                        self.secondary_hold_tone.start()
                    elif self.on_hold_session_count == 0:
                        self.secondary_hold_tone.stop()
                if data.on_hold and data.originator == 'remote' and self.initial_hold_tone and not self.initial_hold_tone.is_active:
                    self.initial_hold_tone.start()
        elif name == "ConferenceHasAddedAudio":
            # TODO: play-resume iTunes after adding audio to conference tone has been played -adi
            self.initial_hold_tone.start()
        elif name == "ChatViewControllerDidDisplayMessage":
            if not settings.audio.silent:
                now = time.time()
                if self.chat_message_outgoing_sound and now - self.chat_beep_time > CHAT_TONE_THROTLE_DELAY and not data.history_entry:
                    if data.direction == 'outgoing':
                        self.chat_message_outgoing_sound.stop()
                        self.chat_message_outgoing_sound.start()
                    else:
                        self.chat_message_incoming_sound.stop()
                        self.chat_message_incoming_sound.start()
                    self.chat_beep_time = now
        elif name == "BlinkFileTransferDidEnd":
            if not settings.audio.silent:
                if data == "send":
                    if self.file_transfer_outgoing_sound:
                        self.file_transfer_outgoing_sound.start()
                else:
                    if self.file_transfer_incoming_sound:
                        self.file_transfer_incoming_sound.start()
        elif name == "CFGSettingsObjectDidChange":
            sound_attributes = ['audio.silent', 'audio.alert_device', 'audio.output_device', 'sounds.audio_inbound', 'sounds.message_sent', 'sounds.message_received', 'sounds.file_sent', 'sounds.file_received']
            if set(sound_attributes).intersection(data.modified):
                self.update_ringtones()
            if 'audio.silent' in data.modified:
                self.update_playing_ringtones()
        elif name == "WavePlayerDidEnd":
            NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")

        on_hold_streams = [stream for stream in chain(*(session.streams for session in self.active_sessions if session.streams)) if stream.on_hold]
        if not on_hold_streams and self.secondary_hold_tone is not None and self.secondary_hold_tone.is_active:
            self.secondary_hold_tone.stop()


