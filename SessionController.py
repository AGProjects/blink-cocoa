# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import cjson
import hashlib
import re
import time
import socket
import urllib
import urlparse
import uuid

from itertools import chain

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton

from datetime import datetime, timedelta
from dateutil.tz import tzlocal

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, ToHeader, SIPCoreError
from sipsimple.lookup import DNSLookup
from sipsimple.session import Session, IllegalStateError, IllegalDirectionError
from sipsimple.util import TimestampedNotificationData, Timestamp

from sipsimple.threading.green import run_in_green_thread

from zope.interface import implements

from AppKit import *
from Foundation import *

from AlertPanel import AlertPanel
from AudioController import AudioController
from AccountSettings import AccountSettings
from BlinkLogger import BlinkLogger
from ChatController import ChatController
from DesktopSharingController import DesktopSharingController, DesktopSharingServerController, DesktopSharingViewerController
from FileTransferController import FileTransferController
from FileTransferSession import OutgoingPushFileTransferHandler
from HistoryManager import ChatHistory, SessionHistory
from MediaStream import *
from SessionRinger import Ringer
from SessionInfoController import SessionInfoController
from SIPManager import SIPManager
from VideoController import VideoController

from interfaces.itunes import MusicApplications

from util import *

SessionIdentifierSerial = 0
OUTBOUND_AUDIO_CALLS = 0

StreamHandlerForType = {
    "chat" : ChatController,
    "audio" : AudioController,
#    "video" : VideoController,
    "video" : ChatController,
    "file-transfer" : FileTransferController,
    "desktop-sharing" : DesktopSharingController,
    "desktop-server" : DesktopSharingServerController,
    "desktop-viewer" : DesktopSharingViewerController
}


class SessionControllersManager(object):
    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='AudioStreamGotDTMF')
        self.notification_center.add_observer(self, name='BlinkSessionDidEnd')
        self.notification_center.add_observer(self, name='BlinkSessionDidFail')
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.add_observer(self, name='SIPAccountDidActivate')
        self.notification_center.add_observer(self, name='SIPAccountDidDeactivate')
        self.notification_center.add_observer(self, name='SIPApplicationWillEnd')
        self.notification_center.add_observer(self, name='SIPSessionNewIncoming')
        self.notification_center.add_observer(self, name='SIPSessionNewOutgoing')
        self.notification_center.add_observer(self, name='SIPSessionDidStart')
        self.notification_center.add_observer(self, name='SIPSessionDidFail')
        self.notification_center.add_observer(self, name='SIPSessionDidEnd')
        self.notification_center.add_observer(self, name='SIPSessionGotProposal')
        self.notification_center.add_observer(self, name='SIPSessionGotRejectProposal')
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, name='MediaStreamDidInitialize')
        self.notification_center.add_observer(self, name='MediaStreamDidEnd')
        self.notification_center.add_observer(self, name='MediaStreamDidFail')

        self.last_calls_connections = {}
        self.last_calls_connections_authRequestCount = {}
        self.sessionControllers = []
        self.ringer = Ringer(self)
        self.incomingSessions = set()
        self.activeAudioStreams = set()
        self.pause_music = True

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPApplicationDidStart(self, sender, data):
        self.ringer.start()
        self.ringer.update_ringtones()
        settings = SIPSimpleSettings()
        self.pause_music = settings.audio.pause_music if settings.audio.pause_music and NSApp.delegate().applicationName != 'Blink Lite' else False

    def _NH_SIPApplicationWillEnd(self, sender, data):
        self.ringer.stop()

    def _NH_SIPSessionDidFail(self, session, data):
        self.incomingSessions.discard(session)
        if self.pause_music:
            self.incomingSessions.discard(session)
            if not self.activeAudioStreams and not self.incomingSessions:
                music_applications = MusicApplications()
                music_applications.resume()

    def _NH_SIPSessionDidStart(self, session, data):
        self.incomingSessions.discard(session)
        if self.pause_music:
            if all(stream.type != 'audio' for stream in data.streams):
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()

        if session.direction == 'incoming':
            if session.account is not BonjourAccount() and session.account.web_alert.show_alert_page_after_connect:
                self.show_web_alert_page(session)

    def _NH_SIPSessionDidEnd(self, session, data):
        if self.pause_music:
            self.incomingSessions.discard(session)
            if not self.activeAudioStreams and not self.incomingSessions:
                music_applications = MusicApplications()
                music_applications.resume()

    def _NH_SIPSessionGotProposal(self, session, data):
        if self.pause_music:
            if any(stream.type == 'audio' for stream in data.streams):
                music_applications = MusicApplications()
                music_applications.pause()

    def _NH_SIPSessionGotRejectProposal(self, session, data):
        if self.pause_music:
            if any(stream.type == 'audio' for stream in data.streams):
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()

    def _NH_MediaStreamDidInitialize(self, stream, data):
        if stream.type == 'audio':
            self.activeAudioStreams.add(stream)

    def _NH_MediaStreamDidEnd(self, stream, data):
        if self.pause_music:
            if stream.type == "audio":
                self.activeAudioStreams.discard(stream)
                # TODO: check if session has other streams and if yes, resume itunes
                # in case of session ends, resume is handled by the Session Controller
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()

    def _NH_MediaStreamDidFail(self, stream, data):
        if self.pause_music:
            if stream.type == "audio":
                self.activeAudioStreams.discard(stream)
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()


    @run_in_gui_thread
    def _NH_SIPSessionNewIncoming(self, session, data):
        streams = [stream for stream in data.streams if self.isProposedMediaTypeSupported([stream])]
        stream_type_list = list(set(stream.type for stream in streams))
        if not streams:
            BlinkLogger().log_info(u"Rejecting session for unsupported media type")
            session.reject(488, 'Incompatible media')
            return

        # if call waiting is disabled and we have audio calls reject with busy
        hasAudio = any(sess.hasStreamOfType("audio") for sess in self.sessionControllers)
        if 'audio' in stream_type_list and hasAudio and session.account is not BonjourAccount() and session.account.audio.call_waiting is False:
            BlinkLogger().log_info(u"Refusing audio call from %s because we are busy and call waiting is disabled" % format_identity_to_string(session.remote_identity))
            session.reject(486, 'Busy Here')
            return

        if 'audio' in stream_type_list and session.account is not BonjourAccount() and session.account.audio.do_not_disturb:
            BlinkLogger().log_info(u"Refusing audio call from %s because do not disturb is enabled" % format_identity_to_string(session.remote_identity))
            session.reject(session.account.sip.do_not_disturb_code, 'Do Not Disturb')
            return

        if 'audio' in stream_type_list and session.account is not BonjourAccount() and session.account.audio.reject_anonymous and session.remote_identity.uri.user.lower() in ('anonymous', 'unknown', 'unavailable'):
            BlinkLogger().log_info(u"Rejecting audio call from anonymous caller")
            session.reject(403, 'Anonymous Not Acceptable')
            return

        # at this stage call is allowed and will alert the user
        self.incomingSessions.add(session)

        if self.pause_music:
            music_applications = MusicApplications()
            music_applications.pause()

        self.ringer.add_incoming(session, streams)
        session.blink_supported_streams = streams

        settings = SIPSimpleSettings()
        stream_type_list = list(set(stream.type for stream in streams))

        if NSApp.delegate().windowController.hasContactMatchingURI(session.remote_identity.uri):
            if settings.chat.auto_accept and stream_type_list == ['chat']:
                BlinkLogger().log_info(u"Automatically accepting chat session from %s" % format_identity_to_string(session.remote_identity))
                self.startIncomingSession(session, streams)
                return
            elif settings.file_transfer.auto_accept and stream_type_list == ['file-transfer']:
                BlinkLogger().log_info(u"Automatically accepting file transfer from %s" % format_identity_to_string(session.remote_identity))
                self.startIncomingSession(session, streams)
                return
        elif session.account is BonjourAccount() and stream_type_list == ['chat']:
            BlinkLogger().log_info(u"Automatically accepting Bonjour chat session from %s" % format_identity_to_string(session.remote_identity))
            self.startIncomingSession(session, streams)
            return

        if stream_type_list == ['file-transfer'] and streams[0].file_selector.name.decode("utf8").startswith('xscreencapture'):
            BlinkLogger().log_info(u"Automatically accepting screenshot from %s" % format_identity_to_string(session.remote_identity))
            self.startIncomingSession(session, streams)
            return

        try:
            session.send_ring_indication()
        except IllegalStateError, e:
            BlinkLogger().log_info(u"IllegalStateError: %s" % e)
        else:
            if settings.answering_machine.enabled and settings.answering_machine.answer_delay == 0:
                self.startIncomingSession(session, [s for s in streams if s.type=='audio'], answeringMachine=True)
            else:
                sessionController = self.addControllerWithSession_(session)

                if not NSApp.delegate().windowController.alertPanel:
                    NSApp.delegate().windowController.alertPanel = AlertPanel.alloc().init()
                NSApp.delegate().windowController.alertPanel.addIncomingSession(session)
                NSApp.delegate().windowController.alertPanel.show()

        if session.account is not BonjourAccount() and not session.account.web_alert.show_alert_page_after_connect:
            self.show_web_alert_page(session)

    @run_in_gui_thread
    def _NH_SIPSessionNewOutgoing(self, session, data):
        self.ringer.add_outgoing(session, data.streams)
        if session.transfer_info is not None:
            # This Session was created as a result of a transfer
            self.addControllerWithSessionTransfer_(session)

    def _NH_AudioStreamGotDTMF(self, sender, data):
        key = data.digit
        filename = 'dtmf_%s_tone.wav' % {'*': 'star', '#': 'pound'}.get(key, key)
        wave_player = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get(filename))
        self.notification_center.add_observer(self, sender=wave_player)
        SIPApplication.voice_audio_bridge.add(wave_player)
        wave_player.start()

    def _NH_WavePlayerDidFail(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)

    def _NH_WavePlayerDidEnd(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)
            
    @run_in_gui_thread
    def _NH_BlinkSessionDidEnd(self, session_controller, data):
        if session_controller.session.direction == "incoming":
            self.log_incoming_session_ended(session_controller, data)
        else:
            self.log_outgoing_session_ended(session_controller, data)

    @run_in_gui_thread
    def _NH_BlinkSessionDidFail(self, session_controller, data):
        if data.direction == "outgoing":
            if data.code == 487:
                self.log_outgoing_session_cancelled(session_controller, data)
            else:
                self.log_outgoing_session_failed(session_controller, data)
        elif data.direction == "incoming":
            session = session_controller.session
            if data.code == 487 and data.failure_reason == 'Call completed elsewhere':
                self.log_incoming_session_answered_elsewhere(session_controller, data)
            else:
                self.log_incoming_session_missed(session_controller, data)
            
            if data.code == 487 and data.failure_reason == 'Call completed elsewhere':
                pass
            elif data.streams == ['file-transfer']:
                pass
            else:
                session_controller.log_info(u"Missed incoming session from %s" % format_identity_to_string(session.remote_identity))
                if 'audio' in data.streams:
                    NSApp.delegate().noteMissedCall()

                growl_data = TimestampedNotificationData()
                growl_data.caller = format_identity_to_string(session.remote_identity, check_contact=True, format='compact')
                growl_data.timestamp = data.timestamp
                growl_data.streams = ",".join(data.streams)
                growl_data.account = session.account.id.username + '@' + session.account.id.domain
                self.notification_center.post_notification("GrowlMissedCall", sender=self, data=growl_data)

    def _NH_SIPAccountDidActivate(self, account, data):
        if account is not BonjourAccount():
            self.get_last_calls(account)

    def _NH_SIPAccountDidDeactivate(self, account, data):
        if account is not BonjourAccount():
            self.close_last_call_connection(account)

    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if 'audio.pause_music' in data.modified:
            settings = SIPSimpleSettings()
            self.pause_music = settings.audio.pause_music if settings.audio.pause_music and NSApp.delegate().applicationName != 'Blink Lite' else False

    def addControllerWithSession_(self, session):
        sessionController = SessionController.alloc().initWithSession_(session)
        self.sessionControllers.append(sessionController)
        return sessionController

    def addControllerWithAccount_target_displayName_(self, account, target, display_name):
        sessionController = SessionController.alloc().initWithAccount_target_displayName_(account, target, display_name)
        self.sessionControllers.append(sessionController)
        return sessionController

    def addControllerWithSessionTransfer_(self, session):
        sessionController = SessionController.alloc().initWithSessionTransfer_(session)
        self.sessionControllers.append(sessionController)
        return sessionController

    def removeController(self, controller):
        try:
            self.sessionControllers.remove(controller)
        except ValueError:
            pass

    def send_files_to_contact(self, account, contact_uri, filenames):
        if not self.isMediaTypeSupported('file-transfer'):
            return

        target_uri = normalize_sip_uri_for_outgoing_session(contact_uri, AccountManager().default_account)

        for file in filenames:
            try:
                xfer = OutgoingPushFileTransferHandler(account, target_uri, file)
                xfer.start()
            except Exception, exc:
                BlinkLogger().log_error(u"Error while attempting to transfer file %s: %s" % (file, exc))

    def startIncomingSession(self, session, streams, answeringMachine=False):
        try:
            session_controller = (controller for controller in self.sessionControllers if controller.session == session).next()
        except StopIteration:
            session_controller = self.addControllerWithSession_(session)
        session_controller.setAnsweringMachineMode_(answeringMachine)
        session_controller.handleIncomingStreams(streams, False)

    def isScreenSharingEnabled(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(('127.0.0.1', 5900))
            s.close()
            return True
        except socket.error, msg:
            s.close()
            return False

    def isProposedMediaTypeSupported(self, streams):
        settings = SIPSimpleSettings()

        stream_type_list = list(set(stream.type for stream in streams))

        if 'desktop-sharing' in stream_type_list:
            ds = [s for s in streams if s.type == "desktop-sharing"]
            if ds and ds[0].handler.type != "active":
                if settings.desktop_sharing.disabled:
                    BlinkLogger().log_info(u"Screen Sharing is disabled in Blink Preferences")
                    return False
                if not self.isScreenSharingEnabled():
                    BlinkLogger().log_info(u"Screen Sharing is disabled in System Preferences")
                    return False

        if settings.file_transfer.disabled and 'file-transfer' in stream_type_list:
            BlinkLogger().log_info(u"File Transfers are disabled")
            return False

        if settings.chat.disabled and 'chat' in stream_type_list:
            BlinkLogger().log_info(u"Chat sessions are disabled")
            return False

        if 'video' in stream_type_list:
            # TODO: enable Video -adi
            return False

        return True

    def isMediaTypeSupported(self, type):
        settings = SIPSimpleSettings()

        if type == 'desktop-server':
            if settings.desktop_sharing.disabled:
                return False
            if not self.isScreenSharingEnabled():
                return False

        if settings.file_transfer.disabled and type == 'file-transfer':
            BlinkLogger().log_info(u"File Transfers are disabled")
            return False

        if settings.chat.disabled and type == 'chat':
            BlinkLogger().log_info(u"Chat sessions are disabled")
            return False

        if type == 'video':
            # TODO: enable Video -adi
            return False

        return True

    def log_incoming_session_missed(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return
        
        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        local_uri = format_identity_to_string(account)
        remote_uri = format_identity_to_string(controller.target_uri)
        focus = "1" if data.focus else "0"
        failure_reason = ''
        duration = 0
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''
        
        self.add_to_history(id, media_types, 'incoming', 'missed', failure_reason, data.timestamp, data.timestamp, duration, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)
        
        if 'audio' in data.streams:
            message = '<h3>Missed Incoming Audio Call</h3>'
            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'missed-call'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))
            
            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_incoming_session_ended(self, controller, data):
        account = controller.account
        session = controller.session
        if account is BonjourAccount():
            return
        
        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        local_uri = format_identity_to_string(account)
        remote_uri = format_identity_to_string(controller.target_uri)
        focus = "1" if data.focus else "0"
        failure_reason = ''
        if session.start_time is None and session.end_time is not None:
            # Session could have ended before it was completely started
            session.start_time = session.end_time
        
        duration = session.end_time - session.start_time
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''
        
        self.add_to_history(id, media_types, 'incoming', 'completed', failure_reason, session.start_time, session.end_time, duration.seconds, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)
        
        if 'audio' in data.streams:
            duration = self.get_printed_duration(session.start_time, session.end_time)
        message = '<h3>Incoming Audio Call</h3>'
        message += '<p>Call duration: %s' % duration
        #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
        media_type = 'audio'
        direction = 'incoming'
        status = 'delivered'
        cpim_from = data.target_uri
        cpim_to = format_identity_to_string(account)
        timestamp = str(Timestamp(datetime.now(tzlocal())))
        
        self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
        NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_incoming_session_answered_elsewhere(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return
        
        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        local_uri = format_identity_to_string(account)
        remote_uri = format_identity_to_string(controller.target_uri)
        focus = "1" if data.focus else "0"
        failure_reason = 'Answered elsewhere'
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''
        
        self.add_to_history(id, media_types, 'incoming', 'completed', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)
        
        if 'audio' in data.streams:
            message= '<h3>Incoming Audio Call</h3>'
            message += '<p>The call has been answered elsewhere'
            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            local_uri = local_uri
            remote_uri = remote_uri
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))
            
            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_outgoing_session_failed(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return
        
        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        focus = "1" if data.focus else "0"
        local_uri = format_identity_to_string(account)
        remote_uri = format_identity_to_string(controller.target_uri)
        failure_reason = '%s (%s)' % (data.reason or data.failure_reason, data.code)
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''
        
        self.add_to_history(id, media_types, 'outgoing', 'failed', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)
        
        if 'audio' in data.streams:
            message = '<h3>Failed Outgoing Audio Call</h3>'
            message += '<p>Reason: %s (%s)' % (data.reason or data.failure_reason, data.code) 
            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            local_uri = local_uri
            remote_uri = remote_uri
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))
            
            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_outgoing_session_cancelled(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return
        
        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        focus = "1" if data.focus else "0"
        local_uri = format_identity_to_string(account)
        remote_uri = format_identity_to_string(controller.target_uri)
        failure_reason = ''
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''
        
        self.add_to_history(id, media_types, 'outgoing', 'cancelled', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)
        
        if 'audio' in data.streams:
            message= '<h3>Cancelled Outgoing Audio Call</h3>'
            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))
            
            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_outgoing_session_ended(self, controller, data):
        account = controller.account
        session = controller.session
        if account is BonjourAccount():
            return
        
        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        focus = "1" if data.focus else "0"
        local_uri = format_identity_to_string(account)
        remote_uri = format_identity_to_string(controller.target_uri)
        direction = 'incoming'
        status = 'delivered'
        failure_reason = ''
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''
        
        if session.start_time is None and session.end_time is not None:
            # Session could have ended before it was completely started
            session.start_time = session.end_time
        
        duration = session.end_time - session.start_time
        
        self.add_to_history(id, media_types, 'outgoing', 'completed', failure_reason, session.start_time, session.end_time, duration.seconds, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)
        
        if 'audio' in data.streams:
            duration = self.get_printed_duration(session.start_time, session.end_time)
            message= '<h3>Outgoing Audio Call</h3>'
            message += '<p>Call duration: %s' % duration
            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))
            
            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity_to_string(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def get_printed_duration(self, start_time, end_time):
        duration = end_time - start_time
        if (duration.days > 0 or duration.seconds > 0):
            duration_print = ""
            if duration.days > 0 or duration.seconds > 3600:
                duration_print  += "%i hours, " % (duration.days*24 + duration.seconds/3600)
            seconds = duration.seconds % 3600
            duration_print += "%02i:%02i" % (seconds/60, seconds%60)
        else:
            duration_print = "00:00"
        
        return duration_print

    def add_to_history(self, id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants, call_id, from_tag, to_tag):
        SessionHistory().add_entry(id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants, call_id, from_tag, to_tag)

    def add_to_chat_history(self, id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=False):
        ChatHistory().add_message(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status, skip_replication=skip_replication)

    def updateGetCallsTimer_(self, timer):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['timer'] == timer).next()
        except StopIteration:
            return
        else:
            try:
                connection = self.last_calls_connections[key]['connection']
                nsurl = NSURL.URLWithString_(self.last_calls_connections[key]['url'])
            except KeyError:
                pass
            else:
                if connection:
                    connection.cancel()
                request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
                connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
                self.last_calls_connections[key]['data'] = ''
                self.last_calls_connections[key]['authRequestCount'] = 0
                self.last_calls_connections[key]['connection'] = connection
    
    # NSURLConnection delegate method
    def connection_didReceiveData_(self, connection, data):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                self.last_calls_connections[key]['data'] = self.last_calls_connections[key]['data'] + str(data)
    
    def connectionDidFinishLoading_(self, connection):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug(u"Calls history for %s retrieved from %s" % (key, self.last_calls_connections[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    calls = cjson.decode(self.last_calls_connections[key]['data'])
                except (TypeError, cjson.DecodeError):
                    BlinkLogger().log_debug(u"Failed to parse calls history for %s from %s" % (key, self.last_calls_connections[key]['url']))
                else:
                    self.syncServerHistoryWithLocalHistory(account, calls)
    
    # NSURLConnection delegate method
    def connection_didFailWithError_(self, connection, error):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            return
        BlinkLogger().log_debug(u"Failed to retrieve calls history for %s from %s" % (key, self.last_calls_connections[key]['url']))
    
    @run_in_green_thread
    def syncServerHistoryWithLocalHistory(self, account, calls):
        growl_notifications = {}
        try:
            for call in calls['received']:
                direction = 'incoming'
                local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                if not len(local_entry):
                    id=str(uuid.uuid1())
                    participants = ""
                    focus = "0"
                    local_uri = str(account.id)
                    try:
                        remote_uri = call['remoteParty']
                        start_time = datetime.strptime(call['startTime'], "%Y-%m-%d  %H:%M:%S")
                        end_time = datetime.strptime(call['stopTime'], "%Y-%m-%d  %H:%M:%S")
                        status = call['status']
                        duration = call['duration']
                        call_id = call['sessionId']
                        from_tag = call['fromTag']
                        to_tag = call['toTag']
                        media_types = ", ".join(call['media']) or 'audio'
                    except KeyError:
                        continue
                    success = 'completed' if duration > 0 else 'missed'
                    BlinkLogger().log_debug(u"Adding incoming %s call at %s from %s from server history" % (success, start_time, remote_uri))
                    self.add_to_history(id, media_types, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag)
                    if 'audio' in call['media']:
                        direction = 'incoming'
                        status = 'delivered'
                        cpim_from = remote_uri
                        cpim_to = local_uri
                        timestamp = str(Timestamp(datetime.now(tzlocal())))
                        if success == 'missed':
                            message = '<h3>Missed Incoming Audio Call</h3>'
                            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
                            media_type = 'missed-call'
                        else:
                            duration = self.get_printed_duration(start_time, end_time)
                            message = '<h3>Incoming Audio Call</h3>'
                            message += '<p>The call has been answered elsewhere'
                            message += '<p>Call duration: %s' % duration
                            #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
                            media_type = 'audio'
                        self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, time=start_time, skip_replication=True)
                        NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True))
                    
                    if 'audio' in call['media'] and success == 'missed' and remote_uri not in growl_notifications.keys():
                        now = datetime(*time.localtime()[:6])
                        elapsed = now - start_time
                        elapsed_hours = elapsed.seconds / (60*60)
                        if elapsed_hours < 48:
                            growl_data = TimestampedNotificationData()
                            try:
                                uri = SIPURI.parse('sip:'+str(remote_uri))
                            except Exception:
                                pass
                            else:
                                growl_data.caller = format_identity_to_string(uri, check_contact=True, format='compact')
                                growl_data.timestamp = start_time
                                growl_data.streams = media_types
                                growl_data.account = str(account.id)
                                self.notification_center.post_notification("GrowlMissedCall", sender=self, data=growl_data)
                                growl_notifications[remote_uri] = True
        except (KeyError, TypeError):
            pass
        
        try:
            for call in calls['placed']:
                direction = 'outgoing'
                local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                if not len(local_entry):
                    id=str(uuid.uuid1())
                    participants = ""
                    focus = "0"
                    local_uri = str(account.id)
                    try:
                        remote_uri = call['remoteParty']
                        start_time = datetime.strptime(call['startTime'], "%Y-%m-%d  %H:%M:%S")
                        end_time = datetime.strptime(call['stopTime'], "%Y-%m-%d  %H:%M:%S")
                        status = call['status']
                        duration = call['duration']
                        call_id = call['sessionId']
                        from_tag = call['fromTag']
                        to_tag = call['toTag']
                        media_types = ", ".join(call['media']) or 'audio'
                    except KeyError:
                        continue
                    
                    if duration > 0:
                        success = 'completed'
                    else:
                        if status == "487":
                            success = 'cancelled'
                        else:
                            success = 'failed'
                    
                    BlinkLogger().log_debug(u"Adding outgoing %s call at %s to %s from server history" % (success, start_time, remote_uri))
                    self.add_to_history(id, media_types, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag)
                    if 'audio' in call['media']:
                        local_uri = local_uri
                        remote_uri = remote_uri
                        direction = 'incoming'
                        status = 'delivered'
                        cpim_from = remote_uri
                        cpim_to = local_uri
                        timestamp = str(Timestamp(datetime.now(tzlocal())))
                        media_type = 'audio'
                        if success == 'failed':
                            message = '<h3>Failed Outgoing Audio Call</h3>'
                            message += '<p>Reason: %s' % status
                        elif success == 'cancelled':
                            message= '<h3>Cancelled Outgoing Audio Call</h3>'
                        else:
                            duration = self.get_printed_duration(start_time, end_time)
                            message= '<h3>Outgoing Audio Call</h3>'
                            message += '<p>Call duration: %s' % duration
                        self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, time=start_time, skip_replication=True)
                        NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True))
        except (KeyError, TypeError):
            pass
    
    # NSURLConnection delegate method
    def connection_didReceiveAuthenticationChallenge_(self, connection, challenge):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    self.last_calls_connections[key]['authRequestCount'] += 1
                except KeyError:
                    self.last_calls_connections[key]['authRequestCount'] = 1
                
                if self.last_calls_connections[key]['authRequestCount'] < 2:
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)

    @run_in_gui_thread
    def show_web_alert_page(self, session):
        # open web page with caller information
        if NSApp.delegate().applicationName == 'Blink Lite':
            return

        try:
            session_controller = (controller for controller in self.sessionControllers if controller.session == session).next()
        except StopIteration:
            return

        if session.account is not BonjourAccount() and session.account.web_alert.alert_url:
            url = unicode(session.account.web_alert.alert_url)
            
            replace_caller = urllib.urlencode({'x:': '%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)})
            caller_key = replace_caller[5:]
            url = url.replace('$caller_party', caller_key)
            
            replace_username = urllib.urlencode({'x:': '%s' % session.remote_identity.uri.user})
            url = url.replace('$caller_username', replace_username[5:])
            
            replace_account = urllib.urlencode({'x:': '%s' % session.account.id})
            url = url.replace('$called_party', replace_account[5:])
            
            settings = SIPSimpleSettings()
            
            if settings.gui.use_default_web_browser_for_alerts:
                session_controller.log_info(u"Opening HTTP URL in default browser %s"% url)
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            else:
                session_controller.log_info(u"Opening HTTP URL %s"% url)
                if not SIPManager()._delegate.accountSettingsPanels.has_key(caller_key):
                    SIPManager()._delegate.accountSettingsPanels[caller_key] = AccountSettings.createWithOwner_(self)
                SIPManager()._delegate.accountSettingsPanels[caller_key].showIncomingCall(session, url)

    @run_in_gui_thread
    def get_last_calls(self, account):
        if not account.server.settings_url:
            return
        query_string = "action=get_history"
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        nsurl = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(60, self, "updateGetCallsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSEventTrackingRunLoopMode)
        self.last_calls_connections[account.id] = { 'connection': connection,
                                                    'authRequestCount': 0,
                                                    'timer': timer,
                                                    'url': url,
                                                    'data': ''
                                                    }
    
    @run_in_gui_thread
    def close_last_call_connection(self, account):
        try:
            connection = self.last_calls_connections[account.id]['connection']
        except KeyError:
            pass
        else:
            if connection:
                connection.cancel()
        try:
            timer = self.last_calls_connections[account.id]['timer']
            if timer and timer.isValid():
                timer.invalidate()
                timer = None
            del self.last_calls_connections[account.id]
        except KeyError:
            pass


class SessionController(NSObject):
    implements(IObserver)

    session = None
    state = STATE_IDLE
    routes = None
    target_uri = None
    remoteParty = None
    endingBy = None
    answeringMachineMode = False
    failureReason = None
    inProposal = False
    proposalOriginator = None
    waitingForITunes = False
    streamHandlers = None
    chatPrintView = None
    collaboration_form_id = None
    remote_conference_has_audio = False
    transfer_window = None
    outbound_audio_calls = 0
    valid_dtmf = re.compile(r"^[0-9*#,]+$")
    pending_chat_messages = {}
    info_panel = None
    call_id = None
    from_tag = None
    to_tag = None
    dealloc_timer = None

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().windowController.sessionControllersManager

    def initWithAccount_target_displayName_(self, account, target_uri, display_name):
        global SessionIdentifierSerial
        assert isinstance(target_uri, SIPURI)
        self = super(SessionController, self).init()
        BlinkLogger().log_debug(u"Creating %s" % self)
        self.contactDisplayName = display_name
        self.remoteParty = display_name or format_identity_to_string(target_uri, format='compact')
        self.remotePartyObject = target_uri
        self.account = account
        self.target_uri = target_uri
        self.postdial_string = None
        self.remoteSIPAddress = format_identity_to_string(target_uri)
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.streamHandlers = []
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, sender=self)
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.nickname = None
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.mustShowDrawer = True
        self.open_chat_window_only = False
        self.try_next_hop = False

        # used for accounting
        self.streams_log = []
        self.participants_log = set()
        self.remote_focus_log = False

        return self

    def initWithSession_(self, session):
        global SessionIdentifierSerial
        self = super(SessionController, self).init()
        BlinkLogger().log_debug(u"Creating %s" % self)
        self.contactDisplayName = None
        self.remoteParty = format_identity_to_string(session.remote_identity, format='compact')
        self.remotePartyObject = session.remote_identity
        self.account = session.account
        self.session = session
        self.target_uri = SIPURI.new(session.remote_identity.uri if session.account is not BonjourAccount() else session._invitation.remote_contact_header.uri)
        self.postdial_string = None
        self.remoteSIPAddress = format_identity_to_string(self.target_uri)
        self.streamHandlers = []
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, sender=self)
        self.notification_center.add_observer(self, sender=self.session)
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.nickname = None
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.mustShowDrawer = True
        self.open_chat_window_only = False
        self.try_next_hop = False
        self.call_id = session._invitation.call_id
        self.initInfoPanel()

        # used for accounting
        self.streams_log = [stream.type for stream in session.proposed_streams or []]
        self.participants_log = set()
        self.remote_focus_log = False

        self.log_info(u'Invite from: "%s" <%s> with %s' % (session.remote_identity.display_name, session.remote_identity.uri, ", ".join(self.streams_log)))
        return self

    def initWithSessionTransfer_(self, session):
        global SessionIdentifierSerial
        self = super(SessionController, self).init()
        BlinkLogger().log_debug(u"Creating %s" % self)
        self.contactDisplayName = None
        self.remoteParty = format_identity_to_string(session.remote_identity, format='compact')
        self.remotePartyObject = session.remote_identity
        self.account = session.account
        self.session = session
        self.target_uri = SIPURI.new(session.remote_identity.uri)
        self.postdial_string = None
        self.remoteSIPAddress = format_identity_to_string(self.target_uri)
        self.streamHandlers = []
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, sender=self)
        self.notification_center.add_observer(self, sender=self.session)
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.nickname = None
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.mustShowDrawer = True
        self.open_chat_window_only = False
        self.try_next_hop = False
        self.initInfoPanel()

        # used for accounting
        self.streams_log = [stream.type for stream in session.proposed_streams or []]
        self.participants_log = set()
        self.remote_focus_log = False

        for stream in session.proposed_streams:
            if self.sessionControllersManager.isMediaTypeSupported(stream.type) and not self.hasStreamOfType(stream.type):
                handlerClass = StreamHandlerForType[stream.type]
                stream_controller = handlerClass(self, stream)
                self.streamHandlers.append(stream_controller)
                stream_controller.startOutgoing(False)

        return self

    def startDeallocTimer(self):
        self.notification_center.remove_observer(self, sender=self)
        self.notification_center.remove_observer(self, name='SystemWillSleep')
        self.destroyInfoPanel()

        if self.dealloc_timer is None:
            self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(10.0, self, "deallocTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

    def deallocTimer_(self, timer):
        self.resetSession()
        if self.chatPrintView is None:
            self.dealloc_timer.invalidate()
            self.dealloc_timer = None

    def dealloc(self):
        BlinkLogger().log_info(u"Disposing %s" % self)
        self.notification_center = None
        super(SessionController, self).dealloc()

    def log_info(self, text):
        BlinkLogger().log_info(u"[Session %d with %s] %s" % (self.identifier, self.remoteSIPAddress, text))

    def isActive(self):
        return self.state in (STATE_CONNECTED, STATE_CONNECTING, STATE_DNS_LOOKUP)

    def canProposeMediaStreamChanges(self):
        # TODO: hold is not handled by this, how to deal with it
        return not self.inProposal

    def canCancelProposal(self):
        if self.session is None:
            return False

        if self.session.state in ('cancelling_proposal', 'received_proposal', 'accepting_proposal', 'rejecting_proposal', 'accepting', 'incoming'):
            return False

        return True

    def acceptIncomingProposal(self, streams):
        self.handleIncomingStreams(streams, True)
        self.session.accept_proposal(streams)

    def handleIncomingStreams(self, streams, is_update=False):
        try:
            # give priority to chat stream so that we do not open audio drawer for composite streams
            sorted_streams = sorted(streams, key=lambda stream: 0 if stream.type=='chat' else 1)
            handled_types = set()
            for stream in sorted_streams:
                if self.sessionControllersManager.isMediaTypeSupported(stream.type):
                    if stream.type in handled_types:
                        self.log_info(u"Stream type %s has already been handled" % stream.type)
                        continue

                    controller = self.streamHandlerOfType(stream.type)
                    if controller is None:
                        handled_types.add(stream.type)
                        handler = StreamHandlerForType.get(stream.type, None)
                        controller = handler(self, stream)
                        self.streamHandlers.append(controller)
                        if stream.type not in self.streams_log:
                            self.streams_log.append(stream.type)
                    else:
                        controller.stream = stream

                    if self.answeringMachineMode and stream.type == "audio":
                        controller.startIncoming(is_update=is_update, is_answering_machine=True)
                    else:
                        controller.startIncoming(is_update=is_update)
                else:
                    self.log_info(u"Unknown incoming Stream type: %s (%s)" % (stream, stream.type))
                    raise TypeError("Unsupported stream type %s" % stream.type)

            if not is_update:
                self.session.accept(streams)
        except Exception, exc:
            # if there was some exception, reject the session
            if is_update:
                self.log_info(u"Error initializing additional streams: %s" % exc)
            else:
                self.log_info(u"Error initializing incoming session, rejecting it: %s" % exc)
                try:
                    self.session.reject(500)
                except IllegalDirectionError:
                    pass

    def setAnsweringMachineMode_(self, flag):
        self.answeringMachineMode = flag

    def hasStreamOfType(self, stype):
        return any(s for s in self.streamHandlers if s.stream and s.stream.type==stype)

    def streamHandlerOfType(self, stype):
        try:
            return (s for s in self.streamHandlers if s.stream and s.stream.type==stype).next()
        except StopIteration:
            return None

    def streamHandlerForStream(self, stream):
        try:
            return (s for s in self.streamHandlers if s.stream==stream).next()
        except StopIteration:
            return None

    def end(self):
        if self.state in (STATE_DNS_FAILED, STATE_DNS_LOOKUP):
            return
        if  self.session:
            self.session.end()

    def endStream(self, streamHandler):
        if self.session:
            if streamHandler.stream.type=="audio" and self.hasStreamOfType("desktop-sharing") and len(self.streamHandlers)==2:
                # if session is desktop-sharing end it 
                self.end()
                return True
            elif self.session.streams is not None and self.streamHandlers == [streamHandler]:
                # session established, streamHandler is the only stream
                self.log_info("Ending session with %s stream"% streamHandler.stream.type)
                # end the whole session
                self.end()
                return True
            elif len(self.streamHandlers) > 1 and self.session.streams and streamHandler.stream in self.session.streams:
                # session established, streamHandler is one of many streams
                if self.canProposeMediaStreamChanges():
                    self.log_info("Removing %s stream" % streamHandler.stream.type)
                    try:
                        self.session.remove_stream(streamHandler.stream)
                        self.notification_center.post_notification("BlinkSentRemoveProposal", sender=self, data=TimestampedNotificationData())
                        return True
                    except IllegalStateError, e:
                        self.log_info("IllegalStateError: %s" % e)
                        return False
                else:
                    self.log_info("Media Stream proposal is already in progress")
                    return False

            elif not self.streamHandlers and streamHandler.stream is None: # 3
                # session established, streamHandler is being proposed but not yet established
                self.log_info("Ending session with not-estabslihed %s stream"% streamHandler.stream.type)
                self.end()
                return True
            else:
                # session not yet established
                if self.session.streams is None:
                    self.end()
                    return True
                return False

    def cancelProposal(self, stream):
        if self.session:
            if self.canCancelProposal():
                self.log_info("Cancelling proposal")
                self.cancelledStream = stream
                try:
                    self.session.cancel_proposal()
                    self.notification_center.post_notification("BlinkWillCancelProposal", sender=self.session, data=TimestampedNotificationData())

                except IllegalStateError, e:
                    self.log_info("IllegalStateError: %s" % e)
            else:
                self.log_info("Cancelling proposal is already in progress")

    @property
    def ended(self):
        return self.state in (STATE_FINISHED, STATE_FAILED, STATE_DNS_FAILED)

    def removeStreamHandler(self, streamHandler):
        try:
            self.streamHandlers.remove(streamHandler)
        except ValueError:
            return

        # notify Chat Window controller to update the toolbar buttons
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self, data=TimestampedNotificationData())

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeSessionState(self, newstate, fail_reason=None):
        self.state = newstate
        # Below it makes a copy of the list because sessionChangedState can have the side effect of removing the handler from the list.
        # This is very bad behavior and should be fixed. -Dan
        for handler in self.streamHandlers[:]:
            handler.sessionStateChanged(newstate, fail_reason)
        self.notification_center.post_notification("BlinkSessionChangedState", sender=self, data=TimestampedNotificationData(state=newstate, reason=fail_reason))

    def resetSession(self):

        self.state = STATE_IDLE
        self.session = None
        self.endingBy = None
        self.failureReason = None
        self.cancelledStream = None
        self.remote_focus = False
        self.remote_focus_log = False
        self.conference_info = None
        self.invited_participants = []
        self.nickname = None
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.participants_log = set()
        self.streams_log = []
        self.remote_conference_has_audio = False
        self.open_chat_window_only = False
        self.destroyInfoPanel()
        NSApp.delegate().windowController.updatePresenceStatus()
        call_id = None
        from_tag = None
        to_tag = None

        SessionControllersManager().removeController(self)

    def initInfoPanel(self):
        if self.info_panel is None:
            self.info_panel = SessionInfoController(self)
            self.info_panel_was_visible = False
            self.info_panel_last_frame = False

    def destroyInfoPanel(self):
        if self.info_panel is not None:
            self.info_panel.close()
            self.info_panel = None

    def lookup_destination(self, target_uri):
        self.changeSessionState(STATE_DNS_LOOKUP)

        lookup = DNSLookup()
        self.notification_center.add_observer(self, sender=lookup)
        settings = SIPSimpleSettings()

        if isinstance(self.account, Account) and self.account.sip.outbound_proxy is not None:
            uri = SIPURI(host=self.account.sip.outbound_proxy.host, port=self.account.sip.outbound_proxy.port, 
                         parameters={'transport': self.account.sip.outbound_proxy.transport})
            self.log_info(u"Starting DNS lookup for %s through proxy %s" % (target_uri.host, uri))
        elif isinstance(self.account, Account) and self.account.sip.always_use_my_proxy:
            uri = SIPURI(host=self.account.id.domain)
            self.log_info(u"Starting DNS lookup for %s via proxy of account %s" % (target_uri.host, self.account.id))
        else:
            uri = target_uri
            self.log_info(u"Starting DNS lookup for %s" % target_uri.host)

        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def startCompositeSessionWithStreamsOfTypes(self, stype_tuple):
        if self.state in (STATE_FINISHED, STATE_DNS_FAILED, STATE_FAILED):
            self.resetSession()

        self.initInfoPanel()

        new_session = False
        add_streams = []
        if self.session is None:
            self.session = Session(self.account)
            if not self.try_next_hop:
                self.routes = None
            self.failureReason = None
            new_session = True

        for stype in stype_tuple:
            if type(stype) == tuple:
                stype, kwargs = stype
            else:
                kwargs = {}

            if stype not in self.streams_log:
                self.streams_log.append(stype)

            if not self.hasStreamOfType(stype):
                stream = None

                if self.sessionControllersManager.isMediaTypeSupported(stype):
                    handlerClass = StreamHandlerForType[stype]
                    stream = handlerClass.createStream(self.account)

                if not stream:
                    self.log_info("Cancelled session")
                    return False

                streamController = handlerClass(self, stream)
                self.streamHandlers.append(streamController)

                if stype == 'chat':
                    if (len(stype_tuple) == 1 and self.open_chat_window_only) or (not new_session and not self.canProposeMediaStreamChanges()):
                        # just show the window and wait for user to type before starting the outgoing session
                        streamController.openChatWindow()
                    else:
                        # starts outgoing chat session
                        streamController.startOutgoing(not new_session, **kwargs)
                else:
                    streamController.startOutgoing(not new_session, **kwargs)

                if not new_session:
                    # there is already a session, add audio stream to it
                    add_streams.append(streamController.stream)

            else:
                self.log_info("Stream already exists: %s"%self.streamHandlers)
                if stype == 'chat':
                    streamController = self.streamHandlerOfType('chat')
                    if streamController.status == STREAM_IDLE and len(stype_tuple) == 1:
                        # starts outgoing chat session
                        if self.streamHandlers == [streamController]:
                            new_session = True
                        else:
                            add_streams.append(streamController.stream)
                        streamController.startOutgoing(not new_session, **kwargs)

        if new_session:
            if not self.open_chat_window_only:
                # starts outgoing session
                if self.routes and self.try_next_hop:
                    self.connectSession()

                    if self.info_panel_was_visible:
                        self.info_panel.show()
                        self.info_panel.window.setFrame_display_animate_(self.info_panel_last_frame, True, True)
                else:
                    self.lookup_destination(self.target_uri)

                    outdev = SIPSimpleSettings().audio.output_device
                    indev = SIPSimpleSettings().audio.input_device
                    if outdev == u"system_default":
                        outdev = u"System Default"
                    if indev == u"system_default":
                        indev = u"System Default"

                    if any(streamHandler.stream.type=='audio' for streamHandler in self.streamHandlers):
                        self.log_info(u"Selected audio input/output devices: %s/%s" % (indev, outdev))
                        global OUTBOUND_AUDIO_CALLS
                        OUTBOUND_AUDIO_CALLS += 1
                        self.outbound_audio_calls = OUTBOUND_AUDIO_CALLS

                    if self.sessionControllersManager.pause_music:
                        if any(streamHandler.stream.type=='audio' for streamHandler in self.streamHandlers):
                            self.waitingForITunes = True
                            music_applications = MusicApplications()
                            music_applications.pause()
                            self.notification_center.add_observer(self, sender=music_applications)
                        else:
                            self.waitingForITunes = False

        else:
            if self.canProposeMediaStreamChanges():
                self.inProposal = True
                for stream in add_streams:
                    self.log_info("Proposing %s stream" % stream.type)
                    try:
                       self.session.add_stream(stream)
                       self.notification_center.post_notification("BlinkSentAddProposal", sender=self, data=TimestampedNotificationData())
                    except IllegalStateError, e:
                        self.log_info("IllegalStateError: %s" % e)
                        log_data = TimestampedNotificationData(timestamp=datetime.now(), failure_reason=e)
                        self.notification_center.post_notification("BlinkProposalDidFail", sender=self, data=log_data)
                        return False
            else:
                self.log_info("A stream proposal is already in progress")
                return False

        self.open_chat_window_only = False
        return True

    def startSessionWithStreamOfType(self, stype, kwargs={}): # pyobjc doesn't like **kwargs
        return self.startCompositeSessionWithStreamsOfTypes(((stype, kwargs), ))

    def startAudioSession(self):
        return self.startSessionWithStreamOfType("audio")

    def startChatSession(self):
        return self.startSessionWithStreamOfType("chat")

    def offerFileTransfer(self, file_path, content_type=None):
        return self.startSessionWithStreamOfType("chat", {"file_path":file_path, "content_type":content_type})

    def addAudioToSession(self):
        if not self.hasStreamOfType("audio"):
            self.startSessionWithStreamOfType("audio")

    def removeAudioFromSession(self):
        if self.hasStreamOfType("audio"):
            audioStream = self.streamHandlerOfType("audio")
            self.endStream(audioStream)

    def addChatToSession(self):
        if not self.hasStreamOfType("chat"):
            self.startSessionWithStreamOfType("chat")
        else:
            self.startSessionWithStreamOfType("chat")

    def removeChatFromSession(self):
        if self.hasStreamOfType("chat"):
            chatStream = self.streamHandlerOfType("chat")
            self.endStream(chatStream)

    def addVideoToSession(self):
        if not self.hasStreamOfType("video"):
            self.startSessionWithStreamOfType("video")

    def removeVideoFromSession(self):
        if self.hasStreamOfType("video"):
            videoStream = self.streamHandlerOfType("video")
            self.endStream(videoStream)

    def addMyDesktopToSession(self):
        if not self.hasStreamOfType("desktop-sharing"):
            self.startSessionWithStreamOfType("desktop-server")

    def addRemoteDesktopToSession(self):
        if not self.hasStreamOfType("desktop-sharing"):
            self.startSessionWithStreamOfType("desktop-viewer")

    def removeDesktopFromSession(self):
        if self.hasStreamOfType("desktop-sharing"):
            desktopStream = self.streamHandlerOfType("desktop-sharing")
            self.endStream(desktopStream)

    def getTitle(self):
        return format_identity_to_string(self.remotePartyObject)

    def getTitleFull(self):
        if self.contactDisplayName and self.contactDisplayName != 'None' and not self.contactDisplayName.startswith('sip:') and not self.contactDisplayName.startswith('sips:'):
            return "%s <%s>" % (self.contactDisplayName, format_identity_to_string(self.remotePartyObject))
        else:
            return self.getTitle()

    def getTitleShort(self):
        if self.contactDisplayName and self.contactDisplayName != 'None' and not self.contactDisplayName.startswith('sip:') and not self.contactDisplayName.startswith('sips:'):
            return self.contactDisplayName
        else:
            return format_identity_to_string(self.remotePartyObject, format='compact')

    @allocate_autorelease_pool
    @run_in_gui_thread
    def setRoutesFailed(self, msg):
        self.log_info("DNS lookup for SIP routes failed: '%s'"%msg)
        log_data = TimestampedNotificationData(direction='outgoing', target_uri=format_identity_to_string(self.target_uri, check_contact=True), timestamp=datetime.now(), code=478, originator='local', reason='DNS Lookup Failed', failure_reason='DNS Lookup Failed', streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log, call_id='', from_tag='', to_tag='')
        self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        self.changeSessionState(STATE_DNS_FAILED, 'DNS Lookup Failed')

    @allocate_autorelease_pool
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        self.routes = routes
        if not self.waitingForITunes:
            self.connectSession()

    def connectSession(self):
        if self.dealloc_timer is not None and self.dealloc_timer.isValid():
            self.dealloc_timer.invalidate()
            self.dealloc_timer = None

        self.log_info('Starting outgoing session to %s' % format_identity_to_string(self.target_uri, format='compact'))
        streams = [s.stream for s in self.streamHandlers]
        target_uri = SIPURI.new(self.target_uri)
        if self.account is not BonjourAccount() and self.account.pstn.dtmf_delimiter and self.account.pstn.dtmf_delimiter in target_uri.user:
            hash_parts = target_uri.user.partition(self.account.pstn.dtmf_delimiter)
            if self.valid_dtmf.match(hash_parts[2]):
                target_uri.user = hash_parts[0]
                self.postdial_string = hash_parts[2]

        self.notification_center.add_observer(self, sender=self.session)
        self.session.connect(ToHeader(target_uri), self.routes, streams)
        self.changeSessionState(STATE_CONNECTING)
        self.log_info("Connecting session to %s" % self.routes[0])
        self.notification_center.post_notification("BlinkSessionWillStart", sender=self, data=TimestampedNotificationData())

    def transferSession(self, target, replaced_session_controller=None):
        if self.session:
            target_uri = str(target)
            if '@' not in target_uri:
                target_uri = target_uri + '@' + self.account.id.domain
            if not target_uri.startswith(('sip:', 'sips:')):
                target_uri = 'sip:' + target_uri
            try:
                target_uri = SIPURI.parse(target_uri)
            except SIPCoreError:
                self.log_info("Bogus SIP URI for transfer %s" % target_uri)
            else:
                self.session.transfer(target_uri, replaced_session_controller.session if replaced_session_controller is not None else None)
                self.log_info("Outgoing transfer request to %s" % sip_prefix_pattern.sub("", str(target_uri)))

    def _acceptTransfer(self):
        self.log_info("Transfer request accepted by user")
        try:
            self.session.accept_transfer()
        except IllegalDirectionError:
            pass
        self.transfer_window = None

    def _rejectTransfer(self):
        self.log_info("Transfer request rejected by user")
        try:
            self.session.reject_transfer()
        except IllegalDirectionError:
            pass
        self.transfer_window = None

    def reject(self, code, reason):
        try:
            self.session.reject(code, reason)
        except IllegalStateError:
            pass

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_DNSLookupDidFail(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)
        message = u"DNS lookup of SIP proxies for %s failed: %s" % (unicode(self.target_uri.host), data.error)
        self.setRoutesFailed(message)
        lookup = None

    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)
        result_text = ', '.join(('%s:%s (%s)' % (result.address, result.port, result.transport.upper()) for result in data.result))
        self.log_info(u"DNS lookup for %s succeeded: %s" % (self.target_uri.host, result_text))
        routes = data.result
        if not routes:
            self.setRoutesFailed("No routes found to SIP Proxy")
        else:
            self.setRoutesResolved(routes)
        lookup = None

    def _NH_SystemWillSleep(self, sender, data):
        self.end()

    def _NH_MusicPauseDidExecute(self, sender, data):
        if not self.waitingForITunes:
            return
        self.notification_center.remove_observer(self, sender=sender)
        self.waitingForITunes = False
        if self.routes:
            self.connectSession()

    def _NH_SIPSessionGotRingIndication(self, sender, data):
        for sc in self.streamHandlers:
            sc.sessionRinging()
        self.notification_center.post_notification("BlinkSessionGotRingIndication", sender=self, data=TimestampedNotificationData())

    def _NH_SIPSessionWillStart(self, sender, data):
        self.log_info("Session will start")

    def _NH_SIPSessionDidStart(self, sender, data):

        self.remoteParty = format_identity_to_string(self.session.remote_identity)
        if self.session.remote_focus:
            self.remote_focus = True
            self.remote_focus_log = True
        else:
            # Remove any invited participants as the remote party does not support conferencing
            self.invited_participants = []
            self.conference_shared_files = []
        self.mustShowDrawer = True
        self.changeSessionState(STATE_CONNECTED)
        self.log_info("Session started")
        for contact in self.invited_participants:
            self.session.conference.add_participant(contact.uri)

        def numerify(num):
            try:
                int(num)
            except ValueError:
                return num
            else:
                return chr(65+int(num))

        # generate a unique id for the collaboration editor without digits, they don't work for some cloudy reason
        # The only common identifier for both parties is the SIP call id, though it may still fail if a B2BUA is in the path -adi
        hash = hashlib.sha1()
        id = '%s' % (self.remoteSIPAddress) if self.remote_focus else self.session._invitation.call_id
        hash.update(id)
        self.collaboration_form_id = ''.join(numerify(c) for c in hash.hexdigest())

        self.notification_center.post_notification("BlinkSessionDidStart", sender=self, data=TimestampedNotificationData())

    def _NH_SIPSessionWillEnd(self, sender, data):
        self.log_info("Session will end %sly"%data.originator)
        self.endingBy = data.originator
        if self.transfer_window is not None:
            self.transfer_window.close()
            self.transfer_window = None

    def _NH_SIPSessionDidFail(self, sender, data):
        try:
            self.call_id = sender._invitation.call_id
        except AttributeError:
            self.call_id = ''
        try:
            self.to_tag = sender._invitation.to_header.parameters['tag']
        except KeyError, AttributeError:
            self.to_tag = ''
        try:
            self.from_tag = sender._invitation.from_header.parameters['tag']
        except KeyError, AttributeError:
            self.from_tag = ''

        if data.failure_reason == 'Unknown error 61':
            status = u"Connection refused"
            self.failureReason = data.failure_reason
        elif data.failure_reason != 'user request':
            status = u"%s" % data.failure_reason
            self.failureReason = data.failure_reason
        elif data.reason:
            status = u"%s" % data.reason
            self.failureReason = data.reason
        else:
            status = u"Session Failed"
            self.failureReason = "failed"

        self.log_info("Session cancelled" if data.code == 487 else "Session failed: %s, %s (%s)" % (data.reason, data.failure_reason, data.code))
        log_data = TimestampedNotificationData(originator=data.originator, direction=sender.direction, target_uri=format_identity_to_string(self.target_uri, check_contact=True), timestamp=data.timestamp, code=data.code, reason=data.reason, failure_reason=self.failureReason, streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log, call_id=self.call_id, from_tag=self.from_tag, to_tag=self.to_tag)
        self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        self.changeSessionState(STATE_FAILED, status)

        if self.info_panel is not None and self.info_panel.window.isVisible():
            self.info_panel_was_visible = True
            self.info_panel_last_frame = self.info_panel.window.frame()

        oldSession = self.session

        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self, data=TimestampedNotificationData())
        self.notification_center.remove_observer(self, sender=sender)

        # redirect
        if data.code in (301, 302) and data.redirect_identities:
            redirect_to = data.redirect_identities[0].uri
            ret = NSRunAlertPanel("Redirect Call",
                  "The remote party has redirected his calls to %s@%s.\nWould you like to call this address?" % (redirect_to.user, redirect_to.host),
                  "Call", "Cancel", None)

            if ret == NSAlertDefaultReturn:
                target_uri = SIPURI.new(redirect_to)

                self.remotePartyObject = target_uri
                self.target_uri = target_uri
                self.remoteSIPAddress = format_identity_to_string(target_uri)

                if len(oldSession.proposed_streams) == 1:
                    self.startSessionWithStreamOfType(oldSession.proposed_streams[0].type)
                else:
                    self.startCompositeSessionWithStreamsOfTypes([s.type for s in oldSession.proposed_streams])

        # local timeout while we have an alternative route
        elif data.code == 408 and data.originator == 'local' and len(self.routes) > 1:
            self.log_info('Trying alternative route')
            self.routes.pop(0)
            self.try_next_hop = True
            if len(oldSession.proposed_streams) == 1:
                self.startSessionWithStreamOfType(oldSession.proposed_streams[0].type)
            else:
                self.startCompositeSessionWithStreamsOfTypes([s.type for s in oldSession.proposed_streams])

    def _NH_SIPSessionNewOutgoing(self, session, data):
        self.log_info(u"Proposed media: %s" % ','.join([s.type for s in data.streams]))

    def _NH_SIPSessionDidEnd(self, sender, data):
        self.call_id = sender._invitation.call_id
        try:
            self.to_tag = sender._invitation.to_header.parameters['tag']
        except KeyError:
            self.to_tag= ''
        try:
            self.from_tag = sender._invitation.from_header.parameters['tag']
        except KeyError:
            self.from_tag = ''

        self.log_info("Session ended")
        self.changeSessionState(STATE_FINISHED, data.originator)
        log_data = TimestampedNotificationData(target_uri=format_identity_to_string(self.target_uri, check_contact=True), streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log, call_id=self.call_id, from_tag=self.from_tag, to_tag=self.to_tag)
        self.notification_center.post_notification("BlinkSessionDidEnd", sender=self, data=log_data)
        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self, data=TimestampedNotificationData())
        self.notification_center.post_notification("BlinkSessionDidProcessTransaction", sender=self, data=TimestampedNotificationData())

        self.notification_center.remove_observer(self, sender=sender)

    def _NH_SIPSessionGotProvisionalResponse(self, sender, data):
        self.log_info("Got provisional response %s: %s" %(data.code, data.reason))
        if data.code != 180:
            log_data = TimestampedNotificationData(timestamp=datetime.now(), reason=data.reason, code=data.code)
            self.notification_center.post_notification("BlinkSessionGotProvisionalResponse", sender=self, data=log_data)

    def _NH_SIPSessionGotProposal(self, session, data):
        self.inProposal = True
        self.proposalOriginator = 'remote'

        if data.originator != "local":
            stream_names = ', '.join(stream.type for stream in data.streams)
            self.log_info(u"Received %s proposal" % stream_names)
            streams = data.streams
        
            settings = SIPSimpleSettings()
            stream_type_list = list(set(stream.type for stream in streams))
            
            if not self.sessionControllersManager.isProposedMediaTypeSupported(streams):
                self.log_info(u"Unsupported media type, proposal rejected")
                session.reject_proposal()
                return
            
            if stream_type_list == ['chat'] and 'audio' in (s.type for s in session.streams):
                self.log_info(u"Automatically accepting chat for established audio session from %s" % format_identity_to_string(session.remote_identity))
                self.acceptIncomingProposal(streams)
                return

            if session.account is BonjourAccount():
                if stream_type_list == ['chat']:
                    self.log_info(u"Automatically accepting Bonjour chat session from %s" % format_identity_to_string(session.remote_identity))
                    self.acceptIncomingProposal(streams)
                    return
                elif 'audio' in stream_type_list and session.account.audio.auto_accept:
                    session_manager = SessionManager()
                    have_audio_call = any(s for s in session_manager.sessions if s is not session and s.streams and 'audio' in (stream.type for stream in s.streams))
                    if not have_audio_call:
                        accepted_streams = [s for s in streams if s.type in ("audio", "chat")]
                        self.log_info(u"Automatically accepting Bonjour audio and chat session from %s" % format_identity_to_string(session.remote_identity))
                        self.acceptIncomingProposal(accepted_streams)
                        return

            if NSApp.delegate().windowController.hasContactMatchingURI(session.remote_identity.uri):
                if settings.chat.auto_accept and stream_type_list == ['chat']:
                    self.log_info(u"Automatically accepting chat session from %s" % format_identity_to_string(session.remote_identity))
                    self.acceptIncomingProposal(streams)
                    return
                elif settings.file_transfer.auto_accept and stream_type_list == ['file-transfer']:
                    self.log_info(u"Automatically accepting file transfer from %s" % format_identity_to_string(session.remote_identity))
                    self.acceptIncomingProposal(streams)
                    return

            try:
                session.send_ring_indication()
            except IllegalStateError, e:
                self.log_info(u"IllegalStateError: %s" % e)
                return
            else:
                if not NSApp.delegate().windowController.alertPanel:
                    NSApp.delegate().windowController.alertPanel = AlertPanel.alloc().init()
                NSApp.delegate().windowController.alertPanel.addIncomingStreamProposal(session, streams)
                NSApp.delegate().windowController.alertPanel.show()

            # needed to temporarily disable the Chat Window toolbar buttons
            self.notification_center.post_notification("BlinkGotProposal", sender=self, data=TimestampedNotificationData())

    def _NH_SIPSessionGotRejectProposal(self, sender, data):
        self.inProposal = False
        self.proposalOriginator = None
        self.log_info("Proposal cancelled" if data.code == 487 else "Proposal was rejected: %s (%s)"%(data.reason, data.code))

        log_data = TimestampedNotificationData(timestamp=datetime.now(), reason=data.reason, code=data.code)
        self.notification_center.post_notification("BlinkProposalGotRejected", sender=self, data=log_data)

        if data.streams:
            for stream in data.streams:
                if stream == self.cancelledStream:
                    self.cancelledStream = None
                if stream.type == "chat":
                    self.log_info("Removing chat stream")
                    handler = self.streamHandlerForStream(stream)
                    if handler:
                        handler.changeStatus(STREAM_FAILED, data.reason)
                elif stream.type == "audio":
                    self.log_info("Removing audio stream")
                    handler = self.streamHandlerForStream(stream)
                    if handler:
                        handler.changeStatus(STREAM_FAILED, data.reason)
                elif stream.type == "desktop-sharing":
                    self.log_info("Removing desktop sharing stream")
                    handler = self.streamHandlerForStream(stream)
                    if handler:
                        handler.changeStatus(STREAM_FAILED, data.reason)
                else:
                    self.log_info("Got reject proposal for unhandled stream type: %r" % stream)

            # notify Chat Window controller to update the toolbar buttons
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self, data=TimestampedNotificationData())

    def _NH_SIPSessionGotAcceptProposal(self, sender, data):
        self.inProposal = False
        self.proposalOriginator = None
        self.log_info("Proposal accepted")
        if data.streams:
            for stream in data.streams:
                handler = self.streamHandlerForStream(stream)
                if not handler and self.cancelledStream == stream:
                    self.log_info("Cancelled proposal for %s was accepted by remote, removing stream" % stream)
                    try:
                        self.session.remove_stream(stream)
                        self.cancelledStream = None
                    except IllegalStateError, e:
                        self.log_info("IllegalStateError: %s" % e)
            # notify by Chat Window controller to update the toolbar buttons
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self, data=TimestampedNotificationData())

    def _NH_SIPSessionHadProposalFailure(self, sender, data):
        self.inProposal = False
        self.proposalOriginator = None
        self.log_info("Proposal failure: %s" % data.failure_reason)

        log_data = TimestampedNotificationData(timestamp=datetime.now(), failure_reason=data.failure_reason)
        self.notification_center.post_notification("BlinkProposalDidFail", sender=self, data=log_data)

        if data.streams:
            for stream in data.streams:
                if stream == self.cancelledStream:
                    self.cancelledStream = None
                self.log_info("Removing %s stream" % stream.type)
                handler = self.streamHandlerForStream(stream)
                if handler:
                    handler.changeStatus(STREAM_FAILED, data.failure_reason)

            # notify Chat Window controller to update the toolbar buttons
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self, data=TimestampedNotificationData())

    def _NH_SIPInvitationChangedState(self, sender, data):
        self.notification_center.post_notification("BlinkInvitationChangedState", sender=self, data=data)

    def _NH_SIPSessionDidProcessTransaction(self, sender, data):
        self.notification_center.post_notification("BlinkSessionDidProcessTransaction", sender=self, data=data)

    def _NH_SIPSessionDidRenegotiateStreams(self, sender, data):
        if data.action == 'remove' and not sender.streams:
            self.log_info("There are no streams anymore, ending the session")
            self.end()
        self.notification_center.post_notification("BlinkDidRenegotiateStreams", sender=self, data=data)

    def _NH_SIPSessionGotConferenceInfo(self, sender, data):
        self.log_info(u"Received conference-info update")

        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.conference_shared_files = []
        self.conference_info = data.conference_info

        remote_conference_has_audio = any(media.media_type == 'audio' for media in chain(*chain(*(user for user in self.conference_info.users))))

        if remote_conference_has_audio and not self.remote_conference_has_audio:
            self.notification_center.post_notification("ConferenceHasAddedAudio", sender=self)
        self.remote_conference_has_audio = remote_conference_has_audio
         
        for user in data.conference_info.users:
            uri = sip_prefix_pattern.sub("", str(user.entity))
            # save uri for accounting purposes
            if uri != self.account.id:
                self.participants_log.add(uri)

            # remove invited participants that joined the conference
            try:
                contact = (contact for contact in self.invited_participants if uri == contact.uri).next()
            except StopIteration:
                pass
            else:
                self.invited_participants.remove(contact)

        if data.conference_info.conference_description.resources is not None and data.conference_info.conference_description.resources.files is not None:
            for file in data.conference_info.conference_description.resources.files:
                self.conference_shared_files.append(file)

        # notify controllers who need conference information
        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self, data=data)

    def _NH_SIPConferenceDidAddParticipant(self, sender, data):
        self.log_info(u"Added participant to conference: %s" % data.participant)
        uri = sip_prefix_pattern.sub("", str(data.participant))
        try:
            contact = (contact for contact in self.invited_participants if uri == contact.uri).next()
        except StopIteration:
            pass
        else:
            self.invited_participants.remove(contact)
            # notify controllers who need conference information
            self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self, data=TimestampedNotificationData())

    def _NH_SIPConferenceDidNotAddParticipant(self, sender, data):
        self.log_info(u"Failed to add participant %s to conference: %s %s" % (data.participant, data.code, data.reason))
        uri = sip_prefix_pattern.sub("", str(data.participant))
        try:
            contact = (contact for contact in self.invited_participants if uri == contact.uri).next()
        except StopIteration:
            pass
        else:
            contact.setDetail('%s (%s)' % (data.reason, data.code))
            self.failed_to_join_participants[uri]=time.time()
            if data.code >= 400 or data.code == 0:
                contact.setDetail('Invite Failed: %s (%s)' % (data.reason, data.code))
                self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self, data=TimestampedNotificationData())

    def _NH_SIPConferenceGotAddParticipantProgress(self, sender, data):
        uri = sip_prefix_pattern.sub("", str(data.participant))
        try:
            contact = (contact for contact in self.invited_participants if uri == contact.uri).next()
        except StopIteration:
            pass
        else:
            if data.code == 100:
                contact.setDetail('Connecting...')
            elif data.code in (180, 183):
                contact.setDetail('Ringing...')
            elif data.code == 200:
                contact.setDetail('Invitation accepted')
            elif data.code < 400:
                contact.setDetail('%s (%s)' % (data.reason, data.code))

            # notify controllers who need conference information
            self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self, data=TimestampedNotificationData())

    def _NH_SIPSessionTransferNewIncoming(self, sender, data):
        target = "%s@%s" % (data.transfer_destination.user, data.transfer_destination.host)
        self.log_info(u'Incoming transfer request to %s' % target)
        self.notification_center.post_notification("BlinkSessionTransferNewIncoming", sender=self, data=data)
        if self.account.audio.auto_transfer:
            self.log_info(u'Auto-accepting transfer request')
            sender.accept_transfer()
        else:
            self.transfer_window = CallTransferWindowController(self, target)
            self.transfer_window.show()

    def _NH_SIPSessionTransferNewOutgoing(self, sender, data):
        target = "%s@%s" % (data.transfer_destination.user, data.transfer_destination.host)
        self.notification_center.post_notification("BlinkSessionTransferNewOutgoing", sender=self, data=data)

    def _NH_SIPSessionTransferDidStart(self, sender, data):
        self.log_info(u'Transfer started')
        self.notification_center.post_notification("BlinkSessionTransferDidStart", sender=self, data=data)

    def _NH_SIPSessionTransferDidEnd(self, sender, data):
        self.log_info(u'Transfer succeeded')
        self.notification_center.post_notification("BlinkSessionTransferDidEnd", sender=self, data=data)

    def _NH_SIPSessionTransferDidFail(self, sender, data):
        self.log_info(u'Transfer failed %s: %s' % (data.code, data.reason))
        self.notification_center.post_notification("BlinkSessionTransferDidFail", sender=self, data=data)

    def _NH_SIPSessionTransferGotProgress(self, sender, data):
        self.log_info(u'Transfer got progress %s: %s' % (data.code, data.reason))
        self.notification_center.post_notification("BlinkSessionTransferGotProgress", sender=self, data=data)

    def _NH_BlinkChatWindowWasClosed(self, sender, data):
        self.startDeallocTimer()

    def _NH_BlinkSessionDidFail(self, sender, data):
        self.startDeallocTimer()

    def _NH_BlinkSessionDidEnd(self, sender, data):
        self.startDeallocTimer()


class CallTransferWindowController(NSObject):
    window = objc.IBOutlet()
    label = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, session_controller, target):
        NSBundle.loadNibNamed_owner_("CallTransferWindow", self)
        self.session_controller = session_controller
        self.label.setStringValue_("Remote party would like to transfer you to %s\nWould you like to proceed and call this address?" % target)

    @objc.IBAction
    def callButtonClicked_(self, sender):
        self.session_controller._acceptTransfer()
        self.close()

    @objc.IBAction
    def cancelButtonClicked_(self, sender):
        self.session_controller._rejectTransfer()
        self.close()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)

    def close(self):
        self.session_controller = None
        self.window.close()

