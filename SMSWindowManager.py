# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSPortraitOrientation, NSFitPagination, NSOffState, NSOnState, NSControlTextDidChangeNotification, NSEventTrackingRunLoopMode

from Foundation import (NSBundle,
                        NSImage,
                        NSLocalizedString,
                        NSNotFound,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSObject,
                        NSColor,
                        NSPrintInfo,
                        NSTabViewItem,
                        NSNotificationCenter,
                        NSWindowController)

import objc
import re
import hashlib
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from binascii import unhexlify, hexlify

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from sipsimple.account import AccountManager
from sipsimple.core import SIPURI
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads.iscomposing import IsComposingMessage
from sipsimple.streams.msrp.chat import CPIMPayload, CPIMParserError
from sipsimple.util import ISOTimestamp

from BlinkLogger import BlinkLogger
from SMSViewController import SMSViewController
from util import format_identity_to_string, html2txt, run_in_gui_thread

unpad = lambda s: s[:-ord(s[len(s) - 1:])]


@implementer(IObserver)
class SMSWindowController(NSWindowController):

    tabView = objc.IBOutlet()
    tabSwitcher = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    encryptionMenu = objc.IBOutlet()
    encryptionIconMenuItem = objc.IBOutlet()
    import_key_window = None

    def initWithOwner_(self, owner):
        self = objc.super(SMSWindowController, self).init()
        if self:
            self._owner = owner
            NSBundle.loadNibNamed_owner_("SMSSession", self)
            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="BlinkShouldTerminate")
            self.notification_center.add_observer(self, name="ChatStreamOTREncryptionStateChanged")
            self.notification_center.add_observer(self, name="OTREncryptionDidStop")
            
            self.unreadMessageCounts = {}
        return self

    @objc.python_method
    def selectedSessionController(self):
        activeTab = self.tabView.selectedTabViewItem()
        if activeTab:
            return activeTab.identifier()
        return None

    @property
    def titleLong(self):
        session = self.selectedSessionController()
        if session:
            display_name = session.display_name
            sip_address = '%s@%s' % (session.target_uri.user.decode(), session.target_uri.host.decode())
            if display_name and display_name != sip_address:
                title = NSLocalizedString("Short Messages with %s", "Window Title") % display_name +  " <%s>" % format_identity_to_string(session.target_uri)
            else:
                title = NSLocalizedString("Short Messages with %s", "Window Title") %  format_identity_to_string(session.target_uri)
        else:
            title = NSLocalizedString("Short Messages", "Window Title")
        return title

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_BlinkShouldTerminate(self, sender, data):
        if self.window():
            self.window().orderOut_(self)

    @objc.python_method
    def _NH_ChatStreamOTREncryptionStateChanged(self, sender, data):
        self.updateEncryptionWidgets()

    @objc.python_method
    def _NH_OTREncryptionDidStop(self, sender, data):
        self.updateEncryptionWidgets()

    def menuWillOpen_(self, menu):
        pass

    def noteNewMessageForSession_(self, session):
        index = self.tabView.indexOfTabViewItemWithIdentifier_(session)
        if index == NSNotFound:
            return
        tabItem = self.tabView.tabViewItemAtIndex_(index)
        item = self.tabSwitcher.itemForTabViewItem_(tabItem)
        if item:
            if self.tabView.selectedTabViewItem() == tabItem:
                item.setBadgeLabel_("")
                session = self.selectedSessionController()
                session.notifyReadMessages()
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
        sip_address = '%s@%s' % (viewer.target_uri.user.decode(), viewer.target_uri.host.decode())
        if viewer.display_name and viewer.display_name != sip_address:
            tabItem.setLabel_("%s" % viewer.display_name)
        else:
            tabItem.setLabel_(format_identity_to_string(viewer.target_uri))
        self.tabSwitcher.addTabViewItem_(tabItem)
        self.tabSwitcher.selectLastTabViewItem_(None)
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
        if selected in self.unreadMessageCounts:
            del self.unreadMessageCounts[selected]
        self.tabSwitcher.removeTabViewItem_(self.tabView.selectedTabViewItem())
        if self.tabView.numberOfTabViewItems() == 0:
            self.window().performClose_(None)

    def tabView_shouldCloseTabViewItem_(self, sender, item):
        if item.identifier() in self.unreadMessageCounts:
            del self.unreadMessageCounts[item.identifier()]
        return True

    def tabView_didSelectTabViewItem_(self, sender, item):
        self.window().setTitle_(self.titleLong)
        if item.identifier() in self.unreadMessageCounts:
            del self.unreadMessageCounts[item.identifier()]
            self.noteNewMessageForSession_(item.identifier())
        selectedSession = self.selectedSessionController()
        selectedSession.notifyReadMessages()
        self.updateEncryptionWidgets(selectedSession)
    
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
    def toolbarButtonClicked_(self, sender):
        session = self.selectedSessionController()
        contactWindow = self._owner._owner
        if sender.itemIdentifier() == 'audio':
            contactWindow.startSessionWithTarget(format_identity_to_string(session.target_uri))
        elif sender.itemIdentifier() == 'video':
            contactWindow.startSessionWithTarget(format_identity_to_string(session.target_uri), media_type="video")
        elif sender.itemIdentifier() == 'smileys':
            chatViewController = self.selectedSessionController().chatViewController
            chatViewController.expandSmileys = not chatViewController.expandSmileys
            sender.setImage_(NSImage.imageNamed_("smiley_on" if chatViewController.expandSmileys else "smiley_off"))
            chatViewController.toggleSmileys(chatViewController.expandSmileys)
        elif sender.itemIdentifier() == 'history' and NSApp.delegate().history_enabled:
            contactWindow.showHistoryViewer_(None)
            contactWindow.historyViewer.filterByURIs((format_identity_to_string(session.target_uri),))

    @objc.IBAction
    def userClickedEncryptionMenu_(self, sender):
        # dispatch the click to the active session
        session = self.selectedSessionController()
        if session:
            session.userClickedEncryptionMenu_(sender)

    def menuWillOpen_(self, menu):
        if menu == self.encryptionMenu:
            settings = SIPSimpleSettings()
            item = menu.itemWithTag_(1)
            item.setHidden_(not settings.chat.enable_encryption)

            item = menu.itemWithTag_(2)
            item.setEnabled_(False)
            item.setState_(NSOffState)

            item = menu.itemWithTag_(3)
            item.setHidden_(True)
            item.setState_(NSOffState)

            item = menu.itemWithTag_(4)
            item.setHidden_(True)

            item = menu.itemWithTag_(5)
            item.setHidden_(True)

            item = menu.itemWithTag_(6)
            item.setHidden_(True)

            selectedSession = self.selectedSessionController()
            if selectedSession:
                chat_stream = selectedSession.encryption
                display_name = selectedSession.display_name
                item = menu.itemWithTag_(1)
                if settings.chat.enable_encryption:
                    item.setHidden_(False)
                    item.setEnabled_(True)
                    item.setTitle_(NSLocalizedString("Activate OTR encryption for this session", "Menu item") if not chat_stream.active else NSLocalizedString("Deactivate OTR encryption for this session", "Menu item"))

                item = menu.itemWithTag_(2)
                item.setHidden_(False)
                if chat_stream.active:
                    item.setTitle_(NSLocalizedString("My fingerprint is %s", "Menu item") % str(chat_stream.key_fingerprint))

                else:
                    item.setEnabled_(False)
                    item.setTitle_(NSLocalizedString("OTR encryption is disabled in Chat preferences", "Menu item"))

                if settings.chat.enable_encryption:
                    if chat_stream.peer_fingerprint:
                        item = menu.itemWithTag_(3)
                        item.setHidden_(False)

                        item = menu.itemWithTag_(4)
                        item.setHidden_(False)
                        item.setEnabled_(False)

                        _t = NSLocalizedString("%s's fingerprint is ", "Menu item") % display_name
                        item.setTitle_( "%s %s" % (_t, chat_stream.peer_fingerprint))
                        
                        item = menu.itemWithTag_(5)
                        item.setHidden_(False)
                        item.setState_(NSOnState if chat_stream.verified else NSOffState)

                        item = menu.itemWithTag_(6)
                        item.setEnabled_(True)
                        item.setHidden_(False)
                        item.setTitle_(NSLocalizedString("Validate the identity of %s" % display_name, "Menu item"))


    @objc.python_method
    def updateEncryptionWidgets(self, selectedSession=None):
        if selectedSession is None:
            selectedSession = self.selectedSessionController()

        if selectedSession and selectedSession.started:
            if selectedSession.encryption.active:
                if selectedSession.encryption.verified:
                    self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-green"))
                else:
                    self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("locked-red"))
            else:
                self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))
        else:
            self.encryptionIconMenuItem.setImage_(NSImage.imageNamed_("unlocked-darkgray"))


    @objc.IBAction
    def printDocument_(self, sender):
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


@implementer(IObserver)
class SMSWindowManagerClass(NSObject):

    #__metaclass__ = Singleton

    windows = []
    received_call_ids = set()

    def init(self):
        self = objc.super(SMSWindowManagerClass, self).init()
        if self:
            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name="SIPEngineGotMessage")
        return self

    def setOwner_(self, owner):
        self._owner = owner

    @objc.python_method
    def raiseLastWindowFront(self):
        try:
            window = self.windows[0]
        except IndexError:
            return False

        window.window().makeKeyAndOrderFront_(None)
        return True

    @objc.python_method
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

    @objc.python_method
    def dettachSMSViewer(self, viewer):
        oldWindow = self.windowForViewer(viewer)
        oldWindow.removeViewer_(viewer)
        window = SMSWindowController.alloc().initWithOwner_(self)
        self.windows.append(window)
        window.addViewer_(viewer)
        window.window().makeKeyAndOrderFront_(None)
        return window

    @objc.python_method
    def windowForViewer(self, viewer):
        for window in self.windows:
            if viewer in window.viewers:
                return window
        else:
            return None

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_SIPEngineGotMessage(self, sender, data):
        account = AccountManager().find_account(data.request_uri)
        if not account:
            BlinkLogger().log_warning("Could not find local account for incoming SMS to %s, using default" % data.request_uri)
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
        imdn_id = None
        imdn_timestamp = None
        
        if data.content_type == 'message/cpim':
            try:
                cpim_message = CPIMPayload.decode(data.body)
            except CPIMParserError:
                BlinkLogger().log_warning("Incoming SMS from %s to %s has invalid CPIM content" % format_identity_to_string(data.from_header), account.id)
                return
            else:
                is_cpim = True
                content = cpim_message.content
                content_type = cpim_message.content_type
                sender_identity = cpim_message.sender or data.from_header
                cpim_imdn_events = None
                imdn_timestamp = cpim_message.timestamp
                for h in cpim_message.additional_headers:
                    if h.name == "Message-ID":
                        imdn_id = h.value
                    if h.name == "Disposition-Notification":
                        cpim_imdn_events = h.value

                if imdn_id:
                    if cpim_imdn_events and 'positive-delivery' in cpim_imdn_events and imdn_timestamp:
                        BlinkLogger().log_info("Will send IMDN delivery notification for %s" % imdn_id)
                    else:
                        imdn_id = None

                if cpim_message.sender and data.from_header.uri == data.to_header.uri and data.from_header.uri == cpim_message.sender.uri:
                    is_replication_message = True
                    window_tab_identity = cpim_message.recipients[0] if cpim_message.recipients else data.to_header
                else:
                    window_tab_identity = data.from_header
        else:
            content = data.body
            content_type = data.content_type
            sender_identity = data.from_header
            window_tab_identity = sender_identity

        if content_type in ('text/plain', 'text/html'):
            BlinkLogger().log_info(u"Incoming SMS %s from %s to %s received" % (call_id, format_identity_to_string(sender_identity), account.id))
        elif content_type in ('text/rsa-public-key'):
            BlinkLogger().log_info(u"Public key from %s received" % (format_identity_to_string(sender_identity)))
            BlinkLogger().log_info(content);
            return
        elif content_type in ('text/rsa-private-key'):
            BlinkLogger().log_info(u"Private key from %s to %s received" % (data.from_header.uri, account.id))

            if account.id == str(data.from_header.uri).split(":")[1]:
                self.import_key_window = ImportPrivateKeyController(account, content);
                self.import_key_window.show()
            return
        elif content_type == 'application/im-iscomposing+xml':
            content = cpim_message.content if is_cpim else data.body
            msg = IsComposingMessage.parse(content)
            state = msg.state.value
            refresh = msg.refresh.value if msg.refresh is not None else None
            content_type = msg.content_type.value if msg.content_type is not None else None
            last_active = msg.last_active.value if msg.last_active is not None else None

            viewer = self.openMessageWindow(SIPURI.new(window_tab_identity.uri), window_tab_identity.display_name, account, create_if_needed=False, note_new_message=False)
            if viewer:
                viewer.gotIsComposing(self.windowForViewer(viewer), state, refresh, last_active)
            return
        else:
            BlinkLogger().log_warning("Incoming SMS %s from %s to %s has unknown content-type %s" % (call_id, format_identity_to_string(data.from_header), account.id, data.content_type))
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
        viewer.gotMessage(sender_identity, call_id, content, content_type, is_replication_message, replication_timestamp, window=window, imdn_id=imdn_id, imdn_timestamp=imdn_timestamp)
        
        self.windowForViewer(viewer).noteView_isComposing_(viewer, False)


class ImportPrivateKeyController(NSObject):
    window = objc.IBOutlet()
    checksum = objc.IBOutlet()
    pincode = objc.IBOutlet()
    status = objc.IBOutlet()
    importButton = objc.IBOutlet()
    publicKey = None
    privateKey = None
    dealloc_timer = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, account, encryptedKeyPair):
        NSBundle.loadNibNamed_owner_("ImportPrivateKeyWindow", self)
        self.account = account;
        self.encryptedKeyPair = encryptedKeyPair;

        self.checksum.setStringValue_('');
        self.importButton.setEnabled_(False)
        self.window.makeFirstResponder_(self.pincode)
        self.status.setTextColor_(NSColor.blackColor())

        self.status.setStringValue_(NSLocalizedString("Enter pincode to decrypt the key", "status label"));

    @objc.python_method
    def get_private_key(self, pincode):
        salt = b'sylksalt';
        return PBKDF2(pincode, salt, 32, 4096)
        
    @objc.python_method
    def decrypt(self, data, pincode):
        password = self.get_private_key(pincode)
        data = unhexlify(data)
        iv = data[:16]
        cipher = AES.new(password, AES.MODE_CBC, iv)
        return unpad(cipher.decrypt(data[16:]))
    
    @objc.IBAction
    def importButtonClicked_(self, sender):
        BlinkLogger().log_info("Import private key")
        pincode = str(self.pincode.stringValue()).strip()
        data = self.encryptedKeyPair.decode()
        keyPair = self.decrypt(data, pincode)

        try:
            keyPair = keyPair.decode()
        except UnicodeDecodeError as e:
            self.status.setTextColor_(NSColor.redColor())
            BlinkLogger().log_error("Import private key failed: %s" % str(e))
            self.status.setStringValue_(NSLocalizedString("Key import failed", "status label"));
        else:
            public_key_checksum_match = re.findall(r"--PUBLIC KEY SHA1 CHECKSUM--(\w+)--",  keyPair)
            private_key_checksum_match = re.findall(r"--PRIVATE KEY SHA1 CHECKSUM--(\w+)--",  keyPair)
            
            if (public_key_checksum_match):
                public_key_checksum = public_key_checksum_match[0]
            else:
                public_key_checksum = None

            if (private_key_checksum_match):
                private_key_checksum = private_key_checksum_match[0]
            else:
                private_key_checksum = None

            public_key = ''
            private_key = ''

            start_public = False
            start_private = False

            for l in keyPair.split("\n"):
                if l == "-----BEGIN RSA PUBLIC KEY-----":
                    start_public = True
                    start_private = False

                if l == "-----END RSA PUBLIC KEY-----":
                    public_key = public_key + l
                    start_public = False
                    start_private = False

                if l == "-----BEGIN RSA PRIVATE KEY-----":
                    start_public = False
                    start_private = True

                if l == "-----END RSA PRIVATE KEY-----":
                    private_key = private_key + l
                    start_public = False
                    start_private = False

                if start_public:
                    public_key = public_key + l + '\n'

                if start_private:
                    private_key = private_key + l + '\n'
                    
            if (public_key and private_key and public_key_checksum):
                self.importButton.setEnabled_(False)
                BlinkLogger().log_info("Key imported sucessfully")
                self.status.setTextColor_(NSColor.greenColor())
                self.status.setStringValue_(NSLocalizedString("Key imported sucessfully", "status label"));
                self.checksum.setStringValue_(public_key_checksum);

                self.account.sms.private_key = private_key
                self.account.sms.public_key = public_key
                self.account.sms.public_key_checksum = public_key_checksum
                self.account.save()

                if self.dealloc_timer is None:
                    self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(6.0, self, "deallocTimer:", None, True)
                    NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
                    NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)
                
            else:
                BlinkLogger().log_error("Key import failed")
                self.status.setStringValue_(NSLocalizedString("Key import failed", "status label"));
                self.status.setTextColor_(NSColor.redColor())

    def deallocTimer_(self, timer):
        self.dealloc_timer.invalidate()
        self.dealloc_timer = None
        self.close()

    def controlTextDidChange_(self, notification):
        pincode = str(self.pincode.stringValue()).strip()
        self.importButton.setEnabled_(len(pincode)==6)

    @objc.IBAction
    def cancelButtonClicked_(self, sender):
        self.close()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)

    def close(self):
        self.window.close()
