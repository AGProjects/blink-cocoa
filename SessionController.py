# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import hashlib
import re
import time
from itertools import chain

from application.notification import IObserver, NotificationCenter
from application.python import Null

from datetime import datetime

from sipsimple.account import BonjourAccount
from sipsimple.session import Session, IllegalStateError, IllegalDirectionError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, ToHeader, SIPCoreError
from sipsimple.util import TimestampedNotificationData

from zope.interface import implements

from AppKit import objc, NSApp, NSBundle, NSRunAlertPanel, NSAlertDefaultReturn
from Foundation import NSObject
from AudioController import AudioController
from VideoController import VideoController
from MediaStream import *
from BlinkLogger import BlinkLogger
from ChatController import ChatController, userClickedToolbarButtonWhileDisconnected, updateToolbarButtonsWhileDisconnected, validateToolbarButtonWhileDisconnected
from DesktopSharingController import DesktopSharingController, DesktopSharingServerController, DesktopSharingViewerController
from FileTransferController import FileTransferController

from SessionInfoController import SessionInfoController
from SIPManager import SIPManager
from interfaces.itunes import MusicApplications
from util import *

SessionIdentifierSerial = 0
OUTBOUND_AUDIO_CALLS = 0

StreamHandlerForType = {
    "chat" : ChatController,
    "audio" : AudioController,
#    "video" : VideoController,  TODO: add video -adi
    "video" : ChatController,
    "file-transfer" : FileTransferController,
    "desktop-sharing" : DesktopSharingController,
    "desktop-server" : DesktopSharingServerController,
    "desktop-viewer" : DesktopSharingViewerController
}

class SessionController(NSObject):
    implements(IObserver)

    owner = None
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
    lastChatOutputView = None
    collaboration_form_id = None
    remote_conference_has_audio = False
    transfer_window = None
    outbound_audio_calls = 0
    valid_dtmf = re.compile(r"^[0-9*#,]+$")

    def initWithAccount_target_displayName_(self, account, target_uri, display_name):
        global SessionIdentifierSerial
        assert isinstance(target_uri, SIPURI)
        self = super(SessionController, self).init()
        self.contactDisplayName = display_name
        self.remoteParty = display_name or format_identity_simple(target_uri)
        self.remotePartyObject = target_uri
        self.account = account
        self.target_uri = target_uri
        self.postdial_string = None
        self.remoteSIPAddress = format_identity_address(target_uri)
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.streamHandlers = []
        self.notification_center = NotificationCenter()
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.mustShowDrawer = True
        self.info_panel = SessionInfoController(self)
        self.open_chat_window_only = False
        self.try_next_hop = False
        self.info_panel_was_visible = False
        self.info_panel_last_frame = False

        # used for accounting
        self.streams_log = []
        self.participants_log = set()
        self.remote_focus_log = False

        self.log_info('Starting outgoing session to %s' % format_identity_simple(target_uri))
        return self

    def initWithSession_(self, session):
        global SessionIdentifierSerial
        self = super(SessionController, self).init()
        self.contactDisplayName = None
        self.remoteParty = format_identity_simple(session.remote_identity)
        self.remotePartyObject = session.remote_identity
        self.account = session.account
        self.session = session
        self.target_uri = SIPURI.new(session.remote_identity.uri if session.account is not BonjourAccount() else session._invitation.remote_contact_header.uri)
        self.postdial_string = None
        self.remoteSIPAddress = format_identity_address(self.target_uri)
        self.streamHandlers = []
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=self.session)
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.mustShowDrawer = True
        self.info_panel = SessionInfoController(self)
        self.open_chat_window_only = False
        self.try_next_hop = False
        self.info_panel_was_visible = False
        self.info_panel_last_frame = False

        # used for accounting
        self.streams_log = [stream.type for stream in session.proposed_streams or []]
        self.participants_log = set()
        self.remote_focus_log = False

        self.log_info(u'Invite from: "%s" <%s> with %s' % (session.remote_identity.display_name, session.remote_identity.uri, ", ".join(self.streams_log)))
        return self

    def initWithSessionTransfer_owner_(self, session, owner):
        global SessionIdentifierSerial
        self = super(SessionController, self).init()
        self.contactDisplayName = None
        self.remoteParty = format_identity_simple(session.remote_identity)
        self.remotePartyObject = session.remote_identity
        self.account = session.account
        self.session = session
        self.target_uri = SIPURI.new(session.remote_identity.uri)
        self.postdial_string = None
        self.remoteSIPAddress = format_identity_address(self.target_uri)
        self.streamHandlers = []
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=self.session)
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.mustShowDrawer = True
        self.info_panel = SessionInfoController(self)
        self.open_chat_window_only = False
        self.owner = owner
        self.try_next_hop = False
        self.info_panel_was_visible = False
        self.info_panel_last_frame = False

        # used for accounting
        self.streams_log = [stream.type for stream in session.proposed_streams or []]
        self.participants_log = set()
        self.remote_focus_log = False

        for stream in session.proposed_streams:
            if SIPManager().isMediaTypeSupported(stream.type) and not self.hasStreamOfType(stream.type):
                handlerClass = StreamHandlerForType[stream.type]
                stream_controller = handlerClass(self, stream)
                self.streamHandlers.append(stream_controller)
                stream_controller.startOutgoing(False)

        self.log_info('Starting outgoing session to %s' % format_identity_simple(self.target_uri))
        return self

    def log_info(self, text):
        BlinkLogger().log_info(u"[Session %d with %s] %s" % (self.identifier, self.remoteSIPAddress, text))

    def isActive(self):
        return self.state in (STATE_CONNECTED, STATE_CONNECTING, STATE_DNS_LOOKUP)

    def canProposeMediaStreamChanges(self):
        # TODO: hold is not handled by this, how to deal with it

        #if self.session is not None and self.session.state in ('received_proposal', 'sending_proposal', 'accepting_proposal', 'rejecting_proposal', 'cancelling_proposal'):
        #    return False

        if self.inProposal:
            return False

        return True

    def canCancelProposal(self):
        if self.session is not None and self.session.state == 'cancelling_proposal':
            return False

        return True    

    def handleIncomingStreams(self, streams, is_update=False):
        try:
            # give priority to chat stream so that we do not open audio drawer for composite streams
            sorted_streams = sorted(streams, key=lambda stream: 0 if stream.type=='chat' else 1)
            for s in sorted_streams:
                if SIPManager().isMediaTypeSupported(s.type):
                    handler = StreamHandlerForType.get(s.type, None)
                    controller = handler(self, s)
                    self.streamHandlers.append(controller)
                    if s.type not in self.streams_log:
                        self.streams_log.append(s.type)
                    if self.answeringMachineMode and s.type == "audio":
                        controller.startIncoming(is_update=is_update, is_answering_machine=True)
                    else:
                        controller.startIncoming(is_update=is_update)
                else:
                    self.log_info(u"Unknown incoming Stream type: %s (%s)" % (s, s.type))
                    raise TypeError("Unsupported stream type %s" % s.type)

            if not is_update:
                self.session.accept(streams)
        except Exception, exc:
            # if there was some exception, reject the session
            if is_update:
                self.log_info(u"Error initializing additional streams: %s" % exc)
            else:
                self.log_info(u"Error initializing incoming session, rejecting it: %s" % exc)
                self.session.reject(500)
            NSRunAlertPanel("Error Accepting Session", "An error occurred while initiating the session:\n %s" % exc, "OK", None, None)

    def setAnsweringMachineMode_(self, flag):
        self.answeringMachineMode = flag

    def setOwner_(self, owner):
        self.owner = owner

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
            elif self.streamHandlers == [streamHandler]:
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
                        self.notification_center.post_notification("BlinkSentRemoveProposal", sender=self)
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
                    self.log_info("Ending session that did not start yet")
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
                    self.notification_center.post_notification("BlinkWillCancelProposal", sender=self.session)

                except IllegalStateError, e:
                    self.log_info("IllegalStateError: %s" % e)
            else:
                self.log_info("Cancelling proposal is already in progress")

    @property
    def ended(self):
        return self.state in (STATE_FINISHED, STATE_FAILED, STATE_DNS_FAILED)

    def removeStreamHandler(self, streamHandler):
        if streamHandler not in self.streamHandlers:
            return

        self.streamHandlers.remove(streamHandler)

        # notify Chat Window controller to update the toolbar buttons
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeSessionState(self, newstate, fail_reason=None):
        self.state = newstate
        # Below it makes a copy of the list because sessionChangedState can have the side effect of removing the handler from the list.
        # This is very bad behavior and should be fixed. -Dan
        for handler in self.streamHandlers[:]:
            handler.sessionStateChanged(newstate, fail_reason)
        self.notification_center.post_notification("BlinkSessionChangedState", sender=self, data=dict(state=newstate, reason=fail_reason))

    def resetSession(self):
        self.notification_center.discard_observer(self, sender=self.session)
        self.streamHandlers = []
        self.state = STATE_IDLE
        self.session = None
        self.endingBy = None
        self.failureReason = None
        self.cancelledStream = None
        self.remote_focus = False
        self.remote_focus_log = False
        self.conference_info = None
        self.invited_participants = []
        self.conference_shared_files = []
        self.pending_removal_participants = set()
        self.failed_to_join_participants = {}
        self.participants_log = set()
        self.streams_log = []
        self.remote_conference_has_audio = False
        self.open_chat_window_only = False

        if self.info_panel is not None:
            self.info_panel.close()
            self.info_panel = None

    def initializeSessionWithAccount(self, account):
        if self.session is None:
            self.session = Session(account)
            self.notification_center.add_observer(self, sender=self.session)
            if not self.try_next_hop:
                self.routes = None
            self.failureReason = None

    def startCompositeSessionWithStreamsOfTypes(self, stype_tuple):
        if self.state in (STATE_FINISHED, STATE_DNS_FAILED, STATE_FAILED):
            self.resetSession()

        new_session = False
        add_streams = []
        if self.session is None:
            # no session yet, initiate it
            self.initializeSessionWithAccount(self.account)
            new_session = True
            self.info_panel = SessionInfoController(self)

        for stype in stype_tuple:
            if type(stype) == tuple:
                stype, kwargs = stype
            else:
                kwargs = {}

            if not self.hasStreamOfType(stype):
                if stype not in self.streams_log:
                    self.streams_log.append(stype)
                stream = None
                if SIPManager().isMediaTypeSupported(stype):
                    handlerClass = StreamHandlerForType[stype]
                    stream = handlerClass.createStream(self.account)
                if not stream:
                    self.log_info("Cancelled session")
                    return False
                controller = handlerClass(self, stream)
                self.streamHandlers.append(controller)

                if stype == 'chat':
                    if (len(stype_tuple) == 1 and self.open_chat_window_only) or (not new_session and not self.canProposeMediaStreamChanges()):
                        # just show the window and wait for user to type before starting the outgoing session
                        controller.openChatWindow()
                    else:
                        # starts outgoing chat session
                        controller.startOutgoing(not new_session, **kwargs)
                else:
                    controller.startOutgoing(not new_session, **kwargs)

                if not new_session:
                    # there is already a session, add audio stream to it
                    add_streams.append(controller.stream)

            else:
                self.log_info("Stream already exists: %s"%self.streamHandlers)
                if stype == 'chat':
                    controller = self.streamHandlerOfType('chat')
                    if controller.status == STREAM_IDLE and len(stype_tuple) == 1:
                        # starts outgoing chat session
                        new_session = True
                        controller.startOutgoing(not new_session, **kwargs)

        if new_session:
            if not self.open_chat_window_only:
                # starts outgoing session
                if self.routes and self.try_next_hop:
                    self.connectSession()

                    if self.info_panel_was_visible:
                        self.info_panel.show()
                        self.info_panel.window.setFrame_display_animate_(self.info_panel_last_frame, True, True)
                else:
                    self.changeSessionState(STATE_DNS_LOOKUP)
                    SIPManager().lookup_sip_proxies(self.account, self.target_uri, self)

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

                    if SIPManager().pause_music:
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
                    except IllegalStateError, e:
                        self.log_info("IllegalStateError: %s" % e)
                        return False
                self.notification_center.post_notification("BlinkSentAddProposal", sender=self)
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
        return format_identity(self.remotePartyObject)

    def getTitleFull(self):
        if self.contactDisplayName and self.contactDisplayName != 'None' and not self.contactDisplayName.startswith('sip:') and not self.contactDisplayName.startswith('sips:'):
            return "%s <%s>" % (self.contactDisplayName, format_identity_address(self.remotePartyObject))
        else:
            return self.getTitle()

    def getTitleShort(self):
        if self.contactDisplayName and self.contactDisplayName != 'None' and not self.contactDisplayName.startswith('sip:') and not self.contactDisplayName.startswith('sips:'):
            return self.contactDisplayName
        else:
            return format_identity_simple(self.remotePartyObject)

    def setRoutesFailed(self, msg):
        self.log_info("DNS lookup for SIP routes failed: '%s'"%msg)

        log_data = TimestampedNotificationData(direction='outgoing', target_uri=format_identity(self.target_uri, check_contact=True), timestamp=datetime.now(), code=478, originator='local', reason='DNS Lookup Failed', failure_reason='DNS Lookup Failed', streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
        self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        self.changeSessionState(STATE_DNS_FAILED, msg)
        self.end()

        if self.info_panel is not None:
            self.info_panel.close()
            self.info_panel = None

    @allocate_autorelease_pool
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        if self.routes != routes:
            self.routes = routes

        if len(routes) == 0:
            self.changeSessionState(STATE_DNS_FAILED, u"No routes found to SIP proxy")
            self.log_info("Session failed: No route found to SIP proxy")

            log_data = TimestampedNotificationData(direction='outgoing', originator='local', target_uri=format_identity(self.target_uri, check_contact=True), timestamp=datetime.now(), code=478, reason='No route found to SIP proxy', failure_reason='No route found to SIP proxy', streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
            self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        elif not self.waitingForITunes:
            self.connectSession()

    def connectSession(self):
        if self.session:
            streams = [s.stream for s in self.streamHandlers]
            target_uri = SIPURI.new(self.target_uri)

            if '#' in target_uri.user:
                hash_parts = target_uri.user.partition('#')
                if self.valid_dtmf.match(hash_parts[2]):
                    target_uri.user = hash_parts[0]
                    self.postdial_string = hash_parts[2]

            self.session.connect(ToHeader(target_uri), self.routes, streams)
            self.changeSessionState(STATE_CONNECTING)
            self.log_info("Connecting session to %s" % self.routes[0])
            self.notification_center.post_notification("BlinkSessionWillStart", sender=self)

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
                self.log_info("Bogus SIP URI for transfer" % target_uri)
            else:
                self.session.transfer(target_uri, replaced_session_controller.session if replaced_session_controller is not None else None)
                self.log_info("Outgoing transfer request to %s" % re.sub("^(sip:|sips:)", "", str(target_uri)))

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
        self.notification_center.post_notification("BlinkSessionGotRingIndication", sender=self)

    def _NH_SIPSessionWillStart(self, sender, data):
        self.log_info("Session will start")

    def _NH_SIPSessionDidStart(self, sender, data):
        self.remoteParty = format_identity(self.session.remote_identity)
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

        self.notification_center.post_notification("BlinkSessionDidStart", sender=self)

    def _NH_SIPSessionWillEnd(self, sender, data):
        self.log_info("Session will end %sly"%data.originator)
        self.endingBy = data.originator
        if self.transfer_window is not None:
            self.transfer_window.close()
            self.transfer_window = None

    def _NH_SIPSessionDidFail(self, sender, data):
        if data.failure_reason == 'Unknown error 61':
            status = u"TLS connection error"
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
        log_data = TimestampedNotificationData(originator=data.originator, direction=sender.direction, target_uri=format_identity(self.target_uri, check_contact=True), timestamp=data.timestamp, code=data.code, reason=data.reason, failure_reason=self.failureReason, streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
        self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        self.changeSessionState(STATE_FAILED, status)

        if self.info_panel is not None and self.info_panel.window.isVisible():
            self.info_panel_was_visible = True
            self.info_panel_last_frame = self.info_panel.window.frame()

        oldSession = self.session
        self.resetSession()

        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)

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
                self.remoteSIPAddress = format_identity_address(target_uri)

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
        elif SIPManager().pause_music:
            SIPManager().incomingSessions.discard(sender)
            if not SIPManager().activeAudioStreams and not SIPManager().incomingSessions:
                music_applications = MusicApplications()
                music_applications.resume()

    def _NH_SIPSessionNewOutgoing(self, session, data):
        self.log_info(u"Proposed media: %s" % ','.join([s.type for s in data.streams]))

    def _NH_SIPSessionDidEnd(self, sender, data):
        self.log_info("Session ended")
        self.changeSessionState(STATE_FINISHED, data.originator)
        log_data = TimestampedNotificationData(target_uri=format_identity(self.target_uri, check_contact=True), streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
        self.notification_center.post_notification("BlinkSessionDidEnd", sender=self, data=log_data)

        self.resetSession()

        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)
        self.notification_center.post_notification("BlinkSessionDidProcessTransaction", sender=self)

        if SIPManager().pause_music:
            SIPManager().incomingSessions.discard(sender)
            if not SIPManager().activeAudioStreams and not SIPManager().incomingSessions:
                music_applications = MusicApplications()
                music_applications.resume()


    def _NH_SIPSessionGotProvisionalResponse(self, sender, data):
        self.log_info("Got provisional response %s: %s" %(data.code, data.reason))
        if data.code != 180:
            log_data = TimestampedNotificationData(timestamp=datetime.now(), reason=data.reason, code=data.code)
            self.notification_center.post_notification("BlinkSessionGotProvisionalResponse", sender=self, data=log_data)

    def _NH_SIPSessionGotProposal(self, sender, data):
        self.inProposal = True
        self.proposalOriginator = 'remote'
        if data.originator != "local":
            stream_names = ', '.join(stream.type for stream in data.streams)
            self.log_info(u"Received %s proposal" % stream_names)
            self.owner.handle_incoming_proposal(sender, data.streams)

            # needed to temporarily disable the Chat Window toolbar buttons
            self.notification_center.post_notification("BlinkGotProposal", sender=self)

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
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

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
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

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
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

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
            uri = re.sub("^(sip:|sips:)", "", str(user.entity))
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
        uri = re.sub("^(sip:|sips:)", "", str(data.participant))
        try:
            contact = (contact for contact in self.invited_participants if uri == contact.uri).next()
        except StopIteration:
            pass
        else:
            self.invited_participants.remove(contact)
            # notify controllers who need conference information
            self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)

    def _NH_SIPConferenceDidNotAddParticipant(self, sender, data):
        self.log_info(u"Failed to add participant %s to conference: %s %s" % (data.participant, data.code, data.reason))
        uri = re.sub("^(sip:|sips:)", "", str(data.participant))
        try:
            contact = (contact for contact in self.invited_participants if uri == contact.uri).next()
        except StopIteration:
            pass
        else:
            contact.setDetail('%s (%s)' % (data.reason, data.code))
            self.failed_to_join_participants[uri]=time.time()
            if data.code >= 400 or data.code == 0:
                contact.setDetail('Invite Failed: %s (%s)' % (data.reason, data.code))
                self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)

    def _NH_SIPConferenceGotAddParticipantProgress(self, sender, data):
        uri = re.sub("^(sip:|sips:)", "", str(data.participant))
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
            self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)

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

    def updateToolbarButtons(self, toolbar, got_proposal=False):
        # update Chat Window toolbar buttons depending on session and stream state
        chatStream = self.streamHandlerOfType("chat")
        if chatStream:
            chatStream.updateToolbarButtons(toolbar, got_proposal)
        else:
            updateToolbarButtonsWhileDisconnected(self, toolbar)

    def validateToolbarButton(self, item):
        # validate the Chat Window toolbar buttons depending on session and stream state
        chatStream = self.streamHandlerOfType("chat")
        if chatStream:
            return chatStream.validateToolbarButton(item)
        else:
            return validateToolbarButtonWhileDisconnected(self, item)

    def userClickedToolbarButton(self, sender):
        # process clicks on Chat Window toolbar buttons depending on session and stream state
        chatStream = self.streamHandlerOfType("chat")
        if chatStream:
            chatStream.userClickedToolbarButton(sender)
        else:
            userClickedToolbarButtonWhileDisconnected(self, sender)


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

