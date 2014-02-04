# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSPortraitOrientation, NSFitPagination, NSOffState, NSOnState

from Foundation import (NSBundle,
                        NSImage,
                        NSLocalizedString,
                        NSNotFound,
                        NSObject,
                        NSPrintInfo,
                        NSTabViewItem,
                        NSWindowController)
import objc

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implements

from sipsimple.account import AccountManager
from sipsimple.core import SIPURI
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads.iscomposing import IsComposingMessage
from sipsimple.streams.applications.chat import CPIMMessage, CPIMParserError
from sipsimple.util import ISOTimestamp

from BlinkLogger import BlinkLogger
from SMSViewController import SMSViewController
from util import allocate_autorelease_pool, format_identity_to_string, html2txt, run_in_gui_thread


class SMSWindowController(NSWindowController):
    implements(IObserver)

    tabView = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    encryptionMenu = objc.IBOutlet()
    encryptionIconMenuItem = objc.IBOutlet()

    def initWithOwner_(self, owner):
        self= super(SMSWindowController, self).init()
        if self:
            self._owner = owner
            NSBundle.loadNibNamed_owner_("SMSSession", self)
            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="BlinkShouldTerminate")
            self.unreadMessageCounts = {}
        return self

    def selectedSessionController(self):
        activeTab = self.tabView.selectedTabViewItem()
        if activeTab:
            return activeTab.identifier()
        return None

    def updateTitle(self, display_name=None):
        title = self.getTitle(display_name=display_name)
        self.window().setTitle_(title)

    def getTitle(self, display_name=None):
        session = self.selectedSessionController()
        if session:
            sip_address = '%s@%s' % (session.target_uri.user, session.target_uri.host)
            if display_name and display_name != sip_address:
                title = u"Instant Messages with %s <%s>" % (display_name, format_identity_to_string(session.target_uri))
            else:
                title = u"Instant Messages with %s" %  format_identity_to_string(session.target_uri)
        else:
            title = u"Instant Messages"
        return title

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_BlinkShouldTerminate(self, sender, data):
        if self.window():
            self.window().orderOut_(self)

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
                display_name = '%s@%s' % (selectedSession.target_uri.user, selectedSession.target_uri.host)
                item.setHidden_(False)
                item = menu.itemWithTag_(1)
                my_fingerprint = selectedSession.otr_account.getPrivkey()
                _f = str(my_fingerprint)
                item.setTitle_(NSLocalizedString("My fingerprint is %s" % _f, "Menu item"))

                item = menu.itemWithTag_(3)
                item.setTitle_(NSLocalizedString("Always require OTR encryption with %s" % display_name, "Menu item"))

                if selectedSession.contact is not None:
                    item.setEnabled_(True)
                    item.setState_(NSOnState if selectedSession.require_encryption else NSOffState)
                    item.setHidden_(not settings.chat.enable_encryption)
                else:
                    item.setEnabled_(False)
                    item.setHidden_(True)
                    item.setState_(NSOffState)

                item = menu.itemWithTag_(4)
                if settings.chat.enable_encryption:
                    item.setHidden_(False)
                    if selectedSession.require_encryption and selectedSession.is_encrypted:
                        item.setEnabled_(False)
                    else:
                        item.setEnabled_(True)
                    item.setTitle_(NSLocalizedString("Activate OTR encryption for this session", "Menu item") if not selectedSession.is_encrypted else NSLocalizedString("Deactivate OTR encryption for this session", "Menu item"))

                else:
                    item.setEnabled_(False)
                    item.setTitle_(NSLocalizedString("OTR encryption is disabled in Chat preferences", "Menu item"))

                if settings.chat.enable_encryption:
                    ctx = selectedSession.otr_account.getContext(selectedSession.session_id)
                    fingerprint = ctx.getCurrentKey()

                    if fingerprint:
                        item = menu.itemWithTag_(6)
                        item.setHidden_(False)

                        item = menu.itemWithTag_(7)
                        item.setHidden_(False)

                        fingerprint_verified = selectedSession.otr_account.getTrust(selectedSession.remote_uri, str(fingerprint))
                        item.setEnabled_(False)
                        _t = NSLocalizedString("%s's fingerprint is " % display_name, "Menu item")
                        item.setTitle_( "%s %s" % (_t, fingerprint) if fingerprint is not None else NSLocalizedString("No Fingerprint Discovered", "Menu item"))

                        item = menu.itemWithTag_(5)
                        item.setEnabled_(True if fingerprint else False)
                        item.setHidden_(False)
                        item.setTitle_(NSLocalizedString("I have verified %s's fingerprint" % display_name, "Menu item"))
                        item.setState_(NSOnState if fingerprint_verified else NSOffState)

                        item = menu.itemWithTag_(9)
                        item.setHidden_(False)
                    else:
                        item = menu.itemWithTag_(9)
                        item.setHidden_(True)

    def noteNewMessageForSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            if self.tabView.selectedTabViewItem() == tabItem:
                item.setBadgeLabel_("")
            else:
                count = self.unreadMessageCounts[session] = self.unreadMessageCounts.get(session, 0) + 1
                item.setBadgeLabel_(str(count))

    def noteView_isComposing_(self, smsview, flag):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(smsview)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            item.setComposing_(flag)

    def addViewer_(self, viewer):
        tabItem = NSTabViewItem.alloc().initWithIdentifier_(viewer)
        tabItem.setView_(viewer.getContentView())
        sip_address = '%s@%s' % (viewer.target_uri.user, viewer.target_uri.host)
        if viewer.display_name and viewer.display_name != sip_address:
            tabItem.setLabel_("%s" % viewer.display_name)
        else:
            tabItem.setLabel_(format_identity_to_string(viewer.target_uri))
        self.tabSwitcher.addTabViewItem_(tabItem)
        self.tabSwitcher.selectLastTabViewItem_(None)
        self.updateTitle(viewer.display_name)
        self.window().makeFirstResponder_(viewer.chatViewController.inputText)

    def removeViewer_(self, viewer):
        i = self.tabView.indexOfTabViewItemWithIdentifier_(viewer)
        if i != NSNotFound:
            item = self.tabView.tabViewItemAtIndex_(i)
            self.tabSwitcher.removeTabViewItem_(item)

    @property
    def viewers(self):
        return (item.identifier() for item in self.tabView.tabViewItems())

    def close_(self, sender):
        selected = self.selectedSessionController()
        if self.unreadMessageCounts.has_key(selected):
            del self.unreadMessageCounts[selected]
        self.tabSwitcher.removeTabViewItem_(self.tabView.selectedTabViewItem())
        if self.tabView.numberOfTabViewItems() == 0:
            self.window().performClose_(None)

    def tabView_shouldCloseTabViewItem_(self, sender, item):
        if self.unreadMessageCounts.has_key(item.identifier()):
            del self.unreadMessageCounts[item.identifier()]
        return True

    def tabView_didSelectTabViewItem_(self, sender, item):
        self.updateTitle()
        if self.unreadMessageCounts.has_key(item.identifier()):
            del self.unreadMessageCounts[item.identifier()]
            self.noteNewMessageForSession_(item.identifier())
        selectedSession = self.selectedSessionController()
        if selectedSession:
            selectedSession.updateEncryptionWidgets()

    def tabViewDidChangeNumberOfTabViewItems_(self, tabView):
        if tabView.numberOfTabViewItems() == 0:
            self.window().performClose_(None)

    def tabView_didDettachTabViewItem_atPosition_(self, tabView, item, pos):
        if tabView.numberOfTabViewItems() > 1:
            session = item.identifier()
            window = SMSWindowManager().dettachSMSViewer(session)
            if window:
                window.window().setFrameOrigin_(pos)

    def windowShouldClose_(self, sender):
        for item in self.tabView.tabViewItems().copy():
            self.tabSwitcher.removeTabViewItem_(item)
        if self in SMSWindowManager().windows:
            SMSWindowManager().windows.remove(self)
            self.notification_center.remove_observer(self, name="BlinkShouldTerminate")
        return True

    @objc.IBAction
    def userClickedEncryptionMenu_(self, sender):
        # dispatch the click to the active session
        selectedSession = self.selectedSessionController()
        if selectedSession:
            selectedSession.userClickedEncryptionMenu_(sender)

    @objc.IBAction
    def toolbarButtonClicked_(self, sender):
        session = self.selectedSessionController()
        contactWindow = self._owner._owner
        if sender.itemIdentifier() == 'audio':
            contactWindow.startSessionWithTarget(format_identity_to_string(session.target_uri))
        elif sender.itemIdentifier() == 'smileys':
            chatViewController = self.selectedSessionController().chatViewController
            chatViewController.expandSmileys = not chatViewController.expandSmileys
            sender.setImage_(NSImage.imageNamed_("smiley_on" if chatViewController.expandSmileys else "smiley_off"))
            chatViewController.toggleSmileys(chatViewController.expandSmileys)
        elif sender.itemIdentifier() == 'history' and NSApp.delegate().applicationName != 'Blink Lite':
            contactWindow.showHistoryViewer_(None)
            contactWindow.historyViewer.filterByURIs((format_identity_to_string(session.target_uri),))

    @objc.IBAction
    def printDocument_(self, sender):
        if NSApp.delegate().applicationName == 'Blink Lite':
            return

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
        print_view = self.selectedSessionController().chatViewController.outputView
        print_view.mainFrame().frameView().documentView().print_(self)

SMSWindowManagerInstance = None

def SMSWindowManager():
    global SMSWindowManagerInstance
    if SMSWindowManagerInstance is None:
        SMSWindowManagerInstance = SMSWindowManagerClass.alloc().init()
    return SMSWindowManagerInstance


class SMSWindowManagerClass(NSObject):
    implements(IObserver)

    #__metaclass__ = Singleton

    windows = []
    received_call_ids = set()

    def init(self):
        self = super(SMSWindowManagerClass, self).init()
        if self:
            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="SIPEngineGotMessage")
        return self

    def setOwner_(self, owner):
        self._owner = owner

    def openMessageWindow(self, target, target_name, account, create_if_needed=True, note_new_message=True):
        for window in self.windows:
            for viewer in window.viewers:
                if viewer.matchesTargetAccount(target, account):
                    break
            else:
                continue
            break
        else:
            window, viewer = None, None

        if not viewer and create_if_needed:
            viewer = SMSViewController.alloc().initWithAccount_target_name_(account, target, target_name)
            if not self.windows:
                window = SMSWindowController.alloc().initWithOwner_(self)
                self.windows.append(window)
            else:
                window = self.windows[0]
            viewer.windowController = window
            window.addViewer_(viewer)
        elif viewer:
            window = self.windowForViewer(viewer)

        if window:
            if note_new_message:
                window.window().makeKeyAndOrderFront_(None)
                NSApp.delegate().noteNewMessage(window)

        return viewer

    def dettachSMSViewer(self, viewer):
        oldWindow = self.windowForViewer(viewer)
        oldWindow.removeViewer_(viewer)
        window = SMSWindowController.alloc().initWithOwner_(self)
        self.windows.append(window)
        window.addViewer_(viewer)
        window.window().makeKeyAndOrderFront_(None)
        return window

    def windowForViewer(self, viewer):
        for window in self.windows:
            if viewer in window.viewers:
                return window
        else:
            return None

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPEngineGotMessage(self, sender, data):
        account = AccountManager().find_account(data.request_uri)
        if not account:
            BlinkLogger().log_warning(u"Could not find local account for incoming SMS to %s, using default" % data.request_uri)
            account = AccountManager().default_account

        call_id = data.headers.get('Call-ID', Null).body
        try:
            self.received_call_ids.remove(call_id)
        except KeyError:
            self.received_call_ids.add(call_id)
        else:
            # drop duplicate message received
            return

        is_cpim = False
        cpim_message = None
        is_replication_message = False

        if data.content_type == 'message/cpim':
            try:
                cpim_message = CPIMMessage.parse(data.body)
            except CPIMParserError:
                BlinkLogger().log_warning(u"Incoming SMS from %s to %s has invalid CPIM content" % format_identity_to_string(data.from_header), account.id)
                return
            else:
                is_cpim = True
                body = cpim_message.body
                content_type = cpim_message.content_type
                sender_identity = cpim_message.sender or data.from_header
                if cpim_message.sender and data.from_header.uri == data.to_header.uri and data.from_header.uri == cpim_message.sender.uri:
                    is_replication_message = True
                    window_tab_identity = cpim_message.recipients[0] if cpim_message.recipients else data.to_header
                else:
                    window_tab_identity = data.from_header
        else:
            body = data.body.decode('utf-8')
            content_type = data.content_type
            sender_identity = data.from_header
            window_tab_identity = sender_identity

        is_html = content_type == 'text/html'

        if content_type in ('text/plain', 'text/html'):
            pass
            #BlinkLogger().log_info(u"Incoming SMS %s from %s to %s received" % (call_id, format_identity_to_string(sender_identity), account.id))
        elif content_type == 'application/im-iscomposing+xml':
            # body must not be utf-8 decoded
            body = cpim_message.body if is_cpim else data.body
            msg = IsComposingMessage.parse(body)
            state = msg.state.value
            refresh = msg.refresh.value if msg.refresh is not None else None
            content_type = msg.content_type.value if msg.content_type is not None else None
            last_active = msg.last_active.value if msg.last_active is not None else None

            viewer = self.openMessageWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, create_if_needed=False, note_new_message=False)
            if viewer:
                viewer.gotIsComposing(self.windowForViewer(viewer), state, refresh, last_active)
            return
        else:
            BlinkLogger().log_warning(u"Incoming SMS %s from %s to %s has unknown content-type %s" % (call_id, format_identity_to_string(data.from_header), account.id, data.content_type))
            return

        # display the message
        note_new_message = False if is_replication_message else True
        viewer = self.openMessageWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, note_new_message=note_new_message)
        self.windowForViewer(viewer).noteNewMessageForSession_(viewer)
        replication_state = None
        replication_timestamp = None

        if is_replication_message:
            replicated_response_code = data.headers.get('X-Replication-Code', Null).body
            if replicated_response_code == '202':
                replication_state = 'deferred'
            elif replicated_response_code == '200':
                replication_state = 'delivered'
            else:
                replication_state = 'failed'
            replicated_timestamp = data.headers.get('X-Replication-Timestamp', Null).body
            try:
                replication_timestamp = ISOTimestamp(replicated_timestamp)
            except Exception:
                replication_timestamp = ISOTimestamp.now()

        window = self.windowForViewer(viewer).window()
        viewer.gotMessage(sender_identity, call_id, body, is_html, is_replication_message, replication_timestamp, window=window)
        self.windowForViewer(viewer).noteView_isComposing_(viewer, False)
