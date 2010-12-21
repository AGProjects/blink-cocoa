# Copyright (C) 2009-2010 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
from WebKit import *

from zope.interface import implements
from application.notification import NotificationCenter, IObserver, Any
from sipsimple.account import BonjourAccount

from ServerConferenceController import AddParticipantsWindow
from BlinkLogger import BlinkLogger
from ContactListModel import Contact
import SessionController
import SessionManager
import FancyTabSwitcher
from util import allocate_autorelease_pool


class ChatWindowController(NSWindowController):
    implements(IObserver)

    tabView = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    desktopShareMenu = objc.IBOutlet()
    drawer = objc.IBOutlet()
    drawerTableView = objc.IBOutlet()
    addParticipants = objc.IBOutlet()
    
    #statusbar = objc.IBOutlet()

    def init(self):
        self = super(ChatWindowController, self).init()
        if self:
            self.participants = []
            self.sessions = {}
            self.toolbarItems = {}
            self.unreadMessageCounts = {}
            NSBundle.loadNibNamed_owner_("ChatSession", self)
            NotificationCenter().add_observer(self, sender=Any, name="BlinkSessionChangedState")
            NotificationCenter().add_observer(self, sender=Any, name="BlinkStreamHandlerChangedState")

            if NSApp.delegate().bundleName == 'Blink Pro':
                self.addParticipants.setEnabled_(True)

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
                    item.setLabel_(newSession.getTitle())
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
        #self.updateStatusText()

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
        if session:
            if isinstance(session.account, BonjourAccount):
                title = u"Chat to %s" % session.getTitleShort()
            else:
                title = u"Chat to %s" % session.getTitleFull()
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

    def validateToolbarItem_(self, item):
        selectedSession = self.selectedSession()
        if selectedSession:
            return selectedSession.validateToolbarButton(item)
        else:
            return False

    #def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, toolbar, identifier, flag):
    #    return self.toolbarItems.get(str(identifier), None)

    @objc.IBAction
    def close_(self, sender):
        selectedSession = self.selectedSession()
        if selectedSession:
            if len(self.sessions) == 1:
                self.window().close()

            chat_handler = selectedSession.streamHandlerOfType("chat")
            if chat_handler:
                chat_handler.end(True)
            else:
                self.detachSession_(selectedSession)

    def windowShouldClose_(self, sender):
        active = len([s for s in self.sessions.values() if s.isActive() and s.hasStreamOfType("chat")])

        if active > 1:
            ret = NSRunAlertPanel(u"Close Chat Window",
                                  u"There are %i active chat sessions, would you like to terminate and close them?" % active,
                                  u"Close", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return False

        self.window().close()
        for s in self.sessions.values(): # we need a copy of the dict contents as it will change as a side-effect of removeSession_()
            handler = s.streamHandlerOfType("chat")
            if handler:
                handler.end()
            self.removeSession_(s)
        return True

    @objc.IBAction
    def addParticipants_(self, sender):
        addParticipantsWindow = AddParticipantsWindow()
        participants = addParticipantsWindow.run()
        if participants is not None:
            msg = 'Invite participants to conference: ' + ','.join(participants)
            BlinkLogger().show_info(msg)
            self.refreshParticipantList(participants)
            # TODO: send REFER to invite each participant -adi

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        if sender.tag() == SessionController.TOOLBAR_DESKTOP_SHARING:
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

    def revalidateToolbar(self):
        if self.tabView.selectedTabViewItem():
            identifier = self.tabView.selectedTabViewItem().identifier()
            self.sessions[identifier].updateToolbarButtons(self.toolbar)
            self.toolbar.validateVisibleItems()

    def tabViewDidChangeNumberOfTabViewItems_(self, tabView):
        if tabView.numberOfTabViewItems() == 0:
            self.window().performClose_(None)

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if self.sessions.has_key(item.identifier()):
            self.revalidateToolbar()
            self.updateTitle()
            session = self.sessions[item.identifier()]

            self.refreshParticipantList()
            if session.showMultiPartyParticipantList:
                self.drawer.open()
            else:
                self.drawer.close()

        self.unreadMessageCounts[item.identifier()] = 0
        sitem = self.tabSwitcher.itemForTabViewItem_(item)
        if sitem:
            sitem.setBadgeLabel_("")
        #self.updateStatusText()



    def tabView_shouldCloseTabViewItem_(self, tabView, item):
        if self.sessions.has_key(item.identifier()):
            chatH = self.sessions[item.identifier()].streamHandlerOfType("chat")
            if chatH:
                chatH.end(True)
                return False
        return True


    def tabView_didDettachTabViewItem_atPosition_(self, tabView, item, pos):
        if len(self.sessions) > 1:
            session = self.sessions[item.identifier()]

            window = SessionManager.SessionManager().dettachChatSession(session)
            if window:
                window.window().setFrameOrigin_(pos)

    def refreshParticipantList(self):
        # TODO: we must store the participant list per session in Session Controller -adi
        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
        self.participants = []

        session = self.selectedSession()
        if session:
            contact = getContactMatchingURI(session.remoteSIPAddress)
            if contact and contact not in self.participants:
                self.participants.append(contact)
            else:
                name = session.remoteParty
                if ":" in name:
                    name = name.partition(":")[2]
                contact = Contact(uri=name, name=name)

                if contact not in self.participants:
                    self.participants.append(contact)

            self.drawerTableView.reloadData()

    # drag/drop
    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            # TODO: skip SIP URIs already in participants list -adi
            #    return NSDragOperationNone            

        table.setDropRow_dropOperation_(self.numberOfRowsInTableView_(table), NSTableViewDropAbove)
        return NSDragOperationAll

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
            self.participants.append(getContactMatchingURI(uri) or uri)
            self.drawerTableView.reloadData()
            BlinkLogger().show_info(u"Invite %s to conference" % uri)
            # TODO: send REFER to invite participant to the conference -adi
        return True

    def drawerDidOpen_(self, notification):
        session = self.selectedSession()
        if session:
            session.showMultiPartyParticipantList = True

    def drawerDidClose_(self, notification):
        session = self.selectedSession()
        if session:
            session.showMultiPartyParticipantList = False

    # TableView dataSource
    def numberOfRowsInTableView_(self, tableView):
        if tableView == self.drawerTableView:
            return len(self.participants)
        return 0

    def tableView_objectValueForTableColumn_row_(self, tableView, tableColumn, row):
        if tableView == self.drawerTableView:
            if type(self.participants[row]) in (str, unicode):
                return self.participants[row]
            else:
                return self.participants[row].name
        return 0

    def tableView_willDisplayCell_forTableColumn_row_(self, tableView, cell, tableColumn, row):
        if tableView == self.drawerTableView:
            if type(self.participants[row]) in (str, unicode):
                cell.setContact_(None)
            else:
                cell.setContact_(self.participants[row])

