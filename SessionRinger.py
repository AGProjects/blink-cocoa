# Copyright (C) 2009-2013 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSEventTrackingRunLoopMode)

from Foundation import (NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer)

import datetime
import time
from dateutil.tz import tzlocal
from itertools import chain

from zope.interface import implementer
from application.notification import NotificationCenter, IObserver
from application.python import Null

from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import AccountManager

from sipsimple.audio import WavePlayer
from sipsimple.application import SIPApplication

from resources import Resources
from util import run_in_gui_thread

from BlinkLogger import BlinkLogger

HANGUP_TONE_THROTLE_DELAY = 2.0
CHAT_TONE_THROTLE_DELAY = 3.0



@implementer(IObserver)
class Ringer(object):

    """
    Manages ringtone playing for incoming sessions.

    - updates ringtone when settings or default account change
    - plays ringtone when an incoming session arrives
    - plays ringtone when an outgoing audio calls gets SIPSessionGotRingIndication
    - selects between user ringtone and "discrete", tone based ringtone according to active sessions
    - stops playing ringtones when appropriate
    - "beeps" when call ends
    """
    owner = None
    incoming_audio_sessions = {}
    outgoing_ringing_sessions = set()
    chat_sessions = {}
    video_sessions = {}
    ds_sessions = {}
    filesend_sessions = {}
    filerecv_sessions = {}
    active_sessions = set()
    on_hold_audio_sessions = set()

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

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    def __init__(self, owner):
        BlinkLogger().log_debug('Starting Ringtone Manager')
        self.owner = owner
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="BlinkFileTransferDidEnd")
        notification_center.add_observer(self, name="RTPStreamDidChangeHoldState")
        notification_center.add_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.add_observer(self, name="ChatViewControllerDidDisplayMessage")
        notification_center.add_observer(self, name="ConferenceHasAddedAudio")
        notification_center.add_observer(self, name="BlinkWillCancelProposal")

        self.cleanupTimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(3, self, "cleanupTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.cleanupTimer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.cleanupTimer, NSEventTrackingRunLoopMode)
        self.update_ringtones()

    def cleanupTimer_(self, timer):
        # Some sessions can remain hanging indefintely due to illegal state errors, this timer will purge them
        outgoing_sessions = list(sessionController.session for sessionController in self.sessionControllersManager.sessionControllers if sessionController.session is not None and sessionController.session.direction == 'outgoing')
        incoming_sessions = list(sessionController.session for sessionController in self.sessionControllersManager.sessionControllers if sessionController.session is not None and sessionController.session.direction == 'incoming')

        for session in list(self.incoming_audio_sessions.keys()):
            if session not in incoming_sessions:
                self.handle_session_end(session)

        for session in self.outgoing_ringing_sessions.copy():
            if session not in outgoing_sessions:
                self.handle_session_end(session)


    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name="BlinkFileTransferDidEnd")
        notification_center.remove_observer(self, name="RTPStreamDidChangeHoldState")
        notification_center.remove_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.remove_observer(self, name="ChatViewControllerDidDisplayMessage")
        notification_center.remove_observer(self, name="ConferenceHasAddedAudio")
        notification_center.remove_observer(self, name="BlinkWillCancelProposal")
        self.cleanupTimer.invalidate()
        self.cleanupTimer = None

    def update_playing_ringtones(self, account=None):
        settings = SIPSimpleSettings()

        should_play_audio_primary_ringtone = False
        should_play_audio_secondary_ringtone = False
        should_play_chat_primary_ringtone = False
        should_play_chat_secondary_ringtone = False

        if 'audio' in (stream.type for stream in chain(*(session.streams for session in self.active_sessions if session.streams))):
            # play only secondary ringtones when there's active audio calls

            if self.incoming_audio_sessions:
                should_play_audio_secondary_ringtone = True
            if self.chat_sessions or self.video_sessions or self.filerecv_sessions or self.ds_sessions:
                should_play_chat_secondary_ringtone = True
        else:
            # play ringtone
            if self.incoming_audio_sessions:
                should_play_audio_primary_ringtone = True
            if self.chat_sessions or self.video_sessions or self.filerecv_sessions or self.ds_sessions:
                should_play_chat_primary_ringtone = True

        if self.outgoing_ringing_sessions:
            # proposal for adding new streams to an ongoing session
            proposed_stream_types = [stream.type for stream in chain(*(session.proposed_streams or [] for session in self.outgoing_ringing_sessions))]
            has_chat_proposed = 'chat' in proposed_stream_types
            has_ds_proposed = 'screen-sharing' in proposed_stream_types
            has_file_proposed = 'file-transfer' in proposed_stream_types
            has_audio_ongoing = 'audio' in (stream.type for stream in chain(*(session.streams for session in self.outgoing_ringing_sessions if session.streams)))
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
            if should_play_audio_secondary_ringtone and not self.audio_secondary_ringtone.is_active:
                self.audio_secondary_ringtone.start()
            elif not should_play_audio_secondary_ringtone and self.audio_secondary_ringtone.is_active:
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
        change_tone("secondary_hold_tone", WavePlayer(app.voice_audio_mixer, Resources.get('hold_tone.wav'), loop_count=0, pause_time=45, volume=10, initial_delay=45))
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
            if 'video' in stream_types:
                self.video_sessions[session] = streams
            if 'file-transfer' in stream_types:
                self.filerecv_sessions[session] = streams
        NotificationCenter().add_observer(self, sender=session)
        self.update_playing_ringtones(session.account)

    def add_outgoing(self, session, streams):
        NotificationCenter().add_observer(self, sender=session)

    def stop_ringing(self, session):
        self.outgoing_ringing_sessions.discard(session)
        self.incoming_audio_sessions.pop(session, None)
        self.chat_sessions.pop(session, None)
        self.video_sessions.pop(session, None)
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

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

        session = notification.sender
        on_hold_streams = [stream for stream in chain(*(session.streams for session in self.active_sessions if session.streams)) if stream.on_hold]
        if not on_hold_streams and self.secondary_hold_tone is not None and self.secondary_hold_tone.is_active:
            self.secondary_hold_tone.stop()

    def _NH_SIPSessionWillStart(self, notification):
        session = notification.sender
        self.active_sessions.add(session)
        self.stop_ringing(session)

    def _NH_SIPSessionProposalAccepted(self, notification):
        session = notification.sender
        self.stop_ringing(session)

    def _NH_SIPSessionProposalRejected(self, notification):
        session = notification.sender
        self.stop_ringing(session)

    def _NH_SIPSessionHadProposalFailure(self, notification):
        session = notification.sender
        self.stop_ringing(session)

    def _NH_BlinkProposalDidFail(self, notification):
        session = notification.sender.session
        self.stop_ringing(session)

    def _NH_BlinkWillCancelProposal(self, notification):
        session = notification.sender
        self.stop_ringing(session)

    def _NH_SIPSessionWillEnd(self, notification):
        self.handle_session_end(notification.sender)

    def _NH_SIPSessionDidEnd(self, notification):
        self.handle_session_end(notification.sender)

    def _NH_SIPSessionDidFail(self, notification):
        self.handle_session_end(notification.sender)

    def handle_session_end(self, session):
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

        self.on_hold_audio_sessions.discard(session)
        if self.secondary_hold_tone and len(self.on_hold_audio_sessions) == 0:
            self.secondary_hold_tone.stop()

    def _NH_SIPSessionNewProposal(self, notification):
        session = notification.sender
        data = notification.data
        stream_types = [stream.type for stream in data.proposed_streams]
        if 'audio' in stream_types:
            if data.originator == "local":
                self.outgoing_ringing_sessions.add(session)
            else:
                self.incoming_audio_sessions[session] = data.proposed_streams
        elif 'chat' in stream_types:
            self.chat_sessions[session] = data.proposed_streams
        elif 'video' in stream_types:
            self.chat_sessions[session] = data.proposed_streams
        elif 'screen-sharing' in stream_types:
            self.ds_sessions[session] = data.proposed_streams
        elif 'file-transfer' in stream_types:
            self.filerecv_sessions[session] = data.proposed_streams
        self.update_playing_ringtones(session.account)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        data = notification.data
        for stream in (s for s in data.removed_streams if s.type=='audio'):
            self.play_hangup()

    def _NH_SIPSessionGotRingIndication(self, notification):
        session = notification.sender
        self.outgoing_ringing_sessions.add(session)
        self.update_playing_ringtones()

    def _NH_RTPStreamDidChangeHoldState(self, notification):
        data = notification.data
        settings = SIPSimpleSettings()
        session = notification.sender.session

        if data.on_hold:
            self.on_hold_audio_sessions.add(session)
        else:
            self.on_hold_audio_sessions.discard(session)

        if not settings.audio.silent:
            if self.secondary_hold_tone:
                if len(self.on_hold_audio_sessions) == 1:
                    self.secondary_hold_tone.start()
                elif len(self.on_hold_audio_sessions) == 0:
                    self.secondary_hold_tone.stop()

            if data.on_hold and data.originator == 'remote' and self.initial_hold_tone and not self.initial_hold_tone.is_active:
                self.initial_hold_tone.start()

    def _NH_ConferenceHasAddedAudio(self, notification):
        # TODO: play-resume iTunes after adding audio to conference tone has been played -adi
        self.initial_hold_tone.start()

    def _NH_ChatViewControllerDidDisplayMessage(self, notification):
        data = notification.data
        settings = SIPSimpleSettings()
        if not settings.audio.silent:
            now = time.time()
            if now - self.chat_beep_time > CHAT_TONE_THROTLE_DELAY and not data.history_entry:
                if data.direction == 'outgoing' and self.chat_message_outgoing_sound:
                    self.chat_message_outgoing_sound.stop()
                    self.chat_message_outgoing_sound.start()
                elif self.chat_message_incoming_sound:
                    self.chat_message_incoming_sound.stop()
                    self.chat_message_incoming_sound.start()
                self.chat_beep_time = now

    def _NH_BlinkFileTransferDidEnd(self, notification):
        settings = SIPSimpleSettings()
        if not settings.audio.silent and not notification.data.error:
            if notification.sender.direction == 'incoming':
                if self.file_transfer_incoming_sound:
                    self.file_transfer_incoming_sound.start()
            else:
                if self.file_transfer_outgoing_sound:
                    self.file_transfer_outgoing_sound.start()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        data = notification.data
        sound_attributes = ['audio.silent',
                            'audio.alert_device',
                            'audio.output_device',
                            'sounds.audio_inbound',
                            'sounds.message_sent',
                            'sounds.message_received',
                            'sounds.file_sent',
                            'sounds.file_received']
        if set(sound_attributes).intersection(data.modified):
            self.update_ringtones()
        if 'audio.silent' in data.modified:
            self.update_playing_ringtones()

    def _NH_WavePlayerDidEnd(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")

