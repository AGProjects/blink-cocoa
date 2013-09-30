# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSAlertDefaultReturn,
                    NSApp,
                    NSCommandKeyMask,
                    NSDragOperationAll,
                    NSDragOperationNone,
                    NSEventTrackingRunLoopMode,
                    NSFilenamesPboardType,
                    NSFitPagination,
                    NSLeftMouseUp,
                    NSModalPanelRunLoopMode,
                    NSOffState,
                    NSOnState,
                    NSPortraitOrientation,
                    NSRunAlertPanel,
                    NSTableViewDropAbove,
                    NSTableViewSelectionDidChangeNotification,
                    NSSplitViewDidResizeSubviewsNotification,
                    NSStringPboardType,
                    NSWindowDocumentIconButton)
from Foundation import (CFURLCreateStringByAddingPercentEscapes,
                        kCFStringEncodingUTF8,
                        NSArray,
                        NSBundle,
                        NSColor,
                        NSDate,
                        NSDefaultRunLoopMode,
                        NSEvent,
                        NSImage,
                        NSIndexSet,
                        NSMakeSize,
                        NSMenu,
                        NSMenuItem,
                        NSNotFound,
                        NSNotificationCenter,
                        NSObject,
                        NSPasteboard,
                        NSPrintInfo,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSString,
                        NSTimer,
                        NSTabViewItem,
                        NSURL,
                        NSWindowController,
                        NSWorkspace,
                        NSZeroPoint)
from Quartz import (CGWindowListCopyWindowInfo,
                    kCGNullWindowID,
                    kCGWindowBounds,
                    kCGWindowIsOnscreen,
                    kCGWindowListOptionOnScreenOnly,
                    kCGWindowListExcludeDesktopElements,
                    kCGWindowName,
                    kCGWindowNumber,
                    kCGWindowOwnerName)
import objc

import os
import time

from application.notification import NotificationCenter, IObserver
from application.python import Null
from itertools import chain
from operator import attrgetter
from sipsimple.account import BonjourAccount
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.util import ISOTimestamp
from sipsimple.streams.applications.chat import CPIMIdentity
from sipsimple.configuration.settings import SIPSimpleSettings
from urllib import unquote
from zope.interface import implements

import FancyTabSwitcher
import ParticipantsTableView
from BlinkLogger import BlinkLogger
from ChatPrivateMessageController import ChatPrivateMessageController
from MediaStream import STREAM_PROPOSING, STREAM_RINGING, STREAM_CONNECTED
from MediaStream import STATE_CONNECTING, STATE_CONNECTED, STATE_DNS_LOOKUP
from ConferenceScreenSharing import ConferenceScreenSharing
from ConferenceFileCell import ConferenceFileCell
from ContactListModel import BlinkConferenceContact, BlinkPresenceContact, BlinkMyselfConferenceContact
from FileTransferSession import OutgoingPullFileTransferHandler
from FileTransferWindowController import openFileTransferSelectionDialog
from NicknameController import NicknameController
from SIPManager import SIPManager
from SmileyManager import SmileyManager
from SubjectController import SubjectController
from util import allocate_autorelease_pool, format_identity_to_string, format_size_rounded, sip_prefix_pattern, beautify_audio_codec, run_in_gui_thread


CONFERENCE_ROOM_MENU_ADD_CONFERENCE_CONTACT = 314
CONFERENCE_ROOM_MENU_ADD_CONTACT = 301
CONFERENCE_ROOM_MENU_REMOVE_FROM_CONFERENCE = 310
CONFERENCE_ROOM_MENU_SEND_PRIVATE_MESSAGE = 311
CONFERENCE_ROOM_MENU_MUTE = 315
CONFERENCE_ROOM_MENU_NICKNAME = 316
CONFERENCE_ROOM_MENU_SUBJECT = 317
CONFERENCE_ROOM_MENU_COPY_ROOM_TO_CLIPBOARD = 318
CONFERENCE_ROOM_MENU_COPY_PARTICIPANT_TO_CLIPBOARD = 319
CONFERENCE_ROOM_MENU_SEND_EMAIL = 325
CONFERENCE_ROOM_MENU_INVITE_TO_CONFERENCE = 312
CONFERENCE_ROOM_MENU_GOTO_CONFERENCE_WEBSITE = 313
CONFERENCE_ROOM_MENU_START_AUDIO_SESSION = 320
CONFERENCE_ROOM_MENU_START_CHAT_SESSION = 321
CONFERENCE_ROOM_MENU_START_VIDEO_SESSION = 322
CONFERENCE_ROOM_MENU_SEND_FILES = 323
CONFERENCE_ROOM_MENU_VIEW_SCREEN = 324
CONFERENCE_ROOM_MENU_SHOW_SESSION_INFO = 400

TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE = 201
TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL = 202
TOOLBAR_SCREENSHARING_MENU_CANCEL = 203

TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH = 401
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM = 403
TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW = 402

SKIP_SCREENSHARING_FOR_APPS= ('SystemUIServer', 'Dock', 'Window Server')


class ChatWindowController(NSWindowController):
    implements(IObserver)

    tabView = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    screenShareMenu = objc.IBOutlet()
    conferenceScreenSharingMenu = objc.IBOutlet()
    participantMenu = objc.IBOutlet()
    encryptionMenu = objc.IBOutlet()
    sharedFileMenu = objc.IBOutlet()
    drawer = objc.IBOutlet()
    participantsTableView = objc.IBOutlet()
    conferenceFilesTableView = objc.IBOutlet()
    drawerScrollView = objc.IBOutlet()
    drawerSplitView = objc.IBOutlet()
    screenSharingPopUpButton = objc.IBOutlet()
    actionsButton = objc.IBOutlet()
    editorButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    recordButton = objc.IBOutlet()
    audioStatus = objc.IBOutlet()
    micLevelIndicator = objc.IBOutlet()
    encryptionIconMenuItem = objc.IBOutlet()

    conferenceFilesView = objc.IBOutlet()
    participantsView = objc.IBOutlet()
    refresh_drawer_counter = 1

    contact_timer = None

    def init(self):
        self = super(ChatWindowController, self).init()
        if self:
            BlinkLogger().log_info('Starting Chat Window Controller')
            smileys = SmileyManager()
            self.closing = False
            self.participants = []
            self.conference_shared_files = []
            self.remote_screens_closed_by_user = set()
            self.sessions = {}
            self.stream_controllers = {}
            self.unreadMessageCounts = {}
            self.remoteScreens = {}
            # keep a reference to the controller object  because it may be used later by cocoa

            NSBundle.loadNibNamed_owner_("ChatWindow", self)

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="AudioStreamDidStartRecordingAudio")
            self.notification_center.add_observer(self, name="AudioStreamDidStopRecordingAudio")
            self.notification_center.add_observer(self, name="BonjourAccountPresenceStateDidChange")
            self.notification_center.add_observer(self, name="BlinkAudioStreamChangedHoldState")
            self.notification_center.add_observer(self, name="BlinkShouldTerminate")
            self.notification_center.add_observer(self, name="BlinkCollaborationEditorContentHasChanged")
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
            self.notification_center.add_observer(self, name="BlinkDidRenegotiateStreams")
            self.notification_center.add_observer(self, name="BlinkVideoEnteredFullScreen")
            self.notification_center.add_observer(self, name="BlinkVideoExitedFullScreen")
            self.notification_center.add_observer(self, name="BlinkConferenceContactPresenceHasChanged")
            self.notification_center.add_observer(self, name="SIPAccountGotSelfPresenceState")
            self.notification_center.add_observer(self, name="SIPAccountDidDeactivate")
            self.notification_center.add_observer(self, name="SIPApplicationWillEnd")

            ns_nc = NSNotificationCenter.defaultCenter()
            ns_nc.addObserver_selector_name_object_(self, "participantSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.participantsTableView)
            ns_nc.addObserver_selector_name_object_(self, "sharedFileSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.conferenceFilesTableView)
            ns_nc.addObserver_selector_name_object_(self, "drawerSplitViewDidResize:", NSSplitViewDidResizeSubviewsNotification, self.drawerSplitView)

            self.refresh_drawer_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.3, self, "refreshDrawerTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.refresh_drawer_timer, NSModalPanelRunLoopMode)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.refresh_drawer_timer, NSDefaultRunLoopMode)

            self.backend = SIPManager()

            if self.backend.is_muted():
                self.muteButton.setImage_(NSImage.imageNamed_("muted"))
                self.muteButton.setState_(NSOnState)
            else:
                self.muteButton.setImage_(NSImage.imageNamed_("mute"))
                self.muteButton.setState_(NSOffState)

            self.setOwnIcon()

            if not self.sessionControllersManager.isMediaTypeSupported('video'):
                for identifier in ('video', 'maximize'):
                    for idx, item in enumerate(self.toolbar.visibleItems()):
                        if item.itemIdentifier() == identifier:
                            self.toolbar.removeItemAtIndex_(idx)
                            break

        return self

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    def addTimer(self):
        if not self.contact_timer:
            self.contact_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateContactTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.contact_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.contact_timer, NSEventTrackingRunLoopMode)

    def removeContactTimer(self):
        if self.contact_timer:
           self.contact_timer.invalidate()

    def setOwnIcon(self):
        self.own_icon = None
        path = NSApp.delegate().contactsWindowController.iconPathForSelf()
        if path:
            self.own_icon = NSImage.alloc().initWithContentsOfFile_(path)

    def setScreenSharingToolbarIconSize(self):
        frame = self.screenSharingPopUpButton.frame()
        frame.size.height = 38
        frame.size.width = 54
        frame.origin.y = 14
        self.screenSharingPopUpButton.setFrame_(frame)

    def updateContactTimer_(self, timer):
        # remove tile after few seconds to have time to see the reason in the drawer
        session = self.selectedSessionController()
        if session:
            change = False
            for uri in session.failed_to_join_participants.keys():
                for contact in session.invited_participants:
                    try:
                        uri_time = session.failed_to_join_participants[uri]
                        if uri == contact.uri and (time.time() - uri_time > 5):
                            session.log_info('Removing %s from list of invited partipants' % uri)
                            session.invited_participants.remove(contact)
                            contact.destroy()
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
        self.participantsTableView.setTarget_(self)
        self.participantsTableView.setDoubleAction_("doubleClickReceived:")
        self.conferenceFilesTableView.setTarget_(self)
        self.conferenceFilesTableView.setDoubleAction_("doubleClickReceived:")
        self.setScreenSharingToolbarIconSize()

    def splitView_shouldHideDividerAtIndex_(self, view, index):
        if self.conference_shared_files:
            return False
        return True

    def shouldPopUpDocumentPathMenu_(self, menu):
        return False

    def _findInactiveSessionCompatibleWith_(self, session):
        session_contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(session.remoteSIPAddress)
        for k, s in self.sessions.iteritems():
            if s == session or s.identifier == session.identifier:
                return k, s
            if not s.isActive():
                contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(s.remoteSIPAddress)
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
        self.stream_controllers[tabItem] = session.streamHandlerOfType("chat")
        tabItem.setView_(view)
        tabItem.setLabel_(session.getTitleShort())
        self.tabSwitcher.addTabViewItem_(tabItem)
        self.tabSwitcher.selectLastTabViewItem_(None)

        chat_stream = session.streamHandlerOfType("chat")
        self.tabSwitcher.setTabViewItem_busy_(tabItem, chat_stream.isConnecting if chat_stream else False)
        if chat_stream and chat_stream.isConnecting:
            chat_stream.chatViewController.loadingProgressIndicator.startAnimation_(None)
        else:
            chat_stream.chatViewController.loadingProgressIndicator.stopAnimation_(None)


        self.updateTitle()
        if session.mustShowDrawer:
            self.drawer.open()
            NSApp.delegate().contactsWindowController.drawer.close()
            self.participantsTableView.deselectAll_(self)

    def removeSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            return None
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        view = tabItem.view()
        view.removeFromSuperview()
        tabItem.setView_(None)
        self.tabSwitcher.removeTabViewItem_(tabItem)
        try:
            del self.stream_controllers[tabItem]
        except KeyError:
            pass

        try:
            del self.sessions[session.identifier]
        except KeyError:
            pass

    def selectSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            raise Exception("Attempt to select invalid tab")
        self.tabView.selectTabViewItemWithIdentifier_(session.identifier)

    def hasSession_(self, session):
        return self.sessions.has_key(session.identifier)

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
                if session.subject is not None:
                    title = u"%s" % session.subject
                else:
                    conf_desc = session.conference_info.conference_description
                    title = u"%s <%s>" % (conf_desc.display_text, format_identity_to_string(session.remotePartyObject)) if conf_desc.display_text else u"%s" % session.getTitleFull()
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
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_BlinkShouldTerminate(self, sender, data):
        if self.window():
            self.window().orderOut_(self)

    def _NH_SIPApplicationWillEnd(self, sender, data):
        if self.refresh_drawer_timer:
            self.refresh_drawer_timer.invalidate()
        if self.contact_timer:
            self.contact_timer.invalidate()

    def _NH_BonjourAccountPresenceStateDidChange(self, sender, data):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            if selectedSession.account == sender:
                self.refreshDrawer()

    def _NH_SIPAccountGotSelfPresenceState(self, sender, data):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            if selectedSession.account == sender:
                self.refreshDrawer()

    def _NH_SIPAccountDidDeactivate(self, sender, data):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            if selectedSession.account == sender:
                self.refreshDrawer()

    def _NH_BlinkStreamHandlerChangedState(self, sender, data):
        session = sender.sessionController
        if session:
            chat_stream = session.streamHandlerOfType("chat")
            if chat_stream:
                index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
                if index != NSNotFound:
                    tabItem = self.tabView.tabViewItemAtIndex_(index)
                    self.tabSwitcher.setTabViewItem_busy_(tabItem, chat_stream.isConnecting)
                    if chat_stream.isConnecting:
                        chat_stream.chatViewController.loadingProgressIndicator.startAnimation_(None)
                    else:
                        chat_stream.chatViewController.loadingProgressIndicator.stopAnimation_(None)
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkSessionChangedState(self, sender, data):
        session = sender
        if session:
            chat_stream = session.streamHandlerOfType("chat")
            if chat_stream:
                index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
                if index != NSNotFound:
                    tabItem = self.tabView.tabViewItemAtIndex_(index)
                    self.tabSwitcher.setTabViewItem_busy_(tabItem, chat_stream.isConnecting)
                    if chat_stream.isConnecting:
                        chat_stream.chatViewController.loadingProgressIndicator.startAnimation_(None)
                    else:
                        chat_stream.chatViewController.loadingProgressIndicator.stopAnimation_(None)
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkAudioStreamChangedHoldState(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkStreamHandlersChanged(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkDidRenegotiateStreams(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkVideoEnteredFullScreen(self, sender, data):
        self.toolbar.setVisible_(False)

    def _NH_BlinkVideoExitedFullScreen(self, sender, data):
        self.toolbar.setVisible_(True)

    def _NH_AudioStreamDidStartRecordingAudio(self, sender, data):
        self.revalidateToolbar()

    def _NH_AudioStreamDidStopRecordingAudio(self, sender, data):
        self.revalidateToolbar()

    def _NH_BlinkGotProposal(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkProposalGotRejected(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkSentAddProposal(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkSentRemoveProposal(self, sender, data):
        self.revalidateToolbar()
        self.refreshDrawer()

    def _NH_BlinkConferenceGotUpdate(self, sender, data):
        self.refreshDrawer()

    def _NH_BlinkContactsHaveChanged(self, sender, data):
        self.setOwnIcon()
        self.refreshDrawer()

    def _NH_BlinkMuteChangedState(self, sender, data):
        if self.backend.is_muted():
            self.muteButton.setImage_(NSImage.imageNamed_("muted"))
            self.muteButton.setState_(NSOnState)
        else:
            self.muteButton.setState_(NSOffState)
            self.muteButton.setImage_(NSImage.imageNamed_("mute"))

    def _NH_BlinkCollaborationEditorContentHasChanged(self, sender, data):
        if not sender.editorVisible:
            self.noteSession_isComposing_(sender.delegate.sessionController, True)
        self.revalidateToolbar()

    def validateToolbarItem_(self, item):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chatStream = selectedSession.streamHandlerOfType("chat")
            if chatStream:
                return chatStream.validateToolbarButton(item)
        else:
            return False

    def _NH_BlinkConferenceContactPresenceHasChanged(self, sender, data):
        try:
            idx = self.participants.index(sender)
            self.participantsTableView.reloadDataForRowIndexes_columnIndexes_(NSIndexSet.indexSetWithIndex_(idx), NSIndexSet.indexSetWithIndex_(0))
        except ValueError:
            pass

    @objc.IBAction
    def stopConferenceScreenSharing_(self, sender):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chat_stream = selectedSession.streamHandlerOfType("chat")
            if chat_stream and chat_stream.screensharing_allowed:
                if chat_stream.screensharing_handler and chat_stream.screensharing_handler.connected:
                    chat_stream.toggleScreensharingWithConferenceParticipants()
                    chat_stream.screensharing_handler.setWindowId(None)

                i = 7
                while i < self.conferenceScreenSharingMenu.numberOfItems() - 2:
                    item = self.conferenceScreenSharingMenu.itemAtIndex_(i)
                    item.setState_(NSOffState)
                    i += 1

    @objc.IBAction
    def selectConferenceScreenSharingWindow_(self, sender):
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chat_stream = selectedSession.streamHandlerOfType("chat")
            if chat_stream and chat_stream.screensharing_allowed:
                wob = sender.representedObject()
                id = wob['id']
                application = wob['application']
                if chat_stream.screensharing_handler and chat_stream.screensharing_handler.connected:
                    if id != chat_stream.screensharing_handler.window_id:
                        selectedSession.log_info('Selecting %s for screen sharing' % application)
                        chat_stream.screensharing_handler.setWindowId(id)
                    elif sender.state() == NSOnState:
                        chat_stream.toggleScreensharingWithConferenceParticipants()
                        chat_stream.screensharing_handler.setWindowId(None)
                else:
                    chat_stream.toggleScreensharingWithConferenceParticipants()
                    chat_stream.screensharing_handler.setWindowId(id)

                i = 7
                while i < self.conferenceScreenSharingMenu.numberOfItems() - 2:
                    item = self.conferenceScreenSharingMenu.itemAtIndex_(i)
                    item.setState_(NSOnState if item.representedObject()['id'] == id else NSOffState)
                    i += 1

    def keyDown_(self, event):
        if (event.modifierFlags() & NSCommandKeyMask):
            keys = event.characters()
            session = self.selectedSessionController()
            if keys[0] == 'i' and session and session.info_panel is not None:
                session.info_panel.toggle()
        else:
            super(ChatWindowController, self).keyDown_(event)

    def close_(self, sender):
        chat_sessions = len([s for s in self.sessions.values() if s.hasStreamOfType("chat")])
        if chat_sessions > 1:
            selectedSession = self.selectedSessionController()
            if selectedSession:
                chat_stream = selectedSession.streamHandlerOfType("chat")
                if chat_stream:
                    chat_stream.closeTab()
        else:
            self.window().performClose_(None)

    def windowWillClose_(self, sender):
        self.removeContactTimer()

    def windowShouldClose_(self, sender):
        active = len([s for s in self.sessions.values() if s.hasStreamOfType("chat")])
        if active > 1:
            ret = NSRunAlertPanel(u"Close Chat Window",
                                  u"There are %i Chat sessions, click Close to terminate them all." % active,
                                  u"Close", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return False

        self.window().close()
        self.closing = True

        # close active sessions
        for s in self.sessions.values(): # we need a copy of the dict contents as it will change as a side-effect of removeSession_()
            chat_stream = s.streamHandlerOfType("chat")
            if chat_stream:
                chat_stream.closeTab()

        # close idle sessions
        for chat_stream in self.stream_controllers.values():
            if chat_stream:
                chat_stream.closeTab()

        self.closing = False
        self.sessions = {}
        self.stream_controllers = {}
        self.notification_center.post_notification("BlinkChatWindowClosed", sender=self)

        return True

    def joinConferenceWindow(self, session, participants=[]):
        media_type = []
        if session.hasStreamOfType("chat"):
            media_type.append("chat")
        if session.hasStreamOfType("audio"):
            media_type.append("audio")

        if format_identity_to_string(session.remotePartyObject) not in participants:
            participants.append(format_identity_to_string(session.remotePartyObject))

        conference = NSApp.delegate().contactsWindowController.showJoinConferenceWindow(participants=participants, media_type=media_type, autostart=True)
        if conference is not None:
            NSApp.delegate().contactsWindowController.joinConference(conference.target, conference.media_type, conference.participants, conference.nickname)

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
                participant = sip_prefix_pattern.sub("", user.entity)
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
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_ADD_CONTACT).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_REMOVE_FROM_CONFERENCE).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_MUTE).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_PRIVATE_MESSAGE).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_NICKNAME).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SUBJECT).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_COPY_ROOM_TO_CLIPBOARD).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_EMAIL).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_COPY_PARTICIPANT_TO_CLIPBOARD).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_START_AUDIO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_START_CHAT_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_START_VIDEO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_FILES).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_VIEW_SCREEN).setEnabled_(False)

        else:
            own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)
            remote_uri = format_identity_to_string(session.remotePartyObject)

            hasContactMatchingURI = NSApp.delegate().contactsWindowController.hasContactMatchingURI
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_ADD_CONTACT).setEnabled_(False if (hasContactMatchingURI(contact.uri) or contact.uri == own_uri or isinstance(session.account, BonjourAccount)) else True)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_REMOVE_FROM_CONFERENCE).setEnabled_(True if self.canBeRemovedFromConference(contact.uri) else False)

            if remote_uri != contact.uri and own_uri != contact.uri and session.hasStreamOfType("chat") and self.isConferenceParticipant(contact.uri):
                chat_stream = session.streamHandlerOfType("chat")
                stream_supports_screen_sharing = chat_stream.screensharing_allowed
                self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_PRIVATE_MESSAGE).setEnabled_(True if chat_stream.stream.private_messages_allowed and 'message' in contact.active_media else False)
            else:
                stream_supports_screen_sharing = False
                self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_PRIVATE_MESSAGE).setEnabled_(False)

            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_VIEW_SCREEN).setEnabled_(True if stream_supports_screen_sharing and contact.uri != own_uri and not isinstance(session.account, BonjourAccount) and (contact.screensharing_url is not None or self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_VIEW_SCREEN).state == NSOnState) else False)

            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_START_AUDIO_SESSION).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_START_CHAT_SESSION).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_START_VIDEO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_FILES).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_COPY_PARTICIPANT_TO_CLIPBOARD).setEnabled_(True)

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
                recipient = CPIMIdentity(SIPURI.parse('sip:%s' % str(contact.uri)), display_name=contact.name)
            except SIPCoreError:
                return

            controller = ChatPrivateMessageController(contact)
            message = controller.runModal()
            controller.release()

            if message:
                chat_stream = session.streamHandlerOfType("chat")
                chat_stream.outgoing_message_handler.send(message, recipient, True)


    def setNickname(self):
        session = self.selectedSessionController()
        if session:
            chat_handler = session.streamHandlerOfType("chat")
            if chat_handler:
                controller = NicknameController()
                nickname = controller.runModal(session.nickname)
                if nickname or (not nickname and session.nickname):
                    chat_handler.setNickname(nickname)

    def setSubject(self):
        session = self.selectedSessionController()
        if session:
            chat_handler = session.streamHandlerOfType("chat")
            if chat_handler:
                controller = SubjectController()
                subject = controller.runModal(session.subject)
                if chat_handler.stream:
                    body = u'SUBJECT %s' % subject if subject else u'SUBJECT'
                    chat_handler.stream.send_message(body, content_type='sylkserver/control', timestamp=ISOTimestamp.now())
                if subject or (not subject and session.subject):
                    session.subject = subject if subject else None

    def canGoToConferenceWebsite(self):
        session = self.selectedSessionController()
        if session.conference_info and session.conference_info.host_info and session.conference_info.host_info.web_page:
            return True
        return False

    def canSetNickname(self):
        session = self.selectedSessionController()
        if session is not None and session.hasStreamOfType("chat"):
            chat_handler = session.streamHandlerOfType("chat")
            try:
                return chat_handler.stream.nickname_allowed
            except Exception:
                pass
        return False

    def canSetSubject(self):
        session = self.selectedSessionController()
        if session is not None and session.hasStreamOfType("chat"):
            chat_handler = session.streamHandlerOfType("chat")
            try:
                return chat_handler.control_allowed
            except Exception:
                pass
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
               else:
                   contact.destroy()

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
                participants = NSApp.delegate().contactsWindowController.showAddParticipantsWindow(target=self.getConferenceTitle(), default_domain=session.account.id.domain)
                if participants is not None:
                    remote_uri = format_identity_to_string(session.remotePartyObject)
                    # prevent loops
                    if remote_uri in participants:
                        participants.remove(remote_uri)
                    for uri in participants:
                        if uri and "@" not in uri:
                            uri='%s@%s' % (uri, session.account.id.domain)

                        try:
                            sip_uri = 'sip:%s' % uri if not uri.startswith("sip:") else uri
                            sip_uri = SIPURI.parse(sip_uri)
                        except SIPCoreError:
                            session.log_info(u"Error inviting to conference: invalid URI %s" % uri)
                            continue

                        presence_contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(uri)
                        if presence_contact:
                            contact = BlinkConferenceContact(uri, name=presence_contact.name, icon=presence_contact.icon, presence_contact=presence_contact)
                        else:
                            contact = BlinkConferenceContact(uri, name=uri)

                        contact.detail = 'Invitation sent...'
                        session.log_info('Adding %s to list of invited partipants' % uri)
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
        point = sender.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                    NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                    sender.window().graphicsContext(), 0, 1, 0)
        NSMenu.popUpContextMenu_withEvent_forView_(self.participantMenu, event, sender)

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chatStream = selectedSession.streamHandlerOfType("chat")
            if chatStream:
                chatStream.userClickedToolbarButton(sender)

    @objc.IBAction
    def userClickedConferenceScreenSharingQualityMenu_(self, sender):
        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chatStream = selectedSession.streamHandlerOfType("chat")
            if chatStream:
                chatStream.userClickedConferenceScreenSharingQualityMenu_(sender)

    @objc.IBAction
    def userClickedScreenSharingMenu_(self, sender):
        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chatStream = selectedSession.streamHandlerOfType("chat")
            if chatStream:
                chatStream.userClickedScreenSharingMenu_(sender)

    @objc.IBAction
    def userClickedEncryptionMenu_(self, sender):
        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chatStream = selectedSession.streamHandlerOfType("chat")
            if chatStream:
                chatStream.userClickedEncryptionMenu_(sender)

    @objc.IBAction
    def userClickedScreenshotMenu_(self, sender):
        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            chatStream = selectedSession.streamHandlerOfType("chat")
            if chatStream:
                chatStream.userClickedScreenshotMenu_(sender)

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

    def showRemoteScreenIfNecessary(self, participant):
        uri = participant.uri
        if uri not in self.remote_screens_closed_by_user:
            try:
                self.remoteScreens[uri]
            except KeyError:
                self.viewSharedScreen(uri, participant.name, participant.screensharing_url)

    def menuWillOpen_(self, menu):
        if menu == self.encryptionMenu:
            settings = SIPSimpleSettings()
            item = menu.itemWithTag_(1)
            item.setHidden_(not settings.chat.enable_encryption)

            item = menu.itemWithTag_(3)
            item.setEnabled_(False)
            item.setState_(NSOffState)
            item.setHidden_(True)

            item = menu.itemWithTag_(4)
            item.setState_(NSOffState)
            item.setEnabled_(False)
            item.setState_(NSOffState)

            item = menu.itemWithTag_(5)
            item.setHidden_(True)

            item = menu.itemWithTag_(6)
            item.setHidden_(True)

            item = menu.itemWithTag_(7)
            item.setHidden_(True)

            item = menu.itemWithTag_(9)
            item.setHidden_(True)

            selectedSession = self.selectedSessionController()
            if selectedSession:
                chat_stream = selectedSession.streamHandlerOfType("chat")
                if chat_stream:
                    display_name = selectedSession.getTitleShort()
                    item = menu.itemWithTag_(1)
                    my_fingerprint = chat_stream.otr_account.getPrivkey()
                    item.setTitle_('My fingerprint is %s' % str(my_fingerprint))

                    item = menu.itemWithTag_(3)
                    item.setTitle_('Always require OTR encryption with %s' % display_name)

                    if selectedSession.contact is not None:
                        item.setEnabled_(True)
                        item.setState_(NSOnState if chat_stream.require_encryption else NSOffState)
                        item.setHidden_(not settings.chat.enable_encryption)
                    else:
                        item.setEnabled_(False)
                        item.setHidden_(True)
                        item.setState_(NSOffState)

                    item = menu.itemWithTag_(4)
                    if settings.chat.enable_encryption:
                        if chat_stream.status == STREAM_CONNECTED:
                            if selectedSession.session.remote_focus:
                                item.setEnabled_(False)
                                item.setTitle_('OTR encryption is not possible within a multi-party conference')
                            else:
                                if chat_stream.require_encryption and chat_stream.is_encrypted:
                                    item.setEnabled_(False)
                                else:
                                    item.setEnabled_(True)
                                item.setTitle_('Activate OTR encryption for this session' if not chat_stream.is_encrypted else 'Deactivate OTR encryption for this session')
                        else:
                            item.setEnabled_(False)
                            item.setTitle_('OTR encryption is possible after connection is established')

                    else:
                        item.setEnabled_(False)
                        item.setTitle_('OTR encryption is disabled in Chat preferences')

                    if settings.chat.enable_encryption:
                        ctx = chat_stream.otr_account.getContext(selectedSession.call_id)
                        fingerprint = ctx.getCurrentKey()

                        if fingerprint:
                            item = menu.itemWithTag_(6)
                            item.setHidden_(False)

                            item = menu.itemWithTag_(7)
                            item.setHidden_(False)

                            fingerprint_verified = chat_stream.otr_account.getTrust(selectedSession.remoteSIPAddress, str(fingerprint))
                            item.setEnabled_(False)
                            item.setTitle_("%s's fingerprint is %s" % (display_name, fingerprint) if fingerprint is not None else 'No Fingerprint Discovered')

                            item = menu.itemWithTag_(5)
                            item.setEnabled_(True if fingerprint else False)
                            item.setHidden_(False)
                            item.setTitle_("I have verified %s's fingerprint" % display_name)
                            item.setState_(NSOnState if fingerprint_verified else NSOffState)

                            item = menu.itemWithTag_(9)
                            item.setHidden_(False)

        elif menu == self.participantMenu:
            session = self.selectedSessionController()
            if session:
                item = self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_EMAIL)
                object = {
                    'subject' : 'Invitation to Conference',
                    'body'    : 'Hello,\n\n' +
                                'You can use a SIP or XMPP client to connect to the room at the following address:\n\n' +
                                'sip:%s\n' % session.remoteSIPAddress +
                                'xmpp:%s\n' % session.remoteSIPAddress
                    }
                item.setRepresentedObject_(object)
                self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SHOW_SESSION_INFO).setEnabled_(True if session.session is not None and session.session.state is not None else False)
                self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SHOW_SESSION_INFO).setTitle_('Hide Session Information' if session.info_panel is not None and session.info_panel.window.isVisible() else 'Show Session Information')
            else:
                self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SHOW_SESSION_INFO).setEnabled_(False)
                self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SHOW_SESSION_INFO).setTitle_('Show Session Information')



            item = menu.itemWithTag_(CONFERENCE_ROOM_MENU_VIEW_SCREEN)
            row = self.participantsTableView.selectedRow()
            try:
                object = self.participants[row]
                uri = object.uri
                item.setState_(NSOnState if self.remoteScreens.has_key(uri) else NSOffState)
                item.setEnabled_(True if 'screen' in object.active_media else False)
            except IndexError:
                item.setState_(NSOffState)
                item.setEnabled_(False)

        elif menu == self.screenShareMenu:
            selectedSession = self.selectedSessionController()
            if selectedSession:
                title = selectedSession.getTitleShort()
                if selectedSession.hasStreamOfType("screen-sharing"):
                    menu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display_red"))
                    screen_sharing_stream = selectedSession.streamHandlerOfType("screen-sharing")
                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_CANCEL)
                    mitem.setEnabled_(True)

                    if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                        mitem.setTitle_("Cancel Screen Sharing Request")
                    elif screen_sharing_stream.status == STREAM_CONNECTED:
                        mitem.setTitle_("Stop Screen Sharing")

                    if screen_sharing_stream.direction == 'active':
                        mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE)
                        if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                            mitem.setTitle_("Requesting Screen from %s..." % title)
                        else:
                            mitem.setTitle_("%s is Sharing Her Screen" % title)

                        mitem.setEnabled_(False)
                        mitem.setHidden_(False)

                        mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL)
                        mitem.setEnabled_(False)
                        mitem.setHidden_(True)
                    else:
                        mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE)
                        mitem.setHidden_(True)
                        mitem.setEnabled_(False)

                        mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL)
                        if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                            mitem.setTitle_("Sharing My Screen with %s..." % title)
                        else:
                            mitem.setTitle_("My Screen is Shared with %s" % title)
                        mitem.setEnabled_(False)
                        mitem.setHidden_(False)

                else:
                    menu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display"))

                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE)
                    mitem.setTitle_("Request Screen from %s" % title)
                    mitem.setEnabled_(True)
                    mitem.setHidden_(False)

                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL)
                    mitem.setTitle_("Share My Screen with %s" % title)
                    mitem.setEnabled_(True)
                    mitem.setHidden_(False)

                    mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_CANCEL)
                    mitem.setTitle_("Cancel Screen Sharing Request")
                    mitem.setEnabled_(False)
            else:
                menu.itemAtIndex_(0).setImage_(NSImage.imageNamed_("display"))

                mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_REQUEST_REMOTE)
                mitem.setTitle_("Request Screen from %s" % title)
                mitem.setEnabled_(False)

                mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_OFFER_LOCAL)
                mitem.setTitle_("Share My Screen with %s" % title)
                mitem.setEnabled_(False)

                mitem = menu.itemWithTag_(TOOLBAR_SCREENSHARING_MENU_CANCEL)
                mitem.setTitle_("Cancel Screen Sharing Request")
                mitem.setEnabled_(False)

        elif menu == self.conferenceScreenSharingMenu:
            while self.conferenceScreenSharingMenu.numberOfItems() > 7:
                self.conferenceScreenSharingMenu.removeItemAtIndex_(7)

            for i in (0,1,2,3,4,5):
                item = self.conferenceScreenSharingMenu.itemAtIndex_(i)
                item.setHidden_(True)

            selected_window = None
            selectedSession = self.selectedSessionController()
            if selectedSession:
                chat_stream = selectedSession.streamHandlerOfType("chat")
                if chat_stream and chat_stream.screensharing_handler:
                        selected_window = chat_stream.screensharing_handler.window_id

            item = self.conferenceScreenSharingMenu.addItemWithTitle_action_keyEquivalent_('Entire Screen', "selectConferenceScreenSharingWindow:", "")
            obj = {'application': 'entire screen', 'id': 0, 'name': 'Entire Screen'}
            item.setRepresentedObject_(obj)
            item.setIndentationLevel_(2)
            item.setState_(NSOnState if selected_window == 0 else NSOffState)
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
                    item = self.conferenceScreenSharingMenu.addItemWithTitle_action_keyEquivalent_(title, "selectConferenceScreenSharingWindow:", "")
                    obj = {'id': id, 'name': name, 'application': application}
                    item.setRepresentedObject_(obj)
                    item.setIndentationLevel_(2)
                    item.setState_(NSOnState if selected_window == id else NSOffState)
                i += 1

            if i:
                i += 2
                self.conferenceScreenSharingMenu.addItem_(NSMenuItem.separatorItem())
                item = self.conferenceScreenSharingMenu.addItemWithTitle_action_keyEquivalent_('Stop Screen Sharing', "stopConferenceScreenSharing:", "")
                obj = {'id': None, 'name': None, 'application': None}
                item.setRepresentedObject_(obj)
                item.setIndentationLevel_(1)
                item.setEnabled_(True if chat_stream.screensharing_handler and chat_stream.screensharing_handler.connected else False)

            if selectedSession and chat_stream:
                item = self.conferenceScreenSharingMenu.itemAtIndex_(0)
                item.setEnabled_(True if chat_stream.screensharing_allowed else False)

                if chat_stream.screensharing_handler and chat_stream.screensharing_handler.connected:
                    for i in (2,3,4,5):
                        item = self.conferenceScreenSharingMenu.itemAtIndex_(i)
                        item.setHidden_(False)

                    item = self.conferenceScreenSharingMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_HIGH)
                    item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'high' else NSOffState)
                    item.setEnabled_(True)
                    item = self.conferenceScreenSharingMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_LOW)
                    item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'low' else NSOffState)
                    item.setEnabled_(True)
                    item = self.conferenceScreenSharingMenu.itemWithTag_(TOOLBAR_SCREENSHOT_MENU_QUALITY_MENU_MEDIUM)
                    item.setState_(NSOnState if chat_stream.screensharing_handler.quality == 'medium' else NSOffState)
                    item.setEnabled_(True)

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
            display_name = object.name
            screensharing_url = object.screensharing_url

            if tag == CONFERENCE_ROOM_MENU_ADD_CONTACT:
                NSApp.delegate().contactsWindowController.addContact(uri, display_name)
            elif tag == CONFERENCE_ROOM_MENU_ADD_CONFERENCE_CONTACT:
                remote_uri = format_identity_to_string(session.remotePartyObject)
                display_name = None
                if session.conference_info is not None:
                    conf_desc = session.conference_info.conference_description
                    display_name = unicode(conf_desc.display_text)
                NSApp.delegate().contactsWindowController.addContact(remote_uri, display_name)
            elif tag == CONFERENCE_ROOM_MENU_REMOVE_FROM_CONFERENCE:
                ret = NSRunAlertPanel(u"Remove from conference", u"You will request the conference server to remove %s from the room. Are your sure?" % uri, u"Remove", u"Cancel", None)
                if ret == NSAlertDefaultReturn:
                    self.removeParticipant(uri)
            elif tag == CONFERENCE_ROOM_MENU_INVITE_TO_CONFERENCE:
                self.addParticipants()
            elif tag == CONFERENCE_ROOM_MENU_SEND_PRIVATE_MESSAGE:
                self.sendPrivateMessage()
            elif tag == CONFERENCE_ROOM_MENU_NICKNAME:
                self.setNickname()
            elif tag == CONFERENCE_ROOM_MENU_SUBJECT:
                self.setSubject()
            elif tag == CONFERENCE_ROOM_MENU_SEND_EMAIL:
                object = sender.representedObject()
                mailtoLink = NSString.stringWithString_('mailto:?subject=%s&body=%s' % (object['subject'], object['body']))
                url_string = CFURLCreateStringByAddingPercentEscapes(None, mailtoLink, None, None, kCFStringEncodingUTF8). autorelease();
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url_string))
            elif tag == CONFERENCE_ROOM_MENU_COPY_ROOM_TO_CLIPBOARD:
                remote_uri = format_identity_to_string(session.remotePartyObject)
                pb = NSPasteboard.generalPasteboard()
                pb.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), self)
                pb.setString_forType_(remote_uri, NSStringPboardType)
            elif tag == CONFERENCE_ROOM_MENU_COPY_PARTICIPANT_TO_CLIPBOARD:
                pb = NSPasteboard.generalPasteboard()
                pb.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), self)
                pb.setString_forType_(uri, NSStringPboardType)
            elif tag == CONFERENCE_ROOM_MENU_GOTO_CONFERENCE_WEBSITE:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(session.conference_info.host_info.web_page.value))
            elif tag == CONFERENCE_ROOM_MENU_START_AUDIO_SESSION:
                NSApp.delegate().contactsWindowController.startSessionWithTarget(uri, media_type="audio", local_uri=session.account.id)
            elif tag == CONFERENCE_ROOM_MENU_START_VIDEO_SESSION:
                NSApp.delegate().contactsWindowController.startSessionWithTarget(uri, media_type="video", local_uri=session.account.id)
            elif tag == CONFERENCE_ROOM_MENU_START_CHAT_SESSION:
                NSApp.delegate().contactsWindowController.startSessionWithTarget(uri, media_type="chat", local_uri=session.account.id)
            elif tag == CONFERENCE_ROOM_MENU_VIEW_SCREEN:
                try:
                    remoteScreen = self.remoteScreens[uri]
                except KeyError:
                    self.viewSharedScreen(uri, display_name, screensharing_url)
                else:
                    remoteScreen.close_(None)
                sender.setState_(NSOffState if sender.state() == NSOnState else NSOnState)
            elif tag == CONFERENCE_ROOM_MENU_SEND_FILES:
                openFileTransferSelectionDialog(session.account, uri)
            elif tag == CONFERENCE_ROOM_MENU_SHOW_SESSION_INFO:
                session.info_panel.toggle()

    def viewSharedScreen(self, uri, display_name, url):
        session = self.selectedSessionController()
        if session:
            session.log_info(u"Opening Shared Screen of %s from %s" % (uri, unquote(url)))
            remoteScreen = ConferenceScreenSharing.createWithOwner_(self)
            remoteScreen.show(display_name, uri, unquote(url))
            self.remoteScreens[uri] = remoteScreen

    def startScreenSharingWithUrl(self, url):
        session = self.selectedSessionController()
        if session and session.conference_info is not None:
            try:
                user = (user for user in session.conference_info.users if user.screen_image_url and user.screen_image_url.value == url).next()
            except StopIteration:
                pass
            else:
                uri = sip_prefix_pattern.sub("", user.entity)
                contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(uri)
                if contact:
                    display_name = user.display_text.value if user.display_text is not None and user.display_text.value else contact.name
                else:
                    display_name = user.display_text.value if user.display_text is not None and user.display_text.value else uri

                try:
                    self.remoteScreens[uri]
                except KeyError:
                    self.viewSharedScreen(uri, display_name, url)
                    return True
        return False

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
                selectedSession = self.sessions[identifier]
            except KeyError:
                pass
            else:
                chatStream = selectedSession.streamHandlerOfType("chat")
                if chatStream:
                    chatStream.updateToolbarButtons(self.toolbar, got_proposal)

            self.toolbar.validateVisibleItems()

    def refreshDrawerTimer_(self, timer):
        if self.refresh_drawer_counter:
            self.refresh_drawer_counter = 0
            self.refreshDrawerIfNecessary()

    @run_in_gui_thread
    def refreshDrawer(self):
        self.refresh_drawer_counter += 1

    def refreshDrawerIfNecessary(self):
        session = self.selectedSessionController()

        participants, self.participants = self.participants, []
        for item in set(participants).difference(session.invited_participants if session else []):
            item.destroy()
        del participants

        self.updateTitle()

        if session is not None:
            state = session.state
            if state == STATE_CONNECTING:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setHidden_(False)
                self.audioStatus.setStringValue_(u"Connecting...")
            elif state == STATE_CONNECTED:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setHidden_(False)
                self.audioStatus.setStringValue_(u"Connected")
            else:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setHidden_(True)
                self.audioStatus.setStringValue_('')
        else:
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
            self.audioStatus.setHidden_(True)
            self.audioStatus.setStringValue_('')

        if session is not None and session.session is None:
            if session.account is BonjourAccount():
                own_uri = '%s@%s' % (session.account.uri.user, session.account.uri.host)
            else:
                own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)

            # Add ourselves
            contact = BlinkMyselfConferenceContact(session.account)
            self.participants.append(contact)

            # Add remote party
            if isinstance(session.selected_contact, BlinkPresenceContact):
                # Find the contact from the all contacts group
                model = NSApp.delegate().contactsWindowController.model
                try:
                    presence_contact = next(item for item in model.all_contacts_group.contacts if item.contact == session.selected_contact.contact)
                except StopIteration:
                    presence_contact = None
            else:
                presence_contact = None

            if not presence_contact:
                presence_contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(session.remoteSIPAddress)

            if presence_contact:
                contact = BlinkConferenceContact(session.remoteSIPAddress, name=presence_contact.name, icon=presence_contact.icon, presence_contact=presence_contact)
            else:
                uri = format_identity_to_string(session.remotePartyObject)
                display_name = session.getTitleShort()
                contact = BlinkConferenceContact(uri, name=display_name)
            self.participants.append(contact)
        elif session is not None and session.session is not None:
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
                contact = BlinkMyselfConferenceContact(session.account)
                contact.active_media = active_media
                self.participants.append(contact)

                # Add remote party
                if isinstance(session.selected_contact, BlinkPresenceContact):
                    # Find the contact from the all contacts group
                    model = NSApp.delegate().contactsWindowController.model
                    try:
                        presence_contact = next(item for item in model.all_contacts_group.contacts if item.contact == session.selected_contact.contact)
                    except StopIteration:
                        presence_contact = None
                else:
                    presence_contact = None

                if not presence_contact:
                    presence_contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(session.remoteSIPAddress)

                icon = None
                if chat_stream.remoteIcon:
                    icon = chat_stream.remoteIcon
                if presence_contact:
                    contact = BlinkConferenceContact(session.remoteSIPAddress, name=presence_contact.name, icon=presence_contact.icon, presence_contact=presence_contact)
                else:
                    uri = format_identity_to_string(session.remotePartyObject)
                    display_name = session.getTitleShort()
                    contact = BlinkConferenceContact(uri, name=display_name, icon=icon)

                if session.state == STATE_DNS_LOOKUP:
                    contact.detail = "Finding Destination..."
                elif session.state == STATE_CONNECTING:
                    contact.detail = "Connecting..."
                else:
                    try:
                        sip_uri = SIPURI.parse(str(contact.uri))
                        puri = '%s@%s' % (sip_uri.user, sip_uri.host)
                    except SIPCoreError:
                        puri = contact.uri
                    contact.detail = puri

                active_media = []

                if session.hasStreamOfType("chat") and chat_stream.status == STREAM_CONNECTED:
                    active_media.append('message')

                if session.hasStreamOfType("audio"):
                    active_media.append('audio' if not audio_stream.holdByRemote else 'audio-onhold')

                contact.active_media = active_media
                self.participants.append(contact)
            elif session.conference_info is not None:
                # Add conference participants if any
                for user in session.conference_info.users:
                    uri = sip_prefix_pattern.sub("", user.entity)
                    if uri == own_uri:
                        # Add ourselves
                        contact = BlinkMyselfConferenceContact(session.account)
                    else:
                         # Add remote party
                        presence_contact = NSApp.delegate().contactsWindowController.getFirstContactFromAllContactsGroupMatchingURI(uri)
                        if presence_contact:
                            display_name = user.display_text.value if user.display_text is not None and user.display_text.value else presence_contact.name
                            contact = BlinkConferenceContact(uri, name=display_name, icon=presence_contact.icon, presence_contact=presence_contact)
                        else:
                            display_name = user.display_text.value if user.display_text is not None and user.display_text.value else uri
                            contact = BlinkConferenceContact(uri, name=display_name)

                    active_media = []

                    if any(media.media_type == 'message' for media in chain(*user)):
                        active_media.append('message')

                    if user.screen_image_url is not None:
                        active_media.append('screen')
                        contact.screensharing_url = user.screen_image_url.value

                    audio_endpoints = [endpoint for endpoint in user if any(media.media_type == 'audio' for media in endpoint)]
                    user_on_hold = all(endpoint.status == 'on-hold' for endpoint in audio_endpoints)
                    if audio_endpoints and not user_on_hold:
                        active_media.append('audio')
                    elif audio_endpoints and user_on_hold:
                        active_media.append('audio-onhold')

                    contact.active_media = active_media
                    # detail will be reset on receival of next conference-info update
                    if uri in session.pending_removal_participants:
                        contact.detail = 'Removal requested...'

                    self.participants.append(contact)

            self.participants.sort(key=attrgetter('name'))

            # Add invited participants if any

            if session.invited_participants:
                for contact in session.invited_participants:
                    self.participants.append(contact)

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
                    if audio_stream.stream.sample_rate >= 32000:
                        hd_label = 'UWB Audio'
                    elif audio_stream.stream.sample_rate >= 16000:
                        hd_label = 'WB Audio'
                    else:
                        hd_label = 'PSTN Audio'

                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    self.audioStatus.setStringValue_(u"%s (%s)" % (hd_label, beautify_audio_codec(audio_stream.stream.codec)))
                    self.audioStatus.setHidden_(False)

            elif session.hasStreamOfType("chat") and chat_stream.status == STREAM_CONNECTED:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setStringValue_(u"Connected")
                self.audioStatus.setHidden_(False)

            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_NICKNAME).setEnabled_(self.canSetNickname())
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SUBJECT).setEnabled_(self.canSetSubject())
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_COPY_ROOM_TO_CLIPBOARD).setEnabled_(True)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_SEND_EMAIL).setEnabled_(True)

            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_INVITE_TO_CONFERENCE).setEnabled_(False if isinstance(session.account, BonjourAccount) else True)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_GOTO_CONFERENCE_WEBSITE).setEnabled_(True if self.canGoToConferenceWebsite() else False)

            hasContactMatchingURI = NSApp.delegate().contactsWindowController.hasContactMatchingURI
            remote_uri = format_identity_to_string(session.remotePartyObject)
            self.participantMenu.itemWithTag_(CONFERENCE_ROOM_MENU_ADD_CONFERENCE_CONTACT).setEnabled_(False if hasContactMatchingURI(remote_uri) else True)

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

        self.participantsTableView.reloadData()

    def drawerDidOpen_(self, notification):
        session = self.selectedSessionController()
        if session:
            session.mustShowDrawer = True

    def drawerDidClose_(self, notification):
        session = self.selectedSessionController()
        if session:
            session.mustShowDrawer = False

    def tabViewDidChangeNumberOfTabViewItems_(self, tabView):
        if tabView.numberOfTabViewItems() == 0 and not self.closing:
            self.window().performClose_(None)

    def tabView_didDettachTabViewItem_atPosition_(self, tabView, item, position):
        pass

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if self.sessions.has_key(item.identifier()):
            self.revalidateToolbar()
            self.updateTitle()
            session = self.sessions[item.identifier()]
            chat_stream = session.streamHandlerOfType("chat")
            if chat_stream:
                chat_stream.updateDatabaseRecordingButton()

            self.refreshDrawer()
            if session.mustShowDrawer:
                self.drawer.open()
                NSApp.delegate().contactsWindowController.drawer.close()
                self.participantsTableView.deselectAll_(self)
                self.conferenceFilesTableView.deselectAll_(self)
            else:
                pass
                #self.drawer.close()

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
                        uri = sip_prefix_pattern.sub("", str(uri))
                    try:
                        table.setDropRow_dropOperation_(self.numberOfRowsInTableView_(table), NSTableViewDropAbove)
                    except:
                        return NSDragOperationNone
                    return NSDragOperationAll
                elif pboard.types().containsObject_(NSFilenamesPboardType):
                    return NSDragOperationAll
            elif not isinstance(session.account, BonjourAccount):
                return NSDragOperationAll

        return NSDragOperationNone

    @allocate_autorelease_pool
    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        session = self.selectedSessionController()

        if not session:
            return False

        sourceContact = None
        if pboard.availableTypeFromArray_(["dragged-contact"]):
            group, blink_contact = eval(pboard.stringForType_("dragged-contact"))
            if blink_contact is not None:
                sourceGroup = NSApp.delegate().contactsWindowController.model.groupsList[group]
                try:
                    sourceContact = sourceGroup.contacts[blink_contact]
                except IndexError:
                    return False

                if len(sourceContact.uris) > 1:
                    point = table.window().convertScreenToBase_(NSEvent.mouseLocation())
                    event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                              NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), table.window().windowNumber(),
                                                                                                                                              table.window().graphicsContext(), 0, 1, 0)
                    invite_menu = NSMenu.alloc().init()
                    titem = invite_menu.addItemWithTitle_action_keyEquivalent_(u'Invite To Conference', "", "")
                    titem.setEnabled_(False)
                    for uri in sourceContact.uris:
                        titem = invite_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, uri.type), "userClickedInviteToConference:", "")
                        titem.setIndentationLevel_(1)
                        titem.setTarget_(self)
                        titem.setRepresentedObject_({'session': session, 'uri': uri.uri, 'contact':sourceContact})

                    NSMenu.popUpContextMenu_withEvent_forView_(invite_menu, event, table)
                else:
                    uri = str(pboard.stringForType_("x-blink-sip-uri"))
                    self.inviteContactToConferenceSessionWithUri(session, uri, sourceContact)

            return True
        elif pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            self.inviteContactToConferenceSessionWithUri(session, uri)
        elif pboard.types().containsObject_(NSFilenamesPboardType):
            chat_controller = session.streamHandlerOfType("chat")
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            return chat_controller.sendFiles(fnames)

    @objc.IBAction
    def userClickedInviteToConference_(self, sender):
        session = sender.representedObject()['session']
        uri = sender.representedObject()['uri']
        contact = sender.representedObject()['contact']
        self.inviteContactToConferenceSessionWithUri(session, uri, contact)

    def inviteContactToConferenceSessionWithUri(self, session, uri, contact=None):
        if uri:
            uri = sip_prefix_pattern.sub("", str(uri))

        if "@" not in uri:
            uri = '%s@%s' % (uri, session.account.id.domain)

        try:
            sip_uri = 'sip:%s' % uri if not uri.startswith("sip:") else uri
            sip_uri = SIPURI.parse(sip_uri)
        except SIPCoreError:
            session.log_info(u"Error inviting to conference: invalid URI %s" % uri)
            return False

        # do not invite remote party itself
        remote_uri = format_identity_to_string(session.remotePartyObject)
        if uri == remote_uri:
            return False

        # do not invite users already invited
        for old_contact in session.invited_participants:
            if uri == old_contact.uri:
                return False

        # do not invite users already present in the conference
        if session.conference_info is not None:
            for user in session.conference_info.users:
                if uri == sip_prefix_pattern.sub("", user.entity):
                    return False

        if session.remote_focus:
            if isinstance(contact, BlinkPresenceContact):
                # Find the contact from the all contacts group
                model = NSApp.delegate().contactsWindowController.model
                try:
                    presence_contact = next(item for item in model.all_contacts_group.contacts if item.contact == contact.contact)
                except StopIteration:
                    presence_contact = None
            else:
                presence_contact = None


            new_contact = BlinkConferenceContact(uri, name=contact.name if contact else None, icon=contact.icon if contact else None, presence_contact=presence_contact)
            new_contact.detail = 'Invitation sent...'
            session.invited_participants.append(new_contact)
            session.participants_log.add(uri)
            self.refreshDrawer()
            session.log_info(u"Invite %s to conference" % uri)
            session.session.conference.add_participant(uri)
        elif not isinstance(session.account, BonjourAccount):
            self.joinConferenceWindow(session, [uri])


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
        return NSString.stringWithString_(sip_prefix_pattern.sub("", self.file.sender))


