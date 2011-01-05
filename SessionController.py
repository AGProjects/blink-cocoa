# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#


from application.notification import IObserver, NotificationCenter
from application.python.util import Null

from datetime import datetime

from sipsimple.session import Session, IllegalStateError
from sipsimple.core import SIPURI, ToHeader
from sipsimple.util import TimestampedNotificationData

from zope.interface import implements

from AppKit import NSImage, NSRunAlertPanel, NSAlertDefaultReturn
from Foundation import NSObject
from AudioController import AudioController
from BaseStream import *
from BlinkLogger import BlinkLogger
from ChatController import ChatController, userClickedToolbarButtonWhileDisconnected, updateToolbarButtonsWhileDisconnected, validateToolbarButtonWhileDisconnected
from DesktopSharingController import DesktopSharingController, DesktopSharingServerController, DesktopSharingViewerController
from FileTransferController import FileTransferController

from SIPManager import SIPManager
from interfaces.itunes import ITunesInterface
from util import *

SessionIdentifierSerial = 0

TOOLBAR_RECONNECT = 100
TOOLBAR_AUDIO = 101
TOOLBAR_HOLD = 102
TOOLBAR_RECORD = 103
TOOLBAR_VIDEO = 104
TOOLBAR_SEND_FILE = 105
TOOLBAR_SMILEY = 106
TOOLBAR_HISTORY = 107
TOOLBAR_PARTICIPANTS = 108

TOOLBAR_DESKTOP_SHARING_BUTTON = 200
TOOLBAR_REQUEST_DESKTOP_MENU = 201
TOOLBAR_SHARE_DESKTOP_MENU = 202

PARTICIPANTS_MENU_ADD_CONTACT = 309

StreamHandlerForType = {
    "chat" : ChatController,
    "audio" : AudioController,
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
    originallyRequestedStreamTypes = None

    def initWithAccount_target_displayName_(self, account, target_uri, display_name):
        global SessionIdentifierSerial
        assert isinstance(target_uri, SIPURI)
        self = super(SessionController, self).init()
        self.contactDisplayName = display_name
        self.remoteParty = display_name or format_identity_simple(target_uri)
        self.remotePartyObject = target_uri
        self.account = account
        self.target_uri = target_uri
        self.remoteSIPAddress = format_identity_address(target_uri)
        SessionIdentifierSerial += 1
        self.identifier = SessionIdentifierSerial
        self.streamHandlers = []
        self.notification_center = NotificationCenter()
        self.cancelledStream = None
        self.remote_focus = False
        self.conference_info = None
        self.invited_participants = []
        self.mustShowDrawer = True

        # used for accounting
        self.streams_log = []
        self.participants_log = []
        self.remote_focus_log = False

        return self

    def initWithSession_(self, session):
        global SessionIdentifierSerial
        self = super(SessionController, self).init()
        self.contactDisplayName = None
        self.remoteParty = format_identity_simple(session.remote_identity)
        self.remotePartyObject = session.remote_identity
        self.account = session.account
        self.session = session
        self.target_uri = SIPURI.new(session.remote_identity.uri)
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
        self.mustShowDrawer = True

        # used for accounting
        self.streams_log = []
        self.participants_log = []
        self.remote_focus_log = False

        return self

    def isActive(self):
        return self.state in (STATE_CONNECTED, STATE_CONNECTING, STATE_DNS_LOOKUP)

    def handleIncomingStreams(self, streams, is_update=False):
        try:
            for s in streams:
                log_info(self, "Handling incoming %s Stream" % s.type)
                handler = StreamHandlerForType.get(s.type, None)
                if not handler:
                    BlinkLogger().log_warning("Unknown incoming Stream type: %s (%s)" % (s, s.type))
                    raise TypeError("Unsupported stream type %s" % s.type)
                else:
                    controller = handler(self, s)
                    self.streamHandlers.append(controller)
                    if s.type not in self.streams_log:
                        self.streams_log.append(s.type)
                    if self.answeringMachineMode and s.type == "audio":
                        controller.startIncoming(is_update=is_update, is_answering_machine=True)
                    else:
                        controller.startIncoming(is_update=is_update)

            if not is_update:
                self.session.accept(streams)
        except Exception, exc:
            import traceback
            traceback.print_exc()
            # if there was some exception, reject the session
            if is_update:
                BlinkLogger().log_error("Error initializing additional streams: %s" % exc)
            else:
                BlinkLogger().log_error("Error initializing incoming session, rejecting it: %s" % exc)
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
            log_info(self, "Ending Session (%s)"%str(self.session.streams))
            self.session.end()

    def endStream(self, streamHandler):
        if streamHandler.stream.type=="audio" and self.hasStreamOfType("desktop-sharing") and len(self.streamHandlers)==2:
            # end the whole ds+audio session
            log_info(self, "Ending the whole desktop sharing session")
            self.end()
            return True

        # we can get called in one of these situations
        # 1 - session established, streamHandler is the only stream
        # 2 - session established, streamHandler is one of many streams
        # 3 - session established, streamHandler is being proposed but not yet established
        # 4 - session not yet established

        if self.streamHandlers == [streamHandler]: # 1
            # end the whole session
            self.end()
            return True
        elif not self.streamHandlers and streamHandler.stream is None: # 3
            self.end()
            return True
        elif len(self.streamHandlers) > 1 and self.session.streams and streamHandler.stream in self.session.streams: # 2
            log_info(self, "Removing %s stream from session" % streamHandler.stream.type)
            try:
                self.session.remove_stream(streamHandler.stream)
                return True
            except IllegalStateError, e:
                log_error(self, "IllegalStateError: %s" % e)
                return False
        else: # 4
            if self.session.streams is None:
                self.end()
                return True
            return False

    def cancelProposal(self, stream):
        self.cancelledStream = stream
        try:
            self.session.cancel_proposal()
        except IllegalStateError, e:
            log_error(self, "IllegalStateError: %s" % e)

    @property
    def ended(self):
        return self.state in (STATE_FINISHED, STATE_FAILED, STATE_DNS_FAILED)

    def removeStreamHandler(self, streamHandler):
        if streamHandler not in self.streamHandlers:
            log_error(self, "Internal inconsistency: attempt to remove invalid stream handler")
            import traceback
            traceback.print_stack()
            return
        self.streamHandlers.remove(streamHandler)

        # notify Chat Window controller to update the toolbar buttons
        self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def changeSessionState(self, newstate, fail_reason=None):
        log_debug(self, "Changing Session state to " + newstate)
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
        self.participants_log = []
        self.streams_log = []
        self.mustShowDrawer = False

    def startBaseSession(self, account):
        if self.session is None:
            self.session = Session(account)
            self.notification_center.add_observer(self, sender=self.session)
            self.routes = None
            self.failureReason = None

    def startCompositeSessionWithStreamsOfTypes(self, stype_tuple):
        if self.state in (STATE_FINISHED, STATE_DNS_FAILED, STATE_FAILED):
            self.resetSession()

        new_session = False
        add_streams = []
        if self.session is None:
            # no session yet, initiate it
            self.startBaseSession(self.account)
            new_session = True

        self.originallyRequestedStreamTypes = []

        for stype in stype_tuple:
            if type(stype) == tuple:
                stype, kwargs = stype
            else:
                kwargs = {}

            if not self.hasStreamOfType(stype):
                if stype not in self.streams_log:
                    self.streams_log.append(stype)
                handlerClass = StreamHandlerForType[stype]
                stream = handlerClass.createStream(self.account)
                if not stream:
                    log_info(self, "Cancelled session")
                    return False
                controller = handlerClass(self, stream)
                self.streamHandlers.append(controller)
                controller.startOutgoing(not new_session, **kwargs)

                self.originallyRequestedStreamTypes.append(controller.stream.type)

                if not new_session:
                    log_debug(self, "Adding %s stream to session"%stype.capitalize())
                    # there is already a session, add audio stream to it
                    add_streams.append(controller.stream)

            else:
                print "Stream already exists: %s"%self.streamHandlers

        if new_session:
            log_debug(self, "Initiating DNS Lookup of %s to %s"%(self.account, self.target_uri))
            self.changeSessionState(STATE_DNS_LOOKUP)
            SIPManager().request_routes_lookup(self.account, self.target_uri, self)
            if any(streamHandler.stream.type=='audio' for streamHandler in self.streamHandlers):
                self.waitingForITunes = True
                itunes_interface = ITunesInterface()
                self.notification_center.add_observer(self, sender=itunes_interface)
                itunes_interface.pause()
            else:
                self.waitingForITunes = False
    
        else:
            for stream in add_streams:
                try:
                   self.session.add_stream(stream)
                except IllegalStateError, e:
                    log_error(self, "IllegalStateError: %s" % e)
                    return False
        return True

    def startSessionWithStreamOfType(self, stype, kwargs={}): # pyobjc doesn't like **kwargs
        return self.startCompositeSessionWithStreamsOfTypes(((stype, kwargs), ))

    def startAudioSession(self):
        return self.startSessionWithStreamOfType("audio")

    def startChatSession(self):
        return self.startSessionWithStreamOfType("chat")

    def offerFileTransfer(self, file_path, content_type=None):
        return self.startSessionWithStreamOfType("chat", {"file_path":file_path, "content_type":content_type})

    def offerDesktopSession(self):
        if not self.hasStreamOfType("desktop"):
            if NSRunAlertPanel("Desktop Sharing",
                "Would you like to allow %s to view the contents of your desktop?" % self.target_uri,
                "Share Desktop", "Cancel", None) != NSAlertDefaultReturn:
                return

            if self.session is None:
                # no session yet, initiate it
                self.startBaseSession(self.account)
                self.desktopRequested = True
            else:
                log_debug(self, "Adding Desktop Stream (server) to session")
                # there is already a session, add stream to it
                pass

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

    def addMyDesktopToSession(self):
        if not self.hasStreamOfType("desktop"):
            self.startSessionWithStreamOfType("desktop-server")

    def addRemoteDesktopToSession(self):
        if not self.hasStreamOfType("desktop"):
            self.startSessionWithStreamOfType("desktop-viewer")

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
        log_error(self, "DNS Lookup for SIP routes failed: '%s'"%msg)

        log_data = TimestampedNotificationData(target_uri=format_identity(self.target_uri, check_contact=True), timestamp=datetime.now(), code=478, failure_reason='DNS Lookup Failed', streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
        self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        self.changeSessionState(STATE_DNS_FAILED, msg)
        self.end()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        if self.routes != routes:
            log_info(self, "DNS Lookup returned %s"%routes)
            self.routes = routes

        if len(routes) == 0:
            self.changeSessionState(STATE_DNS_FAILED, u"No routes found to SIP proxy")
            log_error(self, "Session failed: No route found to SIP proxy")

            log_data = TimestampedNotificationData(target_uri=format_identity(self.target_uri, check_contact=True), timestamp=datetime.now(), code=478, failure_reason='No route found to SIP proxy', streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
            self.notification_center.post_notification("BlinkSessionDidFail", sender=self, data=log_data)

        elif not self.waitingForITunes:
            self.connectSession()

    def connectSession(self):
        if self.session:
            streams = [s.stream for s in self.streamHandlers]
            self.session.connect(ToHeader(self.target_uri), self.routes, streams)
            self.changeSessionState(STATE_CONNECTING)
            log_info(self, "Connecting Session...")

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_ITunesPauseDidExecute(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)
        self.waitingForITunes = False
        if self.routes:
            self.connectSession()

    def _NH_SIPSessionGotRingIndication(self, sender, data):
        for sc in self.streamHandlers:
            sc.sessionRinging()

    def _NH_SIPSessionWillStart(self, sender, data):
        log_info(self, "Session will start")

    def _NH_SIPSessionDidStart(self, sender, data):
        self.remoteParty = format_identity(self.session.remote_identity)
        if hasattr(self.session, "remote_focus") and self.session.remote_focus:
            self.remote_focus = True
            self.remote_focus_log = True
        else:
            # Remove any invited participants as the remote party does not support conferencing
            self.invited_participants = []
        self.mustShowDrawer = True
        self.changeSessionState(STATE_CONNECTED)
        log_info(self, "Session started")

    def _NH_SIPSessionWillEnd(self, sender, data):
        log_info(self, "Session will end (%s)"%data.originator)
        self.endingBy = data.originator

    def _NH_SIPSessionDidFail(self, sender, data):
        failureCode = data.code
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

        log_data = TimestampedNotificationData(target_uri=format_identity(self.target_uri, check_contact=True), timestamp=data.timestamp, code=data.code, failure_reason=data.failure_reason, streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
        self.notification_center.post_notification("BlinkSessionDidFail", sender=sender, data=log_data)

        log_error(self, "Session failed: "+status)

        self.changeSessionState(STATE_FAILED, status)

        oldSession = self.session
        self.notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.cancelledStream = None
        self.remote_focus = False
        self.remote_focus_log = False
        self.conference_info = None
        self.invited_participants = []
        self.participants_log = []
        self.streams_log = []
        self.mustShowDrawer = False

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

    def _NH_SIPSessionDidEnd(self, sender, data):
        self.changeSessionState(STATE_FINISHED, data.originator)
        log_info(self, "Session ended")

        log_data = TimestampedNotificationData(target_uri=format_identity(self.target_uri, check_contact=True), streams=self.streams_log, focus=self.remote_focus_log, participants=self.participants_log)
        self.notification_center.post_notification("BlinkSessionDidEnd", sender=sender, data=log_data)

        self.notification_center.remove_observer(self, sender=self.session)
        self.session = None
        self.cancelledStream = None
        self.remote_focus = False
        self.remote_focus_log = False
        self.conference_info = None
        self.invited_participants = []
        self.participants_log = []
        self.streams_log = []
        self.mustShowDrawer = False

        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)

    def _NH_SIPSessionGotProposal(self, sender, data):
        self.inProposal = True
        self.proposalOriginator = 'remote'
        if data.originator != "local":
            stream_names = ', '.join(stream.type for stream in data.streams)
            log_info(self, "Got a Stream proposal from %s with streams %s" % (sender.remote_identity, stream_names))
            self.owner.handle_incoming_proposal(sender, data.streams)

    def _NH_SIPSessionGotRejectProposal(self, sender, data):
        self.inProposal = False
        self.proposalOriginator = None
        log_info(self, "Proposal got rejected %s"%(data.reason))
        if data.streams:
            for stream in data.streams:
                if stream == self.cancelledStream:
                    self.cancelledStream = None
                if stream.type == "chat":
                    log_info(self, "Removing Chat Stream from Session")
                    handler = self.streamHandlerForStream(stream)
                    if handler:
                        handler.changeStatus(STREAM_FAILED, data.reason)
                elif stream.type == "audio":
                    log_info(self, "Removing Audio Stream from Session")
                    handler = self.streamHandlerForStream(stream)
                    if handler:
                        handler.changeStatus(STREAM_FAILED, data.reason)
                elif stream.type == "desktop-sharing":
                    log_info(self, "Removing Desktop Sharing Stream from Session")
                    handler = self.streamHandlerForStream(stream)
                    if handler:
                        handler.changeStatus(STREAM_FAILED, data.reason)
                else:
                    log_error(self, "Got reject proposal for unhandled stream type: %r" % stream)

            # notify Chat Window controller to update the toolbar buttons
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    def _NH_SIPSessionGotAcceptProposal(self, sender, data):
        self.inProposal = False
        self.proposalOriginator = None
        log_info(self, "Proposal accepted")
        if data.streams:
            for stream in data.streams:
                handler = self.streamHandlerForStream(stream)
                if not handler and self.cancelledStream == stream:
                    log_info(self, "Cancelled proposal for %s was accepted by remote, removing stream" % stream)
                    try:
                        self.session.remove_stream(stream)
                        self.cancelledStream = None
                    except IllegalStateError, e:
                        log_error(self, "IllegalStateError: %s" % e)
            # notify by Chat Window controller to update the toolbar buttons
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    def _NH_SIPSessionHadProposalFailure(self, sender, data):
        self.inProposal = False
        self.proposalOriginator = None
        log_info(self, "Proposal failure %s" % data)
        if data.streams:
            for stream in data.streams:
                if stream == self.cancelledStream:
                    self.cancelledStream = None
                log_info(self, "Removing %s stream from session" % stream.type)
                handler = self.streamHandlerForStream(stream)
                if handler:
                    handler.changeStatus(STREAM_FAILED, data.failure_reason)
                else:
                    log_error(self, "Got proposal failure for unhandled stream type: %r" % stream)

            # notify Chat Window controller to update the toolbar buttons
            self.notification_center.post_notification("BlinkStreamHandlersChanged", sender=self)

    def _NH_SIPSessionGotConferenceInfo(self, sender, data):
        self.conference_info = data.conference_info
        for user in data.conference_info.users:
            uri = user.entity.replace("sips:", "", 1)
            uri = uri.replace("sip:", "", 1)
 
           # save uri for accounting pusposes
            if uri not in self.participants_log:
                self.participants_log.append(uri)    

            # remove invited participants that joined the conference
            for contact in self.invited_participants:
                if uri == contact.uri:
                    self.invited_participants.remove(contact)

        # notify controllers who need conference information
        self.notification_center.post_notification("BlinkConferenceGotUpdate", sender=self)

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

