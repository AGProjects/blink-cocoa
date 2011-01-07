# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from WebKit import *

from itertools import chain
from zope.interface import implements
from application.notification import NotificationCenter, IObserver
from operator import attrgetter
from sipsimple.account import BonjourAccount

from BaseStream import *
from BlinkLogger import BlinkLogger
from ContactListModel import Contact
import ParticipantsTableView
from ServerConferenceWindowController import AddParticipantsWindow, StartConferenceWindow
import SessionController
import ChatWindowManager
from SIPManager import SIPManager
import FancyTabSwitcher
from util import allocate_autorelease_pool, format_identity_address


class ChatWindowController(NSWindowController):
    implements(IObserver)

    tabView = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    desktopShareMenu = objc.IBOutlet()
    participantMenu = objc.IBOutlet()
    drawer = objc.IBOutlet()
    drawerTableView = objc.IBOutlet()
    addParticipants = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    recordButton = objc.IBOutlet()
    audioStatus = objc.IBOutlet()

    def init(self):
        self = super(ChatWindowController, self).init()
        if self:
            self.participants = []
            self.sessions = {}
            self.toolbarItems = {}
            self.unreadMessageCounts = {}
            NSBundle.loadNibNamed_owner_("ChatSession", self)

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="AudioStreamDidStartRecordingAudio")
            self.notification_center.add_observer(self, name="AudioStreamDidStopRecordingAudio")
            self.notification_center.add_observer(self, name="BlinkAudioStreamChangedHoldState")
            self.notification_center.add_observer(self, name="BlinkConferenceGotUpdate")
            self.notification_center.add_observer(self, name="BlinkContactsHaveChanged")
            self.notification_center.add_observer(self, name="BlinkGotProposal")
            self.notification_center.add_observer(self, name="BlinkMuteChangedState")
            self.notification_center.add_observer(self, name="BlinkSessionChangedState")
            self.notification_center.add_observer(self, name="BlinkStreamHandlerChangedState")
            self.notification_center.add_observer(self, name="BlinkStreamHandlersChanged")

            self.backend = SIPManager()

            if self.backend.is_muted():
                self.muteButton.setImage_(NSImage.imageNamed_("muted"))
                self.muteButton.setState_(NSOnState)
            else:
                self.muteButton.setImage_(NSImage.imageNamed_("mute"))
                self.muteButton.setState_(NSOffState)

            self.own_icon = None
            path = NSApp.delegate().windowController.iconPathForSelf()
            if path:
                self.own_icon = NSImage.alloc().initWithContentsOfFile_(path)

        return self

    def awakeFromNib(self):
        self.drawerTableView.registerForDraggedTypes_(NSArray.arrayWithObject_("x-blink-sip-uri"))

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

        chatHandler = session.streamHandlerOfType("chat")
        self.tabSwitcher.setTabViewItem_busy_(tabItem, chatHandler.isConnecting if chatHandler else False)

        self.updateTitle()
        if session.mustShowDrawer:
            self.drawer.open()

    def selectSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            raise Exception("Attempt to select invalid tab")
        self.tabView.selectTabViewItemWithIdentifier_(session.identifier)

    def hasSession_(self, session):
        return self.sessions.has_key(session.identifier)

    def detachSession_(self, session):
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
        if not self.detachSession_(session):
            return False

        chatHandler = session.streamHandlerOfType("chat")
        if chatHandler:
            chatHandler.didRemove()

        return True

    def selectedSession(self):
        activeTab = self.tabView.selectedTabViewItem()
        if activeTab and self.sessions.has_key(activeTab.identifier()):
            return self.sessions[activeTab.identifier()]
        if activeTab:
            print "Request for invalid tab %s"%activeTab.identifier()
        return None

    def updateTitle(self):
        session = self.selectedSession()
        title = None
        if session:
            if session.conference_info is not None:
                conf_desc = session.conference_info.conference_description
                if conf_desc.display_text and conf_desc.free_text:
                    title = u"%s %s" % (conf_desc.display_text, conf_desc.free_text)
                elif conf_desc.display_text:
                    title = u"%s" % conf_desc.display_text
                else:
                    title = u"%s" % conf_desc.free_text

            if title is None:
                if isinstance(session.account, BonjourAccount):
                    title = u"%s" % session.getTitleShort()
                else:
                    title = u"%s" % session.getTitleFull()
        else:
            title = u"Chat"

        self.window().setTitle_(title)

    def noteSession_isComposing_(self, session, flag):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            item.setComposing_(flag)


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
        session = self.selectedSession()

        if session and session.streamHandlerOfType("chat"):
            self.window().makeFirstResponder_(session.streamHandlerOfType("chat").chatViewController.inputText)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        name = notification.name
        sender = notification.sender
        data = notification.data

        if name == "BlinkStreamHandlerChangedState":
            session = sender.sessionController
            if session:
                chatHandler = session.streamHandlerOfType("chat")
                if chatHandler:
                    index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
                    if index != NSNotFound:
                        tabItem = self.tabView.tabViewItemAtIndex_(index)
                        self.tabSwitcher.setTabViewItem_busy_(tabItem, chatHandler.isConnecting)
                    else:
                        print "tab for %s (%s) not found [state %s]"%(session.identifier, session.getTitle(), session.state)
            self.revalidateToolbar()
        elif name == "BlinkSessionChangedState":
            session = sender
            if session:
                chatHandler = session.streamHandlerOfType("chat")
                if chatHandler:
                    index = self.tabView.indexOfTabViewItemWithIdentifier_(session.identifier)
                    if index != NSNotFound:
                        tabItem = self.tabView.tabViewItemAtIndex_(index)
                        self.tabSwitcher.setTabViewItem_busy_(tabItem, chatHandler.isConnecting)
                    else:
                        print "tab for %s (%s) not found (state %s)"%(session.identifier, session.getTitle(), chatHandler.status)
            self.revalidateToolbar()
            self.refreshDrawer()
        elif name == "BlinkAudioStreamChangedHoldState":
            self.refreshDrawer()
        elif name == "BlinkStreamHandlersChanged":
            self.revalidateToolbar()
            self.refreshDrawer()
        elif name in( "AudioStreamDidStartRecordingAudio", "AudioStreamDidStopRecordingAudio"):
            self.revalidateToolbar()
        elif name == "BlinkGotProposal":
            self.revalidateToolbar()
            self.refreshDrawer()
        elif name == "BlinkConferenceGotUpdate":
            self.updateTitle()
            self.refreshDrawer()
        elif name == "BlinkContactsHaveChanged":
            self.updateTitle()
            self.refreshDrawer()
        elif name == "BlinkMuteChangedState":
            if self.backend.is_muted():
                self.muteButton.setImage_(NSImage.imageNamed_("muted"))
                self.muteButton.setState_(NSOnState)
            else:
                self.muteButton.setState_(NSOffState)
                self.muteButton.setImage_(NSImage.imageNamed_("mute"))

    def validateToolbarItem_(self, item):
        selectedSession = self.selectedSession()
        if selectedSession:
            return selectedSession.validateToolbarButton(item)
        else:
            return False

    @objc.IBAction
    def close_(self, sender):
        selectedSession = self.selectedSession()
        if selectedSession:
            if len(self.sessions) == 1:
                self.window().close()
                self.notification_center.remove_observer(self, name="AudioStreamDidStartRecordingAudio")
                self.notification_center.remove_observer(self, name="AudioStreamDidStopRecordingAudio")
                self.notification_center.remove_observer(self, name="BlinkAudioStreamChangedHoldState")
                self.notification_center.remove_observer(self, name="BlinkConferenceGotUpdate")
                self.notification_center.remove_observer(self, name="BlinkContactsHaveChanged")
                self.notification_center.remove_observer(self, name="BlinkGotProposal")
                self.notification_center.remove_observer(self, name="BlinkMuteChangedState")
                self.notification_center.remove_observer(self, name="BlinkSessionChangedState")
                self.notification_center.remove_observer(self, name="BlinkStreamHandlerChangedState")
                self.notification_center.remove_observer(self, name="BlinkStreamHandlersChanged")
            chat_handler = selectedSession.streamHandlerOfType("chat")
            if chat_handler:
                chat_handler.end(True)
            else:
                self.detachSession_(selectedSession)

    def windowShouldClose_(self, sender):
        active = len([s for s in self.sessions.values() if s.hasStreamOfType("chat")])

        if active > 1:
            ret = NSRunAlertPanel(u"Close Chat Window",
                                  u"There are %i Chat sessions, click Close to terminate them all." % active,
                                  u"Close", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return False

        self.drawer.close()
        self.window().close()
        for s in self.sessions.values(): # we need a copy of the dict contents as it will change as a side-effect of removeSession_()
            handler = s.streamHandlerOfType("chat")
            if handler:
                handler.end()
            self.removeSession_(s)

        self.notification_center.post_notification("BlinkChatWindowClosed", sender=self)

        return True

    def startConferenceWindow(self, session, participants=[]):
        media = []
        if session.hasStreamOfType("chat"):
            media.append("chat")
        if session.hasStreamOfType("audio"):
            media.append("audio")

        participants.append(format_identity_address(session.remotePartyObject))

        startConferenceWindow = StartConferenceWindow(participants=participants, media=media)
        conference = startConferenceWindow.run()
        if conference is not None:
            NSApp.delegate().windowController.startConference(conference.target, conference.media_types, conference.participants)

    @objc.IBAction
    def addParticipants_(self, sender):
        session = self.selectedSession()
        if session:
            if session.remote_focus:
                addParticipantsWindow = AddParticipantsWindow()
                participants = addParticipantsWindow.run()
                if participants is not None:
                    getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
                    remote_uri = format_identity_address(session.remotePartyObject)
                    # prevent loops
                    if remote_uri in participants:
                        participants.remove(remote_uri)
                    for uri in participants:
                        contact = getContactMatchingURI(uri)
                        if contact:
                            contact = Contact(contact.uri, name=contact.name, icon=contact.icon)
                        else:
                            contact = Contact(uri, name=uri)
                        contact.setDetail('Invitation sent...')
                        if contact not in session.invited_participants:
                            session.invited_participants.append(contact)
                            session.participants_log.append(uri)
                            BlinkLogger().show_info(u"Invite %s to conference" % uri)
                            # TODO: send REFER to invite each participant -adi

                self.refreshDrawer()
            else:
                self.startConferenceWindow(session)

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        if sender.tag() == SessionController.TOOLBAR_DESKTOP_SHARING_BUTTON:
            for item in self.desktopShareMenu.itemArray():
                item.setEnabled_(self.validateToolbarItem_(item))

            point = sender.convertPointToBase_(NSZeroPoint)
            point.y -= NSHeight(sender.frame())
            event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(), sender.window().graphicsContext(),
                0, 1, 0)
            NSMenu.popUpContextMenu_withEvent_forView_(self.desktopShareMenu, event, sender)
            return

        # dispatch the click to the active session
        selectedSession = self.selectedSession()
        if selectedSession:
            selectedSession.userClickedToolbarButton(sender)

    @objc.IBAction
    def userClickedParticipantMenu_(self, sender):
        tag = sender.tag()

        row = self.drawerTableView.selectedRow()
        try:
            object = self.participants[row]
        except IndexError:
            return

        uri = object.uri
        display_name = object.display_name

        if tag == SessionController.PARTICIPANTS_MENU_ADD_CONTACT:
            NSApp.delegate().windowController.addContact(uri, display_name)

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
            self.sessions[identifier].updateToolbarButtons(self.toolbar, got_proposal)
            self.toolbar.validateVisibleItems()

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
                chat_stream.end(True)
                return False
        return True

    def tabView_didDettachTabViewItem_atPosition_(self, tabView, item, pos):
        if len(self.sessions) > 1:
            session = self.sessions[item.identifier()]

            window = ChatWindowManager.ChatWindowManager().dettachChatSession(session)
            if window:
                window.window().setFrameOrigin_(pos)
                self.refreshDrawer()

    def refreshDrawer(self):
        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI

        self.participants = []

        session = self.selectedSession()
        if session:
            if session.hasStreamOfType("audio"):
                audio_stream = session.streamHandlerOfType("audio")

            if session.conference_info is None or (session.conference_info is not None and not session.conference_info.users):
                active_media = []


                # Add remote party
                if session.hasStreamOfType("chat"):
                    chat_stream = session.streamHandlerOfType("chat")
                    if chat_stream.status == STREAM_CONNECTED:
                        active_media.append('message')

                if session.hasStreamOfType("audio"):
                    if not audio_stream.holdByRemote and not audio_stream.holdByLocal:
                        active_media.append('audio')

                contact = getContactMatchingURI(session.remoteSIPAddress)
                if contact:
                    contact = Contact(contact.uri, name=contact.name, icon=contact.icon)
                else:
                    uri = format_identity_address(session.remotePartyObject)
                    display_name = session.getTitleShort()
                    contact = Contact(uri, name=display_name)

                if session.state == STATE_DNS_LOOKUP:
                    contact.setDetail("Finding Destination...")
                elif session.state == STATE_CONNECTING:
                    contact.setDetail("Connecting...")
                else:
                    contact.setDetail(contact.uri)

                contact.setActiveMedia(active_media)
                self.participants.append(contact)

            # Add conference participants if any
            if session.conference_info is not None:
                if not isinstance(session.account, BonjourAccount):
                    own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)

                for user in session.conference_info.users:
                    uri = user.entity.replace("sip:", "", 1)
                    uri = uri.replace("sips:", "", 1)
                    active_media = []

                    chat_endpoints = [endpoint for endpoint in user if any(media.media_type == 'message' for media in endpoint)]
                    if chat_endpoints:
                        active_media.append('message')

                    audio_endpoints = [endpoint for endpoint in user if any(media.media_type == 'audio' for media in endpoint)]
                    user_on_hold = all(endpoint.status == 'on-hold' for endpoint in audio_endpoints)
                    if audio_endpoints and not user_on_hold:
                        active_media.append('audio')

                    contact = getContactMatchingURI(uri)
                    if contact:
                        display_name = user.display_text.value if user.display_text is not None and user.display_text.value else contact.name
                        contact = Contact(uri, name=display_name, icon=contact.icon)
                    else:
                        display_name = user.display_text.value if user.display_text is not None and user.display_text.value else uri
                        contact = Contact(uri, name=display_name)

                    contact.setActiveMedia(active_media)

                    if own_uri and self.own_icon and contact.uri == own_uri:
                        contact.setIcon(self.own_icon)

                    if contact not in self.participants:
                        self.participants.append(contact)

            self.participants.sort(key=attrgetter('name'))

            # Add invited participants if any
            if session.invited_participants:
                for contact in session.invited_participants:
                    self.participants.append(contact)


            self.drawerTableView.reloadData()

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
            else:
                self.audioStatus.setHidden_(True)

            if NSApp.delegate().applicationName == 'Blink Pro' and not isinstance(session.account, BonjourAccount) and session.account.server.conference_server:
                self.addParticipants.setEnabled_(True)
            else:
                self.addParticipants.setEnabled_(False)

            if not self.participants:
                # hide the drawer if everyone left
                self.drawer.close()

    # drag/drop
    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        session = self.selectedSession()
        if session:
            if session.remote_focus:
                # do not allow drag if remote party is not conference focus
                pboard = info.draggingPasteboard()
                if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
                    uri = str(pboard.stringForType_("x-blink-sip-uri"))
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
                                if uri == user.entity.replace("sip:", "", 1):
                                    return NSDragOperationNone
                                if uri == user.entity.replace("sip:", "", 1):
                                    return NSDragOperationNone
                    except:
                        return NSDragOperationNone

                    return NSDragOperationAll
            elif NSApp.delegate().applicationName == 'Blink Pro' and not isinstance(session.account, BonjourAccount) and session.account.server.conference_server:
                return NSDragOperationAll

        return NSDragOperationNone

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        session = self.selectedSession()
        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            if session.remote_focus:
                try:
                    session = self.selectedSession()
                    if session:
                        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
                        contact = getContactMatchingURI(uri)
                        if contact:
                            contact = Contact(contact.uri, name=contact.name, icon=contact.icon)
                        else:
                            contact = Contact(uri, name=uri)
                        contact.setDetail('Invitation sent...')
                        session.invited_participants.append(contact)
                        session.participants_log.append(uri)
                        self.refreshDrawer()
                        BlinkLogger().show_info(u"Invite %s to conference" % uri)
                        # TODO: send REFER to invite participant to the conference -adi
                except:
                    return False
            elif NSApp.delegate().applicationName == 'Blink Pro' and not isinstance(session.account, BonjourAccount) and session.account.server.conference_server:
                self.startConferenceWindow(session, [uri])

        return True

    def drawerDidOpen_(self, notification):
        session = self.selectedSession()
        if session:
            session.mustShowDrawer = True

    def drawerDidClose_(self, notification):
        session = self.selectedSession()
        if session:
            session.mustShowDrawer = False

    # TableView dataSource
    def numberOfRowsInTableView_(self, tableView):
        if tableView == self.drawerTableView:
            try:
                return len(self.participants)
            except:
                pass
        return 0

    def tableView_objectValueForTableColumn_row_(self, tableView, tableColumn, row):
        if tableView == self.drawerTableView:
            try:
                if row < len(self.participants):
                    if type(self.participants[row]) in (str, unicode):
                        return self.participants[row]
                    else:
                        return self.participants[row].name
            except:
                pass
        return 0

        
    def tableView_willDisplayCell_forTableColumn_row_(self, tableView, cell, tableColumn, row):
        if tableView == self.drawerTableView:
            try:
                if row < len(self.participants):
                    if type(self.participants[row]) in (str, unicode):
                        cell.setContact_(None)
                    else:
                        cell.setContact_(self.participants[row])
            except:
                pass
