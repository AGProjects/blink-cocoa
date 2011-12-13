# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from WebKit import *
from Quartz import *

from zope.interface import implements
from application.notification import NotificationCenter, IObserver
from operator import attrgetter
from sipsimple.account import BonjourAccount
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.streams.applications.chat import CPIMIdentity
from urllib import unquote

from MediaStream import *
from ConferenceScreenSharing import ConferenceScreenSharing
from ConferenceFileCell import ConferenceFileCell
from ContactListModel import BlinkConferenceContact
from FileTransferSession import OutgoingPullFileTransferHandler
from FileTransferWindowController import openFileTransferSelectionDialog
import ParticipantsTableView
import SessionController
import ChatWindowManager
from ChatPrivateMessageController import ChatPrivateMessageController
from SIPManager import SIPManager

import FancyTabSwitcher
from util import allocate_autorelease_pool, format_identity_address, format_size_rounded

import os
import re
import time

PARTICIPANTS_MENU_ADD_CONFERENCE_CONTACT = 314
PARTICIPANTS_MENU_ADD_CONTACT = 301
PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE = 310
PARTICIPANTS_MENU_SEND_PRIVATE_MESSAGE = 311
PARTICIPANTS_MENU_MUTE = 315
PARTICIPANTS_MENU_INVITE_TO_CONFERENCE = 312
PARTICIPANTS_MENU_GOTO_CONFERENCE_WEBSITE = 313
PARTICIPANTS_MENU_START_AUDIO_SESSION = 320
PARTICIPANTS_MENU_START_CHAT_SESSION = 321
PARTICIPANTS_MENU_START_VIDEO_SESSION = 322
PARTICIPANTS_MENU_SEND_FILES = 323
PARTICIPANTS_MENU_VIEW_SCREEN = 324
PARTICIPANTS_MENU_SHOW_SESSION_INFO = 400

TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH = 401
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM = 403
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW = 402

SKIP_SCREENSHARING_FOR_APPS= ('SystemUIServer', 'Dock', 'Window Server')

class ChatWindowController(NSWindowController):
    implements(IObserver)

    tabView = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    desktopShareMenu = objc.IBOutlet()
    conferenceScreeningSharingMenu = objc.IBOutlet()
    screenshotShareMenu = objc.IBOutlet()
    conferenceScreenSharingQualityMenu = objc.IBOutlet()
    conferenceScreenSharingWindowsMenu = objc.IBOutlet()
    participantMenu = objc.IBOutlet()
    sharedFileMenu = objc.IBOutlet()
    drawer = objc.IBOutlet()
    participantsTableView = objc.IBOutlet()
    conferenceFilesTableView = objc.IBOutlet()
    drawerScrollView = objc.IBOutlet()
    drawerSplitView = objc.IBOutlet()
    actionsButton = objc.IBOutlet()
    editorButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    recordButton = objc.IBOutlet()
    audioStatus = objc.IBOutlet()

    conferenceFilesView = objc.IBOutlet()
    participantsView = objc.IBOutlet()

    timer = None

    def init(self):
        self = super(ChatWindowController, self).init()
        if self:
            self.participants = []
            self.conference_shared_files = []
            self.sessions = {}
            self.unreadMessageCounts = {}
            self.remoteScreens = {}
            # keep a reference to the controller object  because it may be used later by cocoa
            self.chat_controllers = set()

            NSBundle.loadNibNamed_owner_("ChatWindow", self)

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="AudioStreamDidStartRecordingAudio")
            self.notification_center.add_observer(self, name="AudioStreamDidStopRecordingAudio")
            self.notification_center.add_observer(self, name="BlinkAudioStreamChangedHoldState")
            self.notification_center.add_observer(self, name="BlinkColaborativeEditorContentHasChanged")
            self.notification_center.add_observer(self, name="BlinkConferenceGotUpdate")
            self.notification_center.add_observer(self, name="BlinkContactsHaveChanged")
            self.notification_center.add_observer(self, name="BlinkGotProposal")
            self.notification_center.add_observer(self, name="BlinkSentAddProposal")
            self.notification_center.add_observer(self, name="BlinkSentRemoveProposal")
            self.notification_center.add_observer(self, name="BlinkProposalGotRejected")
            self.notification_center.add_observer(self, name="BlinkMuteChangedState")
            self.notification_center.add_observer(self, name="BlinkSessionChangedState")
            self.notification_center.add_observer(self, name="BlinkStreamHandlerChangedState")
            self.notification_center.add_observer(self, name="BlinkStreamHandlersChanged")
            self.notification_center.add_observer(self, name="BlinkVideoEnteredFullScreen")
            self.notification_center.add_observer(self, name="BlinkVideoExitedFullScreen")

            self.backend = SIPManager()

            if self.backend.is_muted():
                self.muteButton.setImage_(NSImage.imageNamed_("muted"))
                self.muteButton.setState_(NSOnState)
            else:
                self.muteButton.setImage_(NSImage.imageNamed_("mute"))
                self.muteButton.setState_(NSOffState)

            self.setOwnIcon()

            if not SIPManager().isMediaTypeSupported('video'):
                for identifier in ('video', 'maximize'):
                    try:
                        item = (item for item in self.toolbar.visibleItems() if item.itemIdentifier() == identifier).next()
                        self.toolbar.removeItemAtIndex_(self.toolbar.visibleItems().index(item))
                    except StopIteration:
                        pass

        return self

    def addTimer(self):
        if not self.timer:
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSModalPanelRunLoopMode)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)

    def removeTimer(self):
        if self.timer:
           self.timer.invalidate()

    def setOwnIcon(self):
        self.own_icon = None
        path = NSApp.delegate().windowController.iconPathForSelf()
        if path:
            self.own_icon = NSImage.alloc().initWithContentsOfFile_(path)


    def updateTimer_(self, timer):
        # remove tile after few seconds to have time to see the reason in the drawer
        session = self.selectedSessionController()
        if session:
            change = False
            for uri in session.failed_to_join_participants.keys():
                for contact in session.invited_participants:
                    try:
                        uri_time = session.failed_to_join_participants[uri]
                        if uri == contact.uri and (time.time() - uri_time > 5):
                            session.invited_participants.remove(contact)
                            del session.failed_to_join_participants[uri]
                            change = True
                    except KeyError:
                        pass
            if change:
                self.refreshDrawer()

    def awakeFromNib(self):
        self.participantsTableView.registerForDraggedTypes_(NSArray.arrayWithObject_("x-blink-sip-uri"))
        self.participantsTableView.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
        self.conferenceFilesTableView.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "participantSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.participantsTableView)
        ns_nc.addObserver_selector_name_object_(self, "sharedFileSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.conferenceFilesTableView)
        ns_nc.addObserver_selector_name_object_(self, "drawerSplitViewDidResize:", NSSplitViewDidResizeSubviewsNotification, self.drawerSplitView)

        self.participantsTableView.setTarget_(self)
        self.participantsTableView.setDoubleAction_("doubleClickReceived:")
        self.conferenceFilesTableView.setTarget_(self)
        self.conferenceFilesTableView.setDoubleAction_("doubleClickReceived:")

    def splitView_shouldHideDividerAtIndex_(self, view, index):
        if self.conference_shared_files:
            return False
        return True

    def shouldPopUpDocumentPathMenu_(self, menu):
        return False

    def _findInactiveSessionCompatibleWith_(self, session):
        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
        session_contact = getContactMatchingURI(session.remoteSIPAddress)
        for k, s in self.sessions.iteritems():
            if s == session or s.identifier == session.identifier:
                return k, s
            if not s.isActive():
                contact = getContactMatchingURI(s.remoteSIPAddress)
                if s.remoteSIPAddress==session.remoteSIPAddress or session_contact==contact!=None:
                    return k, s
        else:
            return None, None

    def replaceInactiveWithCompatibleSession_(self, newSession):
        key, oldSession = self._findInactiveSessionCompatibleWith_(newSession)
        ok = False
        if oldSession:
            for item in self.tabView.tabViewItems():
                if item.identifier() == oldSession.identifier:
                    del self.sessions[oldSession.identifier]
                    self.sessions[newSession.identifier] = newSession

                    item.setView_(newSession.streamHandlerOfType("chat").getContentView())
                    item.setLabel_(newSession.getTitleShort())
                    self.tabView.selectTabViewItem_(item)
                    item.setIdentifier_(newSession.identifier)
                    ok = True
                    break
        return ok and oldSession or None

    def addSession_withView_(self, session, view):
        self.sessions[session.identifier] = session
        tabItem = NSTabViewItem.alloc().initWithIdentifier_(session.identifier)
        tabItem.setView_(view)
        tabItem.setLabel_(session.getTitleShort())

        self.tabSwitcher.addTabViewItem_(tabItem)
        self.tabSwitcher.selectLastTabViewItem_(None)

        chat_stream = session.streamHandlerOfType("chat")
        self.tabSwitcher.setTabViewItem_busy_(tabItem, chat_stream.isConnecting if chat_stream else False)

        self.updateTitle()
        if session.mustShowDrawer:
            self.drawer.open()
            self.participantsTableView.deselectAll_(self)

    def selectSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            raise Exception("Attempt to select invalid tab")
        self.tabView.selectTabViewItemWithIdentifier_(session.identifier)

    def hasSession_(self, session):
        return self.sessions.has_key(session.identifier)

    def detachWindow_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            return None
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        view = tabItem.view()
        view.removeFromSuperview()
        #self.tabView.removeTabViewItem_(tabItem)
        self.tabSwitcher.removeTabViewItem_(tabItem)
        del self.sessions[session.identifier]
        return view

    def removeSession_(self, session):
        if not self.detachWindow_(session):
            return False

        chat_stream = session.streamHandlerOfType("chat")
        if chat_stream:
            chat_stream.chatViewController.close()
            chat_stream.removeFromSession()
            chat_stream.handler.setDisconnected()

        return True

    def selectedSessionController(self):
        activeTab = self.tabView.selectedTabViewItem()
        if activeTab and self.sessions.has_key(activeTab.identifier()):
            return self.sessions[activeTab.identifier()]
        return None

    def updateTitle(self):
        title = self.getConferenceTitle()
        icon = None
        if title:
            self.window().setTitle_(title)
            self.window().setRepresentedURL_(NSURL.fileURLWithPath_(title))
            session = self.selectedSessionController()
            if session:
                try:
                    if session.session.transport == "tls":
                        icon = NSImage.imageNamed_("bluelock")
                        icon.setSize_(NSMakeSize(12, 12))
                except AttributeError:
                    pass

        self.window().standardWindowButton_(NSWindowDocumentIconButton).setImage_(icon)

    def window_shouldDragDocumentWithEvent_from_withPasteboard_(self, window, event, point, pasteboard):
        return False

    def getConferenceTitle(self):
        title = None
        session = self.selectedSessionController()
        if session:
            if session.conference_info is not None:
                conf_desc = session.conference_info.conference_description
                title = u"%s <%s>" % (conf_desc.display_text, format_identity_address(session.remotePartyObject)) if conf_desc.display_text else u"%s" % session.getTitleFull()
            else:
                title = u"%s" % session.getTitleShort() if isinstance(session.account, BonjourAccount) else u"%s" % session.getTitleFull()
        return title

    def noteSession_isComposing_(self, session, flag):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            item.setComposing_(flag)

    def noteSession_isScreenSharing_(self, session, flag):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            item.setScreenSharing_(flag)


    def noteNewMessageForSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        if self.tabView.selectedTabViewItem() == tabItem:
            return

        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            count = self.unreadMessageCounts[session.identifier] = self.unreadMessageCounts.get(session.identifier, 0) + 1
            item.setBadgeLabel_(str(count))

    def windowDidBecomeKey_(self, notification):
        session = self.selectedSessionController()

        if session and session.streamHandlerOfType("chat"):
            self.window().makeFirstResponder_(session.streamHandlerOfType("chat").chatViewController.inputText)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        name = notification.name
        sender = notification.sender

        if name == "BlinkStreamHandlerChangedState":
            session = sender.sessionController
            if session:
                chat_stream = session.streamHandlerOfType("chat")
                if chat_stream:
                    index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
                    if index != NSNotFound:
                        tabItem = self.tabView.tabViewItemAtIndex_(index)
                        self.tabSwitcher.setTabViewItem_busy_(tabItem, chat_stream.isConnecting)
            self.revalidateToolbar()
        elif name == "BlinkSessionChangedState":
            session = sender
            if session:
                chat_stream = session.streamHandlerOfType("chat")
                if chat_stream:
                    index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
                    if index != NSNotFound:
                        tabItem = self.tabView.tabViewItemAtIndex_(index)
                        self.tabSwitcher.setTabViewItem_busy_(tabItem, chat_stream.isConnecting)
            self.revalidateToolbar()
            self.refreshDrawer()

            # Update drawer status when not connected
            state = notification.data['state']
            detail = notification.data['reason']
            if state == STATE_CONNECTING:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setHidden_(False)
                self.audioStatus.setStringValue_(u"Connecting...")
            elif state == STATE_CONNECTED:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setHidden_(False)
                self.audioStatus.setStringValue_(u"Connected")
            elif state == STATE_FINISHED:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setHidden_(True)
                self.audioStatus.setStringValue_('')

        elif name == "BlinkAudioStreamChangedHoldState":
            self.revalidateToolbar()
            self.refreshDrawer()
        elif name == "BlinkStreamHandlersChanged":
            self.revalidateToolbar()
            self.refreshDrawer()
        elif name == "BlinkVideoEnteredFullScreen":
            self.toolbar.setVisible_(False)
        elif name == "BlinkVideoExitedFullScreen":
            self.toolbar.setVisible_(True)
        elif name in( "AudioStreamDidStartRecordingAudio", "AudioStreamDidStopRecordingAudio"):
            self.revalidateToolbar()
        elif name in ("BlinkGotProposal", "BlinkProposalGotRejected", "BlinkSentAddProposal", "BlinkSentRemoveProposal"):
            self.revalidateToolbar()
            self.refreshDrawer()
        elif name == "BlinkConferenceGotUpdate":
            self.refreshDrawer()
        elif name == "BlinkContactsHaveChanged":
            self.setOwnIcon()
            self.refreshDrawer()
        elif name == "BlinkMuteChangedState":
            if self.backend.is_muted():
                self.muteButton.setImage_(NSImage.imageNamed_("muted"))
                self.muteButton.setState_(NSOnState)
            else:
                self.muteButton.setState_(NSOffState)
                self.muteButton.setImage_(NSImage.imageNamed_("mute"))
        elif name == "BlinkColaborativeEditorContentHasChanged":
            session = self.selectedSessionController()
            if not sender.editorStatus:
                self.noteSession_isComposing_(sender.delegate.sessionController, True)
            self.revalidateToolbar()

    def validateToolbarItem_(self, item):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            return selectedSession.validateToolbarButton(item)
        else:
            return False

    @objc.IBAction
    def selectScreenSharingWindow_(self, sender):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chat_stream = selectedSession.streamHandlerOfType("chat")
            if chat_stream:
                wob = sender.representedObject()
                id = wob['id']
                name = wob['name']
                application = wob['application']
                if chat_stream.screensharing_handler:
                    selectedSession.log_info('Selecting %s for screen sharing' % application)
                    chat_stream.screensharing_handler.window_id = id
                    i = 6
                    while i < self.conferenceScreeningSharingMenu.numberOfItems():
                        item = self.conferenceScreeningSharingMenu.itemAtIndex_(i)
                        item.setState_(NSOnState if item.representedObject()['id'] == id else NSOffState)
                        i += 1

    @objc.IBAction
    def close_(self, sender):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            if len(self.sessions) == 1:
                self.window().close()
                self.notification_center.remove_observer(self, name="AudioStreamDidStartRecordingAudio")
                self.notification_center.remove_observer(self, name="AudioStreamDidStopRecordingAudio")
                self.notification_center.remove_observer(self, name="BlinkAudioStreamChangedHoldState")
                self.notification_center.remove_observer(self, name="BlinkColaborativeEditorContentHasChanged")
                self.notification_center.remove_observer(self, name="BlinkConferenceGotUpdate")
                self.notification_center.remove_observer(self, name="BlinkContactsHaveChanged")
                self.notification_center.remove_observer(self, name="BlinkGotProposal")
                self.notification_center.remove_observer(self, name="BlinkMuteChangedState")
                self.notification_center.remove_observer(self, name="BlinkSessionChangedState")
                self.notification_center.remove_observer(self, name="BlinkStreamHandlerChangedState")
                self.notification_center.remove_observer(self, name="BlinkStreamHandlersChanged")
            chat_stream = selectedSession.streamHandlerOfType("chat")
            if chat_stream:
                chat_stream.closeTab()
            else:
                self.detachWindow_(selectedSession)

    def windowDidExpose_(self, sender):
        self.addTimer()

    def windowShouldClose_(self, sender):
        active = len([s for s in self.sessions.values() if s.hasStreamOfType("chat")])

        if active > 1:
            ret = NSRunAlertPanel(u"Close Chat Window",
                                  u"There are %i Chat sessions, click Close to terminate them all." % active,
                                  u"Close", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return False


        self.window().close()
        for s in self.sessions.values(): # we need a copy of the dict contents as it will change as a side-effect of removeSession_()
            chat_stream = s.streamHandlerOfType("chat")
            if chat_stream:
                chat_stream.closeTab()
                chat_stream.exitFullScreen()

            self.removeSession_(s)

        self.notification_center.post_notification("BlinkChatWindowClosed", sender=self)
        self.removeTimer()

        return True

    def joinConferenceWindow(self, session, participants=[]):
        media = []
        if session.hasStreamOfType("chat"):
            media.append("chat")
        if session.hasStreamOfType("audio"):
            media.append("audio")

        if format_identity_address(session.remotePartyObject) not in participants:
            participants.append(format_identity_address(session.remotePartyObject))

        conference = NSApp.delegate().windowController.showJoinConferenceWindow(participants=participants, media=media)
        if conference is not None:
            NSApp.delegate().windowController.joinConference(conference.target, conference.media_types, conference.participants)

    def getSelectedParticipant(self):
        row = self.participantsTableView.selectedRow()
        if not self.participantsTableView.isRowSelected_(row):
            return None

        try:
            return self.participants[row]
        except IndexError:
            return None

    def isConferenceParticipant(self, uri):
        session = self.selectedSessionController()
        if session and hasattr(session.conference_info, "users"):
            for user in session.conference_info.users:
                participant = re.sub("^(sip:|sips:)", "", user.entity)
                if participant == uri:
                    return True

        return False

    def isInvitedParticipant(self, uri):
        session = self.selectedSessionController()
        try:
           return uri in (contact.uri for contact in session.invited_participants)
        except AttributeError:
           return False

    def participantSelectionChanged_(self, notification):
        contact = self.getSelectedParticipant()
        session = self.selectedSessionController()

        if not session or contact is None:
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_ADD_CONTACT).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_MUTE).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_PRIVATE_MESSAGE).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_AUDIO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_CHAT_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_VIDEO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_FILES).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_VIEW_SCREEN).setEnabled_(False)

        else:
            own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)
            remote_uri = format_identity_address(session.remotePartyObject)

            hasContactMatchingURI = NSApp.delegate().windowController.hasContactMatchingURI
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_ADD_CONTACT).setEnabled_(False if (hasContactMatchingURI(contact.uri) or contact.uri == own_uri or isinstance(session.account, BonjourAccount)) else True)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE).setEnabled_(True if self.canBeRemovedFromConference(contact.uri) else False)

            if remote_uri != contact.uri and own_uri != contact.uri and session.hasStreamOfType("chat") and self.isConferenceParticipant(contact.uri):
                chat_stream = session.streamHandlerOfType("chat")
                stream_supports_screen_sharing = chat_stream.screensharing_allowed
                self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_PRIVATE_MESSAGE).setEnabled_(True if chat_stream.stream.private_messages_allowed and 'message' in contact.active_media else False)
            else:
                stream_supports_screen_sharing = False
                self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_PRIVATE_MESSAGE).setEnabled_(False)

            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_VIEW_SCREEN).setEnabled_(True if stream_supports_screen_sharing and contact.uri != own_uri and not isinstance(session.account, BonjourAccount) and (contact.screensharing_url is not None or self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_VIEW_SCREEN).state == NSOnState) else False)

            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_AUDIO_SESSION).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_CHAT_SESSION).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_VIDEO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_FILES).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)

    def sharedFileSelectionChanged_(self, notification):
        session = self.selectedSessionController()
        if not session:
            self.sharedFileMenu.itemWithTag_(100).setEnabled_(False)
        else:
            row = self.conferenceFilesTableView.selectedRow()
            if row == -1:
                return
            conference_file = self.conference_shared_files[row]
            self.sharedFileMenu.itemWithTag_(100).setEnabled_(conference_file.file.status == 'OK')

    def resizeDrawerSplitter(self):
        session = self.selectedSessionController()
        if session:
            chat_stream = session.streamHandlerOfType("chat")
            if chat_stream and chat_stream.drawerSplitterPosition is not None:
                self.conferenceFilesView.setFrame_(chat_stream.drawerSplitterPosition['topFrame'])
                self.participantsView.setFrame_(chat_stream.drawerSplitterPosition['bottomFrame'])
            else:
                frame = self.conferenceFilesView.frame()
                frame.size.height = 0
                self.conferenceFilesView.setFrame_(frame)

    def drawerSplitViewDidResize_(self, notification):
        if notification.userInfo() is not None:
            session = self.selectedSessionController()
            if session:
                chat_stream = session.streamHandlerOfType("chat")
                if chat_stream:
                    chat_stream.drawerSplitterPosition = {'topFrame': self.conferenceFilesView.frame(), 'bottomFrame': self.participantsView.frame() }

    def sendPrivateMessage(self):
        session = self.selectedSessionController()
        if session:
            row = self.participantsTableView.selectedRow()
            try:
                contact = self.participants[row]
            except IndexError:
                return

            try:
                recipient = CPIMIdentity(SIPURI.parse('sip:%s' % contact.uri), display_name=contact.display_name)
            except SIPCoreError:
                return

            controller = ChatPrivateMessageController(contact)
            message = controller.runModal()

            if message:
                chat_stream = session.streamHandlerOfType("chat")
                chat_stream.handler.send(message, recipient, True)

    def canGoToConferenceWebsite(self):
        session = self.selectedSessionController()
        if session.conference_info and session.conference_info.host_info and session.conference_info.host_info.web_page:
            return True
        return False

    def canBeRemovedFromConference(self, uri):
        session = self.selectedSessionController()
        own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)
        return session and (self.isConferenceParticipant(uri) or self.isInvitedParticipant(uri)) and own_uri != uri

    def removeParticipant(self, uri):
        session = self.selectedSessionController()
        if session:
            # remove uri from invited participants
            try:
               contact = (contact for contact in session.invited_participants if contact.uri == uri).next()
            except StopIteration:
               pass
            else:
               try:
                   session.invited_participants.remove(contact)
               except ValueError:
                   pass

            if session.remote_focus and self.isConferenceParticipant(uri):
                session.log_info(u"Request server for removal of %s from conference" % uri)
                session.pending_removal_participants.add(uri)
                session.session.conference.remove_participant(uri)

            self.participantsTableView.deselectAll_(self)
            self.refreshDrawer()

    def addParticipants(self):
        session = self.selectedSessionController()
        if session:
            if session.remote_focus:
                participants = NSApp.delegate().windowController.showAddParticipantsWindow(target=self.getConferenceTitle(), default_domain=session.account.id.domain)
                if participants is not None:
                    getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
                    remote_uri = format_identity_address(session.remotePartyObject)
                    # prevent loops
                    if remote_uri in participants:
                        participants.remove(remote_uri)
                    for uri in participants:
                        if uri and "@" not in uri:
                            uri='%s@%s' % (uri, session.account.id.domain)
                        contact = getContactMatchingURI(uri)
                        if contact:
                            contact = BlinkConferenceContact(uri, name=contact.name, icon=contact.icon)
                        else:
                            contact = BlinkConferenceContact(uri, name=uri)
                        contact.setDetail('Invitation sent...')
                        if contact not in session.invited_participants:
                            session.invited_participants.append(contact)
                            session.participants_log.add(uri)
                            session.log_info(u"Invite %s to conference" % uri)
                            session.session.conference.add_participant(uri)

                self.refreshDrawer()
            else:
                self.joinConferenceWindow(session)

    @objc.IBAction
    def printDocument_(self, sender):
        session = self.selectedSessionController()
        chat_stream = session.streamHandlerOfType("chat")
        if session:
            print_view = chat_stream.chatViewController.outputView if chat_stream else session.lastChatOutputView
            if print_view:
                printInfo = NSPrintInfo.sharedPrintInfo()
                printInfo.setTopMargin_(30)
                printInfo.setBottomMargin_(30)
                printInfo.setLeftMargin_(10)
                printInfo.setRightMargin_(10)
                printInfo.setOrientation_(NSPortraitOrientation)
                printInfo.setHorizontallyCentered_(True)
                printInfo.setVerticallyCentered_(False)
                printInfo.setHorizontalPagination_(NSFitPagination)
                printInfo.setVerticalPagination_(NSFitPagination)
                NSPrintInfo.setSharedPrintInfo_(printInfo)

                # print the content of the web view
                print_view.mainFrame().frameView().documentView().print_(self)
 
    @objc.IBAction
    def userClickedActionsButton_(self, sender):
        point = sender.convertPointToBase_(NSZeroPoint)
        point.x += 30
        point.y -= 10
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                    NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                    sender.window().graphicsContext(), 0, 1, 0)
        NSMenu.popUpContextMenu_withEvent_forView_(self.participantMenu, event, sender)

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        if sender.tag() == 200: # desktop sharing menu button
            for item in self.desktopShareMenu.itemArray():
                item.setEnabled_(self.validateToolbarItem_(item))

            point = sender.convertPointToBase_(NSZeroPoint)
            point.y -= NSHeight(sender.frame())
            event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(), sender.window().graphicsContext(),
                0, 1, 0)
            session = self.selectedSessionController()
            if session:
                if session.remote_focus:
                    NSMenu.popUpContextMenu_withEvent_forView_(self.conferenceScreeningSharingMenu, event, sender)
                else:
                    NSMenu.popUpContextMenu_withEvent_forView_(self.desktopShareMenu, event, sender)
            return

        elif sender.tag() == 300: # screenshot sharing menu button
            point = sender.convertPointToBase_(NSZeroPoint)
            point.y -= NSHeight(sender.frame())
            event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(), sender.window().graphicsContext(),
                0, 1, 0)
            NSMenu.popUpContextMenu_withEvent_forView_(self.screenshotShareMenu, event, sender)
            return

        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            selectedSession.userClickedToolbarButton(sender)

    @objc.IBAction
    def useClickedRemoveFromConference_(self, sender):
        session = self.selectedSessionController()
        if session:
            row = self.participantsTableView.selectedRow()
            try:
                object = self.participants[row]
            except IndexError:
                return
            uri = object.uri
            self.removeParticipant(uri)

    def menuWillOpen_(self, menu):
        if menu == self.participantMenu:
            session = self.selectedSessionController()
            if session:
                self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SHOW_SESSION_INFO).setEnabled_(True if session.session is not None and session.session.state is not None else False)
                self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SHOW_SESSION_INFO).setTitle_('Hide Session Information' if session.info_panel is not None and session.info_panel.window.isVisible() else 'Show Session Information')
            else:
                self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SHOW_SESSION_INFO).setEnabled_(False)
                self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SHOW_SESSION_INFO).setTitle_('Show Session Information')

            item = menu.itemWithTag_(PARTICIPANTS_MENU_VIEW_SCREEN)
            row = self.participantsTableView.selectedRow()
            try:
                object = self.participants[row]
                uri = object.uri
                item.setState_(NSOnState if self.remoteScreens.has_key(uri) else NSOffState)
                item.setEnabled_(True if 'screen' in object.active_media else False)
            except IndexError:
                item.setState_(NSOnState if self.remoteScreens.has_key(uri) else NSOffState)
                item.setEnabled_(False)

        elif menu == self.conferenceScreenSharingQualityMenu:
            session = self.selectedSessionController()
            if session and session.hasStreamOfType("chat"):
                chat_stream = session.streamHandlerOfType("chat")
                if chat_stream.screensharing_handler is not None and chat_stream.screensharing_handler.connected:
                    item = self.conferenceScreenSharingQualityMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH)
                    item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'high' else NSOffState)
                    item.setEnabled_(True)
                    item = self.conferenceScreenSharingQualityMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW)
                    item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'low' else NSOffState)
                    item.setEnabled_(True)
                    item = self.conferenceScreenSharingQualityMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM)
                    item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'medium' else NSOffState)
                    item.setEnabled_(True)

        elif menu == self.conferenceScreenSharingWindowsMenu:
            while self.conferenceScreenSharingWindowsMenu.numberOfItems() > 0:
                self.conferenceScreenSharingWindowsMenu.removeItemAtIndex_(0)

            selected_window = None
            selectedSession = self.selectedSessionController()
            if selectedSession:
                chat_stream = selectedSession.streamHandlerOfType("chat")
                if chat_stream and chat_stream.screensharing_handler:
                    selected_window = chat_stream.screensharing_handler.window_id

            item = self.conferenceScreenSharingWindowsMenu.addItemWithTitle_action_keyEquivalent_('Entire Desktop', "selectScreenSharingWindow:", "")
            obj = {'application': 'entire desktop', 'id': None, 'name': None}
            item.setRepresentedObject_(obj)
            item.setState_(NSOnState if selected_window is None else NSOffState)

            listOptions = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
            windowList = CGWindowListCopyWindowInfo(listOptions, kCGNullWindowID)
            i = 0
            while i < windowList.count():
                wob = windowList.objectAtIndex_(i)
                id = wob.objectForKey_(kCGWindowNumber)
                application = wob.objectForKey_(kCGWindowOwnerName)
                name = wob.objectForKey_(kCGWindowName)
                onscreen = wob.objectForKey_(kCGWindowIsOnscreen)
                bounds = wob.objectForKey_(kCGWindowBounds)
                width = bounds.objectForKey_('Width')
                if onscreen and width >= 64 and application not in SKIP_SCREENSHARING_FOR_APPS:
                    if application != name:
                        title = "%s (%s)" % (application, name or id)
                    else:
                        title = "%s (%d)" % (application, id)
                    item = self.conferenceScreenSharingWindowsMenu.addItemWithTitle_action_keyEquivalent_(title, "selectScreenSharingWindow:", "")
                    obj = {'id': id, 'name': name, 'application': application}
                    item.setRepresentedObject_(obj)
                    item.setState_(NSOnState if selected_window == id else NSOffState)

                    item = self.conferenceScreenSharingWindowsMenu.addItemWithTitle_action_keyEquivalent_(title, "selectScreenSharingWindow:", "")
                    obj = {'id': id, 'name': name, 'application': application}
                    item.setRepresentedObject_(obj)
                    item.setState_(NSOnState if selected_window == id else NSOffState)
                i += 1
        elif menu == self.conferenceScreeningSharingMenu:
            while self.conferenceScreeningSharingMenu.numberOfItems() > 6:
                self.conferenceScreeningSharingMenu.removeItemAtIndex_(6)

            selected_window = None
            selectedSession = self.selectedSessionController()
            if selectedSession:
                chat_stream = selectedSession.streamHandlerOfType("chat")
                if chat_stream:
                    if chat_stream.screensharing_handler:
                        selected_window = chat_stream.screensharing_handler.window_id

                    if chat_stream.screensharing_handler is not None and chat_stream.screensharing_handler.connected:
                        for i in (1,2,3,4,5):
                            item = self.conferenceScreeningSharingMenu.itemAtIndex_(i)
                            item.setHidden_(False)

                        item = self.conferenceScreeningSharingMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH)
                        item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'high' else NSOffState)
                        item.setEnabled_(True)
                        item = self.conferenceScreeningSharingMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW)
                        item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'low' else NSOffState)
                        item.setEnabled_(True)
                        item = self.conferenceScreeningSharingMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM)
                        item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'medium' else NSOffState)
                        item.setEnabled_(True)

                        item = self.conferenceScreeningSharingMenu.addItemWithTitle_action_keyEquivalent_('Entire Desktop', "selectScreenSharingWindow:", "")
                        obj = {'application': 'entire desktop', 'id': None, 'name': None}
                        item.setRepresentedObject_(obj)
                        item.setIndentationLevel_(2)
                        item.setState_(NSOnState if selected_window is None else NSOffState)                        
                        listOptions = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
                        windowList = CGWindowListCopyWindowInfo(listOptions, kCGNullWindowID)
                        i = 0
                        while i < windowList.count():
                            wob = windowList.objectAtIndex_(i)
                            id = wob.objectForKey_(kCGWindowNumber)
                            application = wob.objectForKey_(kCGWindowOwnerName)
                            name = wob.objectForKey_(kCGWindowName)
                            onscreen = wob.objectForKey_(kCGWindowIsOnscreen)
                            bounds = wob.objectForKey_(kCGWindowBounds)
                            width = bounds.objectForKey_('Width')
                            if onscreen and width >= 64 and application not in SKIP_SCREENSHARING_FOR_APPS:
                                if application != name:
                                    title = "%s (%s)" % (application, name or id)
                                else:
                                    title = "%s (%d)" % (application, id)
                                item = self.conferenceScreeningSharingMenu.addItemWithTitle_action_keyEquivalent_(title, "selectScreenSharingWindow:", "")
                                obj = {'id': id, 'name': name, 'application': application}
                                item.setRepresentedObject_(obj)
                                item.setIndentationLevel_(2)
                                item.setState_(NSOnState if selected_window == id else NSOffState)
                            i += 1
                    else:
                        for i in (1,2,3,4,5):
                            item = self.conferenceScreeningSharingMenu.itemAtIndex_(i)
                            item.setHidden_(True)

    @objc.IBAction
    def userClickedParticipantMenu_(self, sender):
        session = self.selectedSessionController()
        if session:
            tag = sender.tag()

            row = self.participantsTableView.selectedRow()
            try:
                object = self.participants[row]
            except IndexError:
                return

            uri = object.uri
            display_name = object.display_name
            screensharing_url = object.screensharing_url

            if tag == PARTICIPANTS_MENU_ADD_CONTACT:
                NSApp.delegate().windowController.addContact(uri, display_name)
            elif tag == PARTICIPANTS_MENU_ADD_CONFERENCE_CONTACT:
                remote_uri = format_identity_address(session.remotePartyObject)
                display_name = None
                if session.conference_info is not None:
                    conf_desc = session.conference_info.conference_description
                    display_name = unicode(conf_desc.display_text)
                NSApp.delegate().windowController.addContact(remote_uri, display_name)
            elif tag == PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE:
                ret = NSRunAlertPanel(u"Remove from conference", u"You will request the conference server to remove %s from the room. Are your sure?" % display_name, u"Remove", u"Cancel", None)
                if ret == NSAlertDefaultReturn:
                    self.removeParticipant(uri)
            elif tag == PARTICIPANTS_MENU_INVITE_TO_CONFERENCE:
                self.addParticipants()
            elif tag == PARTICIPANTS_MENU_SEND_PRIVATE_MESSAGE:
                self.sendPrivateMessage()
            elif tag == PARTICIPANTS_MENU_GOTO_CONFERENCE_WEBSITE:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(session.conference_info.host_info.web_page.value))
            elif tag == PARTICIPANTS_MENU_START_AUDIO_SESSION:
                NSApp.delegate().windowController.startSessionWithAccount(session.account, uri, "audio")
            elif tag == PARTICIPANTS_MENU_START_VIDEO_SESSION:
                NSApp.delegate().windowController.startSessionWithAccount(session.account, uri, "video")
            elif tag == PARTICIPANTS_MENU_START_CHAT_SESSION:
                NSApp.delegate().windowController.startSessionWithAccount(session.account, uri, "chat")
            elif tag == PARTICIPANTS_MENU_VIEW_SCREEN:
                try:
                    remoteScreen = self.remoteScreens[uri]
                except KeyError:
                    self.viewSharedScreen(uri, display_name, screensharing_url)
                else:
                    remoteScreen.close_(None)
                sender.setState_(NSOffState if sender.state() == NSOnState else NSOnState)
            elif tag == PARTICIPANTS_MENU_SEND_FILES:
                openFileTransferSelectionDialog(session.account, uri)
            elif tag == PARTICIPANTS_MENU_SHOW_SESSION_INFO:
                session.info_panel.toggle()

    def viewSharedScreen(self, uri, display_name, url):
        session = self.selectedSessionController()
        if session:
            session.log_info(u"Opening Shared Screen of %s from %s" % (uri, unquote(url)))
            remoteScreen = ConferenceScreenSharing.createWithOwner_(self)
            remoteScreen.showSharedScreen(display_name, uri, unquote(url))
            self.remoteScreens[uri] = remoteScreen

    @objc.IBAction
    def userClickedSharedFileMenu_(self, sender):
        self.requestFileTransfer()

    @objc.IBAction
    def doubleClickReceived_(self, sender):
        if sender == self.conferenceFilesTableView:
            self.requestFileTransfer()

    def requestFileTransfer(self):
        session = self.selectedSessionController()
        if session:
            row = self.conferenceFilesTableView.selectedRow()
            if row == -1:
                return
            conference_file = self.conference_shared_files[row]
            file = conference_file.file
            if file.status != 'OK':
                return
            session.log_info(u"Request transfer of file %s with hash %s from %s" % (file.name, file.hash, session.remoteSIPAddress))
            transfer_handler = OutgoingPullFileTransferHandler(session.account, session.target_uri, file.name.encode('utf-8'), file.hash)
            transfer_handler.start()

    @objc.IBAction
    def muteClicked_(self, sender):
        if sender != self.muteButton:
            if self.backend.is_muted():
                self.muteButton.setState_(NSOffState)
            else:
                self.muteButton.setState_(NSOnState)
        if self.muteButton.state() == NSOnState:
            self.backend.mute(True)
            self.muteButton.setImage_(NSImage.imageNamed_("muted"))
        else:
            self.backend.mute(False)
            self.muteButton.setImage_(NSImage.imageNamed_("mute"))

        self.notification_center.post_notification("BlinkMuteChangedState", sender=self)

    def revalidateToolbar(self, got_proposal=False):
        # update the toolbar buttons depending on session and stream state
        if self.tabView.selectedTabViewItem():
            identifier = self.tabView.selectedTabViewItem().identifier()
            try:
                self.sessions[identifier].updateToolbarButtons(self.toolbar, got_proposal)
            except KeyError:
                pass
            self.toolbar.validateVisibleItems()

    def refreshDrawer(self):
        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI

        self.participants = []

        self.updateTitle()

        session = self.selectedSessionController()
        if session:
            if session.account is BonjourAccount():
                own_uri = '%s@%s' % (session.account.uri.user, session.account.uri.host)
            else:
                own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)

            chat_stream = session.streamHandlerOfType("chat")
                
            if session.hasStreamOfType("audio"):
                audio_stream = session.streamHandlerOfType("audio")

            if session.conference_info is None or (session.conference_info is not None and not session.conference_info.users):
                active_media = []

                if session.hasStreamOfType("chat") and chat_stream.status == STREAM_CONNECTED:
                        active_media.append('message')

                if session.hasStreamOfType("audio"):
                    active_media.append('audio' if not audio_stream.holdByLocal else 'audio-onhold')

                # Add ourselves
                contact = BlinkConferenceContact(own_uri, name=session.account.display_name, icon=self.own_icon)
                contact.setActiveMedia(active_media)
                self.participants.append(contact)

                # Add remote party
                contact = getContactMatchingURI(session.remoteSIPAddress)
                if contact:
                    contact = BlinkConferenceContact(contact.uri, name=contact.name, icon=contact.icon)
                else:
                    uri = format_identity_address(session.remotePartyObject)
                    display_name = session.getTitleShort()
                    contact = BlinkConferenceContact(uri, name=display_name)

                if session.state == STATE_DNS_LOOKUP:
                    contact.setDetail("Finding Destination...")
                elif session.state == STATE_CONNECTING:
                    contact.setDetail("Connecting...")
                else:
                    contact.setDetail(contact.uri)

                active_media = []

                if session.hasStreamOfType("chat") and chat_stream.status == STREAM_CONNECTED:
                    active_media.append('message')

                if session.hasStreamOfType("audio"):
                    active_media.append('audio' if not audio_stream.holdByRemote else 'audio-onhold')

                contact.setActiveMedia(active_media)
                self.participants.append(contact)

            # Add conference participants if any
            if session.conference_info is not None:
                for user in session.conference_info.users:
                    uri = re.sub("^(sip:|sips:)", "", user.entity)
                    contact = getContactMatchingURI(uri)
                    if contact:
                        display_name = user.display_text.value if user.display_text is not None and user.display_text.value else contact.name
                        contact = BlinkConferenceContact(uri, name=display_name, icon=contact.icon)
                    else:
                        display_name = user.display_text.value if user.display_text is not None and user.display_text.value else uri
                        contact = BlinkConferenceContact(uri, name=display_name)

                    active_media = []

                    chat_endpoints = [endpoint for endpoint in user if any(media.media_type == 'message' for media in endpoint)]
                    if chat_endpoints:
                        active_media.append('message')

                    if user.screen_image_url is not None:
                        active_media.append('screen')
                        contact.setScreensharingUrl(user.screen_image_url.value)

                    audio_endpoints = [endpoint for endpoint in user if any(media.media_type == 'audio' for media in endpoint)]
                    user_on_hold = all(endpoint.status == 'on-hold' for endpoint in audio_endpoints)
                    if audio_endpoints and not user_on_hold:
                        active_media.append('audio')
                    elif audio_endpoints and user_on_hold:
                        active_media.append('audio-onhold')

                    contact.setActiveMedia(active_media)
                    # detail will be reset on receival of next conference-info update
                    if uri in session.pending_removal_participants:
                        contact.setDetail('Removal requested...')

                    if own_uri and self.own_icon and contact.uri == own_uri:
                        contact.setIcon(self.own_icon)

                    if contact not in self.participants:
                        self.participants.append(contact)

            self.participants.sort(key=attrgetter('name'))

            # Add invited participants if any
            if session.invited_participants:
                for contact in session.invited_participants:
                    self.participants.append(contact)
 
            self.participantsTableView.reloadData()

            # Update drawer status
            if session.hasStreamOfType("audio"):
                if audio_stream.holdByLocal:
                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    self.audioStatus.setStringValue_(u"On Hold")
                    self.audioStatus.setHidden_(False)
                elif audio_stream.holdByRemote:
                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    self.audioStatus.setStringValue_(u"Hold by Remote")
                    self.audioStatus.setHidden_(False)
                elif audio_stream.status ==  STREAM_CONNECTED:
                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    self.audioStatus.setStringValue_(u"%s (%s)" % ("HD Audio" if audio_stream.stream.sample_rate > 8000 else "Audio", audio_stream.stream.codec))
                    self.audioStatus.setHidden_(False)

            elif session.hasStreamOfType("chat") and chat_stream.status == STREAM_CONNECTED:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setStringValue_(u"Connected")
                self.audioStatus.setHidden_(False)
            else:
                self.audioStatus.setHidden_(False)
                self.audioStatus.setStringValue_(u"Not Connected")

            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_INVITE_TO_CONFERENCE).setEnabled_(False if isinstance(session.account, BonjourAccount) else True)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_GOTO_CONFERENCE_WEBSITE).setEnabled_(True if self.canGoToConferenceWebsite() else False)

            hasContactMatchingURI = NSApp.delegate().windowController.hasContactMatchingURI
            remote_uri = format_identity_address(session.remotePartyObject)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_ADD_CONFERENCE_CONTACT).setEnabled_(False if hasContactMatchingURI(remote_uri) else True)

            column_header_title = u'Participants'
            if session.conference_info is not None:
                column_header_title = u'%d Participants' % len(self.participants) if len(self.participants) > 1 else u'Participants'

            self.participantsTableView.tableColumnWithIdentifier_('participant').headerCell(). setStringValue_(column_header_title)

            self.conference_shared_files = []

            for file in reversed(session.conference_shared_files):
                item = ConferenceFile(file)
                self.conference_shared_files.append(item)

            self.conferenceFilesTableView.reloadData()

            if session.conference_shared_files:
                column_header_title = u'%d Remote Conference Files' % len(self.conference_shared_files) if len(self.conference_shared_files) > 1 else u'Remote Conference Files'
                if chat_stream and chat_stream.drawerSplitterPosition is None:
                    top_frame = self.conferenceFilesView.frame()
                    top_frame.size.height = 130
                    bottom_frame = self.participantsView.frame()
                    bottom_frame.size.height = bottom_frame.size.height - 130
                    chat_stream.drawerSplitterPosition = {'topFrame': top_frame, 'bottomFrame': bottom_frame}
            else:
                column_header_title = u'Remote Conference Files'
                if chat_stream:
                    chat_stream.drawerSplitterPosition = None

            self.conferenceFilesTableView.tableColumnWithIdentifier_('files').headerCell(). setStringValue_(column_header_title)

            self.resizeDrawerSplitter()

    def drawerDidOpen_(self, notification):
        session = self.selectedSessionController()
        if session:
            session.mustShowDrawer = True

    def drawerDidClose_(self, notification):
        session = self.selectedSessionController()
        if session:
            session.mustShowDrawer = False

    def tabViewDidChangeNumberOfTabViewItems_(self, tabView):
        if tabView.numberOfTabViewItems() == 0:
            self.window().performClose_(None)

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if self.sessions.has_key(item.identifier()):
            self.revalidateToolbar()
            self.updateTitle()
            session = self.sessions[item.identifier()]
            if session.mustShowDrawer:
                self.refreshDrawer()
                self.drawer.open()
                self.participantsTableView.deselectAll_(self)
                self.conferenceFilesTableView.deselectAll_(self)
            else:
                self.drawer.close()

            if session.hasStreamOfType("audio") and not session.inProposal:
                audio_stream = session.streamHandlerOfType("audio")
                if audio_stream.holdByLocal:
                    audio_stream.unhold()
                    audio_stream.view.setSelected_(True)

        self.unreadMessageCounts[item.identifier()] = 0
        sitem = self.tabSwitcher.itemForTabViewItem_(item)
        if sitem:
            sitem.setBadgeLabel_("")

    def tabView_shouldCloseTabViewItem_(self, tabView, item):
        if self.sessions.has_key(item.identifier()):
            chat_stream = self.sessions[item.identifier()].streamHandlerOfType("chat")
            if chat_stream:
                chat_stream.closeTab()
                return False
        return True

    def tabView_didDettachTabViewItem_atPosition_(self, tabView, item, pos):
        if len(self.sessions) > 1:
            session = self.sessions[item.identifier()]

            window = ChatWindowManager.ChatWindowManager().dettachChatWindow(session)
            if window:
                window.window().setFrameOrigin_(pos)
                self.refreshDrawer()

    # TableView dataSource
    def numberOfRowsInTableView_(self, tableView):
        if tableView == self.participantsTableView:
            try:
                return len(self.participants)
            except:
                pass
        elif tableView == self.conferenceFilesTableView:
            return len(self.conference_shared_files)

        return 0

    def tableView_objectValueForTableColumn_row_(self, tableView, tableColumn, row):
        if tableView == self.participantsTableView:
            try:
                if row < len(self.participants):
                    if type(self.participants[row]) in (str, unicode):
                        return self.participants[row]
                    else:
                        return self.participants[row].name
            except:
                pass
        elif tableView == self.conferenceFilesTableView:
            if row < len(self.conference_shared_files):
                return self.conference_shared_files[row].name
        return None
        
    def tableView_willDisplayCell_forTableColumn_row_(self, tableView, cell, tableColumn, row):
        if tableView == self.participantsTableView:
            try:
                if row < len(self.participants):
                    if type(self.participants[row]) in (str, unicode):
                        cell.setContact_(None)
                    else:
                        cell.setContact_(self.participants[row])
            except:
                pass
        elif tableView == self.conferenceFilesTableView:
            if row < len(self.conference_shared_files):
                cell.conference_file = self.conference_shared_files[row]

    # drag/drop
    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        session = self.selectedSessionController()
        if session:
            if session.remote_focus:
                # do not allow drag if remote party is not conference focus
                pboard = info.draggingPasteboard()
                if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
                    uri = str(pboard.stringForType_("x-blink-sip-uri"))
                    if uri:
                        uri = re.sub("^(sip:|sips:)", "", str(uri))
                    try:
                        table.setDropRow_dropOperation_(self.numberOfRowsInTableView_(table), NSTableViewDropAbove)
                        
                        # do not invite remote party itself
                        remote_uri = format_identity_address(session.remotePartyObject)
                        if uri == remote_uri:
                            return NSDragOperationNone
                        # do not invite users already invited
                        for contact in session.invited_participants:
                            if uri == contact.uri:
                                return NSDragOperationNone
                        # do not invite users already present in the conference
                        if session.conference_info is not None:
                            for user in session.conference_info.users:
                                if uri == re.sub("^(sip:|sips:)", "", user.entity):
                                    return NSDragOperationNone
                    except:
                        return NSDragOperationNone
                    return NSDragOperationAll
                elif pboard.types().containsObject_(NSFilenamesPboardType):
                    return NSDragOperationAll
            elif not isinstance(session.account, BonjourAccount):
                return NSDragOperationAll

        return NSDragOperationNone

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        session = self.selectedSessionController()

        if not session:
            return False

        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            if uri:
                uri = re.sub("^(sip:|sips:)", "", str(uri))
                if "@" not in uri:
                    uri = '%s@%s' % (uri, session.account.id.domain)

            if session.remote_focus:
                getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
                contact = getContactMatchingURI(uri)
                if contact:
                    contact = BlinkConferenceContact(uri, name=contact.name, icon=contact.icon)
                else:
                    contact = BlinkConferenceContact(uri, name=uri)
                contact.setDetail('Invitation sent...')
                session.invited_participants.append(contact)
                session.participants_log.add(uri)
                self.refreshDrawer()
                session.log_info(u"Invite %s to conference" % uri)
                session.session.conference.add_participant(uri)
            elif not isinstance(session.account, BonjourAccount):
                self.joinConferenceWindow(session, [uri])
            return True
        elif pboard.types().containsObject_(NSFilenamesPboardType):
            chat_controller = session.streamHandlerOfType("chat")
            ws = NSWorkspace.sharedWorkspace()
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            return chat_controller.sendFiles(fnames)


class ConferenceFile(NSObject):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, file):
        self.file = file
        name, ext = os.path.splitext(file.name)
        self.icon = NSWorkspace.sharedWorkspace().iconForFileType_(ext.strip('.'))

    def copyWithZone_(self, zone):
        return self

    @property
    def name(self):
        return NSString.stringWithString_('%s (%s)'% (self.file.name, format_size_rounded(self.file.size) if self.file.status == 'OK' else 'failed'))

    @property
    def sender(self):
        return NSString.stringWithString_(re.sub("^(sip:|sips:)", "", self.file.sender))


